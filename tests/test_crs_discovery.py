import asyncio
import sys
import types
import socket

import numpy as np
import pytest

matplotlib_stub = types.ModuleType("matplotlib")
matplotlib_pyplot_stub = types.ModuleType("matplotlib.pyplot")
matplotlib_stub.pyplot = matplotlib_pyplot_stub
sys.modules.setdefault("matplotlib", matplotlib_stub)
sys.modules.setdefault("matplotlib.pyplot", matplotlib_pyplot_stub)

from pychfpga.hardware import Crate, HardwareMap, Motherboard
from pychfpga.hardware.crs import CRS, CRSBackplane
from pychfpga.hardware.crs.crs_discovery import (
    CRSArmFirmwareUploader,
    CRSBeacon,
    CRSDiscovery,
    DEFAULT_DISCOVERY_PORT,
    NetConsole,
    UPLOAD_METHODS,
    _file_md5,
    _format_discovered_board_line,
    tcpipe_version_from_elf,
)
from pychfpga.hardware.crs.netconsole_loads import Loads
from pychfpga.hardware.crs.netconsole_xmodem import (
    XMODEM,
    xmodem1k_pipe_send,
    xmodem1k_send,
)


FWREQ_BEACON = (
    b"FWREQ/1\n"
    b"Manufacturer: t0\n"
    b"Product: crs\n"
    b"Revision: 4\n"
    b"Serial: 0037\n"
    b"Bootloader: u-boot\n"
    b"Bootloader Version: 0.1\n"
    b"\n"
)

TCPIPE_BEACON = (
    b"TCPIPE/1\n"
    b"Manufacturer: t0\n"
    b"Product: crs\n"
    b"Revision: 4\n"
    b"Serial: 0037\n"
    b"Bootloader: tcpipe\n"
    b"Bootloader Version: 1.0\n"
    b"Backplane-Part: CRS\n"
    b"Backplane-Serial: BP001\n"
    b"Backplane-Slot: 2\n"
    b"Crate-Model: CCR\n"
    b"Crate-Serial: CR001\n"
    b"Crate-Number: 2\n"
    b"\n"
)


@pytest.fixture(autouse=True)
def clear_hardware_map():
    HardwareMap.clear_hardware_map()
    yield
    HardwareMap.clear_hardware_map()


def test_parse_fwreq_beacon_with_headers():
    beacon = CRSBeacon(FWREQ_BEACON)

    assert beacon.kind == "FWREQ"
    assert beacon.version == "1"
    assert beacon.board_part == "CRS"
    assert beacon.board_serial == "0037"
    assert beacon.identifier == "t0-crs-rev4"
    assert beacon.fields["bootloader"] == "u-boot"
    assert beacon.fields["bootloader-version"] == "0.1"


def test_legacy_fwreq_format_is_rejected():
    with pytest.raises(ValueError, match="legacy FWREQ"):
        CRSBeacon(b"FWREQ 1.0 unknown-t0-crs-rev? 0037")


TCPIPE_V12_BEACON = (
    b"TCPIPE/1\n"
    b"Manufacturer: t0.technology\n"
    b"Product: crs\n"
    b"Revision: 5\n"
    b"Serial: 0216\n"
    b"Bootloader: tcpipe\n"
    b"Bootloader Version: 1.0\n"
    b"Crate Manufacturer: t0.technology\n"
    b"Crate Model: CCR\n"
    b"Crate Serial: 011\n"
    b"Crate Revision: 1\n"
    b"Crate Slot: 15\n"
    b"\n"
)


def test_parse_tcpipe_beacon():
    beacon = CRSBeacon(TCPIPE_BEACON)

    assert beacon.kind == "TCPIPE"
    assert beacon.version == "1.0"
    assert beacon.identifier == "t0-crs-rev4"
    assert beacon.board_part == "CRS"
    assert beacon.board_serial == "0037"
    assert beacon.fields["bootloader"] == "tcpipe"
    assert beacon.fields["bootloader-version"] == "1.0"
    assert beacon.crate_part == "CCR"
    assert beacon.backplane_part == "CRS"
    assert beacon.backplane_serial == "BP001"
    assert beacon.backplane_slot == 2
    assert beacon.crate_serial == "CR001"
    assert beacon.slot == 2
    assert beacon.crate_slot is None
    assert beacon.crate_number == 2


def test_parse_tcpipe_v12_beacon():
    beacon = CRSBeacon(TCPIPE_V12_BEACON)

    assert beacon.kind == "TCPIPE"
    assert beacon.version == "1.0"
    assert beacon.board_part == "CRS"
    assert beacon.board_serial == "0216"
    assert beacon.backplane_part is None
    assert beacon.backplane_serial is None
    assert beacon.backplane_slot is None
    assert beacon.crate_manufacturer == "t0.technology"
    assert beacon.crate_part == "CCR"
    assert beacon.crate_serial == "011"
    assert beacon.crate_revision == "1"
    assert beacon.crate_slot == 15
    assert beacon.slot == 15


def test_crs_discovery_sets_slot_from_crate_slot():
    discovery = CRSDiscovery(
        board_targets=[("CRS", "0216")],
        crate_targets=[],
        timeout=0)
    beacon = CRSBeacon(TCPIPE_V12_BEACON)
    beacon.address = "192.0.2.16"

    mb = discovery._update_hardware_map(beacon, tcpipe_version=beacon.version)

    assert mb.slot == 15
    assert isinstance(mb.crate, CRSBackplane)
    assert mb.crate.serial == "011"
    assert mb.crate.slot[15] is mb
    assert beacon.crate_slot == 15
    assert beacon.backplane_slot is None


def test_format_discovered_board_line_includes_backplane_and_crate():
    beacon = CRSBeacon(
        b"FWREQ/1\n"
        b"Manufacturer: t0\n"
        b"Product: crs\n"
        b"Revision: 5\n"
        b"Serial: 0139\n"
        b"Bootloader: u-boot\n"
        b"Bootloader Version: 0.1\n"
        b"Backplane Serial: 083\n"
        b"Crate Serial: 011\n"
        b"Backplane Slot: 1\n\n",
        address="10.10.10.182",
    )
    mb = CRS.get_unique_instance(serial="0139", slot=5)

    line = _format_discovered_board_line(beacon, mb=mb, added_to_hwm=True)

    assert line == (
        "CRS SN0139 @ 10.10.10.182 on Backplane CBP SN0083 "
        "/ Crate CCR SN0011 Slot 5 (FWREQ 1 beacon) -> added to hardware map"
    )


def test_crs_clock_source_from_pll_input_reference():
    mb = CRS.__new__(CRS)
    mb.input_reference = CRS.PLL_INPUT_BP_CLK_10MHZ
    mb.pll = None
    assert mb._get_iceboard_clock_source() == "CLOCK_SOURCE_BP"


def test_legacy_tcpipe_beacon_is_rejected():
    with pytest.raises(ValueError, match="CRS discovery beacon"):
        CRSBeacon(b"TCPIPE 1.0 CRS 003")


