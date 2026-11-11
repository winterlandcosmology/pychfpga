"""CRS network discovery through U-Boot and tcpipe beacons."""

import os
import asyncio
import hashlib
import logging
import re
import socket
import sys
import time
from pathlib import Path

from aiohttp import web

from pychfpga.hardware import Crate, Motherboard
from pychfpga.hardware.crs.netconsole_loads import Loads
from pychfpga.hardware.crs.netconsole_xmodem import xmodem1k_pipe_send, xmodem1k_send
from pychfpga.hardware.crs.crs_beacon import (
    BEACON_REQUEST_PAYLOAD,
    CRSBeacon,
    DEFAULT_BIND,
    DEFAULT_DISCOVERY_PORT,
    _normalize_header_key,
    _to_int,
    _usable_field,
    parse_fwreq_datagram,
    request_beacon,
)


logger = logging.getLogger(__name__)

_parse_fwreq = parse_fwreq_datagram

# Wget upload: ``wget ${loadaddr} <url>``, wait for the U-Boot prompt (progress
# and ``Bytes transferred`` arrive on netconsole with current lwIP netconsole),
# then ``md5sum $loadaddr $filesize``, then ``bootelf -p $loadaddr``.
# Fixed HTTP port so host firewalls can allow inbound TCP once (ephemeral ports
# are often blocked while UDP netconsole still works).
DEFAULT_FIRMWARE_HTTP_PORT = 19875
# How often to poll the UDP socket while waiting for a prompt. Keep short: the
# ``ZynqMP>`` prompt often arrives without a trailing newline.
PROMPT_POLL_INTERVAL = 0.05
BOOTELF_IDLE = 1.0
# After bootelf, wait for the board's TCPIPE UDP beacon before opening TCP.
TCPIPE_BEACON_WAIT_TIMEOUT = 60.0
TCPIPE_BEACON_REQUEST_INTERVAL = 2.0

UPLOAD_METHODS = frozenset({"xmodem", "xmodem_pipe", "loads", "wget"})
DEFAULT_UPLOAD_METHOD = "wget"
FWREQ_COLLECT_TIMEOUT = 60.0
XMODEM_PIPE_DEPTH = 2
# 128 (SOH) for pipe smoke tests against ~2 KiB netconsole buffers; 1024 (STX) for 1K.
XMODEM_BLOCK_SIZE = 128

def _file_md5(path):
    """Return the MD5 hex digest of a file."""
    with open(path, "rb") as f:
        return hashlib.file_digest(f, "md5").hexdigest()


_TCPIPE_GIT_MARKER = b"TCPIPE_GIT="


def tcpipe_version_from_elf(pathname):
    """Return the GIT_VERSION compiled into a TCPipe ELF, or None.

    The firmware stores ``TCPIPE_GIT=<git describe>`` as a NUL-terminated
    rodata string. Discovery beacons advertise the same value as
    ``Bootloader Version``, so the uploader can skip boards that already
    match the ELF being loaded.
    """
    if not pathname:
        return None
    path = Path(pathname)
    try:
        data = path.read_bytes()
    except OSError:
        return None
    start = data.find(_TCPIPE_GIT_MARKER)
    if start < 0:
        return None
    start += len(_TCPIPE_GIT_MARKER)
    end = data.find(b"\0", start)
    if end < 0:
        return None
    version = data[start:end].decode("ascii", "replace").strip()
    return version or None


async def _async_sock_recvfrom(sock, bufsize, timeout):
    """Recvfrom on a non-blocking UDP socket (Python 3.10+ compatible)."""
    loop = asyncio.get_running_loop()
    future = loop.create_future()

    def _reader():
        loop.remove_reader(sock.fileno())
        try:
            future.set_result(sock.recvfrom(bufsize))
        except Exception as exc:
            future.set_exception(exc)

    loop.add_reader(sock.fileno(), _reader)
    try:
        return await asyncio.wait_for(future, timeout)
    except asyncio.TimeoutError:
        loop.remove_reader(sock.fileno())
        raise


async def _async_sock_recv(sock, bufsize, timeout):
    """Recv on a connected non-blocking UDP socket."""
    loop = asyncio.get_running_loop()
    future = loop.create_future()

    def _reader():
        loop.remove_reader(sock.fileno())
        try:
            future.set_result(sock.recv(bufsize))
        except Exception as exc:
            future.set_exception(exc)

    loop.add_reader(sock.fileno(), _reader)
    try:
        return await asyncio.wait_for(future, timeout)
    except asyncio.TimeoutError:
        loop.remove_reader(sock.fileno())
        raise


_NETCONSOLE_BINARY_UPLOAD = {
    "xmodem": {
        "cmd": b"loadx\n",
        "send": "xmodem_send",
        "label": "XMODEM",
    },
    "xmodem_pipe": {
        "cmd": b"loadx\n",
        "send": "xmodem_pipe_send",
        "label": "XMODEM pipe",
    },
    "loads": {
        "cmd": None,  # sent inside loads_send
        "send": "loads_send",
        "label": "loads",
    },
}


