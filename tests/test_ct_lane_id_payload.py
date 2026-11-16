"""Offline unit tests of the shuffle256 lane-ID payload.

The payload makes every data byte of output lane k of the board in 0-based
slot s equal lane_id_byte(s, k), by programming identical bypassed
channelizer outputs on every board. The hardware behaviour is checked by
ct_shuffle256_test.test_lane_id_payload_shuffle256; the payload math, the
validation and the call sequence are checked here.
"""

import numpy as np
import pytest

from pychfpga.fpga_firmware.chfpga.chfpga import chFPGA
from pychfpga.fpga_firmware.chfpga.chfpga_array_methods import (
    chFPGAArrayMethods, lane_id_byte, lane_id_payload)


def _default_shuffle256_map(crate=0):
    """Lane k of slot s carries bins 128*m + 16*k + s (ct_shuffle256_test docstring)."""
    return {(crate, s, k): [128 * m + 16 * k + s for m in range(8)]
            for s in range(16) for k in range(8)}


def _to_byte(value):
    return ((int(value.real) & 0xF) << 4) | (int(value.imag) & 0xF)


class _FakeBoard:
    def __init__(self, capture_type='PROBER'):
        self.CAPTURE_TYPE = capture_type
        self.calls = []

    def set_channelizer(self, **kwargs):
        self.calls.append(('set_channelizer', kwargs))

    def set_funcgen(self, function, channels, **kwargs):
        self.calls.append(('set_funcgen', function, channels, kwargs))

    def sync(self):
        self.calls.append(('sync',))

    def set_channelizer_outputs(self, data, local_sync=True):
        self.calls.append(('set_channelizer_outputs', data, local_sync))


class _FakeArray:
    def __init__(self, mode='shuffle256', frequency_bins=None, nboards=16):
        self.ib = [_FakeBoard() for _ in range(nboards)]
        self.ic = []
        self.mode = mode
        self.corner_turn_frequency_bins = frequency_bins


def _array_methods(fake_array):
    methods = chFPGAArrayMethods(fake_array)
    methods.sync_calls = []
    methods.sync = lambda **kwargs: methods.sync_calls.append(kwargs)
    return methods


def test_lane_id_bytes_are_unique_and_never_0x00_or_0xff():
    bytes_ = [lane_id_byte(s, k) for s in range(16) for k in range(8)]
    assert len(set(bytes_)) == 128
    assert 0x00 not in bytes_ and 0xFF not in bytes_
    assert lane_id_byte(0, 0) == 0x01
    assert lane_id_byte(6, 3) == 0x64
    assert lane_id_byte(15, 7) == 0xF8


@pytest.mark.parametrize('slot, lane', [(16, 0), (-1, 0), (0, 8), ('a', 0), (0, None)])
def test_lane_id_byte_rejects_out_of_range_ids(slot, lane):
    with pytest.raises(ValueError):
        lane_id_byte(slot, lane)


def test_default_map_fills_every_lane_with_its_byte():
    frequency_bins = _default_shuffle256_map()
    data, lane_bytes = lane_id_payload(frequency_bins)

    assert data.shape == (16, 1024)
    assert np.all(data == data[0])  # same outputs on every channel
    assert lane_bytes == {lane_id: lane_id_byte(*lane_id[1:]) for lane_id in frequency_bins}
    assigned = set()
    for lane_id, bins in frequency_bins.items():
        for b in bins:
            assert _to_byte(data[0, b]) == lane_bytes[lane_id]
        assigned.update(bins)
    assert assigned == set(range(1024))
    assert np.all(np.abs(data.real) <= 8) and np.all(np.abs(data.imag) <= 8)


def test_shared_bins_with_equal_bytes_are_accepted_across_crates():
    frequency_bins = {**_default_shuffle256_map(crate=0), **_default_shuffle256_map(crate=1)}
    data, lane_bytes = lane_id_payload(frequency_bins)
    assert len(lane_bytes) == 256
    assert _to_byte(data[0, 16 * 3 + 5]) == lane_id_byte(5, 3)


def test_bin_on_two_lanes_with_different_bytes_is_rejected():
    frequency_bins = _default_shuffle256_map()
    frequency_bins[(0, 2, 1)] = frequency_bins[(0, 2, 1)] + [0]  # bin 0 belongs to slot 0 lane 0
    with pytest.raises(ValueError, match=r'Bin 0 is carried by lanes \(0, 0, 0\) and \(0, 2, 1\)'):
        lane_id_payload(frequency_bins)


