import numpy as np
import socket
import struct
import threading
import time


class UDPPacketReceiver:
    """Receives and stores UDP packets until the buffer is full or a timeout has occurred.

        Parameters:

    Parameters
    ----------
    sock : socket.socket
        Socket used for receiving UDP packets.

    buf_size : int
        Packets per batch (public ``buf`` and each ping-pong fill buffer). Default
        8192 is large enough that, with two buffers, one can fill through a
        ``format`` stall (~160 ms at ~70 kpkt/s) while the other sits ready.

    max_packet_size : int
        Maximum expected UDP payload length (second axis of ``buf``).

    threaded : bool
        If True (default), a background thread drains the socket into two
        ping-pong fill buffers while the consumer processes the previous batch.
        After :meth:`receive_packets` returns, ``buf``, ``pkt_len`` and
        ``n_stored`` hold the current batch; ``buf`` keeps the same array
        identity (the completed batch is copied into it) so cached views stay
        valid. Set False for the legacy inline behavior.
    """

    def __init__(self, sock, buf_size=8192, max_packet_size=9000, verbose=1,
                 report_pkt_len_me: int = 0, threaded: bool = True):

        self.sock = sock
        self.n_packets = buf_size
        self.max_packet_size = max_packet_size
        self.verbose = verbose
        self.report_pkt_len_me = report_pkt_len_me

        # Public batch surface (identity is stable; contents are refreshed in place).
        self.buf = np.zeros((self.n_packets, self.max_packet_size), dtype=np.uint8)
        self.pkt_len = np.zeros((self.n_packets,), dtype=np.uint32) # packet length. 0= unused slot.
        self.n_stored = 0  # number of packets currently stored in the buffer

        self._threaded = bool(threaded)
        # Consumer-side wait budget (set via settimeout); how long receive_packets()
        # blocks for a batch before returning empty. In inline mode this is applied
        # directly to the socket instead.
        self._consumer_timeout = 1.0

        if self._threaded:
            # Two fill buffers: the thread writes ``_fill``, publishes it as
            # ``_ready``, then fills the other. It only parks when both are full
            # (ready not yet taken). The socket is owned exclusively by the rx thread.
            self._rx_poll_timeout = 0.01
            self._bufs = [
                np.zeros((self.n_packets, self.max_packet_size), dtype=np.uint8),
                np.zeros((self.n_packets, self.max_packet_size), dtype=np.uint8),
            ]
            self._lens = [
                np.zeros((self.n_packets,), dtype=np.uint32),
                np.zeros((self.n_packets,), dtype=np.uint32),
            ]
            self._ready = -1  # index of a published batch, or -1
            self._ready_n = 0
            self._cond = threading.Condition()
            self._stop = threading.Event()
            self._discard = threading.Event()  # drain-and-drop (flush)
            self._consumer_waiting = False
            self._flush_idle = False  # recv timeout while discard is set
            self._scratch = np.zeros((self.max_packet_size,), dtype=np.uint8)
            self._last_rx = 0.0
            self._thread = threading.Thread(target=self._rx_loop, name="UDPPacketReceiver-rx", daemon=True)
            self._thread.start()

    def settimeout(self, timeout):
        """ Sets the timeout on the socket

        Parameter:

            timeout (float): timeout in seconds

        """
        self._consumer_timeout = timeout
        if not self._threaded:
            self.sock.settimeout(timeout)

    def gettimeout(self):
        """ Returns the current timeout on the socket

        Returns:

            float: current timeout in seconds

        """
        if self._threaded:
            return self._consumer_timeout
        return self.sock.gettimeout()

    # ------------------------------------------------------------------ #
    # Background receiver (threaded mode)
    # ------------------------------------------------------------------ #
    def _rx_loop(self):
        """Drain the socket into the two ping-pong buffers until stopped.

        Fills one buffer until it is full (or the consumer is waiting, for a
        partial). Publishes it, then immediately fills the other. Parks only
        when the previous ready batch has not been taken yet. Flush drains into
        a scratch row so it never needs a ping-pong slot. Idle polls do not
        publish crumbs.
        """
        self.sock.settimeout(self._rx_poll_timeout)
        scratch = self._scratch
        fill = 0
        n = 0
        while not self._stop.is_set():
            if self._discard.is_set():
                n = 0
                try:
                    self.sock.recvfrom_into(scratch)
                    self._last_rx = time.monotonic()
                    self._flush_idle = False
                except socket.timeout:
                    self._flush_idle = True
                except OSError:
                    self._stop.set()
                    break
                continue

            buf = self._bufs[fill]
            lens = self._lens[fill]
            publish = False
            while n < self.n_packets and not self._stop.is_set() and not self._discard.is_set():
                try:
                    nbytes, addr = self.sock.recvfrom_into(buf[n])
                    lens[n] = nbytes
                    self._last_rx = time.monotonic()
                    if self.verbose >= 3 and nbytes > self.report_pkt_len_me:
                        print(f"receive_packets: got raw packet {n:03d} from {addr}, len={nbytes}: {buf[n,:26].tobytes().hex(':')}")
                    n += 1
                except socket.timeout:
                    if n and self._consumer_waiting:
                        publish = True
                        break
                    continue
                except OSError:
                    self._stop.set()
                    break
            if self._stop.is_set():
                break
            if self._discard.is_set():
                n = 0
                continue
            if n >= self.n_packets:
                publish = True
            if publish and n:
                with self._cond:
                    while self._ready >= 0 and not self._stop.is_set() and not self._discard.is_set():
                        self._cond.wait()
                    if self._stop.is_set() or self._discard.is_set():
                        n = 0
                        continue
                    self._ready_n = n
                    self._ready = fill
                    self._cond.notify_all()
                fill = 1 - fill
                n = 0

    def receive_packets(self, verbose=None):
        """Receive packets until timeout or the buffer is full.

        In threaded mode this hands back the next ping-pong batch (copying it
        into :attr:`buf` so its identity is stable), blocking up to the consumer
        timeout for one to appear. In inline mode it drains the socket directly
        (legacy behavior).

        Parameters
        ----------
        verbose : int or None
            If ``None``, use :attr:`verbose` on this receiver. If set, that level is used
            for this call only (it is **not** raised to match :attr:`verbose`), so a parent
            such as :class:`~pychfpga.common.raw_frame_receiver.RawFrameReceiver` can force
            quiet UDP logs with ``verbose=0`` even when the packet receiver was constructed
            with a higher default.
        """
        if not self._threaded:
            return self._receive_packets_inline(verbose)

        v = self.verbose if verbose is None else verbose
        timeout = self._consumer_timeout
        deadline = None if not timeout or timeout <= 0 else time.monotonic() + timeout
        # Only advertise "waiting" while we actually block. Setting it around the
        # whole call would make the rx thread publish crumbs whenever the consumer
        # is merely copying a full batch.
        n = 0
        idx = -1
        try:
            with self._cond:
                while self._ready < 0 and not self._stop.is_set():
                    self._consumer_waiting = True
                    if deadline is None:
                        self._cond.wait()
                    else:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        self._cond.wait(remaining)
                    self._consumer_waiting = False
                if self._ready >= 0:
                    idx = self._ready
                    n = self._ready_n
        finally:
            self._consumer_waiting = False

        if idx < 0:
            if self.n_stored:
                self.pkt_len[:self.n_stored] = 0
            self.n_stored = 0
            if v >= 2:
                print(f'{self.__class__.__name__}: Returning 0 packets (timeout)')
            return n

        # Copy while the slot is still marked ready so the rx thread cannot start
        # refilling it. Recycle after the copy.
        self.buf[:n] = self._bufs[idx][:n]
        self.pkt_len[:n] = self._lens[idx][:n]
        if n < self.n_packets:
            self.pkt_len[n:] = 0
        self.n_stored = n
        with self._cond:
            if self._ready == idx:
                self._ready = -1
                self._ready_n = 0
            self._cond.notify_all()

        if v >= 2:
            print(f'{self.__class__.__name__}: Returning {n} packets')
        return n

    def _receive_packets_inline(self, verbose=None):
        """Legacy single-threaded receive: drain the socket directly into ``buf``."""
        v = self.verbose if verbose is None else verbose
        n = 0
        while n < self.n_packets:
            try:
                nbytes, addr = self.sock.recvfrom_into(self.buf[n])
                self.pkt_len[n] = nbytes
                if v >= 3 and nbytes > self.report_pkt_len_me:
                    print(f"receive_packets: got raw packet {n:03d} from {addr}, len={nbytes}: {self.buf[n,:26].tobytes().hex(':')}")
                n += 1
            except socket.timeout:  # we have a timeout, so we probably have time to process data
                if not n:
                    continue
                if v >= 3:
                    print(f'receive_packets: timeout')
                break
        # we get here if there is a timeout or if the buffer is full
        if v >= 2:
            print(f'{self.__class__.__name__}: Returning {n} packets')
        # Clear the packet length for the packets that were not received
        # We optimize speed by clearing only from n up to the last stored packet, not the entire buffer after n
        # Benchmarks show that this is a significant speed up.
        if n < self.n_stored:
            self.pkt_len[n:self.n_stored] = 0
        elif n < self.n_packets:  # Effectively if n != self.n_packets
            self.pkt_len[n:] = 0
        self.n_stored = n
        return n

    def udp_socket_stats(self):
        """Return ``(rx_queue_bytes, drops)`` for this UDP socket, or ``None``.

        Reads ``/proc/net/udp`` (Linux). ``drops`` is the kernel's per-socket
        RCVBUF overflow counter — the same column ``watch cat /proc/net/udp`` shows.
        """
        sock = getattr(self, "sock", None)
        if sock is None:
            return None
        try:
            ip, port = sock.getsockname()[:2]
        except OSError:
            return None
        try:
            ip_u = struct.unpack("<I", socket.inet_aton(ip))[0]
        except OSError:
            return None
        key = f"{ip_u:08X}:{port:04X}".lower()
        try:
            with open("/proc/net/udp", "r", encoding="ascii") as f:
                next(f)
                for line in f:
                    parts = line.split()
                    if len(parts) < 13:
                        continue
                    if parts[1].lower() == key:
                        rx_queue = int(parts[4].split(":")[1], 16)
                        drops = int(parts[12])
                        return rx_queue, drops
        except OSError:
            return None
        return None

    def flush(self, timeout=0.001, max_flush_time=2, verbose=0):
        """ Flush the UDP buffer (read packets until timeout).
        Note: Partial frame data may start to fill the UDP buffer while we start to flush it. There is still likely a partial frame in the buffer after this operation.

        """
        if timeout is None:
            timeout = 0.001
        if not self._threaded:
            return self._flush_inline(timeout, max_flush_time, verbose)

        # Discard mode drains the kernel into a scratch row (no ping-pong slot).
        # Drop any published batch, then wait until rxq is empty — not a stale
        # last-packet timestamp (the thread may be parked on a full ready slot).
        if verbose >= 2:
            print('Flushing UDP buffer (threaded)')
        self._flush_idle = False
        self._discard.set()
        with self._cond:
            self._ready = -1
            self._ready_n = 0
            self._cond.notify_all()
        try:
            t0 = time.monotonic()
            while time.monotonic() - t0 < max_flush_time:
                stats = self.udp_socket_stats()
                rxq = stats[0] if stats else None
                if rxq is not None:
                    if rxq <= self.max_packet_size:
                        time.sleep(self._rx_poll_timeout)
                        stats = self.udp_socket_stats()
                        if stats and stats[0] <= self.max_packet_size:
                            break
                elif self._flush_idle:
                    break
                time.sleep(min(max(timeout, 0.001), 0.005))
            with self._cond:
                self._ready = -1
                self._ready_n = 0
                self._cond.notify_all()
        finally:
            self._discard.clear()
            with self._cond:
                self._cond.notify_all()

        if self.n_stored:
            self.pkt_len[:self.n_stored] = 0
        self.n_stored = 0
        if verbose:
            print('Flushed UDP buffer (threaded)')
        return 0

    def _flush_inline(self, timeout=0.001, max_flush_time=2, verbose=0):
        """Legacy flush: read packets directly until a timeout gap."""
        flushed_packets = 0
        t0 = time.time()
        # Read packets until timeout
        self.sock.settimeout(timeout)
        if verbose >= 2:
            print('Flushing UDP buffer')
        while time.time() - t0 < max_flush_time:  # give up after some time
            try:
                s = self.sock.recv_into(self.buf[0])
                flushed_packets += 1
                if verbose >= 2:
                    print(f"flushing packet Starting with {self.buf[0][:10].tobytes().hex(':')}")

            except socket.timeout:
                break
        else:
            print(f'Stopped flushing after {max_flush_time} s: packets are arriving faster that the specified timeout period of {timeout} s')
        if verbose:
            print(f'Flushed {flushed_packets} packets')
        return flushed_packets

    def close(self):
        """Stop the background receiver thread (threaded mode). Safe to call repeatedly."""
        if not self._threaded:
            return
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        thread = getattr(self, "_thread", None)
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=1.0)

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
