"""UCorrPacketProcessor overrun latch: per-row, not a stale process-wide OR."""

import sys
import types

import numpy as np

matplotlib_stub = types.ModuleType("matplotlib")
matplotlib_pyplot_stub = types.ModuleType("matplotlib.pyplot")
matplotlib_stub.pyplot = matplotlib_pyplot_stub
sys.modules.setdefault("matplotlib", matplotlib_stub)
sys.modules.setdefault("matplotlib.pyplot", matplotlib_pyplot_stub)

from pychfpga.common.state import State
from pychfpga.common.ucorr_packet_processor import UCorrPacketProcessor


class DummyPacketReceiver:
    # Aggregation packs up to FRAMES_PER_PACKET (8) records of 15 B into one row,
    # so the buffer row must hold at least 8*15 = 120 B.
    def __init__(self, n_packets=16, max_packet_size=256):
        self.n_packets = n_packets
        self.max_packet_size = max_packet_size
        self.buf = np.zeros((n_packets, max_packet_size), dtype=np.uint8)
        self.pkt_len = np.zeros((n_packets,), dtype=np.uint32)
        self.n_stored = 0

    def receive_packets(self, verbose=0):
        return int(self.n_stored)

    def flush(self, *args, **kwargs):
        self.pkt_len.fill(0)
        self.n_stored = 0
        return 0


def _config():
    """Minimal 1-product correlator."""
    return State(
        NCHAN=1,
        NCLK=1,
        NDECIM=1,
        NCORRS=1,
        NPROD_PER_CMAC=1,
        NCMAC=1,
        NBINS=1,
        NPROD=1,
        NPROD_PER_ARRAY=1,
        firmware_integ_period=1,
        bin_map=np.zeros((1, 1, 1, 1), dtype=np.intp),
        prod_map=np.zeros((1, 1, 1), dtype=np.intp),
    )


def _write_header(buf, ts, flags=0, stream_id=0):
    buf[0] = UCorrPacketProcessor.CORR_PACKET_COOKIE
    buf[1] = (stream_id >> 8) & 0xFF
    buf[2] = stream_id & 0xFF
    buf[3] = flags & 0xFF
    ts = int(ts) & 0xFFFFFFFFFFFF
    for i in range(6):
        buf[9 - i] = (ts >> (8 * i)) & 0xFF


# One UCORR product word (single CMAC) is 15 B: a 10 B header + a 5 B (40-bit)
# data word laid out little-endian as imag[0:18] | real[18:36] | sat bits at 36/37.
_REC_SIZE = 15


def _write_record(row, rec, *, ts, stream_id=0, flags=0, real=0, imag=0,
                  real_sat=False, imag_sat=False):
    """Write one aggregated record ``rec`` (header + data word) into buffer ``row``.

    Packs several records per datagram exactly as the aggregating firmware does, so
    the receiver's (n_packets, nf) view parses each record at offset ``rec*15``.
    """
    off = rec * _REC_SIZE
    _write_header(row[off:off + 10], ts=ts, flags=flags, stream_id=stream_id)
    word = (int(imag) & 0x3FFFF) | ((int(real) & 0x3FFFF) << 18)
    if imag_sat:
        word |= 1 << 36  # -> byte4 & 0x10
    if real_sat:
        word |= 1 << 37  # -> byte4 & 0x20
    for i in range(5):
        row[off + 10 + i] = (word >> (8 * i)) & 0xFF


def _agg_config(n_bins=8):
    """Correlator with ``n_bins`` distinct clock bins (one product lane each)."""
    bin_map = np.zeros((1, 1, n_bins, 1), dtype=np.intp)
    bin_map[0, 0, :, 0] = np.arange(n_bins)  # lane k -> output bin k (identity)
    return State(
        NCHAN=1,
        NCLK=1,
        NDECIM=1,
        NCORRS=1,
        NPROD_PER_CMAC=n_bins,
        NCMAC=1,
        NBINS=n_bins,
        NPROD=1,
        NPROD_PER_ARRAY=n_bins,
        firmware_integ_period=1,
        bin_map=bin_map,
        prod_map=np.zeros((1, n_bins, 1), dtype=np.intp),
    )


