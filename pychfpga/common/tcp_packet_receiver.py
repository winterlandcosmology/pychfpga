"""TCP framed packet receiver compatible with :class:`UDPPacketReceiver`."""

from __future__ import annotations

import select
import socket
import struct
import time

import numpy as np

# Little-endian framing: cookie, counter, counter_inv, length
DATA_TCP_COOKIE = 0xDA4A
DATA_TCP_HEADER_STRUCT = struct.Struct("<HHHH")
DATA_TCP_HEADER_LEN = DATA_TCP_HEADER_STRUCT.size
assert DATA_TCP_HEADER_LEN == 8


class TCPPacketReceiver:
    """Receives framed TCP streams into the same buffer contract as UDPPacketReceiver.

    Each record is ``cookie(u16 LE)=0xDA4A``, ``counter``, ``counter ^ 0xFFFF``,
    ``length``, then ``length`` payload bytes. Accepts a list of connected TCP
    sockets (typically one per board).
    """

    def __init__(
        self,
        socks,
        buf_size=2048,
        max_packet_size=9000,
        verbose=1,
        report_pkt_len_me: int = 0,
    ):
        if isinstance(socks, socket.socket):
            socks = [socks]
        self.socks = list(socks)
        self.n_packets = buf_size
        self.max_packet_size = max_packet_size
        self.verbose = verbose
        self.report_pkt_len_me = report_pkt_len_me

        self.buf = np.zeros((self.n_packets, self.max_packet_size), dtype=np.uint8)
        self.pkt_len = np.zeros((self.n_packets,), dtype=np.uint32)
        self.n_stored = 0

        # Per-socket carry of incomplete stream bytes
        self._carry = [bytearray() for _ in self.socks]
        self._dead = set()
        self._timeout = None
        for s in self.socks:
            try:
                self._timeout = s.gettimeout()
                break
            except OSError:
                pass

    def settimeout(self, timeout):
        self._timeout = timeout
        for s in self.socks:
            s.settimeout(timeout)

    def gettimeout(self):
        return self._timeout

    @staticmethod
    def _header_ok(cookie: int, counter: int, counter_inv: int) -> bool:
        return cookie == DATA_TCP_COOKIE and (counter ^ counter_inv) == 0xFFFF

    def _resync_carry(self, carry: bytearray) -> bytearray:
        """Drop bytes until a plausible framed header starts, or leave <2 bytes."""
        i = 0
        n = len(carry)
        while i + DATA_TCP_HEADER_LEN <= n:
            cookie, counter, counter_inv, length = DATA_TCP_HEADER_STRUCT.unpack_from(carry, i)
            if self._header_ok(cookie, counter, counter_inv) and length <= self.max_packet_size:
                if i:
                    del carry[:i]
                return carry
            i += 1
        # Keep a trailing partial cookie candidate
        if i < n:
            del carry[:i]
        else:
            carry.clear()
        return carry

    def _drain_carry_into_buf(self, sock_idx: int, n: int, verbose: int) -> int:
        """Peel complete frames from carry[sock_idx] into buf starting at n. Returns new n."""
        carry = self._carry[sock_idx]
        while n < self.n_packets and len(carry) >= DATA_TCP_HEADER_LEN:
            cookie, counter, counter_inv, length = DATA_TCP_HEADER_STRUCT.unpack_from(carry, 0)
            if not self._header_ok(cookie, counter, counter_inv) or length > self.max_packet_size:
                self._resync_carry(carry)
                if len(carry) < DATA_TCP_HEADER_LEN:
                    break
                continue
            need = DATA_TCP_HEADER_LEN + length
            if len(carry) < need:
                break
            payload = bytes(carry[DATA_TCP_HEADER_LEN:need])
            self.buf[n, :length] = np.frombuffer(payload, dtype=np.uint8)
            self.pkt_len[n] = length
            if verbose >= 3 and length > self.report_pkt_len_me:
                print(
                    f"receive_packets: got TCP packet {n:03d} sock={sock_idx} "
                    f"ctr={counter} len={length}: {self.buf[n, :26].tobytes().hex(':')}"
                )
            del carry[:need]
            n += 1
        return n

    def receive_packets(self, verbose=None):
        """Receive framed packets until timeout or the buffer is full."""
        v = self.verbose if verbose is None else verbose
        n = 0
        deadline = None
        timeout = self._timeout
        if timeout is not None and timeout > 0:
            deadline = time.monotonic() + timeout

        while n < self.n_packets:
            # First empty any already-buffered complete frames
            for i in range(len(self.socks)):
                n = self._drain_carry_into_buf(i, n, v)
                if n >= self.n_packets:
                    break
            if n >= self.n_packets:
                break

            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    if n:
                        break
                    # Match UDPPacketReceiver: keep waiting when empty
                    deadline = time.monotonic() + timeout
                    remaining = timeout
                wait = remaining
            else:
                wait = 0.0 if timeout == 0 else (timeout if timeout is not None else 1.0)

            live = [s for s in self.socks if s not in self._dead]
            if not live:
                break

            try:
                readable, _, _ = select.select(live, [], [], wait)
            except (ValueError, OSError):
                break
            if not readable:
                if n:
                    break
                continue

            for s in readable:
                if n >= self.n_packets:
                    break
                sock_idx = self.socks.index(s)
                try:
                    chunk = s.recv(65536)
                except socket.timeout:
                    continue
                except OSError:
                    self._dead.add(s)
                    continue
                if not chunk:
                    # Peer closed — stop selecting this socket (avoid spin).
                    self._dead.add(s)
                    if v >= 2:
                        print(f"{self.__class__.__name__}: socket {sock_idx} closed by peer")
                    continue
                self._carry[sock_idx].extend(chunk)
                n = self._drain_carry_into_buf(sock_idx, n, v)

        if v >= 2:
            print(f"{self.__class__.__name__}: Returning {n} packets")
        if n < self.n_stored:
            self.pkt_len[n : self.n_stored] = 0
        elif n < self.n_packets:
            self.pkt_len[n:] = 0
        self.n_stored = n
        return n

    def flush(self, timeout=0.001, max_flush_time=2, verbose=0):
        """Drain TCP sockets and clear per-socket carry buffers."""
        flushed_packets = 0
        t0 = time.time()
        old_timeout = self.gettimeout()
        self.settimeout(timeout)
        if verbose >= 2:
            print("Flushing TCP data sockets")
        while time.time() - t0 < max_flush_time:
            try:
                readable, _, _ = select.select(self.socks, [], [], timeout)
            except (ValueError, OSError):
                break
            if not readable:
                break
            for s in readable:
                try:
                    chunk = s.recv(65536)
                except socket.timeout:
                    continue
                except OSError:
                    continue
                if not chunk:
                    continue
                # Count approximate frames for logging
                flushed_packets += max(1, len(chunk) // 64)
                if verbose >= 2:
                    print(f"flushing TCP chunk len={len(chunk)}")
        for carry in self._carry:
            carry.clear()
        if old_timeout is not None:
            self.settimeout(old_timeout)
        if verbose:
            print(f"Flushed ~{flushed_packets} TCP chunks")
        return flushed_packets
