#!/usr/bin/env bash

set -euo pipefail

CONFIG_PATH="${PYCHFPGA_CONFIG_PATH:-/config/fpga_config.yaml}"

# Keep the container running instead of exiting: with `restart: unless-stopped`
# Docker would start fpga_master again, reprogramming every FPGA on each attempt.
# A host or Docker restart still gives the held container one fresh attempt.
hold() {
    echo "$1" >&2
    echo "Holding this container instead of exiting, because a restart would reprogram the FPGAs again." >&2
    echo "Fix the cause, then run: docker compose restart fpga_master" >&2
    exec sleep infinity
}

if [[ -z "${PYCHFPGA_CONFIG_NAME:-}" ]]; then
    echo "PYCHFPGA_CONFIG_NAME must be set." >&2
    exit 1
fi

if [[ ! -f "${CONFIG_PATH}" ]]; then
    echo "Config file not found at ${CONFIG_PATH}." >&2
    exit 1
fi

python /pychfpga/docker/wait_for_iceboards.py

fpga_master "${CONFIG_PATH}:${PYCHFPGA_CONFIG_NAME}" \
    || hold "fpga_master exited with status $?."
