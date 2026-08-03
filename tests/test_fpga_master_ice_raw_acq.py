import asyncio
from pathlib import Path
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from wtl.config import load_yaml_config
from wtl.namespace import NameSpace

import pychfpga.fpga_master as fpga_master_mod
from pychfpga.common.raw_data_packet_processor import RawDataPacketProcessor
from pychfpga.fpga_firmware.chfpga.chfpga import chFPGA
from pychfpga.raw_acq import receiver as raw_acq_receiver_mod
from pychfpga.raw_acq.raw_data_processor import RawDataProcessor


ROOT = Path(__file__).resolve().parents[1]


def load_config(path, name):
    return NameSpace(load_yaml_config(f"{ROOT / path}:{name}"))


@pytest.mark.parametrize(
    "path,name",
    [
        ("config/config_icebox.yaml", "ICEBOX"),
        ("config/example_corr16.yaml", "EXAMPLE"),
        ("config/config_test.yaml", "TEST"),
    ],
)
def test_ice_corr16_configs_enable_raw_acq_with_ping(path, name):
    conf = load_config(path, name)

    assert conf.raw_acq.enable is True
    assert conf.raw_acq.no_ping is False
    assert conf.fpga.channelizer_params.offset_binary_encoding is False
    assert conf.fpga.compute_gains.data_capture.scaler_cap_data_type == "fft_full"
    assert conf.fpga.compute_gains.data_capture.frames_per_burst == 4
    assert conf.fpga.raw_data_capture.frames_per_burst == 1


def test_crs_raw_acq_config_keeps_current_settings():
    conf = load_config("tests/configs/corr8.yaml", "corr8")

    assert conf.raw_acq.enable is True
    assert conf.raw_acq.no_ping is True
    assert conf.fpga.channelizer_params.offset_binary_encoding is False
    assert conf.fpga.compute_gains.data_capture.scaler_cap_data_type == "fft_2x"
    assert conf.fpga.compute_gains.data_capture.ucap_output == "cap"
    assert conf.fpga.raw_data_capture.ucap_output == "corr"


def test_dlite_crs_capture_profiles_select_ucap_outputs():
    conf = load_config("config/config_dlite.yaml", "UAPISHKA")

    assert conf.fpga.compute_gains.data_capture.scaler_cap_data_type == "fft_2x"
    assert conf.fpga.compute_gains.data_capture.ucap_output == "cap"
    assert conf.fpga.raw_data_capture.ucap_output == "corr"


def test_correlator_soft_integ_period_updates_corr_config():
    corr = NameSpace(config=NameSpace())
    fm = fpga_master_mod.FPGAMaster()
    fm.fpgas = NameSpace(ib=[NameSpace(fpga=NameSpace(CORR=corr))])

    fm._set_correlator_soft_integ_period(7)

    assert corr.config.soft_integ_period == 7


class _CaptureTestLogger:
    def debug(self, message):
        pass

    def info(self, message):
        pass

    def warning(self, message):
        pass


class _CaptureTestProber:
    DATA_BUFFER_CAPACITY = 3
    DATA_SOURCE_TABLE = {
        "adc": 0,
        "funcgen": 0,
        "scaler": 1,
    }

    def __init__(self):
        self.RESET = 0
        self.SUB_PERIOD = 0
        self.source = None
        self.capture_config = None

    def set_data_source(self, source):
        self.source = source

    def config_capture(self, **config):
        self.capture_config = config


class _CaptureTestChannel:
    def __init__(self, number):
        self.chan_number = number
        self.PROBER = _CaptureTestProber()
        self.SCALER = SimpleNamespace(set_capture_data_type=lambda value: None)


class _CaptureTestUCAP:
    DATA_SOURCE_TABLE = {
        "funcgen": 0,
        "scaler": 255,
    }

    def __init__(self):
        self.USER_RESET = 0
        self.SUB_PERIOD = 0
        self.capture_config = None

    def set_data_capture(self, **config):
        self.capture_config = config


