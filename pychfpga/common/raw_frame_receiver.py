from typing import Any, Generator, Optional, Tuple
import itertools
import warnings

import numpy as np

from .raw_data_packet_processor import RawDataPacketProcessor
from .udp_packet_receiver import UDPPacketReceiver


class RawFrameReceiver(RawDataPacketProcessor):
    """Convenience wrapper around :class:`RawDataPacketProcessor` for burst reads."""

    def __init__(
        self,
        packet_receiver: UDPPacketReceiver,
        config: dict,
        verbose: int = 0,
        ignore_first_burst: bool = True,
        **kwargs: Any,
    ) -> None:
        """
        Initialize the raw frame receiver.

        Parameters:
            packet_receiver (UDPPacketReceiver): UDP packet receiver providing the packet buffer.
            raw_capture_state (dict): Serialized ``chfpga.config.raw_capture``.
            verbose (int): Verbosity level for optional prints (0 quiet; ``>= 2`` enables
                ``process_packets`` / flush progress lines).
            ignore_first_burst (bool): If True (default), the first yielded burst is skipped
                only when its timestamp is exactly 0. This handles known startup behavior
                where the first post-reconfiguration burst can be incomplete.
            **kwargs (Any): Additional parameters forwarded to RawDataPacketProcessor
                (e.g., ring_buffer_size, decode_strict).
        """
        super().__init__(
            packet_receiver,
            config=config,
            verbose=verbose,
            **kwargs,
        )
        self.ignore_first_burst = bool(ignore_first_burst)
        # One long-lived generator: ``islice`` may stop mid ``for`` over one ``process_packets``
        # batch; a new ``get_data_iter()`` would abandon those rows and the next
        # ``receive_packets`` would overwrite ``pr.buf`` before they are yielded.
        self.data_iter: Generator = self.get_data_iter()

    def flush(self, flush_timeout: Optional[float] = None) -> None:
        """Clear UDP and assembly state and restart the streaming iterator (matches a clean capture)."""
        super().flush(flush_timeout)
        self.data_iter = self.get_data_iter()

    def get_data_iter(self) -> Generator:
        """
        Continuously receive UDP packets, process them, and yield ``(timestamp, data, data_count)``.

        Uses :attr:`verbose` and any config already applied via :meth:`update_config`. Callers
        normally consume this through :meth:`read_raw_frames_iter`, which shares one iterator
        across calls unless :meth:`flush` runs.
        """
        while True:
            self.pr.receive_packets(verbose=self.verbose)
            if not self.pr.n_stored:
                continue
            result = self.process_packets()
            # print(f"RawFrameReceiver: get_data_iter: processed packets returned {result=}.")
            if not result:
                continue
            ts, data, counts = result
            for ts_, data_, data_count_ in zip(ts, data, counts):
                yield ts_, data_, data_count_

    def read_raw_frames_iter(
        self,
        ncap: int = 1,
        flush: bool = False,
        flush_timeout: Optional[int] = None,
        data_timeout: Optional[int] = None,
        config=None,
        verbose=None,
        **kwargs: Any,
    ):
        """
        Yield ``ncap`` bursts as ``(timestamp, data, data_count)`` tuples.

        Reuses :attr:`data_iter` so a slice that ends in the middle of a ``process_packets``
        result does not drop unconsumed rows (the next call continues the same generator).

        Parameters:
            ncap (int): Number of frames to capture.
            flush (bool): If True, flush UDP buffers and internal ring state before capture
                (also restarts :attr:`data_iter`).
            flush_timeout (Optional[int]): Timeout used while flushing the UDP socket.
            data_timeout (Optional[int]): Socket timeout while waiting for packets.
            config (dict): Configuration dictionary.
            **kwargs: ``verbose`` updates :attr:`verbose` when passed; other keys are ignored
                here (use :meth:`update_config` for decode profile changes).

        Returns:
            Iterator yielding timestamp, raw frame data, and per-channel counts.
        """
        if verbose is not None:
            self.verbose = verbose
            self.pr.verbose = verbose
            print(f"RawFrameReceiver: verbose={verbose}")
        if config is not None:
            self.update_config(config)
        if flush:
            self.flush(flush_timeout)

        if data_timeout is not None:
            self.pr.settimeout(data_timeout)

        return itertools.islice(self.data_iter, ncap)

    def read_raw_frames(
        self,
        ncap: int = 1,
        split_frames: bool = False,
        chan_first: bool = True,
        config=None,
        verbose=None,
        **kwargs: Any,
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """
        Read a fixed number of raw frames into preallocated arrays.

        Parameters:
            ncap (int): Number of frames to capture.
            split_frames (bool): Reserved; logical frame layout comes from capture ``split`` config.
            chan_first (bool): If True (default), ``data`` is ``(nchan, ncap, nsamples)``.
                If False, ``data`` is ``(ncap, nchan, nsamples)`` (same as :meth:`format_data`).
            verbose (int): Verbosity level for optional prints (0 quiet; ``>= 2`` enables
                ``process_packets`` / flush progress lines).
            **kwargs (Any): Arguments forwarded to read_raw_frames_iter
                (e.g., flush, data_timeout, ``verbose``, etc.)

        Returns:
            Tuple[np.ndarray, np.ndarray, np.ndarray]: Timestamps, raw frame data, and per-channel counts.
                                                       Return shapes:
                                                       ts: (ncap,)
                                                       data: (nchan, ncap, nsamples) if ``chan_first`` else (ncap, nchan, nsamples)
                                                       data_count: (ncap, nchan)
        """
        if config is not None:
            self.update_config(config)  # make sure the config is updated before we get the packet format
        # Pre-allocate; zero so rows not filled by the iterator (short reads) are deterministic.
        ts, data, counts = self.create_output_buffer(n_ts=ncap, chan_first=chan_first)
        ts.fill(0)
        data.fill(0)
        counts.fill(0)

        n_filled = 0
        for i, (ts_, data_, data_count_) in enumerate(self.read_raw_frames_iter(ncap=ncap, verbose=verbose, **kwargs)):
            ts[i] = ts_
            counts[i] = data_count_
            if chan_first:
                data[:, i] = data_
            else:
                data[i] = data_
            n_filled = i + 1
        if n_filled != ncap:
            warnings.warn(
                f"read_raw_frames: received {n_filled} burst(s), expected {ncap}; "
                "remaining rows are zero-filled (timeouts, no packets passing filters, or slow source).",
                UserWarning,
                stacklevel=2,
            )
        return ts, data, counts
