import asyncio
import logging

import pytest
from wtl.metrics import Metrics

from pychfpga.fpga_firmware.chfpga.chfpga import chFPGA


class _FakeMotherboard:
    def get_id(self, *args, **kwargs):
        return (0, 0)


class _FakeScaler:
    def __init__(self):
        self.output_calls = []

    def set_scaler_output(self, data_type=None, bypass=None):
        self.output_calls.append((data_type, bypass))


class _FakeChannel:
    def __init__(self, prober=None):
        self.SCALER = _FakeScaler()
        self.PROBER = prober


class _FakeCorr:
    REQUIRES_OFFSET_BINARY_ENCODING = False


class _FakeCT:
    def __init__(self, cb1, cb2):
        self.CROSSBAR = cb1
        self.CROSSBAR2 = cb2


class _NoMetricsCrossbar:
    pass


class _MetricsCrossbar:
    def __init__(self):
        self.calls = []

    async def get_metrics(self, reset=True):
        self.calls.append(reset)
        return Metrics()


def _firmware(capture_type, receiver_exc):
    fw = object.__new__(chFPGA)
    fw.CAPTURE_TYPE = capture_type
    fw.default_channels = [0, 1]
    fw.chan = {0: _FakeChannel(), 1: _FakeChannel()}
    fw.logger = logging.getLogger(__name__)
    fw.mb = _FakeMotherboard()

    def get_receiver_config(channels=None):
        raise receiver_exc

    fw.get_receiver_config = get_receiver_config
    return fw


def test_prober_scaler_output_update_skips_mixed_stale_source_refresh():
    fw = _firmware(
        "PROBER",
        ValueError("Selected channels have different sources. Use separate receivers."),
    )

    fw.set_scaler_output_modes(bypass=False, channels=[0, 1])

    assert fw.chan[0].SCALER.output_calls == [(0, False)]
    assert fw.chan[1].SCALER.output_calls == [(0, False)]


def test_scaler_output_update_still_raises_other_receiver_refresh_errors():
    fw = _firmware("PROBER", ValueError("Selected channels have different FFT bypass settings."))

    with pytest.raises(ValueError, match="FFT bypass"):
        fw.set_scaler_output_modes(bypass=False, channels=[0, 1])


def test_scaler_output_update_still_raises_mixed_source_for_non_prober():
    fw = _firmware(
        "UCAP",
        ValueError("Selected channels have different sources. Use separate receivers."),
    )

    with pytest.raises(ValueError, match="different sources"):
        fw.set_scaler_output_modes(bypass=False, channels=[0, 1])


def test_offset_binary_mismatch_raises_clear_error():
    fw = object.__new__(chFPGA)
    fw.CORR = _FakeCorr()
    fw.logger = logging.getLogger(__name__)

    with pytest.raises(RuntimeError, match="correlator requires it to be False"):
        fw.set_offset_binary_encoding(offset=True, channels=[0, 1])


def test_crossbar_metrics_skips_crossbars_without_metrics(caplog):
    fw = object.__new__(chFPGA)
    fw.logger = logging.getLogger(__name__)
    cb = _MetricsCrossbar()
    fw.CT = _FakeCT(_NoMetricsCrossbar(), cb)
    fw.is_open = lambda: True

    async def clear_fpga_udp_errors():
        return None

    fw.clear_fpga_udp_errors = clear_fpga_udp_errors

    with caplog.at_level(logging.ERROR):
        asyncio.run(fw.get_crossbar_metrics_async(reset=False))

    assert cb.calls == [False]
    assert "Unhandled error while" not in caplog.text
