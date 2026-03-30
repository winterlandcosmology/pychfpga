#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
ENV_FILE="${PYCHFPGA_ENV_FILE:-}"
VERSION_FILE="${REPO_ROOT}/pychfpga/_version_scm.py"

if [[ -z "${ENV_FILE}" && -f "${REPO_ROOT}/.env.docker" ]]; then
    ENV_FILE="${REPO_ROOT}/.env.docker"
fi

if [[ -n "${ENV_FILE}" ]]; then
    if [[ ! -f "${ENV_FILE}" ]]; then
        echo "Env file not found: ${ENV_FILE}" >&2
        exit 1
    fi
    set -a
    # shellcheck disable=SC1090
    source "${ENV_FILE}"
    set +a
fi

IMAGE_NAME="${PYCHFPGA_IMAGE:-pychfpga:local}"
SSH_KEY_PATH="${PYCHFPGA_SSH_KEY_PATH:-}"
PYCHFPGA_VERSION="${PYCHFPGA_VERSION:-}"

if [[ -z "${SSH_KEY_PATH}" ]]; then
    echo "PYCHFPGA_SSH_KEY_PATH must point to the private SSH key used for Bitbucket access." >&2
    exit 1
fi

if [[ ! -f "${SSH_KEY_PATH}" ]]; then
    echo "SSH key file not found: ${SSH_KEY_PATH}" >&2
    exit 1
fi

if [[ -z "${PYCHFPGA_VERSION}" && -f "${VERSION_FILE}" ]]; then
    PYCHFPGA_VERSION="$(sed -n "s/^__version__ = version = '\(.*\)'$/\1/p" "${VERSION_FILE}" | head -n 1)"
fi

if [[ -z "${PYCHFPGA_VERSION}" ]]; then
    PYCHFPGA_VERSION="0+docker"
fi

export DOCKER_BUILDKIT=1

printf 'Building Docker image %s with SSH key %s and package version %s\n' "${IMAGE_NAME}" "${SSH_KEY_PATH}" "${PYCHFPGA_VERSION}"
exec docker build \
    --build-arg "PYCHFPGA_VERSION=${PYCHFPGA_VERSION}" \
    --ssh "default=${SSH_KEY_PATH}" \
    -t "${IMAGE_NAME}" \
    "${REPO_ROOT}"
