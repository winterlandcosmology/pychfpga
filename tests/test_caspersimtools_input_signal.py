import numpy as np
import pytest

from firmware_tests.casper_simulations.caspersimtools import (
    InputSignal,
    generate_noise_input,
    generate_ramp_input,
    generate_sine_input,
)


def test_input_signal_from_array_orders_four_parallel_inputs():
    signal = InputSignal.from_array(np.arange(12), adc_bits=8, n_inputs=4)

    np.testing.assert_array_equal(
        signal.data,
        np.array(
            [
                [0, 4, 8],
                [1, 5, 9],
                [2, 6, 10],
                [3, 7, 11],
            ],
            dtype=np.uint8,
        ),
    )


def test_input_signal_from_array_orders_non_four_parallel_inputs():
    signal = InputSignal.from_array(np.arange(10) - 5, adc_bits=8, n_inputs=2)

    np.testing.assert_array_equal(
        signal.data,
        np.array(
            [
                [251, 253, 255, 1, 3],
                [252, 254, 0, 2, 4],
            ],
            dtype=np.uint8,
        ),
    )


@pytest.mark.parametrize(
    ("adc_bits", "expected_dtype"),
    [
        (8, np.uint8),
        (16, np.uint16),
        (24, np.uint32),
        (40, np.uint64),
    ],
)
def test_input_signal_selects_storage_dtype(adc_bits, expected_dtype):
    signal = InputSignal.from_array(np.zeros(4), adc_bits=adc_bits, n_inputs=4)

    assert signal.storage_dtype == np.dtype(expected_dtype)


def test_input_signal_encodes_signed_values_as_twos_complement():
    signal = InputSignal.from_array([-128, -1, 0, 127], adc_bits=8, n_inputs=4)

    np.testing.assert_array_equal(
        signal.data[:, 0],
        np.array([128, 255, 0, 127], dtype=np.uint8),
    )


def test_input_signal_warns_on_fractional_truncation():
    with pytest.warns(RuntimeWarning, match="truncated fractional"):
        signal = InputSignal.from_array([1.9, -1.9, 0, 2], adc_bits=8, n_inputs=4)

    np.testing.assert_array_equal(signal.data[:, 0], np.array([1, 255, 0, 2], dtype=np.uint8))


def test_input_signal_warns_and_clips_out_of_range_values():
    with pytest.warns(RuntimeWarning, match="clipped samples"):
        signal = InputSignal.from_array([-200, 200], adc_bits=8, n_inputs=2)

    np.testing.assert_array_equal(signal.data[:, 0], np.array([128, 127], dtype=np.uint8))


def test_input_signal_binary_round_trip(tmp_path):
    signal = InputSignal.from_array(np.arange(-8, 8), adc_bits=12, n_inputs=4)

    path = signal.to_binary(tmp_path / "input")
    restored = InputSignal.from_binary(path)

    assert path.name == "input.bin"
    assert restored.adc_bits == signal.adc_bits
    assert restored.n_inputs == signal.n_inputs
    assert restored.encoding == signal.encoding
    assert restored.storage_dtype == signal.storage_dtype
    np.testing.assert_array_equal(restored.data, signal.data)


@pytest.mark.parametrize(
    ("instrument", "adc_bits", "n_inputs", "frame_size"),
    [
        ("hirax", 8, 4, 2048),
        ("chime", 8, 4, 2048),
        ("chord", 12, 8, 16384),
    ],
)
def test_shortcuts_use_instrument_presets(instrument, adc_bits, n_inputs, frame_size):
    signal = generate_sine_input(instrument, nframes=2, ampl=4, freq=1.0)

    assert signal.adc_bits == adc_bits
    assert signal.n_inputs == n_inputs
    assert signal.samples_per_input == 2 * frame_size // n_inputs


def test_generate_sine_input_accepts_multiple_mhz_frequencies():
    signal = generate_sine_input("hirax", nframes=1, ampl=32, freq=[150.0, 210.0])

    assert signal.data.shape == (4, 512)
    assert signal.storage_dtype == np.dtype(np.uint8)


def test_generate_ramp_input_uses_scalar_mhz_frequency():
    signal = generate_ramp_input("hirax", nframes=1, ampl=32, freq=100.0)

    assert signal.data.shape == (4, 512)
    assert np.unique(signal.data).size > 1


def test_generate_noise_input_defaults_to_reproducible_gaussian():
    with pytest.warns(RuntimeWarning, match="truncated fractional"):
        signal_a = generate_noise_input("hirax", nframes=1, ampl=8, seed=123)
    with pytest.warns(RuntimeWarning, match="truncated fractional"):
        signal_b = generate_noise_input("hirax", nframes=1, ampl=8, seed=123)

    np.testing.assert_array_equal(signal_a.data, signal_b.data)


def test_generate_noise_input_integer_mode_is_reproducible():
    signal_a = generate_noise_input("hirax", nframes=1, ampl=8, seed=123, mode="integer")
    signal_b = generate_noise_input("hirax", nframes=1, ampl=8, seed=123, mode="integer")

    np.testing.assert_array_equal(signal_a.data, signal_b.data)


def test_shortcut_save_path_writes_binary_and_returns_signal(tmp_path):
    path = tmp_path / "sine_input.bin"

    signal = generate_sine_input("hirax", nframes=1, ampl=4, freq=1.0, save_path=path)
    restored = InputSignal.from_binary(path)

    assert isinstance(signal, InputSignal)
    np.testing.assert_array_equal(restored.data, signal.data)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        (dict(instrument="bad", nframes=1, ampl=1, freq=1), "Unknown instrument"),
        (dict(instrument="hirax", nframes=0, ampl=1, freq=1), "nframes must be positive"),
        (dict(instrument="hirax", nframes=1, ampl=128, freq=1), "ampl must be in"),
        (dict(instrument="hirax", nframes=1, ampl=1, freq=np.nan), "freq must"),
    ],
)
def test_waveform_shortcuts_reject_invalid_common_inputs(kwargs, match):
    with pytest.raises(ValueError, match=match):
        generate_sine_input(**kwargs)


def test_generate_noise_input_rejects_invalid_mode():
    with pytest.raises(ValueError, match="mode must"):
        generate_noise_input("hirax", nframes=1, ampl=8, mode="bad")