def _make_agg_processor(*, soft_integ_period=1, n_bins=8):
    pr = DummyPacketReceiver()
    cfg = _agg_config(n_bins)
    proc = UCorrPacketProcessor(pr, config_getter=lambda: cfg, receive_window=3)
    proc.set_soft_integ_period(soft_integ_period)
    proc.update_config()
    return proc, pr


def _make_processor(*, soft_integ_period=1):
    pr = DummyPacketReceiver()
    cfg = _config()
    proc = UCorrPacketProcessor(pr, config_getter=lambda: cfg, receive_window=3)
    proc.set_soft_integ_period(soft_integ_period)
    proc.update_config()
    return proc, pr


def test_overrun_on_emitted_row_warns(capsys):
    proc, pr = _make_processor()
    _write_header(pr.buf[0], ts=10, flags=0x01)
    pr.pkt_len[0] = 15
    pr.n_stored = 1
    proc.process_udp_packets(update_config=False)
    assert proc.n_out == 1
    assert "*** Warning: the correlator experienced an overrun condition." in capsys.readouterr().out


def test_overrun_on_carry_does_not_warn_for_clean_emit(capsys):
    """Carry packets with the overrun bit must not warn on a clean emitted row."""
    proc, pr = _make_processor(soft_integ_period=2)
    # integ 5 (ts 10-11) complete and clean; integ 10 (ts 20) incomplete with overrun.
    _write_header(pr.buf[0], ts=10, flags=0)
    _write_header(pr.buf[1], ts=11, flags=0)
    _write_header(pr.buf[2], ts=20, flags=0x01)
    pr.pkt_len[:3] = 15
    pr.n_stored = 3
    proc.process_udp_packets(update_config=False)
    assert proc.n_out == 1
    assert int(proc.ts_array[proc.out_rows][0]) == 5
    assert int(proc.overrun_row[proc.out_rows][0]) == 0
    assert int(proc.overrun_row[1]) == 1
    assert "*** Warning: the correlator experienced an overrun condition." not in capsys.readouterr().out


def test_overrun_on_carry_warns_when_that_row_emits(capsys):
    proc, pr = _make_processor(soft_integ_period=2)
    _write_header(pr.buf[0], ts=10, flags=0)
    _write_header(pr.buf[1], ts=11, flags=0)
    _write_header(pr.buf[2], ts=20, flags=0x01)
    pr.pkt_len[:3] = 15
    pr.n_stored = 3
    proc.process_udp_packets(update_config=False)
    capsys.readouterr()

    _write_header(pr.buf[0], ts=21, flags=0)
    pr.pkt_len[0] = 15
    pr.n_stored = 1
    proc.process_udp_packets(update_config=False)
    assert proc.n_out >= 1
    assert "*** Warning: the correlator experienced an overrun condition." in capsys.readouterr().out


def test_identity_carry_accumulates_counts():
    """Two batches of the same software integration must keep the running count.

    The common path is an identity carry map (row 0 stays row 0). Skipping the
    full-row memcpy must not skip preserving ``raw_data_count``.
    """
    proc, pr = _make_processor(soft_integ_period=2)
    _write_header(pr.buf[0], ts=10, flags=0)
    pr.pkt_len[0] = 15
    pr.n_stored = 1
    proc.process_udp_packets(update_config=False)
    assert proc.n_out == 0
    assert int(proc.raw_data_count[0]) == 1

    _write_header(pr.buf[0], ts=11, flags=0)
    pr.pkt_len[0] = 15
    pr.n_stored = 1
    proc.process_udp_packets(update_config=False)
    assert proc.n_out == 1
    assert int(proc.raw_data_count[0]) == 2
    assert int(proc.profile["init_dest_move_s"] * 1000) == 0


def test_flush_clears_overrun_row():
    proc, pr = _make_processor(soft_integ_period=2)
    _write_header(pr.buf[0], ts=10, flags=0x01)
    pr.pkt_len[0] = 15
    pr.n_stored = 1
    proc.process_udp_packets(update_config=False)
    assert int(proc.overrun_row[: proc.n_ts].any()) == 1
    proc.flush()
    assert proc.overrun == 0
    assert int(proc.overrun_row.any()) == 0


