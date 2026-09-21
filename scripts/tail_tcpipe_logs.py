#!/usr/bin/env python3
"""Continuously print TCPipe logs from every board, reconnecting as needed.

The firmware log listener is TCP port 9875.  Unlike a one-shot client, this
tool keeps attempting an unreachable board, which makes an RPC-related loss of
network service visible as both the final log line and a reconnect failure.

Examples:
    ./scripts/tail_tcpipe_logs.py
    ./scripts/tail_tcpipe_logs.py --host 192.168.88.19 --host 192.168.88.26
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import socket
import sys
import time


DEFAULT_HOSTS = ("192.168.88.19", "192.168.88.26", "192.168.88.186", "192.168.88.251")
DEFAULT_PORT = 9875
# Firmware HEALTH lines are every 5 s; treat a longer silence as a dead socket.
DEFAULT_IDLE_S = 15.0


def emit(host: str, message: str) -> None:
    """Print one board-tagged message without buffering."""
    stamp = time.strftime("%H:%M:%S")
    print(f"{stamp} [{host}] {message}", flush=True)


def _exc_text(exc: BaseException) -> str:
    text = str(exc).strip()
    return text if text else type(exc).__name__


async def _close_writer(writer: asyncio.StreamWriter | None) -> None:
    if writer is None:
        return
    writer.close()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(writer.wait_closed(), timeout=1.0)


async def tail_host(
        host: str,
        port: int,
        connect_timeout: float,
        retry_s: float,
        idle_s: float,
) -> None:
    """Connect, print complete log lines, and retry forever after disconnects."""
    last_fail_log = 0.0
    while True:
        writer: asyncio.StreamWriter | None = None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=connect_timeout
            )
            sock = writer.get_extra_info("socket")
            if sock is not None:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            emit(host, f"connected TCP log :{port}")
            last_fail_log = 0.0
            while True:
                try:
                    line = await asyncio.wait_for(reader.readline(), timeout=idle_s)
                except asyncio.TimeoutError:
                    emit(host, f"no log line for {idle_s:g}s; reconnecting")
                    break
                if not line:
                    emit(host, "TCP log closed; reconnecting")
                    break
                emit(host, line.decode("utf-8", errors="replace").rstrip("\r\n"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            now = time.monotonic()
            if now - last_fail_log >= 5.0:
                emit(host, f"TCP log unavailable ({_exc_text(exc)}); retrying")
                last_fail_log = now
        finally:
            await _close_writer(writer)
        await asyncio.sleep(retry_s)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--host",
        action="append",
        metavar="IP_OR_HOST",
        help="board to monitor; repeat for more boards (default: all CRS boards)",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="TCP log port")
    parser.add_argument(
        "--connect-timeout", type=float, default=3.0, metavar="SEC",
        help="per-board connection timeout (default: 3)",
    )
    parser.add_argument(
        "--retry", type=float, default=0.5, metavar="SEC",
        help="delay before reconnecting a board (default: 0.5)",
    )
    parser.add_argument(
        "--idle", type=float, default=DEFAULT_IDLE_S, metavar="SEC",
        help=(
            "reconnect if a live session produces no line for this long "
            f"(default: {DEFAULT_IDLE_S:g})"
        ),
    )
    return parser.parse_args(argv)


async def supervise_host(
        host: str,
        port: int,
        connect_timeout: float,
        retry_s: float,
        idle_s: float,
) -> None:
    """Keep one board's tail loop alive even if it raises unexpectedly."""
    while True:
        try:
            await tail_host(host, port, connect_timeout, retry_s, idle_s)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            emit(host, f"tail loop crashed ({_exc_text(exc)}); restarting")
        await asyncio.sleep(retry_s)


async def main_async(args: argparse.Namespace) -> None:
    hosts = tuple(args.host or DEFAULT_HOSTS)
    emit("-", f"monitoring {len(hosts)} boards on TCP :{args.port}")
    tasks = [
        asyncio.create_task(
            supervise_host(
                host, args.port, args.connect_timeout, args.retry, args.idle
            ),
            name=f"tcpipe-log-{host}",
        )
        for host in hosts
    ]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\nTCP log monitor stopped", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
