# syntax=docker/dockerfile:1

# Build from a checkout where `git lfs pull` has been run (bitstreams and firmware are git-lfs files):
#
#   docker build --target deploy -t pychfpga:local .
#   docker build --target deploy -t pychfpga:local --build-arg PYCHFPGA_VERSION=2.2.0+crs_ct .
#
# export BUILDKIT_PROGRESS=plain # optional, for more verbose output
#
# Release images are built and pushed with ./docker_build
#
# Based on recommendations at
#   https://testdriven.io/blog/docker-best-practices/
#
########################################################
# Base Image with Linux Dependencies
########################################################
FROM python:3.12-slim AS base

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Install Linux Dependencies
RUN set -ex \
    && apt-get update -yqq \
    && apt-get install -yqq --no-install-recommends \
        # include curl for health checks of REST servers
        curl \
        # compiler for dependencies without Python 3.12 wheels (netifaces)
        build-essential \
        # git is needed to install the git+https dependencies
        git \
        git-lfs \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    # make sure git LFS is initialized
    && git lfs install

########################################################
# Python Dependencies
########################################################
FROM base AS pychfpga_install

# .git is not in the build context, so setuptools-scm cannot find the version: pass it explicitly.
ARG PYCHFPGA_VERSION=0.0.0
ENV SETUPTOOLS_SCM_PRETEND_VERSION_FOR_PYCHFPGA=$PYCHFPGA_VERSION

# Change directory to /pychfpga
WORKDIR /pychfpga

# We install the python packages in a virtual environment so all the install products are in a single place so we can copy them in the final image
ENV UV_PROJECT_ENVIRONMENT=/pychfpga/.venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Pre-install pychfpga requirements since these change very rarely
COPY pyproject.toml uv.lock ./
RUN set -ex \
    && uv sync --frozen --no-install-project --no-dev

# Install pychfpga repo. This step is re-done every time any of the files in the repo changes
COPY . .
RUN set -ex \
    # Fail if git-lfs files were not pulled: pointer files are ~130 bytes
    && if find pychfpga -name '*.bit' -size -1k | grep . \
          || find pychfpga/hardware/crs/arm_firmware -name '*.elf' -size -1k | grep . ; then \
        echo "git-lfs pointer files found. Run 'git lfs pull' before building." ; exit 1 ; \
       fi \
    && uv sync --frozen --no-dev --no-editable


########################################################
# Deployment image
########################################################

FROM python:3.12-slim AS deploy
WORKDIR /pychfpga

RUN set -ex \
    && apt-get update -yqq \
    && apt-get install -yqq --no-install-recommends \
        # include curl for health checks of REST servers
        curl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Setup the path to point to the virtual environment that we will copy in the next step
ENV VIRTUAL_ENV=/pychfpga/.venv
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

COPY --from=pychfpga_install $VIRTUAL_ENV $VIRTUAL_ENV

# CRS U-Boot image and upgrade tool: python /opt/pychfpga/crs_arm_firmware/fwupgrade.py
RUN mkdir -p /opt/pychfpga \
    && ln -s $VIRTUAL_ENV/lib/python3.12/site-packages/pychfpga/hardware/crs/arm_firmware /opt/pychfpga/crs_arm_firmware

# Exposing ports normally used by the application
# (CRS discovery beacons use UDP 9875; run with --network host for board discovery)
EXPOSE 54321-54325 54326/udp 5353/udp 9875/udp 6000-6511/udp

# Default entrypoint
CMD ["echo", "Please specify pychfpga command: fpga_master <config_file>:<item>, raw_acq, crs_discovery, pychfpga_ps <config_file>:<item>, pychfpga_gps <config_file>:<item>"]

# FROM runtime as production
# RUN set -ex \
#     && rm -rf /pychfpga/docs \
#     && rm -rf /pychfpga/pychfpga/arm_firmware \
#     && rm -rf /pychfpga/.git

# FROM runtime as developer
# RUN set -ex \
#     && pip install ipython