UNINITIALIZED_BACKPLANE_BEACON = (
    b"FWREQ/1\n"
    b"Manufacturer: t0\n"
    b"Product: crs\n"
    b"Revision: 4\n"
    b"Serial: 0039\n"
    b"Bootloader: u-boot\n"
    b"Bootloader Version: 0.1\n"
    b"Backplane-Part:\n"
    b"Backplane-Serial: unknown\n"
    b"Backplane-Slot: 0\n"
    b"Crate-Number: 0\n"
    b"\n"
)


def test_uninitialized_backplane_fields_are_ignored():
    beacon = CRSBeacon(UNINITIALIZED_BACKPLANE_BEACON)

    assert beacon.board_serial == "0039"
    assert beacon.crate_part is None
    assert beacon.crate_serial is None
    assert beacon.backplane_slot == 0
    assert beacon.slot is None
    assert beacon.crate_number == 0


TCPIPE_UNKNOWN_BACKPLANE_BEACON = (
    b"TCPIPE/1\n"
    b"Manufacturer: t0\n"
    b"Product: crs\n"
    b"Revision: 4\n"
    b"Serial: 0083\n"
    b"Bootloader: tcpipe\n"
    b"Bootloader Version: 1.0\n"
    b"Backplane Part: unknown\n"
    b"Backplane Serial: unknown\n"
    b"Backplane Slot: unknown\n"
    b"Crate Model: unknown\n"
    b"Crate Serial: unknown\n"
    b"\n"
)

TCPIPE_CBP_CCR_BEACON = (
    b"TCPIPE/1\n"
    b"Manufacturer: t0\n"
    b"Product: crs\n"
    b"Revision: 4\n"
    b"Serial: 0083\n"
    b"Bootloader: tcpipe\n"
    b"Bootloader Version: 1.0\n"
    b"Backplane Part: CBP\n"
    b"Backplane Serial: 083\n"
    b"Backplane Slot: 1\n"
    b"Crate Model: CCR\n"
    b"Crate Serial: 011\n"
    b"\n"
)


def test_tcpipe_unknown_backplane_fields_are_ignored():
    beacon = CRSBeacon(TCPIPE_UNKNOWN_BACKPLANE_BEACON)

    assert beacon.board_serial == "0083"
    assert beacon.backplane_part is None
    assert beacon.backplane_serial is None
    assert beacon.backplane_slot is None
    assert beacon.crate_part is None
    assert beacon.crate_serial is None
    assert beacon.crate_slot is None
    assert beacon.slot is None


def test_tcpipe_crate_model_alias():
    beacon = CRSBeacon(TCPIPE_CBP_CCR_BEACON)

    assert beacon.backplane_part == "CBP"
    assert beacon.backplane_serial == "083"
    assert beacon.backplane_slot == 1
    assert beacon.crate_part == "CCR"
    assert beacon.crate_serial == "011"
    assert beacon.crate_slot is None
    assert beacon.slot == 1


def test_crs_discovery_skips_crate_when_tcpipe_backplane_is_unknown():
    discovery = CRSDiscovery(
        board_targets=[("CRS", "0083")],
        crate_targets=[],
        timeout=0)
    beacon = CRSBeacon(TCPIPE_UNKNOWN_BACKPLANE_BEACON)
    beacon.address = "192.0.2.83"

    mb = discovery._update_hardware_map(beacon, tcpipe_version=beacon.version)

    assert isinstance(mb, CRS)
    assert mb.serial == "0083"
    assert mb.crate is None
    assert mb.slot is None
    assert Crate.get_all_registered_instances() == []


def test_crs_discovery_sets_slot_from_backplane_slot():
    discovery = CRSDiscovery(
        board_targets=[("CRS", "0083")],
        crate_targets=[],
        timeout=0)
    beacon = CRSBeacon(TCPIPE_CBP_CCR_BEACON)
    beacon.address = "192.0.2.83"

    mb = discovery._update_hardware_map(beacon, tcpipe_version=beacon.version)

    assert mb.slot == 1
    assert isinstance(mb.crate, CRSBackplane)
    assert mb.crate.serial == "011"
    assert mb.crate.slot[1] is mb
    assert beacon.backplane_slot == 1
    assert beacon.crate_slot is None


def test_crs_discovery_updates_board_crate_and_slot_from_beacon():
    discovery = CRSDiscovery(
        board_targets=[("CRS", "0037")],
        crate_targets=[],
        timeout=0)
    beacon = CRSBeacon(TCPIPE_BEACON)
    beacon.address = "192.0.2.10"

    mb = discovery._update_hardware_map(beacon, tcpipe_version=beacon.version)

    assert isinstance(mb, CRS)
    assert mb.hostname == "192.0.2.10"
    assert mb.serial == "0037"
    assert mb.tcpipe_version == "1.0"
    assert mb.slot == 2
    assert isinstance(mb.crate, CRSBackplane)
    assert mb.crate.serial == "CR001"
    assert mb.crate.crate_number == 2
    assert mb.crate.slot[2] is mb


def test_crs_discovery_uses_backplane_slot_as_crate_slot():
    discovery = CRSDiscovery(
        board_targets=[("CRS", "0139")],
        crate_targets=[],
        timeout=0)
    beacon = CRSBeacon(
        b"FWREQ/1\n"
        b"Manufacturer: t0\n"
        b"Product: crs\n"
        b"Revision: 5\n"
        b"Serial: 0139\n"
        b"Bootloader: u-boot\n"
        b"Bootloader Version: 0.1\n"
        b"Backplane Serial: 083\n"
        b"Crate Serial: 011\n"
        b"Backplane Slot: 1\n\n")
    beacon.address = "10.10.10.182"

    mb = discovery._update_hardware_map(beacon, tcpipe_version=None)

    assert mb.serial == "0139"
    assert beacon.crate_slot is None
    assert beacon.slot == 1
    assert mb.slot == 1
    assert mb.crate.serial == "011"


def test_crs_discovery_preserves_hardware_map_slot():
    mb = CRS.get_unique_instance(serial="0139", slot=5)
    discovery = CRSDiscovery(
        board_targets=[("CRS", "0139")],
        crate_targets=[],
        timeout=0)
    beacon = CRSBeacon(
        b"FWREQ/1\n"
        b"Manufacturer: t0\n"
        b"Product: crs\n"
        b"Revision: 5\n"
        b"Serial: 0139\n"
        b"Bootloader: u-boot\n"
        b"Bootloader Version: 0.1\n"
        b"Backplane Serial: 083\n"
        b"Crate Serial: 011\n"
        b"Backplane Slot: 1\n\n")
    beacon.address = "10.10.10.182"

    updated = discovery._update_hardware_map(beacon, tcpipe_version=None)

    assert updated is mb
    assert mb.slot == 5
    assert mb.hostname == "10.10.10.182"