def test_aggregated_datagram_parses_all_records():
    """One datagram packing 8 bins must accumulate every record, not just the first."""
    proc, pr = _make_agg_processor(n_bins=8)
    # 8 records in a single datagram: bin k gets real=10*(k+1), imag=k+1.
    for k in range(8):
        _write_record(pr.buf[0], k, ts=10, stream_id=k, real=10 * (k + 1), imag=k + 1)
    pr.pkt_len[0] = 8 * _REC_SIZE
    pr.n_stored = 1
    proc.process_udp_packets(update_config=False)

    # ts 10 with firmware/soft period 1 -> complete row (8/8) is emitted.
    assert proc.n_out == 1
    row = proc.out_rows.start
    assert int(proc.raw_data_count[row]) == 8
    for k in range(8):
        assert int(proc.acc_re[row, 0, 0, k, 0]) == 10 * (k + 1)
        assert int(proc.acc_im[row, 0, 0, k, 0]) == k + 1


def test_get_formatted_data_flat_scatter():
    """The precomputed flat-index scatter must place each product at its (bin, prod) cell."""
    proc, pr = _make_agg_processor(n_bins=8)
    for k in range(8):
        _write_record(pr.buf[0], k, ts=10, stream_id=k, real=10 * (k + 1), imag=k + 1)
    pr.pkt_len[0] = 8 * _REC_SIZE
    pr.n_stored = 1
    proc.process_udp_packets(update_config=False)

    result = proc.get_formatted_data()
    assert result is not None
    integ_ts, data, counts, sats = result
    assert data.dtype == proc.OUTPUT_COMPLEX_DTYPE
    assert data.shape == (1, 8, 1)
    for k in range(8):
        assert data[0, k, 0] == complex(10 * (k + 1), k + 1)


def test_short_final_group_masked_by_pkt_len():
    """A datagram whose pkt_len covers only 3 records must ignore stale tail records."""
    proc, pr = _make_agg_processor(n_bins=8)
    # Physically write all 8 slots, but advertise only the first 3 via pkt_len.
    for k in range(8):
        _write_record(pr.buf[0], k, ts=11, stream_id=k, real=10 * (k + 1), imag=k + 1)
    pr.pkt_len[0] = 3 * _REC_SIZE
    pr.n_stored = 1
    proc.process_udp_packets(update_config=False)

    # 3/8 records -> incomplete integration, withheld as carry (nothing emitted).
    assert proc.n_out == 0
    assert int(proc.raw_data_count[0]) == 3
    # Only bins 0..2 were accumulated; the masked tail (3..7) stayed zero.
    for k in range(3):
        assert int(proc.acc_re[0, 0, 0, k, 0]) == 10 * (k + 1)
    for k in range(3, 8):
        assert int(proc.acc_re[0, 0, 0, k, 0]) == 0


def test_non_multiple_length_datagram_rejected():
    """A datagram whose length is not a clean multiple of the record size is dropped.

    Guards the aggregation regression where a foreign/misaligned datagram carrying a
    stray cookie byte at a record boundary would decode to garbage (e.g. bin_set=15).
    """
    proc, pr = _make_agg_processor(n_bins=8)
    sz = proc.packet_dtype.itemsize
    # Valid corr record in slot 0, plus a stray cookie at the slot-1 boundary, but
    # advertise a length that is NOT a multiple of sz -> whole datagram rejected.
    _write_record(pr.buf[0], 0, ts=10, stream_id=0, real=7, imag=3)
    pr.buf[0][sz] = UCorrPacketProcessor.CORR_PACKET_COOKIE  # stray cookie at slot 1
    pr.buf[0][sz + 3] = 0xF0  # would decode to bin_set=15 if wrongly accepted
    pr.pkt_len[0] = sz + 5  # not a multiple of sz
    pr.n_stored = 1
    proc.process_udp_packets(update_config=False)  # must not raise
    assert proc.n_out == 0
    assert int(proc.raw_data_count[: max(proc.n_ts, 1)].sum()) == 0


def test_aggregated_saturation_flags_parsed_per_record():
    """Real- and imag-saturation bits must be decoded independently per aggregated record."""
    proc, pr = _make_agg_processor(n_bins=8)
    real_sat_bins = (2, 5)
    imag_sat_bins = (3, 5)
    for k in range(8):
        _write_record(
            pr.buf[0], k, ts=10, stream_id=k, real=1, imag=1,
            real_sat=(k in real_sat_bins), imag_sat=(k in imag_sat_bins),
        )
    pr.pkt_len[0] = 8 * _REC_SIZE
    pr.n_stored = 1
    proc.process_udp_packets(update_config=False)

    row = proc.out_rows.start
    for k in range(8):
        assert int(proc.sat[row, 0, 0, k, 0, 0]) == (1 if k in real_sat_bins else 0)
        assert int(proc.sat[row, 0, 0, k, 0, 1]) == (1 if k in imag_sat_bins else 0)