class _CaptureTestBoard:
    FRAME_PERIOD = 0.001
    FRAME_LENGTH = 2048

    def __init__(self, capture_type):
        self.CAPTURE_TYPE = capture_type or "PROBER"
        self.default_channels = [0, 1]
        self.logger = _CaptureTestLogger()
        self.chan_reset = 0
        self.trig = None
        self.receiver_config_updates = 0
        self._capture_pkt_frames_per_burst = None
        self._data_capture_configured = False
        self.chan = {
            ch: _CaptureTestChannel(ch) for ch in self.default_channels}
        self.GPIO = SimpleNamespace(HOST_FRAME_READ_RATE=0)
        self.UCAP = _CaptureTestUCAP()
        self.config = SimpleNamespace(
            chan={
                ch: SimpleNamespace(
                    data_source=None,
                    prober_sub_period=None,
                    scaler_cap_data_type=None)
                for ch in self.default_channels
            },
            raw_capture=SimpleNamespace(
                source=None,
                channels=(),
                frames_per_burst=1,
                target_mode=0),
            mode=0,
            source=None,
        )

    def get_receiver_config(self, channels=None):
        self.receiver_config_updates += 1

    def get_chan_reset(self):
        return self.chan_reset

    def set_chan_reset(self, state):
        self.chan_reset = state

    def _normalize_channels(self, channels):
        return list(channels)

    def get_channelizers(self, channels=None):
        channels = self.default_channels if channels is None else channels
        return [self.chan[ch] for ch in channels]

    def set_trig(self, state):
        self.trig = state

    def stop_data_capture(self):
        self.trig = False
        if self.CAPTURE_TYPE == "PROBER":
            for channel in self.chan.values():
                channel.PROBER.RESET = 1
        else:
            self.UCAP.USER_RESET = 1

    configure_data_capture = chFPGA.configure_data_capture
    start_data_capture = chFPGA.start_data_capture


@pytest.mark.parametrize(
    ("capture_type", "mode"),
    [("PROBER", 0), ("UCAP", 2)],
)
@pytest.mark.parametrize("sub_period", [10, 23])
def test_configure_data_capture_keeps_engine_disabled(
        capture_type, mode, sub_period):
    board = _CaptureTestBoard(capture_type)

    board.configure_data_capture(
        period=0.1,
        channels=[0, 1],
        source="funcgen",
        verbose=0,
        mode=mode,
        sub_period=sub_period)

    assert board.chan_reset == 0
    assert board.trig is False
    assert board.receiver_config_updates == 1
    assert board._data_capture_configured
    if capture_type == "PROBER":
        assert all(ch.PROBER.RESET == 1 for ch in board.chan.values())
        assert all(
            ch.PROBER.SUB_PERIOD == sub_period
            for ch in board.chan.values())
    else:
        assert board.UCAP.USER_RESET == 1
        assert board.UCAP.SUB_PERIOD == sub_period


@pytest.mark.parametrize(
    ("capture_type", "mode"),
    [("PROBER", 0), ("UCAP", 2)],
)
def test_argument_free_start_only_enables_configured_engine(
        capture_type, mode):
    board = _CaptureTestBoard(capture_type)

    board.configure_data_capture(
        period=0.1,
        channels=[0, 1],
        source="funcgen",
        verbose=0,
        mode=mode)
    board.start_data_capture()

    assert board.chan_reset == 0
    assert board.trig == 1
    assert board.receiver_config_updates == 1
    if capture_type == "PROBER":
        assert all(ch.PROBER.RESET == 0 for ch in board.chan.values())
    else:
        assert board.UCAP.USER_RESET == 0


def test_start_rejects_capture_configuration_arguments():
    board = _CaptureTestBoard("PROBER")

    with pytest.raises(TypeError):
        board.start_data_capture(frames_per_burst=4)


def test_start_before_configuration_raises():
    board = _CaptureTestBoard("PROBER")

    with pytest.raises(RuntimeError, match="must be configured"):
        board.start_data_capture()


def test_stop_then_start_reuses_cached_profile():
    board = _CaptureTestBoard("PROBER")
    board.configure_data_capture(
        period=0.1,
        channels=[0, 1],
        source="funcgen",
        verbose=0)

    board.start_data_capture()
    board.stop_data_capture()
    board.start_data_capture()

    assert board.receiver_config_updates == 1
    assert board.trig == 1
    assert all(ch.PROBER.RESET == 0 for ch in board.chan.values())