class NetConsole:
    """UDP netconsole session for U-Boot command and XMODEM firmware upload."""

    BUF_SIZE = 1024
    BOOTELF_CMD = "setenv autostart yes; bootelf -p $loadaddr"
    _BOOTELF_ERRORS = ("Unknown command", "Invalid ELF", "## Error", "ERROR:")

    def __init__(self, board_ip, fwreq_port, protocol_version=1, bind=DEFAULT_BIND, log=None):
        """``fwreq_port`` is the board's ephemeral UDP source port from FWREQ, not 9875."""
        self.board_ip = board_ip
        self.fwreq_port = fwreq_port
        self.protocol_version = protocol_version
        self.bind = bind
        self.log = log or logger
        self._sock = None
        self.ncip = None
        self.port = None
        self._rx_buf = ""

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def __repr__(self):
        return f"{self.__class__.__name__}({self.board_ip}:{self.fwreq_port})"

    @staticmethod
    def build_fwresp(version, ncip, ncinport, ncoutport):
        return (
            f"FWRESP/{version}\n"
            f"ncip:{ncip}\n"
            f"ncinport:{ncinport}\n"
            f"ncoutport:{ncoutport}\n\n"
        ).encode("utf-8")

    def open(self):
        if self._sock is not None:
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 256 * 1024)
        sock.bind((self.bind, 0))
        self.port = sock.getsockname()[1]
        sock.connect((self.board_ip, self.port))
        self.ncip = sock.getsockname()[0]
        sock.setblocking(False)
        self._sock = sock
        self.log.debug(
            "%r: bound local port %d, ncip=%s, board=%s fwreq_port=%d",
            self, self.port, self.ncip, self.board_ip, self.fwreq_port,
        )

    def close(self):
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def send_fwresp(self):
        payload = self.build_fwresp(
            self.protocol_version, self.ncip, self.port, self.port)
        self._sock.sendto(payload, (self.board_ip, self.fwreq_port))
        self.log.debug("%r: sent FWRESP/%d to %s:%d", self, self.protocol_version,
                       self.board_ip, self.fwreq_port)

    @staticmethod
    def _md5_timeout_for_size(nbytes):
        return max(60.0, nbytes / 8192.0 + 15.0)

    @staticmethod
    def _wget_timeout_for_size(nbytes):
        # Upper bound only; success is the spontaneous U-Boot prompt after wget.
        return max(60.0, nbytes / 51200.0 + 15.0)

    def _log_console(self, buf):
        text = buf.strip()
        if text:
            self.log.debug("%r: console:\n%s", self, text)

    async def _recv(self, timeout):
        """Receive one UDP datagram, or ``None`` on timeout."""
        try:
            return await _async_sock_recv(self._sock, self.BUF_SIZE, timeout)
        except asyncio.TimeoutError:
            return None
        except OSError:
            return None

    async def read_line(self, timeout=1.0):
        """Accumulate console bytes until a newline, or return partial on timeout.

        Returns the line including the trailing newline when one arrives; on
        timeout returns whatever is buffered (possibly empty). Leftover bytes
        after the first newline stay in ``_rx_buf`` for the next call.
        """
        deadline = time.monotonic() + timeout
        while True:
            nl = self._rx_buf.find("\n")
            if nl >= 0:
                line, self._rx_buf = self._rx_buf[:nl + 1], self._rx_buf[nl + 1:]
                return line
            wait = max(0.0, deadline - time.monotonic())
            if wait <= 0:
                line, self._rx_buf = self._rx_buf, ""
                return line
            chunk = await self._recv(wait)
            if chunk:
                self._rx_buf += chunk.decode(errors="replace")

    async def drain(self, timeout=0.2, idle=0.05):
        """Discard console output until ``idle`` seconds pass with no data."""
        deadline = time.monotonic() + timeout
        self._rx_buf = ""
        while time.monotonic() < deadline:
            wait = min(idle, max(0.0, deadline - time.monotonic()))
            if wait <= 0:
                break
            line = await self.read_line(timeout=wait)
            if not line:
                return

    async def wait_for_prompt(self, timeout=5.0, send_enter=False, poll_interval=None):
        """Wait until a U-Boot shell prompt appears on netconsole.

        Detects ``ZynqMP>`` even when it arrives without a trailing newline.
        ``send_enter`` is optional (wake a quiet shell); wget uploads do not
        need it with current U-Boot lwIP netconsole.
        """
        if poll_interval is None:
            poll_interval = PROMPT_POLL_INTERVAL
        deadline = time.monotonic() + timeout
        next_enter_at = time.monotonic()
        buf = ""
        while time.monotonic() < deadline:
            if send_enter and time.monotonic() >= next_enter_at:
                try:
                    self._sock.send(b"\n")
                except OSError:
                    break
                next_enter_at = time.monotonic() + poll_interval
            wait = min(poll_interval, max(0.0, deadline - time.monotonic()))
            if wait <= 0:
                break
            chunk = await self._recv(wait)
            if chunk:
                self._rx_buf += chunk.decode(errors="replace")
            while True:
                nl = self._rx_buf.find("\n")
                if nl < 0:
                    break
                buf += self._rx_buf[:nl + 1]
                self._rx_buf = self._rx_buf[nl + 1:]
            if self._has_uboot_prompt(buf + self._rx_buf):
                buf = buf + self._rx_buf
                self._rx_buf = ""
                self._log_console(buf)
                return buf
        if self._rx_buf:
            buf += self._rx_buf
            self._rx_buf = ""
        self.log.debug(
            "%r: prompt wait timed out after %ss (send_enter=%s)",
            self, timeout, send_enter,
        )
        if buf:
            self._log_console(buf)
        return buf

    async def send_cmd(self, cmd, timeout=0.1):
        self.log.debug("%r: >>> %s", self, cmd)
        self._sock.send((cmd + "\n").encode("utf-8"))
        reply = ""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            wait = max(0.0, deadline - time.monotonic())
            if wait <= 0:
                break
            line = await self.read_line(timeout=wait)
            if not line:
                break
            reply += line
        output = reply.replace(cmd, "", 1).strip()
        if output:
            self.log.debug("%r: <<< %s", self, output)
        return output

    @staticmethod
    def _has_uboot_prompt(buf):
        tail = buf.rstrip()
        if not tail:
            return False
        last_line = tail.rsplit("\n", 1)[-1].rstrip()
        if not last_line.endswith(">"):
            return False
        # md5sum prints "... ==> <digest>" one UDP datagram at a time; "==>" is not a prompt.
        if last_line.endswith("==>"):
            return False
        after_gt = last_line[last_line.rfind(">") + 1:]
        return not after_gt.strip()

    @staticmethod
    def _parse_uboot_md5(output):
        match = re.search(r"==>\s*([0-9a-fA-F]{32})\b", output)
        return match.group(1).lower() if match else None

    async def _recv_md5_response(self, timeout):
        """Read netconsole until ``md5sum`` prints a digest and the shell prompt."""
        deadline = time.monotonic() + timeout
        buf = ""
        while time.monotonic() < deadline:
            wait = min(0.2, max(0.0, deadline - time.monotonic()))
            if wait <= 0:
                break
            line = await self.read_line(timeout=wait)
            if line:
                buf += line
            if self._parse_uboot_md5(buf) is not None and self._has_uboot_prompt(buf):
                self._log_console(buf)
                return buf
        self.log.error(
            "%r: timed out after %ss waiting for md5sum", self, timeout,
        )
        if buf:
            self._log_console(buf)
        return buf

    async def _recv_until_prompt(self, timeout=600.0, idle=5.0):
        """Collect console output until the U-Boot prompt returns."""
        deadline = time.monotonic() + timeout
        buf = ""
        last_data = time.monotonic()
        while time.monotonic() < deadline:
            wait = min(1.0, max(0.0, deadline - time.monotonic()))
            if wait <= 0:
                break
            line = await self.read_line(timeout=wait)
            if line:
                buf += line
                last_data = time.monotonic()
                if self._has_uboot_prompt(buf):
                    self._log_console(buf)
                    return buf
            elif buf and (time.monotonic() - last_data) >= idle:
                if self._has_uboot_prompt(buf):
                    self._log_console(buf)
                    return buf
        self.log.error("%r: timed out after %ss waiting for U-Boot prompt", self, timeout)
        if buf:
            self._log_console(buf)
        return buf

    async def _recv_after_bootelf(self, timeout=15.0, idle=None):
        """Collect post-bootelf console output.

        Successful ``bootelf`` hands off to the loaded image; U-Boot does not
        return a shell prompt, so a short idle period with no error text is OK.
        """
        if idle is None:
            idle = BOOTELF_IDLE
        deadline = time.monotonic() + timeout
        buf = ""
        last_data = time.monotonic()
        while time.monotonic() < deadline:
            wait = min(1.0, max(0.0, deadline - time.monotonic()))
            if wait <= 0:
                break
            line = await self.read_line(timeout=wait)
            if line:
                buf += line
                last_data = time.monotonic()
                if any(err in buf for err in self._BOOTELF_ERRORS):
                    self._log_console(buf)
                    return buf
                if self._has_uboot_prompt(buf):
                    self._log_console(buf)
                    return buf
            elif (time.monotonic() - last_data) >= idle:
                if buf:
                    self._log_console(buf)
                return buf
        if buf:
            self._log_console(buf)
        return buf

    async def send_cmd_until_prompt(self, cmd, timeout=600.0, idle=5.0):
        """Send a command and wait until the shell prompt returns."""
        self.log.debug("%r: >>> %s", self, cmd)
        self._sock.send((cmd + "\n").encode("utf-8"))
        output = await self._recv_until_prompt(timeout=timeout, idle=idle)
        if cmd in output:
            output = output.replace(cmd, "", 1)
        return output.strip()

    async def xmodem_send(self, filename, block_size=XMODEM_BLOCK_SIZE):
        self.log.debug(
            "%r: starting XMODEM send of %s (block_size=%d)",
            self, filename, block_size)
        # Binary protocols need a blocking socket; restore after transfer.
        self._sock.setblocking(True)
        try:
            ok = xmodem1k_send(
                self._sock, filename, log=self.log, block_size=block_size)
        finally:
            self._sock.setblocking(False)
        self.log.debug("%r: XMODEM send %s", self, "ok" if ok else "failed")
        return ok

    async def xmodem_pipe_send(
            self, filename, pipe_depth=XMODEM_PIPE_DEPTH,
            block_size=XMODEM_BLOCK_SIZE):
        self.log.debug(
            "%r: starting XMODEM pipe send of %s (pipe_depth=%d, block_size=%d)",
            self, filename, pipe_depth, block_size)
        self._sock.setblocking(True)
        try:
            ok = xmodem1k_pipe_send(
                self._sock, filename, pipe_depth=pipe_depth, log=self.log,
                block_size=block_size)
        finally:
            self._sock.setblocking(False)
        self.log.debug("%r: XMODEM pipe send %s", self, "ok" if ok else "failed")
        return ok

    async def loads_send(self, filename, line_batch=1):
        self.log.debug("%r: starting loads send of %s", self, filename)
        self._sock.setblocking(True)
        try:
            ok = Loads(self._sock, log=self.log).send(filename, line_batch=line_batch)
        finally:
            self._sock.setblocking(False)
        self.log.debug("%r: loads send %s", self, "ok" if ok else "failed")
        return ok

    async def verify_loaded_md5(self, expected_md5, timeout=None, file_size=None):
        """Check U-Boot ``md5sum $loadaddr $filesize`` matches the host digest."""
        nbytes = file_size or 0
        if timeout is None:
            timeout = self._md5_timeout_for_size(nbytes)
        cmd = "md5sum $loadaddr $filesize"
        self.log.debug(
            "%r: >>> %s (%d bytes, timeout %.0fs)",
            self, cmd, nbytes, timeout,
        )
        self._sock.send((cmd + "\n").encode("utf-8"))
        output = await self._recv_md5_response(timeout)
        return self._check_md5_output(output, expected_md5)

    def _check_md5_output(self, output, expected_md5):
        digest = self._parse_uboot_md5(output)
        if digest != expected_md5.lower():
            self.log.error(
                "%r: MD5 mismatch (expected %s): %s",
                self, expected_md5, output or "(no reply)",
            )
            return False
        self.log.debug("%r: MD5 OK (%s)", self, expected_md5)
        return True

    async def upload_and_boot_elf(self, elf_path, verify_md5=False, upload_method="xmodem"):
        """Send ELF over netconsole and boot it with bootelf.

        ``upload_method`` may be ``xmodem``, ``xmodem_pipe``, or ``loads``.
        MD5 is always verified on the loaded image before bootelf.
        """
        if upload_method not in _NETCONSOLE_BINARY_UPLOAD:
            raise ValueError(
                f"upload_method must be one of {sorted(_NETCONSOLE_BINARY_UPLOAD)}")
        upload_cfg = _NETCONSOLE_BINARY_UPLOAD[upload_method]
        elf_path = Path(elf_path)
        if not elf_path.is_file():
            self.log.error("%r: platform firmware ELF not found: %s", self, elf_path)
            return False

        expected_size = elf_path.stat().st_size
        expected_md5 = _file_md5(elf_path)

        self.send_fwresp()
        await self.wait_for_prompt()
        t_upload = time.monotonic()
        if upload_cfg["cmd"] is not None:
            self._sock.send(upload_cfg["cmd"])
            self.log.debug("%r: sent %r", self, upload_cfg["cmd"])
        try:
            t0 = time.monotonic()
            send_fn = getattr(self, upload_cfg["send"])
            if not await send_fn(elf_path):
                self.log.error(
                    "%r: %s transfer failed for %s",
                    self, upload_cfg["label"], elf_path.name)
                return False
            transfer_s = time.monotonic() - t0
            self.log.debug(
                "%r: %s sent %d bytes in %.2fs (%.0f KB/s)",
                self, upload_cfg["label"], expected_size, transfer_s,
                expected_size / transfer_s / 1024,
            )
            if upload_method == "loads":
                await self.wait_for_prompt(timeout=5.0)
            else:
                await self.drain(timeout=0.2, idle=0.05)
        except OSError as exc:
            self.log.error("%r: failed to upload %s: %s", self, elf_path, exc)
            return False

        load_info = await self.send_cmd("printenv loadaddr filesize", timeout=2)
        self.log.debug("%r: loaded %s: %s", self, elf_path.name, load_info or "(no reply)")
        if "filesize=" in load_info:
            try:
                loaded_size = int(load_info.split("filesize=", 1)[1].strip(), 16)
                if loaded_size != expected_size:
                    self.log.warning(
                        "%r: U-Boot filesize 0x%x != host size 0x%x (%d bytes)",
                        self, loaded_size, expected_size, expected_size,
                    )
            except ValueError:
                pass

        if not await self.verify_loaded_md5(expected_md5, file_size=expected_size):
            return False
        self.log.debug(
            "%r: firmware upload took %.2fs", self, time.monotonic() - t_upload)

        t_boot = time.monotonic()
        output = await self.send_cmd(self.BOOTELF_CMD, timeout=10)
        if output:
            self.log.debug("%r: boot response: %s", self, output)
        ok = self._bootelf_ok(output)
        if ok:
            self.log.debug(
                "%r: bootelf handoff took %.2fs", self, time.monotonic() - t_boot)
        return ok

    async def upload_and_boot_via_wget(self, url, elf_path, verify_md5=False):
        """Send FWRESP/1, download ELF via wget over netconsole, verify MD5, and boot.

        Modern U-Boot no longer accepts shell commands in the FWRESP body, so wget is
        executed on the netconsole session after FWRESP/1 establishes the link.

        Waits for the spontaneous U-Boot prompt after wget (progress and
        ``Bytes transferred`` are visible on netconsole). ``md5sum`` then verifies
        the loaded image. After ``bootelf``, silence means U-Boot handed off; the
        uploader still waits for a TCPIPE beacon before opening TCP.
        """
        del verify_md5  # MD5 is always verified, same as other netconsole binary paths.
        elf_path = Path(elf_path)
        if not elf_path.is_file():
            self.log.error("%r: platform firmware ELF not found: %s", self, elf_path)
            return False
        expected_size = elf_path.stat().st_size
        expected_md5 = _file_md5(elf_path)
        wget_timeout = self._wget_timeout_for_size(expected_size)

        self.send_fwresp()
        await self.wait_for_prompt()

        t_upload = time.monotonic()
        wget_cmd = f"setenv autostart yes; wget ${{loadaddr}} {url}"
        self.log.debug("%r: >>> %s", self, wget_cmd)
        self._sock.send((wget_cmd + "\n").encode("utf-8"))

        prompt = await self.wait_for_prompt(timeout=wget_timeout)
        if not self._has_uboot_prompt(prompt):
            self.log.error(
                "%r: U-Boot prompt did not return after wget", self)
            return False
        if not await self.verify_loaded_md5(expected_md5, file_size=expected_size):
            return False
        self.log.debug(
            "%r: firmware upload took %.2fs", self, time.monotonic() - t_upload)

        t_boot = time.monotonic()
        self.log.debug("%r: >>> %s", self, self.BOOTELF_CMD)
        self._sock.send((self.BOOTELF_CMD + "\n").encode("utf-8"))
        output = await self._recv_after_bootelf()
        if output:
            self.log.debug("%r: boot response: %s", self, output)
        ok = self._bootelf_ok(output)
        if ok:
            self.log.debug(
                "%r: bootelf handoff took %.2fs", self, time.monotonic() - t_boot)
        return ok

    def _bootelf_ok(self, output):
        if any(err in output for err in self._BOOTELF_ERRORS):
            self.log.error("%r: bootelf failed: %s", self, output)
            return False
        return True

    async def upload_and_run(
            self,
            elf_path,
            upload_method=DEFAULT_UPLOAD_METHOD,
            wget_url=None,
            verify_md5=False):
        """Upload platform firmware over netconsole and boot it."""
        if upload_method in _NETCONSOLE_BINARY_UPLOAD:
            return await self.upload_and_boot_elf(
                elf_path, verify_md5=verify_md5, upload_method=upload_method)
        if upload_method == "wget":
            if wget_url is None:
                raise ValueError("wget_url is required when upload_method='wget'")
            return await self.upload_and_boot_via_wget(
                wget_url,
                elf_path=elf_path,
                verify_md5=verify_md5,
            )
        raise ValueError(f"upload_method must be one of {sorted(UPLOAD_METHODS)}")


