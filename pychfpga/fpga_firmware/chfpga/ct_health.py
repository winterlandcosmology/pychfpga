"""Backplane and corner-turn health measurement for a populated ICE crate.

This is the measurement that answers "are the backplane links and both shuffle
crossbars error-free on every board", expressed as a list of human-readable
failure strings (empty list == healthy). It is shared by the firmware test
suite (``firmware_tests/ct_shuffle256_test.py``) and the crate pre-deployment
QC suite (``qc/ICE_crate/f_engine_tests.py``) so that both judge crate health
by the same criteria.

What is and is not consulted, and why, is load-bearing - several registers on
these crossbars look meaningful and are not:

- Backplane link health is ``gtx[n].BLOCK_LOCK`` (64b66b sync). ``RX_RESETDONE``
  is timer-based and asserts with no cable attached, so it is never used.
- ``MISSING_FRAME`` is the real lane-absent flag. ``DATA_TIMEOUT_MON``,
  ``HAD_TIMEOUT``, ``CAPTURE_DONE`` and ``TIMEOUT_PERIOD`` are not driven by
  the firmware and are not read here.
- ``PACKET_ERROR_CTR`` is indexed by ``LANE_MONITOR_SEL`` and only selections
  0..7 are valid; higher selections read out of range.
- Both crossbars' packet-error counters are asserted on. (On bitstreams
  before chfpga_ice_ct_pbg_sep24 the CROSSBAR3 counter saturated
  unconditionally - unread sublane FIFOs overflowed into the packet-flags
  trailer, asserting a spurious CRC bit that CB3's compile-time
  ``SHUFFLE_FLAGS_MASK`` counted on every packet - and had to be skipped
  here. The sep24 trailer fix made it a real error indicator again.)

Only the boards' monitor plane is written (counter/sticky resets); no datapath
or configuration register is touched, so this is safe to run against a live
crate.
"""

import logging

from pychfpga.common.async_utils import run_async
from time import monotonic, sleep

__all__ = ['measure_crate_health', 'populated_slots', 'require_bct_corner_turn',
           'summarize_health_passes', 'ber_equivalent', 'measure_link_error_counts',
           'summarize_link_error_rates']

_logger = logging.getLogger(__name__)

# Aligner stickies read from both crossbars, with the physical meaning used to
# explain a failure to someone who cannot attach a debugger to the crate.
LANE_MONITOR_NAMES = ['BAD_TLAST', 'BAD_TVALID', 'BAD_FRAME_LENGTH',
                      'MISSING_FRAME', 'ALIGN_FIFO_OVERFLOW']

LANE_MONITOR_HINTS = {
    'MISSING_FRAME': 'lane delivers no frames inside the SOF window',
    'BAD_TLAST': 'misaligned end-of-frame',
    'BAD_TVALID': 'lane dropped valid mid-frame',
    'BAD_FRAME_LENGTH': 'data discarded by the aligner',
    'ALIGN_FIFO_OVERFLOW': 'aligner FIFO overflow',
}


def populated_slots(boards):
    """Return the set of slot numbers occupied by `boards`."""
    return {mb.slot for mb in boards}


def require_bct_corner_turn(boards):
    """Raise unless every board exposes the backplane corner-turn engine.

    ``BP_SHUFFLE``/``CROSSBAR2``/``CROSSBAR3`` exist only on BCT (backplane
    corner-turn) bitstreams. On a UCT build - the CRS ``shuffle8``/``chan8``
    firmware - they are simply absent, and reading them raises a bare
    ``AttributeError`` that says nothing about why. Fail with the platform and
    capability named instead.
    """
    for mb in boards:
        ct_type = getattr(mb, 'CT_TYPE', None)
        if getattr(mb, 'BP_SHUFFLE', None) is None or ct_type != 'BCT':
            raise RuntimeError(
                f'{mb!r} (slot {getattr(mb, "slot", "?")}) reports corner-turn type '
                f'{ct_type!r}, not "BCT": it has no BP_SHUFFLE/CROSSBAR2/CROSSBAR3 to '
                f'measure. Crate backplane health is a BCT-bitstream feature '
                f'(ICE shuffle16/128/256/512); it is neither implemented nor '
                f'generated for UCT builds such as the CRS shuffle8/chan8 firmware.')


