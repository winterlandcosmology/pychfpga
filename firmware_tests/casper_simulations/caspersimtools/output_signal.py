from dataclasses import dataclass, field
from pathlib import Path

import numpy as np


OUTPUT_PRESETS = {
    "ice": {"n_outputs": 2, "fft_stages": 11, "n_parallel_bits": 2},
    "crs": {"n_outputs": 4, "fft_stages": 14, "n_parallel_bits": 3},
}


def _preset_for_instrument(instrument):
    name = str(instrument).lower()
    try:
        return OUTPUT_PRESETS[name]
    except KeyError as exc:
        valid = ", ".join(sorted(OUTPUT_PRESETS))
        raise ValueError(f"Unknown instrument {instrument!r}; expected one of: {valid}") from exc


def casper_fft_descramble(n_bit_fft, n_bit_parallel):
    """Return the CASPER FFT descramble map for one complete FFT frame."""
    n_fft = 2**n_bit_fft
    n_parallel = 2**n_bit_parallel
    return np.arange(n_fft).reshape(n_fft // n_parallel, n_parallel).transpose().flatten()


@dataclass
class OutputSignal:
    """Parallel FFT output samples from a CASPER simulation binary.

    Each binary row is one simulation clock:

        sync_out, out00_real, out00_imag, ..., out0N_real, out0N_imag, of

    `real` and `imag` are two-dimensional arrays shaped
    `(n_outputs, nsamples)`, so `real[0]` is out00 real, `imag[0]` is out00
    imag, and so on.  No time axis is stored.

    `n_outputs` is the number of saved complex output lanes.  `n_parallel_bits`
    is the FFT scalar parallelism.  For ICE, each clock emits four scalar
    values, saved as two complex outputs: out00 real/imag and out01 real/imag.
    """

    sync_out: np.ndarray
    real: np.ndarray
    imag: np.ndarray
    of: np.ndarray
    instrument: str
    fft_stages: int = field(init=False)
    n_parallel_bits: int = field(init=False)

    def __post_init__(self):
        preset = _preset_for_instrument(self.instrument)
        self.instrument = str(self.instrument).lower()
        self.fft_stages = preset["fft_stages"]
        self.n_parallel_bits = preset["n_parallel_bits"]

        self.sync_out = np.asarray(self.sync_out)
        self.real = np.asarray(self.real)
        self.imag = np.asarray(self.imag)
        self.of = np.asarray(self.of)

        if self.sync_out.ndim != 1:
            raise ValueError(f"sync_out must be 1D, got shape {self.sync_out.shape}")
        if self.of.ndim != 1:
            raise ValueError(f"of must be 1D, got shape {self.of.shape}")
        if self.real.ndim != 2:
            raise ValueError(f"real must be 2D, got shape {self.real.shape}")
        if self.imag.ndim != 2:
            raise ValueError(f"imag must be 2D, got shape {self.imag.shape}")
        if self.real.shape != self.imag.shape:
            raise ValueError(f"real shape {self.real.shape} must match imag shape {self.imag.shape}")
        if self.real.shape[0] != preset["n_outputs"]:
            raise ValueError(
                f"{self.instrument!r} expects {preset['n_outputs']} outputs, got {self.real.shape[0]}"
            )
        if self.real.shape[1] != self.sync_out.size or self.of.size != self.sync_out.size:
            raise ValueError("sync_out, of, real, and imag must contain the same number of samples")

    @classmethod
    def from_file(cls, path: Path, instrument: str, dtype):
        """Read raw MATLAB/Simulink binary output for an instrument.

        The file must contain rows laid out as:

            sync_out, out00_real, out00_imag, ..., out0N_real, out0N_imag, of

        ICE uses two output lanes.  CRS uses four output lanes.
        """
        preset = _preset_for_instrument(instrument)
        dtype = np.dtype(dtype)
        arr = np.fromfile(path, dtype=dtype)
        values_per_row = 2 + 2 * preset["n_outputs"]
        if arr.size % values_per_row:
            raise ValueError(
                f"{path} contains {arr.size} {dtype} values, which is not divisible "
                f"by {values_per_row} values per {instrument!r} output vector"
            )

        rows = arr.reshape(-1, values_per_row)
        return cls(
            sync_out=rows[:, 0],
            real=rows[:, 1:-1:2].T,
            imag=rows[:, 2:-1:2].T,
            of=rows[:, -1],
            instrument=instrument,
        )

    def slice(self, start: int = 0, end: int = None):
        if isinstance(end, int) and end > self.sync_out.size:
            raise ValueError(f"end index (currently {end}) must be smaller than signals size={self.sync_out.size}")
        return type(self)(
            sync_out=self.sync_out[start:end],
            real=self.real[:, start:end],
            imag=self.imag[:, start:end],
            of=self.of[start:end],
            instrument=self.instrument,
        )

    def strip_before_sync(self):
        sync_idx = np.argwhere(self.sync_out == 1).item()
        return self.slice(sync_idx + 1)

    def get_sync_frame(self):
        frame_rows = 1 << (self.fft_stages - self.n_parallel_bits)
        return self.strip_before_sync().slice(0, frame_rows)

    def get_real_imag(self, descramble=False):
        """Return lane-interleaved real and imaginary arrays.

        With `descramble=False`, samples are ordered out00, out01, ... for each
        simulation clock.  With `descramble=True`, the CASPER FFT descramble map
        is applied only to the returned arrays; stored data is not modified.
        """
        real = self.real.T.ravel()
        imag = self.imag.T.ravel()
        if descramble:
            real = self._descramble_array(real)
            imag = self._descramble_array(imag)
        return real, imag

    def get_complex(self, descramble=False):
        real, imag = self.get_real_imag(descramble=descramble)
        return real + 1j * imag

    def calc_fft_power(self, descramble=False):
        real, imag = self.get_real_imag(descramble=descramble)
        return real**2 + imag**2

    def _descramble_array(self, arr):
        frame_size = 1 << (self.fft_stages - 1)
        complex_parallel_bits = self.n_parallel_bits - 1
        if arr.size % frame_size:
            raise ValueError(
                f"Cannot descramble {arr.size} values into {frame_size}-sample FFT output frames"
            )
        idxmap = casper_fft_descramble(self.fft_stages - 1, complex_parallel_bits)
        return arr.reshape(-1, frame_size)[:, idxmap].ravel()
