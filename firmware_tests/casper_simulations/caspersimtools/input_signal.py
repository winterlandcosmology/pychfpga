"""Helpers for building ADC input vectors for CASPER/Simulink tests.

The simulation input path stores signed ADC counts exactly as they should
appear on the parallel ADC input ports: one row per ADC lane.  Binary files are
raw payload only, with samples written in simulation-clock order so Simulink's
Binary File Reader can consume them directly with `File header` set to
`struct([])` and a signed storage type such as `int8`.
"""

from dataclasses import dataclass
from pathlib import Path
import warnings

import numpy as np


def _signed_dtype_for_bits(nbits: int):
    """Return the smallest NumPy signed integer dtype that can hold nbits.

    Simulink should read these files as signed integers.  If it reads an 8-bit
    two's-complement payload as `uint8` and then converts numerically to
    `Fix_8_0`, values with the sign bit set saturate instead of becoming
    negative.  Using signed containers keeps the numeric interpretation correct.
    """
    if nbits <= 0 or nbits > 64:
        raise ValueError(f"adc_bits must be in [1, 64], got {nbits}")
    if nbits <= 8:
        return np.dtype(np.int8)
    if nbits <= 16:
        return np.dtype(np.int16)
    if nbits <= 32:
        return np.dtype(np.int32)
    return np.dtype(np.int64)


