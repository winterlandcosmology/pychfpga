#!/usr/bin/env python3
"""Copy a newly built tcpipe_rtos.elf into the CRS firmware folder, then
power-cycle the crate, while printing board consoles.

Typical loop while iterating on TCPipe in Vitis:

    ~/pychfpga/.py312_venv/bin/python ~/pychfpga/scripts/watch_tcpipe.py
    ./watch_tcpipe.py 55 37 48 39
    ./watch_tcpipe.py '*'
    ./watch_tcpipe.py all
    ./watch_tcpipe.py '5*'
    ./watch_tcpipe.py /dev/ttyUSB2 '*'

Positional targets:

    a.b.c.d          IPv4 address; TCP log on that host
    /dev/...         UART device (character device path)
    glob or integer  serial glob matched against discovered board serials
                     (``55`` matches ``0055`` / ``r4b.0055``; ``*`` or ``all``
                     matches every board). Quote ``*`` so the shell does not
                     expand it; unquoted ``*`` is ignored as cwd filenames
                     and treated as ``all``.

UDP beacons are always listened for. A TCP log session is started for each
discovered board whose serial matches any provided glob. With no serial glob
and no explicit IP, the glob defaults to ``*``.

An existing ELF at startup is ignored unless ``--once`` is used. When the
Release ELF appears or changes, it is copied into the CRS ``arm_firmware``
folder and the crate PDU outlet is cycled. UART printing continues across
the cycle (the USB serial device drops and comes back). Console lines are
written to ``all.log`` in the current directory by default (overwritten at
each start, and truncated again on each crate power cycle). ``--log PATH``
selects a different file.

Power cycle uses the Synaccess NetCommander (same unit as the CRS notebooks):

    nc = NetCommander('TCPIP::PDU_609c69::TELNET')
    nc.set_power(2, 0)   # off
    # wait --off-delay seconds
    nc.set_power(2, 1)   # on

With no targets, the default UART is the by-id path for what is currently
``/dev/ttyUSB3`` (Xilinx CRS SN00039, interface 1 = PS console). After a
power cycle the ``ttyUSBn`` number often moves; the by-id symlink does not.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import fcntl
import fnmatch
import ipaddress
import os
import re
import shutil
import socket
import stat
import sys
import threading
import time
from pathlib import Path
from typing import TextIO

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from pychfpga.hardware.crs.crs_discovery import (  # noqa: E402
    CRSBeacon,
    DEFAULT_DISCOVERY_PORT,
    request_beacon,
    serials_equal,
)

DEFAULT_SRC = Path.home() / "tcpipe/tcpipe_rtos/Release/tcpipe_rtos.elf"
DEFAULT_DEST_DIR = (
    Path(__file__).resolve().parent.parent
    / "pychfpga/hardware/crs/arm_firmware"
)
DEFAULT_PDU = "TCPIP::PDU_609c69::TELNET"
DEFAULT_PDU_PORT = 2
DEFAULT_OFF_DELAY_S = 3.0
DEFAULT_SETTLE_S = 2.0
DEFAULT_POLL_S = 0.5
# Currently /dev/ttyUSB3; if01 is the Zynq PS UART on this JTAG/UART composite.
DEFAULT_SERIAL = "/dev/serial/by-id/usb-Xilinx_t0.technology_CRS_00039-if01-port0"
DEFAULT_BAUD = 115200
DEFAULT_SERIAL_REOPEN_DELAY_S = 2.0
DEFAULT_TCP_PORT = 9875
DEFAULT_TCP_IDLE_S = 15.0
DEFAULT_TCP_CONNECT_S = 3.0
DEFAULT_TCP_RETRY_S = 0.5
DEFAULT_LOG = Path("all.log")
BEACON_REQUEST_S = 5.0
USBDEVFS_RESET = 21780  # _IO('U', 20)
NO_UART_BYTES_S = 10.0

_PRINT_LOCK = threading.Lock()
_session_log: TextIO | None = None
_session_log_path: Path | None = None


def _stamp() -> str:
    return time.strftime("%H:%M:%S")


def _session_write_unlocked(line: str) -> None:
    if _session_log is not None:
        _session_log.write(line + "\n")
        _session_log.flush()


def log(msg: str) -> None:
    line = f"{_stamp()} {msg}"
    with _PRINT_LOCK:
        print(line, file=sys.stderr, flush=True)
        _session_write_unlocked(line)


def emit(tag: str, message: str) -> None:
    """Print one board-tagged log line without buffering."""
    line = f"{_stamp()} [{tag}] {message}"
    with _PRINT_LOCK:
        print(line, flush=True)
        _session_write_unlocked(line)


def open_session_log(path: Path) -> None:
    """Start a session log file, replacing any previous contents."""
    global _session_log, _session_log_path

    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _PRINT_LOCK:
        if _session_log is not None:
            _session_log.close()
            _session_log = None
        _session_log_path = path
        _session_log = path.open("w", encoding="utf-8")
        header = f"{_stamp()} log file opened {path}"
        print(header, file=sys.stderr, flush=True)
        _session_write_unlocked(header)


def restart_session_log(reason: str) -> None:
    """Truncate and reopen the session log (crate power cycle)."""
    global _session_log

    if _session_log_path is None:
        return
    with _PRINT_LOCK:
        if _session_log is not None:
            _session_log.close()
            _session_log = None
        _session_log = _session_log_path.open("w", encoding="utf-8")
        header = f"{_stamp()} log file restarted ({reason})"
        print(header, file=sys.stderr, flush=True)
        _session_write_unlocked(header)


def close_session_log() -> None:
    global _session_log, _session_log_path

    with _PRINT_LOCK:
        if _session_log is not None:
            _session_log.close()
            _session_log = None
        _session_log_path = None


def pretty_serial(serial: str) -> str:
    try:
        return f"SN{int(serial)}"
    except (TypeError, ValueError):
        match = re.search(r"(\d+)$", str(serial))
        if match:
            return f"SN{int(match.group(1))}"
        return f"SN{serial}"


def serial_match_candidates(serial: str) -> list[str]:
    """Return serial spellings a glob / integer pattern may match."""
    text = str(serial)
    out = [text]
    try:
        n = int(text)
        out.extend((str(n), f"{n:04d}"))
    except (TypeError, ValueError):
        match = re.search(r"(\d+)$", text)
        if match:
            n = int(match.group(1))
            out.extend((match.group(1), str(n), f"{n:04d}"))
    seen: set[str] = set()
    uniq: list[str] = []
    for item in out:
        if item not in seen:
            seen.add(item)
            uniq.append(item)
    return uniq


def serial_matches_glob(serial: str, pattern: str) -> bool:
    """True if a discovered serial matches a command-line glob or integer."""
    if pattern in ("*", "all"):
        return True
    for candidate in serial_match_candidates(serial):
        if fnmatch.fnmatchcase(candidate, pattern):
            return True
        if serials_equal(candidate, pattern):
            return True
    return False


def serial_matches_any(serial: str, patterns: list[str]) -> bool:
    return any(serial_matches_glob(serial, pattern) for pattern in patterns)


def is_uart_token(token: str) -> bool:
    if token.startswith("/dev/"):
        return True
    try:
        return stat.S_ISCHR(Path(token).stat().st_mode)
    except OSError:
        return False


def is_accidental_shell_file(token: str) -> bool:
    """Cwd regular file, typically from an unquoted shell ``*``."""
    if "/" in token:
        return False
    try:
        return Path(token).is_file()
    except OSError:
        return False


def classify_target(token: str) -> tuple[str, str]:
    """Classify a monitor target.

    Returns:
        ``("glob", pattern)`` for a board serial glob (including integers,
        ``*`` and ``all``),
        ``("ip", dotted-quad)`` for an IPv4 address,
        ``("uart", token)`` for a serial device path,
        ``("ignore", token)`` for a cwd file from unquoted ``*``.
    """
    text = token.strip()
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        ip = None
    if isinstance(ip, ipaddress.IPv4Address):
        return ("ip", str(ip))
    if is_uart_token(text):
        return ("uart", text)
    if is_accidental_shell_file(text):
        return ("ignore", text)
    if text == "all":
        return ("glob", "*")
    return ("glob", text)


def uart_label(port: str) -> str:
    name = Path(port).name
    match = re.search(r"CRS_(\d+)", name, re.IGNORECASE)
    if match:
        return f"SN{int(match.group(1))} uart"
    return name


def _serial_target(port: str) -> str | None:
    try:
        return str(Path(port).resolve())
    except OSError:
        return None


def usb_sysfs_device(port: str) -> Path | None:
    """Return the USB device sysfs dir for a tty or by-id path, or None."""
    resolved = Path(port).resolve()
    sysdev = Path(f"/sys/class/tty/{resolved.name}/device")
    if not sysdev.exists():
        return None
    p = sysdev.resolve()
    for _ in range(8):
        if (p / "busnum").is_file() and (p / "devnum").is_file():
            return p
        if p.parent == p:
            break
        p = p.parent
    return None


def usb_reset_serial(port: str) -> bool:
    """Issue USBDEVFS_RESET on the FTDI (or other USB-UART) behind ``port``.

    The CRS console chip is host-powered, so a crate cycle does not re-enumerate
    it. Resetting the USB device is what actually recovers the UART after the
    Zynq side has been powered off.
    """
    usb = usb_sysfs_device(port)
    if usb is None:
        log(f"USB reset skipped: no sysfs device for {port}")
        return False
    try:
        bus = int((usb / "busnum").read_text())
        dev = int((usb / "devnum").read_text())
    except (OSError, ValueError) as exc:
        log(f"USB reset skipped: {exc}")
        return False
    path = f"/dev/bus/usb/{bus:03d}/{dev:03d}"
    try:
        fd = os.open(path, os.O_WRONLY)
        try:
            fcntl.ioctl(fd, USBDEVFS_RESET)
        finally:
            os.close(fd)
    except OSError as exc:
        log(f"USB reset {path} failed ({exc})")
        return False
    log(f"USB reset {path} ({usb.name})")
    return True


def wait_for_serial_path(port: str, timeout_s: float = 10.0) -> bool:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_s:
        if Path(port).exists():
            return True
        time.sleep(0.2)
    log(f"serial path still missing after {timeout_s:g}s: {port}")
    return False


class SerialPrinter:
    """Print UART bytes to stdout, reconnecting when the USB device drops."""

    def __init__(self, port: str, baud: int, label: str | None = None):
        self.port = port
        self.baud = baud
        self.label = label or uart_label(port)
        self._stop = threading.Event()
        self._hold = threading.Event()
        self._thread: threading.Thread | None = None
        self._line_buf = b""

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name=f"serial-{self.label}", daemon=True
        )
        self._thread.start()
        resolved = _serial_target(self.port)
        extra = f" -> {resolved}" if resolved and resolved != self.port else ""
        log(f"serial {self.port}{extra} @ {self.baud}")

    def stop(self) -> None:
        self._stop.set()
        self._hold.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def suspend(self) -> None:
        """Close the UART so a crate power cycle can re-enumerate the USB device."""
        log(f"serial {self.label} closing for power cycle")
        self._hold.set()

    def resume(self) -> None:
        """Re-open the UART after power is back."""
        log(f"serial {self.label} reopening")
        self._hold.clear()

    def _flush_line_buf(self, leftover: bool = False) -> None:
        if leftover and self._line_buf:
            text = self._line_buf.decode("utf-8", errors="replace").rstrip("\r")
            if text:
                emit(self.label, text)
            self._line_buf = b""
            return
        while b"\n" in self._line_buf:
            line, self._line_buf = self._line_buf.split(b"\n", 1)
            text = line.decode("utf-8", errors="replace").rstrip("\r")
            emit(self.label, text)

    def _open(self):
        import serial

        kwargs = dict(
            port=self.port,
            baudrate=self.baud,
            timeout=0.2,
            dsrdtr=False,
            rtscts=False,
        )
        try:
            ser = serial.Serial(exclusive=True, **kwargs)
        except TypeError:
            ser = serial.Serial(**kwargs)
        # Do not leave DTR/RTS asserted; some FTDI channels treat DTR as reset,
        # and a crate cycle with the USB chip still powered can leave them stuck.
        try:
            ser.dtr = False
            ser.rts = False
        except (OSError, AttributeError):
            pass
        try:
            ser.reset_input_buffer()
        except OSError:
            pass
        return ser

    def _run(self) -> None:
        import serial

        waiting_logged = False
        usb_reset_on_open = False
        watchdog_used = False
        while not self._stop.is_set():
            if self._hold.is_set():
                self._stop.wait(0.2)
                continue
            if usb_reset_on_open:
                usb_reset_serial(self.port)
                wait_for_serial_path(self.port)
                usb_reset_on_open = False
            try:
                ser = self._open()
            except serial.SerialException as exc:
                if not waiting_logged:
                    log(f"serial waiting for {self.port} ({exc})")
                    waiting_logged = True
                self._stop.wait(0.5)
                continue
            waiting_logged = False
            resolved = _serial_target(self.port)
            extra = f" -> {resolved}" if resolved and resolved != self.port else ""
            log(f"serial connected {self.port}{extra}")
            self._line_buf = b""
            got_bytes = False
            opened_at = time.monotonic()
            try:
                while not self._stop.is_set() and not self._hold.is_set():
                    data = ser.read(4096)
                    if data:
                        got_bytes = True
                        watchdog_used = False
                        self._line_buf += data
                        self._flush_line_buf()
                    elif (
                        not got_bytes
                        and not watchdog_used
                        and time.monotonic() - opened_at >= NO_UART_BYTES_S
                    ):
                        log(
                            f"no UART bytes for {NO_UART_BYTES_S:g}s; "
                            "USB-resetting the adapter and reopening"
                        )
                        watchdog_used = True
                        usb_reset_on_open = True
                        break
            except serial.SerialException as exc:
                log(f"serial {self.label} disconnected ({exc})")
            finally:
                self._flush_line_buf(leftover=True)
                try:
                    ser.close()
                except serial.SerialException:
                    pass
                if not self._stop.is_set() and not self._hold.is_set() and not usb_reset_on_open:
                    self._stop.wait(0.2)


class BoardIpMap:
    """Serial-number to IPv4 map, updated from UDP discovery beacons."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._serial_to_ip: dict[str, str] = {}

    def update(self, board_serial: str, ip: str) -> bool:
        with self._lock:
            for known, current in self._serial_to_ip.items():
                if serials_equal(known, board_serial):
                    if current == ip:
                        return False
                    self._serial_to_ip[known] = ip
                    return True
            self._serial_to_ip[board_serial] = ip
            return True

    def all_serials(self) -> list[str]:
        with self._lock:
            return list(self._serial_to_ip)

    def ip_for(self, wanted: str) -> str | None:
        with self._lock:
            for known, ip in self._serial_to_ip.items():
                if serials_equal(known, wanted):
                    return ip
        return None