def test_crs_discovery_skips_crate_when_backplane_eeprom_is_uninitialized():
    discovery = CRSDiscovery(
        board_targets=[("CRS", "0039")],
        crate_targets=[],
        timeout=0)
    beacon = CRSBeacon(UNINITIALIZED_BACKPLANE_BEACON)
    beacon.address = "192.0.2.39"

    mb = discovery._update_hardware_map(beacon, tcpipe_version=None)

    assert isinstance(mb, CRS)
    assert mb.serial == "0039"
    assert mb.hostname == "192.0.2.39"
    assert mb.crate is None
    assert mb.slot is None
    assert Crate.get_all_registered_instances() == []


def test_crs_discovery_skips_crate_with_serial_but_no_part():
    discovery = CRSDiscovery(
        board_targets=[("CRS", "0039")],
        crate_targets=[],
        timeout=0)
    beacon = CRSBeacon(
        b"FWREQ/1\n"
        b"Manufacturer: t0\n"
        b"Product: crs\n"
        b"Revision: 4\n"
        b"Serial: 0039\n"
        b"Bootloader: u-boot\n"
        b"Bootloader Version: 0.1\n"
        b"Backplane-Serial: BP001\n"
        b"Crate-Number: 2\n"
        b"\n")
    beacon.address = "192.0.2.39"

    mb = discovery._update_hardware_map(beacon, tcpipe_version=None)

    assert isinstance(mb, CRS)
    assert mb.crate is None
    assert Crate.get_all_registered_instances() == []


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


def test_crate_only_discovery_uses_timeout_and_does_not_stop_after_first_beacon():
    discovery = CRSDiscovery(
        board_targets=[],
        crate_targets=[("CRC4", "1")],
        timeout=5.0,
    )
    assert discovery._initial_deadline() is not None
    discovery._record_discovered_crate(
        CRSBeacon(
            b"FWREQ/1\nProduct: crs\nSerial: 0048\n"
            b"Crate Model: CRC4\nCrate Serial: 001\n\n",
        ),
    )
    assert not discovery._complete()


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


def test_fwreq_matches_crs_target_by_serial():
    discovery = CRSDiscovery(
        board_targets=[("CRS", "0037")],
        crate_targets=[],
        timeout=0)
    beacon = CRSBeacon(FWREQ_BEACON)

    assert discovery._matches_requested_targets(beacon)


def test_tcpipe_adds_matching_board_to_hardware_map():
    discovery = CRSDiscovery(
        board_targets=[("CRS", "0037")],
        crate_targets=[],
        timeout=0,
    )
    beacon = CRSBeacon(TCPIPE_BEACON, address="127.0.0.1")

    handled_kind = discovery._process_beacon(None, beacon, ("127.0.0.1", 9875))

    [mb] = Motherboard.get_all_registered_instances()
    assert handled_kind == "TCPIPE"
    assert mb.hostname == "127.0.0.1"
    assert mb.tcpipe_version == "1.0"
    assert discovery.discovered_serials == {"0037"}


def test_arm_firmware_runner_uploads_elf_for_uboot_board(monkeypatch):
    upload_calls = []

    async def fake_collect(self, sock, targets, timeout=60):
        return {board.hostname: 10132 for board in targets}

    async def fake_upload(self, board, firmware, http_site, fwreq_port):
        upload_calls.append({
            "hostname": board.hostname,
            "fwreq_port": fwreq_port,
            "firmware": str(firmware),
            "upload_method": self.upload_method,
            "http_port": http_site.port if http_site else None,
        })
        return True

    monkeypatch.setattr(CRSArmFirmwareUploader, "_collect_fwreq_ports", fake_collect)
    monkeypatch.setattr(CRSArmFirmwareUploader, "_upload_board", fake_upload)

    board = CRS(serial="0037", hostname="192.0.2.10")
    board.tcpipe_version = None
    runner = CRSArmFirmwareUploader(
        boards=[board],
        required_version=CRS.REQUIRED_TCPIPE_VERSION,
        firmware_pathname=CRS.DEFAULT_ARM_FIRMWARE_PATHNAME,
        upload_method="xmodem",
    )

    asyncio.run(runner.run())

    assert upload_calls == [{
        "hostname": "192.0.2.10",
        "fwreq_port": 10132,
        "firmware": CRS.DEFAULT_ARM_FIRMWARE_PATHNAME,
        "upload_method": "xmodem",
        "http_port": None,
    }]


def test_arm_firmware_uploader_skips_matching_tcpipe_version():
    board = CRS(serial="0037", hostname="192.0.2.10")
    board.tcpipe_version = CRS.REQUIRED_TCPIPE_VERSION
    runner = CRSArmFirmwareUploader(
        boards=[board],
        required_version=CRS.REQUIRED_TCPIPE_VERSION,
        firmware_pathname=CRS.DEFAULT_ARM_FIRMWARE_PATHNAME,
    )

    assert runner._boards_to_upload() == []


def test_arm_firmware_uploader_raises_on_wrong_tcpipe_version(tmp_path):
    elf = tmp_path / "tcpipe_rtos.elf"
    elf.write_bytes(b"payload")
    board = CRS(serial="0037", hostname="192.0.2.10")
    board.tcpipe_version = "0.9"
    runner = CRSArmFirmwareUploader(
        boards=[board],
        required_version="v1.2-3-gabcd12",
        firmware_pathname=str(elf),
    )

    with pytest.raises(RuntimeError, match=(
            r"measured '0\.9', expected 'v1\.2-3-gabcd12'"
            r".*Reboot the boards")):
        asyncio.run(runner.run())


def test_set_platform_firmware_uses_elf_git_version(monkeypatch, tmp_path):
    elf = tmp_path / "tcpipe_rtos.elf"
    elf.write_bytes(b"padding\0TCPIPE_GIT=v1.2-3-gabcd12-dirty\0")
    captured = {}

    async def fake_run(self):
        captured["required_version"] = self.required_tcpipe_version
        captured["firmware"] = str(self.firmware_pathname)

    monkeypatch.setattr(CRSArmFirmwareUploader, "run", fake_run)
    board = CRS(serial="0037", hostname="192.0.2.10")
    board.tcpipe_version = "1.0"
    asyncio.run(CRS.set_platform_firmware(boards=[board], firmware_pathname=str(elf)))
    assert captured["required_version"] == "v1.2-3-gabcd12-dirty"
    assert captured["firmware"] == str(elf)


def test_arm_firmware_uploader_skips_boards_without_hostname():
    board = CRS(serial="0037", hostname=None)
    board.tcpipe_version = None
    runner = CRSArmFirmwareUploader(
        boards=[board],
        required_version=CRS.REQUIRED_TCPIPE_VERSION,
        firmware_pathname=CRS.DEFAULT_ARM_FIRMWARE_PATHNAME,
    )

    assert runner._boards_to_upload() == []


