"""UCORR UDP packet processor."""

import math
import time
import warnings
from typing import Optional

import numpy as np

from .packet_processor_base import PacketProcessorBase
from .state import State

class UCorrPacketProcessor(PacketProcessorBase):
    """
    Packet processor for UCORR FPGA-based N-square firmware correlator.

    The processor reads data in the specified UDP packet buffer, decodes, assembles and integrate the correlation products, and returns
    all the processed correlation products that are ready to be passed to the user.

    Parameters:

                packet_receiver (UDPPacketReceiver): UDP packet source holding the shared packet buffer.

                config (dict or State or None): Initial correlator configuration. Can be updated later.

                receive_window (int): Maximum number of trailing incomplete integration rows withheld between
                    UDP packet blocks (carried into the next ``process_packets`` call).

                verbose (int): Verbosity level for debug prints.

    """

    CORR_PACKET_COOKIE = 0xCF
    NBYTES_PER_HEADER = 10 # Number of bytes in the UDP payload header
    NBYTES_PER_PROD = 5  # Number of bytes used to store each complex correlator product ( (18+18) bits = 36 bits, 5 bytes = 40 bits)

    # Firmware aggregates this many UCORR bins (each a full header+products record)
    # into one AXIS frame / UDP datagram by gating TLAST (``BINS_PER_FRAME_LOG2`` in
    # UCMAC44_ARRAY.vhd; 2**3 = 8). The receiver parses each datagram as up to this
    # many records along an added sub-packet axis. Must match the firmware factor.
    FRAMES_PER_PACKET = 8

    # Dtype of the formatted correlation output (:meth:`get_formatted_data`). complex64
    # halves the format-stage scatter write and the returned buffer versus complex128,
    # and is lossless here: accumulated products are integers < ~2**21 (soft_integ_period
    # * 2**17), well within float32's exact-integer range (2**24). Set to np.complex128
    # if a consumer requires double precision.
    OUTPUT_COMPLEX_DTYPE = np.complex64


    def __init__(self, packet_receiver, config_getter, receive_window=3, verbose=0):
        """Build a packet parser and linear integ-buffer pipeline.

        Parameters
        ----------
        packet_receiver : UDPPacketReceiver
            UDP packet source holding the shared packet buffer.
        config_getter : callable
            Callable returning a stable :class:`State` correlator config.
        receive_window : int
            Maximum number of trailing incomplete integration rows withheld between
            UDP packet blocks (carried into the next ``process_packets`` call).
        verbose : int
            Verbosity level for debug prints.
        """
        if config_getter is None:
            raise TypeError("config_getter is required")
        self._config_getter = config_getter
        self.config = None
        self.NCHAN = None
        self.NCLK = None
        self.bin_map = None
        self.prod_map = None
        self.conj_map = None  # products the firmware delivers conjugated (CT3 channel order), un-conjugated on receive
        self.firmware_integ_period = None
        self.soft_integ_period = 1
        self._applied_soft_integ_period = 1
        super().__init__(packet_receiver, receive_window, verbose)

    def set_soft_integ_period(self, soft_integ_period):
        """Set host-side software integration depth (not an FPGA register)."""
        self.soft_integ_period = max(1, int(soft_integ_period or 1))

    def init(self) -> None:
        # Header layout is fixed; product layout depends on NCMAC and is rebuilt on bin_map changes.
        self.header_dtype = np.dtype(dict(
            names=["cookie", "stream_id", "flags", "ts"],
            offsets=[0, 1, 3, 2],
            formats=["u1", ">u2", "u1", ">u8"]))
        self.product_dtype = np.dtype(dict(
            names=["sat", "l", "h"],
            offsets=[4, 0, 1],
            formats=["u1", "<i4", "<i4"]))
        self.ts_mask = np.uint64(0xFFFFFFFFFFFF)
        self.bin_mask = np.uint16(0b111_11111111)

        # Per-frame decode scratch is indexed by the flat frame index ``bix``
        # (one entry per aggregated sub-packet), so size it to the flat space
        # M = n_packets * FRAMES_PER_PACKET, matching the base-class scratch.
        # Was one slot per datagram (FRAMES_PER_PACKET == 1):
        #     n_packets = self.pr.n_packets
        #     self.pkt_corr = np.empty((n_packets,), dtype=np.uint16)
        #     self.pkt_bin = np.empty((n_packets,), dtype=np.uint16)
        #     self.pkt_bin_set = np.empty((n_packets,), dtype=np.uint8)
        m = self.pr.n_packets * self.FRAMES_PER_PACKET
        self.pkt_corr = np.empty((m,), dtype=np.uint16)
        self.pkt_bin = np.empty((m,), dtype=np.uint16)
        self.pkt_bin_set = np.empty((m,), dtype=np.uint8)

        # Linear buffers, sized in update_config().
        self.acc_re_buf = np.empty(0, dtype=np.uint8)
        self.acc_im_buf = np.empty(0, dtype=np.uint8)
        self.sat_buf = np.empty(0, dtype=np.uint8)
        self.raw_data_count_buf = np.empty(0, dtype=np.uint8)
        self.integ_array_buf = np.empty(0, dtype=np.uint8)
        self.integ_data_buf = np.empty(0, dtype=np.uint8)
        self.integ_sats_buf = np.empty(0, dtype=np.uint8)
        self.temp32_buf = np.empty(0, dtype=np.uint8)
        self.overrun_row_buf = np.empty(0, dtype=np.uint8)
        self.overrun_row = np.empty(0, dtype=np.uint8)
        self.ts_array = np.empty(0, dtype=np.uint64)
        self.max_timestamps = 0
        self.profile = {
            "recv_s": 0.0,
            "recv_n": 0,
            "recv_calls": 0,
            "process_s": 0.0,
            "process_calls": 0,
            "init_dest_s": 0.0,
            "init_dest_move_s": 0.0,
            "store_s": 0.0,
            "format_s": 0.0,
            "format_calls": 0,
        }

    def reset_profile(self):
        """Zero wall-time counters used by :meth:`read_corr_frames` / process path."""
        for k in self.profile:
            self.profile[k] = 0.0 if k.endswith("_s") else 0

    def update_config(self) -> None:
        """Update the correlator receiver configuration.

        ``bin_map`` is the only static attribute: changing it triggers reallocation of
        packet-buffer views, the per-packet ``temp32`` scratch, and all integ-indexed
        accumulators / output buffers. Everything else is dynamic and may be applied
        with no buffer reallocation (or only ``max_timestamps`` resizing when the integration
        period or per-integ packet count changes).
        """
        source = self._config_getter()  # This call must be fast, it is made on every processing call
        if self.config is None:
            self.config = source
        elif source is not self.config:
            raise RuntimeError("config_getter must return a stable State")
        config = self.config

        from pychfpga.fpga_firmware.chfpga.x_engine.UCORR import UCORR

        # get independent parameters from the configuration
        self.NCHAN = config.NCHAN
        self.NCLK = config.NCLK

        self.NPROD = self.NCHAN * (self.NCHAN + 1) // 2

        self.bin_map = np.asarray(config.bin_map)
        self.prod_map = np.asarray(config.prod_map)
        conj_map = getattr(config, 'conj_map', None)  # absent in hand-built configs: nothing to un-conjugate
        self.conj_map = None if conj_map is None else np.asarray(conj_map, dtype=bool)
        self.NDECIM = config.NDECIM
        self.NCORRS = config.NCORRS
        self.NPROD_PER_CMAC = config.NPROD_PER_CMAC
        self.NCMAC = config.NCMAC
        self.NBINS = config.NBINS
        self.NPROD_PER_ARRAY = config.NPROD_PER_ARRAY



        # Static attributes changes cause loss of data as the main integ buffer needs to be resized. 
        buf_view_attrs = ("NCMAC",)
        if config.has_changed(buf_view_attrs, context=buf_view_attrs):
            if self.verbose:
                print("UCorrPacketProcessor: bin_map changed; rebuilding views and buffers.")

            # Packet view depends on NCMAC.
            # Was one record per datagram (no sub-packet axis):
            #     self.buf_struct = self.pr.buf[:, :self.packet_dtype.itemsize].view(self.packet_dtype)[:, 0]
            #     self.buf_cookie = self.buf_struct["header"]["cookie"][:, 0]          # (n_packets,)
            #     self.buf_flags = self.buf_struct["header"]["flags"][:, 0]
            #     self.buf_stream_id = self.buf_struct["header"]["stream_id"][:, 0]
            #     self.buf_ts = self.buf_struct["header"]["ts"][:, 0]
            #     self.buf_data_h = self.buf_struct["data"]["h"]    # (n_packets, NCMAC)
            #     self.buf_data_l = self.buf_struct["data"]["l"]
            #     self.buf_data_sat = self.buf_struct["data"]["sat"]
            #     self.temp32 = self.resize_buffer("temp32_buf", (self.pr.n_packets, NCMAC), np.int32)
            NCMAC = config.NCMAC
            self.packet_dtype = np.dtype([("header", self.header_dtype, (1,)), ("data", self.product_dtype, (NCMAC,))])
            # Each datagram now holds up to FRAMES_PER_PACKET aggregated records
            # (firmware TLAST gating). View the leading sz*nf bytes of every buffer
            # row as an (n_packets, nf) record grid; these are zero-copy strided
            # views into pr.buf, so NEVER reshape/ravel the product views below
            # (element stride 5 B, record stride sz B, row stride buf-row) — that
            # would silently copy the whole buffer. Gather with two indices instead.
            sz = self.packet_dtype.itemsize
            nf = self.FRAMES_PER_PACKET
            if sz * nf > self.pr.buf.shape[1]:
                raise ValueError(
                    f"UCorrPacketProcessor: {nf} frames x {sz} B = {sz * nf} B exceeds "
                    f"UDP buffer row of {self.pr.buf.shape[1]} B"
                )
            self.buf_struct = self.pr.buf[:, :sz * nf].view(self.packet_dtype)   # (n_packets, nf)
            self.buf_cookie = self.buf_struct["header"]["cookie"][:, :, 0]       # (n_packets, nf)
            self.buf_flags = self.buf_struct["header"]["flags"][:, :, 0]         # (n_packets, nf)
            self.buf_stream_id = self.buf_struct["header"]["stream_id"][:, :, 0] # (n_packets, nf)
            self.buf_ts = self.buf_struct["header"]["ts"][:, :, 0]               # (n_packets, nf)
            self.buf_data_h = self.buf_struct["data"]["h"]    # (n_packets, nf, NCMAC)
            self.buf_data_l = self.buf_struct["data"]["l"]    # (n_packets, nf, NCMAC)
            self.buf_data_sat = self.buf_struct["data"]["sat"]  # (n_packets, nf, NCMAC)

            # Per-frame sign-extend scratch (one int32 lane per CMAC entry), sized to
            # the flat frame space so it can be sliced by the number of selected frames.
            self.temp32 = self.resize_buffer("temp32_buf", (self.pr.n_packets * nf, NCMAC), np.int32)

            self.n_ts = 0
            self.carry_len = 0
            config.ack(buf_view_attrs, context=buf_view_attrs)

        # Dynamic: all non-shape attributes. NCHAN drives ij_to_prod_map; integ-period
        # drives max_timestamps which sizes the linear accumulators / output buffers.
        ij_attrs = ("NCHAN",)
        if config.has_changed(ij_attrs, context=ij_attrs):
            self.ij_to_prod_map = UCORR.get_ij_to_prod_map(self.NCHAN)
            config.ack(ij_attrs, context=ij_attrs)

        firm_integ_attrs = (
            "firmware_integ_period",
            "NPROD_PER_ARRAY",
            "NDECIM",
            "NCORRS",
            "NPROD_PER_CMAC",
            "NCMAC",
            "NBINS",
        )
        firm_integ_changed = config.has_changed(firm_integ_attrs, context=firm_integ_attrs)
        soft_integ_changed = self.soft_integ_period != self._applied_soft_integ_period
        if firm_integ_changed or soft_integ_changed:
            # If we now have enough info, (re)size the integ-indexed buffers.
            if self.verbose:
                print(f"UCorrPacketProcessor:  rebuilding integration buffers.")
            self.firmware_integ_period = config.firmware_integ_period

            self.integ_period = np.uint64(self.soft_integ_period * self.firmware_integ_period)
            self.expected_packets = self.NPROD_PER_ARRAY * self.soft_integ_period
            self.max_timestamps = math.ceil(self.pr.n_packets / max(1, self.expected_packets)) + self.receive_window

            # Accumulator dtype: each packet product is an 18-bit signed value (|v| < 2**17)
            # and a row sums only soft_integ_period of them, so int32 (|.| < 2**31) is safe
            # while soft_integ_period * 2**17 stays < 2**31 (i.e. <= 16384). int32 halves the
            # memory bandwidth of the store scatter-add and the format read (both bandwidth
            # bound on these ~100 MiB buffers); fall back to int64 for huge soft integrations.
            # Was always:
            #     self.acc_re = ...(..., np.int64)
            #     self.acc_im = ...(..., np.int64)
            #     self.integ_data = ...(..., np.complex128)
            acc_dtype = np.int32 if self.soft_integ_period <= 8192 else np.int64
            self.acc_re = self.resize_buffer("acc_re_buf", (self.max_timestamps, self.NDECIM, self.NCORRS, self.NPROD_PER_CMAC, self.NCMAC), acc_dtype)
            self.acc_im = self.resize_buffer("acc_im_buf", (self.max_timestamps, self.NDECIM, self.NCORRS, self.NPROD_PER_CMAC, self.NCMAC), acc_dtype)
            self.sat = self.resize_buffer("sat_buf", (self.max_timestamps, self.NDECIM, self.NCORRS, self.NPROD_PER_CMAC, self.NCMAC, 2), np.int32)
            self.raw_data_count = self.resize_buffer("raw_data_count_buf", (self.max_timestamps,), np.int64)
            self.ts_array = self.resize_buffer("integ_array_buf", (self.max_timestamps,), np.uint64)
            self.integ_data = self.resize_buffer("integ_data_buf", (self.max_timestamps, self.NBINS, self.NPROD), self.OUTPUT_COMPLEX_DTYPE)
            self.integ_sats = self.resize_buffer("integ_sats_buf", (self.max_timestamps, self.NBINS, self.NPROD), np.complex64)
            self.overrun_row = self.resize_buffer("overrun_row_buf", (self.max_timestamps,), np.uint8)
            self.acc_re.fill(0)
            self.acc_im.fill(0)
            self.sat.fill(0)
            self.raw_data_count.fill(0)
            self.overrun_row.fill(0)
            self.overrun = 0
            self.n_ts = 0
            self.carry_len = 0
            if firm_integ_changed:
                config.ack(firm_integ_attrs, context=firm_integ_attrs)
            self._applied_soft_integ_period = self.soft_integ_period

    def process_udp_packets(self, verbose=None, update_config=True):
        if update_config:
            self.update_config()
        t0 = time.perf_counter()
        super().process_udp_packets(verbose=verbose)
        self.profile["process_s"] += time.perf_counter() - t0
        self.profile["process_calls"] += 1

    def get_ij_to_prod_map(self):
        """Return the ``(i, j)`` → linear product-index map for the current ``NCHAN``.

        Reads ``NCHAN`` from the correlator config returned by ``config_getter``.
        Does not call :meth:`update_config` (no packet-buffer reallocation).
        """
        from pychfpga.fpga_firmware.chfpga.x_engine.UCORR import UCORR
        config = self._config_getter()
        return UCORR.get_ij_to_prod_map(int(config.NCHAN))

    def get_good_bin_mask(self):
        """Return a boolean map of FFT bins expected to contain valid correlator data.

        True for bins present in ``bin_map``, except correlator sacrificial
        readout bins (stolen for the accumulation-memory readout cycle). Culled
        bins (e.g. the first 256 FFT bins dropped at CT level 2) stay False
        because they are absent from ``bin_map``.

        ``READOUT_BIN`` is a compile-time generic and does not follow FFT
        bypass. ``config.READOUT_BINS`` is one clkbin per correlator (from
        ``chFPGA.CORR_READOUT_BINS``).

        The mask length is ``NBINS`` (typically 8192). Reads ``bin_map``,
        ``NBINS``, and ``READOUT_BINS`` from the correlator config. Does not
        call :meth:`update_config`.
        """
        config = self._config_getter()
        bin_map = np.asarray(config.bin_map)
        n_bins = int(bin_map.max()) + 1
        if config.has_attr("NBINS"):
            n_bins = max(n_bins, int(config.NBINS))
        mask = np.zeros(n_bins, dtype=bool)
        mask[bin_map.reshape(-1)] = True
        # Product index does not matter; the FFT bin is the same for every CMAC.
        n_corrs = bin_map.shape[1]
        readout_bins = config.READOUT_BINS if config.has_attr("READOUT_BINS") else None
        if readout_bins is None or len(readout_bins) != n_corrs:
            raise ValueError(
                f"READOUT_BINS length {0 if readout_bins is None else len(readout_bins)} "
                f"does not match number of correlators {n_corrs}"
            )
        for corr, clkbin in enumerate(readout_bins):
            mask[bin_map[:, corr, clkbin, 0]] = False
        return mask

    def create_output_buffer(self, n_ts):
        """Allocate caller-side output arrays matching the shapes produced by :meth:`get_formatted_data`."""
        ts = np.empty((n_ts,), dtype=self.ts_array.dtype)
        data = np.empty((n_ts, self.NBINS, self.NPROD), dtype=self.integ_data.dtype)
        sats = np.empty((n_ts, self.NBINS, self.NPROD), dtype=self.integ_sats.dtype)
        counts = np.empty((n_ts,), dtype=self.raw_data_count.dtype)
        return ts, data, counts, sats

    def filter_packets(self, n: int) -> Optional[np.ndarray]:
        """Decode UCORR headers; leave integration index in ``pkt_ts``; return frame indices.

        Each of the ``n`` received datagrams packs up to ``FRAMES_PER_PACKET``
        aggregated records (firmware TLAST gating). We decode over the flat
        ``(n, nf)`` record grid and return a 1D *linear* frame index ``bix`` into
        the ``M = n_packets * nf`` scratch space, so the base-class pipeline
        (``pkt_ts[bix]``, ``bix_mask[bix]``, unique-timestamp merge) is reused as-is.
        """
        # Decode over an (n, nf) record grid. Was one record per datagram:
        #     bix_mask = self.bix_mask[:n]
        #     np.equal(self.buf_cookie[:n], self.CORR_PACKET_COOKIE, out=bix_mask)
        #     np.right_shift(self.buf_stream_id[:n], 11, out=self.pkt_corr[:n], where=bix_mask)
        #     np.bitwise_and(self.buf_stream_id[:n], self.bin_mask, out=self.pkt_bin[:n], where=bix_mask)
        #     np.right_shift(self.buf_flags[:n], 4, out=self.pkt_bin_set[:n], where=bix_mask)
        #     np.bitwise_and(self.buf_ts[:n], self.ts_mask, out=self.pkt_ts[:n], where=bix_mask)
        #     np.floor_divide(self.pkt_ts[:n], self.integ_period, out=self.pkt_ts[:n], where=bix_mask)
        #     np.greater(self.pkt_ts[:n], self._last_emitted_ts, out=tmp_mask, where=bix_mask)
        #     np.bitwise_and(bix_mask, tmp_mask, out=bix_mask)
        #     return np.flatnonzero(bix_mask)
        # The reshape views below write the same pkt_* / bix_mask flat arrays; bix is
        # then a linear frame index (datagram * nf + sub-packet) instead of a datagram row.
        nf = self.FRAMES_PER_PACKET
        m = n * nf
        sz = self.packet_dtype.itemsize
        # (n, nf) views of the flat scratch — no copy; writes land in the flat arrays.
        mask = self.bix_mask[:m].reshape(n, nf)
        corr = self.pkt_corr[:m].reshape(n, nf)
        bin_ = self.pkt_bin[:m].reshape(n, nf)
        binset = self.pkt_bin_set[:m].reshape(n, nf)
        ts = self.pkt_ts[:m].reshape(n, nf)

        # Validity = correct cookie AND the record slot actually arrived. A genuine
        # aggregated corr datagram is EXACTLY n_rec*sz bytes (firmware shifts sz bytes
        # per bin with no padding; the final group may be < nf bins). We therefore
        # accept records only when pkt_len is an exact multiple of sz, up to nf slots.
        # A non-multiple length means the datagram is not a clean corr frame (foreign
        # packet, or a size mismatch) — reject it wholesale rather than trust a floored
        # pkt_len//sz, which could otherwise admit a stale/garbage slot whose byte at a
        # record boundary happens to equal the cookie. The per-slot cookie test below
        # is kept as a second line of defense (and masks the short final group). This
        # replaces the old "header layout fixed, no size check needed" assumption now
        # that a datagram holds a variable number of records.
        pkt_len = self.pr.pkt_len[:n]
        n_rec = np.where((pkt_len % sz) == 0, pkt_len // sz, 0)
        np.equal(self.buf_cookie[:n], self.CORR_PACKET_COOKIE, out=mask)
        n_cookie = int(mask.sum()) if self.verbose >= 2 else 0
        mask &= np.arange(nf) < n_rec[:, None]

        # Decode header fields. stream_id top 5 bits = corr id, low 11 bits = bin index, flags hi nibble = bin_set.
        np.right_shift(self.buf_stream_id[:n], 11, out=corr, where=mask)
        np.bitwise_and(self.buf_stream_id[:n], self.bin_mask, out=bin_, where=mask)
        np.right_shift(self.buf_flags[:n], 4, out=binset, where=mask)
        np.bitwise_and(self.buf_ts[:n], self.ts_mask, out=ts, where=mask)
        # integ_period is uint64 — keep dividend uint64 to avoid float promotion.
        np.floor_divide(ts, self.integ_period, out=ts, where=mask)

        # Drop packets at or before the latest integration already returned (monotonic emits).
        tmp_mask = self.tmp_mask[:m].reshape(n, nf)
        np.greater(ts, self._last_emitted_ts, out=tmp_mask, where=mask)
        np.bitwise_and(mask, tmp_mask, out=mask)

        if self.verbose >= 2:
            # Datagrams whose length is not a clean multiple of the record size are
            # rejected above; a large count here usually means sz (NCMAC) disagrees
            # with the firmware, so surface it to make that failure mode obvious.
            n_badlen = int(((pkt_len % sz) != 0).sum())
            bix = np.flatnonzero(self.bix_mask[:m])
            print(f"UCorrPacketProcessor: filter sz={sz} badlen={n_badlen}/{n}")
            if bix.size:
                p_idx, s_idx = np.divmod(bix, nf)
                hw_ts = np.bitwise_and(self.buf_ts[p_idx, s_idx], self.ts_mask)
                bin_sets = np.unique(self.pkt_bin_set[bix])
                fw_ts = hw_ts // self.firmware_integ_period
                print(
                    f"UCorrPacketProcessor: filter cookie={n_cookie} accepted={bix.size}/{n} "
                    f"bin_set={bin_sets.tolist()} corr={np.unique(self.pkt_corr[bix]).tolist()} "
                    f"clkbin={int(self.pkt_bin[bix].min())}-{int(self.pkt_bin[bix].max())} "
                    f"integ_idx={int(self.pkt_ts[bix].min())}-{int(self.pkt_ts[bix].max())} "
                    f"fw_integ={int(fw_ts.min())}-{int(fw_ts.max())} "
                    f"hw_ts={int(hw_ts.min())}-{int(hw_ts.max())} "
                    f"soft_integ={self.soft_integ_period} fw_period={self.firmware_integ_period} "
                    f"expected_pkt/integ={self.expected_packets} last_emitted={int(self._last_emitted_ts)}"
                )
            else:
                print(
                    f"UCorrPacketProcessor: filter cookie={n_cookie} accepted=0/{n} "
                    f"last_emitted={int(self._last_emitted_ts)}"
                )
        return np.flatnonzero(self.bix_mask[:m])

    def on_ts_overflow(self, dropped: int) -> None:
        """Warn when integration keys are dropped because the buffer is full."""
        warnings.warn(f"UCorrPacketProcessor: {dropped} feel out of the buffer.")

    def init_dest_rows(self, n_ts, cix, carry_tix):
        """Relocate carryover rows to their new rows and zero the rest.
        
        Parameters
        ----------
        n_ts (int): Number of timestamps in the new output buffer.
        cix (slice): Indices of the carryover rows in the old output buffer.
        carry_tix (np.ndarray): Indices of the carryover rows in the new output buffer.

        """
        t0 = time.perf_counter()
        tix_to_clear = np.ones(n_ts, dtype=bool)
        cix_mask = carry_tix >= 0  # find indices of carryover timestamps that were not dropped
        if cix_mask.any():
            src_tix = np.arange(cix.start, cix.stop)[cix_mask]
            dst_tix = carry_tix[cix_mask]
            tix_to_clear[dst_tix] = False  # don't clear the carryover rows
            # Identity maps are the common case (one in-flight integration): a
            # fancy-index copy still memcpy's a full corr64 row (~126 MiB × 3
            # arrays, ~75 ms) and overruns the 2048-packet UDP budget.
            if src_tix.size and not np.array_equal(src_tix, dst_tix):
                t_move = time.perf_counter()
                if self.verbose:
                    print(
                        f"UCorrPacketProcessor: preserving carryover state for {cix_mask.sum()} "
                        f"carryover timestamps {cix_mask=}, {carry_tix[cix_mask]=}."
                    )
                self.acc_re[dst_tix] = self.acc_re[src_tix]
                self.acc_im[dst_tix] = self.acc_im[src_tix]
                self.sat[dst_tix] = self.sat[src_tix]
                self.raw_data_count[dst_tix] = self.raw_data_count[src_tix]
                self.overrun_row[dst_tix] = self.overrun_row[src_tix]
                self.profile["init_dest_move_s"] += time.perf_counter() - t_move
            elif self.verbose >= 2:
                print(
                    f"UCorrPacketProcessor: identity carryover {src_tix.tolist()} "
                    f"-> {dst_tix.tolist()}; skip row copy."
                )

        if tix_to_clear.any():
            n = tix_to_clear.size
            self.acc_re[:n][tix_to_clear] = 0
            self.acc_im[:n][tix_to_clear] = 0
            self.sat[:n][tix_to_clear] = 0
            self.raw_data_count[:n][tix_to_clear] = 0
            self.overrun_row[:n][tix_to_clear] = 0
        self.profile["init_dest_s"] += time.perf_counter() - t0

    def store_packets(self, bix: np.ndarray, tix: np.ndarray) -> None:
        """Accumulate correlation products from valid packets into ``acc_re`` / ``acc_im``."""
        # integrate the packets into the accumulators
        # Per-row overrun (LSB of flags). Do not OR into a process-wide latch:
        # that poisoned later emits from carry packets / incomplete batches.
        t0 = time.perf_counter()
        # bix is a flat frame index; decompose it into (datagram, sub-packet) to
        # gather the product/flags views, which now carry an extra sub-packet axis
        # (n_packets, nf, ...). buf_*[p_idx, s_idx] produces the same (n_valid, ...)
        # transient the old buf_*[bix] did — not a new copy. The per-frame header
        # scratch (pkt_*) stays indexed by the flat bix.
        # Was (one record per datagram, bix = UDP row):
        #     np.bitwise_or.at(self.overrun_row, tix, (self.buf_flags[bix] & 1)...)
        #     np.left_shift(self.buf_data_h[bix], 4, out=temp32)
        #     np.left_shift(self.buf_data_l[bix], 14, out=temp32)
        #     sat_pkt = self.buf_data_sat[bix]
        p_idx, s_idx = np.divmod(bix, self.FRAMES_PER_PACKET)

        np.bitwise_or.at(
            self.overrun_row, tix, (self.buf_flags[p_idx, s_idx] & np.uint8(1)).astype(np.uint8, copy=False)
        )

        bin_set = self.pkt_bin_set[bix]
        corr = self.pkt_corr[bix]
        bin_ = self.pkt_bin[bix]

        temp32 = self.temp32[:len(bix)]
        # Sign-extend the 18-bit packed real/imag fields into preallocated int32 lanes.
        #   real: bits 27:10 of buf_data_h  -> shift<<4 then >>14 sign-extends from bit 31.
        #   imag: bits 17:0  of buf_data_l  -> shift<<14 then >>14 sign-extends from bit 31.
        np.left_shift(self.buf_data_h[p_idx, s_idx], 4, out=temp32)
        np.right_shift(temp32, 14, out=temp32)
        # Fancy-index += matches the original behavior; relies on (tix, bin_set, corr, bin) tuples
        # being unique within a single batch (one frame per (corr,bin) per soft integration).
        self.acc_re[tix, bin_set, corr, bin_] += temp32

        np.left_shift(self.buf_data_l[p_idx, s_idx], 14, out=temp32)
        np.right_shift(temp32, 14, out=temp32)
        # Firmware products flagged in conj_map are X_j conj(X_i): negate imag to the V_ij = X_i conj(X_j) convention
        if self.conj_map is not None:
            np.negative(temp32, out=temp32, where=self.conj_map[corr, bin_])
        self.acc_im[tix, bin_set, corr, bin_] += temp32

        # Sat counters: bit 0x20 = real saturation, bit 0x10 = imag saturation.
        # Copy into ``temp32`` first: ``temp32 = sat_pkt`` only aliases, so the
        # in-place ``&=`` used to clobber ``sat_pkt`` and zero the imag flag.
        sat_pkt = self.buf_data_sat[p_idx, s_idx]
        temp32[:] = sat_pkt
        np.bitwise_and(temp32, 0x20, out=temp32)
        np.right_shift(temp32, 5, out=temp32)
        self.sat[tix, bin_set, corr, bin_, :, 0] += temp32
        temp32[:] = sat_pkt
        np.bitwise_and(temp32, 0x10, out=temp32)
        np.right_shift(temp32, 4, out=temp32)
        self.sat[tix, bin_set, corr, bin_, :, 1] += temp32

        np.add.at(self.raw_data_count, tix, 1)
        self.profile["store_s"] += time.perf_counter() - t0

        if self.verbose >= 2 and len(bix):
            for tix_val in np.unique(tix):
                row_mask = tix == tix_val
                print(
                    f"UCorrPacketProcessor: store integ_row={int(tix_val)} "
                    f"n_packets={int(row_mask.sum())} "
                    f"bin_set={np.unique(bin_set[row_mask]).tolist()} "
                    f"corr={np.unique(corr[row_mask]).tolist()} "
                    f"clkbin={int(bin_[row_mask].min())}-{int(bin_[row_mask].max())}"
                )

    def on_emit(self):
        """Log bin-set coverage when integration rows are ready (verbose>=2)."""
        super().on_emit()
        # Warn only for rows actually emitted this call; carry keeps its flag.
        self.overrun = int(self.overrun_row[self.out_rows].any()) if self.n_out else 0
        if self.verbose >= 2:
            for row in range(self.out_rows.start, self.out_rows.stop):
                active_bin_sets = [
                    bs for bs in range(self.NDECIM)
                    if self.acc_re[row, bs].any()
                ]
                print(
                    f"UCorrPacketProcessor: emit row={row} integ_key={int(self.ts_array[row])} "
                    f"pkt_count={int(self.raw_data_count[row])}/{self.expected_packets} "
                    f"active_bin_sets={active_bin_sets} NDECIM={self.NDECIM}"
                )
            print(
                f"UCorrPacketProcessor: emitted {self.n_out} integ(s), "
                f"carry={self.carry_len}, overrun={self.overrun}"
            )
        if self.overrun:
            print(
                "*** Warning: the correlator experienced an overrun condition. "
                "All products could not be sent within the hardware integration period."
            )
            self.overrun = 0

    def _ensure_flat_scatter_idx(self):
        """Return (and cache) the 1-D scatter index mapping acc lanes to (NBINS, NPROD).

        ``bin_map``/``prod_map`` broadcast against the accumulator trailing shape
        ``(NDECIM, NCORRS, NPROD_PER_CMAC, NCMAC)`` and select an output cell per lane.
        Collapsing that into a single precomputed ``bin*NPROD + prod`` index lets
        :meth:`get_formatted_data` do one flat complex scatter per array instead of
        rebuilding a broadcast 4-D fancy index (real and imag) on every call — that
        index construction, not the memory write, was the format-stage bottleneck.
        Recomputed only when the maps, NPROD, or accumulator shape change.
        """
        trailing = self.acc_re.shape[1:]
        key = (id(self.bin_map), id(self.prod_map), self.NPROD, trailing)
        if getattr(self, "_fmt_idx_key", None) != key:
            bm = np.broadcast_to(self.bin_map, trailing).ravel()
            pm = np.broadcast_to(self.prod_map, trailing).ravel()
            self._fmt_flat_idx = bm.astype(np.intp) * self.NPROD + pm.astype(np.intp)
            self._fmt_idx_key = key
        return self._fmt_flat_idx

    def get_formatted_data(self, rows=slice(None), **kwargs):
        """Return formatted integration rows assembled by the last ``process_udp_packets`` call.

        Remaps accumulator rows into ``(NBINS, NPROD)`` complex outputs. ``acc_re``/``acc_im``/``sat``
        are indexed by ``(integ, NDECIM, NCORRS, NPROD_PER_CMAC, NCMAC)``; ``bin_map`` (4D) and
        ``prod_map`` (3D) broadcast together to place each product into the dense ``(NBINS, NPROD)``
        plane. The scatter uses a single precomputed flat index (see
        :meth:`_ensure_flat_scatter_idx`): we build a compact ``(nrows, K)`` complex source
        from the real/imag accumulators, then scatter it once per output array. The
        output rows are cleared first so cells the maps do not cover stay zero
        rather than leftover data from the previous integration.

        Parameters:
            rows (slice or int): Physical index rows in the internal assembly buffer.
                ``None`` or ``slice(None)`` selects ``out_rows``. An ``int`` selects
                ``slice(0, rows)``, then clipped to ``out_rows``.

        Returns:
            None if no rows are ready; otherwise ``(integ_ts, data, counts, sats)`` where
            ``integ_ts`` and ``counts`` have shape ``(n,)``, and ``data`` and ``sats`` have
            shape ``(n, NBINS, NPROD)``.

        Raises:
            TypeError: If ``source`` or ``format`` keyword arguments are passed.
        """
        if kwargs:
            if "source" in kwargs or "format" in kwargs:
                raise TypeError("UCorrPacketProcessor.get_formatted_data does not accept source or format")
            raise TypeError(f"unexpected keyword arguments: {list(kwargs)}")
        rows = self.clip_out_rows(rows)
        if rows is None:
            return None
        t0 = time.perf_counter()
        integ_ts = self.ts_array[rows]
        counts = self.raw_data_count[rows]

        # Equivalent to the original broadcast fancy-index scatter:
        #     ix = (rows, self.bin_map, self.prod_map)
        #     self.integ_data.real[ix] = self.acc_re[rows]
        #     self.integ_data.imag[ix] = self.acc_im[rows]
        #     self.integ_sats.real[ix] = self.sat[rows, ..., 0]
        #     self.integ_sats.imag[ix] = self.sat[rows, ..., 1]
        # bin_map/prod_map broadcast against acc trailing dims and pick (bin, prod)
        # in the dense output. Below is the same mapping with a cached 1-D index
        # (flat = bin*NPROD + prod) so NumPy does not rebuild that 4-D index on
        # every call. .real/.imag of integ_* must still be written via views that
        # share memory with the dest (here: scatter into a reshape of integ_*).
        flat = self._ensure_flat_scatter_idx()
        nrows = rows.stop - rows.start
        K = flat.size
        # Flat (max_ts, NBINS*NPROD) views over the dense output buffers (zero-copy;
        # both are C-contiguous). Scatter along the flattened bin/prod axis.
        data_flat = self.integ_data.reshape(self.max_timestamps, -1)
        sats_flat = self.integ_sats.reshape(self.max_timestamps, -1)
        # Unmapped cells used to keep the previous integration. Clear the rows
        # before scattering so only products present in the maps are nonzero.
        data_flat[rows] = 0
        sats_flat[rows] = 0

        # Compact complex source built from the contiguous real/imag accumulators, then
        # scattered once (one complex write instead of separate strided real/imag writes).
        src = np.empty((nrows, K), self.integ_data.dtype)
        src.real = self.acc_re[rows].reshape(nrows, K)
        src.imag = self.acc_im[rows].reshape(nrows, K)
        data_flat[rows][:, flat] = src  # was: integ_data.real/imag[ix] = acc_re/acc_im[rows]

        ssrc = np.empty((nrows, K), self.integ_sats.dtype)
        ssrc.real = self.sat[rows, ..., 0].reshape(nrows, K)
        ssrc.imag = self.sat[rows, ..., 1].reshape(nrows, K)
        sats_flat[rows][:, flat] = ssrc  # was: integ_sats.real/imag[ix] = sat[..., 0/1]

        data = self.integ_data[rows]
        sats = self.integ_sats[rows]
        self.profile["format_s"] += time.perf_counter() - t0
        self.profile["format_calls"] += 1
        return integ_ts, data, counts, sats

    def flush(self, flush_timeout=None):
        """Drain UDP and clear assembly state for a clean restart."""
        if flush_timeout is None:
            self.pr.flush()
        else:
            self.pr.flush(flush_timeout)
        if self.bin_map is not None and self.firmware_integ_period is not None:
            n = self.n_ts or 0
            if n:
                self.acc_re[:n] = 0
                self.acc_im[:n] = 0
                self.sat[:n] = 0
                self.raw_data_count[:n] = 0
                self.overrun_row[:n] = 0
                self.ts_array[:n] = 0
        self.n_ts = 0
        self.carry_len = 0
        self._last_emitted_ts = np.uint64(0)
        self._set_out_rows(0, 0)
        self.overrun = 0
