"""QSFP (CGE 100G) loopback BER tests: every board tests its own QSFP lanes at the same time.

Each board sends PRBS-31 on QSFP TX lane n and checks it on its own RX lane n through a loopback plug
(passive copper, or an optical module with a loopback fibre). Needs the crs_ct bitstream
(FPGAArray mode='shuffle8') with the CGE TX drive control bytes.

Results are keyed by (slot, lane). Long functions take optional callbacks: log(str), progress(done, total),
partial(obj) for live display, and stop() -> bool to abort.
"""
import time

import numpy as np

BIT_RATE = 25.78125e9          # per lane (100GBASE-R4)
PRBS31 = 5
LANES = range(4)


def _noop(*a, **k):
    return False


def qsfp_boards(ca):
    """Boards with a CGE that has the TX drive control bytes, sorted by slot."""
    return sorted((ib for ib in ca.ib if getattr(ib, 'GPU', None) is not None and ib.GPU.tx_drive_supported()),
                  key=lambda ib: ib.slot)


def read_plugs(ca, log=_noop):
    """Identify what is in each QSFP cage: {slot: dict(serial, vendor, part, kind, si...)}. Never raises."""
    out = {}
    for ib in sorted(ca.ib, key=lambda ib: ib.slot):
        q = ib.i2c1_qsfp
        r = dict(serial=ib.serial)
        try:
            r['present'] = bool(q.is_present())
            r['vendor'] = bytes(q.read(148, 16)).decode(errors='replace').strip()
            r['part'] = bytes(q.read(168, 16)).decode(errors='replace').strip()
            tech = q.read(147)[0] >> 4                  # SFF-8636 byte 147 bits 7-4: transmitter technology
            r['kind'] = ('copper' if tech >= 10 or 'LOOPBACK' in r['part'].upper()   # passive plugs often report 1000 (other)
                         else 'unknown' if tech == 8 else 'optical')
            r.update(q.get_si_capabilities())
            if r.get('paged') and r.get('tx_eq_programmable'):
                r.update(tx_eq_db=q.get_tx_input_eq(), rx_emphasis_db=q.get_rx_output_emphasis(),
                         tx_adaptive_eq=q.get_tx_adaptive_eq())
        except Exception as e:
            r['error'] = repr(e)
        try:
            r['fpga_temp_c'] = float(ib.SYSMON.temperature())
        except Exception:
            pass
        out[ib.slot] = r
        log(f"slot {ib.slot} SN{ib.serial}: {r.get('vendor', '?')} {r.get('part', '?')} ({r.get('kind', r.get('error', '?'))})")
    return out


def _each_lane(boards, fn):
    for ib in boards:
        for lane in LANES:
            fn(ib, lane)


def set_prbs(boards, tx=PRBS31):
    """PRBS-31 checker on all lanes; PRBS-31 (tx=5) or no pattern (tx=0) from all transmitters.
    Control byte 7 is copied to the selected lane, so every lane gets the same value."""
    def f(ib, lane):
        ib.GPU.select_gt_lane(lane)
        ib.GPU.TXPRBSSEL = tx
        ib.GPU.RXPRBSSEL = PRBS31
    _each_lane(boards, f)


def set_tx(boards, swing, pre, post):
    def f(ib, lane):
        ib.GPU.write_control(8 + 3 * lane, bytes([swing, pre, post]))   # checked once in qsfp_boards()
    _each_lane(boards, f)


def lpm_by_plug(plugs):
    """'Auto' receiver mode: LPM behind an optical module, DFE behind a copper plug (measured on CRS rev5:
    passive copper loopbacks fail in LPM, optical SR4 modules are best in LPM). {slot: bool}."""
    return {slot: p.get('kind') == 'optical' for slot, p in plugs.items()}


def set_rx_lpm(boards, lpm):
    """lpm: True (LPM), False (DFE), or {slot: bool} per board."""
    for ib in boards:
        ib.GPU.set_rx_lpm(lpm.get(ib.slot, False) if isinstance(lpm, dict) else lpm)


def _counts(boards):
    out = {}
    for ib in boards:
        for lane in LANES:
            ch = ib.GPU.gtx[lane]
            ch.select()
            try:
                out[(ib.slot, lane)] = ch.get_prbs_error_ctr()
            finally:
                ch.unselect()
    return out


def rearm(boards):
    """Reset the PRBS error counters. A checker that lost the pattern (lock proof, stopped lane) only locks again
    after this reset."""
    def clear(ib, lane):
        ib.GPU.select_gt_lane(lane)
        ib.GPU.RXPRBSCNTRESET = 1
        ib.GPU.RXPRBSCNTRESET = 0
    _each_lane(boards, clear)


