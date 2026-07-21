import asyncio
import logging

import pytest
from wtl.metrics import Metrics

from pychfpga.common.state import State
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


class _FakeProber:
    def __init__(self, reset=0):
        self._reset = int(reset)
        self.reset_writes = []
        self.BURST_LENGTH = 0

    @property
    def RESET(self):
        return self._reset

    @RESET.setter
    def RESET(self, value):
        self._reset = int(value)
        self.reset_writes.append(int(value))


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


def _prober_firmware(reset_states=(0, 1)):
    fw = object.__new__(chFPGA)
    fw.CAPTURE_TYPE = 'PROBER'
    fw.default_channels = [0, 1]
    fw.chan = {
        ch: _FakeChannel(_FakeProber(reset=reset_states[ch]))
        for ch in fw.default_channels
    }
    fw._capture_pkt_frames_per_burst = 1
    fw.config = State(
        raw_capture=State(frames_per_burst=1),
        chan={ch: State() for ch in fw.default_channels},
    )
    fw.receiver_refreshes = []

    def get_receiver_config(channels=None):
        fw.receiver_refreshes.append(list(channels))
        return 'receiver-config'

    fw.get_receiver_config = get_receiver_config
    return fw


def test_set_data_capture_frames_per_burst_updates_prober_and_metadata():
    fw = _prober_firmware()

    result = fw.set_data_capture_frames_per_burst(4, channels=[0, 1])

    assert result == 'receiver-config'
    assert [fw.chan[ch].PROBER.BURST_LENGTH for ch in [0, 1]] == [3, 3]
    assert fw.chan[0].PROBER.reset_writes == [1, 0]
    assert fw.chan[1].PROBER.reset_writes == [1, 1]
    assert fw._capture_pkt_frames_per_burst == 4
    assert fw.config.raw_capture.frames_per_burst == 4
    assert fw.receiver_refreshes == [[0, 1]]


@pytest.mark.parametrize('frames_per_burst', [0, 5])
def test_set_data_capture_frames_per_burst_rejects_invalid_values(frames_per_burst):
    fw = _prober_firmware()

    with pytest.raises(ValueError, match=r'range \[1, 4\]'):
        fw.set_data_capture_frames_per_burst(frames_per_burst)

    assert fw.chan[0].PROBER.reset_writes == []


def test_set_data_capture_frames_per_burst_rejects_ucap():
    fw = _prober_firmware()
    fw.CAPTURE_TYPE = 'UCAP'

    with pytest.raises(RuntimeError, match='only by PROBER'):
        fw.set_data_capture_frames_per_burst(4)


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
