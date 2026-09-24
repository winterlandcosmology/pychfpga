#!/usr/bin/env python3
"""Capture one known correlator integration and report whether it looks right.

Injects a constant 1 on two channels and 0 on the rest, with the FFT bypassed,
so each sample is 1+1j and each hot product is 2. Reads the full triangle, then
autocorr-only, on the same receiver. The formatted array stays (n, NBINS, 36);
autocorr-only should write only the two diagonal slots.

Does not call stop_data_capture. That holds UCAP in reset and the correlator
then sends nothing.
"""

import argparse
import asyncio
import signal

import numpy as np

from pychfpga.fpga_array import FPGAArray


HOT = (0, 1)
INTEGRATION_PERIOD = 32768


def product_index(i, j, nchan):
    if i > j:
        i, j = j, i
    return i * (2 * nchan - i - 1) // 2 + j


def diagonals(nchan):
    return [product_index(i, i, nchan) for i in range(nchan)]


def inject(board, hot):
    n = board.ADC_SAMPLES_PER_FRAME
    nchan = board.NUMBER_OF_CHANNELIZERS
    board.set_data_source(source='arb', data=np.ones(n), channels=list(hot))
    quiet = [c for c in range(nchan) if c not in hot]
    board.set_data_source(source='arb', data=np.zeros(n), channels=quiet)
    board.set_channelizer(
        fft_bypass=True,
        scaler_bypass=False,
        scaler_bypass_data_type=0,
        postscaler=26,
        gains=1,
        symmetric_saturation=False,
        offset_binary_encoding=False,
    )
    board.sync()


def capture(receiver, board, period):
    def _alarm(signum, frame):
        raise TimeoutError(
            "read_corr_frames got no correlator packets "
            "(UCAP reset or the correlator is not sending)"
        )

    signal.signal(signal.SIGALRM, _alarm)
    timeout = max(5, int(period / board.FRAME_RATE + 3))
    signal.alarm(timeout)
    try:
        ts, data, counts, sats = receiver.read_corr_frames(
            flush=True,
            soft_integ_period=1,
            number_of_results=1,
            data_timeout=period / board.FRAME_RATE + 0.5,
            verbose=1,
        )
    finally:
        signal.alarm(0)
    return ts, data, counts, sats


def report(label, board, receiver, data, counts):
    nchan = board.NUMBER_OF_CHANNELIZERS
    nprod = nchan * (nchan + 1) // 2
    rec_bytes = int(receiver.packet_dtype.itemsize)
    keep = np.asarray(receiver.get_good_bin_mask())
    good = int(np.flatnonzero(keep)[0]) if keep.any() else 0
    row = np.asarray(data[0, good]) / INTEGRATION_PERIOD
    nz = np.flatnonzero(np.abs(row) > 0.05)
    diag = diagonals(nchan)
    cross = product_index(HOT[0], HOT[1], nchan)
    hot_diag = [product_index(c, c, nchan) for c in HOT]

    print(f"\n== {label} ==")
    print(f"NCMAC={receiver.NCMAC}  NPROD={receiver.NPROD}  record={rec_bytes} B  "
          f"full datagram={rec_bytes * receiver.FRAMES_PER_PACKET} B")
    print(f"shape={data.shape}  counts={counts.tolist()}  expected={receiver.expected_packets}")
    print(f"bin {good} nonzero |p|>0.05: {nz.tolist()}")
    print(f"  hot diagonals {hot_diag}: {np.array2string(row[hot_diag].real, precision=4)}")
    print(f"  cross {cross}: {row[cross].real:.4f}{row[cross].imag:+.4f}j")
    quiet_diag = [d for d in diag if d not in hot_diag]
    print(f"  other diagonals max |.|: {np.max(np.abs(row[quiet_diag])):.4g}")

    ok_counts = int(np.min(counts)) == int(receiver.expected_packets)
    ok_shape = data.shape[-1] == nprod
    ok_diag = np.allclose(row[hot_diag].real, 2.0, atol=0.02) and np.allclose(row[hot_diag].imag, 0, atol=0.02)
    if "autocorr" in label:
        off = np.ones(nprod, dtype=bool)
        off[hot_diag] = False
        ok_off = np.max(np.abs(row[off])) < 0.05
        ok_ncmac = receiver.NCMAC == nchan and rec_bytes == 10 + 5 * nchan
    else:
        ok_cross = np.allclose(row[cross].real, 2.0, atol=0.02)
        ok_off = ok_cross
        ok_ncmac = receiver.NCMAC == nprod and rec_bytes == 10 + 5 * nprod
    passed = ok_counts and ok_shape and ok_diag and ok_off and ok_ncmac
    print("PASS" if passed else "FAIL")
    return passed


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hwm", default="crs 55")
    parser.add_argument("--prog", type=int, default=1)
    args = parser.parse_args()

    ca = await FPGAArray(
        args.hwm,
        mode="corr8",
        prog=args.prog,
        sync_method="local",
        ignore_missing_boards=True,
        stderr_log_level="warn",
    )
    board = ca.ib[0]
    receiver = await ca.get_corr_receiver_async(verbose=1)
    try:
        inject(board, HOT)
        results = []
        for autocorr_only in (False, True):
            board.stop_correlator()
            board.start_correlator(
                integration_period=INTEGRATION_PERIOD,
                autocorr_only=autocorr_only,
            )
            board.sync()
            _ts, data, counts, _sats = capture(receiver, board, INTEGRATION_PERIOD)
            label = "autocorr_only" if autocorr_only else "full triangle"
            results.append(report(label, board, receiver, data, counts))
        print("\nALL PASS" if all(results) else "\nSOME FAILED")
    finally:
        board.stop_correlator()


if __name__ == "__main__":
    asyncio.run(main())
