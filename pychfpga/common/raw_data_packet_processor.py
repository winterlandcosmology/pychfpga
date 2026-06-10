import math
import re
import warnings
from warnings import warn as _warn

import numpy as np

from .packet_processor_base import PacketProcessorBase
from .state import State


PACKET_COOKIE_ADC = 0xA0
PACKET_COOKIE_FFT = 0xA1
PACKET_COOKIE_MASK = 0xFE
RAW_SOURCE_EMPTY = 255
NUM_SOURCES = 2


def decode_format_string(format):
    """Decode a raw capture format string into formatting parameters.

    Format string: ``<width>`` [``+<width>``] [``<modifiers>``]

    Bit widths (``<width>``): ``8``, ``14``, ``16``, ``32``, ``64``.

    Modifiers (``<modifiers>``): ``d`` — deinterleave.

    Args:
        format: Format string such as ``"16"``, ``"32+32"``, or ``"32+32d"``.

    Returns:
        dict with keys ``is_complex``, ``bit_width``, ``deinterleave``, ``split``
        (split is not encoded in the string; caller must set if needed),
        ``output_dtype``, ``scalar_bytes``, ``integer_dtype``, and ``modifiers``.

    Raises:
        ValueError: If the format string is invalid.
    """
    if not (m := re.fullmatch(r'(\d+)(?:\+(\d+))?(.*)', format)):
        raise ValueError(
            f"Invalid raw data format {format!r}: must be in the format <width>[+<width>][<modifiers>]"
        )
    base = int(m.group(1))
    second = m.group(2)
    suffix = m.group(3) or ""
    if base not in (8, 14, 16, 32, 64):
        raise ValueError(
            f"Invalid bit width in data format {format!r}: must be 8, 14, 16, 32, or 64"
        )
    is_complex = second is not None
    if is_complex:
        second = int(second)
        if base != second:
            raise ValueError(
                f"Invalid raw data format {format!r}: second width must be the same as the base width"
            )
    if not is_complex and suffix:
        raise ValueError(f"Modifiers {suffix!r} are not supported for non-complex data")

    scalar_bytes = math.ceil(base / 8)
    out_dtype = np.dtype(f"i{scalar_bytes}")
    if is_complex:
        out_dtype = np.dtype(np.complex128 if base == 64 else np.complex64)

    return dict(
        is_complex=is_complex,
        bit_width=base,
        deinterleave="d" in suffix,
        split=False,
        output_dtype=out_dtype,
        scalar_bytes=scalar_bytes,
        integer_dtype=np.dtype(f">i{scalar_bytes}"),
        modifiers=suffix)




    # def parse_format(self):
    #     """Decode the format string and set formatting parameters for buffers and transforms.

    #     Format string: ``<width>`` [``+<width>``] [``<modifiers>``]

    #     Bit widths (``<width>``): ``8``, ``14``, ``16``, ``32``, ``64``.

    #     Modifiers (``<modifiers>``): ``d`` — deinterleave.

    #     Returns:
    #         None
    #     """
    #     if not (m := re.fullmatch(r'(\d+)(?:\+(\d+))?(.*)', self.format)):
    #         raise ValueError(f"Invalid raw data format {self.format!r}: must be in the format <width>[+<width>][<modifiers>]")
    #     base, second, suffix = m.groups()
    #     base = int(m.group(1))
    #     if base not in (8, 14, 16, 32, 64):
    #         raise ValueError(f"Invalid bit width in data format {self.format!r}: must be 8, 14, 16, 32, or 64")
    #     self.fmt_is_complex = False
    #     if second is not None:
    #         second = int(m.group(2))
    #         self.fmt_is_complex = True
    #         if base != second:
    #             raise ValueError(f"Invalid raw data format {self.format!r}: second width must be the same as the base width")
    #     self.fmt_modifiers = m.group(3) or ""

    #     self.fmt_base_width = base
    #     self.fmt_scalar_bytes = math.ceil(base / 8)
    #     self.fmt_integer_dtype = np.dtype(f">i{self.fmt_scalar_bytes}")  # integer number format

