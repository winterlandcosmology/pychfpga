import numpy as np
import pytest

from firmware_tests.casper_simulations.caspersimtools import (
    OutputSignal,
    casper_fft_descramble,
)


def test_output_signal_reads_hirax_binary_rows(tmp_path):
    rows = np.array(
        [
            [1, 10, 20, 11, 21, 0],
            [0, 12, 22, 13, 23, 1],
            [0, 14, 24, 15, 25, 0],
        ],
        dtype=np.int32,
    )
    path = tmp_path / "hirax.bin"
    rows.tofile(path)

    signal = OutputSignal.from_file(path, instrument="hirax", dtype=np.int32)

    np.testing.assert_array_equal(signal.sync_out, [1, 0, 0])
    np.testing.assert_array_equal(signal.of, [0, 1, 0])
    np.testing.assert_array_equal(signal.real, [[10, 12, 14], [11, 13, 15]])
    np.testing.assert_array_equal(signal.imag, [[20, 22, 24], [21, 23, 25]])
    assert signal.instrument == "hirax"
    assert signal.fft_stages == 11
    assert signal.n_parallel_bits == 2


def test_output_signal_reads_chord_binary_rows(tmp_path):
    rows = np.array(
        [
            [1, 10, 20, 11, 21, 12, 22, 13, 23, 0],
            [0, 14, 24, 15, 25, 16, 26, 17, 27, 1],
        ],
        dtype=np.int32,
    )
    path = tmp_path / "chord.bin"
    rows.tofile(path)

    signal = OutputSignal.from_file(path, instrument="chord", dtype=np.int32)

    np.testing.assert_array_equal(signal.sync_out, [1, 0])
    np.testing.assert_array_equal(signal.of, [0, 1])
    np.testing.assert_array_equal(signal.real, [[10, 14], [11, 15], [12, 16], [13, 17]])
    np.testing.assert_array_equal(signal.imag, [[20, 24], [21, 25], [22, 26], [23, 27]])
    assert signal.fft_stages == 14
    assert signal.n_parallel_bits == 3


def test_output_signal_respects_dtype_parameter(tmp_path):
    rows = np.array([[1, -10, 20, -11, 21, 0]], dtype=np.int16)
    path = tmp_path / "hirax_i16.bin"
    rows.tofile(path)

    signal = OutputSignal.from_file(path, instrument="chime", dtype=np.int16)

    assert signal.real.dtype == np.dtype(np.int16)
    np.testing.assert_array_equal(signal.real, [[-10], [-11]])


def test_output_signal_get_real_imag_interleaves_lanes(tmp_path):
    rows = np.array(
        [
            [1, 10, 20, 11, 21, 0],
            [0, 12, 22, 13, 23, 1],
            [0, 14, 24, 15, 25, 0],
        ],
        dtype=np.int32,
    )
    path = tmp_path / "hirax.bin"
    rows.tofile(path)
    signal = OutputSignal.from_file(path, instrument="hirax", dtype=np.int32)

    real, imag = signal.get_real_imag()

    np.testing.assert_array_equal(real, [10, 11, 12, 13, 14, 15])
    np.testing.assert_array_equal(imag, [20, 21, 22, 23, 24, 25])


def test_output_signal_get_complex_matches_interleaved_real_imag(tmp_path):
    rows = np.array(
        [
            [1, 10, 20, 11, 21, 0],
            [0, 12, 22, 13, 23, 1],
        ],
        dtype=np.int32,
    )
    path = tmp_path / "hirax.bin"
    rows.tofile(path)
    signal = OutputSignal.from_file(path, instrument="hirax", dtype=np.int32)

    np.testing.assert_array_equal(
        signal.get_complex(),
        np.array([10 + 20j, 11 + 21j, 12 + 22j, 13 + 23j]),
    )


def test_output_signal_calc_fft_power_uses_interleaved_real_imag(tmp_path):
    rows = np.array(
        [
            [1, 3, 4, 5, 12, 0],
            [0, 8, 15, 7, 24, 1],
        ],
        dtype=np.int32,
    )
    path = tmp_path / "hirax.bin"
    rows.tofile(path)
    signal = OutputSignal.from_file(path, instrument="hirax", dtype=np.int32)

    np.testing.assert_array_equal(signal.calc_fft_power(), [25, 169, 289, 625])


