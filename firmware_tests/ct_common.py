'''Shared corner-turn capture helpers used by the ct hardware test suites.

These are module-level functions taking an explicit board object so both the
single-board probe suite (ct_prober_test.py) and the multi-board crate suite
(ct_shuffle256_test.py) can use them. The filename intentionally does not
match pytest.ini's python_files patterns, so pytest never collects it.
'''

import asyncio

import numpy as np
import pytest


def _stop_ct_capture(board):
    '''Park the corner-turn probe when this firmware has one.

    ``chFPGA.stop_ct_capture`` raises when the probe is absent. Parking is
    optional on a channelizer-only bitstream, so that check lives here.
    '''
    if getattr(board, 'CT_PROBER', None) is not None:
        board.stop_ct_capture()


def stop_captures(board):
    '''Park the channelizer capture and, when present, the corner-turn probe.'''
    board.stop_data_capture()
    _stop_ct_capture(board)


def start_ct_capture(board, **kwargs):
    '''Park the channelizer probers, then capture one corner-turn lane.

    ``chFPGA.start_ct_capture`` leaves the channelizer probers running, and
    both streams share the capture link. It still raises when the firmware
    has no corner-turn probe.
    '''
    board.stop_data_capture()
    board.start_ct_capture(**kwargs)


def start_data_capture(board, *args, **kwargs):
    '''Park the corner-turn probe when present, then start channelizer capture.

    ``chFPGA.start_data_capture`` does not park the corner-turn probe.
    '''
    _stop_ct_capture(board)
    board.start_data_capture(*args, **kwargs)


def decode_ct_byte(byte, use_offset_binary=False):
    '''Reference (4+4)-bit complex decode of one corner-turn payload byte.'''
    byte = int(byte) & 0xFF
    if use_offset_binary:
        byte ^= 0x88
    re = ((byte >> 4) + 8) % 16 - 8 # sign-extend the 4-bit nibbles
    im = ((byte & 0xF) + 8) % 16 - 8
    return complex(re, im)


async def require_ct_dataflow(board, cache, logger, sync=None, key='ct_prober_dataflow'):
    '''Skip capture tests when the corner turn emits no output packets.

    The probe can only capture what the corner turn produces, and
    read_raw_frames() has no timeout: without this check a silent
    corner-turn output would hang the capture tests forever.
    CROSSBAR3.OUTPUT_FRAME_CTR counts frames leaving the last corner-turn
    stage - the stream the probe taps - so a static counter proves the
    corner turn itself is silent, independently of the probe.

    Parameters:
        cache: dict-like session store for the verdict (re-checked only
            after the caller invalidates `key`).

        sync (callable or None): optional pre-check trigger. The
            single-board suite passes its trig+local-sync sequence; crate
            callers MUST pass None - a board-local sync would break the
            array-wide frame alignment established at crate bring-up.
    '''
    if key not in cache:
        cb3 = getattr(board.CT, 'CROSSBAR3', None)
        if cb3 is None:
            cache[key] = True # no independent monitor; try anyway
        else:
            if sync is not None:
                sync()
            a = int(cb3.OUTPUT_FRAME_CTR)
            await asyncio.sleep(0.3)
            b = int(cb3.OUTPUT_FRAME_CTR)
            logger.debug(f"require_ct_dataflow: {board!r} output_frame_ctr {a}->{b} advancing={a != b}")
            cache[key] = a != b
    if not cache[key]:
        pytest.skip("The corner-turn output is not streaming on this board "
                    "(CROSSBAR3 OUTPUT_FRAME_CTR is static): complete the "
                    "corner-turn dataflow bring-up before running capture tests")


async def set_capture_ct(board, array, *, ncap=1, lane=0, frames_per_burst=1, format=None,
                          period=0.01, split=None, recv_verbose=0, logger=None):
    '''Configure a corner-turn capture on `board`'s given lane and read it back.

    Returns (ts, data, counts, geometry), where geometry is the packet
    geometry read back from the crossbar registers. Raw captures
    (format='32') of multi-packet bursts are only split into per-packet rows
    when split=True (decoded complex captures are always split).

    The channelizer probers are parked first (see :func:`start_ct_capture`),
    so the read is only cookie 0xA2.
    '''
    if logger is not None:
        logger.debug(f"set_capture_ct: {board!r} lane={lane} ncap={ncap} "
                     f"frames_per_burst={frames_per_burst} period={period} "
                     f"format={format} split={split}")
    start_ct_capture(
        board, period=period, lane=lane, frames_per_burst=frames_per_burst)
    geometry = board.CT_PROBER.get_packet_geometry(lane)
    if logger is not None:
        logger.debug(f"set_capture_ct: geometry={geometry}")
    receiver = await array.get_ct_data_receiver_async(verbose=recv_verbose)
    receiver.flush()
    ts, data, counts = receiver.read_frames(
        ncap=ncap, format=format, chan_first=False, split=split)
    if logger is not None:
        logger.debug(f"set_capture_ct: ts={ts.tolist()} data_shape={data.shape} "
                     f"counts={counts.tolist()}")
    return ts, data, counts, geometry


async def gpu_clean_start(gpu, logger, retries=8):
    '''Reset the GPU link MACs until their TX FIFOs come up clean.

    The GPU core reset released while the corner-turn stream is running
    intermittently (roughly one attempt in three on the bench) leaves one
    lane's TX FIFO permanently wedged: the MAC accepts stream words the
    moment the fabric reset deasserts, but the FIFO/GTX are still completing
    their own reset, and the dropped words desynchronize the header and data
    FIFOs - both then overflow continuously (the sticky flags re-assert
    within 100 ms of every clear) until the next core reset. This is a
    firmware reset-sequencing weakness of the reconnected GPU path
    (candidate fix: gate the s_axis feed until resetdone and FIFO
    wr_rst_busy clear, re-enabling at a packet boundary); until it is fixed,
    a clean start is obtained by re-resetting until the overflow flags stay
    quiet.
    '''
    for attempt in range(retries):
        gpu.reset()
        await asyncio.sleep(0.25)
        gpu.OVERFLOW_RESET = 1
        gpu.OVERFLOW_RESET = 0
        await asyncio.sleep(0.25)
        data_overflow = int(gpu.DATA_FIFO_OVERFLOW)
        frame_overflow = int(gpu.FRAME_FIFO_OVERFLOW)
        if data_overflow == 0 and frame_overflow == 0:
            logger.debug(f"gpu_clean_start: clean after {attempt + 1} attempt(s)")
            return
        logger.info(f"gpu_clean_start: attempt {attempt + 1} left a wedged TX FIFO "
                    f"(data_overflow={data_overflow:#04x} frame_overflow={frame_overflow:#04x}), "
                    f"re-resetting")
    raise AssertionError(
        f"GPU link TX FIFOs still overflow after {retries} core resets "
        f"(data_overflow={data_overflow:#04x} frame_overflow={frame_overflow:#04x})")
