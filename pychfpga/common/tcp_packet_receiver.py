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

    Multi-socket receive is fair: sockets are drained round-robin so one busy
    board cannot fill the packet buffer alone (which would leave other
    channels as zeros after assembly).

    The data sockets are assumed stable for a session (firmware drops on
    backpressure instead of closing). Reconnect by calling
    ``get_data_receiver_async(..., force=True)`` on array reinit.
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
        self._rr = 0  # round-robin start index
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
            try:
                s.settimeout(timeout)
            except OSError:
                pass

    def gettimeout(self):
        return self._timeout

    def _live_socks(self):
        return [s for s in self.socks if s not in self._dead]

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

    def _pop_one_frame(self, sock_idx: int, n: int, verbose: int) -> int:
        """If carry[sock_idx] has one complete frame, copy it into buf[n]. Returns new n."""
        carry = self._carry[sock_idx]
        while len(carry) >= DATA_TCP_HEADER_LEN:
            cookie, counter, counter_inv, length = DATA_TCP_HEADER_STRUCT.unpack_from(carry, 0)
            if not self._header_ok(cookie, counter, counter_inv) or length > self.max_packet_size:
                self._resync_carry(carry)
                if len(carry) < DATA_TCP_HEADER_LEN:
                    return n
                continue
            need = DATA_TCP_HEADER_LEN + length
            if len(carry) < need:
                return n
            payload = bytes(carry[DATA_TCP_HEADER_LEN:need])
            self.buf[n, :length] = np.frombuffer(payload, dtype=np.uint8)
            self.pkt_len[n] = length
            if verbose >= 3 and length > self.report_pkt_len_me:
                print(
                    f"receive_packets: got TCP packet {n:03d} sock={sock_idx} "
                    f"ctr={counter} len={length}: {self.buf[n, :26].tobytes().hex(':')}"
                )
            del carry[:need]
            return n + 1
        return n

    def _drain_fair(self, n: int, verbose: int) -> int:
        """Round-robin peel complete frames from all carries into ``buf``."""
        nsock = len(self.socks)
        if nsock == 0:
            return n
        start = self._rr % nsock
        # Keep taking one frame per socket until no socket yields or buffer full.
        progress = True
        while n < self.n_packets and progress:
            progress = False
            for k in range(nsock):
                if n >= self.n_packets:
                    break
                sock_idx = (start + k) % nsock
                before = n
                n = self._pop_one_frame(sock_idx, n, verbose)
                if n > before:
                    progress = True
        self._rr = (start + 1) % nsock
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
            n = self._drain_fair(n, v)
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

            live = self._live_socks()
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

            # Fair read order: rotate which readable socket we service first.
            nsock = len(self.socks)
            order = []
            for k in range(nsock):
                sock_idx = (self._rr + k) % nsock
                s = self.socks[sock_idx]
                if s in readable:
                    order.append((sock_idx, s))

            for sock_idx, s in order:
                if n >= self.n_packets:
                    break
                try:
                    chunk = s.recv(65536)
                except socket.timeout:
                    continue
                except OSError:
                    self._dead.add(s)
                    if v >= 2:
                        print(f"{self.__class__.__name__}: socket {sock_idx} recv error")
                    continue
                if not chunk:
                    self._dead.add(s)
                    if v >= 2:
                        print(f"{self.__class__.__name__}: socket {sock_idx} closed by peer")
                    continue
                self._carry[sock_idx].extend(chunk)
                # One frame from this socket then move on (fairness).
                n = self._pop_one_frame(sock_idx, n, v)

            # After reading, drain any remaining complete frames fairly.
            n = self._drain_fair(n, v)

        if v >= 2:
            print(f"{self.__class__.__name__}: Returning {n} packets")
        if n < self.n_stored:
            self.pkt_len[n : self.n_stored] = 0
        elif n < self.n_packets:
            self.pkt_len[n:] = 0
        self.n_stored = n
        return n

    def _discard_complete_frames(self, sock_idx: int) -> int:
        """Drop complete framed records from carry[sock_idx]; keep a trailing partial.

        Returns the number of complete frames discarded.
        """
        carry = self._carry[sock_idx]
        discarded = 0
        while len(carry) >= DATA_TCP_HEADER_LEN:
            cookie, counter, counter_inv, length = DATA_TCP_HEADER_STRUCT.unpack_from(carry, 0)
            if not self._header_ok(cookie, counter, counter_inv) or length > self.max_packet_size:
                self._resync_carry(carry)
                if len(carry) < DATA_TCP_HEADER_LEN:
                    break
                continue
            need = DATA_TCP_HEADER_LEN + length
            if len(carry) < need:
                break
            del carry[:need]
            discarded += 1
        return discarded

    def flush(self, timeout=0.001, max_flush_time=2, verbose=0):
        """Discard complete framed records until the stream goes quiet.

        Bytes are fed through the per-socket carry buffer so a trailing partial
        record is preserved (unlike clearing carry, which desyncs the stream).
        Stops when no socket is readable for ``timeout`` (typically less than the
        inter-timestamp gap). ``max_flush_time`` is only a hang safety if the
        stream never quiets.
        """
        if timeout is None:
            timeout = 0.001
        if max_flush_time is None:
            max_flush_time = 2.0
        flushed_packets = 0
        t0 = time.time()
        old_timeout = self.gettimeout()
        self.settimeout(timeout)
        if verbose >= 1:
            print(
                f"Flushing TCP data sockets "
                f"(quiet={timeout:.3g}s, max={max_flush_time:.3g}s; carry preserved)"
            )
        live = self._live_socks()
        while live and (time.time() - t0) < max_flush_time:
            try:
                readable, _, _ = select.select(live, [], [], timeout)
            except (ValueError, OSError):
                break
            if not readable:
                # Quiet for ``timeout`` — stop between bursts when timeout < inter-ts gap.
                break
            for s in readable:
                try:
                    idx = self.socks.index(s)
                except ValueError:
                    continue
                try:
                    chunk = s.recv(65536)
                except socket.timeout:
                    continue
                except OSError:
                    self._dead.add(s)
                    continue
                if not chunk:
                    self._dead.add(s)
                    continue
                self._carry[idx].extend(chunk)
                flushed_packets += self._discard_complete_frames(idx)
                if verbose >= 2:
                    print(
                        f"flushing TCP sock={idx} chunk={len(chunk)} "
                        f"carry={len(self._carry[idx])}"
                    )
            live = self._live_socks()
        else:
            if verbose:
                print(
                    f"Stopped flushing after {max_flush_time} s: data arriving faster "
                    f"than quiet timeout {timeout} s"
                )
        if old_timeout is not None:
            self.settimeout(old_timeout)
        if verbose:
            print(
                f"Flushed {flushed_packets} TCP frames in {time.time() - t0:.3f}s "
                f"(carry kept; {len(self._dead)} dead sockets)"
            )
        return flushed_packets