def test_configuration_failure_restores_channelizer_reset():
    board = _CaptureTestBoard("UCAP")
    board.chan_reset = 7

    def fail_configuration(**kwargs):
        raise RuntimeError("register write failed")

    board.UCAP.set_data_capture = fail_configuration

    with pytest.raises(RuntimeError, match="register write failed"):
        board.configure_data_capture(
            period=0.1,
            channels=[0, 1],
            source="funcgen",
            mode=2,
            verbose=0)

    assert board.chan_reset == 7
    assert board.UCAP.USER_RESET == 1
    assert not board._data_capture_configured


def test_dynamic_set_data_capture_still_updates_sub_period():
    board = _CaptureTestBoard("PROBER")

    chFPGA.set_data_capture(
        board,
        channels=[0, 1],
        source="funcgen",
        sub_period=10)

    assert all(ch.PROBER.SUB_PERIOD == 10 for ch in board.chan.values())
    assert board.config.raw_capture.source == "funcgen"
    assert board.config.raw_capture.channels == (0, 1)


class _FakeReceiverConfig:
    def as_dict(self):
        return {"raw": {}, "corr": {}}


class _FakeBoard:
    BSB_TRANSPORT_UDP = "udp"
    BSB_TRANSPORT_TCPIPE = "tcpipe"
    _PLATFORM_ID_MGK7MB_REV0 = 2
    _PLATFORM_ID_MGK7MB_REV2 = 3
    _PLATFORM_ID_CRS = 5

    def __init__(self, transport, capture_type=None):
        self.hostname = "fpga.example"
        self.port = 80
        self.BSB_TRANSPORT = transport
        self.CAPTURE_TYPE = capture_type
        self.PLATFORM_ID = (
            self._PLATFORM_ID_CRS if capture_type == "UCAP"
            else self._PLATFORM_ID_MGK7MB_REV2)
        self.fpga = self
        self.target_addr = None
        self.capture_source = "mixed"
        self.configure_data_capture_calls = []
        self.start_data_capture_calls = []
        self.set_data_capture_calls = []

    def get_channels(self):
        return [0, 1]

    def get_stream_ids(self, channels=None):
        channels = channels or self.get_channels()
        return [10 + ch for ch in channels]

    def configure_data_capture(self, **kwargs):
        self.configure_data_capture_calls.append(kwargs)
        self.capture_source = kwargs["source"]

    def start_data_capture(self):
        self.start_data_capture_calls.append({})

    def stop_data_capture(self):
        pass

    def set_data_capture(self, channels=None, source="adc", sub_period=23):
        self.set_data_capture_calls.append((channels, source, sub_period))
        self.capture_source = source

    def get_receiver_config(self):
        if self.CAPTURE_TYPE == "PROBER" and self.capture_source == "mixed":
            raise ValueError("Selected channels have different sources. Use separate receivers.")
        return _FakeReceiverConfig()

    def get_id(self, default_crate=0, default_slot=0):
        return (0, 0)

    async def set_udp_data_target_address_async(self, ip_addr, port, eth_addr):
        self.target_addr = (ip_addr, port, eth_addr)


class _FakeFPGAs:
    def __init__(self, board):
        self.board = board

    def get_iceboards(self, sources):
        return [self.board]


def _master_with_board(board):
    fm = fpga_master_mod.FPGAMaster()
    fm.config = NameSpace(
        {
            "raw_acq": {
                "use_fixed_port_numbers": True,
                "servers": {
                    "raw_acq0": {
                        "hostname": "localhost",
                        "port": 54322,
                        "receiver_ports": [
                            {"port": "myport", "sources": [{"crate": "*", "slot": "*"}]}
                        ],
                    }
                },
                "common_config": {
                    "comet_broker": {},
                    "jump_thresholds": [],
                    "metrics_refresh_time": 1,
                    "adc_rms_refresh_count": 1,
                },
            },
            "fpga": {
                "raw_data_capture": {
                    "capture_source": "adc",
                    "baseline_capture_rate": 10,
                    "capture_rate": 23,
                    "tmux_factor": 3,
                    "scaler_cap_data_type": 0,
                    "frames_per_burst": 1,
                },
                "sync_params": {"enable": False},
            },
        }
    )
    fm.fpgas = _FakeFPGAs(board)
    fm.data_folder = "/tmp"
    fm.run_folder = "/tmp"
    fm.run_name = "test"
    fm.corr_name = "test"
    return fm


