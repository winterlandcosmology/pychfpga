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
    def __init__(self, n_packets=16, max_packet_size=64):
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