def measure_crate_health(boards, populated=None, logger=None):
    """One full health pass over a populated crate.

    Clears every monitor, lets them re-accumulate on live traffic for 0.5 s,
    then reads each stage. Returns the list of failure descriptions; an empty
    list means the crate is healthy.

    Parameters:

        boards (iterable): the crate's motherboards (e.g. ``ca.ib``).

        populated (set): slot numbers that hold a board. Lanes fed by an empty
            slot are skipped rather than reported. Defaults to the slots of
            `boards`.

        logger (logging.Logger): where the per-board monitor readings are
            logged at INFO. Defaults to this module's logger.
    """
    boards = sorted(boards, key=lambda m: m.slot)
    if populated is None:
        populated = populated_slots(boards)
    if logger is None:
        logger = _logger
    require_bct_corner_turn(boards)

    failures = []

    for mb in boards:
        mb.BP_SHUFFLE.reset_stats()
        for cb in (mb.CROSSBAR2, mb.CROSSBAR3):
            cb.LANE_MONITOR_RESET = 1
            cb.LANE_MONITOR_RESET = 0
    sleep(0.5) # let counters/stickies re-accumulate on live traffic

    ctrs_before = {}
    for mb in boards:
        bp = mb.BP_SHUFFLE
        for lane in range(1, 16): # lane 0 is the internal bypass (no GTX)
            tx_slot, tx_lane = mb.crate.get_matching_tx((mb.slot, lane))
            if tx_slot not in populated:
                continue
            gtx = bp.gtx_map['pcb'][lane]
            if not int(gtx.BLOCK_LOCK):
                failures.append(
                    f"[slot {mb.slot}] BP pcb lane {lane} (fed by slot {tx_slot} lane {tx_lane}): "
                    f"BLOCK_LOCK=0 - 64b66b sync not achieved; check that link/transmitter")
        err, rxovf, fdet, minlen, maxlen = run_async(bp.get_rx_lane_monitor(
            ('ERROR_CTR', 'RX_FIFO_OVERFLOW', 'FRAME_DETECT',
             'MIN_FRAME_LENGTH', 'MAX_FRAME_LENGTH'), 'pcb'))
        logger.info(f"[slot {mb.slot}] BP pcb monitors: err={err} rx_fifo_ovf={rxovf} "
                    f"frame_detect={fdet} min_len={minlen} max_len={maxlen}")
        for lane in range(16):
            # Name the transmitter so a link fault is actionable from the log alone.
            if lane > 0:
                tx_slot, tx_lane = mb.crate.get_matching_tx((mb.slot, lane))
                if tx_slot not in populated:
                    continue
                where = f"[slot {mb.slot}] BP pcb lane {lane} (fed by slot {tx_slot} lane {tx_lane})"
            else:
                where = f"[slot {mb.slot}] BP pcb lane 0 (internal bypass)"
            if err[lane]:
                failures.append(f"{where}: {err[lane]} CRC/frame errors on the backplane link")
            if rxovf[lane]:
                failures.append(f"{where}: RX FIFO overflow (downstream crossbar not draining)")
            if not fdet[lane]:
                failures.append(f"{where}: no frames detected (source board not streaming)")
            if minlen[lane] != maxlen[lane]:
                failures.append(f"{where}: frame length varies "
                                f"({minlen[lane]}..{maxlen[lane]} words) - corrupted stream")

        for name, cb, n_outputs in (('CROSSBAR2', mb.CROSSBAR2, 8),
                                    ('CROSSBAR3', mb.CROSSBAR3, 8)):
            mons = run_async(cb.get_lane_monitor(LANE_MONITOR_NAMES))
            logger.info(f"[slot {mb.slot}] {name} monitors: "
                        + ' '.join(f'{n}={v}' for n, v in zip(LANE_MONITOR_NAMES, mons)))
            for mon_name, values in zip(LANE_MONITOR_NAMES, mons):
                for lane, v in enumerate(values):
                    if v:
                        failures.append(f"[slot {mb.slot}] {name} input lane {lane}: "
                                        f"{mon_name}={v} - {LANE_MONITOR_HINTS[mon_name]}")
            # PACKET_ERROR_CTR is indexed by LANE_MONITOR_SEL and only
            # lanes 0..7 are valid (higher selects read out of range).
            for sel in range(n_outputs):
                cb.LANE_MONITOR_SEL = sel
                v = int(cb.PACKET_ERROR_CTR)
                if v:
                    failures.append(f"[slot {mb.slot}] {name} lane {sel}: "
                                    f"PACKET_ERROR_CTR={v} (CRC/length/alignment errors)")
            cb.LANE_MONITOR_SEL = 0
            ctrs_before[(mb.slot, name)] = (int(cb.INPUT_FRAME_CTR),
                                            int(cb.ALIGN_FRAME_CTR),
                                            int(cb.OUTPUT_FRAME_CTR))
    sleep(0.3)
    ctr_regs = {'INPUT': 'INPUT_FRAME_CTR', 'ALIGN': 'ALIGN_FRAME_CTR',
                'OUTPUT': 'OUTPUT_FRAME_CTR'}
    for mb in boards:
        for name, cb in (('CROSSBAR2', mb.CROSSBAR2), ('CROSSBAR3', mb.CROSSBAR3)):
            for ctr_name, before in zip(('INPUT', 'ALIGN', 'OUTPUT'),
                                        ctrs_before[(mb.slot, name)]):
                # These counters are 8-bit and free-running at ~200k counts/s,
                # so a healthy counter matches its earlier reading by
                # wraparound coincidence with probability 1/256 - across the
                # 96 counters read here, roughly every third pass would flag
                # one spuriously. A genuinely stalled counter stays equal on
                # every re-read, so require the match to survive two retries.
                # One retry (~1.5e-5 per counter) still flagged ~9% of 60-pass
                # soaks; three coincidences compound to ~6e-8 per counter,
                # ~3e-4 per 60-pass soak.
                value = int(getattr(cb, ctr_regs[ctr_name]))
                for _ in range(2):
                    if value != before:
                        break
                    sleep(0.05)
                    value = int(getattr(cb, ctr_regs[ctr_name]))
                if value == before:
                    failures.append(f"[slot {mb.slot}] {name} {ctr_name}_FRAME_CTR static "
                                    f"at {before} - no dataflow through this stage")

    return failures


