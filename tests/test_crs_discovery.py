import sys
import types

import pytest

matplotlib_stub = types.ModuleType("matplotlib")
matplotlib_pyplot_stub = types.ModuleType("matplotlib.pyplot")
matplotlib_stub.pyplot = matplotlib_pyplot_stub
sys.modules.setdefault("matplotlib", matplotlib_stub)
sys.modules.setdefault("matplotlib.pyplot", matplotlib_pyplot_stub)

from pychfpga.fpga_array import FPGAArray
from pychfpga.hardware import HardwareMap, Motherboard, Crate
from pychfpga.hardware.crs import CRS, CRSBackplane
from pychfpga.hardware.crs.crs_discovery import CRSArmFirmwareUploader, CRSBeacon, CRSDiscovery


@pytest.fixture(autouse=True)
def clear_hardware_map():
    HardwareMap.clear_hardware_map()
    yield
    HardwareMap.clear_hardware_map()


def test_parse_tcpipe_beacon_with_crs_metadata():
    beacon = CRSBeacon(
        b"TCPIPE 1.0 CRS 003 "
        b"backplane-part=CRS backplane-serial=BP001 "
        b"backplane-slot=4 crate-number=2")

    assert beacon.kind == "TCPIPE"
    assert beacon.version == "1.0"
    assert beacon.board_part == "CRS"
    assert beacon.board_serial == "003"
    assert beacon.crate_part == "CRS"
    assert beacon.crate_serial == "BP001"
    assert beacon.slot == 4
    assert beacon.crate_number == 2


def test_crs_discovery_updates_board_crate_and_slot_from_beacon():
    discovery = CRSDiscovery(
        board_targets=[("CRS", "003")],
        crate_targets=[],
        timeout=0)
    beacon = CRSBeacon(
        b"TCPIPE 1.0 CRS 003 "
        b"backplane-part=CRS backplane-serial=BP001 "
        b"backplane-slot=4 crate-number=2")
    beacon.address = "192.0.2.10"

    mb = discovery._update_hardware_map(beacon, tcpipe_version=beacon.version)

    assert isinstance(mb, CRS)
    assert mb.hostname == "192.0.2.10"
    assert mb.serial == "003"
    assert mb.tcpipe_version == "1.0"
    assert mb.slot == 4
    assert isinstance(mb.crate, CRSBackplane)
    assert mb.crate.serial == "BP001"
    assert mb.crate.crate_number == 2
    assert mb.crate.slot[4] is mb


def test_crs_discovery_completion_accepts_integer_equivalent_serials():
    discovery = CRSDiscovery(
        board_targets=[("CRS", "3")],
        crate_targets=[],
        timeout=0)

    discovery.discovered_serials.add("0003")

    assert discovery._complete()


def test_explicit_targets_wait_without_deadline_even_when_timeout_is_set():
    discovery = CRSDiscovery(
        board_targets=[("CRS", "0037")],
        crate_targets=[],
        timeout=0)

    assert discovery._initial_deadline() is None


def test_crs_discovery_reports_missing_explicit_targets():
    discovery = CRSDiscovery(
        board_targets=[("CRS", "0037")],
        crate_targets=[],
        timeout=0)
    discovery.seen_serials.add("0037")

    with pytest.raises(RuntimeError, match="discovery is incomplete"):
        discovery._raise_if_incomplete()


def test_wildcard_targets_keep_timeout_deadline():
    discovery = CRSDiscovery(
        board_targets=[("CRS", "*")],
        crate_targets=[],
        timeout=0)

    assert discovery._initial_deadline() is not None


def test_fwreq_matches_crs_target_by_serial_when_identifier_is_not_model():
    discovery = CRSDiscovery(
        board_targets=[("CRS", "0037")],
        crate_targets=[],
        timeout=0)
    beacon = CRSBeacon(b"FWREQ 1.0 unknown-t0-crs-rev? 0037")

    assert discovery._matches_requested_targets(beacon)


def test_fwreq_sends_firmware_without_adding_board_to_hardware_map(monkeypatch):
    class FakeSock:
        def __init__(self):
            self.sent = []

        def sendto(self, data, addr):
            self.sent.append((data, addr))

    discovery = CRSDiscovery(
        board_targets=[("CRS", "003")],
        crate_targets=[],
        timeout=0,
        platform_firmware_pathname=CRS.DEFAULT_ARM_FIRMWARE_PATHNAME,
    )
    monkeypatch.setattr(
        discovery,
        "_send_platform_firmware",
        lambda sock, beacon, addr: sock.sendto(b"FWRESP 1.0\n\nboot", addr),
    )
    beacon = CRSBeacon(b"FWREQ 1.0 CRS 003", address="127.0.0.1")
    sock = FakeSock()

    handled_kind = discovery._process_beacon(sock, beacon, ("127.0.0.1", 9875))

    assert handled_kind == "FWREQ"
    assert Motherboard.get_all_registered_instances() == []
    assert discovery.discovered_serials == set()
    assert sock.sent == [(b"FWRESP 1.0\n\nboot", ("127.0.0.1", 9875))]


