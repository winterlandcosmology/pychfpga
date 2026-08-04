#!/usr/bin/env python3

import asyncio
import logging
import math
import os
import sys
import time


LOGGER = logging.getLogger("pychfpga.docker.wait_for_iceboards")
DEFAULT_PING_TIMEOUT = 1.0


def _parse_hosts():
    return [
        host.strip()
        for host in os.environ.get("PYCHFPGA_WAIT_HOSTS", "").split(",")
        if host.strip()
    ]


def _parse_timeout():
    raw_timeout = os.environ.get("PYCHFPGA_WAIT_TIMEOUT", "").strip()
    if not raw_timeout:
        return None
    try:
        timeout = float(raw_timeout)
    except ValueError as exc:
        raise ValueError(
            "PYCHFPGA_WAIT_TIMEOUT must be a number of seconds.") from exc
    return timeout if timeout > 0 else None


def _parse_ping_timeout():
    raw_timeout = os.environ.get("PYCHFPGA_WAIT_PING_TIMEOUT", "").strip()
    if not raw_timeout:
        raw_timeout = os.environ.get(
            "PYCHFPGA_WAIT_CONNECT_TIMEOUT", "").strip()
    if not raw_timeout:
        return DEFAULT_PING_TIMEOUT
    try:
        timeout = float(raw_timeout)
    except ValueError as exc:
        raise ValueError(
            "PYCHFPGA_WAIT_PING_TIMEOUT must be a number of seconds.") from exc
    if timeout <= 0:
        raise ValueError(
            "PYCHFPGA_WAIT_PING_TIMEOUT must be greater than zero.")
    return timeout


async def _ping_host(host, ping_timeout):
    try:
        process = await asyncio.create_subprocess_exec(
            "ping",
            "-c",
            "1",
            "-W",
            str(max(1, math.ceil(ping_timeout))),
            host,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError:
        LOGGER.error("The 'ping' command is not available in the container.")
        raise

    try:
        return await asyncio.wait_for(
            process.wait(), timeout=ping_timeout + 1) == 0
    except asyncio.TimeoutError:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.wait()
        LOGGER.debug("Ping probe timed out for %s", host)
        return False


async def _wait_for_hosts(hosts, timeout, ping_timeout):
    start_time = time.monotonic()
    attempt = 1

    while True:
        results = await asyncio.gather(
            *(_ping_host(host, ping_timeout) for host in hosts))
        pending_hosts = [
            host for host, is_ready in zip(hosts, results) if not is_ready]

        if not pending_hosts:
            LOGGER.info(
                "All IceBoards became reachable after %.1fs: %s",
                time.monotonic() - start_time,
                ", ".join(hosts),
            )
            return 0

        elapsed = time.monotonic() - start_time
        if timeout is not None and elapsed >= timeout:
            LOGGER.error(
                "Timed out after %.1fs waiting for IceBoards to answer ping: %s",
                elapsed,
                ", ".join(pending_hosts),
            )
            return 1

        LOGGER.info(
            "Attempt %d: waiting for IceBoards to answer ping: %s",
            attempt,
            ", ".join(pending_hosts),
        )
        attempt += 1
        await asyncio.sleep(1)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    try:
        timeout = _parse_timeout()
        ping_timeout = _parse_ping_timeout()
    except ValueError as exc:
        LOGGER.error("%s", exc)
        return 2

    hosts = _parse_hosts()
    if not hosts:
        LOGGER.info(
            "PYCHFPGA_WAIT_HOSTS is empty. Skipping IceBoard readiness wait.")
        return 0

    LOGGER.info("Waiting for IceBoards to answer ping: %s", ", ".join(hosts))

    try:
        return asyncio.run(_wait_for_hosts(hosts, timeout, ping_timeout))
    except KeyboardInterrupt:
        LOGGER.warning("Interrupted while waiting for IceBoards.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
