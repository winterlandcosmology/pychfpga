import h5py
from pathlib import Path
from dataclasses import dataclass
import numpy as np


@dataclass
class Signals:
    t: np.ndarray
    out00_real: np.ndarray
    out00_imag: np.ndarray
    out01_real: np.ndarray
    out01_imag: np.ndarray
    of: np.ndarray
    sync_out: np.ndarray

    def __iter__(self):
        from dataclasses import fields
        for field in fields(self):
            yield field.name, getattr(self, field.name)

@dataclass
class InputSignals:
    t: np.ndarray
    adc0: np.ndarray
    adc1: np.ndarray
    adc2: np.ndarray
    adc3: np.ndarray


def read_casper_sim(path: Path):
    with h5py.File(path, "r") as f:
        arr = f["data"][()]
        return Signals(
            t=arr[:, 0],
            out00_real=arr[:, 1],
            out00_imag=arr[:, 2],
            out01_real=arr[:, 3],
            out01_imag=arr[:, 4],
            of=arr[:, 5],
            sync_out=arr[:, 6],
        )

def slice_data(signals, start: int = 0, end: int = None):
    if isinstance(end, int) and end > signals.t.size:
        raise ValueError(f"end index (currently {end}) must be smaller than signals.t.size={signals.t.size}")
    for name, value in signals:
        setattr(signals, name, value[start:end])
    return signals


def strip_data_before_sync(signals):
    sync_idx = np.argwhere(signals.sync_out == 1).item()
    return slice_data(signals, sync_idx+1)

def remix_real_imag(signals):
    real = np.empty(signals.out00_real.size*2, dtype=signals.out00_real.dtype)
    imag = np.empty(signals.out00_imag.size*2, dtype=signals.out00_imag.dtype)
    real[::2] = signals.out00_real[:]
    real[1::2] = signals.out01_real[:]
    imag[::2] = signals.out00_imag[:]
    imag[1::2] = signals.out01_imag[:]
    return real, imag

def calc_fft_power(signals):
    full_real, full_imag = remix_real_imag(signals)
    fft_power = full_real**2 + full_imag**2
    return fft_power


def casper_fft_descramble(n_bit_fft, n_bit_parallel):
    """
    Get the descramble map for a CASPER FFT with 2**n_bit_fft channels,
    presenting 2**n_bit_parallel on each cycle
    """
    n_fft = 2**n_bit_fft
    n_parallel = 2**n_bit_parallel
    return np.arange(n_fft).reshape(n_fft // n_parallel, n_parallel).transpose().flatten()

def unscramble_frame(frame: np.ndarray, n_bit_fft, n_bit_parallel):
    return frame[..., casper_fft_descramble(n_bit_fft, n_bit_parallel)]


def unscramble_signals(signals: Signals, n_bit_fft, n_bit_parallel):
    npoints = 2**(n_bit_fft - n_bit_parallel)
    nframes = len(signals.out00_real) / npoints
    if not nframes.is_integer():
        raise ValueError("The length of signals must include an integer number of full frames")
    else:
        nframes = int(nframes)
    idxmap = casper_fft_descramble(n_bit_fft-n_bit_parallel, 1)
    arridxmap = ((np.arange(nframes) * npoints)[:, None] + idxmap).ravel()
    signals.out00_real[:] = signals.out00_real[..., arridxmap]
    signals.out01_real[:] = signals.out01_real[..., arridxmap]
    signals.out00_imag[:] = signals.out00_imag[..., arridxmap]
    signals.out01_imag[:] = signals.out01_imag[..., arridxmap]
    signals.of[:] = signals.of[..., arridxmap]
    return signals


def convert_fft_to_integer(signals: Signals, nbits: int):
    dtype = np.int32 if nbits <= 32 else np.int64
    signals.out00_real = signals.out00_real.astype(dtype)
    signals.out01_real = signals.out01_real.astype(dtype)
    signals.out00_imag = signals.out00_imag.astype(dtype)
    signals.out01_imag = signals.out01_imag.astype(dtype)
    return signals



def load_sim(path: Path, nframes: int = None, n_bit_fft: int = None, n_bit_parallel: int = None, unscramble: bool = False):
    """

    """
    signals = read_casper_sim(path)
    signals = strip_data_before_sync(signals)
    # signals = convert_fft_to_integer(signals, 18+n_bit_fft)
    if nframes is not None:
        framesize = 2**(n_bit_fft - n_bit_parallel)
        signals = slice_data(signals, 0, nframes * framesize)
        if unscramble:
            signals = unscramble_signals(signals, n_bit_fft, n_bit_parallel)
    return signals


def load_input(path: Path):
    with h5py.File(path, "r") as f:
        arr = f["data"][()]
        signals = InputSignals(
            t=arr[:, 0],
            adc0=arr[:, 1],
            adc1=arr[:, 2],
            adc2=arr[:, 3],
            adc3=arr[:, 4],
        )
        return signals