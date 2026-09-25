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
- CROSSBAR3's packet-error counter saturates unconditionally on current
  bitstreams (the packet-flags trailer asserts the missing/CRC bits on every
  packet regardless of datapath health, and CB3's compile-time
  ``SHUFFLE_FLAGS_MASK`` counts them), so it is deliberately not asserted on.
  CB3 remains covered by its per-lane stickies and the frame-counter advance.

Only the boards' monitor plane is written (counter/sticky resets); no datapath
or configuration register is touched, so this is safe to run against a live
crate.
"""

import logging

from pychfpga.common.async_utils import run_async
from time import sleep

__all__ = ['measure_crate_health', 'stickies_quiet', 'populated_slots',
           'require_bct_corner_turn']

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
                    f"[slot {mb.slot}] BP pcb lane {lane}: BLOCK_LOCK=0 "
                    f"(fed by slot {tx_slot} lane {tx_lane}) - 64b66b sync not "
                    f"achieved; check that link/transmitter")
        err, rxovf, fdet, minlen, maxlen = run_async(bp.get_rx_lane_monitor(
            ('ERROR_CTR', 'RX_FIFO_OVERFLOW', 'FRAME_DETECT',
             'MIN_FRAME_LENGTH', 'MAX_FRAME_LENGTH'), 'pcb'))
        logger.info(f"[slot {mb.slot}] BP pcb monitors: err={err} rx_fifo_ovf={rxovf} "
                    f"frame_detect={fdet} min_len={minlen} max_len={maxlen}")
        for lane in range(16):
            if lane > 0:
                tx_slot, _ = mb.crate.get_matching_tx((mb.slot, lane))
                if tx_slot not in populated:
                    continue
            where = f"[slot {mb.slot}] BP pcb lane {lane}"
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
            # CROSSBAR3 is skipped: its counter saturates unconditionally on
            # this bitstream (see the module docstring).
            if name != 'CROSSBAR3':
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
    for mb in boards:
        for name, cb in (('CROSSBAR2', mb.CROSSBAR2), ('CROSSBAR3', mb.CROSSBAR3)):
            after = (int(cb.INPUT_FRAME_CTR), int(cb.ALIGN_FRAME_CTR),
                     int(cb.OUTPUT_FRAME_CTR))
            for ctr_name, a, b in zip(('INPUT', 'ALIGN', 'OUTPUT'),
                                      ctrs_before[(mb.slot, name)], after):
                if a == b:
                    failures.append(f"[slot {mb.slot}] {name} {ctr_name}_FRAME_CTR static "
                                    f"at {a} - no dataflow through this stage")

    return failures


def stickies_quiet(boards, window=5.0):
    """Cheap settle probe: True if nothing re-asserted over `window` seconds.

    Clears both crossbars' monitors on all boards, waits `window` seconds of
    live traffic, and returns True if no aligner sticky re-asserted and no
    packet error was counted anywhere. The full health measurement is run
    separately once a quiet window is seen.
    """
    for mb in boards:
        for cb in (mb.CROSSBAR2, mb.CROSSBAR3):
            cb.LANE_MONITOR_RESET = 1
            cb.LANE_MONITOR_RESET = 0
    sleep(window)
    for mb in boards:
        for cb in (mb.CROSSBAR2, mb.CROSSBAR3):
            mons = run_async(cb.get_lane_monitor(LANE_MONITOR_NAMES))
            if any(int(v) for row in mons for v in row):
                return False
            # CB3's packet counter saturates unconditionally on this
            # bitstream (trailer artifact - see the module docstring),
            # so only CB2's counter is a meaningful quiet criterion.
            if cb is mb.CROSSBAR2:
                for sel in range(8):
                    cb.LANE_MONITOR_SEL = sel
                    if int(cb.PACKET_ERROR_CTR):
                        return False
                cb.LANE_MONITOR_SEL = 0
    return True