@dataclass
class InputSignal:
    """Packed parallel ADC input samples for a CASPER simulation.

    `data` is always a two-dimensional signed integer array with shape
    `(n_inputs, samples_per_input)`.  Values are signed ADC counts.  This
    matches the way the parallel Simulink input ports should consume samples:
    row 0 is ADC lane 0, row 1 is ADC lane 1, etc.
    """

    adc_bits: int
    n_inputs: int
    data: np.ndarray
    encoding: str = "twos_complement"

    def __post_init__(self):
        """Validate metadata and normalize data to the selected storage dtype."""
        self.adc_bits = int(self.adc_bits)
        self.n_inputs = int(self.n_inputs)
        if self.n_inputs <= 0:
            raise ValueError(f"n_inputs must be positive, got {self.n_inputs}")
        if self.encoding != "twos_complement":
            raise ValueError(f"Unsupported input encoding {self.encoding!r}")

        dtype = _signed_dtype_for_bits(self.adc_bits)
        data = np.asarray(self.data)
        if data.ndim != 2:
            raise ValueError(f"InputSignal data must be 2D, got shape {data.shape}")
        if data.shape[0] != self.n_inputs:
            raise ValueError(
                f"InputSignal data has {data.shape[0]} inputs, expected {self.n_inputs}"
            )
        if not np.issubdtype(data.dtype, np.signedinteger):
            raise TypeError(f"InputSignal data must use a signed integer dtype, got {data.dtype}")
        if np.dtype(data.dtype).itemsize < dtype.itemsize:
            raise TypeError(f"InputSignal data dtype {data.dtype} cannot hold {self.adc_bits} bits")
        self.data = np.ascontiguousarray(data, dtype=dtype)

    @property
    def storage_dtype(self):
        """Signed dtype used for ADC counts in memory and on disk."""
        return self.data.dtype

    @property
    def samples_per_input(self):
        """Number of time samples carried by each parallel ADC lane."""
        return self.data.shape[1]

    @classmethod
    def from_array(
        cls,
        data,
        adc_bits: int,
        n_inputs: int = 4,
        *,
        repeat_frame: bool = True,
        frame_size: int = None,
        nframes: int = None,
    ):
        """Create an InputSignal from a single time-ordered ADC sample stream.

        Samples are split across lanes in hardware order: sample 0 goes to lane
        0, sample 1 to lane 1, and so on until `n_inputs - 1`, then the next
        sample returns to lane 0.  Floating inputs are truncated toward zero to
        match NumPy integer casting and the old generator scripts.  Values that
        do not fit the signed ADC range are clipped, with warnings for both
        cases because either one changes the requested waveform.

        When `repeat_frame=True` and `frame_size` is supplied, only the first
        ADC frame is used as the source waveform and it is tiled `nframes`
        times.  This matches FUNCGEN playback, where one programmed ADC frame
        repeats continuously.  If `frame_size` is omitted, no repetition is
        applied so low-level callers can still pack exactly the samples they
        pass in.
        """
        adc_bits = int(adc_bits)
        n_inputs = int(n_inputs)
        dtype = _signed_dtype_for_bits(adc_bits)
        arr = np.asarray(data).ravel()
        if arr.size % n_inputs:
            raise ValueError(
                f"Input data has {arr.size} samples, which is not divisible by n_inputs={n_inputs}"
            )
        if np.iscomplexobj(arr):
            raise ValueError("InputSignal does not support complex ADC samples")

        if np.issubdtype(arr.dtype, np.integer):
            values = arr.astype(np.int64, copy=False)
        else:
            float_values = arr.astype(np.float64, copy=False)
            truncated = np.trunc(float_values)
            if np.any(float_values != truncated):
                warnings.warn(
                    "InputSignal.from_array truncated fractional ADC samples toward zero",
                    RuntimeWarning,
                    stacklevel=2,
                )
            values = truncated.astype(np.int64)

        min_value = -(1 << (adc_bits - 1))
        max_value = (1 << (adc_bits - 1)) - 1
        if np.any((values < min_value) | (values > max_value)):
            warnings.warn(
                f"InputSignal.from_array clipped samples outside signed {adc_bits}-bit range",
                RuntimeWarning,
                stacklevel=2,
            )
            values = np.clip(values, min_value, max_value)

        if repeat_frame and frame_size is not None:
            frame_size = int(frame_size)
            if frame_size <= 0:
                raise ValueError(f"frame_size must be positive, got {frame_size}")
            if frame_size % n_inputs:
                raise ValueError(
                    f"frame_size={frame_size} must be divisible by n_inputs={n_inputs}"
                )
            if values.size < frame_size:
                raise ValueError(
                    f"repeat_frame=True requires at least one full frame of {frame_size} samples, "
                    f"got {values.size}"
                )
            if nframes is None:
                if values.size % frame_size:
                    raise ValueError(
                        f"Input data has {values.size} samples, which is not divisible by frame_size={frame_size}"
                    )
                nframes = values.size // frame_size
            nframes = int(nframes)
            if nframes <= 0:
                raise ValueError(f"nframes must be positive, got {nframes}")
            values = np.tile(values[:frame_size], nframes)

        lane_data = values.astype(dtype, copy=False).reshape(-1, n_inputs).T
        return cls(adc_bits=adc_bits, n_inputs=n_inputs, data=lane_data)

    def to_binary(self, path):
        """Write a raw Simulink-ready binary input file.

        The file has no header.  Payload order is one simulation clock at a
        time: lane 0 sample 0, lane 1 sample 0, ..., lane N sample 0, then the
        next clock.  Configure Simulink's Binary File Reader with:

            File header: struct([])
            Storage data type: int8/int16/etc. matching `storage_dtype`
            Samples per frame: 1
            Number of channels: n_inputs
        """
        path = Path(path)
        if not path.suffix:
            path = path.with_suffix(".bin")

        payload = np.ascontiguousarray(self.data.T, dtype=self.storage_dtype.newbyteorder("<"))

        with path.open("wb") as f:
            f.write(payload.tobytes(order="C"))
        return path

    @classmethod
    def from_binary(cls, path, adc_bits: int, n_inputs: int = 4):
        """Read a raw binary file written by `to_binary`.

        Because the file has no header, the ADC width and parallel input count
        must be supplied by the caller.  The raw payload is interpreted in the
        same simulation-clock order written by `to_binary` and returned as
        lane-major `InputSignal.data`.
        """
        path = Path(path)
        adc_bits = int(adc_bits)
        n_inputs = int(n_inputs)
        dtype = _signed_dtype_for_bits(adc_bits)
        payload = np.fromfile(path, dtype=dtype.newbyteorder("<"))

        if payload.size % n_inputs:
            raise ValueError(
                f"{path} payload has {payload.size} samples, which is not divisible by n_inputs={n_inputs}"
            )
        data = payload.reshape(-1, n_inputs).T.astype(dtype, copy=False)
        return cls(
            adc_bits=adc_bits,
            n_inputs=n_inputs,
            data=data,
        )