class _FakeRawAcqClient:
    target_addr = []
    start_calls = []

    def __init__(self, *args, **kwargs):
        self.update_config_calls = []

    async def ping(self):
        return True

    async def start(self, **kwargs):
        self.start_calls.append(kwargs)
        return {"target_addr": self.target_addr}

    async def update_config(self, config):
        self.update_config_calls.append(config)


def test_ice_udp_raw_acq_requires_ping_for_target_mac(monkeypatch):
    board = _FakeBoard("udp")
    _FakeRawAcqClient.start_calls = []
    _FakeRawAcqClient.target_addr = [
        ((board.hostname, board.port), ("0.0.0.0", 54322, None))
    ]
    monkeypatch.setattr(fpga_master_mod, "RawAcqAsyncRESTClient", _FakeRawAcqClient)

    fm = _master_with_board(board)

    with pytest.raises(RuntimeError, match="raw_acq.no_ping: False"):
        asyncio.run(fm.start_raw_acq_servers())


def test_tcpipe_raw_acq_allows_no_ping_target(monkeypatch):
    board = _FakeBoard("tcpipe")
    _FakeRawAcqClient.start_calls = []
    _FakeRawAcqClient.target_addr = [
        ((board.hostname, board.port), ("0.0.0.0", 54322, None))
    ]
    monkeypatch.setattr(fpga_master_mod, "RawAcqAsyncRESTClient", _FakeRawAcqClient)

    fm = _master_with_board(board)

    asyncio.run(fm.start_raw_acq_servers())

    assert board.target_addr == ("0.0.0.0", 54322, None)


def test_udp_raw_acq_sources_include_explicit_ping_port(monkeypatch):
    board = _FakeBoard("udp")
    _FakeRawAcqClient.start_calls = []
    _FakeRawAcqClient.target_addr = [
        ((board.hostname, board.port), ("10.0.0.1", 54322, "aa:bb:cc:dd:ee:ff"))
    ]
    monkeypatch.setattr(fpga_master_mod, "RawAcqAsyncRESTClient", _FakeRawAcqClient)

    fm = _master_with_board(board)

    asyncio.run(fm.start_raw_acq_servers())

    assert _FakeRawAcqClient.start_calls[0]["ports"][0]["sources"] == [
        (board.hostname, board.port, board.port)
    ]


def test_raw_acq_initial_decoder_follows_programmed_gain_profile(monkeypatch):
    board = _FakeBoard("tcpipe", capture_type="PROBER")
    _FakeRawAcqClient.start_calls = []
    _FakeRawAcqClient.target_addr = [
        ((board.hostname, board.port), ("0.0.0.0", 54322, None))
    ]
    monkeypatch.setattr(fpga_master_mod, "RawAcqAsyncRESTClient", _FakeRawAcqClient)

    fm = _master_with_board(board)

    gain_capture = NameSpace({
        "capture_source": "scaler",
        "baseline_capture_rate": 10,
        "capture_rate": 12,
        "tmux_factor": 3,
        "scaler_cap_data_type": "fft_full",
        "frames_per_burst": 4,
    })
    asyncio.run(fm.start_raw_acq_servers(
        initial_capture_config=gain_capture,
        gain_capture=True))

    assert board.configure_data_capture_calls[0]["frames_per_burst"] == 4
    assert board.configure_data_capture_calls[0]["source"] == "scaler"
    assert board.configure_data_capture_calls[0]["data_type"] == "fft_full"
    assert board.configure_data_capture_calls[0]["sub_period"] == 12
    assert board.start_data_capture_calls == [{}]
    assert board.set_data_capture_calls == []


