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
    """Decode a raw capture format string into a ``State`` for ``get_formatted_data``.

    Format string: ``<width>`` [``+<width>``] [``<modifiers>``]

    Bit widths (``<width>``): ``8``, ``14``, ``16``, ``32``, ``64``.

    Modifiers (``<modifiers>``): ``d`` — deinterleave.

    Args:
        format: Format string such as ``"16"``, ``"32+32"``, or ``"32+32d"``.

    Returns:
        State with ``fmt_bus_width``, ``fmt_data_width``, ``fmt_is_complex``,
        ``fmt_interleave_factor``, and ``use_offset_encoding`` (same fields as
        ``config.capture_format[i]``).

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
    if base not in (8, 14, 16, 32, 64, 128):
        raise ValueError(
            f"Invalid bit width in data format {format!r}: must be 8, 14, 16, 32, 64, or 128"
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

    deinterleave = "d" in suffix
    bus_width = 8 * math.ceil(base / 8)
    return State(
        fmt_bus_width=bus_width,
        fmt_data_width=base,
        fmt_is_complex=is_complex,
        fmt_interleave_factor=2 if deinterleave else 1,
        use_offset_encoding=False,
    )




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

    #     self.fmt_bus_width = base
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
        config_getter,
        receive_window=3,
        verbose=0,
        decode_strict=True,
    ):
        """
        Build a UDP packet processor including packet reception and timestamp-indexed assembly pipeline.

        Parameters
        ----------
        packet_receiver: UDP packet source holding the shared packet buffer.
        config_getter: Callable returning a stable :class:`State` capture config.
        receive_window: Maximum incomplete trailing timestamp rows retained as carryover.
        verbose: Verbosity level for debug prints.
        decode_strict: Strictness control for multi-packet logical decode.
        """
        if config_getter is None:
            raise TypeError("config_getter is required")
        self._config_getter = config_getter
        self.decode_strict = decode_strict
        self.config = None
        self.packet_cookie_mask = PACKET_COOKIE_MASK
        self.source_target_mode = np.zeros((NUM_SOURCES,), dtype=np.uint8)
        self._capture_format = [None, None]
        self._fmt_scratch = [{}, {}]
        super().__init__(packet_receiver, receive_window, verbose)

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
        self.fmt_cdata_buf1_0, self.fmt_cdata_buf1_1 = np.empty(0, dtype=np.uint8), np.empty(0, dtype=np.uint8)
        self.fmt_cdata_buf2_0, self.fmt_cdata_buf2_1 = np.empty(0, dtype=np.uint8), np.empty(0, dtype=np.uint8)
        self.fmt_cdata1 = [None, None]
        self.fmt_cdata2 = [None, None]

    def update_config(self):
        source = self._config_getter()
        if self.config is None:
            self.config = source
        elif source is not self.config:
            raise RuntimeError("config_getter must return a stable State")

        state = self.config

        # List if state attributes that do not change during normal operation and will require reallocation of internal buffers
        # The tuple is used both as a list of attributes and the context token

        static_state_attrs = (
            "bytes_per_sample",
            "samples_per_frame",
            "frames_per_burst",
            "fft_bins_per_word",
            "capture_bits_per_word",
            "subframes_per_frame",
            "stream_ids",
            "target_mode",
        )

        # Recompute output buffer arrays if the packet geometry parameter has changed 
        if state.has_changed(static_state_attrs, context=static_state_attrs):  
            if self.verbose:
                print(f"update_config: static state attributes have changed. Buffers will be cleared.")

            self.TARGET_MODE = state.target_mode
            self.BYTES_PER_SAMPLE = state.bytes_per_sample
            self.SAMPLES_PER_FRAME = state.samples_per_frame
            self.FRAMES_PER_BURST = state.frames_per_burst
            self.NUMBER_OF_SUBFRAMES = state.subframes_per_frame
            self.stream_ids = state.stream_ids

            if len(set(self.stream_ids)) != len(self.stream_ids):
                raise ValueError("stream_ids must be unique ")
            if max(self.stream_ids) >= 1 << 12:
                raise ValueError("stream_ids must be less than 1 << 12")

            self.BYTES_PER_FRAME = int(state.bytes_per_frame)
            self.BYTES_PER_SUBFRAME = int(state.bytes_per_subframe)
            self.PACKET_SIZE = self.header_dtype.itemsize + self.BYTES_PER_SUBFRAME

            self.FFT_BINS_PER_WORD = int(state.fft_bins_per_word)
            self.CAPTURE_BITS_PER_WORD = int(state.capture_bits_per_word)

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
            self._last_emitted_ts = np.uint64(0)
            self._set_out_rows(0, 0)

            # Per (channel, frame_index) cell in ``raw_data_count``; used for carryover completion.
            self.packets_per_frame = self.NUMBER_OF_SUBFRAMES
            # Total subframe packets per channel across one full burst (all frame indices).
            self.expected_packets = self.FRAMES_PER_BURST * self.NUMBER_OF_SUBFRAMES

            state.ack(static_state_attrs, context=static_state_attrs)

        dynamic_state_attrs = (
            "capture_format0",
            "capture_format1",
        )

        for source, cf in enumerate(state.capture_format):
            if cf is None:
                continue
            if not isinstance(cf, State):
                cf = State(cf)
                state.capture_format[source] = cf
            if cf.has_changed(context=dynamic_state_attrs[source]):
                if cf.fmt_is_complex:
                    fmt_bus_width = int(cf.fmt_bus_width)
                    fmt_data_width = int(cf.fmt_data_width)
                    bytes_per_integer = math.ceil(fmt_bus_width / 8)
                    # Use the meaningful Re/Im width for output precision. The bus width
                    # may include padding, while float32 cannot exactly hold >24-bit ints.
                    out_dtype = np.dtype(np.complex128 if fmt_data_width > 24 else np.complex64)
                    complex_values_per_frame = self.BYTES_PER_FRAME // bytes_per_integer // 2
                    interleave_factor = int(cf.fmt_interleave_factor or 1)
                    if interleave_factor > 2:
                        if interleave_factor % 2:
                            raise ValueError(
                                f"Full-resolution complex interleave factor must be even, "
                                f"got {interleave_factor}."
                            )
                        if self.CAPTURE_BITS_PER_WORD % fmt_bus_width:
                            raise ValueError(
                                f"capture_bits_per_word={self.CAPTURE_BITS_PER_WORD} must be a "
                                f"multiple of fmt_bus_width={fmt_bus_width}."
                            )
                        words_per_capture_word = max(1, self.CAPTURE_BITS_PER_WORD // fmt_bus_width)
                        complex_values_per_frame = self.BYTES_PER_FRAME // bytes_per_integer // words_per_capture_word
                        cdata2_dims = (
                            self.max_timestamps * self.FRAMES_PER_BURST // interleave_factor,
                            self.NCHAN,
                            complex_values_per_frame * (interleave_factor // 2),
                        )
                    else:
                        cdata2_dims = (self.max_timestamps * self.FRAMES_PER_BURST // interleave_factor, self.NCHAN, complex_values_per_frame * interleave_factor) # deinterleaved complex value storage buffer
                    cdata1_dims = (self.max_timestamps * self.FRAMES_PER_BURST, self.NCHAN, complex_values_per_frame) # complex value storage buffer
                    self.fmt_cdata1[source] = self.resize_buffer(f"fmt_cdata_buf1_{source}", cdata1_dims, element_dtype=out_dtype)
                    self.fmt_cdata2[source] = self.resize_buffer(f"fmt_cdata_buf2_{source}", cdata2_dims, element_dtype=out_dtype)
            cf.ack(context=dynamic_state_attrs[source])

    def process_udp_packets(self, verbose=None, update_config=True):
        if update_config:
            self.update_config()
        super().process_udp_packets(verbose=verbose)

    def get_formatted_data(self, rows=slice(None), source=0, split=False, format=None, complex_override=None):
        """Transform assembled raw rows into ``(ts, data, counts)`` for source ``source``.

        Parameters
        ----------
        rows (slice): Index rows in the internal assembly buffer. These are burst rows, not packet or formatted data rows.
        source (int): Capture source index (ADC vs FFT).
        split (bool): If True, split each burst into ``frames_per_burst`` rows with consecutive timestamps and per-frame packet counts.
        format (str): Ovverides the capture format for the specified source.

        Returns
        -------
        tuple
            ``(ts, data, counts)`` with matching leading dimensions.
        """
        raw_data = self.raw_data[rows]
        ts = self.ts_array[rows]
        counts = self.raw_data_count[rows]
        sources = self.raw_source[rows]

        if format is not None:
            cf = decode_format_string(format)
        else:
            cf = self.config.capture_format[source]
            if cf is not None and not isinstance(cf, State):
                cf = State(cf)
        base = cf.fmt_bus_width
        is_complex = cf.fmt_is_complex
        if complex_override is not None:
            is_complex = complex_override


        # derive format parameters
        scalar_bytes = math.ceil(base / 8)
        integer_dtype = np.dtype(f">i{scalar_bytes}") if scalar_bytes <= 8 else np.dtype(f'>i8')
        right_shift = base - cf.fmt_data_width
        # View the buffer as N-byte integers
        data = raw_data.view(integer_dtype)
        if scalar_bytes > 8:
            data = data[..., ::2] #drop every second byte
            right_shift -= 64
        if right_shift > 0:
            np.right_shift(data, right_shift, data)

        if cf.use_offset_encoding:
            # Offset-binary codes are fmt_data_width bits, MSB-aligned in the bus
            # slot. Arithmetic right-shift sign-extends from the offset MSB, so
            # recover the unsigned field and subtract the data_width midpoint
            # (not fmt_bus_width, which would subtract 2^15 on a 16-bit CRS slot).
            data_width = int(cf.fmt_data_width)
            unsigned = data.astype(np.int64) & ((1 << data_width) - 1)
            data = (unsigned - (1 << (data_width - 1))).astype(data.dtype, copy=False)

        if is_complex:
            split = True
        if split:  # split a burst into multiple independent frames. Frames are always split for complex data.
            n_ts, nchan, nsamples_burst = data.shape
            frames_per_burst = int(self.FRAMES_PER_BURST)
            samples_per_frame = nsamples_burst // frames_per_burst
            data = data.reshape(n_ts, nchan, frames_per_burst, samples_per_frame)\
                .transpose(0, 2, 1, 3)\
                .reshape(n_ts * frames_per_burst, nchan, samples_per_frame)

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

        # From here we have complex data, possibly with modifiers.
        interleave_factor = int(cf.fmt_interleave_factor or 1)
        if interleave_factor > 2:
            # Full resolution is dependent on the capture bus width (W) and number of bins per word (B).
            # We send W-bit Re then W-bit Im, skipping 2*B bins between them. It takes 2B frames to get the full spectrum.
            # ICE: W=32, B=2, 32+32-bit complex values, 2B= 4 frames (Max burst length = 4 frames)
            # CRS: W=128, B=4, 128+128-bit complex values, 2B= 8 frames (use mode 2 (8 frames/2 channels) or more 3 (16 frames/1 channel))
            if interleave_factor % 2:
                raise ValueError(
                    f"Full-resolution complex interleave factor must be even, got {interleave_factor}."
                )
            if data.shape[0] % interleave_factor:
                raise ValueError(
                    f"Full-resolution complex data has {data.shape[0]} physical rows, "
                    f"not a multiple of fmt_interleave_factor={interleave_factor}."
                )
            if self.CAPTURE_BITS_PER_WORD % base:
                raise ValueError(
                    f"capture_bits_per_word={self.CAPTURE_BITS_PER_WORD} must be a multiple "
                    f"of fmt_bus_width={base}."
                )
            words_per_capture_word = max(1, self.CAPTURE_BITS_PER_WORD // base)
            if words_per_capture_word > 1:
                data = data[:, :, ::words_per_capture_word]

            logical_rows = data.shape[0] // interleave_factor
            bins_per_word = interleave_factor // 2
            v = data.reshape(logical_rows, interleave_factor, data.shape[1], data.shape[2])
            out = self.fmt_cdata2[source][:logical_rows]
            # Full-resolution frames carry Re/Im pairs in each physical row.
            complex_values_per_frame = data.shape[2] // 2
            cdata = self.fmt_cdata1[source][:data.shape[0], :, :complex_values_per_frame]
            np.copyto(cdata.real, data[:, :, ::2])
            np.copyto(cdata.imag, data[:, :, 1::2])
            cv = cdata.reshape(logical_rows, interleave_factor, data.shape[1], cdata.shape[2])
            for lane in range(bins_per_word):
                frame_pair = cv[:, 2 * lane:2 * lane + 2, :, :]
                out[..., lane::bins_per_word] = (
                    frame_pair
                    .transpose(0, 2, 1, 3)
                    .reshape(logical_rows, data.shape[1], 2 * cdata.shape[2], order='F')
                )
            ts = ts.reshape(logical_rows, interleave_factor)[:, 0]
            counts = counts.reshape(logical_rows, interleave_factor, counts.shape[1]).min(axis=1)
            data = out
        else:
            # fmt_cdata* are sized for max_timestamps; only slice the active row prefix.
            n_rows = data.shape[0]
            cdata = self.fmt_cdata1[source][:n_rows]
            np.copyto(cdata.real, data[:, :, ::2])
            np.copyto(cdata.imag, data[:, :, 1::2])

            if interleave_factor == 1:
                data = cdata
            elif interleave_factor == 2:
                # fft_2x: deinterleave pairs of consecutive physical frame rows (legacy ``32+32d``).
                s0, nchan, s2 = cdata.shape
                out_rows = s0 // interleave_factor
                if out_rows * interleave_factor != s0:
                    raise ValueError(
                        f'Cannot deinterleave {s0} physical frame rows with '
                        f'interleave_factor={interleave_factor}'
                    )
                data = self.fmt_cdata2[source][:out_rows]
                v = cdata.reshape(out_rows, interleave_factor, nchan, s2)
                data[:] = v.transpose(0, 2, 1, 3).reshape(
                    out_rows, nchan, s2 * interleave_factor, order='F')
                ts = ts.reshape(out_rows, interleave_factor)[:, 0]
                counts = counts.reshape(out_rows, interleave_factor, nchan).min(axis=1)

        bin_map = self.config.get("fft_bin_map")
        if bin_map is not None:
            bin_map = np.asarray(bin_map, dtype=np.intp)
            if bin_map.size and bin_map.size == data.shape[-1]:
                data = data[..., bin_map]

        return ts, data, counts


    def create_output_buffer(self, n_ts, source=0, split=False, format=None, complex_override=None):
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
        # format a dummy 1-row buffer to get the shape of the data and counts
        ts_, data_, counts_ = self.get_formatted_data(slice(0, 1), source=source, split=split, format=format, complex_override=complex_override)
        ts_mult, nchan, nvalues = data_.shape  # ts_mult indicates how many formatted rowswe get from a single burst
        data = np.empty((n_ts*ts_mult, nchan, nvalues), dtype=data_.dtype)
        counts = np.empty((n_ts*ts_mult, nchan), dtype=counts_.dtype)
        ts = np.empty((n_ts*ts_mult,), dtype=ts_.dtype)
        return ts, data, counts

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
        n_cookie = int(bix_mask.sum()) if self.verbose >= 2 else 0

        # Process packet fields (slice UDP buffer views to active prefix ``n``)
        np.bitwise_and(self.buf_ts[:n], self.ts_mask, out=self.pkt_ts[:n], where=bix_mask)
        np.bitwise_and(self.buf_frame_stream_id[:n], self.sid_mask, out=self.pkt_stream_id[:n]) # removed mask to make sure take works
        np.right_shift(self.buf_frame_number[:n], 4, out=self.pkt_frame[:n], where=bix_mask)
        np.bitwise_and(self.pkt_frame[:n], 0x0F, out=self.pkt_frame[:n], where=bix_mask)
        np.right_shift(self.buf_mode_subframe[:n], 2, out=self.pkt_mode[:n], where=bix_mask)
        np.bitwise_and(self.pkt_mode[:n], 0x3, out=self.pkt_mode[:n], where=bix_mask)
        np.bitwise_and(self.buf_mode_subframe[:n], 0x3, out=self.pkt_subframe[:n], where=bix_mask)
        packet_mode_str = "-"
        if self.verbose >= 2 and n_cookie:
            packet_mode_str = ",".join(
                str(int(m)) for m in np.unique(self.pkt_mode[:n][bix_mask[:n]])
            )

        # Compute the stream index for each packet
        np.take(self.six_map, self.pkt_stream_id[:n], out=self.pkt_six[:n]) # vectorized lookup of the stream index for each packet

        # Filter out packets with an invalid frame
        np.less(self.pkt_frame[:n], self.FRAMES_PER_BURST, out=temp_mask, where=bix_mask)
        np.bitwise_and(bix_mask, temp_mask, out=bix_mask)
        n_frame = int(bix_mask.sum()) if self.verbose >= 2 else 0
        # Filter out packets with an invalid subframe number
        np.less(self.pkt_subframe[:n], self.NUMBER_OF_SUBFRAMES, out=temp_mask, where=bix_mask)
        np.bitwise_and(bix_mask, temp_mask, out=bix_mask)
        # Drop UDP packets with an invalid stream index
        np.greater_equal(self.pkt_six[:n].view(np.int16), 0, out=temp_mask, where=bix_mask)
        np.bitwise_and(bix_mask, temp_mask, out=bix_mask)
        n_stream = int(bix_mask.sum()) if self.verbose >= 2 else 0
        # Drop UDP packets with the wrong mode
        np.equal(self.pkt_mode[:n], self.TARGET_MODE, out=temp_mask, where=bix_mask)
        np.bitwise_and(bix_mask, temp_mask, out=bix_mask)
        n_mode = int(bix_mask.sum()) if self.verbose >= 2 else 0
        # Drop UDP packets at or before the latest timestamp already returned (monotonic emits).
        if self.verbose:
            rejected_packets = int(np.sum(bix_mask[:n] & (self.pkt_ts[:n] <= self._last_emitted_ts)))
        np.greater(self.pkt_ts[:n], self._last_emitted_ts, out=temp_mask, where=bix_mask)
        np.bitwise_and(bix_mask, temp_mask, out=bix_mask)

        if self.verbose >= 2:
            print(
                f"process_packets: cookie/size={n_cookie} "
                f"frame<{self.FRAMES_PER_BURST}={n_frame} "
                f"stream_id={n_stream} mode={packet_mode_str} expected={self.TARGET_MODE} match={n_mode} "
                f"after_ts_filter={int(bix_mask.sum())} "
                f"(stream_ids={self.stream_ids})"
            )
        if self.verbose and rejected_packets:
            print(
                f"RawDataPacketProcessor: process_packets: rejected {rejected_packets} packets "
                f"at or before the latest timestamp already returned "
                f"(monotonic emits). {self._last_emitted_ts=} {self.pkt_ts[:n].min()=}."
            )

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

    def flush(self, flush_timeout=None):
        """Flush UDP receiver and clear assembled-buffer state."""
        self.pr.flush(timeout=flush_timeout, verbose=self.verbose)
        if self.verbose >= 1:
            print("Flushing RawDataPacketProcessor linear buffer")
        self.ts_array.fill(self.TS_EMPTY)
        self.raw_data.fill(0)
        self.raw_data_count.fill(0)
        self.raw_source.fill(RAW_SOURCE_EMPTY)
        self.n_ts = 0
        self.carry_len = 0
        self._last_emitted_ts = np.uint64(0)
        self._set_out_rows(0, 0)
