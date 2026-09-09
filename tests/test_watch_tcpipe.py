"""Unit tests for watch_tcpipe target classification (no hardware)."""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "watch_tcpipe.py"
_SPEC = importlib.util.spec_from_file_location("watch_tcpipe_under_test", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
watch_tcpipe = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(watch_tcpipe)


def test_classify_serial_number():
    assert watch_tcpipe.classify_target("39") == ("serial", "39")
    assert watch_tcpipe.classify_target("0037") == ("serial", "0037")
    assert watch_tcpipe.classify_target(" 55 ") == ("serial", "55")


def test_classify_ipv4():
    assert watch_tcpipe.classify_target("192.168.88.19") == ("ip", "192.168.88.19")
    assert watch_tcpipe.classify_target("10.0.0.1") == ("ip", "10.0.0.1")


def test_classify_uart_path():
    assert watch_tcpipe.classify_target("/dev/ttyUSB3") == ("uart", "/dev/ttyUSB3")
    assert watch_tcpipe.classify_target(
        "/dev/serial/by-id/usb-Xilinx_t0.technology_CRS_00039-if01-port0"
    )[0] == "uart"
    assert watch_tcpipe.classify_target("ttyUSB0") == ("uart", "ttyUSB0")


def test_collect_monitors_serials_and_ip_and_uart():
    args = argparse.Namespace(
        targets=["55", "37", "192.168.88.19", "/dev/ttyUSB3"],
        serial_devices=None,
        no_serial=False,
    )
    uarts, ips, serials = watch_tcpipe.collect_monitors(args)
    assert serials == ["55", "37"]
    assert ips == ["192.168.88.19"]
    assert uarts == ["/dev/ttyUSB3"]


def test_collect_monitors_default_uart_when_no_targets():
    args = argparse.Namespace(targets=[], serial_devices=None, no_serial=False)
    uarts, ips, serials = watch_tcpipe.collect_monitors(args)
    assert uarts == [watch_tcpipe.DEFAULT_SERIAL]
    assert ips == []
    assert serials == []


def test_collect_monitors_no_serial_skips_uart():
    args = argparse.Namespace(
        targets=["39", "/dev/ttyUSB0"],
        serial_devices=["/dev/ttyUSB3"],
        no_serial=True,
    )
    uarts, ips, serials = watch_tcpipe.collect_monitors(args)
    assert uarts == []
    assert serials == ["39"]


def test_board_ip_map_matches_integer_serials():
    ip_map = watch_tcpipe.BoardIpMap()
    assert ip_map.update("0039", "192.168.88.19") is True
    assert ip_map.ip_for("39") == "192.168.88.19"
    assert ip_map.update("39", "192.168.88.19") is False
    assert ip_map.update("0039", "192.168.88.20") is True
    assert ip_map.ip_for("39") == "192.168.88.20"


def test_uart_label_extracts_crs_serial():
    label = watch_tcpipe.uart_label(
        "/dev/serial/by-id/usb-Xilinx_t0.technology_CRS_00039-if01-port0"
    )
    assert label == "SN39 uart"
    assert watch_tcpipe.uart_label("/dev/ttyUSB3") == "ttyUSB3"