def _format_discovery_serial(serial):
    """Format a board/crate/backplane serial for discovery log lines."""
    if serial is None:
        return "SN????"
    text = str(serial).strip()
    if text.upper().startswith("SN"):
        text = text[2:]
    try:
        return f"SN{int(text):04d}"
    except (TypeError, ValueError):
        return f"SN{text}"


def _format_discovered_board_line(beacon, mb=None, added_to_hwm=False):
    """Build a one-line summary of a discovered CRS board."""
    line = (
        f"{beacon.board_part} {_format_discovery_serial(beacon.board_serial)} "
        f"@ {beacon.address}"
    )

    if beacon.backplane_serial is not None or beacon.backplane_part is not None:
        bp_part = beacon.backplane_part or "CBP"
        bp_serial = _format_discovery_serial(beacon.backplane_serial)
        line += f" on Backplane {bp_part} {bp_serial}"

    crate_serial = beacon.crate_serial
    if mb is not None and mb.crate is not None and mb.crate.serial:
        crate_serial = crate_serial or mb.crate.serial
    crate_slot = None
    if mb is not None and mb.slot:
        crate_slot = mb.slot
    elif isinstance(beacon.slot, int) and beacon.slot > 0:
        crate_slot = beacon.slot

    if crate_serial is not None or crate_slot is not None:
        crate_part = beacon.crate_part or "CCR"
        crate_text = f"/ Crate {crate_part} {_format_discovery_serial(crate_serial)}"
        if crate_slot is not None:
            crate_text += f" Slot {crate_slot}"
        line += f" {crate_text}"

    version = f" {beacon.version}" if beacon.version else ""
    line += f" ({beacon.kind}{version} beacon)"
    if added_to_hwm:
        line += " -> added to hardware map"
    return line