def test_output_signal_descramble_changes_return_only(tmp_path):
    interleaved_real = np.arange(1024, dtype=np.int32)
    interleaved_imag = interleaved_real + 10000
    rows = np.empty((512, 6), dtype=np.int32)
    rows[:, 0] = 0
    rows[:, 1] = interleaved_real[0::2]
    rows[:, 2] = interleaved_imag[0::2]
    rows[:, 3] = interleaved_real[1::2]
    rows[:, 4] = interleaved_imag[1::2]
    rows[:, 5] = 0
    path = tmp_path / "hirax_frame.bin"
    rows.tofile(path)
    signal = OutputSignal.from_file(path, instrument="hirax", dtype=np.int32)
    stored_real = signal.real.copy()
    stored_imag = signal.imag.copy()

    real, imag = signal.get_real_imag(descramble=True)
    idxmap = casper_fft_descramble(10, 1)

    np.testing.assert_array_equal(real, interleaved_real[idxmap])
    np.testing.assert_array_equal(imag, interleaved_imag[idxmap])
    np.testing.assert_array_equal(signal.real, stored_real)
    np.testing.assert_array_equal(signal.imag, stored_imag)


def test_output_signal_descramble_requires_complete_frames(tmp_path):
    rows = np.array([[0, 10, 20, 11, 21, 0]], dtype=np.int32)
    path = tmp_path / "short.bin"
    rows.tofile(path)
    signal = OutputSignal.from_file(path, instrument="hirax", dtype=np.int32)

    with pytest.raises(ValueError, match="Cannot descramble"):
        signal.get_real_imag(descramble=True)


def test_output_signal_slice_and_strip_before_sync(tmp_path):
    rows = np.array(
        [
            [0, 10, 20, 11, 21, 0],
            [1, 12, 22, 13, 23, 1],
            [0, 14, 24, 15, 25, 0],
        ],
        dtype=np.int32,
    )
    path = tmp_path / "hirax.bin"
    rows.tofile(path)
    signal = OutputSignal.from_file(path, instrument="hirax", dtype=np.int32)

    stripped = signal.strip_before_sync()

    assert isinstance(stripped, OutputSignal)
    np.testing.assert_array_equal(stripped.sync_out, [0])
    np.testing.assert_array_equal(stripped.real, [[14], [15]])
    np.testing.assert_array_equal(signal.sync_out, [0, 1, 0])
    np.testing.assert_array_equal(signal.real, [[10, 12, 14], [11, 13, 15]])

    empty = stripped.slice(0, 0)
    assert isinstance(empty, OutputSignal)
    assert empty.sync_out.size == 0
    np.testing.assert_array_equal(stripped.sync_out, [0])


def test_output_signal_get_sync_frame_returns_one_complete_frame(tmp_path):
    interleaved_real = np.arange(1024, dtype=np.int32)
    interleaved_imag = interleaved_real + 10000
    rows = np.empty((514, 6), dtype=np.int32)
    rows[:, 0] = 0
    rows[:, 5] = 0
    rows[0, 0] = 1
    rows[:, 1] = np.arange(514, dtype=np.int32)
    rows[:, 2] = rows[:, 1] + 20000
    rows[:, 3] = rows[:, 1] + 30000
    rows[:, 4] = rows[:, 1] + 40000
    rows[1:513, 1] = interleaved_real[0::2]
    rows[1:513, 2] = interleaved_imag[0::2]
    rows[1:513, 3] = interleaved_real[1::2]
    rows[1:513, 4] = interleaved_imag[1::2]
    path = tmp_path / "hirax_frame.bin"
    rows.tofile(path)
    signal = OutputSignal.from_file(path, instrument="hirax", dtype=np.int32)

    frame = signal.get_sync_frame()

    assert isinstance(frame, OutputSignal)
    assert frame.sync_out.size == 512
    real, imag = frame.get_real_imag()
    np.testing.assert_array_equal(real, interleaved_real)
    np.testing.assert_array_equal(imag, interleaved_imag)


def test_output_signal_rejects_invalid_instrument(tmp_path):
    path = tmp_path / "output.bin"
    np.array([0, 1, 2, 3, 4, 5], dtype=np.int32).tofile(path)

    with pytest.raises(ValueError, match="Unknown instrument"):
        OutputSignal.from_file(path, instrument="bad", dtype=np.int32)


def test_output_signal_rejects_bad_file_length(tmp_path):
    path = tmp_path / "bad.bin"
    np.array([0, 1, 2, 3, 4], dtype=np.int32).tofile(path)

    with pytest.raises(ValueError, match="not divisible"):
        OutputSignal.from_file(path, instrument="hirax", dtype=np.int32)