class RawDataPacketProcessor(PacketProcessorBase):
    """
    Fast packet-to-linear-buffer processor for raw UDP FPGA captures.

    Packet payloads are written to an internal timestamp-indexed linear buffer.
    Complete timestamp rows are formatted on demand via ``get_formatted_data`` as
    direct views when the output format allows it, or into reusable per-source
    formatted buffers when a transform is required.

    Notes
    -----
    The decode path is intentionally copy-lean:
    - incoming packet bytes are scattered into ``raw_data`` in one vectorized write,
    - simple output formats return views into ``raw_data``,
    - transformed output is written into preallocated ``formatted_data``,
    - formatting relies on NumPy views/reshape/slicing/bit operations.
    """

    PACKET_COOKIE = 0xA0
    PACKET_COOKIE_MASK = 0xFE

    def __init__(
        self,
        packet_receiver,
        config=None,
        receive_window=3,
        verbose=0,
        decode_strict=True,
    ):
        """
        Build a UDP packet processor including packet reception and timestamp-indexed assembly pipeline.

        Parameters
        ----------
        packet_receiver: UDP packet source holding the shared packet buffer.
        config: Flat capture configuration (geometry, streams, ``capture_format``, etc.).
            Deep-copied into an internal State so the caller can retain and mutate their copy.
        receive_window: Maximum incomplete trailing timestamp rows retained as carryover.
        verbose: Verbosity level for debug prints.
        decode_strict: Strictness control for multi-packet logical decode.
        """
        self.decode_strict = decode_strict
        self.config = State()
        self.packet_cookie_mask = PACKET_COOKIE_MASK
        self.source_target_mode = np.zeros((NUM_SOURCES,), dtype=np.uint8)
        self._capture_format = [None, None]
        self._fmt_scratch = [{}, {}]
        super().__init__(packet_receiver, receive_window, verbose)
        if config is not None:
            self.update_config(State(config).deepcopy())

    def init(self):
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
        # self.pkt_source = np.empty((n_packets,), dtype=np.uint8)

        self.pkt_six = np.empty((n_packets,), dtype=np.uint16)
        self.pkt_off = np.empty((n_packets,), dtype=np.uint32)

        # Instialize empty buffers. These will be resized when the configuration is updated below.
        self.raw_data_buf = np.empty(0, dtype=np.uint8)
        self.raw_data_count_buf = np.empty(0, dtype=np.int64)
        self.raw_source_buf = np.empty(0, dtype=np.uint8)
        self.formatted_data_bufs = [np.empty(0, dtype=np.uint8), np.empty(0, dtype=np.uint8)]
        self.fmt_cdata_bufs1 = [np.empty(0, dtype=np.uint8), np.empty(0, dtype=np.uint8)]
        self.fmt_cdata_bufs2 = [np.empty(0, dtype=np.uint8), np.empty(0, dtype=np.uint8)]
        self.fmt_cdata1 = [None, None]
        self.fmt_cdata2 = [None, None]

    def update_config(self, config=None):
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
            self.FRAMES_PER_BURST = state.frames_per_burst
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

            self.NCHAN = len(self.stream_ids)
            # Map of stream IDs to stream index
            self.six_map = np.full((1 << 12,), -1, dtype=np.int64)
            self.six_map[np.asarray(self.stream_ids, dtype=np.int64)] = np.arange(self.NCHAN)

            # Timestamp-indexed assembled data buffer. Rows are compacted between calls.
            self.TS_EMPTY = np.uint64(0xFFFFFFFFFFFFFFFF)

            self.packets_per_timestamp = self.FRAMES_PER_BURST * self.NUMBER_OF_SUBFRAMES * self.NCHAN
            self.max_timestamps = math.ceil(self.pr.n_packets / self.packets_per_timestamp) + self.receive_window  # number of timestamps to allocate, including the receive window
            self.ts_array = np.full(self.max_timestamps, self.TS_EMPTY, dtype=np.uint64)
            self.n_ts = 0
            self._emit_stop = 0

            bytes_per_burst = self.FRAMES_PER_BURST * self.BYTES_PER_FRAME
            self.raw_data = self.resize_buffer(
                'raw_data_buf',
                (self.max_timestamps, self.NCHAN, bytes_per_burst),
                element_dtype=np.uint8,
            )
            self.raw_data_count = self.resize_buffer(
                'raw_data_count_buf',
                (self.max_timestamps, self.NCHAN, self.FRAMES_PER_BURST),
                element_dtype=np.int64,
            )
            self.raw_source = self.resize_buffer(
                'raw_source_buf',
                (self.max_timestamps, self.NCHAN, self.FRAMES_PER_BURST),
                element_dtype=np.uint8,
            )
            self.raw_source.fill(RAW_SOURCE_EMPTY)


            # No carryover
            self.carry_len = 0
            self.carry_tix = 0
            self._last_emitted_ts = 0
            self._ready_n_out = 0

            self.expected_packets = self.FRAMES_PER_BURST * self.NUMBER_OF_SUBFRAMES

            state.ack(static_state_attrs, context=static_state_attrs)

        dynamic_state_attrs = (
            "capture_format0",
            "capture_format1",
        )

        for source, cf in enumerate(state.capture_format):
            if cf.has_changed(context=dynamic_state_attrs[source]):
                if cf.fmt_is_complex:
                    bytes_per_integer = math.ceil(cf.fmt_base_width / 8)
                    out_dtype = np.dtype(np.complex128 if cf.fmt_base_width == 64 else np.complex64)
                    complex_values_per_frame = self.BYTES_PER_FRAME // bytes_per_integer // 2
                    interleave_factor = cf.fmt_interleave_factor
                    # interleaved_values_per_frame = complex_values_per_frame * interleaved_factor # 2 for interleaved, 1 for non-interleaved
                    cdata1_dims = (self.max_timestamps * self.FRAMES_PER_BURST, self.NCHAN, complex_values_per_frame) # complex value storage buffer
                    cdata2_dims = (self.max_timestamps * self.FRAMES_PER_BURST // interleave_factor, self.NCHAN, complex_values_per_frame * interleave_factor) # deinterleaved complex value storage buffer  
                    self.fmt_cdata1[source] = self.resize_buffer("fmt_cdata_buf1", cdata1_dims, element_dtype=out_dtype)
                    self.fmt_cdata2[source] = self.resize_buffer("fmt_cdata_buf2", cdata2_dims, element_dtype=out_dtype)
            cf.ack(context=dynamic_state_attrs[source])





    def format_data(self, n_out, source=0, split=False):
        """Transform the first `n_out` rows of assembled data from source `source` into ``(ts, data, counts)`` views with the format associated with that source in the config. Bursts of frames can be split into individual frames if `split` is True.

        Parameters
        ----------
        n_out: int - the number of rows to format
        source: int - the source index
        split: bool - if True, split each burst into ``frames_per_burst`` rows with
            consecutive timestamps and per-frame packet counts

        Returns:
            tuple: ``(ts, data, counts)`` with matching leading dimensions
        """
        raw_data = self.raw_data[:n_out]
        ts = self.ts_array[:n_out]
        counts = self.raw_data_count[:n_out]
        sources = self.raw_source[:n_out]

        cf = self.config.capture_format[source]
        base = cf.fmt_base_width
        is_complex = cf.fmt_is_complex

        # derive format parameters
        scalar_bytes = math.ceil(base / 8)
        integer_dtype = np.dtype(f">i{scalar_bytes}")
        right_shift = 8*scalar_bytes - base
        # View the buffer as N-byte integers
        data = raw_data.view(integer_dtype)
        if right_shift > 0:
            np.right_shift(data, right_shift, data)

        if is_complex:
            split = True
        if split:  # split a burst into multiple independent frames. Frames are always split for complex data.
            n_ts, nchan, nsamples_burst = data.shape
            frames_per_burst = int(self.FRAMES_PER_BURST)
            samples_per_frame = nsamples_burst // frames_per_burst
            data = data.swapaxes(-1,-2).reshape(n_ts * frames_per_burst, nchan,-1)

            frame_offsets = np.arange(frames_per_burst, dtype=ts.dtype)
            ts = (ts[:, None] + frame_offsets[None, :]).reshape(-1)
            per_frame_counts = counts // frames_per_burst
            sources = sources.swapaxes(-1,-2).reshape(n_ts * frames_per_burst, -1)
            counts = counts.swapaxes(-1,-2).reshape(n_ts * frames_per_burst, -1) * (sources==source)
        else:
            sources = (sources==source).any(axis=-1)
            counts = counts.sum(axis=-1)

        if not is_complex:
            return ts, data, counts

        # From here we have complex data, possibly with modifiers
        cdata = self.fmt_cdata1[source]
        np.copyto(cdata.real, data[:, :, ::2])
        np.copyto(cdata.imag, data[:, :, 1::2])

        interleave_factor = cf.fmt_interleave_factor
        if interleave_factor == 2:  # apply double resolution deinterleave ([[0,1,2,3], [4,5,6,7]] -> [[0 4 1 5 2 6 3 7]])
            s0, s1, s2 = cdata.shape
            v = cdata.reshape(s0, -1, interleave_factor, s2)
            data = self.fmt_cdata2[source]
            np.copyto(data[..., 0::2], v[..., 0, :])
            np.copyto(data[..., 1::2], v[..., 1, :])

        elif interleave_factor > 2:  # full resolution
            # Full resolution is dependent on the capture bus width (W) and number of bins per word (B).
            # We send W-bit Re then W-bit Im, skipping 2*B bins between them. It takes 2B frames to get the full spectrum.
            # ICE: W=32, B=2, 32+32-bit complex values, 2B= 4 frames (Max burst length = 4 frames)
            # CRS: W=128, B=4, 128+128-bit complex values, 2B= 8 frames (use mode 2 (8 frames/2 channels) or more 3 (16 frames/1 channel))
            raise NotImplementedError("Full resolution is not implemented yet")

        return ts, data, counts


    def create_output_buffer(self, n_ts, source=0, split=False):
        """Create arrays to store the timestamps, formatted data, and data counts returned by process_packets.

        This is used by applications that need to crate a storage space for the formatted data returned by the process_packets method.
        Arrays are allocated with ``numpy.empty``; callers that rely on defined values for
        unfilled rows (e.g. partial ``read_raw_frames``) should zero them before use.

        Parameters:
            n_ts (int): Number of timestamps to create the output buffer for.
            source (int): The source index
            split (bool): If True, split each burst into ``frames_per_burst`` rows with
                consecutive timestamps and per-frame packet counts

        Returns:
            Tuple of ``ts`` ``(n_ts,)``, ``data``, and ``counts`` ``(n_ts, nchan)``.
        """
        _, formatted_row, _ = self.format_data(n_out=1, source=source, split=split)
        _, nchan, nvalues = formatted_row.shape
        data = np.empty((n_ts, nchan, nvalues), dtype=formatted_row.dtype)
        counts = np.empty((n_ts, nchan), dtype=self.raw_data_count.dtype)
        ts = np.empty((n_ts,), dtype=self.ts_array.dtype)
        return ts, data, counts

    # def process_packets(
    #     self,
    #     config: Optional[Union[Dict[str, Any], State]] = None,
    #     verbose: Optional[int] = None,
    # ) -> Optional[Tuple[np.ndarray, Any, np.ndarray]]:
    #     """Process one active UDP packet block.

    #     Parameters
    #     ----------
    #     config : dict or State: The configuration to use for the processor.

    #     verbose : int: The verbosity level to use for the processor.

    #     Returns
    #     -------
    #     tuple or None
    #         ``(ts, data, data_count)`` views for complete timestamp rows in this batch,
    #         or ``None`` when there is nothing to process.


    #     Notes:

    #     The packet processing flow is as follows:

    #     - Compute the UDP buffer index (bix) for packets that match the followinf conditions:
    #         - The packet header matches the cookie and packet size
    #         - Target mode matches
    #         - Timestamp is not too old
    #         - Stream ID is valid
    #         - Frame number and subframe number are within valid range
    #         - Subframe number is within valid range
    #         - Frame number is within valid range
    #         - Subframe number is within valid range
    #     - Run a loop for each unique timestamp in the buffer for valid packets to:
    #         - Find the ringbuffer slot for the timestamp
    #         - If the slot is not found, allocate a new slot
    #         - Write the packet data to the ringbuffer slot
    #         - Update the raw data count and raw data complete arrays
    #         - If the slot is full, copy the slot to the output buffer
    #         - If the output buffer is full, raise an error
    #         - If the output buffer is not full, copy the slot to the output buffer
    #         - If the output buffer is full, raise an error

    #     """
    #     return super().process_packets(config, verbose)


    def filter_packets(self, n):
        """Decode UCAP headers and return UDP row indices for packets to integrate."""
        # Select the packets to process by creating a boolean mask
        bix_mask = self.bix_mask[:n]
        temp_mask = self.tmp_mask[:n]

        # Filter out packets with the wrong cookie or packet size
        np.bitwise_and(self.buf_cookie[:n], self.PACKET_COOKIE_MASK, out=self.pkt_cookie[:n])
        np.equal(self.pkt_cookie[:n], self.PACKET_COOKIE, out=bix_mask)
        np.equal(self.pr.pkt_len[:n], self.PACKET_SIZE, out=temp_mask[:n], where=bix_mask)
        np.bitwise_and(bix_mask, temp_mask[:n], out=bix_mask)

        if self.verbose >= 2:
            n_packets = bix_mask.sum()
            if n_packets or self.verbose >= 3:
                print(f"process_packets: found {n_packets} packets matching the cookie ({self.packet_cookie_mask}) and packet size")
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
        np.less(self.pkt_frame[:n], self.FRAMES_PER_BURST, out=temp_mask, where=bix_mask)
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
        if self.verbose:
            rejected_packets = int(np.sum(bix_mask[:n] & (self.pkt_ts[:n] <= self._last_emitted_ts)))
        np.greater(self.pkt_ts[:n], self._last_emitted_ts, out=temp_mask, where=bix_mask)
        np.bitwise_and(bix_mask, temp_mask, out=bix_mask)

        if self.verbose and rejected_packets:
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
            if self.verbose >= 2:
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
            self.raw_source[dst_tix] = self.raw_source[src_tix]
            tix_to_clear[dst_tix] = False  # don't clear the carryover rows

        n = tix_to_clear.size
        self.raw_data_count[:n][tix_to_clear] = 0
        self.raw_data[:n][tix_to_clear] = 0
        self.raw_source[:n][tix_to_clear] = RAW_SOURCE_EMPTY

    def store_packets(self, bix, tix):
        """Scatter subframe bytes into ``raw_data`` and update per-channel packet counts."""
        # Now that we have all the info we need to copy data from the UDP buffer and reassemble it into the output buffer.
        # This is done in a very efficient single vectorized operation.
        self.raw_data[
            tix[:, None],
            self.pkt_six[bix, None],
            self.pkt_off[bix, None] + np.arange(self.BYTES_PER_SUBFRAME, dtype=self.pkt_off.dtype),
        ] = self.buf_data[bix, :self.BYTES_PER_SUBFRAME] # copy the payload bytes from the UDP buffer to the output buffer

        self.raw_source[tix, self.pkt_six[bix], self.pkt_frame[bix]] = self.buf_cookie[bix] & 1

        # Update the raw data count and raw data complete arrays
        np.add.at(self.raw_data_count, (tix, self.pkt_six[bix], self.pkt_frame[bix]), 1)

    def get_formatted_data(self, split=False):
        split = bool(split)
        """Return ``(ts, formatted_data, counts, sources)`` views for complete rows."""
        if self.verbose >= 3:
            print(f"RawDataPacketProcessor: process_packets: returning {self.n_out} timestamps")
        n_out = self.n_out
        ts0, data0, counts0 = self.format_data(n_out=n_out, source=0, split=split)
        ts1, data1, counts1 = self.format_data(n_out=n_out, source=1, split=split)
        return (ts0, data0, counts0), (ts1, data1, counts1)


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
        self.raw_source.fill(RAW_SOURCE_EMPTY)
        self.n_ts = 0
        self.carry_len = 0
        self._emit_stop = 0
        self._last_emitted_ts = 0
        self._ready_n_out = 0