def measure(boards, dwell=1.0, train=2.0, lock_proof=True):
    """Retrain the RX equalizers, wait `train` s, clear the counters, count PRBS-31 errors for `dwell` s.
    Lock proof: stop the transmitted pattern for 0.3 s; a locked checker must count errors.
    Returns {(slot, lane): dict(errors, ber, locked)}; ber of 0 errors is the 1/bits floor."""
    def retrain(ib, lane):
        ib.GPU.select_gt_lane(lane)
        ib.GPU.RXDFELPMRESET = 1
        ib.GPU.RXDFELPMRESET = 0
    _each_lane(boards, retrain)
    time.sleep(train)

    rearm(boards)
    c0 = _counts(boards)
    t0 = time.time()
    time.sleep(dwell)
    c1 = _counts(boards)
    bits = BIT_RATE * (time.time() - t0)
    locked = {k: True for k in c0}
    if lock_proof:
        set_prbs(boards, 0)
        p0 = _counts(boards)
        time.sleep(0.3)
        p1 = _counts(boards)
        set_prbs(boards, PRBS31)
        locked = {k: p1[k] != p0[k] for k in p0}
    out = {}
    for k in c0:
        e = (c1[k] - c0[k]) & 0xFFFFFFFF
        out[k] = dict(errors=e, ber=max(e, 1) / bits if locked[k] else 0.5, zero=(e == 0 and locked[k]), locked=locked[k])
    return out


def check_loopback(boards, swing=0, pre=0, post=21, lpm=False, log=_noop):
    """Check that TX lane n reaches RX lane n of the same board. The pattern of one TX lane at a time is stopped
    (on every board at once); an RX lane is hit if its BER during the stop is above 0.1 (a stopped pattern gives
    about 0.5). Lanes without lock or with a BER above 0.05 at this setting are reported, not judged.
    Returns {(slot, lane): dict(rx_lanes_hit, background_ber, ok)}."""
    set_prbs(boards)
    set_rx_lpm(boards, lpm)
    set_tx(boards, swing, pre, post)
    # An unlocked checker counts nothing, so its lane cannot be judged: find those first with the lock proof.
    nolock = {k for k, v in measure(boards, dwell=0.2).items() if not v['locked']}
    rearm(boards)
    time.sleep(0.2)
    out = {}
    for lane in LANES:
        t0 = time.time(); c0 = _counts(boards); time.sleep(0.3); c1 = _counts(boards)
        bits = BIT_RATE * (time.time() - t0)
        bg = {k: ((c1[k] - c0[k]) & 0xFFFFFFFF) / bits for k in c0}
        # Byte 7 (TXPRBSSEL) is copied to whichever lane is selected, and reading a counter selects its lane.
        # So lane `lane` stays selected from the stop until its counter is read and its pattern is restored;
        # the other lanes are read afterwards, with the pattern on.
        c0 = _counts(boards)
        t0 = time.time()
        for ib in boards:
            ib.GPU.select_gt_lane(lane); ib.GPU.TXPRBSSEL = 0
        time.sleep(0.3)
        c1 = {}
        for ib in boards:
            ch = ib.GPU.gtx[lane]
            ch.select()
            try:
                c1[(ib.slot, lane)] = ch.get_prbs_error_ctr()
            finally:
                ch.unselect()
        bits_stop = BIT_RATE * (time.time() - t0)
        for ib in boards:
            ib.GPU.select_gt_lane(lane); ib.GPU.TXPRBSSEL = PRBS31
        rest = _counts(boards)
        bits_rest = BIT_RATE * (time.time() - t0)
        for k, v in rest.items():
            c1.setdefault(k, v)
        bits = {k: bits_stop if k[1] == lane else bits_rest for k in c1}
        for ib in boards:
            ber = [((c1[(ib.slot, l)] - c0[(ib.slot, l)]) & 0xFFFFFFFF) / bits[(ib.slot, l)] for l in LANES]
            hit = [l for l in LANES if ber[l] > 0.1 and bg[(ib.slot, l)] < 0.05]
            bad = [l for l in LANES if bg[(ib.slot, l)] >= 0.05 or (ib.slot, l) in nolock]
            ok = hit == [lane]
            out[(ib.slot, lane)] = dict(rx_lanes_hit=hit, background_ber=[bg[(ib.slot, l)] for l in LANES],
                                        not_judged=bad, ok=ok)
            log(f"slot {ib.slot} SN{ib.serial} TX lane {lane} stopped -> RX lanes hit {hit}"
                + ('' if ok or lane in bad else '  CHECK') + (f' (no lock or BER > 0.05 at this setting on RX lanes {bad}: not judged)' if bad else ''))
        set_prbs(boards)
        rearm(boards)
        time.sleep(0.2)
    return out