def test_arm_firmware_uploader_marks_board_ready_after_upload(monkeypatch):
    async def fake_collect(self, sock, targets, timeout=60):
        return {board.hostname: 10132 for board in targets}

    async def fake_upload_and_run(
            self, elf_path, upload_method, wget_url=None, verify_md5=False):
        return True

    async def fake_wait_beacons(self, sock, boards, timeout=60):
        return {board.hostname: 0.5 for board in boards}

    monkeypatch.setattr(CRSArmFirmwareUploader, "_collect_fwreq_ports", fake_collect)
    monkeypatch.setattr(CRSArmFirmwareUploader, "_wait_for_tcpipe_beacons", fake_wait_beacons)
    monkeypatch.setattr(NetConsole, "upload_and_run", fake_upload_and_run)
    monkeypatch.setattr(NetConsole, "open", lambda self: None)
    monkeypatch.setattr(NetConsole, "close", lambda self: None)

    board = CRS(serial="0037", hostname="192.0.2.11")
    board.tcpipe_version = None
    runner = CRSArmFirmwareUploader(
        boards=[board],
        required_version=CRS.REQUIRED_TCPIPE_VERSION,
        firmware_pathname=CRS.DEFAULT_ARM_FIRMWARE_PATHNAME,
        upload_method="xmodem",
    )

    asyncio.run(runner.run())

    assert board.tcpipe_version == CRS.REQUIRED_TCPIPE_VERSION


def test_arm_firmware_uploader_does_not_mark_ready_without_tcpipe_beacon(monkeypatch):
    async def fake_collect(self, sock, targets, timeout=60):
        return {board.hostname: 10132 for board in targets}

    async def fake_upload_and_run(
            self, elf_path, upload_method, wget_url=None, verify_md5=False):
        return True

    async def fake_wait_beacons(self, sock, boards, timeout=60):
        return {}

    monkeypatch.setattr(CRSArmFirmwareUploader, "_collect_fwreq_ports", fake_collect)
    monkeypatch.setattr(CRSArmFirmwareUploader, "_wait_for_tcpipe_beacons", fake_wait_beacons)
    monkeypatch.setattr(NetConsole, "upload_and_run", fake_upload_and_run)
    monkeypatch.setattr(NetConsole, "open", lambda self: None)
    monkeypatch.setattr(NetConsole, "close", lambda self: None)

    board = CRS(serial="0037", hostname="192.0.2.11")
    board.tcpipe_version = None
    runner = CRSArmFirmwareUploader(
        boards=[board],
        required_version=CRS.REQUIRED_TCPIPE_VERSION,
        firmware_pathname=CRS.DEFAULT_ARM_FIRMWARE_PATHNAME,
        upload_method="xmodem",
    )

    asyncio.run(runner.run())

    assert board.tcpipe_version is None


def test_has_uboot_prompt_rejects_md5sum_arrow():
    assert not NetConsole._has_uboot_prompt("md5 for 08000000 ... 081a8897 ==> ")
    assert NetConsole._has_uboot_prompt(
        "md5 for 08000000 ... 081a8897 ==> e55261bbcecddbf1b9dbc7fc601d4ed4\nZynqMP>")


def test_recv_until_prompt_waits_for_md5_digest():
    nc = NetConsole("192.0.2.1", 9875)
    nc._sock = types.SimpleNamespace(send=lambda data: None)
    _install_async_recv(nc, [
        b"md5 for 08000000 ... 081a8897 ==> ",
        b"e55261bbcecddbf1b9dbc7fc601d4ed4\n",
        b"ZynqMP> ",
    ])
    buf = asyncio.run(nc._recv_until_prompt(timeout=5.0, idle=0.05))
    assert "e55261bbcecddbf1b9dbc7fc601d4ed4" in buf


def test_netconsole_verify_loaded_md5_accepts_matching_digest():
    nc = NetConsole("192.0.2.1", 9875)
    nc._sock = types.SimpleNamespace(send=lambda data: None)

    async def _recv_md5(timeout):
        return (
            "md5 for 08000000 ... 081a8897 ==> abcd1234deadbeefcafebabe12345678\nZynqMP>"
        )

    nc._recv_md5_response = _recv_md5
    assert asyncio.run(nc.verify_loaded_md5("abcd1234deadbeefcafebabe12345678"))


def test_netconsole_verify_loaded_md5_rejects_mismatch():
    nc = NetConsole("192.0.2.1", 9875)
    nc._sock = types.SimpleNamespace(send=lambda data: None)

    async def _recv_md5(timeout):
        return (
            "md5 for 08000000 ... 081a8897 ==> deadbeefcafebabe12345678abcd1234\nZynqMP>"
        )

    nc._recv_md5_response = _recv_md5
    assert not asyncio.run(nc.verify_loaded_md5("abcd1234deadbeefcafebabe12345678"))


def test_netconsole_verify_loaded_md5_rejects_partial_md5_line():
    nc = NetConsole("192.0.2.1", 9875)
    nc._sock = types.SimpleNamespace(send=lambda data: None)

    async def _recv_md5(timeout):
        return "md5 for 08000000 ... 081a8897 ==>"

    nc._recv_md5_response = _recv_md5
    assert not asyncio.run(nc.verify_loaded_md5("abcd1234deadbeefcafebabe12345678"))


def _patch_async_console(nc):
    nc.send_fwresp = lambda: None
    nc.wait_for_prompt = (
        lambda timeout=5.0, send_enter=False, poll_interval=None:
        _async_const("ZynqMP>")
    )
    nc.drain = lambda timeout=0.5, idle=0.05: _async_const(None)


def test_read_line_returns_on_newline_and_keeps_remainder():
    nc = NetConsole("192.0.2.1", 9875)
    nc._sock = types.SimpleNamespace(send=lambda data: None)
    _install_async_recv(nc, [b"hello\nZynqMP> "])
    assert asyncio.run(nc.read_line(timeout=1.0)) == "hello\n"
    assert asyncio.run(nc.read_line(timeout=0.05)) == "ZynqMP> "


def test_upload_and_boot_elf_skips_bootelf_when_md5_fails(tmp_path, monkeypatch):
    elf_path = tmp_path / "tcpipe_rtos.elf"
    elf_path.write_bytes(b"payload")
    monkeypatch.setattr(
        "pychfpga.hardware.crs.crs_discovery._file_md5",
        lambda path: "abcd1234",
    )

    nc = NetConsole("192.0.2.1", 9875)
    _patch_async_console(nc)
    nc._sock = types.SimpleNamespace(send=lambda data: None)
    nc.xmodem_send = lambda filename: _async_const(True)
    nc.verify_loaded_md5 = lambda expected_md5, **kwargs: _async_const(False)
    boot_calls = []
    nc.send_cmd = lambda cmd, timeout=0.1: boot_calls.append(cmd) or _async_const("")

    assert not asyncio.run(nc.upload_and_boot_elf(elf_path, verify_md5=False))
    assert boot_calls == ["printenv loadaddr filesize"]


