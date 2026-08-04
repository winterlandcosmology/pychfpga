#!/usr/bin/env bash

set -euo pipefail

CONFIG_PATH="${PYCHFPGA_CONFIG_PATH:-/config/fpga_config.yaml}"

if [[ -z "${PYCHFPGA_CONFIG_NAME:-}" ]]; then
    echo "PYCHFPGA_CONFIG_NAME must be set." >&2
    exit 1
fi

if [[ ! -f "${CONFIG_PATH}" ]]; then
    echo "Config file not found at ${CONFIG_PATH}." >&2
    exit 1
fi

python /pychfpga/docker/wait_for_iceboards.py

exec fpga_master "${CONFIG_PATH}:${PYCHFPGA_CONFIG_NAME}"
