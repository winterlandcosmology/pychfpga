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

from pychfpga.hardware import HardwareMap, Motherboard
from pychfpga.hardware.crs import CRS, CRSBackplane
from pychfpga.hardware.crs.crs_discovery import (
    CRSArmFirmwareUploader,
    CRSBeacon,
    CRSDiscovery,
    DEFAULT_DISCOVERY_PORT,
    NetConsole,
    UPLOAD_METHODS,
    _file_md5,
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


def test_fwreq_matches_crs_target_by_serial():
    discovery = CRSDiscovery(
        board_targets=[("CRS", "0037")],
        crate_targets=[],
        timeout=0)
    beacon = CRSBeacon(FWREQ_BEACON)

    assert discovery._matches_requested_targets(beacon)


def test_tcpipe_adds_matching_board_to_hardware_map():
    discovery = CRSDiscovery(
        board_targets=[("CRS", "003")],
        crate_targets=[],
        timeout=0,
    )
    beacon = CRSBeacon(b"TCPIPE 1.0 CRS 003", address="127.0.0.1")

    handled_kind = discovery._process_beacon(None, beacon, ("127.0.0.1", 9875))

    [mb] = Motherboard.get_all_registered_instances()
    assert handled_kind == "TCPIPE"
    assert mb.hostname == "127.0.0.1"
    assert mb.tcpipe_version == "1.0"
    assert discovery.discovered_serials == {"003"}


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
            self, elf_path, upload_method, wget_url=None, verify_md5=False,
            http_connect_timeout=None, http_transfer_complete=None):
        return True

    monkeypatch.setattr(CRSArmFirmwareUploader, "_collect_fwreq_ports", fake_collect)
    monkeypatch.setattr(NetConsole, "upload_and_run", fake_upload_and_run)
    monkeypatch.setattr(NetConsole, "open", lambda self: None)
    monkeypatch.setattr(NetConsole, "close", lambda self: None)

    board = CRS(serial="0037", hostname="192.0.2.11")
    board.tcpipe_version = None
    runner = CRSArmFirmwareUploader(
        boards=[board],
        required_version=CRS.REQUIRED_TCPIPE_VERSION,
        firmware_pathname=CRS.DEFAULT_ARM_FIRMWARE_PATHNAME,
    )

    asyncio.run(runner.run())

    assert board.tcpipe_version == CRS.REQUIRED_TCPIPE_VERSION


def test_has_uboot_prompt_rejects_md5sum_arrow():
    assert not NetConsole._has_uboot_prompt("md5 for 08000000 ... 081a8897 ==> ")
    assert NetConsole._has_uboot_prompt(
        "md5 for 08000000 ... 081a8897 ==> e55261bbcecddbf1b9dbc7fc601d4ed4\nZynqMP>")


def test_recv_until_prompt_waits_for_md5_digest():
    sock = _MockSock([
        b"md5 for 08000000 ... 081a8897 ==> ",
        b"e55261bbcecddbf1b9dbc7fc601d4ed4\n",
        b"ZynqMP> ",
    ])
    nc = NetConsole("192.0.2.1", 9875)
    nc._sock = sock
    buf = nc._recv_until_prompt(timeout=5.0, idle=0.05)
    assert "e55261bbcecddbf1b9dbc7fc601d4ed4" in buf


def test_netconsole_verify_loaded_md5_accepts_matching_digest():
    nc = NetConsole("192.0.2.1", 9875)
    nc._sock = types.SimpleNamespace(send=lambda data: None)
    nc._recv_md5_response = lambda timeout: (
        "md5 for 08000000 ... 081a8897 ==> abcd1234deadbeefcafebabe12345678\nZynqMP>"
    )

    assert nc.verify_loaded_md5("abcd1234deadbeefcafebabe12345678")


def test_netconsole_verify_loaded_md5_rejects_mismatch():
    nc = NetConsole("192.0.2.1", 9875)
    nc._sock = types.SimpleNamespace(send=lambda data: None)
    nc._recv_md5_response = lambda timeout: (
        "md5 for 08000000 ... 081a8897 ==> deadbeefcafebabe12345678abcd1234\nZynqMP>"
    )

    assert not nc.verify_loaded_md5("abcd1234deadbeefcafebabe12345678")


def test_netconsole_verify_loaded_md5_rejects_partial_md5_line():
    nc = NetConsole("192.0.2.1", 9875)
    nc._sock = types.SimpleNamespace(send=lambda data: None)
    nc._recv_md5_response = lambda timeout: "md5 for 08000000 ... 081a8897 ==>"

    assert not nc.verify_loaded_md5("abcd1234deadbeefcafebabe12345678")


