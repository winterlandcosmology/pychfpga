#!/usr/bin/env python3
"""Reproduce corr_test constant_autocorr +1-frame bump and isolate cause.

Injects constant (1+1j) like corr_test, integrates over N frames, compares
correlator autocorr product 0 to expected PSD 2.0 on good bins.
"""

import argparse
import asyncio
import sys
from pathlib import Path

import numpy as np

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "firmware_tests"))

from firmware_tests.utils import gen_data  # noqa: E402
from pychfpga.fpga_array import FPGAArray  # noqa: E402

REF = 2.0  # |1+1j|^2
DEFAULT_PERIOD = 32768


def inject_constant(board, postscaler=26):
    n = board.ADC_SAMPLES_PER_FRAME
    value = 1 * (1 + 1j)
    spectrum_iq = gen_data("ab", samples=n, a=np.real(value), b=np.imag(value))
    board.set_channelizer(
        data_source="buffer",
        buffer=spectrum_iq,
        channels=None,
        fft_bypass=True,
        scaler_bypass=False,
        scaler_bypass_data_type=0,
        postscaler=postscaler,
        gains=1,
        symmetric_saturation=False,
        offset_binary_encoding=False,
    )
    board.sync()


def compress_good(data_row, keep):
    keep = np.asarray(keep, dtype=bool)
    n = min(keep.shape[0], data_row.shape[0])
    return np.compress(keep[:n], data_row[:n], axis=0)


def analyze_hw_row(hw_good, period, ref=REF):
    """Summarize mismatch vs constant ref on good-bin axis."""
    hw = np.asarray(hw_good[:, 0].real if hw_good.ndim > 1 else hw_good.real, dtype=np.float64)
    diff = hw - ref
    inv_n = ref / float(period)
    atol = 1e-12
    bad = ~np.isclose(hw, ref, rtol=0, atol=atol)
    n_bad = int(np.count_nonzero(bad))
    bump = bad & (np.abs(ref) > 0) & np.isclose(diff, inv_n, rtol=0.02, atol=1e-8)
    bad_ix = np.flatnonzero(bad)
    bump_ix = np.flatnonzero(bump)
    max_excess = float(np.max(diff)) if diff.size else 0.0
    return {
        "n_bad": n_bad,
        "n_bump": int(np.count_nonzero(bump)),
        "max_excess": max_excess,
        "inv_n": inv_n,
        "bad_good_bins": bad_ix[:24].tolist(),
        "bump_good_bins": bump_ix[:24].tolist(),
        "examples": [(int(i), float(hw[i]), float(diff[i])) for i in bad_ix[:6]],
    }


def print_ucorr(board, label=""):
    u = board.CORR
    print(
        f"  UCORR {label}: NDECIM={int(u.BIN_DECIMATION_FACTOR)} NCORR={int(u.NCORR)} "
        f"NCLK_PER_BIN={int(u.NCLK_PER_BIN)} AUTO_BIN_SET={int(u.AUTO_BIN_SET)} "
        f"BIN_SET={int(u.BIN_SET)} INTEG_REG={int(u.INTEGRATION_PERIOD)} "
        f"(frames={int(u.INTEGRATION_PERIOD)+1}) "
        f"IN_CTR={int(u.IN_FRAME_CTR)} OUT_CTR={int(u.OUT_FRAME_CTR)} OVERRUN={int(u.OVERRUN)}"
    )


def configure_correlator(board, period, auto_bin_set=True, bin_set=0, autocorr_only=False):
    """Mirror UCORR.start_correlator with explicit AUTO_BIN_SET control."""
    u = board.CORR
    board.fpga.set_corr_reset(1)
    u.SOFT_RESET = 1
    u.INTEGRATION_PERIOD = period - 1
    u.AUTOCORR_ONLY = autocorr_only
    u.NO_ACCUM = 0
    u.pulse_bit("OVERRUN_RESET")
    u.AUTO_BIN_SET = 1 if auto_bin_set else 0
    u.BIN_SET = int(bin_set)
    u.SOFT_RESET = 0
    board.fpga.set_corr_reset(0)


