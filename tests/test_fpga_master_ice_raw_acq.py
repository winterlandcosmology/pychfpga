import asyncio
from pathlib import Path

import numpy as np
import pytest

from wtl.config import load_yaml_config
from wtl.namespace import NameSpace

import pychfpga.fpga_master as fpga_master_mod
from pychfpga.raw_acq import receiver as raw_acq_receiver_mod


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
        self.scaler_calls = []
        self.start_data_capture_calls = []
        self.set_data_capture_calls = []

    def get_channels(self):
        return [0, 1]

    def get_stream_ids(self, channels=None):
        channels = channels or self.get_channels()
        return [10 + ch for ch in channels]

    def set_scaler_output_modes(self, **kwargs):
        self.scaler_calls.append(kwargs)

    def start_data_capture(self, **kwargs):
        self.start_data_capture_calls.append(kwargs)
        self.capture_source = kwargs["source"]

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

    assert board.scaler_calls == [{
        "cap_data_type": "fft_full",
        "channels": [0, 1],
    }]
    assert board.start_data_capture_calls[0]["frames_per_burst"] == 4
    assert board.start_data_capture_calls[0]["source"] == "scaler"
    assert board.set_data_capture_calls == [([0, 1], "scaler", 12)]


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


def test_raw_acq_receiver_flush_only_clears_packet_buffer():
    receiver = raw_acq_receiver_mod.RawAcqReceiver()
    receiver.n_stored = 2
    receiver.pkt_len = np.array([64, 64, 0], dtype=np.uint32)
    receiver.processors = {"raw": _ProcessorThatMustNotBeFlushed()}

    assert receiver.flush() == 0
    assert receiver.n_stored == 0
    np.testing.assert_array_equal(receiver.pkt_len, np.zeros(3, dtype=np.uint32))


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