def test_out_of_range_bin_is_rejected():
    with pytest.raises(ValueError, match='outside the channelizer range'):
        lane_id_payload({(0, 0, 0): [1024]})


def test_set_lane_id_payload_programs_every_board_then_syncs_the_array_once():
    fake = _FakeArray(frequency_bins=_default_shuffle256_map())
    methods = _array_methods(fake)

    lane_bytes = methods.set_lane_id_payload(max_sync_time_difference=50)

    expected_data, expected_bytes = lane_id_payload(fake.corner_turn_frequency_bins)
    assert lane_bytes == expected_bytes
    for mb in fake.ib:
        assert len(mb.calls) == 1
        name, data, local_sync = mb.calls[0]
        assert name == 'set_channelizer_outputs' and local_sync is False
        np.testing.assert_array_equal(data, expected_data)
    assert methods.sync_calls == [dict(max_sync_time_difference=50)]


@pytest.mark.parametrize('mode, frequency_bins, error', [
    ('shuffle16', _default_shuffle256_map(), ValueError),
    ('shuffle256', None, RuntimeError),
    ('shuffle256', {}, RuntimeError),
    ('shuffle256', {(0, 'x', 0): [0]}, ValueError),
])
def test_set_lane_id_payload_rejects_before_touching_any_board(mode, frequency_bins, error):
    fake = _FakeArray(mode=mode, frequency_bins=frequency_bins)
    methods = _array_methods(fake)
    with pytest.raises(error):
        methods.set_lane_id_payload()
    assert all(mb.calls == [] for mb in fake.ib)
    assert methods.sync_calls == []


@pytest.mark.parametrize('local_sync', [True, False])
def test_set_channelizer_outputs_bypasses_fft_and_scaler_and_writes_the_buffers(local_sync):
    data, _ = lane_id_payload(_default_shuffle256_map())
    mb = _FakeBoard()

    d = chFPGA.set_channelizer_outputs(mb, data, local_sync=local_sync)

    assert mb.calls[0] == ('set_channelizer', dict(
        fft_bypass=1, scaler_bypass=1, scaler_bypass_data_type=0,
        offset_binary_encoding=False, local_sync=False))
    funcgen_calls = mb.calls[1:17]
    assert [c[:3] for c in funcgen_calls] == [('set_funcgen', 'arb', [ch]) for ch in range(16)]
    for ch, (_, _, _, kwargs) in enumerate(funcgen_calls):
        buf = kwargs['data']
        assert buf.dtype == np.uint8 and buf.size == 2048
        assert kwargs['left_shift'] == 0 and kwargs['right_shift'] == 0
        np.testing.assert_array_equal(buf, d[ch].view(np.uint8))
        # With both bypasses the scaler forwards the top nibble of each sample
        out = (buf[0::2] & 0xF0) | (buf[1::2] >> 4)
        np.testing.assert_array_equal(out, [_to_byte(v) for v in data[ch]])
    assert mb.calls[17:] == ([('sync',)] if local_sync else [])


@pytest.mark.parametrize('data', [
    np.zeros((16, 512), complex),
    np.full((16, 1024), 8 + 0j),
    np.full((16, 1024), -9j),
    np.full((16, 1024), 0.5 + 0j),
])
def test_set_channelizer_outputs_rejects_invalid_data_before_any_write(data):
    mb = _FakeBoard()
    with pytest.raises(ValueError):
        chFPGA.set_channelizer_outputs(mb, data)
    assert mb.calls == []


@pytest.mark.parametrize('capture_type, error, match', [
    ('UCAP', NotImplementedError, 'CRS/UCAP'),
    ('OTHER', RuntimeError, "CAPTURE_TYPE='OTHER'"),
])
def test_set_channelizer_outputs_refuses_non_ice_platforms_before_any_write(capture_type, error, match):
    data, _ = lane_id_payload(_default_shuffle256_map())
    mb = _FakeBoard(capture_type)
    with pytest.raises(error, match=match):
        chFPGA.set_channelizer_outputs(mb, data)
    assert mb.calls == []