def test_non_prober_raw_acq_does_not_normalize_capture_source(monkeypatch):
    board = _FakeBoard("tcpipe", capture_type="UCAP")
    _FakeRawAcqClient.start_calls = []
    _FakeRawAcqClient.target_addr = [
        ((board.hostname, board.port), ("0.0.0.0", 54322, None))
    ]
    monkeypatch.setattr(fpga_master_mod, "RawAcqAsyncRESTClient", _FakeRawAcqClient)

    fm = _master_with_board(board)

    asyncio.run(fm.start_raw_acq_servers())

    assert board.set_data_capture_calls == []


class _FakeRawProcessor:
    def __init__(self, *args, **kwargs):
        pass


class _FakeCorrProcessor:
    def __init__(self, *args, **kwargs):
        pass


class _FakeSocket:
    def __init__(self, addr):
        ip_addr, port = addr
        self.addr = (ip_addr, 42424 if port == 0 else port)

    def getsockname(self):
        return self.addr


class _ProcessorThatMustNotBeFlushed:
    def flush(self):
        raise AssertionError("RawAcqReceiver.flush() must not flush processors")


def test_raw_acq_receiver_flush_without_active_buffer_clears_bookkeeping():
    receiver = raw_acq_receiver_mod.RawAcqReceiver()
    receiver.n_stored = 2
    receiver.pkt_len = np.array([64, 64, 0], dtype=np.uint32)
    receiver.processors = {"raw": _ProcessorThatMustNotBeFlushed()}

    assert receiver.flush() == 0
    assert receiver.n_stored == 0
    np.testing.assert_array_equal(receiver.pkt_len, np.zeros(3, dtype=np.uint32))


class _FlushRawProcessor:
    def __init__(self):
        self.packet_lengths = []

    def process_udp_packets(self):
        self.packet_lengths.append(self.pr.pkt_len[:self.pr.n_stored].copy())


class _FlushCorrProcessor:
    CORR_PACKET_COOKIE = 0xCF

    def __init__(self):
        self.packet_lengths = []
        self.consumed = []

    def process_udp_packets(self):
        self.packet_lengths.append(self.pr.pkt_len[:self.pr.n_stored].copy())

    def after_process_udp_packets(self, elapsed):
        pass

    def get_formatted_data(self):
        return "corr-result"

    def consume(self, result):
        self.consumed.append(result)


def test_raw_acq_receiver_flush_discards_mixed_raw_and_corr_batch():
    receiver = raw_acq_receiver_mod.RawAcqReceiver()
    receiver.BUF_SIZE = receiver.n_packets = 4
    receiver.buf = np.zeros((4, 64), dtype=np.uint8)
    receiver.pkt_len = np.array([64, 64, 64, 0], dtype=np.uint32)
    receiver.source_socket = np.zeros(4, dtype=np.uint16)
    receiver.n_stored = 3
    receiver.sockets = []
    receiver.received_packets = 0
    receiver.packet_max_processing_time = 0
    receiver.packet_current_processing_time = 0
    receiver.buf[:3, 0] = [0xA0, 0xA1, 0xCF]
    raw = _FlushRawProcessor()
    corr = _FlushCorrProcessor()
    raw.pr = receiver
    corr.pr = receiver
    receiver.processors = {"raw": raw, "corr": corr}

    assert receiver.flush(timeout=0) == 3

    assert raw.packet_lengths == []
    assert corr.packet_lengths == []
    assert corr.consumed == []
    assert receiver.n_stored == 0
    assert not receiver.pkt_len.any()


def test_raw_data_processor_formats_only_sources_present_in_batch(monkeypatch):
    """A one-frame ADC batch must not be passed to the retained fft_full formatter."""
    processor = object.__new__(RawDataProcessor)
    processor.n_out = 1
    processor.raw_source = np.zeros((1, 1, 1), dtype=np.uint8)
    adc_results = []
    fft_results = []
    processor.adc = SimpleNamespace(consume=adc_results.append)
    processor.fft = SimpleNamespace(consume=fft_results.append)
    formatted_sources = []

    def get_formatted_data(rows, source):
        formatted_sources.append(source)
        if source == 1:
            raise AssertionError("absent FFT source must not be formatted")
        return "ts", "data", "counts"

    processor.get_formatted_data = get_formatted_data
    monkeypatch.setattr(
        RawDataPacketProcessor,
        "process_udp_packets",
        lambda self, config=None, verbose=None: None)

    processor.process_udp_packets()

    assert formatted_sources == [0]
    assert adc_results == [("ts", "data", "counts", np.array([0]))]
    assert fft_results == []