async def discover_crs(
        boards=None,
        crates=None,
        timeout=2,
        **kwargs):
    """Discover CRS boards and crates from U-Boot/TCPIPE UDP beacons."""
    if kwargs:
        logger.debug(f"discover_crs: keyword arguments {kwargs} will be ignored")
    await CRSDiscovery(
        board_targets=boards or [],
        crate_targets=crates or [],
        timeout=timeout
    ).run()


async def set_platform_firmware(
        boards=None,
        required_version=None,
        firmware_pathname=None,
        upload_method=DEFAULT_UPLOAD_METHOD,
        verify_md5=False,
        **kwargs):
    """Load tcpipe firmware on U-Boot CRS boards in parallel.

    Boards must already have a hostname (from discovery) and are assumed to be
    in U-Boot.  The uploader listens on UDP port 9875 for each board's FWREQ
    broadcast and learns the ephemeral reply port before sending FWRESP.

    Parameters:
        boards (list): Boards to load the platform firmware on.
        required_version (str): Required version of the platform firmware.
        firmware_pathname (str): Path to the platform firmware ELF file.
        upload_method (str): ``'wget'``, ``'xmodem'``, ``'xmodem_pipe'``, or ``'loads'``.
        verify_md5 (bool): Ignored for netconsole binary uploads (MD5 is always checked).

        **kwargs: Additional keyword arguments (ignored).
    Returns:
        None
    """
    if not boards:
        return  # nothing to discover
    if not firmware_pathname:
        raise ValueError("firmware_pathname is required")
    if kwargs:
        logger.debug(f"set_platform_firmware: keyword arguments {kwargs} will be ignored")
    await CRSArmFirmwareUploader(
        boards=boards or [],
        required_version=required_version,
        firmware_pathname=firmware_pathname,
        upload_method=upload_method,
        verify_md5=verify_md5,
    ).run()


