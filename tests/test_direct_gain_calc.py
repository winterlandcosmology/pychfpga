"""Tests for DirectGainCalc and FFT RMS acquisition used by direct gain computation."""

import asyncio

import numpy as np
import pytest

from pychfpga.calculate_gains import (
    DirectGainCalc,
    GainCalc,
    _ensure_scaler_float_encodable,
    _sanitize_float_gains,
)
from pychfpga.common.raw_data_packet_processor import RawDataPacketProcessor
from pychfpga.common.state import State
from pychfpga.raw_acq.fft_data_processor import FFTDataProcessor
from pychfpga.raw_acq.raw_data_processor import _merge_fft_decoder_into_raw


NBINS = GainCalc.NBINS


@pytest.fixture
def two_channel_calc():
    channel_ids = [(0, 1, 0), (0, 1, 1)]
    stream_ids = [256, 257]
    return DirectGainCalc(
        channel_ids=channel_ids,
        stream_ids=stream_ids,
        target_rms=1.5 * np.sqrt(2),
        post_gain_bits=44,
    )


def test_direct_gain_formula(two_channel_calc):
    gc = two_channel_calc
    target_full = gc.target_full
    rms = np.full((1, NBINS), 1000.0, dtype=np.float32)
    rms[0, 0] = 1e6  # DC bin — masked in gain table

    gains = gc.update_gains(np.array([256]), rms)
    assert gc.is_done() is False
    assert gc.percent_done() == 50.0

    gain_vec, glog = gains[(0, 1, 0)]
    assert glog == -1
    assert gain_vec.shape == (NBINS,)
    assert np.isnan(gain_vec[0])
    np.testing.assert_allclose(gain_vec[1], target_full / 1000.0, rtol=1e-5)


def test_direct_gain_completes_all_channels(two_channel_calc):
    gc = two_channel_calc
    rms = np.full((2, NBINS), 500.0, dtype=np.float32)

    gc.update_gains(np.array([256, 257]), rms)
    assert gc.is_done()
    assert gc.percent_done() == 100.0

    for cid in gc.channel_ids:
        gain_vec, glog = gc.get_gains()[cid]
        assert glog == -1
        assert gain_vec.shape == (NBINS,)


def test_direct_gain_skips_already_done_channels(two_channel_calc):
    gc = two_channel_calc
    rms = np.full((1, NBINS), 1000.0, dtype=np.float32)
    gc.update_gains(np.array([256]), rms)

    rms2 = np.full((2, NBINS), 2000.0, dtype=np.float32)
    gc.update_gains(np.array([256, 257]), rms2)

    assert gc.gains[0, 1] == pytest.approx(gc.target_full / 1000.0)
    assert gc.gains[1, 1] == pytest.approx(gc.target_full / 2000.0)


def test_direct_gain_get_filtered_gains(two_channel_calc):
    gc = two_channel_calc
    rms = np.full((1, NBINS), 1000.0, dtype=np.float32)
    gc.update_gains(np.array([256]), rms)

    filtered, mask = gc.get_filtered_gains()
    assert (0, 1, 0) in filtered
    gain_vec, glog = filtered[(0, 1, 0)]
    assert glog == -1
    assert gain_vec.shape == (NBINS,)
    assert mask.shape == (1, NBINS)


class _MockRawProcessor:
    NCHAN = 1
    stream_ids = (10,)
    NUMBER_OF_SUBFRAMES = 1
    config = State(
        packets_per_logical_frame=1,
        capture_format=[
            None,
            State(fmt_base_width=32, fmt_is_complex=True, fmt_interleave_factor=2),
        ],
    )


@pytest.fixture
def fft_processor():
    mock_raw = _MockRawProcessor()
    return FFTDataProcessor(_DummyPacketReceiver(), mock_raw, stream_ids=(10,))


def _start_fft_rms(proc, n_bins, n_frames):
    """Pre-size RMS buffers so the first frame does not reset acquisition state."""
    proc._alloc_fft_rms_buffers(proc.NCHAN, n_bins)
    asyncio.run(proc.start_fft_rms_async(list(proc.stream_ids), target_gain_bank=0, number_of_frames=n_frames))


def _emit_complex_frames(proc, spectra, counts_val=1):
    """Feed complex spectra shaped (n_frames, nchan, n_bins) into the processor."""
    n_frames, nchan, n_bins = spectra.shape
    ts = np.arange(n_frames, dtype=np.uint64)
    counts = np.full((n_frames, nchan), counts_val, dtype=np.int64)
    indices = np.arange(n_frames)
    proc.consume((ts, spectra, counts, indices))


def test_fft_rms_accumulation(fft_processor):
    proc = fft_processor
    n_bins = 8
    n_frames = 5

    _start_fft_rms(proc, n_bins, n_frames)

    re = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0], dtype=np.float32)
    im = np.array([0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0], dtype=np.float32)
    spectrum = re + 1j * im
    expected_power = re * re + im * im
    expected_rms = np.sqrt(expected_power)

    spectra = np.tile(spectrum, (n_frames, 1, 1))
    _emit_complex_frames(proc, spectra)

    assert proc.fft_rms_done[0]
    np.testing.assert_allclose(proc.fft_rms[0], expected_rms, rtol=1e-5)


