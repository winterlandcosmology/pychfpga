"""Dual-source RawDataPacketProcessor on one UDP buffer."""

import numpy as np
import pytest

from pychfpga.common.raw_data_packet_processor import (
    PACKET_COOKIE_ADC,
    PACKET_COOKIE_FFT,
    RawDataPacketProcessor,
)

from test_raw_data_processor import (
    DummyPacketReceiver,
    capture_format_for_tests,
    config_getter_from_mapping,
    default_config,
    process_and_get,
    write_burst,
)


def make_dual(stream_ids=(10, 11)):
    pr = DummyPacketReceiver(n_packets=16, max_packet_size=64)
    cfg = default_config(stream_ids=stream_ids, packet_cookie=PACKET_COOKIE_ADC)
    cfg["capture_format"] = [capture_format_for_tests("8"), capture_format_for_tests("8")]
    proc = RawDataPacketProcessor(
        pr, config_getter=config_getter_from_mapping(cfg), receive_window=3
    )
    proc.update_config()
    return proc, pr


def write_legacy_packet(pr, ix, cookie, stream_id, ts, payload=None):
    payload = np.asarray(payload if payload is not None else np.arange(4), dtype=np.uint8)
    pr.buf[ix, 0] = cookie
    pr.buf[ix, 1] = ((0 & 0xF) << 4) | ((stream_id >> 8) & 0xF)
    pr.buf[ix, 2] = stream_id & 0xFF
    pr.buf[ix, 3] = 0
    pr.buf[ix, 4:10] = np.frombuffer(int(ts).to_bytes(6, "big"), dtype=np.uint8)
    pr.buf[ix, 10:14] = payload
    pr.pkt_len[ix] = 10 + 4
    pr.n_stored = max(pr.n_stored, ix + 1)


def test_adc_and_fft_filters_are_disjoint():
    proc, pr = make_dual()
    write_burst(proc, pr, 0, ts=100, payload_base=1)
    write_burst(proc, pr, 2, ts=101, payload_base=2)
    pr.buf[2, 0] = PACKET_COOKIE_FFT
    pr.buf[3, 0] = PACKET_COOKIE_FFT
    pr.n_stored = 4

    adc_bix = proc.filter_packets(4)
    assert adc_bix is not None
    assert adc_bix.size >= 1
    assert all(int(pr.buf[i, 0]) in (PACKET_COOKIE_ADC, PACKET_COOKIE_FFT) for i in adc_bix)


def test_dual_processors_on_ucap_burst():
    proc, pr = make_dual()
    write_burst(proc, pr, 0, ts=5, payload_base=11)
    write_burst(proc, pr, 2, ts=6, payload_base=21)
    write_burst(proc, pr, 4, ts=7, payload_base=31)
    write_burst(proc, pr, 6, ts=8, payload_base=41)
    pr.n_stored = 8
    out0 = process_and_get(proc, source=0)
    assert out0 is not None
    ts, data, counts = out0
    assert ts.shape[0] > 0
    assert counts.sum() > 0
    assert np.all((proc.raw_source[proc.out_rows] & 1) == 0)