def test_upload_and_boot_elf_always_verifies_md5(tmp_path, monkeypatch):
    elf_path = tmp_path / "tcpipe_rtos.elf"
    elf_path.write_bytes(b"payload")
    monkeypatch.setattr(
        "pychfpga.hardware.crs.crs_discovery._file_md5",
        lambda path: "abcd1234",
    )
    md5_calls = []

    nc = NetConsole("192.0.2.1", 9875)
    _patch_async_console(nc)
    nc._sock = types.SimpleNamespace(send=lambda data: None)
    nc.xmodem_send = lambda filename: _async_const(True)

    async def _verify(expected_md5, **kwargs):
        md5_calls.append(expected_md5)
        return True

    nc.verify_loaded_md5 = _verify
    nc.send_cmd = lambda cmd, timeout=0.1: _async_const("")

    assert asyncio.run(nc.upload_and_boot_elf(elf_path, verify_md5=False))
    assert md5_calls == ["abcd1234"]


def test_upload_and_boot_elf_runs_bootelf_after_md5_ok(tmp_path, monkeypatch):
    elf_path = tmp_path / "tcpipe_rtos.elf"
    elf_path.write_bytes(b"payload")
    monkeypatch.setattr(
        "pychfpga.hardware.crs.crs_discovery._file_md5",
        lambda path: "abcd1234",
    )

    nc = NetConsole("192.0.2.1", 9875)
    _patch_async_console(nc)
    nc._sock = types.SimpleNamespace(send=lambda data: None)
    nc.xmodem_send = lambda filename: _async_const(True)
    nc.verify_loaded_md5 = (
        lambda expected_md5, **kwargs: _async_const(expected_md5 == "abcd1234")
    )
    boot_calls = []
    nc.send_cmd = lambda cmd, timeout=0.1: boot_calls.append(cmd) or _async_const("")

    assert asyncio.run(nc.upload_and_boot_elf(elf_path, verify_md5=True))
    assert boot_calls == [
        "printenv loadaddr filesize",
        NetConsole.BOOTELF_CMD,
    ]


def test_upload_and_boot_elf_uses_xmodem_pipe(tmp_path, monkeypatch):
    elf_path = tmp_path / "tcpipe_rtos.elf"
    elf_path.write_bytes(b"payload")
    monkeypatch.setattr(
        "pychfpga.hardware.crs.crs_discovery._file_md5",
        lambda path: "abcd1234",
    )
    pipe_calls = []

    nc = NetConsole("192.0.2.1", 9875)
    _patch_async_console(nc)
    nc._sock = types.SimpleNamespace(send=lambda data: None)
    nc.xmodem_pipe_send = (
        lambda filename, pipe_depth=8: pipe_calls.append(filename) or _async_const(True)
    )
    nc.verify_loaded_md5 = lambda expected_md5, **kwargs: _async_const(True)
    nc.send_cmd = lambda cmd, timeout=0.1: _async_const("")

    assert asyncio.run(nc.upload_and_boot_elf(elf_path, upload_method="xmodem_pipe"))
    assert pipe_calls == [elf_path]


def test_upload_and_boot_elf_uses_loads(tmp_path, monkeypatch):
    elf_path = tmp_path / "tcpipe_rtos.elf"
    elf_path.write_bytes(b"payload")
    monkeypatch.setattr(
        "pychfpga.hardware.crs.crs_discovery._file_md5",
        lambda path: "abcd1234",
    )
    loads_calls = []

    nc = NetConsole("192.0.2.1", 9875)
    _patch_async_console(nc)
    nc.loads_send = (
        lambda filename, line_batch=16: loads_calls.append(filename) or _async_const(True)
    )
    nc.verify_loaded_md5 = lambda expected_md5, **kwargs: _async_const(True)
    nc.send_cmd = lambda cmd, timeout=0.1: _async_const("")

    assert asyncio.run(nc.upload_and_boot_elf(elf_path, upload_method="loads"))
    assert loads_calls == [elf_path]


def test_numpy_crc16_is_deterministic():
    data = np.frombuffer(b"hello world", dtype=np.uint8)
    assert XMODEM.crc16_ccitt(data) == XMODEM.crc16_ccitt(data)


def test_s3_line_from_segment():
    data = np.array([0x27, 0x05, 0x19, 0x56], dtype=np.uint8)
    line = Loads.s3_record_line(0x1000, data)
    assert line.startswith(b"S30900001000")
    assert line.endswith(b"\n")


def test_xmodem1k_block_framing():
    data = np.frombuffer(b"abc", dtype=np.uint8)
    block = XMODEM.block_1k(5, data)
    assert block[0] == 0x02
    assert block[1] == 5
    assert block[2] == 0xFA
    assert len(block) == 3 + 1024 + 2


def test_xmodem128_block_framing():
    data = np.frombuffer(b"abc", dtype=np.uint8)
    block = XMODEM.block(5, data, block_size=128)
    assert block[0] == 0x01
    assert block[1] == 5
    assert block[2] == 0xFA
    assert len(block) == 3 + 128 + 2


def test_xmodem_block_size_must_be_128_or_1024():
    data = np.frombuffer(b"abc", dtype=np.uint8)
    try:
        XMODEM.block(1, data, block_size=256)
    except ValueError as exc:
        assert "block_size" in str(exc)
    else:
        raise AssertionError("expected ValueError")


class _MockSock:
    def __init__(self, recv_chunks):
        self.recv_chunks = list(recv_chunks)
        self.sent = []
        self._timeout = None

    def settimeout(self, timeout):
        self._timeout = timeout

    def setblocking(self, flag):
        return None

    def recv(self, size):
        if not self.recv_chunks:
            raise socket.timeout
        chunk = self.recv_chunks.pop(0)
        if chunk is None:
            raise socket.timeout
        return chunk

    def send(self, data):
        self.sent.append(data)
        return len(data)

    def sendall(self, data):
        self.sent.append(data)


def _install_async_recv(nc, chunks):
    """Drive NetConsole async reads from a chunk list (``None`` = timeout)."""
    pending = list(chunks)

    async def _recv(timeout):
        if not pending:
            return None
        return pending.pop(0)

    nc._recv = _recv
    return pending


async def _async_const(value, *args, **kwargs):
    return value


def test_xmodem_send_sends_blocks_and_eot(tmp_path):
    elf_path = tmp_path / "fw.elf"
    elf_path.write_bytes(b"x" * 2048)
    sock = _MockSock([
        bytes([0x43]),  # CRC handshake
        bytes([0x06]),  # ACK block 1
        bytes([0x06]),  # ACK block 2
        bytes([0x06]),  # ACK EOT
    ])
    assert xmodem1k_send(sock, elf_path)
    assert len(sock.sent) == 3  # two 1K blocks + EOT


def test_xmodem_pipe_sends_blocks_and_eot(tmp_path):
    elf_path = tmp_path / "fw.elf"
    elf_path.write_bytes(b"x" * 2048)
    sock = _MockSock([
        bytes([0x43]),  # CRC handshake
        bytes([0x06]),  # ACK block 1
        bytes([0x06]),  # ACK block 2
        bytes([0x06]),  # ACK EOT
    ])
    assert xmodem1k_pipe_send(sock, elf_path, pipe_depth=4)
    assert len(sock.sent) == 3  # two 1K blocks + EOT
    payloads = b"".join(sock.sent)
    assert payloads[0:1] == bytes([0x02])
    assert sock.sent[-1] == bytes([0x04])


