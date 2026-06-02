"""Shared UDP packet batch processor base for timestamp-indexed assembly."""

from abc import ABC, abstractmethod
from typing import Any, Optional, Tuple, Union

import warnings

import numpy as np

from .udp_packet_receiver import UDPPacketReceiver


class PacketProcessorBase(ABC):
    """Shared UDP batch processor: timestamp rows, carryover, monotonic emit.

    ``pkt_ts`` holds the per-packet row key: masked packet timestamp (raw capture)
    or integration index (UCORR, ``buf_ts // integ_period``).
    """

    def __init__(
        self,
        packet_receiver: UDPPacketReceiver,
        receive_window: int = 3,
        verbose: int = 0,
    ) -> None:
        self.pr = packet_receiver
        self.receive_window = receive_window
        self.verbose = verbose
        self.carry_len = 0
        self.n_out = 0
        self.overrun = 0
        self._last_emitted_ts = np.uint64(0)
        self.n_ts = 0

        n_packets = self.pr.n_packets
        self.bix_mask = np.zeros((n_packets,), dtype=bool)
        self.tmp_mask = np.zeros((n_packets + receive_window,), dtype=bool)
        # Extra timestamp row(s) for carryover before recomputing unique timestamps of a new UDP batch.
        self.pkt_ts_with_carry = np.empty(
            (n_packets + receive_window,), dtype=np.uint64
        )
        self.pkt_ts = self.pkt_ts_with_carry[:n_packets]
        self.pkt_tix = np.empty((n_packets + receive_window,), dtype=np.int64)

        self.init()

    @abstractmethod
    def init(self) -> None:
        """Set up UDP buffer views, per-packet decode scratch, and empty data buffers."""

    def resize_buffer(self, buf_name: str, dims, element_dtype=None) -> np.ndarray:
        """Return a ``dims`` view into ``buf_name``, reallocating raw bytes if needed."""
        dt = np.dtype(element_dtype)
        nel = int(np.prod(dims))
        if nel == 0:
            return np.empty(dims, dtype=dt)
        nbytes = nel * dt.itemsize
        buf = getattr(self, buf_name)
        raw = buf.view(np.uint8).reshape(-1) if buf.size else np.empty(0, dtype=np.uint8)
        if nbytes > raw.size:
            raw = np.zeros(nbytes, dtype=np.uint8)
            setattr(self, buf_name, raw)
        return raw[:nbytes].view(dt).reshape(dims)

    def config_ready(self) -> bool:
        """Return True when ``process_packets`` may run (subclasses may override)."""
        return True

    @abstractmethod
    def update_config(self, config=None) -> None:
        """Apply configuration (subclass-specific)."""

    @abstractmethod
    def filter_packets(self, n: int) -> Optional[np.ndarray]:
        """Decode and select packets; return UDP row indices ``bix`` or ``None`` if unusable."""

    @abstractmethod
    def _relocate_carryover_rows(
        self, cix: slice, carry_tix: np.ndarray, tix_to_clear: np.ndarray
    ) -> None:
        """Copy carryover row state to new timestamp indices; clear ``tix_to_clear`` flags."""

    @abstractmethod
    def _clear_ts_rows(self, n_ts: int, tix_to_clear: np.ndarray) -> None:
        """Zero timestamp rows marked True in ``tix_to_clear``."""

    @abstractmethod
    def store_packets(self, n: int, bix: np.ndarray, tix: np.ndarray) -> None:
        """Integrate/scatter valid packets into the timestamp-indexed data buffer."""

    @abstractmethod
    def _compute_carry_len(self, n_ts: int) -> int:
        """Trailing incomplete timestamp rows to withhold until the next UDP block."""

    @abstractmethod
    def _emit_complete_rows(
        self, n_out: int, verbose: int
    ) -> Optional[Tuple[Any, ...]]:
        """Build return values for ``n_out`` complete leading rows; update ``_last_emitted_ts``."""

    def _on_ts_overflow(self, dropped: int) -> None:
        """Hook when oldest timestamps are dropped from the buffer."""

    def process_packets(
        self,
        config=None,
        verbose: Optional[int] = None,
    ) -> Optional[Tuple[Any, ...]]:
        """Process one active UDP packet block.

        Returns a subclass-specific tuple of views for complete timestamp rows, or
        ``None`` when there is nothing to emit.
        """
        verbose = self.verbose if verbose is None else verbose
        self.update_config(config)
        if not self.config_ready():
            return None

        n = self.pr.n_stored  # number of packets in the UDP buffer
        if not n:
            return None

        bix = self.filter_packets(n)
        if bix is None:
            return None

        old_nts = self.n_ts
        n_carry = self.carry_len
        cix = slice(old_nts - n_carry, old_nts)  # indices of the carryover rows in the output buffer

        if not bix.size and not n_carry:
            return None

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
            self._on_ts_overflow(dropped)
            ts_unique = ts_unique[dropped:]
            tix -= dropped  # decrease the destination timestamp index for each packet. Last n_carry are the carry ones
            carry_tix -= dropped
            if bix.size:
                self.bix_mask[bix[tix < 0]] = False  # skip the packets that were dropped in the buffer row index mask
                bix = bix[tix >= 0]  # skip the packets that were dropped in the buffer row list
                tix = tix[tix >= 0]

        tix_to_clear = np.ones(n_ts, dtype=bool)
        cix_mask = carry_tix >= 0  # find indices of carryover timestamps that were not dropped
        if cix_mask.any():
            self._relocate_carryover_rows(cix, carry_tix, tix_to_clear)

        self._clear_ts_rows(n_ts, tix_to_clear)

        self.ts_array[:n_ts] = ts_unique
        self.n_ts = n_ts

        self.store_packets(n, bix, tix)

        self.carry_len = self._compute_carry_len(n_ts)

        # Withhold trailing *incomplete* rows (``self.carry_len``), not the carry merge width
        # from the previous block (that was ``n_carry`` only).
        n_out = max(0, n_ts - self.carry_len)

        if not n_out:
            return None

        return self._emit_complete_rows(n_out, verbose)
