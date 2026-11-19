#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
ENV_FILE="${PYCHFPGA_ENV_FILE:-}"

if [[ -z "${ENV_FILE}" && -f "${REPO_ROOT}/.env" ]]; then
    ENV_FILE="${REPO_ROOT}/.env"
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
PYCHFPGA_VERSION="${PYCHFPGA_VERSION:-}"

# --- Validation: nothing below runs docker until the build inputs are known good ---

# The bitstreams are git-lfs files and the build context excludes .git, so the
# pointer stubs of a checkout without `git lfs pull` would be packaged as-is.
LFS_HEADER='version https://git-lfs.github.com/spec/v1'
for bitstream in "${REPO_ROOT}"/pychfpga/fpga_firmware/bitstreams/*.bit; do
    if cmp -s -n "${#LFS_HEADER}" "${bitstream}" <(printf '%s' "${LFS_HEADER}"); then
        echo "${bitstream#"${REPO_ROOT}/"} is a git-lfs pointer, not a bitstream." >&2
        echo "Run 'git lfs pull' and build again." >&2
        exit 1
    fi
done

# The image is built from the working tree as-is. Label it with the version
# `pip install` would give this checkout: setuptools-scm's version of HEAD, with
# a .dYYYYMMDD suffix when tracked files are modified. No tag is required.
if [[ -z "${PYCHFPGA_VERSION}" ]]; then
    PYTHON="${REPO_ROOT}/.venv/bin/python"
    [[ -x "${PYTHON}" ]] || PYTHON=python3
    if ! PYCHFPGA_VERSION="$(cd "${REPO_ROOT}" && "${PYTHON}" -m setuptools_scm)"; then
        echo "Cannot compute the package version with setuptools-scm (${PYTHON})." >&2
        echo "Install it with: pip install 'setuptools-scm>=8', or set PYCHFPGA_VERSION." >&2
        exit 1
    fi
fi

# setuptools-scm ignores untracked files, but they still enter the image.
CHANGES="$(git -C "${REPO_ROOT}" status --porcelain)"
if [[ -n "${CHANGES}" ]]; then
    echo "Warning: the image includes these uncommitted or untracked changes:" >&2
    echo "${CHANGES}" >&2
fi

# --- Build ---

export DOCKER_BUILDKIT=1

printf 'Building Docker image %s with package version %s\n' \
    "${IMAGE_NAME}" "${PYCHFPGA_VERSION}"
exec docker build \
    --build-arg "PYCHFPGA_VERSION=${PYCHFPGA_VERSION}" \
    -t "${IMAGE_NAME}" \
    "${REPO_ROOT}"