def serials_equal(a, b) -> bool:
    """Return True if two board/crate serial tokens refer to the same unit."""
    if a is None or b is None:
        return False
    if str(a).upper() == str(b).upper():
        return True
    try:
        return int(a) == int(b)
    except (TypeError, ValueError):
        return False


def _match_token(target, value, match_integers=False):
    """Check if a value matches a target.

    Parameters:
        target (str): Target token. Can be "*", in which case the value is always matched.
        value (str): Value token. Can be None, in which case the target is not matched.
        match_integers (bool): Whether to do integer matching in addition to case-insensitive string matching.
    Returns:
        bool: True if the target matches the value, False otherwise.
    """
    if target == "*":
        return True
    if value is None:
        return False
    if str(target).upper() == str(value).upper():
        return True
    if match_integers:
        return _to_int(target) == _to_int(value)
    return False


def _board_matches_discovery_beacon(board, beacon_ip, beacon_serial):
    """Return True if a UDP beacon belongs to ``board`` (serial and/or hostname/IP).

    ``board.hostname`` is expected to already be a dotted-quad when
    ``FPGAArray`` used ``ping=True`` (see ``gethostbyname`` after a successful
    ping). For ``hwm`` with a literal IP or matching serial, compare to
    ``beacon_ip`` directly.
    """
    if board.hostname and beacon_ip == str(board.hostname):
        return True
    serial = board.serial
    if serial is not None and str(serial) not in ("", "*"):
        return _match_token(str(serial), beacon_serial, match_integers=True)
    return False


def _board_for_beacon(pending, beacon_ip, beacon_serial):
    """Map a beacon source to a pending upload target, if any."""
    board = pending.get(beacon_ip)
    if board is not None and _board_matches_discovery_beacon(
            board, beacon_ip, beacon_serial):
        return board
    for board in pending.values():
        if _board_matches_discovery_beacon(board, beacon_ip, beacon_serial):
            return board
    return None