def test_xmodem_pipe_128_sends_soh_blocks(tmp_path):
    elf_path = tmp_path / "fw.elf"
    elf_path.write_bytes(b"x" * 256)
    sock = _MockSock([
        bytes([0x43]),  # CRC handshake
        bytes([0x06]),  # ACK block 1
        bytes([0x06]),  # ACK block 2
        bytes([0x06]),  # ACK EOT
    ])
    assert xmodem1k_pipe_send(sock, elf_path, pipe_depth=2, block_size=128)
    assert len(sock.sent) == 3  # two 128-byte blocks + EOT
    assert sock.sent[0][0] == 0x01
    assert len(sock.sent[0]) == 3 + 128 + 2
    assert sock.sent[-1] == bytes([0x04])


class _PipeResendSock:
    """Mock that ACKs the first frame, times out on the second until resent."""

    def __init__(self, retry_with_c=False):
        self.sent = []
        self._handshake_done = False
        self._acks = 0
        self._retry_with_c = retry_with_c
        self._sent_retry_signal = False

    def settimeout(self, timeout):
        return None

    def sendall(self, data):
        self.sent.append(data)

    def _data_frames(self):
        return [s for s in self.sent if len(s) > 1]

    def recv(self, size):
        if not self._handshake_done:
            self._handshake_done = True
            return bytes([0x43])
        if self.sent and self.sent[-1] == bytes([0x04]):
            return bytes([0x06])  # ACK EOT
        n_data = len(self._data_frames())
        if self._acks == 0 and n_data >= 2:
            self._acks = 1
            return bytes([0x06])
        if self._acks == 1 and n_data < 3:
            if self._retry_with_c and not self._sent_retry_signal:
                self._sent_retry_signal = True
                return bytes([0x43])
            raise socket.timeout
        if self._acks == 1 and n_data >= 3:
            self._acks = 2
            return bytes([0x06])
        raise socket.timeout


def test_xmodem_pipe_resends_after_missing_ack(tmp_path):
    """After ACK1, a timeout on ACK2 resends only the unacked frame."""
    elf_path = tmp_path / "fw.elf"
    elf_path.write_bytes(b"x" * 256)
    sock = _PipeResendSock(retry_with_c=False)
    assert xmodem1k_pipe_send(
        sock, elf_path, pipe_depth=2, block_size=128,
        ack_timeout=0.3, max_retries=3,
    )
    assert len(sock.sent) == 4  # B1, B2, resend B2, EOT
    assert sock.sent[0][1] == 1 and sock.sent[1][1] == 2 and sock.sent[2][1] == 2
    assert sock.sent[-1] == bytes([0x04])


def test_xmodem_pipe_resends_on_receiver_c(tmp_path):
    """U-Boot retry ``C`` after a lost frame triggers resend of the remainder."""
    elf_path = tmp_path / "fw.elf"
    elf_path.write_bytes(b"x" * 256)
    sock = _PipeResendSock(retry_with_c=True)
    assert xmodem1k_pipe_send(
        sock, elf_path, pipe_depth=2, block_size=128,
        ack_timeout=0.3, max_retries=3,
    )
    assert len(sock.sent) == 4
    assert sock.sent[2][1] == 2


def test_loads_send_streams_srec_lines(tmp_path):
    elf_path = tmp_path / "fw.elf"
    elf_path.write_bytes(b"payload")
    sock = _MockSock([
        b"## Ready for S-Record download ...\n",
        b"## First Load Addr = 0x08000000\n## Total Size = 0x7 = 7 Bytes\n"
        b"## Start Addr      = 0x00000000\nZynqMP> ",
    ])
    assert Loads(sock).send(elf_path, line_batch=1, drain_timeout=0, timeout=5)
    payloads = b"".join(sock.sent)
    assert payloads.startswith(b"loads $loadaddr\n")
    assert b"S3" in payloads
    assert b"S705" in payloads
    assert payloads.count(b"\n") >= 3  # at least one data line + S5 + S7


def test_upload_methods_include_pipe_and_loads():
    assert {"xmodem_pipe", "loads"}.issubset(UPLOAD_METHODS)


def test_arm_firmware_runner_xmodem_pipe_method(monkeypatch, tmp_path):
    upload_calls = []
    elf_path = tmp_path / "tcpipe_rtos.elf"
    elf_path.write_bytes(b"payload")

    async def fake_collect(self, sock, targets, timeout=60):
        return {board.hostname: 10132 for board in targets}

    async def fake_upload(self, board, firmware, http_site, fwreq_port):
        upload_calls.append(self.upload_method)
        return True

    monkeypatch.setattr(CRSArmFirmwareUploader, "_collect_fwreq_ports", fake_collect)
    monkeypatch.setattr(CRSArmFirmwareUploader, "_upload_board", fake_upload)

    board = CRS(serial="0037", hostname="192.0.2.10")
    board.tcpipe_version = None
    runner = CRSArmFirmwareUploader(
        boards=[board],
        required_version=CRS.REQUIRED_TCPIPE_VERSION,
        firmware_pathname=str(elf_path),
        upload_method="xmodem_pipe",
    )

    asyncio.run(runner.run())

    assert upload_calls == ["xmodem_pipe"]


def test_upload_and_boot_via_wget_waits_for_prompt_and_verifies_md5(tmp_path, monkeypatch):
    elf_path = tmp_path / "tcpipe_rtos.elf"
    elf_path.write_bytes(b"payload")
    expected_md5 = "abcd1234deadbeefcafebabe12345678"
    monkeypatch.setattr(
        "pychfpga.hardware.crs.crs_discovery._file_md5",
        lambda path: expected_md5,
    )
    sent = []
    prompt_calls = []

    async def exercise():
        nc = NetConsole("192.0.2.1", 9875)
        nc.send_fwresp = lambda: None

        async def _wait_for_prompt(timeout=5.0, send_enter=False, poll_interval=None):
            prompt_calls.append({"timeout": timeout, "send_enter": send_enter})
            return "ZynqMP>"

        nc.wait_for_prompt = _wait_for_prompt
        nc._sock = types.SimpleNamespace(send=lambda data: sent.append(data))
        nc._recv_after_bootelf = lambda timeout=15.0, idle=None: _async_const("")
        nc.verify_loaded_md5 = (
            lambda expected_md5, **kwargs: _async_const(
                expected_md5 == "abcd1234deadbeefcafebabe12345678"
            )
        )

        return await nc.upload_and_boot_via_wget(
            "http://10.0.0.1:19875/tcpipe_rtos.elf",
            elf_path,
        )

    assert asyncio.run(exercise())
    assert any(b"bootelf" in data for data in sent)
    assert prompt_calls
    assert all(not call["send_enter"] for call in prompt_calls)