def _exc_text(exc: BaseException) -> str:
    text = str(exc).strip()
    return text if text else type(exc).__name__


async def _close_writer(writer: asyncio.StreamWriter | None) -> None:
    if writer is None:
        return
    writer.close()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(writer.wait_closed(), timeout=1.0)


async def tail_tcp_host(
    host: str,
    port: int,
    tag: str,
    connect_timeout: float,
    retry_s: float,
    idle_s: float,
    stop: threading.Event,
) -> None:
    """Connect, print complete log lines, and retry until ``stop`` is set."""
    last_fail_log = 0.0
    while not stop.is_set():
        writer: asyncio.StreamWriter | None = None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=connect_timeout
            )
            sock = writer.get_extra_info("socket")
            if sock is not None:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            emit(tag, f"connected TCP log :{port}")
            last_fail_log = 0.0
            while not stop.is_set():
                try:
                    line = await asyncio.wait_for(reader.readline(), timeout=idle_s)
                except asyncio.TimeoutError:
                    emit(tag, f"no log line for {idle_s:g}s; reconnecting")
                    break
                if not line:
                    emit(tag, "TCP log closed; reconnecting")
                    break
                emit(tag, line.decode("utf-8", errors="replace").rstrip("\r\n"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            now = time.monotonic()
            if now - last_fail_log >= 5.0:
                emit(tag, f"TCP log unavailable ({_exc_text(exc)}); retrying")
                last_fail_log = now
        finally:
            await _close_writer(writer)
        if not stop.is_set():
            await asyncio.sleep(retry_s)


async def tail_resolved_serial(
    wanted: str,
    ip_map: BoardIpMap,
    port: int,
    connect_timeout: float,
    retry_s: float,
    idle_s: float,
    stop: threading.Event,
) -> None:
    """Wait for a beacon to map ``wanted`` to an IP, then tail TCP logs."""
    pretty = pretty_serial(wanted)
    logged_wait = False
    last_ip: str | None = None
    while not stop.is_set():
        ip = ip_map.ip_for(wanted)
        if ip is None:
            if not logged_wait:
                log(f"{pretty}: waiting for UDP beacon on :{DEFAULT_DISCOVERY_PORT}")
                logged_wait = True
            await asyncio.sleep(0.25)
            continue
        logged_wait = False
        if ip != last_ip:
            log(f"{pretty} -> {ip}")
            last_ip = ip
        await tail_tcp_host(
            ip, port, f"{pretty} {ip}", connect_timeout, retry_s, idle_s, stop
        )


def _already_started(started: list[str], serial: str) -> bool:
    return any(serials_equal(serial, known) for known in started)


async def tail_matching_discovered(
    ip_map: BoardIpMap,
    patterns: list[str],
    port: int,
    connect_timeout: float,
    retry_s: float,
    idle_s: float,
    stop: threading.Event,
) -> None:
    """Start a TCP log tail for each discovered serial that matches ``patterns``."""
    started: list[str] = []
    skipped: list[str] = []
    tasks: list[asyncio.Task] = []
    log(f"TCP log serial globs: {', '.join(patterns)}")
    try:
        while not stop.is_set():
            for serial in ip_map.all_serials():
                if _already_started(started, serial) or _already_started(skipped, serial):
                    continue
                pretty = pretty_serial(serial)
                if not serial_matches_any(serial, patterns):
                    skipped.append(serial)
                    log(f"{pretty} does not match TCP globs {patterns}")
                    continue
                started.append(serial)
                log(f"discovered {pretty}; starting TCP log")
                tasks.append(
                    asyncio.create_task(
                        tail_resolved_serial(
                            serial,
                            ip_map,
                            port,
                            connect_timeout,
                            retry_s,
                            idle_s,
                            stop,
                        )
                    )
                )
            await asyncio.sleep(0.25)
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def _bind_beacon_socket() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind(("0.0.0.0", DEFAULT_DISCOVERY_PORT))
    sock.setblocking(False)
    return sock


async def beacon_resolver_loop(ip_map: BoardIpMap, stop: threading.Event) -> None:
    """Listen for FWREQ/TCPIPE beacons and refresh serial→IP mappings."""
    last_request = 0.0
    bind_fail_logged = False
    while not stop.is_set():
        sock: socket.socket | None = None
        try:
            sock = _bind_beacon_socket()
            bind_fail_logged = False
            log(f"listening for board beacons on UDP :{DEFAULT_DISCOVERY_PORT}")
            while not stop.is_set():
                now = time.monotonic()
                if now - last_request >= BEACON_REQUEST_S:
                    try:
                        request_beacon()
                    except OSError as exc:
                        log(f"BEACONREQ failed ({exc})")
                    last_request = now
                await asyncio.sleep(0.05)
                try:
                    data, addr = sock.recvfrom(4096)
                except BlockingIOError:
                    continue
                except OSError as exc:
                    log(f"beacon socket error ({exc})")
                    break
                try:
                    beacon = CRSBeacon(data, address=addr[0])
                except (UnicodeDecodeError, ValueError):
                    continue
                ip = beacon.address or addr[0]
                if ip_map.update(beacon.board_serial, ip):
                    pretty = pretty_serial(str(beacon.board_serial))
                    log(
                        f"beacon {beacon.kind} {pretty} at {ip} "
                        f"({beacon.board_part})"
                    )
        except OSError as exc:
            if not bind_fail_logged:
                log(
                    f"cannot bind UDP :{DEFAULT_DISCOVERY_PORT} ({exc}); "
                    "retrying"
                )
                bind_fail_logged = True
            await asyncio.sleep(2.0)
        finally:
            if sock is not None:
                sock.close()


class TcpLogMonitor:
    """Background asyncio loop: beacon resolve + TCP log tails."""

    def __init__(
        self,
        ips: list[str],
        serial_globs: list[str],
        port: int,
        connect_timeout: float,
        retry_s: float,
        idle_s: float,
    ):
        self.ips = ips
        self.serial_globs = serial_globs
        self.port = port
        self.connect_timeout = connect_timeout
        self.retry_s = retry_s
        self.idle_s = idle_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="tcp-log-monitor", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._main())
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            loop.close()
            self._loop = None

    async def _main(self) -> None:
        ip_map = BoardIpMap()
        tasks = [
            asyncio.create_task(beacon_resolver_loop(ip_map, self._stop)),
        ]
        if self.serial_globs:
            tasks.append(
                asyncio.create_task(
                    tail_matching_discovered(
                        ip_map,
                        self.serial_globs,
                        self.port,
                        self.connect_timeout,
                        self.retry_s,
                        self.idle_s,
                        self._stop,
                    )
                )
            )
        for ip in self.ips:
            tasks.append(
                asyncio.create_task(
                    tail_tcp_host(
                        ip,
                        self.port,
                        ip,
                        self.connect_timeout,
                        self.retry_s,
                        self.idle_s,
                        self._stop,
                    )
                )
            )
        stop_waiter = asyncio.create_task(self._wait_stop())
        tasks.append(stop_waiter)
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            with contextlib.suppress(asyncio.CancelledError):
                exc = task.exception() if not task.cancelled() else None
                if exc is not None:
                    log(f"TCP monitor task failed ({_exc_text(exc)})")

    async def _wait_stop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(0.2)


def file_mtime_ns(path: Path) -> int | None:
    """Return the ELF modification time in ns, or None if the path is missing."""
    try:
        return path.stat().st_mtime_ns
    except FileNotFoundError:
        return None


def wait_until_stable(path: Path, settle_s: float, poll_s: float) -> int:
    """Wait until mtime stops changing for settle_s seconds."""
    last = file_mtime_ns(path)
    if last is None:
        raise FileNotFoundError(path)
    stable_since = time.monotonic()
    while True:
        time.sleep(poll_s)
        now = file_mtime_ns(path)
        if now is None:
            log(f"source disappeared while settling: {path}")
            last = None
            continue
        if last is None or now != last:
            last = now
            stable_since = time.monotonic()
            log("source mtime still changing")
            continue
        if time.monotonic() - stable_since >= settle_s:
            return now


def copy_elf(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    shutil.copy2(src, tmp)
    tmp.replace(dest)
    log(f"copied {src} -> {dest} ({dest.stat().st_size} bytes)")


def power_cycle(
    pdu: str,
    port: int,
    off_delay_s: float,
    printers: list[SerialPrinter] | None = None,
    serial_reopen_delay_s: float = DEFAULT_SERIAL_REOPEN_DELAY_S,
) -> None:
    from labpy.instruments.synaccess import NetCommander

    printers = printers or []
    log(f"power-cycling crate: {pdu} port {port} (off {off_delay_s:g}s)")
    for printer in printers:
        printer.suspend()
    if printers:
        time.sleep(0.3)
    restart_session_log("power cycle")
    nc = NetCommander(pdu)
    nc.set_power(port, 0)
    log(f"PDU port {port} OFF")
    time.sleep(off_delay_s)
    nc.set_power(port, 1)
    log(f"PDU port {port} ON")
    if printers:
        time.sleep(serial_reopen_delay_s)
        for printer in printers:
            usb_reset_serial(printer.port)
            wait_for_serial_path(printer.port)
            printer.resume()


def deploy(
    src: Path,
    dest: Path,
    pdu: str,
    port: int,
    off_delay_s: float,
    cycle: bool,
    printers: list[SerialPrinter] | None = None,
) -> None:
    copy_elf(src, dest)
    if cycle:
        power_cycle(pdu, port, off_delay_s, printers=printers)
    else:
        log("skipping power cycle (--no-cycle)")


def watch_loop(
    src: Path,
    dest: Path,
    pdu: str,
    port: int,
    off_delay_s: float,
    settle_s: float,
    poll_s: float,
    cycle: bool,
    printers: list[SerialPrinter] | None = None,
) -> None:
    last = file_mtime_ns(src)
    if last is None:
        log(f"waiting for first ELF: {src}")
    else:
        log(f"watching {src}; will copy when mtime changes")
    while True:
        time.sleep(poll_s)
        now = file_mtime_ns(src)
        if now is None:
            if last is not None:
                log("source removed; waiting for a new ELF")
            last = None
            continue
        if now == last:
            continue
        log(f"ELF mtime changed; waiting {settle_s:g}s for a stable file")
        try:
            last = wait_until_stable(src, settle_s=settle_s, poll_s=poll_s)
        except FileNotFoundError:
            last = None
            continue
        deploy(src, dest, pdu, port, off_delay_s, cycle, printers=printers)


def wait_until_interrupt() -> None:
    log("monitoring; Ctrl-C to exit")
    while True:
        time.sleep(1)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Copy a new tcpipe_rtos.elf into arm_firmware, power-cycle the crate, "
            "and print board UART and/or TCP logs."
        )
    )
    p.add_argument(
        "targets",
        nargs="*",
        metavar="TARGET",
        help=(
            "UART device (/dev/...), IPv4 address, or serial glob "
            "(55, '5*', '*', all). UDP beacons are always listened for; "
            "a TCP log is started when a discovered serial matches a glob. "
            "Quote '*' so the shell does not expand it"
        ),
    )
    p.add_argument(
        "--src",
        type=Path,
        default=DEFAULT_SRC,
        help=f"built ELF to watch (default: {DEFAULT_SRC})",
    )
    p.add_argument(
        "--dest",
        type=Path,
        default=DEFAULT_DEST_DIR / "tcpipe_rtos.elf",
        help="destination ELF served to boards (default: CRS arm_firmware/tcpipe_rtos.elf)",
    )
    p.add_argument(
        "--pdu",
        default=DEFAULT_PDU,
        help=f"NetCommander resource string (default: {DEFAULT_PDU})",
    )
    p.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PDU_PORT,
        help=f"PDU outlet that feeds the crate (default: {DEFAULT_PDU_PORT})",
    )
    p.add_argument(
        "--serial",
        action="append",
        dest="serial_devices",
        default=None,
        metavar="DEVICE",
        help=(
            "extra UART to print (repeatable). With no positional targets this "
            f"replaces the default ({DEFAULT_SERIAL})"
        ),
    )
    p.add_argument(
        "--baud",
        type=int,
        default=DEFAULT_BAUD,
        help=f"UART baud rate (default: {DEFAULT_BAUD})",
    )
    p.add_argument(
        "--no-serial",
        action="store_true",
        help="do not open or print any UART",
    )
    p.add_argument(
        "--tcp-port",
        type=int,
        default=DEFAULT_TCP_PORT,
        help=f"TCP log port (default: {DEFAULT_TCP_PORT})",
    )
    p.add_argument(
        "--connect-timeout",
        type=float,
        default=DEFAULT_TCP_CONNECT_S,
        metavar="SEC",
        help=f"TCP log connect timeout (default: {DEFAULT_TCP_CONNECT_S:g})",
    )
    p.add_argument(
        "--retry",
        type=float,
        default=DEFAULT_TCP_RETRY_S,
        metavar="SEC",
        help=f"delay before reconnecting a TCP log (default: {DEFAULT_TCP_RETRY_S:g})",
    )
    p.add_argument(
        "--idle",
        type=float,
        default=DEFAULT_TCP_IDLE_S,
        metavar="SEC",
        help=(
            "reconnect a TCP log if a live session produces no line for this long "
            f"(default: {DEFAULT_TCP_IDLE_S:g})"
        ),
    )
    p.add_argument(
        "--log",
        type=Path,
        default=DEFAULT_LOG,
        metavar="PATH",
        help=(
            "write console log lines to PATH (default: all.log). "
            "The file is overwritten at each start and truncated on each "
            "crate PDU power cycle"
        ),
    )
    p.add_argument(
        "--off-delay",
        type=float,
        default=DEFAULT_OFF_DELAY_S,
        metavar="SEC",
        help=f"seconds to leave the crate off (default: {DEFAULT_OFF_DELAY_S:g})",
    )
    p.add_argument(
        "--settle",
        type=float,
        default=DEFAULT_SETTLE_S,
        metavar="SEC",
        help=f"require the ELF mtime to stay unchanged this long before copying "
        f"(default: {DEFAULT_SETTLE_S:g})",
    )
    p.add_argument(
        "--poll",
        type=float,
        default=DEFAULT_POLL_S,
        metavar="SEC",
        help=f"watch poll interval (default: {DEFAULT_POLL_S:g})",
    )
    p.add_argument(
        "--once",
        action="store_true",
        help="copy the current ELF (waiting for it if needed) and power-cycle once",
    )
    p.add_argument(
        "--no-cycle",
        action="store_true",
        help="copy only; do not power-cycle the crate",
    )
    p.add_argument(
        "--cycle-only",
        action="store_true",
        help="power-cycle the crate without copying an ELF",
    )
    return p.parse_args(argv)


