#!/usr/bin/env python3
"""Live sinewave capture via raw_acq REST and quick plot of channel 0."""

from __future__ import annotations

import asyncio
import glob
import sys
from pathlib import Path

import aiohttp
import h5py
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from check_raw_acq_udp import _capture_states_json

BASE = "http://127.0.0.1:54322"
OUT_DIR = Path(__file__).resolve().parents[1] / "captures" / "sinewave_ch0_test"
OUT_DIR.mkdir(parents=True, exist_ok=True)


async def main() -> None:
    start_body = {
        "comet_broker": {"enabled": False},
        "ports": [{"port": 33745, "sources": [["192.168.88.109", 62510]]}],
        "stream_ids": list(range(8)),
        "raw_capture_state": _capture_states_json(),
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(f"{BASE}/start", json=start_body) as resp:
            text = await resp.text()
            print(f"POST /start -> {resp.status}: {text[:300]}")
            if resp.status >= 400:
                return

        print("Waiting 3s for packets...")
        await asyncio.sleep(3)

        async with session.get(f"{BASE}/get-rms") as resp:
            rms = await resp.json()
        print("RMS (ch0 should dominate):", rms.get("rms", [])[:3], "...")

        hdf5_body = {
            "base_dir": str(OUT_DIR),
            "base_filename": "sinewave_ch0",
            "capture_duration": 3,
            "capture_refresh_time": 0,
            "elements_per_file": 50000,
            "capture_aux_enable": True,
            "aux_base_dir": str(OUT_DIR),
            "aux_base_filename": "sinewave_ch0_only",
            "capture_aux_stream_ids": [0],
            "capture_aux_refresh_time": 0,
        }
        async with session.post(f"{BASE}/start-raw-hdf5", json=hdf5_body) as resp:
            print(f"POST /start-raw-hdf5 -> {resp.status}: {await resp.text()}")

        await asyncio.sleep(4)

        async with session.get(f"{BASE}/stop") as resp:
            print(f"GET /stop -> {await resp.text()}")

    repo = Path(__file__).resolve().parents[1]
    candidates = list(OUT_DIR.glob("sinewave_ch0*")) + list(OUT_DIR.glob("*.h5"))
    h5_paths = sorted(
        {p for p in candidates if p.is_file() and p.stat().st_size > 1000},
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not h5_paths:
        print(f"No capture files in {OUT_DIR}")
        return

    path = h5_paths[0]
    print(f"Analyzing {path} ({path.stat().st_size / 1e6:.2f} MB)")
    with h5py.File(path, "r") as f:
        dtype = f["timestream"].dtype
        n_samp = f["timestream"].shape[1]
        attrs_dtype = f.attrs.get("storage_dtype", "?")
        w = f["timestream"][-1, :].astype(np.float64)
        ch = int(f["adc_input"][-1, 0])
        n_rows = f["timestream"].shape[0]

    w -= w.mean()
    rms = float(np.sqrt(np.mean(w**2)))
    spec = np.abs(np.fft.rfft(w))
    spec[0] = 0
    k_peak = int(np.argmax(spec))
    peak_ratio = float(spec[k_peak] / (np.median(spec[1:]) + 1e-9))

    print(
        f"rows={n_rows} dtype={dtype} attrs.storage_dtype={attrs_dtype} "
        f"samples/row={n_samp} ch={ch} RMS={rms:.2f} peak/med={peak_ratio:.1f}"
    )

    n_show = int(w.size)
    fig, axes = plt.subplots(2, 1, figsize=(14, 7), constrained_layout=True)
    t = np.arange(n_show)
    axes[0].plot(t, w, lw=0.25, rasterized=True)
    axes[0].set_title(f"Channel {ch} time domain (full row, {n_show} samples, {dtype})")
    axes[0].set_xlim(0, n_show - 1)
    axes[0].set_xlabel("sample")
    axes[0].set_ylabel("ADC")

    axes[1].semilogy(np.arange(spec.size), spec, lw=0.25, rasterized=True)
    axes[1].set_title(f"Magnitude spectrum ({spec.size} bins)")
    axes[1].set_xlim(0, spec.size - 1)
    axes[1].set_xlabel("FFT bin")
    axes[1].set_ylabel("|X|")

    plot_path = OUT_DIR / "sinewave_ch0_plot.png"
    fig.savefig(plot_path, dpi=120)
    print(f"Wrote plot {plot_path}")


if __name__ == "__main__":
    asyncio.run(main())
