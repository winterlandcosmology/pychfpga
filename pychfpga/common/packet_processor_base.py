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

    @abstractmethod
    def update_config(self, config=None) -> None:
        """Apply configuration (subclass-specific)."""

    @abstractmethod
    def filter_packets(self, n: int) -> Optional[np.ndarray]:
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
    def store_packets(self, bix: np.ndarray, tix: np.ndarray) -> None:
        """Put UDP packets data into the raw_data output buffer.

        Must update self.raw_data_count with the number of packets that were processed. Will be used to compute how many packets are returned.
        
        Parameters
        ----------
        bix (np.ndarray): Row indices into the UDP buffer that contain the data we want to process.
        tix (np.ndarray): Array of the target row index in the output buffer for each selected UDP packet.
        """


    @abstractmethod
    def emit_complete_rows(self, n_out: int, verbose: int):
        """Build the return tuple for ``n_out`` complete leading rows.

        Must update ``self._last_emitted_ts`` to the last emitted timestamp key.
        """

    def process_packets(self, config=None, verbose = None):
        """Process one active UDP packet block.

        Parameters
        ----------
        config (dict or State or None): The configuration to use for the processor.
        verbose (int or None): The verbosity level to use for the processor.

        Returns a subclass-specific tuple of views for complete timestamp rows, or
        ``None`` when there is nothing to emit.
        """
        verbose = self.verbose if verbose is None else verbose
        self.update_config(config)

        n = self.pr.n_stored  # number of packets in the UDP buffer
        if not n:
            return None

        # Filter the packets to process. Return list of row indices into the udb buffer that contain the data we want to process.
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
        w = min(self.receive_window, n_ts)
        raw_counts_tail = self.raw_data_count[n_ts - w:n_ts]
        complete_tail = (raw_counts_tail >= self.expected_packets).reshape(w,-1).all(axis=-1)[::-1]  # collapse all but the first dimension and reverse order
        self.carry_len = int(np.argmax(complete_tail)) if complete_tail.size and complete_tail.any() else w

        # COmpute the number of packets to return, 
        # Withholding up carryover rows 
        n_out = max(0, n_ts - self.carry_len)

        if not n_out:
            return None

        return self.emit_complete_rows(n_out, verbose)