def test_tcpipe_adds_matching_board_to_hardware_map():
    discovery = CRSDiscovery(
        board_targets=[("CRS", "003")],
        crate_targets=[],
        timeout=0,
        required_tcpipe_version="1.0",
    )
    beacon = CRSBeacon(b"TCPIPE 1.0 CRS 003", address="127.0.0.1")

    handled_kind = discovery._process_beacon(None, beacon, ("127.0.0.1", 9875))

    [mb] = Motherboard.get_all_registered_instances()
    assert handled_kind == "TCPIPE"
    assert mb.hostname == "127.0.0.1"
    assert mb.tcpipe_version == "1.0"
    assert discovery.discovered_serials == {"003"}


def test_arm_firmware_runner_sends_fwresp_for_uboot_board(monkeypatch):
    class FakeSock:
        def __init__(self):
            self.sent = []

        def sendto(self, data, addr):
            self.sent.append((data, addr))

    board = CRS(serial="0037", hostname="192.0.2.10")
    board.tcpipe_version = None
    runner = CRSArmFirmwareUploader(
        boards=[board],
        required_version=CRS.REQUIRED_TCPIPE_VERSION,
        firmware_pathname=CRS.DEFAULT_ARM_FIRMWARE_PATHNAME,
    )
    runner.target_serials = {"0037"}
    runner._firmware_session = type("S", (), {"command_for": lambda _self, _beacon: "FWRESP 1.0\n\nboot"})()
    beacon = CRSBeacon(b"FWREQ 1.0 unknown-t0-crs-rev? 0037")
    beacon.address = "127.0.0.1"
    sock = FakeSock()

    runner._process_beacon(sock, beacon, ("127.0.0.1", 9875))

    assert sock.sent == [(b"FWRESP 1.0\n\nboot", ("127.0.0.1", 9875))]
    assert runner.ready_serials == set()


def test_arm_firmware_uploader_skips_matching_tcpipe_version():
    board = CRS(serial="0037", hostname="192.0.2.10")
    board.tcpipe_version = CRS.REQUIRED_TCPIPE_VERSION
    runner = CRSArmFirmwareUploader(
        boards=[board],
        required_version=CRS.REQUIRED_TCPIPE_VERSION,
        firmware_pathname=CRS.DEFAULT_ARM_FIRMWARE_PATHNAME,
    )
    runner.target_serials = {
        str(board.serial) for board in runner.boards
        if board.tcpipe_version != runner.required_tcpipe_version
    }

    assert runner.target_serials == set()


def test_arm_firmware_uploader_rejects_bad_tcpipe_version():
    board = CRS(serial="0037", hostname="192.0.2.10")
    board.tcpipe_version = "0.9"
    runner = CRSArmFirmwareUploader(
        boards=[board],
        required_version=CRS.REQUIRED_TCPIPE_VERSION,
        firmware_pathname=CRS.DEFAULT_ARM_FIRMWARE_PATHNAME,
    )
    runner.target_serials = {"0037"}
    beacon = CRSBeacon(b"TCPIPE 0.9 CRS 0037", address="192.0.2.11")

    with pytest.raises(RuntimeError, match="expected 1.0"):
        runner._process_beacon(None, beacon, ("192.0.2.11", 9875))


def test_arm_firmware_uploader_marks_board_ready_from_tcpipe():
    board = CRS(serial="0037", hostname="192.0.2.10")
    board.tcpipe_version = None
    runner = CRSArmFirmwareUploader(
        boards=[board],
        required_version=CRS.REQUIRED_TCPIPE_VERSION,
        firmware_pathname=CRS.DEFAULT_ARM_FIRMWARE_PATHNAME,
    )
    runner.target_serials = {"0037"}
    beacon = CRSBeacon(b"TCPIPE 1.0 CRS 0037", address="192.0.2.11")

    runner._process_beacon(None, beacon, ("192.0.2.11", 9875))

    assert board.hostname == "192.0.2.11"
    assert board.tcpipe_version == "1.0"
    assert runner.ready_serials == {"0037"}


def test_fpga_array_routes_crs_crates_to_crs_network_discovery():
    crate = CRSBackplane(serial="BP001")

    assert FPGAArray._network_discovery_class_for_crate(crate) is CRS


def test_discovery_rejects_unexpected_tcpipe_version():
    discovery = CRSDiscovery(
        board_targets=[("CRS", "003")],
        crate_targets=[],
        timeout=0,
        required_tcpipe_version="1.0",
    )
    beacon = CRSBeacon(b"TCPIPE 0.9 CRS 003", address="192.0.2.10")

    with pytest.raises(RuntimeError, match="expected 1.0"):
        discovery._process_beacon(None, beacon, ("192.0.2.10", 9875))