def test_get_ij_to_prod_map_does_not_need_update_config():
    """(i, j) map comes from config.NCHAN; packet-processor update_config is not required."""
    from pychfpga.fpga_firmware.chfpga.x_engine.UCORR import UCORR

    pr = DummyPacketReceiver()
    cfg = _config()
    cfg.update(NCHAN=4)
    proc = UCorrPacketProcessor(pr, config_getter=lambda: cfg, receive_window=3)
    got = proc.get_ij_to_prod_map()
    assert proc.config is None  # update_config was not called
    expect = UCORR.get_ij_to_prod_map(4)
    assert np.array_equal(got, expect)
    assert got.shape == (4, 4)
    assert int(got[0, 0]) == 0
    assert int(got[0, 3]) == int(got[3, 0])


def test_get_good_bin_mask_does_not_need_update_config():
    """Good-bin map is bins in bin_map minus correlator sacrificial clkbin 0."""
    pr = DummyPacketReceiver()
    n_bins = 8
    n_corrs = 2
    bin_map = np.zeros((1, n_corrs, n_bins, 1), dtype=np.intp)
    bin_map[0, 0, :, 0] = np.arange(n_bins)
    bin_map[0, 1, :, 0] = np.arange(n_bins) + n_bins  # correlator 1 uses bins 8..15
    cfg = State(
        NCHAN=1,
        NCLK=1,
        NDECIM=1,
        READOUT_BINS=(0, 0),
        NCORRS=n_corrs,
        NPROD_PER_CMAC=n_bins,
        NCMAC=1,
        NBINS=16,
        NPROD=1,
        NPROD_PER_ARRAY=n_bins * n_corrs,
        firmware_integ_period=1,
        bin_map=bin_map,
        prod_map=np.zeros((n_corrs, n_bins, 1), dtype=np.intp),
    )
    proc = UCorrPacketProcessor(pr, config_getter=lambda: cfg, receive_window=3)
    mask = proc.get_good_bin_mask()
    assert proc.config is None  # update_config was not called
    assert mask.shape == (16,)
    assert mask.dtype == bool
    # Culled / unmapped bins stay False; clkbin 0 of each correlator is sacrificial.
    assert not mask[0]
    assert not mask[8]
    assert mask[1:8].all()
    assert mask[9:16].all()


def test_get_good_bin_mask_corr8_sacrifices_clkbin_equal_to_correlator():
    """READOUT_BINS=(0, 1, 2, 3) sacrifices clkbin i on correlator i."""
    pr = DummyPacketReceiver()
    n_corrs = 4
    n_clk = 4
    # Bypass layout: lane c, clock k -> FFT bin c + n_corrs*k (0,4,8,12 / 1,5,9,13 / ...).
    bin_map = np.zeros((1, n_corrs, n_clk, 1), dtype=np.intp)
    for corr in range(n_corrs):
        for clk in range(n_clk):
            bin_map[0, corr, clk, 0] = corr + n_corrs * clk
    cfg = State(
        NCHAN=8,
        NCLK=1,
        NDECIM=1,
        READOUT_BINS=(0, 1, 2, 3),
        NCORRS=n_corrs,
        NPROD_PER_CMAC=n_clk,
        NCMAC=1,
        NBINS=16,
        NPROD=1,
        NPROD_PER_ARRAY=n_clk * n_corrs,
        firmware_integ_period=1,
        bin_map=bin_map,
        prod_map=np.zeros((n_corrs, n_clk, 1), dtype=np.intp),
    )
    proc = UCorrPacketProcessor(pr, config_getter=lambda: cfg, receive_window=3)
    mask = proc.get_good_bin_mask()
    # READOUT_BINS = (0, 1, 2, 3) -> FFT bins 0, 5, 10, 15.
    assert not mask[[0, 5, 10, 15]].any()
    # clkbin 0 of correlators 1..3 is valid data in bypass (FFT bins 1, 2, 3).
    assert mask[[1, 2, 3]].all()
    assert mask[[4, 6, 7, 8, 9, 11, 12, 13, 14]].all()