class CRSDiscovery:
    """Discover CRS boards and crates via U-Boot and tcpipe beacons, and load ARM firmware."""
    discovery_port = DEFAULT_DISCOVERY_PORT
    bind = DEFAULT_BIND

    def __init__(self, board_targets, crate_targets, timeout):

        self.logger = logging.getLogger(__name__)
        self.board_targets = _normalize_targets(board_targets)
        self.crate_targets = _normalize_targets(crate_targets)
        self.timeout = timeout
        self.discovered_serials = set()
        self.discovered_crates = []
        self.seen_serials = set()
        self._fwreq_addr = {}
        self._reported_boards = set()
        self._progress_line_open = False
        self._last_progress_dot = 0.0

    def __repr__(self):
        return f"{self.__class__.__name__}()"

    @staticmethod
    def _format_target(model, serial, default_model):
        if model == "*" and serial == "*":
            return f"any {default_model}"
        label = model if model != "*" else default_model
        if serial == "*":
            return f"any {label}"
        return f"{label} SN{serial}"

    def _search_target_lines(self):
        lines = []
        if not self.board_targets and not self.crate_targets:
            lines.append("any CRS board")
        else:
            for model, serial in self.board_targets:
                lines.append(self._format_target(model, serial, "CRS"))
            for model, serial in self.crate_targets:
                lines.append(self._format_target(model, serial, "crate"))
        return lines

    def _progress_dot(self):
        now = time.monotonic()
        if now - self._last_progress_dot < 1.0:
            return
        sys.stdout.write(".")
        sys.stdout.flush()
        self._progress_line_open = True
        self._last_progress_dot = now

    def _end_progress_line(self):
        if self._progress_line_open:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self._progress_line_open = False

    def _report_discovered_board(self, beacon, mb=None, added_to_hwm=False):
        serial = str(beacon.board_serial)
        if serial in self._reported_boards:
            return
        self._reported_boards.add(serial)
        self._end_progress_line()
        self.logger.info(
            _format_discovered_board_line(beacon, mb=mb, added_to_hwm=added_to_hwm))

    async def run(self):
        targets = self._search_target_lines()
        self.logger.info("Looking for: %s", ", ".join(targets))
        self._last_progress_dot = time.monotonic()
        request_beacon()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.bind((self.bind, self.discovery_port))
            sock.setblocking(False)

            deadline = self._initial_deadline()
            while True:
                if self._complete():
                    break
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    break
                wait_time = min(remaining, 0.5) if remaining is not None else 0.5
                try:
                    data, addr = await _async_sock_recvfrom(sock, 4096, wait_time)
                except asyncio.TimeoutError:
                    self._progress_dot()
                    continue
                try:
                    beacon = CRSBeacon(data, address=addr[0])
                except (UnicodeDecodeError, ValueError):
                    logger.debug(
                        f"Ignoring non-CRS discovery packet from {addr[0]}:{addr[1]}")
                    continue
                self._process_beacon(sock, beacon, addr)
        finally:
            self._end_progress_line()
            sock.close()
        self._raise_if_incomplete()

    def _process_beacon(self, sock, beacon, addr):
        """Process a CRS beacon.

        Returns the kind of the beacon that was processed, or None if the beacon was ignored.
        """
        self.logger.debug(
            f"{self!r}: {beacon.kind} beacon from {beacon.board_part} "
            f"SN{beacon.board_serial} at {beacon.address}")
        if not self._matches_requested_targets(beacon):
            self.logger.debug(
                f"{self!r}: Ignoring {beacon.kind} beacon from serial "
                f"{beacon.board_serial} (not in {self.board_targets} or {self.crate_targets})")
            return None

        self.seen_serials.add(beacon.board_serial)

        if beacon.kind in ("FWREQ", "TCPIPE"):
            if not self._matches_board_serial_target(beacon):
                return None
            tcpipe_version = beacon.version if beacon.kind == "TCPIPE" else None
            mb = self._update_hardware_map(beacon, tcpipe_version=tcpipe_version)
            self._report_discovered_board(beacon, mb=mb, added_to_hwm=True)
            self.discovered_serials.add(beacon.board_serial)
            self._clear_fwreq_ip(beacon.board_serial)
            self._record_discovered_crate(beacon)
            return beacon.kind
        return None

    def _fwreq_ip_for(self, board_serial):
        for serial, ip in self._fwreq_addr.items():
            if serials_equal(serial, board_serial):
                return ip
        return None

    def _clear_fwreq_ip(self, board_serial):
        for serial in list(self._fwreq_addr):
            if serials_equal(serial, board_serial):
                del self._fwreq_addr[serial]
                return

    def _matches_board_serial_target(self, beacon):
        if not self.board_targets:
            return True
        board_id = (beacon.board_part, beacon.board_serial)
        return any(_target_matches(target, board_id) for target in self.board_targets)

    def _initial_deadline(self):
        if self.timeout is None:
            return None
        if self.crate_targets and not self.board_targets:
            return time.monotonic() + self.timeout
        if self._has_explicit_targets():
            return None
        return time.monotonic() + self.timeout

    def _matches_requested_targets(self, beacon):
        if not self.board_targets and not self.crate_targets:
            return True
        board_id = (beacon.board_part, beacon.board_serial)
        crate_id = (beacon.crate_part, beacon.crate_serial)
        return (
            any(_target_matches(target, board_id) for target in self.board_targets)
            or any(_target_matches(target, crate_id) for target in self.crate_targets)
        )

    def _complete(self):
        if not self._has_explicit_targets():
            return False
        if self._missing_explicit_board_targets():
            return False
        # Crate-only HWM (e.g. ``CRC4 1``): keep listening until the discovery
        # timeout, not after the first matching beacon.
        if self.crate_targets and not self.board_targets:
            return False
        return not self._missing_explicit_crate_targets()

    def _has_explicit_targets(self):
        return any(target[0] != "*" and target[1] != "*" for target in self.board_targets + self.crate_targets)

    def _missing_explicit_board_targets(self):
        explicit_board_targets = [
            (model, serial) for model, serial in self.board_targets
            if model != "*" and serial != "*"]
        return [
            (model, serial) for model, serial in explicit_board_targets
            if not any(serials_equal(serial, discovered) for discovered in self.discovered_serials)]

    def _missing_explicit_crate_targets(self):
        explicit_crate_targets = [
            target for target in self.crate_targets
            if target[0] != "*" and target[1] != "*"
        ]
        return [
            target for target in explicit_crate_targets
            if not any(_target_matches(target, discovered) for discovered in self.discovered_crates)
        ]

    def _record_discovered_crate(self, beacon):
        if beacon.crate_part and beacon.crate_serial:
            crate_id = (beacon.crate_part, beacon.crate_serial)
            if crate_id not in self.discovered_crates:
                self.discovered_crates.append(crate_id)

    def _raise_if_incomplete(self):
        missing = self._missing_explicit_board_targets() + self._missing_explicit_crate_targets()
        if not missing:
            return
        raise RuntimeError(
            "CRS discovery is incomplete: "
            f"missing={missing}, fwreq_seen={sorted(self.seen_serials)}, "
            f"discovered={sorted(self.discovered_serials)}")

    def _update_hardware_map(self, beacon, tcpipe_version):
        """Update the hardware map from a matching TCPIPE beacon."""
        crate = self._update_crate(beacon)
        board_part = beacon.board_part
        board_cls = Motherboard.get_class_by_ipmi_part_number(board_part.upper())
        mb = Motherboard.get_unique_instance(
            new_class=board_cls,
            serial=beacon.board_serial,
            hostname=beacon.address)
        mb.tcpipe_version = tcpipe_version
        slot = beacon.slot
        if mb.slot is not None:
            if slot is not None and slot != mb.slot:
                self.logger.debug(
                    f"{self!r}: Keeping hardware-map slot {mb.slot} for SN{mb.serial}; "
                    f"beacon advertises slot {slot} "
                    f"(backplane slot {beacon.backplane_slot})")
            slot = None
        if crate is not None or slot is not None:
            mb.update_instance(slot=slot, crate=crate)
        self.logger.debug(
            f"{self!r}: Updated hardware map with {beacon.kind} beacon from "
            f"{beacon.board_part} SN{beacon.board_serial} @ {beacon.address} on crate "
            f"SN{beacon.crate_serial} backplane SN{beacon.backplane_serial} "
            f"(crate number {beacon.crate_number}, slot {beacon.slot}, "
            f"backplane slot {beacon.backplane_slot})")
        return mb

    def _update_crate(self, beacon):
        # Crate identity comes from crate-serial / crate-part. Backplane serial
        # is a separate 4-slot segment and must not be used as the crate serial.
        from pychfpga.hardware.crs.crs_bp import CRSBackplane

        crate_part = beacon.crate_part or beacon.backplane_part
        crate_serial = beacon.crate_serial
        crate_number = beacon.crate_number
        if isinstance(crate_number, int) and crate_number <= 0:
            crate_number = None
        if not crate_part and not crate_serial:
            return None
        if crate_part:
            crate_cls = Crate.get_class_by_ipmi_part_number(crate_part)
            if crate_cls is None or not crate_cls.part_number:
                self.logger.warning(
                    f"{self!r}: Ignoring crate with unsupported part number {crate_part}")
                return None
        else:
            crate_cls = CRSBackplane
        return Crate.get_unique_instance(
            new_class=crate_cls,
            serial=crate_serial,
            crate_number=crate_number)


