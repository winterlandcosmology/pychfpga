#!/usr/bin/env python3
"""Start raw_acq on a live UDP port and monitor FFT RMS accumulation (fft_2x / UCAP)."""

import argparse
import asyncio
import sys
import time

import aiohttp


def crs_ucap_fft_2x_firmware_config(stream_ids=None):
    """Firmware config snapshot for CRS UCAP scaler fft_2x (8 channels, mode 0)."""
    stream_ids = tuple(int(s) for s in (stream_ids or range(8)))
    nchan = len(stream_ids)
    adc_cf = dict(fmt_base_width=16, fmt_is_complex=False, fmt_interleave_factor=1)
    fft_cf = dict(fmt_base_width=32, fmt_is_complex=True, fmt_interleave_factor=2)
    shared = dict(
        capture_engine="UCAP",
        channels=tuple(range(nchan)),
        stream_ids=stream_ids,
        frames_per_burst=2,
        subframes_per_frame=4,
        target_mode=0,
        bytes_per_sample=2,
        samples_per_frame=16384,
        bytes_per_frame=32768,
        bytes_per_subframe=8192,
        packets_per_channel_per_burst=8,
        logical_frames_per_burst=1,
        fft_bins_per_word=4,
        packet_header_format="raw-v1",
    )
    raw = dict(
        **shared,
        source="scaler",
        capture_format=[adc_cf, fft_cf],
        source_target_mode=[0, 0],
        packets_per_logical_frame=2,
        packet_cookie=0xA1,
        fft_offset_encoding=False,
        offset_encoding=False,
    )
    adc = dict(
        **shared,
        source="funcgen",
        capture_format=[adc_cf, None],
        storage_dtype="i2",
        packets_per_logical_frame=1,
        packet_cookie=0xA0,
    )
    fft = dict(
        **shared,
        source="scaler",
        capture_format=[None, fft_cf],
        packets_per_logical_frame=2,
        packet_cookie=0xA1,
        fft_offset_encoding=False,
        offset_encoding=False,
    )
    corr = dict(
        capture_engine="UCORR",
        NCHAN=8,
        NCLK=4,
        CT_LEVEL=1,
        NDECIM=2,
        firmware_integ_period=16384,
        soft_integ_period=1,
        fft_bin_map=[
            [int(x) for x in row]
            for row in __import__("numpy").arange(2048).reshape(4, 512)
        ],
    )
    return dict(raw=raw, adc=adc, fft=fft, corr=corr)


async def wait_for_server(session, base_url, timeout=30.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            async with session.get(f"{base_url}/status") as resp:
                if resp.status == 200:
                    return True
        except aiohttp.ClientError:
            pass
        await asyncio.sleep(0.5)
    return False


async def run(args):
    base_url = f"http://{args.rest_host}:{args.rest_port}"
    stream_ids = list(range(args.nchan))
    firmware = crs_ucap_fft_2x_firmware_config(stream_ids)

    async with aiohttp.ClientSession() as session:
        if not await wait_for_server(session, base_url):
            print(f"raw_acq REST server not reachable at {base_url}", file=sys.stderr)
            print("Start it with: python3 -m pychfpga.raw_acq", file=sys.stderr)
            return 1

        start_body = {
            "name": "fft_rms_live",
            "comet_broker": {"enabled": False},
            "no_ping": True,
            "ports": [{"port": args.udp_port, "sources": [[args.source_host, args.source_port]]}],
            "stream_ids": stream_ids,
            "config": firmware,
            "metrics_refresh_time": 1,
        }
        print(f"POST /start  listen=0.0.0.0:{args.udp_port}  source={args.source_host}:{args.source_port}")
        async with session.post(f"{base_url}/start", json=start_body) as resp:
            text = await resp.text()
            print(f"  -> {resp.status}: {text[:400]}")
            if resp.status >= 400:
                return 1

        rms_body = {
            "stream_ids": stream_ids,
            "target_gain_bank": 0,
            "number_of_frames": args.number_of_frames,
        }
        print(f"POST /start-fft-rms  n_frames={args.number_of_frames}  stream_ids={stream_ids}")
        async with session.post(f"{base_url}/start-fft-rms", json=rms_body) as resp:
            text = await resp.text()
            print(f"  -> {resp.status}: {text[:200]}")

        print(f"Monitoring FFT RMS for {args.seconds:.0f}s (check raw_acq logs for per-spectrum messages)...")
        t_end = time.time() + args.seconds
        last_rows = None
        while time.time() < t_end:
            await asyncio.sleep(2.0)
            async with session.get(f"{base_url}/get-fft-rms-status") as resp:
                if resp.status != 200:
                    print(f"  get-fft-rms-status -> {resp.status}")
                    continue
                body = await resp.json()
                if isinstance(body, dict) and body.get("error"):
                    print(f"  get-fft-rms-status error: {body['error'][:120]}")
                    continue
                if isinstance(body, dict) and "channels" in body:
                    diag = body.get("diagnostics") or {}
                    if diag:
                        print(
                            f"  diag: complete={diag.get('rows_complete')} "
                            f"incomplete={diag.get('rows_incomplete')} "
                            f"max_count={diag.get('max_count_seen')} "
                            f"expected={diag.get('expected_packets')}"
                        )
                    rows = body["channels"]
                else:
                    rows = body
            if rows != last_rows:
                for row in rows:
                    print(
                        f"  stream_id={row['stream_id']:3d}  "
                        f"{row['n_frames']:4d}/{row['target']} frames  "
                        f"done={row['done']}  bins={row['n_bins']}"
                    )
                if not rows:
                    print("  (no active FFT RMS channels yet)")
                last_rows = rows

            async with session.get(f"{base_url}/get-fft-rms") as resp:
                if resp.status == 200:
                    sids, rms = await resp.json()
                    if sids:
                        print(f"  COMPLETE: got FFT RMS for stream_ids={sids} ({len(sids)} channels)")
                        for sid, vec in zip(sids, rms):
                            import numpy as np
                            arr = np.asarray(vec, dtype=np.float32)
                            print(
                                f"    stream_id={sid}: "
                                f"mean_rms={float(np.sqrt(np.mean(arr**2))):.4g}  "
                                f"n_bins={arr.size}"
                            )
                        break

        async with session.get(f"{base_url}/status") as resp:
            print(f"Final status: {await resp.json()}")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--udp-port", type=int, default=59539)
    parser.add_argument("--source-host", default="192.168.88.253", help="CRS 37 (SN0037)")
    parser.add_argument("--source-port", type=int, default=7, help="UCAP TCP ping / source port")
    parser.add_argument("--rest-host", default="127.0.0.1")
    parser.add_argument("--rest-port", type=int, default=54322)
    parser.add_argument("--nchan", type=int, default=8)
    parser.add_argument("--number-of-frames", type=int, default=100)
    parser.add_argument("--seconds", type=float, default=120.0)
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