class _BlockingRawProcessor:
    def __init__(self):
        self.processing = threading.Event()
        self.release = threading.Event()
        self.flush_calls = 0
        self.update_calls = []

    def process_udp_packets(self):
        self.processing.set()
        assert self.release.wait(1)

    def flush(self, flush_timeout=None):
        self.flush_calls += 1

    def update_config(self, **kwargs):
        self.update_calls.append(kwargs)


def test_raw_acq_config_update_waits_for_packet_processing():
    receiver = raw_acq_receiver_mod.RawAcqReceiver()
    receiver.n_stored = 1
    receiver.received_packets = 0
    receiver.packet_max_processing_time = 0
    receiver.packet_current_processing_time = 0
    raw = _BlockingRawProcessor()
    receiver.processors = {"raw": raw}
    processing_thread = threading.Thread(target=receiver.process_packets)
    processing_thread.start()
    assert raw.processing.wait(1)

    update_finished = threading.Event()

    def update():
        receiver.update_config({"raw": {"frames_per_burst": 1}})
        update_finished.set()

    update_thread = threading.Thread(target=update)
    update_thread.start()
    assert not update_finished.wait(0.05)
    raw.release.set()
    processing_thread.join(1)
    update_thread.join(1)

    assert not processing_thread.is_alive()
    assert not update_thread.is_alive()
    assert raw.flush_calls == 1
    assert len(raw.update_calls) == 1


class _NestedFlushRawProcessor:
    def __init__(self, receiver):
        self.receiver = receiver
        self.updated = False

    def flush(self, flush_timeout=None):
        self.receiver.flush(timeout=0)

    def update_config(self, **kwargs):
        self.updated = True


def test_raw_acq_nested_receiver_flush_does_not_deadlock():
    receiver = raw_acq_receiver_mod.RawAcqReceiver()
    receiver.BUF_SIZE = receiver.n_packets = 1
    receiver.buf = np.zeros((1, 64), dtype=np.uint8)
    receiver.pkt_len = np.zeros(1, dtype=np.uint32)
    receiver.source_socket = np.zeros(1, dtype=np.uint16)
    receiver.n_stored = 0
    receiver.sockets = []
    raw = _NestedFlushRawProcessor(receiver)
    receiver.processors = {"raw": raw}

    update_thread = threading.Thread(
        target=receiver.update_config,
        args=({"raw": {"frames_per_burst": 1}},))
    update_thread.start()
    update_thread.join(1)

    assert not update_thread.is_alive()
    assert raw.updated


class _BoundaryRawProcessor:
    def __init__(self, events=None):
        self.partial_rows = 3
        self.update_calls = []
        self.events = events

    def flush(self, flush_timeout=None):
        self.partial_rows = 0
        if self.events is not None:
            self.events.append("raw flush")

    def update_config(self, **kwargs):
        self.update_calls.append(kwargs)
        if self.events is not None:
            self.events.append("raw update")


class _BoundaryCorrProcessor:
    def __init__(self, events=None):
        self.partial_integrations = 2
        self.update_calls = []
        self.events = events

    def flush(self, flush_timeout=None):
        self.partial_integrations = 0
        if self.events is not None:
            self.events.append("corr flush")

    def update_config(self, config):
        self.update_calls.append(config)
        if self.events is not None:
            self.events.append("corr update")


