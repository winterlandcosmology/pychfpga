"""Convenience waveform generators for CASPER simulation input binaries.

These helpers build time-ordered ADC sample streams for supported board
geometries, then hand them to `InputSignal.from_array()` for signed lane
packing.  Frequencies are specified in MHz, and amplitudes are signed ADC
counts, not normalized fractions.  By default, one ADC frame is generated and
repeated for the requested number of frames so Simulink sees the same periodic
input that FUNCGEN replays on hardware.
"""

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .input_signal import InputSignal


@dataclass(frozen=True)
class InstrumentPreset:
    adc_bits: int
    n_inputs: int
    fft_stages: int
    sample_rate_mhz: float

    @property
    def frame_size(self):
        return 1 << self.fft_stages

    @property
    def max_ampl(self):
        return (1 << (self.adc_bits - 1)) - 1


INSTRUMENT_PRESETS = {
    "ice": InstrumentPreset(adc_bits=8, n_inputs=4, fft_stages=11, sample_rate_mhz=800.0),
    "crs": InstrumentPreset(adc_bits=14, n_inputs=8, fft_stages=14, sample_rate_mhz=1200.0),
}


def _get_preset(instrument):
    name = str(instrument).lower()
    try:
        return INSTRUMENT_PRESETS[name]
    except KeyError as exc:
        valid = ", ".join(sorted(INSTRUMENT_PRESETS))
        raise ValueError(f"Unknown instrument {instrument!r}; expected one of: {valid}") from exc


def _validate_common(instrument, nframes, ampl):
    preset = _get_preset(instrument)
    nframes = int(nframes)
    ampl = float(ampl)
    if nframes <= 0:
        raise ValueError(f"nframes must be positive, got {nframes}")
    if not np.isfinite(ampl) or ampl < 0 or ampl > preset.max_ampl:
        raise ValueError(
            f"ampl must be in [0, {preset.max_ampl}] signed ADC counts for {instrument!r}, got {ampl}"
        )
    return preset, nframes, ampl


def _save_if_requested(signal, save_path):
    if save_path is not None:
        signal.to_binary(Path(save_path))
    return signal


def generate_sine_input(instrument, nframes, ampl, freq, save_path=None, repeat_frame=True):
    """Generate one or more MHz sine tones as an `InputSignal`.

    `freq` may be a scalar or an array-like set of tone frequencies in MHz.
    When several tones are requested, their sum is averaged before applying
    `ampl`, so the requested amplitude remains the nominal full waveform scale.
    If `save_path` is provided, the binary file is written and the returned
    value is still the `InputSignal` object.

    With the default `repeat_frame=True`, only the first full ADC frame is
    generated and then tiled `nframes` times.  Set `repeat_frame=False` to
    generate a continuous multi-frame waveform for Simulink-only studies.
    """
    preset, nframes, ampl = _validate_common(instrument, nframes, ampl)
    freqs = np.atleast_1d(np.asarray(freq, dtype=np.float64))
    if freqs.size == 0 or np.any(~np.isfinite(freqs)):
        raise ValueError("freq must contain at least one finite MHz value")

    nsamp = preset.frame_size if repeat_frame else nframes * preset.frame_size
    n = np.arange(nsamp, dtype=np.float64)
    phases = 2 * np.pi * freqs[:, None] * n[None, :] / preset.sample_rate_mhz
    data = np.rint(ampl * np.sin(phases).mean(axis=0)).astype(np.int64)
    signal = InputSignal.from_array(
        data,
        adc_bits=preset.adc_bits,
        n_inputs=preset.n_inputs,
        repeat_frame=repeat_frame,
        frame_size=preset.frame_size,
        nframes=nframes,
    )
    return _save_if_requested(signal, save_path)


def generate_ramp_input(instrument, nframes, ampl, freq, save_path=None, repeat_frame=True):
    """Generate a zero-mean periodic sawtooth ramp as an `InputSignal`.

    `freq` is a scalar frequency in MHz.  The ramp covers `[-ampl, ampl]` over
    each period and is rounded to integer ADC counts before lane packing.
    `repeat_frame=True` tiles the first ADC frame for hardware/FUNCGEN
    comparisons.
    """
    preset, nframes, ampl = _validate_common(instrument, nframes, ampl)
    freq_arr = np.asarray(freq, dtype=np.float64)
    if freq_arr.ndim != 0 or not np.isfinite(freq_arr):
        raise ValueError("freq must be a finite MHz value")
    freq = float(freq_arr)

    nsamp = preset.frame_size if repeat_frame else nframes * preset.frame_size
    n = np.arange(nsamp, dtype=np.float64)
    phase = np.mod(freq * n / preset.sample_rate_mhz, 1.0)
    data = np.rint(ampl * (2.0 * phase - 1.0)).astype(np.int64)
    signal = InputSignal.from_array(
        data,
        adc_bits=preset.adc_bits,
        n_inputs=preset.n_inputs,
        repeat_frame=repeat_frame,
        frame_size=preset.frame_size,
        nframes=nframes,
    )
    return _save_if_requested(signal, save_path)


def generate_noise_input(instrument, nframes, ampl, seed=None, mode="gaussian", save_path=None, repeat_frame=True):
    """Generate seeded ADC noise as an `InputSignal`.

    The default `mode="gaussian"` draws natural Gaussian samples with standard
    deviation `ampl` and does not pre-clip; `InputSignal.from_array()` will warn
    and clip if samples exceed the signed ADC range.  `mode="integer"` draws
    bounded uniform integer samples in `[-ampl, ampl]`.  With the default
    `repeat_frame=True`, one random ADC frame is generated and then tiled for
    the requested number of frames.
    """
    preset, nframes, ampl = _validate_common(instrument, nframes, ampl)
    mode = str(mode).lower()
    nsamp = preset.frame_size if repeat_frame else nframes * preset.frame_size
    rng = np.random.default_rng(seed)

    if mode == "gaussian":
        data = rng.normal(0.0, ampl, nsamp)
    elif mode == "integer":
        int_ampl = int(np.floor(ampl))
        data = rng.integers(-int_ampl, int_ampl + 1, size=nsamp, dtype=np.int64)
    else:
        raise ValueError("mode must be either 'gaussian' or 'integer'")

    signal = InputSignal.from_array(
        data,
        adc_bits=preset.adc_bits,
        n_inputs=preset.n_inputs,
        repeat_frame=repeat_frame,
        frame_size=preset.frame_size,
        nframes=nframes,
    )
    return _save_if_requested(signal, save_path)