def read_integration(receiver, board, period, flush=True, label=""):
    timeout = period / board.FRAME_RATE + 1.5
    ts, data, counts, _sats = receiver.read_corr_frames(
        flush=flush,
        soft_integ_period=1,
        number_of_results=1,
        data_timeout=timeout,
        verbose=0,
    )
    pkt = int(np.min(counts))
    exp = int(receiver.expected_packets)
    if pkt != exp:
        print(f"  WARN {label}: packets {pkt}/{exp}")
    keep = np.asarray(receiver.get_good_bin_mask())
    hw_good = compress_good(data[0], keep) / period
    stats = analyze_hw_row(hw_good, period)
    rb = np.flatnonzero(keep)
    fft_for = {int(gi): int(rb[gi]) for gi in stats["bump_good_bins"][:8]}
    print(
        f"  {label}: n_bad={stats['n_bad']} n_+1/N={stats['n_bump']} "
        f"max_excess={stats['max_excess']:.6g} (1/N={stats['inv_n']:.6g}) "
        f"bump_good_bins={stats['bump_good_bins'][:12]} fft_bins={fft_for}"
    )
    if stats["examples"]:
        print(f"    examples (good_bin, hw, delta): {stats['examples']}")
    return stats, hw_good, data


def map_fft_bins_to_corr(board, fft_bins):
    u = board.CORR
    u.update_config()
    bin_map = np.asarray(u.config.bin_map)
    out = []
    for fb in fft_bins:
        hits = np.argwhere(bin_map == fb)
        if hits.size:
            bs, corr, clk, prod = hits[0]
            out.append((fb, int(bs), int(corr), int(clk), int(prod)))
        else:
            out.append((fb, None, None, None, None))
    return out


async def run_experiment(name, board, receiver, period, setup_fn, read_fn):
    print(f"\n=== {name} ===")
    setup_fn()
    print_ucorr(board, "after setup")
    return read_fn()


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hwm", default="crs 55")
    parser.add_argument("--prog", type=int, default=1)
    parser.add_argument("--period", type=int, default=DEFAULT_PERIOD)
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
    receiver = await ca.get_corr_receiver_async(verbose=0)
    period = args.period

    print_ucorr(board, "after connect")
    inject_constant(board)

    def baseline_setup(auto=True):
        board.stop_correlator()
        configure_correlator(board, period, auto_bin_set=auto, bin_set=0)
        board.sync()

    def read_once(flush=True, tag="read"):
        return read_integration(receiver, board, period, flush=flush, label=tag)

    # 1) Baseline (pytest-like)
    await run_experiment(
        "baseline (auto_bin_set=1, flush, 1st integ after inject)",
        board,
        receiver,
        period,
        lambda: baseline_setup(auto=True),
        lambda: read_once(flush=True, tag="integ#1"),
    )

    # 2) Second integration, no reinject
    await run_experiment(
        "2nd integration, same run (no reinject, stop/start)",
        board,
        receiver,
        period,
        lambda: baseline_setup(auto=True),
        lambda: read_once(flush=True, tag="integ#2"),
    )

    # 3) Back-to-back read without stop between (still running correlator)
    print("\n=== back-to-back read (no stop between integ #3 and #4) ===")
    read_once(flush=True, tag="integ#3")
    stats4, _, _ = read_once(flush=False, tag="integ#4 (no flush)")

    # 4) Discard-first pattern
    print("\n=== discard first integration, keep second ===")
    board.stop_correlator()
    configure_correlator(board, period, auto_bin_set=True)
    board.sync()
    read_once(flush=True, tag="discard")
    stats_keep, hw_keep, _ = read_once(flush=True, tag="kept")

    # 5) Fixed bin set
    await run_experiment(
        "AUTO_BIN_SET=0, BIN_SET=0",
        board,
        receiver,
        period,
        lambda: baseline_setup(auto=False),
        lambda: read_once(flush=True, tag="fixed_bs0"),
    )

    # 6) Extra sync after start
    print("\n=== extra sync() after start_correlator ===")
    board.stop_correlator()
    configure_correlator(board, period, auto_bin_set=True)
    board.sync()
    board.sync()
    read_once(flush=True, tag="double_sync")

    # 7) Reinject before start
    print("\n=== reinject + baseline ===")
    inject_constant(board)
    board.stop_correlator()
    configure_correlator(board, period, auto_bin_set=True)
    board.sync()
    read_once(flush=True, tag="after_reinject")

    # Map canonical bad FFT bins if present
    bump_fft = [5, 10, 15]
    print("\n=== bin_map hits for FFT bins 5, 10, 15 ===")
    for row in map_fft_bins_to_corr(board, bump_fft):
        print(f"  fft={row[0]} -> bin_set={row[1]} corr={row[2]} clkbin={row[3]} prod={row[4]}")

    print("\n=== summary ===")
    print(
        "If +1/N bumps appear on integ#1 but not integ#2/kept/discarded-second, "
        "the issue is first-integration-after-arm, not steady-state math."
    )
    print(
        f"After discard-first: n_bad={stats_keep['n_bad']} n_+1/N={stats_keep['n_bump']}"
    )

    board.stop_correlator()


if __name__ == "__main__":
    asyncio.run(main())
