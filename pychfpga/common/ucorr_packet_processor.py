"""UCORR UDP packet processor."""

import math
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


    def __init__(self, packet_receiver, config=None, receive_window=3, verbose=0):
        """Build a packet parser and linear integ-buffer pipeline.

        Parameters
        ----------
        packet_receiver : UDPPacketReceiver
            UDP packet source holding the shared packet buffer.
        config : dict or State or None
            Initial correlator configuration (see class docstring). Deep-copied into
            an internal :class:`State` so the caller can mutate their copy freely.
        receive_window : int
            Maximum number of trailing incomplete integration rows withheld between
            UDP packet blocks (carried into the next ``process_packets`` call).
        verbose : int
            Verbosity level for debug prints.
        """
        self.config = State()
        self.NCHAN = None
        self.NCLK = None
        self.bin_map = None
        self.prod_map = None
        self.firmware_integ_period = None
        self.soft_integ_period = 1
        super().__init__(packet_receiver, receive_window, verbose)
        if config is not None:
            self.update_config(State(config).deepcopy())

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

        n_packets = self.pr.n_packets
        self.pkt_corr = np.empty((n_packets,), dtype=np.uint16)
        self.pkt_bin = np.empty((n_packets,), dtype=np.uint16)
        self.pkt_bin_set = np.empty((n_packets,), dtype=np.uint8)

        # Linear buffers, sized in update_config().
        self.acc_re_buf = np.empty(0, dtype=np.uint8)
        self.acc_im_buf = np.empty(0, dtype=np.uint8)
        self.sat_buf = np.empty(0, dtype=np.uint8)
        self.raw_data_count_buf = np.empty(0, dtype=np.uint8)
        self.integ_array_buf = np.empty(0, dtype=np.uint8)
        self.integ_data_buf = np.empty(0, dtype=np.uint8)
        self.integ_sats_buf = np.empty(0, dtype=np.uint8)
        self.temp32_buf = np.empty(0, dtype=np.uint8)
        self.ts_array = np.empty(0, dtype=np.uint64)
        self.max_timestamps = 0

    def update_config(self, config=None) -> None:
        """Update the correlator receiver configuration.

        ``bin_map`` is the only static attribute: changing it triggers reallocation of
        packet-buffer views, the per-packet ``temp32`` scratch, and all integ-indexed
        accumulators / output buffers. Everything else is dynamic and may be applied
        with no buffer reallocation (or only ``max_timestamps`` resizing when the integration
        period or per-integ packet count changes).
        """
        if config is None:
            return  # hot-path fast exit: skip the deep-copy snapshots in has_changed when nothing changed
        self.config.update(config)
        state = self.config

        from pychfpga.fpga_firmware.chfpga.x_engine.UCORR import UCORR

        # get independent parameters from the configuration
        self.NCHAN = state.NCHAN
        self.NCLK = state.NCLK

        self.NPROD = self.NCHAN * (self.NCHAN + 1) // 2

        # Update the correlator bin map, product map and derived geometry parameters based on the current configuration if the dependent parameters have changed.
        UCORR.update_corr_maps(self.config)
        self.bin_map = state.bin_map
        self.prod_map = state.prod_map
        self.NDECIM = state.NDECIM
        self.NCORRS = state.NCORRS
        self.NPROD_PER_CMAC = state.NPROD_PER_CMAC
        self.NCMAC = state.NCMAC
        self.NBINS = state.NBINS
        self.NPROD_PER_ARRAY = state.NPROD_PER_ARRAY



        # Static attributes changes cause loss of data as the main integ buffer needs to be resized. 
        buf_view_attrs = tuple("NCMAC",)
        if state.has_changed(buf_view_attrs, context=buf_view_attrs):
            if self.verbose:
                print("UCorrPacketProcessor: bin_map changed; rebuilding views and buffers.")

            # Packet view depends on NCMAC.
            NCMAC = state.NCMAC
            self.packet_dtype = np.dtype([("header", self.header_dtype, (1,)), ("data", self.product_dtype, (NCMAC,))])
            self.buf_struct = self.pr.buf[:, :self.packet_dtype.itemsize].view(self.packet_dtype)[:, 0]
            self.buf_cookie = self.buf_struct["header"]["cookie"][:, 0]
            self.buf_flags = self.buf_struct["header"]["flags"][:, 0]
            self.buf_stream_id = self.buf_struct["header"]["stream_id"][:, 0]
            self.buf_ts = self.buf_struct["header"]["ts"][:, 0]
            self.buf_data_h = self.buf_struct["data"]["h"]    # (NPACKETS, NCMAC)
            self.buf_data_l = self.buf_struct["data"]["l"]    # (NPACKETS, NCMAC)
            self.buf_data_sat = self.buf_struct["data"]["sat"]  # (NPACKETS, NCMAC)

            # Per-packet sign-extend scratch (one int32 lane per CMAC entry).
            self.temp32 = self.resize_buffer("temp32_buf", (self.pr.n_packets, NCMAC), np.int32)

            self.n_ts = 0
            self.carry_len = 0
            state.ack(buf_view_attrs, context=buf_view_attrs)

        # Dynamic: all non-shape attributes. NCHAN drives ij_to_prod_map; integ-period
        # drives max_timestamps which sizes the linear accumulators / output buffers.
        ij_attrs = ("NCHAN",)
        if state.has_changed(ij_attrs, context=ij_attrs):
            self.ij_to_prod_map = UCORR.get_ij_to_prod_map(self.NCHAN)
            state.ack(ij_attrs, context=ij_attrs)

        integ_attrs = ("firmware_integ_period", "soft_integ_period", "NPROD_PER_ARRAY", "NDECIM", "NCORRS", "NPROD_PER_CMAC", "NCMAC", "NBINS")
        if state.has_changed(integ_attrs, context=integ_attrs):
            # If we now have enough info, (re)size the integ-indexed buffers.
            if self.verbose:
                print(f"UCorrPacketProcessor:  rebuilding integration buffers.")
            self.firmware_integ_period = state.firmware_integ_period
            self.soft_integ_period = state.soft_integ_period

            self.integ_period = np.uint64(self.soft_integ_period * self.firmware_integ_period)
            self.expected_packets = self.NPROD_PER_ARRAY * self.soft_integ_period
            self.max_timestamps = math.ceil(self.pr.n_packets / max(1, self.expected_packets)) + self.receive_window

            self.acc_re = self.resize_buffer("acc_re_buf", (self.max_timestamps, self.NDECIM, self.NCORRS, self.NPROD_PER_CMAC, self.NCMAC), np.int64)
            self.acc_im = self.resize_buffer("acc_im_buf", (self.max_timestamps, self.NDECIM, self.NCORRS, self.NPROD_PER_CMAC, self.NCMAC), np.int64)
            self.sat = self.resize_buffer("sat_buf", (self.max_timestamps, self.NDECIM, self.NCORRS, self.NPROD_PER_CMAC, self.NCMAC, 2), np.int32)
            self.raw_data_count = self.resize_buffer("raw_data_count_buf", (self.max_timestamps,), np.int64)
            self.ts_array = self.resize_buffer("integ_array_buf", (self.max_timestamps,), np.uint64)
            self.integ_data = self.resize_buffer("integ_data_buf", (self.max_timestamps, self.NBINS, self.NPROD), np.complex128)
            self.integ_sats = self.resize_buffer("integ_sats_buf", (self.max_timestamps, self.NBINS, self.NPROD), np.complex64)
            self.acc_re.fill(0)
            self.acc_im.fill(0)
            self.sat.fill(0)
            self.raw_data_count.fill(0)
            self.n_ts = 0
            self.carry_len = 0
            state.ack(integ_attrs, context=integ_attrs)

    def get_ij_to_prod_map(self):
        return self.ij_to_prod_map

    def format_data(self, n_out: int):
        """Remap leading ``n_out`` accumulator rows into ``(NBINS, NPROD)`` complex outputs.

        ``acc_re``/``acc_im``/``sat`` are indexed by ``(integ, NDECIM, NCORRS, NPROD_PER_CMAC, NCMAC)``;
        ``bin_map`` (4D) and ``prod_map`` (3D) broadcast together to scatter products
        into the dense ``(NBINS, NPROD)`` plane. Beware: the .real/.imag view must be
        taken *before* the fancy-index assignment, otherwise the write hits a copy.
        """
        ix = (slice(None, n_out), self.bin_map, self.prod_map)
        self.integ_data.real[ix] = self.acc_re[:n_out]
        self.integ_data.imag[ix] = self.acc_im[:n_out]
        self.integ_sats.real[ix] = self.sat[:n_out, ..., 0] * (1.0 / 32.0)
        self.integ_sats.imag[ix] = self.sat[:n_out, ..., 1] * (1.0 / 16.0)
        return self.integ_data[:n_out], self.integ_sats[:n_out]

    def create_output_buffer(self, n_ts):
        """Allocate caller-side output arrays matching the shapes produced by :meth:`process_packets`."""
        ts = np.empty((n_ts,), dtype=self.ts_array.dtype)
        data = np.empty((n_ts, self.NBINS, self.NPROD), dtype=self.integ_data.dtype)
        sats = np.empty((n_ts, self.NBINS, self.NPROD), dtype=self.integ_sats.dtype)
        counts = np.empty((n_ts,), dtype=self.raw_data_count.dtype)
        return ts, data, sats, counts

    def filter_packets(self, n: int) -> Optional[np.ndarray]:
        """Decode UCORR headers; leave integration index in ``pkt_ts``; return row indices."""
        # Cookie filter — UCORR packets only. Header layout is fixed so no packet-size check needed.
        bix_mask = self.bix_mask[:n]
        np.equal(self.buf_cookie[:n], self.CORR_PACKET_COOKIE, out=bix_mask)

        # Decode header fields. stream_id top 5 bits = corr id, low 11 bits = bin index, flags hi nibble = bin_set.
        np.right_shift(self.buf_stream_id[:n], 11, out=self.pkt_corr[:n], where=bix_mask)
        np.bitwise_and(self.buf_stream_id[:n], self.bin_mask, out=self.pkt_bin[:n], where=bix_mask)
        np.right_shift(self.buf_flags[:n], 4, out=self.pkt_bin_set[:n], where=bix_mask)
        np.bitwise_and(self.buf_ts[:n], self.ts_mask, out=self.pkt_ts[:n], where=bix_mask)
        # integ_period is uint64 — keep dividend uint64 to avoid float promotion.
        np.floor_divide(self.pkt_ts[:n], self.integ_period, out=self.pkt_ts[:n], where=bix_mask)

        # Drop packets at or before the latest integration already returned (monotonic emits).
        tmp_mask = self.tmp_mask[:n]
        np.greater(self.pkt_ts[:n], self._last_emitted_ts, out=tmp_mask, where=bix_mask)
        np.bitwise_and(bix_mask, tmp_mask, out=bix_mask)

        return np.flatnonzero(bix_mask)

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
        tix_to_clear = np.ones(n_ts, dtype=bool)        
        cix_mask = carry_tix >= 0  # find indices of carryover timestamps that were not dropped
        if cix_mask.any():
            if self.verbose:
                print(
                    f"UCorrPacketProcessor: preserving carryover state for {cix_mask.sum()} "
                    f"carryover timestamps {cix_mask=}, {carry_tix[cix_mask]=}."
                )
            src_tix = np.arange(cix.start, cix.stop)[cix_mask]
            dst_tix = carry_tix[cix_mask]
            self.acc_re[dst_tix] = self.acc_re[src_tix]
            self.acc_im[dst_tix] = self.acc_im[src_tix]
            self.sat[dst_tix] = self.sat[src_tix]
            self.raw_data_count[dst_tix] = self.raw_data_count[src_tix]
            tix_to_clear[dst_tix] = False  # don't clear the carryover rows

        n = tix_to_clear.size
        self.acc_re[:n][tix_to_clear] = 0
        self.acc_im[:n][tix_to_clear] = 0
        self.sat[:n][tix_to_clear] = 0
        self.raw_data_count[:n][tix_to_clear] = 0

    def store_packets(self, bix: np.ndarray, tix: np.ndarray) -> None:
        """Accumulate correlation products from valid packets into ``acc_re`` / ``acc_im``."""
        # integrate the packets into the accumulators
        # Track overrun bit (LSB of flags) over valid packets only.
        self.overrun |= int((self.buf_flags[bix] & 1).any())

        bin_set = self.pkt_bin_set[bix]
        corr = self.pkt_corr[bix]
        bin_ = self.pkt_bin[bix]

        temp32 = self.temp32[:len(bix)]
        # Sign-extend the 18-bit packed real/imag fields into preallocated int32 lanes.
        #   real: bits 27:10 of buf_data_h  -> shift<<4 then >>14 sign-extends from bit 31.
        #   imag: bits 17:0  of buf_data_l  -> shift<<14 then >>14 sign-extends from bit 31.
        np.left_shift(self.buf_data_h[bix], 4, out=temp32)
        np.right_shift(temp32, 14, out=temp32)
        # Fancy-index += matches the original behavior; relies on (tix, bin_set, corr, bin) tuples
        # being unique within a single batch (one packet per (corr,bin) per soft integration).
        self.acc_re[tix, bin_set, corr, bin_] += temp32

        np.left_shift(self.buf_data_l[bix], 14, out=temp32)
        np.right_shift(temp32, 14, out=temp32)
        self.acc_im[tix, bin_set, corr, bin_] += temp32

        # Sat counters: bit 0x20 = real saturation, bit 0x10 = imag saturation. Increment each cmac lane.
        sat_pkt = self.buf_data_sat[bix]
        self.sat[tix, bin_set, corr, bin_, :, 0] += sat_pkt & 0x20
        self.sat[tix, bin_set, corr, bin_, :, 1] += sat_pkt & 0x10

        np.add.at(self.raw_data_count, tix, 1)


    def emit_complete_rows(self, n_out: int, verbose: int):
        """Return ``(ts, data, sats, counts)`` views for complete integration rows."""
        integ_ts = self.ts_array[:n_out]
        data, sats = self.format_data(n_out)
        counts = self.raw_data_count[:n_out]

        self._last_emitted_ts = integ_ts[n_out - 1]

        if verbose >= 2:
            print(f"UCorrPacketProcessor: emitted {n_out} integ(s), carry={self.carry_len}, overrun={self.overrun}")
        if self.overrun:
            print("*** Warning: the correlator experienced an overrun condition. All products could not be sent within the hardware integration period.")
            self.overrun = 0

        return integ_ts, data, sats, counts

    def flush(self, flush_timeout=None):
        """Drain UDP and clear assembly state for a clean restart."""
        if flush_timeout is None:
            self.pr.flush()
        else:
            self.pr.flush(flush_timeout)
        if self.bin_map is not None and self.firmware_integ_period is not None:
            self.acc_re.fill(0)
            self.acc_im.fill(0)
            self.sat.fill(0)
            self.raw_data_count.fill(0)
            self.ts_array.fill(0)
        self.n_ts = 0
        self.carry_len = 0
        self._last_emitted_ts = np.uint64(0)
        self.overrun = 0
