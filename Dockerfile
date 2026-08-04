# syntax=docker/dockerfile:1

FROM python:3.12-slim AS builder

WORKDIR /build

ARG PYCHFPGA_VERSION=0+docker

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SETUPTOOLS_SCM_PRETEND_VERSION=${PYCHFPGA_VERSION} \
    SETUPTOOLS_SCM_PRETEND_VERSION_FOR_PYCHFPGA=${PYCHFPGA_VERSION}

ENV VIRTUAL_ENV=/opt/venv
ENV PATH="${VIRTUAL_ENV}/bin:${PATH}"

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        git \
        git-lfs \
        libffi-dev \
        libhdf5-dev \
        libssl-dev \
        openssh-client \
        pkg-config; \
    git lfs install; \
    mkdir -p -m 0700 /root/.ssh; \
    ssh-keyscan bitbucket.org >> /root/.ssh/known_hosts; \
    rm -rf /var/lib/apt/lists/*

RUN python -m venv "${VIRTUAL_ENV}"

COPY . /build

RUN --mount=type=ssh \
    set -eux; \
    python -m pip install --upgrade pip setuptools wheel; \
    python -m pip install .

FROM python:3.12-slim AS runtime

WORKDIR /pychfpga

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

ENV VIRTUAL_ENV=/opt/venv
ENV PATH="${VIRTUAL_ENV}/bin:${PATH}"

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        curl \
        iputils-ping \
        libhdf5-310 \
        openssh-client; \
    rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv
COPY docker/start_fpga_master.sh docker/wait_for_iceboards.py /pychfpga/docker/

EXPOSE 54321 54322 54326/udp 5353/udp 6000-6511/udp

CMD ["bash", "-c", "echo 'Specify a command such as pychfpga_raw_acq or /pychfpga/docker/start_fpga_master.sh.' >&2; exit 1"]