def sweep_tx(boards, swings, pres, posts, dwell=1.0, lpm=True, train=2.0,
             log=_noop, progress=_noop, partial=_noop, stop=_noop):
    """FPGA TX grid on every board at once. Returns dict(swings, pres, posts, lpm, dwell,
    ber={(slot, lane): array[len(swings), len(pres), len(posts)]}, zero=..., locked=..., temps=[...]).
    partial(result) is called after each TX pre-cursor row."""
    set_prbs(boards)
    set_rx_lpm(boards, lpm)
    shape = (len(swings), len(pres), len(posts))
    keys = [(ib.slot, lane) for ib in boards for lane in LANES]
    res = dict(swings=list(swings), pres=list(pres), posts=list(posts), lpm=lpm, dwell=dwell,
               serials={ib.slot: ib.serial for ib in boards},
               ber={k: np.full(shape, np.nan) for k in keys}, zero={k: np.zeros(shape, bool) for k in keys},
               errors={k: np.full(shape, -1, np.int64) for k in keys}, temps=[], t_start=time.time(), stopped=False)
    total, done = int(np.prod(shape)), 0
    for a, s in enumerate(swings):
        for b, pr in enumerate(pres):
            for c, po in enumerate(posts):
                if stop():
                    log('sweep stopped')
                    res['stopped'] = True
                    return res
                set_tx(boards, s, pr, po)
                m = measure(boards, dwell=dwell, train=train)
                for k, v in m.items():
                    res['ber'][k][a, b, c] = v['ber']
                    res['zero'][k][a, b, c] = v['zero']
                    res['errors'][k][a, b, c] = v['errors'] if v['locked'] else -1
                done += 1
                progress(done, total)
            partial(res)
        res['temps'].append(dict(time=time.time(), fpga_c={ib.slot: _temp(ib) for ib in boards}))
    log(f'sweep done: {total} points, {time.time() - res["t_start"]:.0f} s')
    return res


def _temp(ib):
    try:
        return float(ib.SYSMON.temperature())
    except Exception:
        return None


def scan_eyes(boards, h=range(-32, 33, 4), v=range(-50, 51, 5), max_prescale=5, log=_noop, progress=_noop, stop=_noop):
    """Eye diagram of every lane at the present TX / RX settings (DFE or LPM from RXLPMEN).
    Returns {(slot, lane): ber_map[len(h), len(v)]}."""
    h, v = list(h), list(v)
    for ib in boards:
        ib.GPU.enable_eye_scan()                         # RX datapath reset and per-lane phase calibration
    set_prbs(boards)
    out, total = {}, len(boards) * 4
    for ib in boards:
        for lane in LANES:
            if stop():
                return out
            out[(ib.slot, lane)] = ib.GPU.gtx[lane].get_eye_diagram(h, v, max_prescale=max_prescale).ber_map
            progress(len(out), total)
        log(f'eyes done: slot {ib.slot} SN{ib.serial}')
    return out


def best_index(zero, ber):
    """Index of the best grid point. Among 0-error points: the one farthest (in grid steps, same swing) from any
    point with errors; ties go to the point nearest the centre of the pre/post grid. Without any 0-error point:
    the lowest BER."""
    if not zero.any():
        return np.unravel_index(np.nanargmin(np.where(np.isnan(ber), 1.0, ber)), ber.shape)
    n_pre, n_post = zero.shape[1:]
    best, key = None, None
    for a in range(zero.shape[0]):
        bad = np.argwhere(~zero[a])
        for b, c in np.argwhere(zero[a]):
            d = np.hypot(bad[:, 0] - b, bad[:, 1] - c).min() if len(bad) else np.inf
            k = (d, -np.hypot(b - (n_pre - 1) / 2, c - (n_post - 1) / 2))
            if key is None or k > key:
                best, key = (a, b, c), k
    return best


def summarize(res):
    """Per (slot, lane): number of 0-error settings and the best setting (swing, pre, post codes)."""
    out = {}
    for k, z in res['zero'].items():
        i = best_index(z, res['ber'][k])
        out[k] = dict(n_zero=int(z.sum()), n_total=int(z.size), best_zero=bool(z[i]),
                      best=(res['swings'][i[0]], res['pres'][i[1]], res['posts'][i[2]]))
    return out


if __name__ == '__main__':                               # offline check of best_index
    zero = np.zeros((1, 5, 5), bool)
    zero[0, 1:4, 1:4] = True
    zero[0, 0, 4] = True                                 # isolated 0-error point must not win
    assert best_index(zero, np.ones(zero.shape)) == (0, 2, 2)
    zero = np.ones((1, 5, 7), bool)
    zero[0, 4, 6] = False                                # one failing corner: best is far from it
    assert best_index(zero, np.ones(zero.shape)) == (0, 0, 0)
    assert best_index(np.ones((1, 5, 5), bool), np.ones((1, 5, 5))) == (0, 2, 2)   # all good: centre
    ber = np.full((2, 2, 2), 1e-3); ber[1, 0, 1] = 1e-9
    assert best_index(np.zeros(ber.shape, bool), ber) == (1, 0, 1)
    print('qsfp_ber_tests self-check pass')