def summarize_health_passes(pass_failures, n_passes):
    """Render repeated health passes as one actionable message.

    `pass_failures` maps each pass number to the failure list
    :func:`measure_crate_health` returned for it. Problems are grouped by
    location (the text before the first ': '), so a single flaky link - one
    location in a few passes - reads differently from a systemic fault - many
    locations at once. Returns an empty string when every pass was clean.
    """
    by_location = {}  # location -> [(pass number, detail), ...]
    for n in sorted(pass_failures):
        for failure in pass_failures[n]:
            location, _, detail = failure.partition(': ')
            by_location.setdefault(location, []).append((n, detail))
    if not by_location:
        return ''
    bad_passes = sum(1 for failures in pass_failures.values() if failures)
    lines = [f'{len(by_location)} location(s) reported problems in {bad_passes} of '
             f'{n_passes} health passes:']
    for location, hits in by_location.items():
        passes = ', '.join(str(n) for n, _ in hits[:10]) + (', ...' if len(hits) > 10 else '')
        lines.append(f'  {location}: {len(hits)}/{n_passes} passes ({passes}); '
                     f'first: {hits[0][1]}')
    return '\n'.join(lines)


# Link error-rate characterization uses get_ber()'s conventions so that a result is
# directly comparable with the qc/MGK7BP16 test_bitErrorRate limit (BER < 1e-10).
BER_LINE_RATE = 10e9       # bits/s per backplane link, as get_ber() assumes
BER_BITS_PER_ERROR = 16    # get_ber() counts each error as a 16-bit word
RX_ERROR_CTR_MAX = 0xFFFF  # the backplane RX_ERROR_CTR saturates here


def ber_equivalent(errors, seconds):
    """Bit error rate implied by `errors` counted over `seconds`, with get_ber()'s conventions."""
    return errors * BER_BITS_PER_ERROR / (seconds * BER_LINE_RATE)


def measure_link_error_counts(boards, populated=None, window=60.0):
    """Count CRC/frame errors on every populated backplane PCB link over one window.

    Unlike :func:`measure_crate_health`, which samples 0.5 s per pass, this clears
    the counters once and reads them after `window` seconds, so every link is
    observed continuously for a known time. Only the monitor plane is touched
    (``RESET_STATS``), so this is safe on a live crate.

    Returns ``{(rx_slot, rx_lane): (tx_slot, tx_lane, errors, seconds)}``. An
    `errors` value of :data:`RX_ERROR_CTR_MAX` means the counter saturated.
    """
    boards = sorted(boards, key=lambda m: m.slot)
    if populated is None:
        populated = populated_slots(boards)
    require_bct_corner_turn(boards)
    t_reset = {}
    for mb in boards:
        mb.BP_SHUFFLE.reset_stats()
        t_reset[mb.slot] = monotonic()
    sleep(window)
    counts = {}
    for mb in boards:
        err = run_async(mb.BP_SHUFFLE.get_rx_lane_monitor('ERROR_CTR', 'pcb'))
        seconds = monotonic() - t_reset[mb.slot]
        for lane in range(1, 16):  # lane 0 is the internal bypass (no backplane link)
            tx_slot, tx_lane = mb.crate.get_matching_tx((mb.slot, lane))
            if tx_slot in populated:
                counts[(mb.slot, lane)] = (tx_slot, tx_lane, int(err[lane]), seconds)
    return counts


def summarize_link_error_rates(totals, ber_limit=1e-10):
    """Render accumulated link error counts as ``(failing links, report lines)``.

    `totals` maps ``(rx_slot, rx_lane)`` to ``(tx_slot, tx_lane, errors, seconds,
    saturated)``. A link fails when its BER-equivalent reaches `ber_limit` or its
    counter saturated in any window; error-free links are counted in the first line.
    """
    errored = sorted(((t[2], key) for key, t in totals.items() if t[2]), reverse=True)
    failing = []
    lines = []
    for errors, key in errored:
        tx_slot, tx_lane, _, seconds, saturated = totals[key]
        ber = ber_equivalent(errors, seconds)
        bad = saturated or ber >= ber_limit
        if bad:
            failing.append(key)
        lines.append(f'[slot {key[0]}] BP pcb lane {key[1]} (fed by slot {tx_slot} lane {tx_lane}): '
                     f'{errors}{"+ (counter saturated)" if saturated else ""} CRC/frame errors in '
                     f'{seconds:.0f} s, BER-equivalent {ber:.1e}' + (' - OVER THE LIMIT' if bad else ''))
    lines.insert(0, f'{len(totals)} backplane links: {len(errored)} with errors, {len(failing)} at or '
                    f'over BER {ber_limit:g}, {len(totals) - len(errored)} error-free')
    return failing, lines