def test_wait_for_prompt_detects_prompt_without_newline():
    nc = NetConsole("192.0.2.1", 9875)
    nc._sock = _MockSock([])
    _install_async_recv(nc, [b"ZynqMP> "])
    buf = asyncio.run(nc.wait_for_prompt(timeout=1.0, poll_interval=0.01))
    assert nc._has_uboot_prompt(buf)
    assert "ZynqMP>" in buf


def test_wait_for_prompt_send_enter_polls_until_prompt():
    nc = NetConsole("192.0.2.1", 9875)
    sock = _MockSock([])
    nc._sock = sock
    _install_async_recv(nc, [None, None, b"ZynqMP> "])
    buf = asyncio.run(
        nc.wait_for_prompt(timeout=5.0, send_enter=True, poll_interval=0.01)
    )
    assert nc._has_uboot_prompt(buf)
    assert b"\n" in sock.sent


def test_recv_md5_response_returns_digest():
    nc = NetConsole("192.0.2.1", 9875)
    nc._sock = types.SimpleNamespace(send=lambda data: None)
    _install_async_recv(nc, [
        b"md5 for 08000000 ... 08000006 ==> ",
        b"abcd1234deadbeefcafebabe12345678\n",
        b"ZynqMP> ",
    ])
    output = asyncio.run(nc._recv_md5_response(timeout=5.0))
    assert "abcd1234deadbeefcafebabe12345678" in output
    assert nc._parse_uboot_md5(output) == "abcd1234deadbeefcafebabe12345678"


def test_recv_after_bootelf_treats_silence_as_success():
    nc = NetConsole("192.0.2.1", 9875)
    nc._sock = types.SimpleNamespace(send=lambda data: None)
    _install_async_recv(nc, [None, None, None])
    assert asyncio.run(nc._recv_after_bootelf(timeout=0.2, idle=0.05)) == ""


def test_recv_after_bootelf_detects_boot_error():
    nc = NetConsole("192.0.2.1", 9875)
    nc._sock = types.SimpleNamespace(send=lambda data: None)
    _install_async_recv(nc, [b"Unknown command 'bootelf'\nZynqMP> "])
    output = asyncio.run(nc._recv_after_bootelf(timeout=1.0, idle=0.05))
    assert "Unknown command" in output
    assert not nc._bootelf_ok(output)


def test_upload_and_boot_via_wget_fails_when_prompt_missing(tmp_path, monkeypatch):
    elf_path = tmp_path / "tcpipe_rtos.elf"
    elf_path.write_bytes(b"payload")
    monkeypatch.setattr(
        "pychfpga.hardware.crs.crs_discovery._file_md5",
        lambda path: "abcd1234",
    )

    async def exercise():
        nc = NetConsole("192.0.2.1", 9875)
        nc.send_fwresp = lambda: None
        nc.wait_for_prompt = (
            lambda timeout=5.0, send_enter=False, poll_interval=None:
            _async_const("")
        )
        nc._sock = types.SimpleNamespace(send=lambda data: None)
        return await nc.upload_and_boot_via_wget(
            "http://10.0.0.1:19875/tcpipe_rtos.elf",
            elf_path,
        )

    assert not asyncio.run(exercise())


def test_arm_firmware_runner_wget_method_passes_url(monkeypatch, tmp_path):
    captured = {}
    elf_path = tmp_path / "tcpipe_rtos.elf"
    elf_path.write_bytes(b"payload")

    async def fake_collect(self, sock, targets, timeout=60):
        return {board.hostname: 10132 for board in targets}

    async def fake_upload_and_run(
            self, elf_path, upload_method, wget_url=None, verify_md5=False):
        captured["upload_method"] = upload_method
        captured["wget_url"] = wget_url
        return True

    async def fake_wait_beacons(self, sock, boards, timeout=60):
        return {board.hostname: 0.1 for board in boards}

    monkeypatch.setattr(CRSArmFirmwareUploader, "_collect_fwreq_ports", fake_collect)
    monkeypatch.setattr(CRSArmFirmwareUploader, "_wait_for_tcpipe_beacons", fake_wait_beacons)
    monkeypatch.setattr(NetConsole, "upload_and_run", fake_upload_and_run)
    monkeypatch.setattr(NetConsole, "open", lambda self: None)
    monkeypatch.setattr(NetConsole, "close", lambda self: None)
    monkeypatch.setattr(
        CRSArmFirmwareUploader,
        "_iface_address_for",
        lambda self, host: "10.0.0.1",
    )

    board = CRS(serial="0037", hostname="192.0.2.10")
    board.tcpipe_version = None
    runner = CRSArmFirmwareUploader(
        boards=[board],
        required_version=CRS.REQUIRED_TCPIPE_VERSION,
        firmware_pathname=str(elf_path),
        upload_method="wget",
    )

    asyncio.run(runner.run())

    assert captured["upload_method"] == "wget"
    assert captured["wget_url"] == "http://10.0.0.1:19875/tcpipe_rtos.elf"


def test_collect_fwreq_ports_maps_ephemeral_reply_port():
    fwreq = FWREQ_BEACON
    board = CRS(serial="0037", hostname="127.0.0.1")
    board.tcpipe_version = None
    runner = CRSArmFirmwareUploader(
        boards=[board],
        required_version=CRS.REQUIRED_TCPIPE_VERSION,
        firmware_pathname=CRS.DEFAULT_ARM_FIRMWARE_PATHNAME,
    )

    async def exercise():
        recv_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            CRSArmFirmwareUploader._bind_fwreq_socket(recv_sock)
            send_sock.bind(("127.0.0.1", 10132))

            async def send_fwreq():
                await asyncio.sleep(0.05)
                send_sock.sendto(fwreq, ("127.0.0.1", DEFAULT_DISCOVERY_PORT))

            sender = asyncio.create_task(send_fwreq())
            ports = await runner._collect_fwreq_ports(recv_sock, [board], timeout=2)
            await sender
            return ports
        finally:
            recv_sock.close()
            send_sock.close()

    ports = asyncio.run(exercise())
    assert ports == {"127.0.0.1": 10132}


def test_collect_fwreq_ports_matches_hostname_without_serial():
    fwreq = FWREQ_BEACON
    board = CRS(serial=None, hostname="127.0.0.1")
    board.tcpipe_version = None
    runner = CRSArmFirmwareUploader(
        boards=[board],
        required_version=CRS.REQUIRED_TCPIPE_VERSION,
        firmware_pathname=CRS.DEFAULT_ARM_FIRMWARE_PATHNAME,
    )

    async def exercise():
        recv_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            CRSArmFirmwareUploader._bind_fwreq_socket(recv_sock)
            send_sock.bind(("127.0.0.1", 10135))

            async def send_fwreq():
                await asyncio.sleep(0.05)
                send_sock.sendto(fwreq, ("127.0.0.1", DEFAULT_DISCOVERY_PORT))

            sender = asyncio.create_task(send_fwreq())
            ports = await runner._collect_fwreq_ports(recv_sock, [board], timeout=2)
            await sender
            return ports
        finally:
            recv_sock.close()
            send_sock.close()

    ports = asyncio.run(exercise())
    assert ports == {"127.0.0.1": 10135}


