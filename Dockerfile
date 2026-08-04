# syntax=docker/dockerfile:1

FROM python:3.12-slim

WORKDIR /pychfpga

ARG PYCHFPGA_VERSION=0+docker

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SETUPTOOLS_SCM_PRETEND_VERSION=${PYCHFPGA_VERSION} \
    SETUPTOOLS_SCM_PRETEND_VERSION_FOR_PYCHFPGA=${PYCHFPGA_VERSION}

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        bash \
        build-essential \
        ca-certificates \
        curl \
        gcc \
        g++ \
        git \
        git-lfs \
        iputils-ping \
        libffi-dev \
        libhdf5-dev \
        libssl-dev \
        openssh-client \
        pkg-config; \
    git lfs install; \
    mkdir -p -m 0700 /root/.ssh; \
    ssh-keyscan bitbucket.org >> /root/.ssh/known_hosts; \
    rm -rf /var/lib/apt/lists/*

COPY . /pychfpga

RUN --mount=type=ssh \
    set -eux; \
    python -m pip install --upgrade pip setuptools wheel; \
    python -m pip install .

EXPOSE 54321 54322 54326/udp 5353/udp 6000-6511/udp

CMD ["bash", "-lc", "echo 'Specify a command such as pychfpga_raw_acq or /pychfpga/docker/start_fpga_master.sh.' >&2; exit 1"]
