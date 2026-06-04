from typing import Any, Dict, Optional, Tuple, Union

import copy
import math
import re
import warnings
from warnings import warn as _warn

import numpy as np

from .packet_processor_base import PacketProcessorBase
from .udp_packet_receiver import UDPPacketReceiver
from .state import State


class RawDataPacketProcessor(PacketProcessorBase):
    """
    Fast packet-to-linear-buffer processor for raw UDP FPGA captures.

    Packet payloads are written to an internal timestamp-indexed linear buffer.
    Complete timestamp rows are returned as direct views when the output format
    allows it, or copied into a reusable formatted buffer when a transform is
    required.

    Notes
    -----
    The decode path is intentionally copy-lean:
    - incoming packet bytes are scattered into ``raw_data`` in one vectorized write,
    - simple output formats return views into ``raw_data``,
    - transformed output is written into preallocated ``formatted_data``,
    - formatting relies on NumPy views/reshape/slicing/bit operations.
    """

    PACKET_COOKIE = 0xA0
    PACKET_COOKIE_MASK = 0xFF


    def __init__(
        self,
        packet_receiver: UDPPacketReceiver,
        config: Union[Dict[str, Any], State],
        receive_window: int = 3,
        verbose: int = 0,
        decode_strict: bool = True,
    ) -> None:
        """
        Build a UDP packet processor including packet reception and timestamp-indexed assembly pipeline.

        Parameters
        ----------
        packet_receiver (UDPPacketReceiver): UDP packet source holding the shared packet buffer.
        config (State or dict): Flat capture configuration (what callers typically take from
            ``raw_capture`` / decoder state: geometry, streams, ``format``, ``split``, etc.).
            The mapping is deep-copied into an internal :class:`State` so the caller can retain
            and mutate their copy without affecting this processor.
        receive_window (int): Maximum number of incomplete trailing timestamp rows retained as
            carryover between UDP packet blocks. This is used to handle the case where the last timestamps could not be completed
            with the current UDP packet blocks, and should be carried over to the next processing block so they can be completed 
            with the next batch of UDP packets.
            in a block is incomplete, and the next block starts with a timestamp that is less than the
            last packet in the previous block.
        verbose (int): Verbosity level for debug prints.
        decode_strict (bool): Strictness control for multi-packet logical decode.
            - ``True``: invalid profile/layout and incomplete logical groups
              raise explicit errors.
            - ``False``: tolerate some layout mismatches by falling back to coarser framing where possible.
        """
        self.decode_strict = decode_strict
        self.config = State()
        self.packet_cookie = self.PACKET_COOKIE
        self.packet_cookie_mask = self.PACKET_COOKIE_MASK
        super().__init__(packet_receiver, receive_window, verbose)
        self.update_config(State(config).deepcopy())

    def init(self) -> None:
        self.header_dtype = np.dtype(
            dict(
                names=["cookie", "stream_id", "frame_number", "slot_chan", "mode_subframe", "ts"],
                offsets=[0, 1, 1, 2, 3, 2],
                formats=["u1", ">u2", "u1", "u1", "u1", ">u8"],
            )
        )
        max_packet_size = self.pr.max_packet_size

        self.packet_dtype = np.dtype(
            [
                ("header", self.header_dtype, (1,)),
                ("data", np.uint8, (max_packet_size - self.header_dtype.itemsize,)), # everything after the header can be the payload
            ]
        )

        # Views into the USB packet buffer. The dimension of the UDB buffer is fixed, so these views do not change with the processor configuration.
        self.buf_struct = self.pr.buf[:, :].view(self.packet_dtype)[:, 0]
        self.buf_cookie = self.buf_struct["header"]["cookie"][:, 0]
        self.buf_frame_stream_id = self.buf_struct["header"]["stream_id"][:, 0]
        self.buf_frame_number = self.buf_struct["header"]["frame_number"][:, 0]
        self.buf_slot_chan = self.buf_struct["header"]["slot_chan"][:, 0]
        self.buf_mode_subframe = self.buf_struct["header"]["mode_subframe"][:, 0]
        self.buf_ts = self.buf_struct["header"]["ts"][:, 0]
        self.buf_data = self.buf_struct["data"]
        self.ts_mask = np.uint64(0xFFFFFFFFFFFF)
        self.sid_mask = 0x0FFF

        # Buffers for processed packets and packet metadata
        n_packets = self.pr.n_packets
        self.pkt_stream_id = np.empty((n_packets,), dtype=np.uint16)
        self.pkt_mode = np.empty((n_packets,), dtype=np.uint8)
        self.pkt_cookie = np.empty((n_packets,), dtype=np.uint8)
        self.pkt_frame = np.empty((n_packets,), dtype=np.uint32) # same as pkt_off  into the data buffer for each packet
        self.pkt_subframe = np.empty((n_packets,), dtype=np.uint32) # must be wide enough to compute the offset into the data buffer for each packet
        self.pkt_six = np.empty((n_packets,), dtype=np.uint16)
        self.pkt_off = np.empty((n_packets,), dtype=np.uint32)

        # Instialize empty buffers. These will be resized when the configuration is updated below.
        self.raw_data_buf = np.empty(0, dtype=np.uint8)
        self.raw_data_count_buf = np.empty(0, dtype=np.int64)
        self.fmt_cdata_buf1 = np.empty(0, dtype=np.uint8)
        self.fmt_cdata_buf2 = np.empty(0, dtype=np.uint8)



        # self.configure_output(format=format)

        # self.last_decode_meta: Dict[str, Any] = {}
        # self._allocate_block_output()

    def update_config(self, config=None) -> None:
        if config is not None:
            self.config.update(config)

        state = self.config

        # List if state attributes that do not change during normal operation and will require reallocation of internal buffers
        # The tuple is used both as a list of attributes and the context token

        static_state_attrs = (
                "capture_engine",
                "bytes_per_sample",
                "samples_per_frame",
                "frames_per_burst",
                "fft_bins_per_word",
                "subframes_per_frame",
                "stream_ids",
                "packets_per_logical_frame",
                "logical_frames_per_burst",
                "packet_header_format",
        )

        # Recompute output buffer arrays if the packet geometry parameter has changed 
        if state.has_changed(static_state_attrs, context=static_state_attrs):  
            if self.verbose:
                print(f"update_config: static state attributes have changed. Buffers will be cleared.")

            self.CAPTURE_ENGINE = state.capture_engine
            self.TARGET_MODE = state.target_mode 
            self.BYTES_PER_SAMPLE = state.bytes_per_sample
            self.SAMPLES_PER_FRAME = state.samples_per_frame
            self.PKT_FRAMES_PER_BURST = state.frames_per_burst
            self.NUMBER_OF_SUBFRAMES = state.subframes_per_frame or state.number_of_subframes or 1
            self.stream_ids = state.stream_ids

            if len(set(self.stream_ids)) != len(self.stream_ids):
                raise ValueError("stream_ids must be unique ")
            if max(self.stream_ids) >= 1 << 12:
                raise ValueError("stream_ids must be less than 1 << 12")

            if state.get("bytes_per_frame") is not None:
                self.BYTES_PER_FRAME = int(state.bytes_per_frame)
            else:
                self.BYTES_PER_FRAME = self.BYTES_PER_SAMPLE * self.SAMPLES_PER_FRAME
            if state.get("bytes_per_subframe") is not None:
                self.BYTES_PER_SUBFRAME = int(state.bytes_per_subframe)
            else:
                self.BYTES_PER_SUBFRAME = self.BYTES_PER_FRAME // max(1, self.NUMBER_OF_SUBFRAMES)
            self.PACKET_SIZE = self.header_dtype.itemsize + self.BYTES_PER_SUBFRAME

            self.CAPTURE_SOURCE = state.source
            self.FFT_BINS_PER_WORD = state.fft_bins_per_word or 1

            # self.PACKETS_PER_LOGICAL_FRAME = state.packets_per_logical_frame or 1
            # if self.PACKETS_PER_LOGICAL_FRAME < 1:
            #     raise ValueError("packets_per_logical_frame must be >= 1")

            # if self.PKT_FRAMES_PER_BURST % self.PACKETS_PER_LOGICAL_FRAME != 0:
            #     if self.decode_strict:
            #         raise ValueError(
            #             "pkt_frames_per_burst is not a multiple of packets_per_logical_frame "
            #             f"({self.PKT_FRAMES_PER_BURST} vs {self.PACKETS_PER_LOGICAL_FRAME})."
            #         )

            # self.LOGICAL_FRAMES_PER_BURST = state.logical_frames_per_burst
            # if self.LOGICAL_FRAMES_PER_BURST <= 0:
            #     self.LOGICAL_FRAMES_PER_BURST = max(
            #         1,
            #         self.PKT_FRAMES_PER_BURST // self.PACKETS_PER_LOGICAL_FRAME,
            #     )

            self.NCHAN = len(self.stream_ids)
            # Map of stream IDs to stream index
            self.six_map = np.full((1 << 12,), -1, dtype=np.int64)
            self.six_map[np.asarray(self.stream_ids, dtype=np.int64)] = np.arange(self.NCHAN)

            # Timestamp-indexed assembled data buffer. Rows are compacted between calls.
            self.TS_EMPTY = np.uint64(0xFFFFFFFFFFFFFFFF)

            self.packets_per_timestamp = self.PKT_FRAMES_PER_BURST * self.NUMBER_OF_SUBFRAMES * self.NCHAN
            self.max_timestamps = math.ceil(self.pr.n_packets / self.packets_per_timestamp) + self.receive_window  # number of timestamps to allocate, including the receive window
            self.ts_array = np.full(self.max_timestamps, self.TS_EMPTY, dtype=np.uint64)
            self.n_ts = 0
            self._emit_stop = 0


            self.raw_data = self.resize_buffer('raw_data_buf', (self.max_timestamps, self.NCHAN, self.PKT_FRAMES_PER_BURST * self.BYTES_PER_FRAME), element_dtype=np.uint8)
            self.raw_data_count = self.resize_buffer('raw_data_count_buf', (self.max_timestamps, self.NCHAN), element_dtype=np.int64)

            # No carryover
            self.carry_len = 0
            self.carry_tix = 0
            self._last_emitted_ts = 0 

            self.expected_packets = self.PKT_FRAMES_PER_BURST * self.NUMBER_OF_SUBFRAMES
    
            state.ack(static_state_attrs, context=static_state_attrs)

        dynamic_state_attrs = (
            "target_mode",
            "source",
            "format",
            "split",
        )

        if state.get("packet_cookie") is not None:
            self.packet_cookie = int(state.packet_cookie)
        if state.get("packet_cookie_mask") is not None:
            self.packet_cookie_mask = int(state.packet_cookie_mask)

        if state.has_changed(dynamic_state_attrs, context=dynamic_state_attrs):
            self.format = state.format
            self.split = bool(state.split)
            self.parse_format()
            if self.fmt_is_complex:
                self.fmt_cdata1 = self.resize_buffer("fmt_cdata_buf1", self.fmt_cdata1_dims, element_dtype=self.fmt_out_dtype)
                self.fmt_cdata2 = self.resize_buffer("fmt_cdata_buf2", self.fmt_cdata2_dims, element_dtype=self.fmt_out_dtype)

            state.ack(dynamic_state_attrs, context=dynamic_state_attrs)


    def parse_format(self):
        """Decode the format string and returns the associated formatting parameters that can be used to create formatted buffers and transform the data.
        
        Format: <width>[+<width>][<modifiers>]

        <width>: 8, 14, 16, 32, 64
        <modifiers>: 
            d = deinterleave

        Returns: None
        """
        if not (m := re.fullmatch(r'(\d+)(?:\+(\d+))?(.*)', self.format)):
            raise ValueError(f"Invalid raw data format {self.format!r}: must be in the format <width>[+<width>][<modifiers>]")
        base, second, suffix = m.groups()
        base = int(m.group(1))
        if base not in (8, 14, 16, 32, 64):
            raise ValueError(f"Invalid bit width in data format {self.format!r}: must be 8, 14, 16, 32, or 64")
        self.fmt_is_complex = False
        if second is not None:
            second = int(m.group(2))
            self.fmt_is_complex = True
            if base != second:
                raise ValueError(f"Invalid raw data format {self.format!r}: second width must be the same as the base width")
        self.fmt_modifiers = m.group(3) or ""

        self.fmt_base_width = base
        self.fmt_scalar_bytes = math.ceil(base / 8)
        self.fmt_integer_dtype = np.dtype(f">i{self.fmt_scalar_bytes}")  # integer number format
        self.fmt_out_dtype = np.dtype(f"i{self.fmt_scalar_bytes}")        
        if self.fmt_is_complex:
            self.fmt_out_dtype = np.dtype(np.complex128 if self.fmt_base_width == 64 else np.complex64)
            complex_values_per_frame = self.BYTES_PER_FRAME // self.fmt_scalar_bytes // 2
            interleaved_factor = 2 if 'd' in self.fmt_modifiers else 1
            interleaved_values_per_frame = complex_values_per_frame * interleaved_factor # 2 for interleaved, 1 for non-interleaved
            self.fmt_cdata1_dims = (self.max_timestamps * self.PKT_FRAMES_PER_BURST, self.NCHAN, complex_values_per_frame) # complex value storage buffer
            self.fmt_cdata2_dims = (self.max_timestamps * self.PKT_FRAMES_PER_BURST // interleaved_factor, self.NCHAN, complex_values_per_frame * interleaved_factor) # deinterleaved complex value storage buffer  

        if not self.fmt_is_complex and self.fmt_modifiers:
            raise ValueError(f"Modifiers {self.fmt_modifiers!r} are not supported for non-complex data")
        if self.fmt_is_complex and not self.split:
            raise ValueError("Complex format can only be used with split=True")

    def format_data(self, raw_data) -> np.ndarray:
        """Transform the raw data in the output buffer into the required format.

        Returns:
            data: np.ndarray - the formatted data
        """

        base = self.fmt_base_width
        is_complex = self.fmt_is_complex
        modifiers = self.fmt_modifiers
        right_shift = 8*self.fmt_scalar_bytes - base
        split = self.split

        # View the buffer as N-byte integers
        data = raw_data.view(self.fmt_integer_dtype)
        if right_shift > 0:
            np.right_shift(data, right_shift, data)

        if split:  # split a busrt into multiple independent frames
            nchan, nframes, nsamples = data.shape
            data = data.reshape((nchan, nframes * self.PKT_FRAMES_PER_BURST, -1))

        if not is_complex:
            return data

        # From here we have complex data, possibly with modifiers
        cdata = self.fmt_cdata1
        np.copyto(cdata.real, data[:, :, ::2]) 
        np.copyto(cdata.imag, data[:, :, 1::2])
 
        if 'd' in modifiers:  # apply double resolution deinterleave ([[0,1,2,3], [4,5,6,7]] -> [[0 4 1 5 2 6 3 7]])
            s0, s1, s2 = cdata.shape
            v = cdata.reshape(s0, -1, 2, s2)
            data = self.fmt_cdata2
            np.copyto(data[..., 0::2], v[..., 0, :])
            np.copyto(data[..., 1::2], v[..., 1, :])

        elif 'f' in modifiers:  # full resolution
            # Full resolution is dependent on the capture bus width (W) and number of bins per word (B). 
            # We send W-bit Re then W-bit Im, skipping 2*B bins between them. It takes 2B frames to get the full spectrum.
            # ICE: W=32, B=2, 32+32-bit complex values, 2B= 4 frames (Max burst length = 4 frames)
            # CRS: W=128, B=4, 128+128-bit complex values, 2B= 8 frames (use mode 2 (8 frames/2 channels) or more 3 (16 frames/1 channel))
            raise NotImplementedError("Full resolution is not implemented yet")
        
        return data


    def create_output_buffer(
        self, n_ts: int, chan_first: bool = False
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Create arrays to store the timestamps, formatted data, and data counts returned by process_packets.

        This is used by applications that need to crate a storage space for the formatted data returned by the process_packets method.
        Arrays are allocated with ``numpy.empty``; callers that rely on defined values for
        unfilled rows (e.g. partial ``read_raw_frames``) should zero them before use.

        Parameters:
            n_ts (int): Number of timestamps to create the output buffer for.
            chan_first (bool): If False (default), ``data`` is ``(n_ts, nchan, nvalues)``.
                If True, ``data`` is ``(nchan, n_ts, nvalues)``.

        Returns:
            Tuple of ``ts`` ``(n_ts,)``, ``data``, and ``counts`` ``(n_ts, nchan)``.
        """
        formatted_row = self.format_data(self.raw_data[0:1])  # format a single row so we can determine the shape and dtype of the output buffer
        _, nchan, nvalues = formatted_row.shape
        if chan_first:
            data = np.empty((nchan, n_ts, nvalues), dtype=formatted_row.dtype)
        else:
            data = np.empty((n_ts, nchan, nvalues), dtype=formatted_row.dtype)
        counts = np.empty((n_ts, nchan), dtype=self.raw_data_count.dtype)
        ts = np.empty((n_ts,), dtype=self.ts_array.dtype)
        return ts, data, counts

    def process_packets(
        self,
        config: Optional[Union[Dict[str, Any], State]] = None,
        verbose: Optional[int] = None,
    ) -> Optional[Tuple[np.ndarray, Any, np.ndarray]]:
        """Process one active UDP packet block.

        Parameters
        ----------
        config : dict or State: The configuration to use for the processor.

        verbose : int: The verbosity level to use for the processor.

        Returns
        -------
        tuple or None
            ``(ts, data, data_count)`` views for complete timestamp rows in this batch,
            or ``None`` when there is nothing to process.


        Notes:

        The packet processing flow is as follows:

        - Compute the UDP buffer index (bix) for packets that match the followinf conditions:
            - The packet header matches the cookie and packet size
            - Target mode matches
            - Timestamp is not too old
            - Stream ID is valid
            - Frame number and subframe number are within valid range
            - Subframe number is within valid range
            - Frame number is within valid range
            - Subframe number is within valid range
        - Run a loop for each unique timestamp in the buffer for valid packets to:
            - Find the ringbuffer slot for the timestamp
            - If the slot is not found, allocate a new slot
            - Write the packet data to the ringbuffer slot
            - Update the raw data count and raw data complete arrays
            - If the slot is full, copy the slot to the output buffer
            - If the output buffer is full, raise an error
            - If the output buffer is not full, copy the slot to the output buffer
            - If the output buffer is full, raise an error

        """
        return super().process_packets(config, verbose)

    def on_ts_overflow(self, dropped: int) -> None:
        """Warn when timestamp keys are dropped because the buffer is full."""
        _warn(
            f"timestamp backlog: {dropped} oldest timestamps dropped.",
            UserWarning,
            stacklevel=3,
        )

    def filter_packets(self, n: int) -> Optional[np.ndarray]:
        """Decode UCAP headers and return UDP row indices for packets to integrate."""
        # Select the packets to process by creating a boolean mask
        bix_mask = self.bix_mask[:n]
        temp_mask = self.tmp_mask[:n]

        # Filter out packets with the wrong cookie or packet size
        np.bitwise_and(self.buf_cookie[:n], self.packet_cookie_mask, out=self.pkt_cookie[:n])
        np.equal(self.pkt_cookie[:n], self.packet_cookie, out=bix_mask)
        np.equal(self.pr.pkt_len[:n], self.PACKET_SIZE, out=temp_mask[:n], where=bix_mask)
        np.bitwise_and(bix_mask, temp_mask[:n], out=bix_mask)

        if self.verbose >= 2:
            n_packets = bix_mask.sum()
            if n_packets or self.verbose >= 3:
                print(f"process_packets: found {n_packets} packets matching the cookie and packet size")
        # bix_mask[:n] = (self.buf_cookie[:n] & self.PACKET_COOKIE_MASK) == self.PACKET_COOKIE)
        #     & (pkt_len[:n] == self.PACKET_SIZE)
        # )

        # Process packet fields (slice UDP buffer views to active prefix ``n``)
        np.bitwise_and(self.buf_ts[:n], self.ts_mask, out=self.pkt_ts[:n], where=bix_mask)
        np.bitwise_and(self.buf_frame_stream_id[:n], self.sid_mask, out=self.pkt_stream_id[:n]) # removed mask to make sure take works
        np.right_shift(self.buf_frame_number[:n], 4, out=self.pkt_frame[:n], where=bix_mask)
        np.bitwise_and(self.pkt_frame[:n], 0x0F, out=self.pkt_frame[:n], where=bix_mask)
        np.right_shift(self.buf_mode_subframe[:n], 2, out=self.pkt_mode[:n], where=bix_mask)
        np.bitwise_and(self.pkt_mode[:n], 0x3, out=self.pkt_mode[:n], where=bix_mask)
        np.bitwise_and(self.buf_mode_subframe[:n], 0x3, out=self.pkt_subframe[:n], where=bix_mask)

        # Compute the stream index for each packet
        np.take(self.six_map, self.pkt_stream_id[:n], out=self.pkt_six[:n]) # vectorized lookup of the stream index for each packet

        # Filter out packets with an invalid frame
        np.less(self.pkt_frame[:n], self.PKT_FRAMES_PER_BURST, out=temp_mask, where=bix_mask)
        np.bitwise_and(bix_mask, temp_mask, out=bix_mask)
        # Filter out packets with an invalid subframe number
        np.less(self.pkt_subframe[:n], self.NUMBER_OF_SUBFRAMES, out=temp_mask, where=bix_mask)
        np.bitwise_and(bix_mask, temp_mask, out=bix_mask)
        # Drop UDP packets with an invalid stream index
        np.greater_equal(self.pkt_six[:n].view(np.int16), 0, out=temp_mask, where=bix_mask)
        np.bitwise_and(bix_mask, temp_mask, out=bix_mask)
        # Drop UDP packets with the wrong mode
        np.equal(self.pkt_mode[:n], self.TARGET_MODE, out=temp_mask, where=bix_mask)
        np.bitwise_and(bix_mask, temp_mask, out=bix_mask)
        # Drop UDP packets at or before the latest timestamp already returned (monotonic emits).
        np.greater(self.pkt_ts[:n], self._last_emitted_ts, out=temp_mask, where=bix_mask)
        np.bitwise_and(bix_mask, temp_mask, out=bix_mask)

        if self.verbose:
            rejected_packets = np.sum(self.pkt_ts[:n][bix_mask] <= self._last_emitted_ts)
            print(f"RawDataPacketProcessor: process_packets: rejected {rejected_packets} packets at or before the latest timestamp already returned (monotonic emits). {self._last_emitted_ts=} {self.pkt_ts[:n].min() =}.")

        # Byte offset: reuse pkt_frame / pkt_subframe (already decoded above) — no temp arrays.
        # Only ``pkt_off[bix]`` is read later; entries where ``bix_mask`` is false are left undefined.
        np.multiply(self.pkt_frame[:n], self.BYTES_PER_FRAME, out=self.pkt_off[:n], where=bix_mask)
        np.multiply(self.pkt_subframe[:n], self.BYTES_PER_SUBFRAME, out=self.pkt_subframe[:n], where=bix_mask)
        np.add(self.pkt_off[:n], self.pkt_subframe[:n], out=self.pkt_off[:n], where=bix_mask)

        # Find the indices of the packets to process
        return np.flatnonzero(bix_mask)

    def init_dest_rows(self, n_ts, cix, carry_tix):
        """Relocate carryover rows to their new rows and zero the rest.
        
        Parameters
        ----------
        n_ts (int): Number of timestamps in the new output buffer.
        cix (slice): Indices of the carryover rows in the old output buffer.
        carry_tix (np.ndarray): Indices of the carryover rows in the new output buffer.

        """
        tix_to_clear = np.ones(n_ts, dtype=bool)
        cix_mask = carry_tix >= 0  # find indices of carryover timestamps that were not dropped
        if cix_mask.any():
            if self.verbose:
                print(
                    f"RawPacketProcessor: preserving carryover state for {cix_mask.sum()} "
                    f"carryover timestamps {cix=}, {cix_mask=}, {carry_tix[cix_mask]=}."
                )
            src_tix = np.arange(cix.start, cix.stop)[cix_mask]
            dst_tix = carry_tix[cix_mask]
            assert np.unique(dst_tix).size == dst_tix.size, (
                "Duplicate carryover destination indices. This should not happen."
            )
            self.raw_data_count[dst_tix] = self.raw_data_count[src_tix] # Fancy indexing on the RHS cretes a copy, so no danger if source and destination overlap
            self.raw_data[dst_tix] = self.raw_data[src_tix]
            tix_to_clear[dst_tix] = False  # don't clear the carryover rows

        n = tix_to_clear.size
        self.raw_data_count[:n][tix_to_clear] = 0
        self.raw_data[:n][tix_to_clear] = 0

    def store_packets(self, bix: np.ndarray, tix: np.ndarray) -> None:
        """Scatter subframe bytes into ``raw_data`` and update per-channel packet counts."""
        # Now that we have all the info we need to copy data from the UDP buffer and reassemble it into the output buffer.
        # This is done in a very efficient single vectorized operation.
        self.raw_data[
            tix[:, None],
            self.pkt_six[bix, None],
            self.pkt_off[bix, None] + np.arange(self.BYTES_PER_SUBFRAME, dtype=self.pkt_off.dtype),
        ] = self.buf_data[bix, :self.BYTES_PER_SUBFRAME] # copy the payload bytes from the UDP buffer to the output buffer

        # Update the raw data count and raw data complete arrays
        np.add.at(self.raw_data_count, (tix, self.pkt_six[bix]), 1)

    def emit_complete_rows(
        self, n_out: int, verbose: int
    ) -> Optional[Tuple[np.ndarray, Any, np.ndarray]]:
        """Return ``(ts, formatted_data, counts)`` views for complete rows."""
        if verbose >= 1:
            print(f"RawDataPacketProcessor: process_packets: returning {n_out} timestamps")

        ts = self.ts_array[:n_out]
        data = self.format_data(self.raw_data[:n_out])
        counts = self.raw_data_count[:n_out]

        self._last_emitted_ts = ts[n_out - 1]

        return ts, data, counts


    def flush(self, flush_timeout=None):
        """Flush UDP receiver and clear assembled-buffer state."""
        flush_kwargs = {}
        if flush_timeout is not None:
            flush_kwargs["timeout"] = flush_timeout
        self.pr.flush(**flush_kwargs)
        if self.verbose >= 2:
            print("Flushing RawDataPacketProcessor linear buffer")
        self.ts_array.fill(self.TS_EMPTY)
        self.raw_data.fill(0)
        self.raw_data_count.fill(0)
        self.n_ts = 0
        self.carry_len = 0
        self._emit_stop = 0
        self._last_emitted_ts = 0
