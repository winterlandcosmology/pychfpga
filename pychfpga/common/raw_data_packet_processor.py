import math
import re
import warnings
from warnings import warn as _warn

import numpy as np

from .packet_processor_base import PacketProcessorBase
from .state import State


PACKET_COOKIE_ADC = 0xA0
PACKET_COOKIE_FFT = 0xA1
PACKET_COOKIE_CT = 0xA2  # corner-turn output capture probe (ct_prober)
PACKET_COOKIE_MASK = 0xFE
RAW_SOURCE_EMPTY = 255
NUM_SOURCES = 3


def decode_format_string(format):
    """Decode a raw capture format string into a ``State`` for ``get_formatted_data``.

    Format string: ``<width>`` [``+<width>``] [``<modifiers>``]

    Bit widths (``<width>``): ``8``, ``14``, ``16``, ``32``, ``64``.

    Modifiers (``<modifiers>``): ``d`` — deinterleave.

    Args:
        format: Format string such as ``"16"``, ``"32+32"``, or ``"32+32d"``.

    Returns:
        State with ``fmt_bus_width``, ``fmt_data_width``, ``fmt_is_complex``,
        and ``fmt_interleave_factor`` (same fields as
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
        self._capture_format = [None] * NUM_SOURCES
        self._fmt_scratch = [{} for _ in range(NUM_SOURCES)]
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
        self.pkt_source = np.empty((n_packets,), dtype=np.int8)  # source index per packet from the cookie lookup (-1 = unknown cookie)

        self.pkt_six = np.empty((n_packets,), dtype=np.uint16)
        self.pkt_off = np.empty((n_packets,), dtype=np.uint32)

        # Instialize empty buffers. These will be resized when the configuration is updated below.
        self.raw_data_buf = np.empty(0, dtype=np.uint8)
        self.raw_data_count_buf = np.empty(0, dtype=np.int64)
        self.raw_source_buf = np.empty(0, dtype=np.uint8)
        self.raw_subframe_mask_buf = np.empty(0, dtype=np.uint8)
        self.raw_source_mask_buf = np.empty(0, dtype=np.uint8)
        self.formatted_data_bufs = [np.empty(0, dtype=np.uint8) for _ in range(NUM_SOURCES)]
        self.fmt_cdata_buf1_0, self.fmt_cdata_buf1_1 = np.empty(0, dtype=np.uint8), np.empty(0, dtype=np.uint8)
        self.fmt_cdata_buf2_0, self.fmt_cdata_buf2_1 = np.empty(0, dtype=np.uint8), np.empty(0, dtype=np.uint8)
        self.fmt_cdata_buf1_2, self.fmt_cdata_buf2_2 = np.empty(0, dtype=np.uint8), np.empty(0, dtype=np.uint8)
        self.fmt_cdata1 = [None] * NUM_SOURCES
        self.fmt_cdata2 = [None] * NUM_SOURCES

    def update_config(self, config=None):
        if config is not None:
            self.config.update(config)

        state = self.config
        if state.get("capture_bits_per_word") is None:
            state.capture_bits_per_word = int(state.get("bytes_per_sample", 1) or 1) * 8
        if state.get("packet_cookie") is None:
            # Legacy configs (CRS/UCAP, older ICE) list no cookies: default to
            # the two channelizer sources, which never admit 0xA2.
            state.packet_cookie = [PACKET_COOKIE_ADC, PACKET_COOKIE_FFT]

        # List if state attributes that do not change during normal operation and will require reallocation of internal buffers
        # The tuple is used both as a list of attributes and the context token

        static_state_attrs = (
            "capture_engine",
            "bytes_per_sample",
            "samples_per_frame",
            "frames_per_burst",
            "fft_bins_per_word",
            "capture_bits_per_word",
            "subframes_per_frame",
            "stream_ids",
            "packets_per_logical_frame",
            "logical_frames_per_burst",
            "packet_header_format",
            "packet_cookie",
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
            if self.NUMBER_OF_SUBFRAMES > 64:
                raise ValueError(
                    "subframes_per_frame must be at most 64 for unique-packet tracking"
                )
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
            self.CAPTURE_BITS_PER_WORD = int(state.capture_bits_per_word)

            # Cookie-to-source-index lookup table. Configs list the accepted
            # cookies in source order (0xA0 -> 0, 0xA1 -> 1, and 0xA2 -> 2 for
            # firmware with the corner-turn capture probe); every other cookie
            # maps to -1 and is dropped by filter_packets. Legacy two-entry
            # configs (CRS/UCAP, older ICE builds) therefore never admit 0xA2.
            cookies = list(state.packet_cookie)
            if len(cookies) > NUM_SOURCES:
                raise ValueError(
                    f"packet_cookie lists {len(cookies)} cookies but only "
                    f"{NUM_SOURCES} capture sources are supported")
            self.cookie_source_map = np.full(256, -1, dtype=np.int8)
            self.cookie_source_map[np.asarray(cookies, dtype=np.intp)] = np.arange(len(cookies), dtype=np.int8)

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
            self.raw_subframe_mask = self.resize_buffer(
                'raw_subframe_mask_buf',
                (self.max_timestamps, self.NCHAN, self.FRAMES_PER_BURST),
                element_dtype=np.uint64,
            )
            self.raw_source_mask = self.resize_buffer(
                'raw_source_mask_buf',
                (self.max_timestamps, self.NCHAN, self.FRAMES_PER_BURST),
                element_dtype=np.uint8,
            )
            self.raw_source.fill(RAW_SOURCE_EMPTY)
            self.raw_subframe_mask.fill(0)
            self.raw_source_mask.fill(0)


            # No carryover
            self.carry_len = 0
            self.carry_tix = 0
            self._last_emitted_ts = 0
            self.n_out = 0

            # Per (channel, frame_index) cell in ``raw_data_count``; used for carryover completion.
            self.packets_per_frame = self.NUMBER_OF_SUBFRAMES
            # Total subframe packets per channel across one full burst (all frame indices).
            self.expected_packets = self.FRAMES_PER_BURST * self.NUMBER_OF_SUBFRAMES

            state.ack(static_state_attrs, context=static_state_attrs)

        dynamic_state_attrs = (
            "capture_format0",
            "capture_format1",
            "capture_format2",
        )

        for source, cf in enumerate(state.capture_format):
            if cf is None:
                continue
            if not isinstance(cf, State):
                cf = State(cf)
                state.capture_format[source] = cf
            if cf.has_changed(context=dynamic_state_attrs[source]):
                if cf.get("fmt_ct_packed"):
                    # Corner-turn packets: one (4+4)-bit complex value per
                    # payload byte, fmt_data_words 32-bit words per CT packet
                    # (the 4-word CT header, optional flags and the trailer
                    # word are stripped by get_formatted_data). Output rows
                    # are per wire packet, so only fmt_cdata1 is needed.
                    cdata1_dims = (
                        self.max_timestamps * self.FRAMES_PER_BURST,
                        self.NCHAN,
                        4 * int(cf.fmt_data_words),
                    )
                    self.fmt_cdata1[source] = self.resize_buffer(
                        f"fmt_cdata_buf1_{source}", cdata1_dims, element_dtype=np.complex64)
                elif cf.fmt_is_complex:
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

    def _fft_offset_encoding_enabled(self):
        """Return True if FFT offset-binary compensation is enabled in config."""
        if self.config.get("offset_encoding") is not None:
            return bool(self.config.offset_encoding)
        return bool(self.config.get("fft_offset_encoding", False))

    def _apply_fft_formatter_compensation(self, data, source, bit_width):
        """Apply offset-binary decode and natural-order bin mapping for FFT spectra.

        Downstream sinks assume natural frequency order with offset encoding removed.
        """
        if source != 1:
            return data
        if self._fft_offset_encoding_enabled():
            # Offset-binary samples are unsigned around a midpoint; convert to signed.
            bias = 1 << (int(bit_width) - 1)
            data.real -= bias
            data.imag -= bias
        bin_map = self.config.get("fft_bin_map")
        if bin_map is not None:
            bin_map = np.asarray(bin_map, dtype=np.intp)
            if bin_map.size and bin_map.size == data.shape[-1]:
                data = data[..., bin_map]
        return data





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
            if not 0 <= source < len(self.config.capture_format):
                raise ValueError(
                    f"No capture format is configured for source {source}. "
                    f"Source 2 (CT, cookie 0xA2) requires an active "
                    f"start_data_capture(source='ct') on an ICE corner-turn "
                    f"firmware; this configuration only has "
                    f"{len(self.config.capture_format)} source(s).")
            cf = self.config.capture_format[source]
            if cf is None:
                raise ValueError(
                    f"No capture format is configured for source {source}. "
                    f"Source 2 (CT, cookie 0xA2) requires an active "
                    f"start_data_capture(source='ct') on an ICE corner-turn "
                    f"firmware; reconfigure the capture or pass format=.")
            if not isinstance(cf, State):
                cf = State(cf)

        if cf.get("fmt_ct_packed"):
            # Corner-turn packets: each burst row holds FRAMES_PER_BURST
            # complete CT packets of big-endian 32-bit words (4-word CT
            # header, data block, optional flags blocks, one trailer word).
            # Strip the header and everything after the data block, and
            # unpack the (4+4)-bit complex bytes. The logical layout of the
            # returned samples is (frames_per_packet, bins, channels_per_bin)
            # flattened, i.e. reshape with
            # data.reshape(-1, fmt_frames_per_packet, fmt_bins, fmt_channels_per_bin);
            # the bin numbers map through CT.CROSSBAR.BIN_SEL[lane].get_selected_bins().
            # Use format='32' to get the raw words including the CT header.
            if not cf.get("fmt_four_bits", True):
                raise NotImplementedError(
                    "8-bit corner-turn payload decode is not implemented; "
                    "use format='32' to read the raw packet words")
            n_ts, nchan, nbytes_burst = raw_data.shape
            frames_per_burst = int(self.FRAMES_PER_BURST)
            words_per_packet = nbytes_burst // frames_per_burst // 4
            header_words = int(cf.get("fmt_header_words", 4))
            data_words = int(cf.fmt_data_words)
            w = raw_data.view(">u4").reshape(n_ts, nchan, frames_per_burst, words_per_packet)
            payload = w[..., header_words:header_words + data_words]
            # The wire serializes each word MSB-first, but channel 0 of a bin
            # word sits in the LSBs: byteswap so bytes come out in logical
            # channel order (same convention as the legacy GPU-link decoder).
            b = np.ascontiguousarray(payload.astype("<u4")).view(np.uint8)
            b = b.reshape(n_ts, nchan, frames_per_burst, 4 * data_words)
            if cf.get("use_offset_encoding"):
                b = b ^ np.uint8(0x88)  # offset-binary nibbles -> two's complement
            s = b.view(np.int8)
            # Split each wire packet into its own output row; the packets of a
            # burst are consecutive CT packets, so extend the timestamps the
            # same way as the generic split path below.
            s = s.transpose(0, 2, 1, 3).reshape(n_ts * frames_per_burst, nchan, 4 * data_words)
            cdata = self.fmt_cdata1[source][:n_ts * frames_per_burst]
            np.copyto(cdata.real, s >> 4)                                # Re = signed high nibble
            np.copyto(cdata.imag, np.left_shift(s, 4).astype(np.int8) >> 4)  # Im = signed low nibble
            frame_offsets = np.arange(frames_per_burst, dtype=ts.dtype)
            ts = (ts[:, None] + frame_offsets[None, :]).reshape(-1)
            sources = sources.swapaxes(-1, -2).reshape(n_ts * frames_per_burst, -1)
            counts = counts.swapaxes(-1, -2).reshape(n_ts * frames_per_burst, -1) * (sources == source)
            return ts, cdata, counts

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
            source_frames = sources == source
            sources = source_frames.any(axis=-1)
            counts = (counts * source_frames).sum(axis=-1)

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

        data = self._apply_fft_formatter_compensation(data, source, base)

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

        # Filter out packets with an unknown cookie or the wrong packet size.
        # The cookie lookup also yields the source index for each packet
        # (0xA0 -> 0, 0xA1 -> 1, 0xA2 -> 2 when configured; -1 = drop).
        np.take(self.cookie_source_map, self.buf_cookie[:n], out=self.pkt_source[:n])
        np.greater_equal(self.pkt_source[:n], 0, out=bix_mask)
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

        # Compute each payload byte offset while preserving the decoded frame
        # and subframe numbers for unique-packet/source validation below.
        # Only ``pkt_off[bix]`` is read later; entries where ``bix_mask`` is false are left undefined.
        np.multiply(self.pkt_frame[:n], self.BYTES_PER_FRAME, out=self.pkt_off[:n], where=bix_mask)
        # Keep pkt_subframe as the decoded subframe number. Unique-subframe
        # accounting in store_packets() needs it to distinguish duplicates
        # from a complete set of packets.
        self.pkt_off[:n] += self.pkt_subframe[:n] * self.BYTES_PER_SUBFRAME

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
            self.raw_subframe_mask[dst_tix] = self.raw_subframe_mask[src_tix]
            self.raw_source_mask[dst_tix] = self.raw_source_mask[src_tix]
            tix_to_clear[dst_tix] = False  # don't clear the carryover rows

        n = tix_to_clear.size
        self.raw_data_count[:n][tix_to_clear] = 0
        self.raw_data[:n][tix_to_clear] = 0
        self.raw_source[:n][tix_to_clear] = RAW_SOURCE_EMPTY
        self.raw_subframe_mask[:n][tix_to_clear] = 0
        self.raw_source_mask[:n][tix_to_clear] = 0

    def store_packets(self, bix, tix):
        """Scatter payloads and update unique-subframe/source completeness state."""
        # Now that we have all the info we need to copy data from the UDP buffer and reassemble it into the output buffer.
        # This is done in a very efficient single vectorized operation.
        self.raw_data[
            tix[:, None],
            self.pkt_six[bix, None],
            self.pkt_off[bix, None] + np.arange(self.BYTES_PER_SUBFRAME, dtype=self.pkt_off.dtype),
        ] = self.buf_data[bix, :self.BYTES_PER_SUBFRAME] # copy the payload bytes from the UDP buffer to the output buffer

        six = self.pkt_six[bix]
        frame = self.pkt_frame[bix]
        subframe = self.pkt_subframe[bix]
        cell = (tix * self.NCHAN + six) * self.FRAMES_PER_BURST + frame
        subframe_key = cell * self.NUMBER_OF_SUBFRAMES + subframe
        _, first, subframe_repeats = np.unique(
            subframe_key, return_index=True, return_counts=True
        )
        repeated_cells = cell[first[subframe_repeats > 1]]

        subframe_bit = np.left_shift(
            np.uint64(1), subframe.astype(np.uint64, copy=False)
        )
        old_subframe_mask = self.raw_subframe_mask[tix, six, frame]
        duplicate_cells = cell[(old_subframe_mask & subframe_bit) != 0]
        np.bitwise_or.at(
            self.raw_subframe_mask, (tix, six, frame), subframe_bit
        )

        source_bit = np.left_shift(
            np.uint8(1), self.pkt_source[bix].astype(np.uint8, copy=False)
        )
        np.bitwise_or.at(self.raw_source_mask, (tix, six, frame), source_bit)

        affected = np.unique(cell)
        affected_frame = affected % self.FRAMES_PER_BURST
        affected_stream = (
            affected // self.FRAMES_PER_BURST
        ) % self.NCHAN
        affected_timestamp = affected // (
            self.FRAMES_PER_BURST * self.NCHAN
        )
        affected_index = (
            affected_timestamp, affected_stream, affected_frame
        )
        subframe_masks = self.raw_subframe_mask[affected_index]
        source_masks = self.raw_source_mask[affected_index]
        counts = np.fromiter(
            (int(mask).bit_count() for mask in subframe_masks),
            dtype=np.int64,
            count=affected.size,
        )
        invalid = self.raw_data_count[affected_index] < 0
        invalid |= np.isin(affected, duplicate_cells)
        invalid |= np.isin(affected, repeated_cells)
        invalid |= (source_masks & (source_masks - 1)) != 0
        counts[invalid] = -1
        self.raw_data_count[affected_index] = counts
        self.raw_source[affected_index] = np.where(
            source_masks == 1,
            0,
            np.where(
                source_masks == 2,
                1,
                np.where(source_masks == 4, 2, RAW_SOURCE_EMPTY),
            ),
        )

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
        self.raw_subframe_mask.fill(0)
        self.raw_source_mask.fill(0)
        self.n_ts = 0
        self.carry_len = 0
        self._last_emitted_ts = 0
        self.n_out = 0