def test_raw_acq_decoder_boundary_clears_raw_and_corr_assembly():
    receiver = raw_acq_receiver_mod.RawAcqReceiver()
    events = []
    raw = _BoundaryRawProcessor(events)
    corr = _BoundaryCorrProcessor(events)
    receiver.processors = {"raw": raw, "corr": corr}

    receiver.update_config({
        "raw": {"frames_per_burst": 1},
        "corr": {"soft_integ_period": 8},
    })

    assert raw.partial_rows == 0
    assert len(raw.update_calls) == 1
    assert corr.partial_integrations == 0
    assert corr.update_calls == [{"soft_integ_period": 8}]
    assert events == [
        "raw flush", "corr flush", "raw update", "corr update"]


def test_raw_acq_corr_only_update_does_not_flush_raw():
    receiver = raw_acq_receiver_mod.RawAcqReceiver()
    raw = _BoundaryRawProcessor()
    corr = _BoundaryCorrProcessor()
    receiver.processors = {"raw": raw, "corr": corr}

    receiver.update_config({"corr": {"soft_integ_period": 4}})

    assert raw.partial_rows == 3
    assert raw.update_calls == []
    assert corr.update_calls == [{"soft_integ_period": 4}]


class _NeverReadSocket:
    def __init__(self):
        self.recv_called = False

    def recv_into(self, target):
        self.recv_called = True
        raise AssertionError("stale select result must not be read")


def test_raw_acq_receiver_rechecks_stale_select_result(monkeypatch):
    receiver = raw_acq_receiver_mod.RawAcqReceiver()
    receiver.BUF_SIZE = receiver.n_packets = 1
    receiver.buf = np.zeros((1, 64), dtype=np.uint8)
    receiver.pkt_len = np.zeros(1, dtype=np.uint32)
    receiver.source_socket = np.zeros(1, dtype=np.uint16)
    receiver.n_stored = 0
    receiver.started = True
    sock = _NeverReadSocket()
    receiver.sockets = [sock]
    select_calls = 0

    def fake_select(read, write, error, timeout):
        nonlocal select_calls
        select_calls += 1
        return ([sock] if select_calls == 1 else [], [], [])

    monkeypatch.setattr(raw_acq_receiver_mod.select, "select", fake_select)
    receiver.receive_packets(
        timeout=0,
        stop_condition=lambda: select_calls >= 2)

    assert not sock.recv_called
    assert receiver.n_stored == 0


class _ContinuousRawSocket:
    def recv_into(self, target):
        target[0] = 0xA0
        return 64


def test_raw_acq_flush_rejects_continuing_raw_traffic(monkeypatch):
    receiver = raw_acq_receiver_mod.RawAcqReceiver()
    receiver.BUF_SIZE = receiver.n_packets = 2
    receiver.buf = np.zeros((2, 64), dtype=np.uint8)
    receiver.pkt_len = np.zeros(2, dtype=np.uint32)
    receiver.source_socket = np.zeros(2, dtype=np.uint16)
    receiver.n_stored = 0
    receiver.received_packets = 0
    receiver.packet_max_processing_time = 0
    receiver.packet_current_processing_time = 0
    receiver.processors = {}
    sock = _ContinuousRawSocket()
    receiver.sockets = [sock]
    monotonic_time = 0.0

    def monotonic():
        nonlocal monotonic_time
        monotonic_time += 0.002
        return monotonic_time

    monkeypatch.setattr(raw_acq_receiver_mod.time, "monotonic", monotonic)
    monkeypatch.setattr(
        raw_acq_receiver_mod.select,
        "select",
        lambda read, write, error, timeout: (list(read), [], []))

    with pytest.raises(RuntimeError, match="raw packets did not stop"):
        receiver.flush(timeout=0.001, max_flush_time=0.02)


