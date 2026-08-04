"""U-Boot ``loads`` (S-record) transfers over a netconsole socket."""

import logging
import shutil
import socket
import subprocess
import time
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)


class Loads:
    """S-record sender for U-Boot ``loads`` over a netconsole socket."""

    RECORD_LEN = 250  # max data bytes per S3 line (count field fits in one byte)

    def __init__(self, sock, log=None):
        self.sock = sock
        self.log = log or logger

    @staticmethod
    def _srec_checksum(body_bytes):
        """Motorola S-record line checksum byte."""
        return (~sum(body_bytes)) & 0xFF

    @staticmethod
    def s3_record_line(addr, data):
        """Format one S3 record line (data is a uint8 numpy array, len <= RECORD_LEN)."""
        addr_bytes = np.array([
            (addr >> 24) & 0xFF,
            (addr >> 16) & 0xFF,
            (addr >> 8) & 0xFF,
            addr & 0xFF,
        ], dtype=np.uint8)
        count = len(data) + 5
        body = np.concatenate([
            np.array([count], dtype=np.uint8),
            addr_bytes,
            data,
        ])
        csum = Loads._srec_checksum(body.tobytes())
        hex_payload = data.tobytes().hex().upper()
        return f"S3{count:02X}{addr:08X}{hex_payload}{csum:02X}\n".encode("ascii")

    @classmethod
    def _s3_record_lines(cls, addr, data, record_len=None):
        """Yield S3 record lines covering a contiguous uint8 segment."""
        if record_len is None:
            record_len = cls.RECORD_LEN
        offset = 0
        size = int(data.size)
        while offset < size:
            chunk = data[offset:offset + record_len]
            yield cls.s3_record_line(addr + offset, chunk)
            offset += record_len

    @staticmethod
    def _flat_file_segments(path):
        """Load a file as one segment at address 0 (for ``loads $loadaddr`` offset)."""
        return [(0, np.fromfile(path, dtype=np.uint8))]

    @classmethod
    def _iter_srec_lines(cls, path):
        """Yield S-record lines for U-Boot ``loads $loadaddr`` without buffering the whole file."""
        segments = cls._flat_file_segments(path)
        record_count = 0
        for addr, data in segments:
            for line in cls._s3_record_lines(addr, data):
                record_count += 1
                yield line

        count_body = bytes([3, (record_count >> 8) & 0xFF, record_count & 0xFF])
        count_csum = cls._srec_checksum(count_body)
        yield f"S503{record_count:04X}{count_csum:02X}\n".encode("ascii")
        end_body = bytes([5, 0, 0, 0, 0])
        end_csum = cls._srec_checksum(end_body)
        yield f"S70500000000{end_csum:02X}\n".encode("ascii")

    @classmethod
    def _iter_srec_lines_objcopy(cls, path):
        """Yield S-records from objcopy stdout (relative addresses)."""
        objcopy = shutil.which("objcopy")
        if objcopy is None:
            raise ValueError("objcopy is not on PATH")
        proc = subprocess.Popen(
            [objcopy, "-O", "srec", "--srec-forceS3", f"--srec-len={cls.RECORD_LEN}",
             str(path), "-"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            assert proc.stdout is not None
            for raw_line in proc.stdout:
                line = raw_line.strip()
                if line:
                    yield line + b"\n"
        finally:
            proc.wait(timeout=30)
            if proc.returncode != 0:
                stderr = proc.stderr.read().decode() if proc.stderr else ""
                raise ValueError(f"objcopy failed: {stderr}")

    def _recv_until_text(self, markers, timeout=120.0):
        """Block until any marker substring appears in inbound console data."""
        deadline = time.monotonic() + timeout
        buf = b""
        self.sock.settimeout(0.2)
        while time.monotonic() < deadline:
            try:
                chunk = self.sock.recv(4096)
                if chunk:
                    buf += chunk
                    text = buf.decode(errors="replace")
                    if any(marker in text for marker in markers):
                        return True, text
            except socket.timeout:
                continue
            except OSError:
                break
        return False, buf.decode(errors="replace")

    def send(self, filename, line_batch=1, drain_timeout=0, timeout=300):
        """Send a file as S-records for U-Boot ``loads`` (no per-line ACK).

        Each S-record line is sent in its own datagram so U-Boot's 512-byte
        netconsole input buffer is not overrun mid-line.  ``drain_timeout`` should
        stay at zero; a non-zero value adds per-line latency on high-RTT links.
        """
        path = Path(filename)
        try:
            line_iter = self._iter_srec_lines(path)
        except (ValueError, OSError):
            line_iter = self._iter_srec_lines_objcopy(path)

        self.sock.sendall(b"loads $loadaddr\n")
        ready, banner = self._recv_until_text(
            ("Ready for S-Record download", "S-Record download"),
            timeout=30,
        )
        if not ready:
            self.log.error("loads: timed out waiting for U-Boot ready banner: %r", banner)
            return False

        sent_lines = 0
        pending = []
        for line in line_iter:
            pending.append(line)
            if len(pending) < line_batch:
                continue
            for record in pending:
                self.sock.sendall(record)
                sent_lines += 1
            pending.clear()
            if drain_timeout > 0:
                deadline = time.monotonic() + drain_timeout
                self.sock.settimeout(drain_timeout)
                while time.monotonic() < deadline:
                    try:
                        self.sock.recv(4096)
                    except socket.timeout:
                        break
                    except OSError:
                        break

        for record in pending:
            self.sock.sendall(record)
            sent_lines += 1

        if sent_lines == 0:
            self.log.error("loads: no S-records generated for %s", path)
            return False

        done, trailer = self._recv_until_text(
            ("## Start Addr", "S-Record download aborted"),
            timeout=timeout,
        )
        if not done:
            self.log.error("loads: timed out waiting for completion banner: %r", trailer)
            return False
        if "aborted" in trailer:
            self.log.error("loads: U-Boot reported aborted transfer: %r", trailer)
            return False
        return True