class CRSArmFirmwareUploader:
    """Upload platform firmware to U-Boot boards (parallel async netconsole sessions)."""
    discovery_port = DEFAULT_DISCOVERY_PORT
    bind = DEFAULT_BIND

    def __init__(
            self,
            boards,
            required_version,
            firmware_pathname=None,
            upload_method=DEFAULT_UPLOAD_METHOD,
            verify_md5=False):
        if upload_method not in UPLOAD_METHODS:
            raise ValueError(f"upload_method must be one of {sorted(UPLOAD_METHODS)}")
        self.logger = logging.getLogger(__name__)
        self.required_tcpipe_version = required_version
        self.boards = list(boards)
        self.firmware_pathname = firmware_pathname
        self.upload_method = upload_method
        self.verify_md5 = verify_md5
        self._progress_line_open = False
        self._last_progress_dot = 0.0

    def __repr__(self):
        return f"{self.__class__.__name__}()"

    def _progress_dot(self):
        now = time.monotonic()
        if now - self._last_progress_dot < 1.0:
            return
        sys.stdout.write(".")
        sys.stdout.flush()
        self._progress_line_open = True
        self._last_progress_dot = now

    def _end_progress_line(self):
        if self._progress_line_open:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self._progress_line_open = False

    def _boards_to_upload(self):
        """Boards that need firmware (U-Boot, or TCPipe that is not the required version)."""
        return [
            board for board in self.boards
            if board.hostname
            and board.tcpipe_version != self.required_tcpipe_version
        ]

    def _tcpipe_version_mismatch(self, measured):
        """True when ``measured`` is present and is not the required ELF version."""
        required = self.required_tcpipe_version
        return required is not None and measured is not None and measured != required

    def _raise_tcpipe_version_mismatch(self, mismatches):
        """Raise when boards are running TCPipe that cannot be replaced in-place.

        ``mismatches`` is a sequence of ``(board, measured_version)``. Firmware
        upload only works from U-Boot, so the boards must be rebooted first.
        """
        details = "; ".join(
            f"SN{board.serial} @ {board.hostname}: measured {measured!r}, "
            f"expected {self.required_tcpipe_version!r}"
            for board, measured in mismatches
        )
        raise RuntimeError(
            "TCPipe version mismatch: "
            f"{details}. Reboot the boards so they return to U-Boot, then retry."
        )

    async def run(self):
        firmware = Path(self.firmware_pathname)
        if not firmware.is_file():
            raise FileNotFoundError(f"Platform firmware ELF not found: {firmware}")

        targets = self._boards_to_upload()
        if not targets:
            return

        # Discovery already recorded a TCPIPE beacon version. Those boards are
        # not in U-Boot, so waiting for FWREQ would hang until timeout.
        running_wrong = [
            (board, board.tcpipe_version)
            for board in targets
            if self._tcpipe_version_mismatch(board.tcpipe_version)
        ]
        if running_wrong:
            self._raise_tcpipe_version_mismatch(running_wrong)

        http_site = None
        fwreq_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self._bind_fwreq_socket(fwreq_sock)
            reply_ports = await self._collect_fwreq_ports(fwreq_sock, targets)
            upload_targets = [
                board for board in targets
                if board.tcpipe_version != self.required_tcpipe_version
            ]
            if not upload_targets:
                self.logger.debug(
                    "%r: all targets already running TCPipe %s",
                    self, self.required_tcpipe_version)
                return

            if self.upload_method == "wget":
                bind_host = (
                    self._iface_address_for(upload_targets[0].hostname)
                    if upload_targets else self.bind)
                http_site = HTTPFirmwareServer(firmware, bind=bind_host)
                await http_site.start()
                self.logger.debug(
                    "%r: serving %s on http://%s:%d/%s",
                    self, firmware.name, bind_host, http_site.port, firmware.name)
                if bind_host not in ("0.0.0.0", ""):
                    self.logger.debug(
                        "%r: if wget hangs, allow inbound TCP port %d to %s "
                        "from the board subnet",
                        self, http_site.port, bind_host)

            results = await asyncio.gather(
                *[
                    self._upload_board(
                        board, firmware, http_site, reply_ports.get(board.hostname))
                    for board in upload_targets
                ],
                return_exceptions=True,
            )
            booted = []
            for board, result in zip(upload_targets, results):
                if isinstance(result, Exception):
                    self.logger.error(
                        "%r: upload failed for %s @ %s: %s",
                        self, board.serial, board.hostname, result)
                elif not result:
                    self.logger.error(
                        "%r: upload failed for %s @ %s",
                        self, board.serial, board.hostname)
                else:
                    booted.append(board)

            if booted:
                # bootelf silence only means U-Boot handed off; TCP is not ready
                # until TCPipe is up and advertising. Wait before open_platform.
                ready = await self._wait_for_tcpipe_beacons(fwreq_sock, booted)
                for board in booted:
                    if board.hostname in ready:
                        board.tcpipe_version = self.required_tcpipe_version
                        self.logger.debug(
                            "%r: TCPipe ready on SN%s @ %s (boot %.2fs after handoff)",
                            self, board.serial, board.hostname,
                            ready[board.hostname],
                        )
                    else:
                        self.logger.error(
                            "%r: TCPipe beacon not seen for SN%s @ %s after bootelf",
                            self, board.serial, board.hostname)
        finally:
            fwreq_sock.close()
            if http_site is not None:
                await http_site.stop()

    @staticmethod
    def _bind_fwreq_socket(sock):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind((CRSArmFirmwareUploader.bind, CRSArmFirmwareUploader.discovery_port))
        sock.setblocking(False)

    async def _collect_fwreq_ports(self, sock, targets, timeout=FWREQ_COLLECT_TIMEOUT):
        """Wait for FWREQ broadcasts and map board IP -> ephemeral reply port.

        U-Boot binds a random port, broadcasts FWREQ to port 9875, and expects
        FWRESP unicast back to that ephemeral port (``addr[1]`` of the FWREQ).
        """
        pending = {board.hostname: board for board in targets if board.hostname}
        reply_ports = {}
        deadline = time.monotonic() + timeout
        self._last_progress_dot = time.monotonic()
        waiting_for = ", ".join(sorted(pending))
        self.logger.info(
            "%r: waiting up to %.0fs for FWREQ/TCPIPE from %s",
            self, timeout, waiting_for)
        for board in pending.values():
            request_beacon(board.hostname)
        while pending and time.monotonic() < deadline:
            wait = min(1.0, deadline - time.monotonic())
            if wait <= 0:
                break
            try:
                data, addr = await _async_sock_recvfrom(sock, 4096, wait)
            except asyncio.TimeoutError:
                self._progress_dot()
                continue
            board_ip, reply_port = addr[0], addr[1]
            try:
                beacon = CRSBeacon(data, address=board_ip)
            except (UnicodeDecodeError, ValueError):
                continue
            board = _board_for_beacon(pending, board_ip, beacon.board_serial)
            if board is None:
                continue
            pending_key = board.hostname
            if beacon.kind == "TCPIPE":
                if self._tcpipe_version_mismatch(beacon.version):
                    self._raise_tcpipe_version_mismatch([(board, beacon.version)])
                board.tcpipe_version = self.required_tcpipe_version
                pending.pop(pending_key, None)
                self._end_progress_line()
                self.logger.debug(
                    "%r: TCPipe %s already running on SN%s @ %s",
                    self, beacon.version, beacon.board_serial, board_ip)
                continue
            if beacon.kind != "FWREQ":
                continue
            reply_ports[board.hostname] = reply_port
            pending.pop(pending_key, None)
            self._end_progress_line()
            self.logger.debug(
                "%r: FWREQ from SN%s @ %s:%d",
                self, beacon.board_serial, board_ip, reply_port)

        self._end_progress_line()
        if pending:
            still_need = [
                b for b in pending.values()
                if b.tcpipe_version != self.required_tcpipe_version
            ]
            if still_need:
                self.logger.error(
                    "%r: timed out waiting for FWREQ from %s",
                    self, ", ".join(sorted(b.hostname for b in still_need)))
        return reply_ports

    async def _wait_for_tcpipe_beacons(
            self, sock, boards, timeout=TCPIPE_BEACON_WAIT_TIMEOUT):
        """Wait until each board emits a TCPIPE beacon after bootelf.

        Returns ``{hostname: seconds_since_wait_start}`` for boards seen.
        """
        pending = {board.hostname: board for board in boards}
        ready = {}
        t0 = time.monotonic()
        deadline = t0 + timeout
        next_request_at = t0
        self._last_progress_dot = t0
        self.logger.info(
            "%r: waiting up to %.0fs for TCPIPE beacons from %s",
            self, timeout, ", ".join(sorted(pending)),
        )
        while pending and time.monotonic() < deadline:
            now = time.monotonic()
            if now >= next_request_at:
                request_beacon()
                next_request_at = now + TCPIPE_BEACON_REQUEST_INTERVAL
            wait = min(1.0, deadline - time.monotonic())
            if wait <= 0:
                break
            try:
                data, addr = await _async_sock_recvfrom(sock, 4096, wait)
            except asyncio.TimeoutError:
                self._progress_dot()
                continue
            board_ip = addr[0]
            try:
                beacon = CRSBeacon(data, address=board_ip)
            except (UnicodeDecodeError, ValueError):
                continue
            if beacon.kind != "TCPIPE":
                continue
            board = _board_for_beacon(pending, board_ip, beacon.board_serial)
            if board is None:
                continue
            pending_key = board.hostname
            if self._tcpipe_version_mismatch(beacon.version):
                self._raise_tcpipe_version_mismatch([(board, beacon.version)])
            elapsed = time.monotonic() - t0
            ready[board.hostname] = elapsed
            pending.pop(pending_key, None)
            self._end_progress_line()
            self.logger.debug(
                "%r: TCPIPE %s beacon from SN%s @ %s after %.2fs",
                self, beacon.version, beacon.board_serial, board_ip, elapsed,
            )

        self._end_progress_line()
        if pending:
            self.logger.error(
                "%r: timed out waiting for TCPIPE beacon from %s",
                self, ", ".join(sorted(pending)))
        return ready

    async def _upload_board(self, board, firmware, http_site, fwreq_port):
        short_path = os.path.relpath(
            self.firmware_pathname,
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
        if fwreq_port is None:
            self.logger.error(
                "%r: no FWREQ reply port for SN%s @ %s",
                self, board.serial, board.hostname)
            return False

        self.logger.debug(
            "%r: uploading TCPipe firmware %s to SN%s @ %s:%d via %s",
            self, short_path, board.serial, board.hostname, fwreq_port,
            self.upload_method)

        wget_url = None
        if self.upload_method == "wget":
            host = self._iface_address_for(board.hostname)
            wget_url = f"http://{host}:{http_site.port}/{firmware.name}"

        nc = NetConsole(board.hostname, fwreq_port, log=self.logger)
        try:
            nc.open()
            ok = await nc.upload_and_run(
                firmware,
                upload_method=self.upload_method,
                wget_url=wget_url,
                verify_md5=self.verify_md5,
            )
        finally:
            nc.close()

        if ok:
            self.logger.info(
                "uploaded firmware to SN%s @ %s",
                board.serial, board.hostname)
        return ok

    def _iface_address_for(self, remote_host):
        """Return the local IP used to reach ``remote_host``."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect((remote_host, 1))
            return sock.getsockname()[0]
        finally:
            sock.close()


class HTTPFirmwareServer:
    """Async HTTP server for a single firmware ELF (aiohttp, no threads)."""

    def __init__(self, firmware_path, bind=DEFAULT_BIND, port=DEFAULT_FIRMWARE_HTTP_PORT):
        self._firmware_path = Path(firmware_path)
        self._bind = bind
        self._port = port
        self.port = None
        self._runner = None
        self._site = None
        self._log = logging.getLogger(__name__)

    async def start(self):
        firmware_path = self._firmware_path
        route = f"/{firmware_path.name}"
        firmware_data = firmware_path.read_bytes()

        async def handle(request):
            peer = request.transport.get_extra_info("peername")
            self._log.debug(
                "firmware HTTP GET %s from %s",
                request.path, peer[0] if peer else "?",
            )
            return web.Response(
                body=firmware_data,
                headers={"Content-Length": str(len(firmware_data))},
            )

        app = web.Application()
        app.router.add_get(route, handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        try:
            self._site = web.TCPSite(self._runner, self._bind, self._port)
            await self._site.start()
        except OSError:
            self._log.warning(
                "firmware HTTP port %d unavailable on %s, retrying on 0.0.0.0",
                self._port, self._bind,
            )
            try:
                self._site = web.TCPSite(self._runner, DEFAULT_BIND, self._port)
                await self._site.start()
            except OSError:
                self._log.warning(
                    "firmware HTTP port %d unavailable, using ephemeral port",
                    self._port,
                )
                self._site = web.TCPSite(self._runner, DEFAULT_BIND, 0)
                await self._site.start()
        self.port = self._site._server.sockets[0].getsockname()[1]

    async def stop(self):
        if self._site is not None:
            await self._site.stop()
            self._site = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None


def _normalize_targets(targets):
    """Normalize a list of targets to a list of tuples of (model, serial).

    Parameters:
        targets (list): List of targets to normalize.

    Returns:
        list: List of tuples of (model, serial), where model can be "*", and serial is forced into a string and can also be "*".
    """
    if targets == "*":
        return [("*", "*")]
    return [(model or "*", str(serial)) for model, serial in (targets or [])]


def _target_matches(target, value):
    """Check if a target matches a value.

    Parameters:
        target (tuple): Tuple of (model, serial).
        value (tuple): Tuple of (model, serial).
    Returns:
        bool: True if the target matches the value, False otherwise.
    """
    target_model, target_serial = target
    value_model, value_serial = value
    if value_serial is None:
        return False
    return (
        _match_token(target_model, value_model)
        and _match_token(target_serial, value_serial, match_integers=True)
    )