def test_collect_fwreq_ports_matches_serial_when_ip_differs():
    fwreq = FWREQ_BEACON
    board = CRS(serial="0037", hostname="10.0.0.99")
    board.tcpipe_version = None
    runner = CRSArmFirmwareUploader(
        boards=[board],
        required_version=CRS.REQUIRED_TCPIPE_VERSION,
        firmware_pathname=CRS.DEFAULT_ARM_FIRMWARE_PATHNAME,
    )

    async def exercise():
        recv_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            CRSArmFirmwareUploader._bind_fwreq_socket(recv_sock)
            send_sock.bind(("127.0.0.1", 10136))

            async def send_fwreq():
                await asyncio.sleep(0.05)
                send_sock.sendto(fwreq, ("127.0.0.1", DEFAULT_DISCOVERY_PORT))

            sender = asyncio.create_task(send_fwreq())
            ports = await runner._collect_fwreq_ports(recv_sock, [board], timeout=2)
            await sender
            return ports
        finally:
            recv_sock.close()
            send_sock.close()

    ports = asyncio.run(exercise())
    assert ports == {"10.0.0.99": 10136}


def test_collect_fwreq_ports_marks_tcpipe_already_running():
    board = CRS(serial=None, hostname="127.0.0.1")
    board.tcpipe_version = None
    runner = CRSArmFirmwareUploader(
        boards=[board],
        required_version=CRS.REQUIRED_TCPIPE_VERSION,
        firmware_pathname=CRS.DEFAULT_ARM_FIRMWARE_PATHNAME,
    )

    async def exercise():
        recv_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            CRSArmFirmwareUploader._bind_fwreq_socket(recv_sock)
            send_sock.bind(("127.0.0.1", 10137))

            async def send_tcpipe():
                await asyncio.sleep(0.05)
                send_sock.sendto(
                    TCPIPE_BEACON, ("127.0.0.1", DEFAULT_DISCOVERY_PORT))

            sender = asyncio.create_task(send_tcpipe())
            ports = await runner._collect_fwreq_ports(recv_sock, [board], timeout=2)
            await sender
            return ports
        finally:
            recv_sock.close()
            send_sock.close()

    ports = asyncio.run(exercise())
    assert ports == {}
    assert board.tcpipe_version == CRS.REQUIRED_TCPIPE_VERSION


def test_collect_fwreq_ports_raises_on_wrong_tcpipe_version():
    wrong = TCPIPE_BEACON.replace(
        b"Bootloader Version: 1.0", b"Bootloader Version: 0.9")
    board = CRS(serial="0037", hostname="127.0.0.1")
    board.tcpipe_version = None
    runner = CRSArmFirmwareUploader(
        boards=[board],
        required_version="v1.2-3-gabcd12",
        firmware_pathname=CRS.DEFAULT_ARM_FIRMWARE_PATHNAME,
    )

    async def exercise():
        recv_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            CRSArmFirmwareUploader._bind_fwreq_socket(recv_sock)
            send_sock.bind(("127.0.0.1", 10133))

            async def send_tcpipe():
                await asyncio.sleep(0.05)
                send_sock.sendto(wrong, ("127.0.0.1", DEFAULT_DISCOVERY_PORT))

            sender = asyncio.create_task(send_tcpipe())
            try:
                await runner._collect_fwreq_ports(recv_sock, [board], timeout=2)
            finally:
                await sender
        finally:
            recv_sock.close()
            send_sock.close()

    with pytest.raises(RuntimeError, match=(
            r"measured '0\.9', expected 'v1\.2-3-gabcd12'"
            r".*Reboot the boards")):
        asyncio.run(exercise())


def test_wait_for_tcpipe_beacons_raises_on_wrong_version(monkeypatch):
    wrong = TCPIPE_BEACON.replace(
        b"Bootloader Version: 1.0", b"Bootloader Version: 0.9")
    board = CRS(serial="0037", hostname="127.0.0.1")
    runner = CRSArmFirmwareUploader(
        boards=[board],
        required_version="v1.2-3-gabcd12",
        firmware_pathname=CRS.DEFAULT_ARM_FIRMWARE_PATHNAME,
    )
    monkeypatch.setattr(
        "pychfpga.hardware.crs.crs_discovery.request_beacon", lambda *a, **k: None)

    async def exercise():
        recv_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            CRSArmFirmwareUploader._bind_fwreq_socket(recv_sock)
            send_sock.bind(("127.0.0.1", 10134))

            async def send_tcpipe():
                await asyncio.sleep(0.05)
                send_sock.sendto(wrong, ("127.0.0.1", DEFAULT_DISCOVERY_PORT))

            sender = asyncio.create_task(send_tcpipe())
            try:
                await runner._wait_for_tcpipe_beacons(
                    recv_sock, [board], timeout=2)
            finally:
                await sender
        finally:
            recv_sock.close()
            send_sock.close()

    with pytest.raises(RuntimeError, match=(
            r"measured '0\.9', expected 'v1\.2-3-gabcd12'"
            r".*Reboot the boards")):
        asyncio.run(exercise())


def test_fpga_array_routes_crs_crates_to_crs_network_discovery():
    assert CRS.get_network_discovery_function().__name__ == "discover_crs"


def test_tcpipe_version_from_elf_reads_marker(tmp_path):
    elf = tmp_path / "tcpipe_rtos.elf"
    elf.write_bytes(b"junk\0TCPIPE_GIT=1.5-3-g571c24-dirty\0more")
    assert tcpipe_version_from_elf(elf) == "1.5-3-g571c24-dirty"


def test_tcpipe_version_from_elf_missing(tmp_path):
    elf = tmp_path / "empty.elf"
    elf.write_bytes(b"no marker here")
    assert tcpipe_version_from_elf(elf) is None
    assert tcpipe_version_from_elf(tmp_path / "missing.elf") is None


def test_discovery_records_tcpipe_version():
    discovery = CRSDiscovery(
        board_targets=[("CRS", "003")],
        crate_targets=[],
        timeout=0,
    )
    beacon = CRSBeacon(
        b"TCPIPE/1\n"
        b"Manufacturer: t0\n"
        b"Product: crs\n"
        b"Revision: 4\n"
        b"Serial: 003\n"
        b"Bootloader: tcpipe\n"
        b"Bootloader Version: 0.9\n"
        b"\n",
        address="192.0.2.10",
    )

    handled_kind = discovery._process_beacon(None, beacon, ("192.0.2.10", 9875))
    [mb] = Motherboard.get_all_registered_instances()
    assert handled_kind == "TCPIPE"
    assert mb.tcpipe_version == "0.9"