def test_fft_rms_averages_multiple_frames(fft_processor):
    proc = fft_processor
    n_bins = 4
    n_frames = 3

    _start_fft_rms(proc, n_bins, n_frames)

    frame0 = np.array([1 + 0j, 2 + 0j, 3 + 0j, 4 + 0j], dtype=np.complex64)
    frame1 = np.array([3 + 0j, 4 + 0j, 5 + 0j, 6 + 0j], dtype=np.complex64)
    frame2 = np.array([5 + 0j, 6 + 0j, 7 + 0j, 8 + 0j], dtype=np.complex64)
    spectra = np.stack([frame0, frame1, frame2])[:, np.newaxis, :]
    _emit_complex_frames(proc, spectra)

    power_sum = frame0.real**2 + frame1.real**2 + frame2.real**2
    expected_rms = np.sqrt(power_sum / n_frames)
    np.testing.assert_allclose(proc.fft_rms[0], expected_rms, rtol=1e-5)


def test_merge_fft_decoder_into_raw_complex_format():
    raw = State(
        source='funcgen',
        capture_format=[
            State(fmt_base_width=16, fmt_is_complex=False, fmt_interleave_factor=1),
            State(fmt_base_width=16, fmt_is_complex=False, fmt_interleave_factor=1),
        ],
        packets_per_logical_frame=1,
    )
    fft = State(
        source='scaler',
        capture_format=[
            None,
            State(fmt_base_width=32, fmt_is_complex=True, fmt_interleave_factor=2),
        ],
        packets_per_logical_frame=2,
        offset_encoding=True,
    )
    merged = _merge_fft_decoder_into_raw(raw, fft)
    assert merged.source == 'scaler'
    assert merged.packets_per_logical_frame == 2
    assert merged.capture_format[1].fmt_is_complex is True
    assert merged.capture_format[1].fmt_interleave_factor == 2


def test_start_fft_rms_rejects_non_complex_decoder():
    mock_raw = _MockRawProcessor()
    mock_raw.config = State(
        packets_per_logical_frame=1,
        capture_format=[
            State(fmt_base_width=16, fmt_is_complex=False, fmt_interleave_factor=1),
            State(fmt_base_width=16, fmt_is_complex=False, fmt_interleave_factor=1),
        ],
    )
    proc = FFTDataProcessor(_DummyPacketReceiver(), mock_raw, stream_ids=(10,))
    with pytest.raises(RuntimeError, match='complex capture_format'):
        asyncio.run(proc.start_fft_rms_async([10], target_gain_bank=0, number_of_frames=2))


def test_direct_gain_caps_zero_rms_bins():
    gc = DirectGainCalc(
        channel_ids=[(0, 1, 0)],
        stream_ids=[0],
        n_bins=16,
    )
    rms = np.zeros((1, 16), dtype=np.float32)
    rms[0, 5:10] = 500.0
    gc.update_gains(np.array([0]), rms)
    gain_vec, glog = gc.get_gains()[(0, 1, 0)]
    assert glog == -1
    assert np.isnan(gain_vec[0])  # DC masked
    assert np.all(np.isfinite(gain_vec[1:]))
    assert np.all(gain_vec[1:] > 0)
    filtered, _ = gc.get_filtered_gains()
    filt_vec, _ = filtered[(0, 1, 0)]
    _ensure_scaler_float_encodable(filt_vec)  # must not raise


def test_sanitize_float_gains_handles_all_zero():
  row = _sanitize_float_gains(np.zeros(8))
  assert np.all(row == 1.0)


def test_direct_gain_accepts_full_resolution_rms():
    n_bins = 8192
    gc = DirectGainCalc(
        channel_ids=[(0, 1, 0)],
        stream_ids=[0],
        n_bins=n_bins,
    )
    rms = np.full((1, n_bins), 500.0, dtype=np.float32)
    gc.update_gains(np.array([0]), rms)
    assert gc.is_done()
    gain_vec, glog = gc.get_gains()[(0, 1, 0)]
    assert gain_vec.shape == (n_bins,)
    assert glog == -1


def test_fft_rms_accepts_ucap_split_frame_counts():
    """Split complex rows carry per-frame counts (4), not burst totals (8)."""
    mock_raw = _MockRawProcessor()
    mock_raw.NUMBER_OF_SUBFRAMES = 4
    mock_raw.config = State(
        packets_per_logical_frame=2,
        capture_format=[
            None,
            State(fmt_base_width=32, fmt_is_complex=True, fmt_interleave_factor=1),
        ],
    )
    proc = FFTDataProcessor(_DummyPacketReceiver(), mock_raw, stream_ids=(10,))
    n_bins = 8
    n_frames = 2

    _start_fft_rms(proc, n_bins, n_frames)

    half0 = np.array([1 + 0j, 2 + 0j, 3 + 0j, 4 + 0j], dtype=np.complex64)
    half1 = np.array([5 + 0j, 6 + 0j, 7 + 0j, 8 + 0j], dtype=np.complex64)
    full = np.concatenate([half0, half1])
    for _ in range(n_frames):
        _emit_complex_frames(proc, half0.reshape(1, 1, 4), counts_val=4)
        _emit_complex_frames(proc, half1.reshape(1, 1, 4), counts_val=4)

    assert proc.fft_rms_done[0]
    np.testing.assert_allclose(proc.fft_rms[0], np.abs(full), rtol=1e-5)


