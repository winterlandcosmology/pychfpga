from typing import Any, Optional, Tuple, Generator

import numpy as np
from numpy import unsignedinteger, ndarray
from numpy._typing import _64Bit

from .raw_data_processor import RawDataProcessor
from .udp_packet_receiver import UDPPacketReceiver

class RawFrameReceiver(RawDataProcessor):
    """Convenience wrapper around :class:`RawDataProcessor` for burst reads."""

    def __init__(self, packet_receiver: UDPPacketReceiver, platform_id: str, bytes_per_sample: int, 
                 samples_per_frame: int, target_mode: Optional[int] = None, verbose: int = 0,
                 **kwargs: Any) -> None:
        """
        Initialize the raw frame receiver.

        Parameters:
            packet_receiver (UDPPacketReceiver): UDP packet receiver providing the packet buffer.
            platform_id (str): Platform identifier used to select constants and layout.
            bytes_per_sample (int): Bytes per ADC sample.
            samples_per_frame (int): ADC samples per frame.
            target_mode (Optional[int]): Target mode override for platform-specific packet layout.
            verbose (int): Verbosity level (0 quiet, 1 basic, 2 detailed, 3 debug).
            **kwargs (Any): Additional parameters forwarded to RawDataProcessor
                (e.g., format, stream_ids, ring_buffer_size, capture_engine,
                effective_decode_profile, decode_strict, pkt_frames_per_burst).
        """
        super().__init__(
            packet_receiver,
            platform_id,
            bytes_per_sample,
            samples_per_frame,
            target_mode=target_mode,
            verbose=verbose,
            **kwargs,
        )

    def read_raw_frames_iter(self, ncap: int = 1, flush: bool = True, flush_timeout: Optional[int] = None,
                             data_timeout: Optional[int] = None, auto_receive: bool = True, **kwargs: Any) -> Generator[
        Tuple[int, ndarray, ndarray], None, None]:
        """
        Yield ``ncap`` bursts as ``(timestamp, data, data_count)`` tuples.

        Parameters:
            ncap (int): Number of data bursts to capture.
            flush (bool): If True, flush UDP buffers and internal ring state before capture.
            flush_timeout (Optional[int]): Timeout used while flushing the UDP socket.
            data_timeout (Optional[int]): Socket timeout while waiting for packets.
            auto_receive (bool): If True, automatically receive packets from ring buffer. Set this to False
                                 if you want multiple RawFrameReceiver objects to read from the same UDPPacketReceiver
                                 buffer. In that case you would need to call UDPPacketReceiver.receive_packets()
                                 manually.
            **kwargs (Any): Arguments forwarded to ``get_data_iter`` (e.g., verbose).

        Returns:
            Generator[Tuple[uint64, np.ndarray, np.ndarray]]: Yields timestamp, data, and per-channel counts.
        """

        if flush:
            self.flush(flush_timeout)

        self.pr.settimeout(data_timeout)

        if data_timeout:
            self.pr.settimeout(data_timeout)

        n_yielded = 0

        while n_yielded < ncap:
            if auto_receive:
                if self.verbose >= 3:
                    print(f"read_raw_frames_iter: internally triggering UDPPacketReceiver.receive_packets()")
                self.pr.receive_packets()
            if not self.pr.n_packets:
                continue
            for ts_, data_, data_count_ in self.get_data_iter(**kwargs):
                yield ts_, data_, data_count_
                if self.verbose >= 3:
                    print(f"read_raw_frames_iter: yielded ts = {ts_}")
                n_yielded += 1
                if self.verbose >= 3:
                    print(f"read_raw_frames_iter: increased n_yielded to {n_yielded}")
                if n_yielded >= ncap:
                    if self.verbose >= 3:
                        print(f"read_raw_frames_iter:  {n_yielded=} >= {ncap=}. Breaking the loop.")
                    return

    def read_raw_frames(self, ncap: int = 1, split_frames: bool = False, **kwargs: Any) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """
        Read a fixed number of raw frames into preallocated arrays.

        Parameters:
            ncap (int): Number of frames to capture.
            split_frames (bool): Keep logical frames split along axis ``-2``.
                                 If False, that axis is flattened into samples.
            **kwargs (Any): Arguments forwarded to read_raw_frames_iter (e.g., flush, auto_receive, verbose, etc.)

        Returns:
            Tuple[np.ndarray, np.ndarray, np.ndarray]: Timestamps, raw frame data, and per-channel counts.
                                                       Return shapes:
                                                       ts: (ncap)
                                                       data: (ncap, nchannels, nframes_per_cap, nsamples_per_frame)
                                                       data_count: (ncap, nchannels)
        """
        # Pre-Allocate memory to store processed data
        data = self._allocate_ybuf_data(ncap=ncap)
        data_view = data.view()
        frames_per_burst = getattr(self, "LOGICAL_FRAMES_PER_BURST", self.PKT_FRAMES_PER_BURST)
        ts = np.empty((self.NCHAN, ncap * frames_per_burst), dtype=self.ts_array.dtype)
        # data_count shows "number of received packets per channel per burst", it doesn't really make sense for separate frames
        data_count = np.empty(shape=(self.NCHAN, ncap), dtype=self.raw_data_count.dtype)

        for i, (ts_, data_, data_count_) in enumerate(self.read_raw_frames_iter(ncap=ncap, **kwargs)):
            lo, hi = i * frames_per_burst, (i + 1) * frames_per_burst
            ts[:, lo:hi], data_view[:, lo:hi, ...], data_count[:, i] = ts_, data_, data_count_
        if not split_frames:
            # By default yielded data has frames split
            data_view.shape = (*data_view.shape[:-2], data_view.shape[-2] * data_view.shape[-1])
        return ts, data_view, data_count