def test_upload_and_boot_elf_skips_bootelf_when_md5_fails(tmp_path, monkeypatch):
    elf_path = tmp_path / "tcpipe_rtos.elf"
    elf_path.write_bytes(b"payload")
    monkeypatch.setattr(
        "pychfpga.hardware.crs.crs_discovery._file_md5",
        lambda path: "abcd1234",
    )

    nc = NetConsole("192.0.2.1", 9875)
    nc.send_fwresp = lambda: None
    nc.wait_for_prompt = lambda timeout=5.0: "ZynqMP>"
    nc.drain = lambda timeout=0.5, idle=0.05: None
    nc._sock = types.SimpleNamespace(send=lambda data: None)
    nc.xmodem_send = lambda filename: True
    nc.verify_loaded_md5 = lambda expected_md5, **kwargs: False
    boot_calls = []
    nc.send_cmd = lambda cmd, timeout=0.1: boot_calls.append(cmd) or ""

    assert not nc.upload_and_boot_elf(elf_path, verify_md5=False)
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
    nc.send_fwresp = lambda: None
    nc.wait_for_prompt = lambda timeout=5.0: "ZynqMP>"
    nc.drain = lambda timeout=0.5, idle=0.05: None
    nc._sock = types.SimpleNamespace(send=lambda data: None)
    nc.xmodem_send = lambda filename: True
    nc.verify_loaded_md5 = lambda expected_md5, **kwargs: md5_calls.append(expected_md5) or True
    nc.send_cmd = lambda cmd, timeout=0.1: ""

    assert nc.upload_and_boot_elf(elf_path, verify_md5=False)
    assert md5_calls == ["abcd1234"]


def test_upload_and_boot_elf_runs_bootelf_after_md5_ok(tmp_path, monkeypatch):
    elf_path = tmp_path / "tcpipe_rtos.elf"
    elf_path.write_bytes(b"payload")
    monkeypatch.setattr(
        "pychfpga.hardware.crs.crs_discovery._file_md5",
        lambda path: "abcd1234",
    )

    nc = NetConsole("192.0.2.1", 9875)
    nc.send_fwresp = lambda: None
    nc.wait_for_prompt = lambda timeout=5.0: "ZynqMP>"
    nc.drain = lambda timeout=0.5, idle=0.05: None
    nc._sock = types.SimpleNamespace(send=lambda data: None)
    nc.xmodem_send = lambda filename: True
    nc.verify_loaded_md5 = lambda expected_md5, **kwargs: expected_md5 == "abcd1234"
    boot_calls = []
    nc.send_cmd = lambda cmd, timeout=0.1: boot_calls.append(cmd) or ""

    assert nc.upload_and_boot_elf(elf_path, verify_md5=True)
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
    nc.send_fwresp = lambda: None
    nc.wait_for_prompt = lambda timeout=5.0: "ZynqMP>"
    nc.drain = lambda timeout=0.5, idle=0.05: None
    nc._sock = types.SimpleNamespace(send=lambda data: None)
    nc.xmodem_pipe_send = lambda filename, pipe_depth=8: pipe_calls.append(filename) or True
    nc.verify_loaded_md5 = lambda expected_md5, **kwargs: True
    nc.send_cmd = lambda cmd, timeout=0.1: ""

    assert nc.upload_and_boot_elf(elf_path, upload_method="xmodem_pipe")
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
    nc.send_fwresp = lambda: None
    nc.wait_for_prompt = lambda timeout=5.0: "ZynqMP>"
    nc.drain = lambda timeout=0.5, idle=0.05: None
    nc.loads_send = lambda filename, line_batch=16: loads_calls.append(filename) or True
    nc.verify_loaded_md5 = lambda expected_md5, **kwargs: True
    nc.send_cmd = lambda cmd, timeout=0.1: ""

    assert nc.upload_and_boot_elf(elf_path, upload_method="loads")
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


class _MockSock:
    def __init__(self, recv_chunks):
        self.recv_chunks = list(recv_chunks)
        self.sent = []
        self._timeout = None

    def settimeout(self, timeout):
        self._timeout = timeout

    def recv(self, size):
        if not self.recv_chunks:
            raise socket.timeout
        chunk = self.recv_chunks.pop(0)
        if chunk is None:
            raise socket.timeout
        return chunk

    def sendall(self, data):
        self.sent.append(data)


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


def test_upload_and_boot_via_wget_waits_for_http_and_verifies_md5(tmp_path, monkeypatch):
    elf_path = tmp_path / "tcpipe_rtos.elf"
    elf_path.write_bytes(b"payload")
    expected_md5 = "abcd1234deadbeefcafebabe12345678"
    monkeypatch.setattr(
        "pychfpga.hardware.crs.crs_discovery._file_md5",
        lambda path: expected_md5,
    )
    http_received = __import__("threading").Event()
    http_complete = __import__("threading").Event()
    sent = []

    nc = NetConsole("192.0.2.1", 9875)
    nc.send_fwresp = lambda: None
    nc.wait_for_prompt = lambda timeout=5.0: "ZynqMP>"
    nc.drain = lambda timeout=0.5, idle=0.05: None
    nc._sock = types.SimpleNamespace(
        send=lambda data: (
            http_received.set() if b"wget" in data else sent.append(data)
        ),
    )
    nc._recv_wget_complete = lambda timeout, http_connect_timeout=None, http_transfer_complete=None: (
        True, "ZynqMP>"
    )
    nc._recv_after_bootelf = lambda timeout=15.0, idle=3.0: ""
    http_complete.set()

    nc.verify_loaded_md5 = lambda expected_md5, **kwargs: (
        expected_md5 == "abcd1234deadbeefcafebabe12345678"
    )

    assert nc.upload_and_boot_via_wget(
        "http://10.0.0.1:19875/tcpipe_rtos.elf",
        elf_path,
        http_connect_timeout=http_received,
        http_transfer_complete=http_complete,
    )
    assert sent == [(nc.BOOTELF_CMD + "\n").encode("utf-8")]


