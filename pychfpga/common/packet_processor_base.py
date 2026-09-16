"""Shared UDP packet batch processor base for timestamp-indexed assembly."""

from abc import ABC, abstractmethod
import warnings

import numpy as np


class PacketProcessorBase(ABC):
    """Shared UDP batch processor: timestamp rows, carryover, monotonic emit.

    ``pkt_ts`` holds the per-packet row key: masked packet timestamp (raw capture)
    or integration index (UCORR, ``buf_ts // integ_period``).
    """

    # Number of processed "frames" (logical packets) carried by one received UDP
    # datagram. Default 1: one datagram is one frame (raw capture and every
    # subclass that does not aggregate). Subclasses that pack several records per
    # datagram (e.g. UCORR bin aggregation) override this so all per-packet scratch
    # below is sized to the flat frame space ``M = n_packets * FRAMES_PER_PACKET``.
    FRAMES_PER_PACKET = 1

    def __init__(
        self,
        packet_receiver,
        receive_window=3,
        verbose=0,
    ):
        self.pr = packet_receiver
        if not receive_window:
            raise ValueError("receive_window must be greater than 0")
        self.receive_window = receive_window
        self.verbose = verbose
        self.carry_len = 0
        self.out_rows = slice(0, 0)
        self.n_out = 0  # visible count: out_rows.stop - out_rows.start
        self.skip_first_incomplete = False
        self.overrun = 0
        self._last_emitted_ts = np.uint64(0)
        self.n_ts = 0

        # Per-packet scratch is indexed by a 1D linear frame index ``bix`` (see
        # ``filter_packets``/``store_packets``), so it must span the flat frame
        # space, not just the datagram count. For non-aggregating subclasses
        # FRAMES_PER_PACKET==1 leaves these byte-for-byte as before.
        m = self.pr.n_packets * self.FRAMES_PER_PACKET
        self.bix_mask = np.zeros((m,), dtype=bool)
        self.tmp_mask = np.zeros((m + receive_window,), dtype=bool)
        # Extra timestamp row(s) for carryover before recomputing unique timestamps of a new UDP batch.
        self.pkt_ts_with_carry = np.empty(
            (m + receive_window,), dtype=np.uint64
        )
        self.pkt_ts = self.pkt_ts_with_carry[:m]
        self.pkt_tix = np.empty((m + receive_window,), dtype=np.int64)

        self.init()

    @abstractmethod
    def init(self):
        """Set up UDP buffer views, per-packet decode scratch, and empty data buffers."""

    def resize_buffer(self, buf_name, dims, element_dtype=None):
        """Return a ``dims`` view into ``buf_name``, reallocating raw bytes if needed."""
        dt = np.dtype(element_dtype)
        nel = int(np.prod(dims))
        if nel == 0:
            return np.empty(dims, dtype=dt)
        nbytes = nel * dt.itemsize
        buf_index = None
        if isinstance(buf_name, tuple):
            buf_name, buf_index = buf_name
        buf = getattr(self, buf_name)
        if buf_index is not None:
            buf = buf[buf_index]
        raw = buf.view(np.uint8).reshape(-1) if buf.size else np.empty(0, dtype=np.uint8)
        if nbytes > raw.size:
            raw = np.zeros(nbytes, dtype=np.uint8)
            if buf_index is None:
                setattr(self, buf_name, raw)
            else:
                getattr(self, buf_name)[buf_index] = raw
        return raw[:nbytes].view(dt).reshape(dims)

    def _set_out_rows(self, start, stop):
        """Publish physical usable rows and cache the visible count in ``n_out``."""
        start = int(start)
        stop = int(stop)
        self.out_rows = slice(start, stop)
        self.n_out = max(0, stop - start)

    def is_row_complete(self, row):
        """True if assembler row ``row`` meets the per-cell packet threshold.

        Uses ``packets_per_frame`` when set (raw capture: one cell per channel/frame),
        otherwise ``expected_packets`` (correlator: one count per integration).
        Extra dimensions must all meet the threshold.
        """
        per_cell = getattr(self, "packets_per_frame", None) or self.expected_packets
        counts = np.asarray(self.raw_data_count[row])
        if counts.ndim:
            return bool((counts >= per_cell).all())
        return bool(counts >= per_cell)

    def clip_out_rows(self, rows=None):
        """Clip ``rows`` to ``out_rows``. ``None`` / ``slice(None)`` selects ``out_rows``.

        Returns ``None`` if nothing remains. Explicit slices are physical indices.
        """
        if not self.n_out:
            return None
        lo, hi = self.out_rows.start, self.out_rows.stop
        if rows is None or rows == slice(None):
            return self.out_rows
        if isinstance(rows, int):
            rows = slice(0, rows)
        start, stop, step = rows.indices(hi)
        if step != 1:
            raise ValueError("clip_out_rows only supports step=1")
        start = max(start, lo)
        stop = min(stop, hi)
        if start >= stop:
            return None
        return slice(start, stop)

    @abstractmethod
    def filter_packets(self, n):
        """Decode and select packets; return UDP row indices ``bix`` or ``None`` if unusable."""

    @abstractmethod
    def init_dest_rows(self, n_ts, cix, carry_tix):
        """Relocate carryover rows to their new rows and zero the rest.

        Parameters
        ----------
        n_ts (int): Number of timestamps in the new output buffer.
        cix (slice): Indices of the carryover rows in the old output buffer.
        carry_tix (np.ndarray): Indices of the carryover rows in the new output buffer.

        """

    @abstractmethod
    def store_packets(self, bix, tix):
        """Put UDP packets data into the raw_data output buffer.

        Must update self.raw_data_count with the number of packets that were processed. Will be used to compute how many packets are returned.

        Parameters
        ----------
        bix (np.ndarray): Row indices into the UDP buffer that contain the data we want to process.
        tix (np.ndarray): Array of the target row index in the output buffer for each selected UDP packet.
        """


    def process_udp_packets(self, verbose=None):
        """Assemble one active UDP packet block into internal raw buffers.

        Filters packets, merges timestamps with carryover, scatters payload bytes,
        and publishes ready rows as ``out_rows`` (physical slice) and ``n_out``
        (visible count). Index via ``out_rows``; ``n_out`` is only a count.

        Future: ``get_formatted_data`` returns row ``indices`` into the raw buffer,
        so a follow-up can replace compaction/carry-over with a ring-buffer layout.

        Args:
            verbose: Verbosity level, or None to use ``self.verbose``.

        Returns:
            None. Ready rows are stored in ``self.out_rows``; ``self.n_out`` is
            ``out_rows.stop - out_rows.start``.
        """
        verbose = self.verbose if verbose is None else verbose
        self._set_out_rows(0, 0)

        n = self.pr.n_stored  # number of packets in the UDP buffer
        if not n:
            return

        # Filter the packets to process. Return list of row indices into the udb buffer that contain the data we want to process.
        bix = self.filter_packets(n)
        if bix is None:
            return

        old_nts = self.n_ts
        n_carry = self.carry_len
        cix = slice(old_nts - n_carry, old_nts)  # indices of the carryover rows in the output buffer

        if not bix.size and not n_carry:
            return

        if n_carry > old_nts:
            raise RuntimeError(
                f"{type(self).__name__}: carry_len={n_carry} exceeds prior n_ts={old_nts} "
                "(internal state bug)"
            )

        # Merge carryover timestamps from the previous block into the unique-key pool so their
        # row keeps the same tix as any new packets landing on the same timestamp.
        # Integer index list (valid UDP rows + synthetic carry rows); benchmarked slightly faster
        # than boolean compress + unique on this path (see scripts/bench_ucorr_ts_unique.py).
        ts = np.concatenate((self.pkt_ts[bix], self.ts_array[cix]))
        ts_unique, target_ts = np.unique(ts, return_inverse=True)
        tix, carry_tix = np.split(target_ts, [ts.size - n_carry])

        # Compute how many timestamps we keep in the buffer, and adjust index lists if necessary.
        n_raw_ts = len(ts_unique)
        n_ts = min(n_raw_ts, self.max_timestamps)
        dropped = n_raw_ts - n_ts
        if dropped:
            # drop the oldest timestamps (np.unique sorted them). This is rarely executed,
            # so we don't care about the performance of the code within
            self.on_ts_overflow(dropped)
            if self.verbose:
                print(f"{type(self).__name__}: dropping {dropped} oldest timestamps from the buffer.")
            ts_unique = ts_unique[dropped:]
            tix -= dropped  # decrease the destination timestamp index for each packet. Last n_carry are the carry ones
            carry_tix -= dropped
            if bix.size:
                self.bix_mask[bix[tix < 0]] = False  # skip the packets that were dropped in the buffer row index mask
                bix = bix[tix >= 0]  # skip the packets that were dropped in the buffer row list
                tix = tix[tix >= 0]

        # Relocate carryover rows to their new rows and zero the rest.
        self.init_dest_rows(n_ts, cix, carry_tix)

        self.ts_array[:n_ts] = ts_unique
        self.n_ts = n_ts

        if bix.size:
            self.store_packets(bix, tix)

        # Compute the carry length by checking the raw data count for the trailing rows that are not complete.
        if n_ts == 0:
            self.carry_len = 0
        else:
            w = min(self.receive_window, n_ts) # w cannot be zero
            raw_counts_tail = self.raw_data_count[n_ts - w:n_ts]
            # Raw UCAP rows are (timestamp, channel, frame_index); each cell counts
            # subframes for one physical frame (``packets_per_frame``).  Do not use
            # ``expected_packets`` (full burst total) per cell — that threshold is
            # never reached and causes premature emit of incomplete bursts.
            per_cell = getattr(self, 'packets_per_frame', None) or self.expected_packets
            if raw_counts_tail.ndim > 1:
                # Collapse channel/frame (and any other) dims so argmax counts timestamp rows only.
                complete_tail = (
                    (raw_counts_tail >= per_cell)
                    .reshape(w, -1)
                    .all(axis=-1)[::-1]
                )
            else:
                complete_tail = (raw_counts_tail >= per_cell)[::-1]
            self.carry_len = int(np.argmax(complete_tail)) if complete_tail.size and complete_tail.any() else w

        # Leading rows before the carry window are ready. ``stop`` is a physical
        # exclusive end; skip_first_incomplete may raise ``out_rows.start``.
        stop = max(0, n_ts - self.carry_len)
        if stop:
            # Watermark the last *physical* ready row before skip so a discarded
            # incomplete first row still advances _last_emitted_ts.
            self._last_emitted_ts = self.ts_array[stop - 1]
        start = 0
        if self.skip_first_incomplete and stop > 0:
            if not self.is_row_complete(0):
                start = 1
            self.skip_first_incomplete = False
        self._set_out_rows(start, stop)
        if stop:
            self.on_emit()

    def on_emit(self):
        """Hook after ``out_rows`` is published. Watermark is already updated."""

    @abstractmethod
    def get_formatted_data(self, *args, **kwargs):
        """Return formatted views for rows assembled by the last ``process_udp_packets`` call."""

    def on_ts_overflow(self, dropped: int) -> None:
        """Warn when timestamp keys are dropped because the buffer is full."""
        warnings.warn(
            f"timestamp backlog: {dropped} oldest timestamps dropped.",
            UserWarning,
            stacklevel=3,
        )
