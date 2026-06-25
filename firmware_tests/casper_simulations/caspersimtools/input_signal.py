"""Helpers for building ADC input vectors for CASPER/Simulink tests.

The simulation input path stores the values exactly as they should appear on
the parallel ADC input ports: one row per ADC lane, with signed ADC samples
encoded as unsigned two's-complement words.  The binary wrapper adds a small
self-describing JSON header so a MATLAB script can recover the lane count, ADC
width, and storage dtype before reading the raw payload.
"""

from dataclasses import dataclass
import json
from pathlib import Path
import warnings

import numpy as np


def _uint_dtype_for_bits(nbits: int):
    """Return the smallest NumPy unsigned integer dtype that can hold nbits.

    The file stores raw ADC codes rather than Python integers, so keeping the
    dtype as small as possible makes the binary payload match the requested ADC
    width without wasting more bytes than necessary.
    """
    if nbits <= 0 or nbits > 64:
        raise ValueError(f"adc_bits must be in [1, 64], got {nbits}")
    if nbits <= 8:
        return np.dtype(np.uint8)
    if nbits <= 16:
        return np.dtype(np.uint16)
    if nbits <= 32:
        return np.dtype(np.uint32)
    return np.dtype(np.uint64)


@dataclass
class InputSignal:
    """Packed parallel ADC input samples for a CASPER simulation.

    `data` is always a two-dimensional unsigned integer array with shape
    `(n_inputs, samples_per_input)`.  Values are raw two's-complement ADC codes,
    not decoded signed integers.  This matches the way the parallel Simulink
    input ports consume samples: row 0 is ADC lane 0, row 1 is ADC lane 1, etc.
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

        dtype = _uint_dtype_for_bits(self.adc_bits)
        data = np.asarray(self.data)
        if data.ndim != 2:
            raise ValueError(f"InputSignal data must be 2D, got shape {data.shape}")
        if data.shape[0] != self.n_inputs:
            raise ValueError(
                f"InputSignal data has {data.shape[0]} inputs, expected {self.n_inputs}"
            )
        if not np.issubdtype(data.dtype, np.unsignedinteger):
            raise TypeError(f"InputSignal data must use an unsigned integer dtype, got {data.dtype}")
        if np.dtype(data.dtype).itemsize < dtype.itemsize:
            raise TypeError(f"InputSignal data dtype {data.dtype} cannot hold {self.adc_bits} bits")
        self.data = np.ascontiguousarray(data, dtype=dtype)

    @property
    def storage_dtype(self):
        """Unsigned dtype used for the raw ADC codes in memory and on disk."""
        return self.data.dtype

    @property
    def samples_per_input(self):
        """Number of time samples carried by each parallel ADC lane."""
        return self.data.shape[1]

    @classmethod
    def from_array(cls, data, adc_bits: int, n_inputs: int = 4):
        """Create an InputSignal from a single time-ordered ADC sample stream.

        Samples are split across lanes in hardware order: sample 0 goes to lane
        0, sample 1 to lane 1, and so on until `n_inputs - 1`, then the next
        sample returns to lane 0.  Floating inputs are truncated toward zero to
        match NumPy integer casting and the old generator scripts.  Values that
        do not fit the signed ADC range are clipped, with warnings for both
        cases because either one changes the requested waveform.
        """
        adc_bits = int(adc_bits)
        n_inputs = int(n_inputs)
        dtype = _uint_dtype_for_bits(adc_bits)
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

        modulus = 1 << adc_bits
        raw_values = np.array(
            [int(value) if value >= 0 else int(value) + modulus for value in values],
            dtype=dtype,
        )
        lane_data = raw_values.reshape(-1, n_inputs).T
        return cls(adc_bits=adc_bits, n_inputs=n_inputs, data=lane_data)

    def to_binary(self, path):
        """Write this signal to a self-describing binary input file.

        File layout is:
            uint32 little-endian JSON header length
            UTF-8 JSON metadata
            raw lane payload in C order

        MATLAB can read this by reading the header length, passing the JSON
        bytes through `jsondecode`, then reading the remaining payload using the
        `storage_dtype` recorded in metadata.
        """
        path = Path(path)
        if not path.suffix:
            path = path.with_suffix(".bin")

        metadata = {
            "adc_bits": self.adc_bits,
            "n_inputs": self.n_inputs,
            "storage_dtype": self.storage_dtype.name,
            "encoding": self.encoding,
            "sample_count": int(self.data.size),
            "samples_per_input": int(self.samples_per_input),
            "payload_shape": list(self.data.shape),
            "payload_order": "C",
            "byte_order": "little",
        }
        header = json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode("utf-8")
        payload = np.ascontiguousarray(self.data, dtype=self.storage_dtype.newbyteorder("<"))

        with path.open("wb") as f:
            f.write(np.array([len(header)], dtype="<u4").tobytes())
            f.write(header)
            f.write(payload.tobytes(order="C"))
        return path

    @classmethod
    def from_binary(cls, path):
        """Read a binary file written by `to_binary`.

        The metadata drives the payload dtype and shape, so this is a strict
        round-trip reader rather than a generic binary loader.  A missing header
        length or unexpected payload length is treated as a format error.
        """
        path = Path(path)
        with path.open("rb") as f:
            header_size_raw = f.read(4)
            if len(header_size_raw) != 4:
                raise ValueError(f"{path} is missing an InputSignal header length")
            header_size = int(np.frombuffer(header_size_raw, dtype="<u4")[0])
            metadata = json.loads(f.read(header_size).decode("utf-8"))

            dtype = np.dtype(metadata["storage_dtype"]).newbyteorder("<")
            payload_shape = tuple(metadata["payload_shape"])
            payload = np.fromfile(f, dtype=dtype)

        expected_size = int(np.prod(payload_shape))
        if payload.size != expected_size:
            raise ValueError(
                f"{path} payload has {payload.size} samples, expected {expected_size}"
            )
        data = payload.reshape(payload_shape).astype(np.dtype(metadata["storage_dtype"]), copy=False)
        return cls(
            adc_bits=metadata["adc_bits"],
            n_inputs=metadata["n_inputs"],
            data=data,
            encoding=metadata.get("encoding", "twos_complement"),
        )
