#!/usr/bin/env python3
"""Decode one assembled ch0 frame from live UDP and compare endian / LSB views."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from check_raw_acq_udp import _capture_states, _json_safe

from pychfpga.raw_acq.receiver import RawAcqReceiver


async def main() -> None:
    recv = RawAcqReceiver()
    await recv.start_async(
        name="wire-fmt",
        ports=[{"port": 33745, "sources": [["192.168.88.109", 62510]]}],
        stream_ids=list(range(8)),
        raw_capture_state=_json_safe(_capture_states(for_rest=False)),
    )
    await asyncio.sleep(2.0)
    adc = recv.processors["adc"]
    for _ in range(300):
        adc.process_packets()
        if (adc.raw_data_count > 0).any():
            break
    recv.stop()

    cix = 0
    tix = int(np.argmax(adc.raw_data_count[:, cix]))
    wire = np.asarray(adc.raw_data[tix, cix], dtype=np.uint8)
    formatted = adc.format_data(wire.reshape(1, 1, -1))[0, 0]
    frame = adc._channel_frame(wire)

    def rms(x):
        x = np.asarray(x, dtype=np.float64)
        return float(np.sqrt(np.mean((x - x.mean()) ** 2)))

    print(f"format={adc.format!r} bytes_per_sample={adc.BYTES_PER_SAMPLE} storage={adc.adc_storage_dtype}")
    print(f"complete packets={adc.raw_data_count[tix, cix]} expected={adc.expected_packets}")
    print(f"samples_per_row={frame.size} (HDF5 width={adc.adc_samples_per_frame})")
    print(f"wire hex (first 16 B): {wire[:16].tobytes().hex()}")
    print(f"format_data first 8: {formatted[:8]}  rms={rms(formatted):.2f}")
    print(f"_channel_frame first 8: {frame[:8]}  rms={rms(frame):.2f} min/max={frame.min()}/{frame.max()}")


if __name__ == "__main__":
    asyncio.run(main())
