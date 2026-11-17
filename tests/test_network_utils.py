import subprocess
from types import SimpleNamespace

import pytest

from pychfpga import network_utils


def _fake_sysctl(monkeypatch, values):
    """Make check_udp_buffer_settings() read `values` ({sysctl name: string}) instead of sysctl."""
    def run(cmd, capture_output, text, check):
        return SimpleNamespace(stdout=values[cmd[-1]] + '\n')
    monkeypatch.setattr(network_utils.subprocess, 'run', run)
    monkeypatch.setattr(network_utils.os, 'sysconf', lambda name: 4096)


BYTES_OK = {
    'net.core.rmem_max': '26214400',
    'net.core.rmem_default': '26214400',
    'net.ipv4.udp_rmem_min': '26214400',
}


def test_kernel_default_udp_mem_is_sufficient(monkeypatch):
    # frost's kernel defaults: udp_mem is in 4 KiB pages (~1.4 / 1.9 / 2.9 GiB)
    _fake_sysctl(monkeypatch, {**BYTES_OK, 'net.ipv4.udp_mem': '376188\t501588\t752376'})

    results = network_utils.check_udp_buffer_settings()

    assert all(is_ok for is_ok, _, _ in results.values())
    assert results['udp_mem'][1] == [376188, 501588, 752376]


def test_small_udp_mem_fails_and_suggestion_never_lowers_values(monkeypatch):
    _fake_sysctl(monkeypatch, {**BYTES_OK, 'net.ipv4.udp_mem': '500 1000 9000'})

    is_ok, current, required = network_utils.check_udp_buffer_settings()['udp_mem']

    assert not is_ok
    assert current == [500, 1000, 9000]
    assert required == [500, 6400, 9000]  # 25 MiB = 6400 pages; the max is already larger


def test_low_byte_settings_fail(monkeypatch):
    _fake_sysctl(monkeypatch, {**BYTES_OK, 'net.core.rmem_default': '212992',
                               'net.ipv4.udp_mem': '376188 501588 752376'})

    results = network_utils.check_udp_buffer_settings()

    assert results['rmem_default'] == (False, 212992, 26214400)
    assert results['udp_mem'][0]
