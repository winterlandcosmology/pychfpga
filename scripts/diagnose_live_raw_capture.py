#!/usr/bin/env python3
"""Diagnose live RawDataProcessor capture on a UDP port (REST start + metrics)."""

import argparse
import gzip
import json
import sys
import time
import urllib.request

# reuse firmware stub from live FFT script
from start_fft_rms_live import crs_ucap_fft_2x_firmware_config


def post_json(url, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, r.read().decode()


def get_metrics():
    req = urllib.request.Request(
        "http://127.0.0.1:54322/get-monitoring-data",
        headers={"Accept-Encoding": "gzip"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        text = gzip.decompress(r.read()).decode()
    out = {}
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        name_labels, val, _ts = line.rsplit(" ", 2)
        out[name_labels.split("{")[0]] = float(val)
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--rest-port", type=int, default=54322)
    p.add_argument("--udp-port", type=int, default=59539)
    p.add_argument("--source-host", default="192.168.88.253")
    p.add_argument("--seconds", type=float, default=8.0)
    args = p.parse_args()
    base = f"http://127.0.0.1:{args.rest_port}"

    firmware = crs_ucap_fft_2x_firmware_config(list(range(8)))
    start_body = {
        "name": "diag_capture",
        "comet_broker": {"enabled": False},
        "no_ping": True,
        "ports": [{"port": args.udp_port, "sources": [[args.source_host, 7]]}],
        "stream_ids": list(range(8)),
        "config": firmware,
        "metrics_refresh_time": 1,
    }
    print("POST /start ...")
    status, body = post_json(f"{base}/start", start_body)
    print(f"  -> {status}: {body[:300]}")

    m0 = get_metrics()
    time.sleep(args.seconds)
    m1 = get_metrics()

    keys = [
        "raw_acq_received_packets",
        "raw_acq_processed_packets",
        "raw_acq_processed_adc_packets",
        "raw_acq_processed_fft_packets",
        "raw_acq_corr_udp_cookie_packets",
        "raw_acq_corr_udp_packets",
        "raw_acq_corr_integrations_emitted",
    ]
    print(f"\n=== {args.seconds:.0f}s delta ===")
    for k in keys:
        d = m1.get(k, 0) - m0.get(k, 0)
        print(f"  {k}: {d:+.0f}  (total {m1.get(k, 0):.0f})")

    # check log tail for errors
    log_path = "/home/jfcliche/temp/data/raw_acq_fft_rms.log"
    try:
        with open(log_path) as f:
            lines = f.readlines()[-40:]
        errs = [ln.strip() for ln in lines if "ERROR" in ln and "process_packets" in ln]
        if errs:
            print(f"\n=== recent process_packets errors ({len(errs)} lines) ===")
            print(errs[-3])
    except OSError:
        pass

    recv_d = m1.get("raw_acq_received_packets", 0) - m0.get("raw_acq_received_packets", 0)
    proc_d = m1.get("raw_acq_processed_fft_packets", 0) - m0.get("raw_acq_processed_fft_packets", 0)
    if recv_d > 0 and proc_d == 0:
        print("\nDIAGNOSIS: UDP arriving but RawDataProcessor FFT path processed 0 packets.")
        print("Likely causes: packet filter reject (size/stream/mode/ts), corr exception aborting")
        print("batch before metrics flush, or get_formatted_data(source=1) zero counts.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
