"""Corner-turn capture receiver used by the firmware tests.

ADC and FFT stay on :class:`RawDataPacketProcessor`. This class reuses that
assembler for cookie ``0xA2`` only: one packet size, one stream id per armed
probe, and the ``(4+4)``-bit payload unpack.
"""

import numpy as np

from .raw_data_packet_processor import RawDataPacketProcessor


CT_HEADER_WORDS = 4


class CTPacketProcessor(RawDataPacketProcessor):
    """Assemble corner-turn datagrams and unpack their payload."""

    PACKET_COOKIE = 0xA2
    PACKET_COOKIE_MASK = 0xFF

    def update_config(self):
        super().update_config()
        data_words = int(self.config.data_words)
        dims = (
            self.max_timestamps * self.FRAMES_PER_BURST,
            self.NCHAN,
            4 * data_words,
        )
        current = self.fmt_cdata1[0]
        if current is None or current.shape != dims:
            self.fmt_cdata1[0] = self.resize_buffer(
                "fmt_cdata_buf1_0", dims, element_dtype=np.complex64)

    def get_formatted_data(
        self,
        rows=slice(None),
        source=0,
        split=False,
        format=None,
        complex_override=None,
    ):
        """Return ``(ts, data, counts)`` for the assembled corner-turn rows.

        ``format='32'`` returns the raw packet words. Otherwise the 4-word
        header is stripped and the ``(4+4)``-bit payload is unpacked.
        """
        if format is not None:
            return super().get_formatted_data(
                rows, source=source, split=split, format=format,
                complex_override=complex_override)

        raw_data = self.raw_data[rows]
        ts = self.ts_array[rows]
        counts = self.raw_data_count[rows]
        cf = self.config
        if not bool(getattr(cf, "four_bits", True)):
            raise NotImplementedError(
                "8-bit corner-turn payload decode is not implemented; "
                "use format='32' to read the raw packet words")

        n_ts, nchan, nbytes_burst = raw_data.shape
        frames_per_burst = int(self.FRAMES_PER_BURST)
        words_per_packet = nbytes_burst // frames_per_burst // 4
        header_words = int(getattr(cf, "header_words", CT_HEADER_WORDS))
        data_words = int(cf.data_words)
        words = raw_data.view(">u4").reshape(
            n_ts, nchan, frames_per_burst, words_per_packet)
        payload = words[..., header_words:header_words + data_words]
        # Channel 0 of a bin word sits in the LSBs. Byteswap so bytes
        # come out in logical channel order.
        packed = np.ascontiguousarray(payload.astype("<u4")).view(np.uint8)
        packed = packed.reshape(n_ts, nchan, frames_per_burst, 4 * data_words)
        if bool(getattr(cf, "use_offset_binary", False)):
            packed = packed ^ np.uint8(0x88)
        signed = packed.view(np.int8)
        signed = signed.transpose(0, 2, 1, 3).reshape(
            n_ts * frames_per_burst, nchan, 4 * data_words)
        cdata = self.fmt_cdata1[0][:n_ts * frames_per_burst]
        np.copyto(cdata.real, signed >> 4)
        np.copyto(cdata.imag, np.left_shift(signed, 4).astype(np.int8) >> 4)
        frame_offsets = np.arange(frames_per_burst, dtype=ts.dtype)
        ts = (ts[:, None] + frame_offsets[None, :]).reshape(-1)
        counts = counts.swapaxes(-1, -2).reshape(n_ts * frames_per_burst, -1)
        return ts, cdata, counts


class CTDataReceiver(CTPacketProcessor):
    """Burst reader for one armed corner-turn capture."""

    def __init__(self, packet_receiver, config_getter, verbose=0, **kwargs):
        super().__init__(
            packet_receiver, config_getter=config_getter, verbose=verbose, **kwargs)
        self.current_row = 0
        self.update_config()

    def flush(self, flush_timeout=None):
        super().flush(flush_timeout)
        self.current_row = 0
        self.skip_first_incomplete = True

    def read_frames(
        self,
        ncap: int = 1,
        split=None,
        flush=False,
        flush_timeout=None,
        chan_first: bool = True,
        format=None,
        complex_override=None,
        verbose=None,
        skip_first_incomplete=None,
    ):
        """Read ``ncap`` corner-turn bursts.

        Returns ``(ts, data, counts)``. ``chan_first=False`` keeps the row
        axis first, which is what the firmware tests use.
        """
        if verbose is not None:
            self.verbose = verbose
            self.pr.verbose = verbose

        self.update_config()
        ts, data, counts = self.create_output_buffer(
            n_ts=ncap, source=0, split=split, format=format,
            complex_override=complex_override)

        raw_rows_completed = 0
        fmt_rows_completed = 0

        if flush:
            self.flush(flush_timeout)
        if skip_first_incomplete is None:
            skip_first_incomplete = bool(flush)
        self.skip_first_incomplete = bool(skip_first_incomplete)

        while raw_rows_completed < ncap:
            stop = self.out_rows.stop
            if self.current_row < stop:
                n = min(ncap - raw_rows_completed, stop - self.current_row)
                rows = slice(self.current_row, self.current_row + n)
                ts_, data_, counts_ = self.get_formatted_data(
                    rows, split=split, format=format,
                    complex_override=complex_override)
                raw_rows_completed += n
                n_fmt = ts_.shape[0]
                fmt_rows = slice(fmt_rows_completed, fmt_rows_completed + n_fmt)
                ts[fmt_rows], data[fmt_rows], counts[fmt_rows] = ts_, data_, counts_
                fmt_rows_completed += n_fmt
                self.current_row += n
            if raw_rows_completed >= ncap:
                break
            while True:
                self.pr.receive_packets(verbose=self.verbose)
                if self.pr.n_stored:
                    self.process_udp_packets(update_config=False)
                    self.current_row = self.out_rows.start
                    if self.n_out:
                        break

        if chan_first:
            data = data.swapaxes(0, 1)
        return ts, data, counts
