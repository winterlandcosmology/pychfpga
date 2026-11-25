"""Regression tests: stray UCORR packets must not make the receiver drop most of each integration.

On crate 012 (2026-11-24) three packets that started with the UCORR cookie but carried timestamps
more than 18 h in the future took 3 of the 4 assembly rows. Every real integration was then
emitted after its first firmware integration and the rest was dropped (counts 1024 of 131072,
for 18 h). These tests feed the real CorrDataProcessor the same situation with a small geometry
that keeps the 4 assembly rows: 16 packets per firmware integration, 4 firmware integrations per
emitted integration (expected_packets 64).
"""
import logging
import warnings

import numpy as np
import pytest

from pychfpga.raw_acq.corr_data_processor import CorrDataProcessor

FIRM = 16  # firmware integration period, frames
SOFT = 4   # firmware integrations per emitted integration
START = 1000  # first integration index


class FakeReceiver:
    """Shared packet buffer as filled by RawAcqReceiver.receive_packets()."""

    def __init__(self, n_packets=64):
        self.n_packets = n_packets
        self.buf = np.zeros((n_packets, 9000), dtype=np.uint8)
        self.pkt_len = np.zeros(n_packets, dtype=np.uint16)
        self.n_stored = 0

    def flush(self, *args, **kwargs):
        self.n_stored = 0


def make_processor():
    config = dict(NCHAN=4, NCLK=1, NDECIM=1, CT_LEVEL=1, CORR_ID=0,
                  fft_bin_map=np.arange(16).reshape(2, 8),
                  firmware_integ_period=FIRM, soft_integ_period=SOFT)
    rx = FakeReceiver()
    proc = CorrDataProcessor(rx, config=config)
    assert proc.max_timestamps == 4 and proc.expected_packets == 64
    return rx, proc


def put_packet(rx, proc, row, ts, corr=0, bin_=0, length=None):
    """Write one UCORR packet (header + products) into receiver row `row`."""
    sid = (corr << 11) | bin_
    header = bytes([0xCF, sid >> 8, sid & 0xFF, 0]) + int(ts).to_bytes(6, 'big')
    size = proc.packet_dtype.itemsize
    rx.buf[row, :size] = 0
    rx.buf[row, :10] = np.frombuffer(header, dtype=np.uint8)
    rx.pkt_len[row] = size if length is None else length


def ts_of(integ, fw=0):
    return integ * SOFT * FIRM + fw * FIRM + 3


class Stream:
    """Feed firmware integrations (one receive batch each) and collect emitted (integ, counts)."""

    def __init__(self):
        self.rx, self.proc = make_processor()
        self.emitted = []

    def batch(self, integ, fw, strays=(), burst=True):
        row = 0
        if burst:
            for corr in range(self.proc.NCORRS):
                for bin_ in range(self.proc.NPROD_PER_CMAC):
                    put_packet(self.rx, self.proc, row, ts_of(integ, fw), corr, bin_)
                    row += 1
        for stray in strays:
            put_packet(self.rx, self.proc, row, stray.get('ts', 0), length=stray.get('length'))
            row += 1
        self.rx.n_stored = row
        self.proc.process_udp_packets()
        result = self.proc.get_formatted_data()
        self.proc.consume(result)
        if result is not None:
            ts, _, _, counts = result
            self.emitted += [(int(t), int(c)) for t, c in zip(ts, counts)]

    def integrations(self, first, last, strays_at=None):
        for integ in range(first, last + 1):
            for fw in range(SOFT):
                self.batch(integ, fw, (strays_at or {}).get((integ, fw), ()))


def test_normal_stream_emits_complete_integrations():
    s = Stream()
    s.integrations(START, START + 5)
    assert s.emitted == [(i, 64) for i in range(START, START + 6)]


def test_far_future_strays_do_not_truncate_integrations():
    # Three strays with timestamps far in the future, as on 2026-11-24.
    strays = [dict(ts=ts_of(START + 5000 + k)) for k in (0, 2, 4)]
    s = Stream()
    with pytest.warns(UserWarning, match='ahead of the last emitted'):
        s.integrations(START, START + 8, strays_at={(START + 2, 1): strays})
    assert s.emitted == [(i, 64) for i in range(START, START + 9)]
    assert s.proc.future_packets == 3
    assert s.proc.corr_incomplete_integrations == 0


def test_short_cookie_packets_are_rejected():
    # A short datagram that starts with 0xCF would otherwise decode stale header bytes.
    s = Stream()
    with pytest.warns(UserWarning, match='not 60 bytes long'):
        s.integrations(START, START + 4, strays_at={(START + 1, 2): [dict(ts=ts_of(START + 900), length=6)] * 3})
    assert s.emitted == [(i, 64) for i in range(START, START + 5)]
    assert s.proc.bad_length_packets == 3


def test_strays_before_the_first_emit_are_purged():
    s = Stream()
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        s.batch(START, 0, strays=[dict(ts=ts_of(START + 5000 + k)) for k in (0, 2, 4)])
        for fw in range(1, SOFT):
            s.batch(START, fw)
        s.integrations(START + 1, START + 6)
    # The first integration is emitted early (the strays filled the held rows), after which the
    # strays are purged and every integration is complete again.
    assert all(c == 64 for i, c in s.emitted if i > START + 1)
    assert s.proc.n_ts <= s.proc.max_timestamps and int(s.proc.ts_array[:s.proc.n_ts].max()) < START + 100


def test_real_forward_jump_is_followed(monkeypatch):
    # Data lost for 50 integrations (longer than the window): the stream must resume, not stall.
    # Capture the module logger directly: other tests reconfigure logging globally.
    logger = logging.getLogger('pychfpga.common.ucorr_packet_processor')
    messages = []
    handler = logging.Handler()
    handler.emit = lambda record: messages.append(record.getMessage())
    monkeypatch.setattr(logger, 'disabled', False)
    monkeypatch.setattr(logger, 'level', logging.WARNING)
    logger.addHandler(handler)
    try:
        s = Stream()
        s.integrations(START, START + 3)
        s.integrations(START + 50, START + 55)
    finally:
        logger.removeHandler(handler)
    assert any('jumped' in m for m in messages)
    after = [(i, c) for i, c in s.emitted if i >= START + 50]
    assert after and all(c == 64 for i, c in after[1:])  # first one may lack the jump burst


def test_incomplete_integrations_are_counted_and_logged(caplog):
    s = Stream()
    with caplog.at_level(logging.WARNING):
        s.integrations(START, START + 1)
        for fw in range(SOFT - 1):  # one firmware integration of START+2 never arrives
            s.batch(START + 2, fw)
        s.integrations(START + 3, START + 5)
    assert (START + 2, 48) in s.emitted
    assert s.proc.corr_incomplete_integrations == 1
    assert 'missing packets' in caplog.text