def collect_monitors(
    args: argparse.Namespace,
) -> tuple[list[str], list[str], list[str]]:
    """Return (uart_paths, tcp_ips, serial_globs) from argparse results."""
    uarts: list[str] = []
    ips: list[str] = []
    globs: list[str] = []
    ignored: list[str] = []
    for token in args.targets:
        kind, value = classify_target(token)
        if kind == "uart":
            uarts.append(value)
        elif kind == "ip":
            ips.append(value)
        elif kind == "ignore":
            ignored.append(value)
        else:
            globs.append(value)
    if ignored:
        log(
            "ignoring shell-expanded path(s) "
            + ", ".join(ignored)
            + "; quote '*' or pass 'all' for every board"
        )
    if args.serial_devices:
        uarts.extend(args.serial_devices)
    elif not args.targets and not args.no_serial:
        uarts.append(DEFAULT_SERIAL)
    if args.no_serial:
        uarts = []
    if not globs and not ips:
        globs = ["*"]
    return (
        list(dict.fromkeys(uarts)),
        list(dict.fromkeys(ips)),
        list(dict.fromkeys(globs)),
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    src = args.src.expanduser()
    dest = args.dest.expanduser()
    cycle = not args.no_cycle
    printers: list[SerialPrinter] = []
    tcp_monitor: TcpLogMonitor | None = None
    try:
        open_session_log(args.log)
        uart_paths, tcp_ips, serial_globs = collect_monitors(args)
        printers = [SerialPrinter(path, args.baud) for path in uart_paths]
        tcp_monitor = TcpLogMonitor(
            ips=tcp_ips,
            serial_globs=serial_globs,
            port=args.tcp_port,
            connect_timeout=args.connect_timeout,
            retry_s=args.retry,
            idle_s=args.idle,
        )
        for printer in printers:
            printer.start()
        tcp_monitor.start()

        if args.cycle_only:
            power_cycle(args.pdu, args.port, args.off_delay, printers=printers)
            wait_until_interrupt()
            return 0

        if args.once:
            if file_mtime_ns(src) is None:
                log(f"waiting for {src}")
                while file_mtime_ns(src) is None:
                    time.sleep(args.poll)
            log(f"waiting {args.settle:g}s for a stable ELF")
            wait_until_stable(src, settle_s=args.settle, poll_s=args.poll)
            deploy(src, dest, args.pdu, args.port, args.off_delay, cycle, printers=printers)
            wait_until_interrupt()
            return 0

        watch_loop(
            src=src,
            dest=dest,
            pdu=args.pdu,
            port=args.port,
            off_delay_s=args.off_delay,
            settle_s=args.settle,
            poll_s=args.poll,
            cycle=cycle,
            printers=printers,
        )
    except KeyboardInterrupt:
        log("stopped")
    finally:
        if tcp_monitor is not None:
            tcp_monitor.stop()
        for printer in printers:
            printer.stop()
        close_session_log()
    return 0


if __name__ == "__main__":
    sys.exit(main())
