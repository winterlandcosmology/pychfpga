"""Dual ADCDataProcessor / FFTDataProcessor instances on one UDP buffer."""

import numpy as np
import pytest

from pychfpga.raw_acq import ADCDataProcessor, FFTDataProcessor
from pychfpga.common.state import State

from test_raw_data_processor import DummyPacketReceiver, default_config, write_burst


def make_dual(stream_ids=(10, 11)):
    pr = DummyPacketReceiver(n_packets=16, max_packet_size=64)
    adc_cfg = default_config(stream_ids=stream_ids, packet_cookie=0xA0, packet_cookie_mask=0xFF)
    fft_cfg = default_config(stream_ids=stream_ids, packet_cookie=0xA1, packet_cookie_mask=0xFF)
    adc = ADCDataProcessor(pr, config=adc_cfg, stream_ids=stream_ids)
    fft = FFTDataProcessor(pr, config=fft_cfg, stream_ids=stream_ids)
    return adc, fft, pr


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
    adc, fft, pr = make_dual()
    write_burst(adc, pr, 0, ts=100, payload_base=1)
    write_burst(adc, pr, 2, ts=101, payload_base=2)
    pr.buf[2, 0] = 0xA1
    pr.buf[3, 0] = 0xA1
    pr.n_stored = 4

    adc_bix = adc.filter_packets(4)
    fft_bix = fft.filter_packets(4)
    assert adc_bix is not None and fft_bix is not None
    assert adc_bix.size >= 1 and fft_bix.size >= 1
    assert all(int(pr.buf[i, 0]) == 0xA0 for i in adc_bix)
    assert all(int(pr.buf[i, 0]) == 0xA1 for i in fft_bix)
    assert not np.intersect1d(adc_bix, fft_bix).size


def test_dual_processors_on_ucap_burst():
    adc, fft, pr = make_dual()
    write_burst(adc, pr, 0, ts=5, payload_base=11)
    write_burst(adc, pr, 2, ts=6, payload_base=21)
    write_burst(adc, pr, 4, ts=7, payload_base=31)
    write_burst(adc, pr, 6, ts=8, payload_base=41)
    pr.n_stored = 8
    adc_out = adc.process_packets()
    fft_out = fft.process_packets()
    assert adc_out is not None
    assert fft_out is None