def test_fft_rms_rejects_non_complex():
    with pytest.raises(TypeError, match='complex spectra'):
        FFTDataProcessor._bin_power(np.arange(8, dtype=np.float32))


class _DummyPacketReceiver:
    def __init__(self, n_packets=4, max_packet_size=256):
        self.n_packets = n_packets
        self.max_packet_size = max_packet_size
        self.buf = np.zeros((n_packets, max_packet_size), dtype=np.uint8)
        self.pkt_len = np.zeros((n_packets,), dtype=np.uint32)
        self.n_stored = 0

    def flush(self, **kwargs):
        self.pkt_len.fill(0)
        self.n_stored = 0
        return 0


def test_formatter_bin_map_and_offset_encoding():
    """Formatter unscrambles bins and removes offset-binary bias on FFT source."""
    proc = object.__new__(RawDataPacketProcessor)
    proc.config = State(
        offset_encoding=True,
        fft_bin_map=np.array([2, 0, 3, 1], dtype=np.intp),
    )
    bias = 1 << 15
    scrambled = np.array(
        [complex(bias + 100, bias), complex(bias + 200, bias),
         complex(bias + 300, bias), complex(bias + 400, bias)],
        dtype=np.complex64,
    )
    data = scrambled.reshape(1, 1, 4)
    out = proc._apply_fft_formatter_compensation(data, source=1, bit_width=16)
    expected = np.array([300 + 0j, 100 + 0j, 400 + 0j, 200 + 0j], dtype=np.complex64)
    np.testing.assert_allclose(out[0, 0], expected, rtol=0, atol=1e-3)


def test_full_resolution_complex_formatter_reconstructs_lanes():
    """Mode-5/full-resolution rows are reconstructed from Re/Im lane frames."""
    proc = object.__new__(RawDataPacketProcessor)
    proc.config = State(
        capture_format=[
            None,
            State(fmt_base_width=32, fmt_is_complex=True, fmt_interleave_factor=4),
        ],
        offset_encoding=False,
    )
    proc.FRAMES_PER_BURST = 4
    proc.CAPTURE_BITS_PER_WORD = 32
    proc.raw_data = np.array(
        [
            [
                [
                    10, 30,  # lane 0 real -> bins 0, 2
                    1, 3,    # lane 0 imag -> bins 0, 2
                    20, 40,  # lane 1 real -> bins 1, 3
                    2, 4,    # lane 1 imag -> bins 1, 3
                ],
            ],
        ],
        dtype=">i4",
    ).view(np.uint8)
    proc.ts_array = np.array([100], dtype=np.uint64)
    proc.raw_data_count = np.ones((1, 1, 4), dtype=np.int64)
    proc.raw_source = np.ones((1, 1, 4), dtype=np.uint8)
    proc.fmt_cdata2 = [
        None,
        np.empty((1, 1, 4), dtype=np.complex64),
    ]

    ts, data, counts = proc.get_formatted_data(source=1)

    np.testing.assert_array_equal(ts, np.array([100], dtype=np.uint64))
    np.testing.assert_array_equal(counts, np.array([[1]], dtype=np.int64))
    expected = np.array([10 + 1j, 20 + 2j, 30 + 3j, 40 + 4j], dtype=np.complex64)
    np.testing.assert_allclose(data[0, 0], expected, rtol=0, atol=0)


def test_end_to_end_direct_gain_from_fft_rms(two_channel_calc, fft_processor):
    """DirectGainCalc consumes RMS values produced by FFTDataProcessor."""
    gc = two_channel_calc
    proc = fft_processor
    n_bins = NBINS
    n_frames = 4
    re_val = 1000.0

    _start_fft_rms(proc, n_bins, n_frames)
    spectrum = np.full(n_bins, re_val + 0j, dtype=np.complex64)
    spectra = np.tile(spectrum, (n_frames, 1, 1))
    _emit_complex_frames(proc, spectra)

    sid, rms = asyncio.run(proc.get_fft_rms_async())
    assert sid[0] == 10
    np.testing.assert_allclose(rms[0, 1], re_val, rtol=1e-5)

    # Wire stream 10 to first channel in gain calc for this smoke test
    gc.stream_id_map = {10: 0}
    gc.stream_ids = [10]
    gc.channel_ids = [(0, 1, 0)]
    gc.nchan = 1
    gc.gains = np.zeros((1, NBINS), dtype=np.float64)
    gc.done = np.zeros(1, dtype=bool)

    gc.update_gains(np.array([10]), rms)
    assert gc.is_done()
    gain_vec, glog = gc.get_gains()[(0, 1, 0)]
    assert glog == -1
    assert gain_vec[1] == pytest.approx(gc.target_full / re_val, rel=1e-5)
