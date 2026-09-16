#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$ROOT/.py312_venv/bin/python"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
OUT_DIR="$ROOT/captures/sinewave_ch0_test"
mkdir -p "$OUT_DIR"
fuser -k 54322/tcp 2>/dev/null || true
sleep 2
cd "$ROOT"
"$VENV" -m pychfpga.raw_acq >> /tmp/raw_acq_server.log 2>&1 &
SRV=$!
sleep 5
"$VENV" "$ROOT/scripts/capture_and_plot_sinewave.py"
kill "$SRV" 2>/dev/null || fuser -k 54322/tcp 2>/dev/null || true
