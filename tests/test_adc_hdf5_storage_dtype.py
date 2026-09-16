"""ADC HDF5 layout from storage_dtype / bytes_per_sample config."""

import numpy as np
import pytest

from pychfpga.common.state import State
from pychfpga.raw_acq.adc_data_processor import ADCDataProcessor


def test_storage_dtype_from_explicit_string():
    dt = ADCDataProcessor.storage_dtype_from_config(State(storage_dtype="i4", bytes_per_sample=4))
    assert dt == np.dtype("i4")


def test_storage_dtype_from_bytes_per_sample():
    dt = ADCDataProcessor.storage_dtype_from_config(State(bytes_per_sample=2))
    assert dt == np.dtype("i2")


def _proc_16bit(**kw):
    from test_raw_data_processor import DummyPacketReceiver, make_processor

    raw = make_processor(
        n_packets=4,
        format="16",
        bytes_per_sample=2,
        bytes_per_frame=8,
        samples_per_frame=4,
    )[0]
    cfg = State(
        bytes_per_sample=2,
        storage_dtype="i2",
        **kw,
    )
    return ADCDataProcessor(None, raw, config_getter=lambda: cfg)


def test_channel_frame_uses_format_data_for_16bit_be():
    """Match ``read_raw_frames(format='16')``: big-endian int16 on the wire."""
    wire = np.array([0, 12, 0, 36, 0, 60, 0, 84], dtype=np.uint8)
    p = _proc_16bit()
    p.update_config()
    p.raw.raw_data[0, 0, : wire.size] = wire
    p.raw.ts_array[0] = 0
    p.raw.raw_data_count[0, 0, :] = p.raw.expected_packets
    p.raw.raw_source[0, 0, :] = 0
    _, formatted, _ = p.raw.get_formatted_data(slice(0, 1), source=0)
    frame = np.asarray(formatted[0, 0], dtype=p.adc_storage_dtype)
    np.testing.assert_array_equal(frame, [12, 36, 60, 84])


def test_channel_frame_full_scale_sinewave_shape():
    p = _proc_16bit()
    p.update_config()
    p.raw.BYTES_PER_FRAME = 32768
    p.raw.FRAMES_PER_BURST = 1
    p._sync_adc_hdf5_layout()
    assert p.adc_samples_per_frame == 16384
    assert p.adc_storage_dtype == np.dtype("i2")