def test_raw_acq_receiver_three_field_sources_return_source_key(monkeypatch):
    receiver = raw_acq_receiver_mod.RawAcqReceiver()
    ping_addrs = []

    async def ping(addr, timeout=0.3):
        ping_addrs.append(addr)
        return ("10.10.10.33", 53000)

    monkeypatch.setattr(raw_acq_receiver_mod, "RawDataProcessor", _FakeRawProcessor)
    monkeypatch.setattr(raw_acq_receiver_mod, "CorrDataProcessor", _FakeCorrProcessor)
    monkeypatch.setattr(receiver, "_ping_async", ping)
    monkeypatch.setattr(receiver, "get_udp_socket", lambda addr: _FakeSocket(addr))
    monkeypatch.setattr(receiver, "_get_mac_address", lambda if_addr: "aa:bb:cc:dd:ee:ff")

    result = asyncio.run(receiver.start_async(
        name="raw_acq0Recv",
        ports=[{"port": "myport", "sources": [("10.10.10.103", 80, 80)]}],
        stream_ids=[0],
        config={"raw": {}, "corr": {}},
        start_thread=False,
    ))

    assert ping_addrs == [("10.10.10.103", 80)]
    assert result["target_addr"] == [
        (("10.10.10.103", 80), ("10.10.10.33", 42424, "aa:bb:cc:dd:ee:ff"))
    ]


class _FakeCorrServer:
    def __init__(self):
        self.calls = []

    async def start_corr_hdf5(self, **kwargs):
        self.calls.append(kwargs)


class _FakeStoppedRawHDF5Server:
    async def stop_raw_hdf5(self):
        raise RuntimeError("HDF5 dataWriter is not running. Cannot stop it.")


class _FakeRawHDF5Server:
    def __init__(self):
        self.stop_calls = 0

    async def stop_raw_hdf5(self):
        self.stop_calls += 1


def test_corr_hdf5_capture_preserves_explicit_falsey_overrides():
    server = _FakeCorrServer()
    fm = fpga_master_mod.FPGAMaster()
    fm.config = NameSpace(
        {
            "fpga": {
                "firmware_correlator": {
                    "enable": True,
                    "hdf5_capture_folder": "/tmp/corr",
                    "hdf5_capture_filename": "data",
                    "hdf5_capture_duration": 60,
                    "hdf5_capture_elements_per_file": 8,
                    "hdf5_capture_n_inputs": 16,
                    "autocorr_only": True,
                    "software_integration_period": 250,
                }
            }
        }
    )
    fm.raw_acq = {"raw_acq0": server}
    fm.corr_firmware_integration_period = 16384
    fm.frame0_irigb_time = None

    asyncio.run(
        fm.start_corr_hdf5_capture(
            capture_duration=0,
            capture_n_inputs=[],
            autocorr_only=False,
            capture_elements_per_file=0,
        )
    )

    assert len(server.calls) == 1
    call = server.calls[0]
    assert call["capture_duration"] == 0
    assert call["capture_n_inputs"] == []
    assert call["autocorr_only"] is False
    assert call["elements_per_file"] == 0


def test_stop_allows_missing_iceboard_callback(monkeypatch):
    monkeypatch.setattr(fpga_master_mod, "reap_cached_sockets", lambda: None)
    fm = fpga_master_mod.FPGAMaster()
    fm.state = "on"
    fm.logging_handlers = {}
    fm.raw_acq = {}
    fm.gain_hdf5 = None

    asyncio.run(fm.stop())

    assert fm.state == "off"


def test_stop_allows_raw_hdf5_not_running(monkeypatch):
    monkeypatch.setattr(fpga_master_mod, "reap_cached_sockets", lambda: None)
    fm = fpga_master_mod.FPGAMaster()
    fm.state = "on"
    fm.logging_handlers = {}
    fm.raw_acq = {"raw_acq0": _FakeStoppedRawHDF5Server()}
    fm.gain_hdf5 = None

    asyncio.run(fm.stop())

    assert fm.state == "off"
    assert fm.raw_acq == {}


def test_stop_skips_raw_hdf5_stop_when_config_duration_is_none(monkeypatch):
    monkeypatch.setattr(fpga_master_mod, "reap_cached_sockets", lambda: None)
    server = _FakeRawHDF5Server()
    fm = fpga_master_mod.FPGAMaster()
    fm.state = "on"
    fm.logging_handlers = {}
    fm.raw_acq = {"raw_acq0": server}
    fm.gain_hdf5 = None
    fm.config = NameSpace(
        {"raw_acq": {"common_config": {"hdf5_capture_duration": None}}}
    )

    asyncio.run(fm.stop())

    assert fm.state == "off"
    assert server.stop_calls == 0
