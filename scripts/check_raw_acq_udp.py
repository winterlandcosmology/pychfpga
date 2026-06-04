#!/usr/bin/env python3
"""Quick UDP check and optional raw_acq receiver start on a live port."""

import argparse
import asyncio
import socket
import sys
import time


def udp_listen(port: int, seconds: float) -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", port))
    sock.settimeout(1.0)
    print(f"Listening on 0.0.0.0:{port} for {seconds:.0f}s ...")
    count = 0
    nbytes = 0
    t_end = time.time() + seconds
    while time.time() < t_end:
        try:
            data, addr = sock.recvfrom(65535)
            count += 1
            nbytes += len(data)
            if count <= 5 or count % 100 == 0:
                print(f"  pkt {count}: {len(data)} bytes from {addr[0]}:{addr[1]}")
        except socket.timeout:
            pass
    sock.close()
    print(f"UDP summary: {count} packets, {nbytes} total bytes")
    return count


def _ucap_adc_state(stream_ids=None, target_mode=0):
    """UCAP funcgen/ADC geometry for 8202-byte ``0xA0`` packets (CRS-like)."""
    if stream_ids is None:
        stream_ids = tuple(range(8))
    frames_per_burst = {0: 2, 1: 4, 2: 8, 3: 16}[target_mode]
    nchan = len(stream_ids)
    return dict(
        capture_engine="UCAP",
        source="funcgen",
        channels=tuple(range(nchan)),
        stream_ids=tuple(int(s) for s in stream_ids),
        frames_per_burst=frames_per_burst,
        subframes_per_frame=4,
        target_mode=target_mode,
        bytes_per_sample=2,
        storage_dtype="i2",
        samples_per_frame=16384,
        bytes_per_frame=32768,
        bytes_per_subframe=8192,
        packets_per_channel_per_burst=frames_per_burst * 4,
        packets_per_logical_frame=1,
        logical_frames_per_burst=frames_per_burst,
        fft_bins_per_word=1,
        raw_decoder=dict(format="16"),
        packet_header_format="raw-v1",
        format="16",
        split=False,
        packet_cookie=0xA0,
        packet_cookie_mask=0xFF,
        receiver_recreate_attrs=(
            "capture_engine",
            "bytes_per_sample",
            "storage_dtype",
            "samples_per_frame",
            "frames_per_burst",
            "subframes_per_frame",
            "stream_ids",
            "packets_per_logical_frame",
            "logical_frames_per_burst",
            "packet_header_format",
        ),
        receiver_update_attrs=("stream_ids", "target_mode", "source", "format", "split"),
    )


def _ucap_fft_state(stream_ids=None, target_mode=0):
    """Placeholder FFT/scaler state (not used while FPGA sends ``0xA0``)."""
    state = _ucap_adc_state(stream_ids=stream_ids, target_mode=target_mode)
    state = dict(state)
    state.update(source="scaler", packet_cookie=0xA1, format="16", raw_decoder=dict(format="16"))
    return state


def _corr_state_minimal():
    """Correlator params only (maps built in ``CorrDataProcessor.update_config``)."""
    n_bins, n_decim, n_chan = 2048, 2, 16
    fft_bin_map = [
        list(row)
        for row in __import__("numpy").arange(n_bins).reshape(4, n_bins // 4)
    ]
    return dict(
        NCHAN=n_chan,
        NCLK=4,
        CT_LEVEL=1,
        NDECIM=n_decim,
        fft_bin_map=fft_bin_map,
        firmware_integ_period=1,
        soft_integ_period=1,
    )


def _corr_state():
    """Full correlator state for local receiver tests (includes ``bin_map`` arrays)."""
    import numpy as np

    from pychfpga.common.state import State
    from pychfpga.fpga_firmware.chfpga.x_engine.UCORR import UCORR

    corr = State(**_corr_state_minimal())
    corr.fft_bin_map = np.asarray(corr.fft_bin_map)
    UCORR.update_corr_maps(corr)
    return corr.as_dict()


def _json_safe(obj):
    """Convert numpy scalars/arrays in capture state for REST JSON."""
    import numpy as np

    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, np.generic):
        return obj.item()
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


def _capture_states(*, for_rest=False):
    stream_ids = tuple(range(8))
    return {
        "adc": _ucap_adc_state(stream_ids=stream_ids),
        "fft": _ucap_fft_state(stream_ids=stream_ids),
        "corr": _corr_state_minimal() if for_rest else _corr_state(),
    }


def _capture_states_json():
    return _json_safe(_capture_states(for_rest=True))


async def run_raw_acq(port: int, source_host: str, source_port: int, seconds: float) -> None:
    from pychfpga.raw_acq.receiver import RawAcqReceiver

    recv = RawAcqReceiver()

    # key_port = UDP source port in packets; TCP ping uses CRS echo (port 7) by default.
    ports = [{"port": port, "sources": [[source_host, source_port]]}]
    raw_capture_state = _capture_states(for_rest=False)
    print(f"Starting RawAcqReceiver on UDP port {port} (ping mocked) ...")
    result = await recv.start_async(
        name="live-check",
        ports=ports,
        stream_ids=list(range(8)),
        raw_capture_state=raw_capture_state,
    )
    print("start_async returned:", result)
    t0 = time.time()
    last = 0
    while time.time() - t0 < seconds:
        await asyncio.sleep(1.0)
        n = recv.received_packets
        stored = recv.n_stored
        adc = recv.processors["adc"]
        if n != last:
            print(
                f"  received_packets={n}  n_stored={stored}  "
                f"adc_emitted_ts={adc.n_ts}  adc_processed={getattr(adc, 'adc_current_processed_packets', '?')}"
            )
            last = n
    recv.stop()
    print(f"Final received_packets={recv.received_packets}")


async def run_rest(port: int, source_host: str, source_port: int, seconds: float) -> None:
    import aiohttp

    base_url = "http://127.0.0.1:54322"
    start_body = {
        "comet_broker": {"enabled": False},
        "ports": [{"port": port, "sources": [[source_host, source_port]]}],
        "stream_ids": [0x100],
        "raw_capture_state": _capture_states_json(),
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(f"{base_url}/start", json=start_body) as resp:
            text = await resp.text()
            print(f"POST /start -> {resp.status}: {text[:500]}")
            if resp.status >= 400:
                return

        t0 = time.time()
        while time.time() - t0 < seconds:
            await asyncio.sleep(2.0)
            async with session.get(f"{base_url}/status") as resp:
                print(f"GET /status -> {await resp.json()}")

        async with session.get(f"{base_url}/stop") as resp:
            print(f"GET /stop -> {await resp.text()}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=33745)
    parser.add_argument("--source-host", default="192.168.88.109")
    parser.add_argument("--source-port", type=int, default=62510)
    parser.add_argument("--seconds", type=float, default=10.0)
    parser.add_argument("--mode", choices=("udp", "receiver", "rest"), default="udp")
    args = parser.parse_args()

    if args.mode == "udp":
        n = udp_listen(args.port, args.seconds)
        sys.exit(0 if n else 1)

    if args.mode == "receiver":
        asyncio.run(run_raw_acq(args.port, args.source_host, args.source_port, args.seconds))
        return

    print("Start raw_acq REST server separately: python3 -m pychfpga.raw_acq")
    asyncio.run(run_rest(args.port, args.source_host, args.source_port, args.seconds))


if __name__ == "__main__":
    main()