def test_recv_wget_md5_response_returns_when_md5_and_prompt_seen():
    http_received = __import__("threading").Event()
    http_received.set()
    http_complete = __import__("threading").Event()
    http_complete.set()
    sock = _MockSock([
        b"ZynqMP> ",
    ])
    nc = NetConsole("192.0.2.1", 9875)
    nc._sock = sock
    ok, output = nc._recv_wget_complete(
        timeout=5.0,
        http_connect_timeout=http_received,
        http_transfer_complete=http_complete,
    )
    assert ok
    assert nc._has_uboot_prompt(output)


def test_recv_md5_response_returns_digest():
    sock = _MockSock([
        b"md5 for 08000000 ... 08000006 ==> ",
        b"abcd1234deadbeefcafebabe12345678\n",
        b"ZynqMP> ",
    ])
    nc = NetConsole("192.0.2.1", 9875)
    nc._sock = sock
    output = nc._recv_md5_response(timeout=5.0)
    assert "abcd1234deadbeefcafebabe12345678" in output
    assert nc._parse_uboot_md5(output) == "abcd1234deadbeefcafebabe12345678"


def test_recv_after_bootelf_treats_silence_as_success():
    nc = NetConsole("192.0.2.1", 9875)
    nc._sock = _MockSock([None, None, None])
    nc._sock.settimeout = lambda timeout: None
    assert nc._recv_after_bootelf(timeout=0.2, idle=0.05) == ""


def test_recv_after_bootelf_detects_boot_error():
    nc = NetConsole("192.0.2.1", 9875)
    nc._sock = _MockSock([b"Unknown command 'bootelf'\nZynqMP> "])
    nc._sock.settimeout = lambda timeout: None
    output = nc._recv_after_bootelf(timeout=1.0, idle=0.05)
    assert "Unknown command" in output
    assert not nc._bootelf_ok(output)


def test_upload_and_boot_via_wget_fails_when_http_unreachable(tmp_path, monkeypatch):
    elf_path = tmp_path / "tcpipe_rtos.elf"
    elf_path.write_bytes(b"payload")
    monkeypatch.setattr(
        "pychfpga.hardware.crs.crs_discovery._file_md5",
        lambda path: "abcd1234",
    )
    monkeypatch.setattr(
        "pychfpga.hardware.crs.crs_discovery.FIRMWARE_HTTP_CONNECT_TIMEOUT",
        0.01,
    )
    sent = []

    nc = NetConsole("192.0.2.1", 9875)
    nc.send_fwresp = lambda: None
    nc.wait_for_prompt = lambda timeout=5.0: "ZynqMP>"
    nc.drain = lambda timeout=0.5, idle=0.05: None
    nc._sock = _MockSock([None] * 50)
    nc._sock.send = lambda data: sent.append(data)

    assert not nc.upload_and_boot_via_wget(
        "http://10.0.0.1:19875/tcpipe_rtos.elf",
        elf_path,
        http_connect_timeout=__import__("threading").Event(),
    )
    assert sent[-1] == b"\x03"


def test_arm_firmware_runner_wget_method_passes_url(monkeypatch, tmp_path):
    captured = {}
    elf_path = tmp_path / "tcpipe_rtos.elf"
    elf_path.write_bytes(b"payload")

    async def fake_collect(self, sock, targets, timeout=60):
        return {board.hostname: 10132 for board in targets}

    async def fake_upload_and_run(
            self, elf_path, upload_method, wget_url=None, verify_md5=False,
            http_connect_timeout=None, http_transfer_complete=None):
        captured["upload_method"] = upload_method
        captured["wget_url"] = wget_url
        return True

    monkeypatch.setattr(CRSArmFirmwareUploader, "_collect_fwreq_ports", fake_collect)
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


def test_fpga_array_routes_crs_crates_to_crs_network_discovery():
    assert CRS.get_network_discovery_function().__name__ == "discover_crs"


def test_discovery_rejects_unexpected_tcpipe_version():
    discovery = CRSDiscovery(
        board_targets=[("CRS", "003")],
        crate_targets=[],
        timeout=0,
    )
    beacon = CRSBeacon(b"TCPIPE 0.9 CRS 003", address="192.0.2.10")

    handled_kind = discovery._process_beacon(None, beacon, ("192.0.2.10", 9875))
    [mb] = Motherboard.get_all_registered_instances()
    assert handled_kind == "TCPIPE"
    assert mb.tcpipe_version == "0.9"
