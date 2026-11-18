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


RECOMMENDED = network_utils.RECOMMENDED_UDP_RCVBUF_BYTES
BYTES_OK = {name: str(value) for name, value in network_utils.RECOMMENDED_UDP_SYSCTLS.items()}


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
    assert required == [500, 65536, 65536]  # 256 MiB = 65536 pages; never lower than current


def test_low_byte_settings_fail(monkeypatch):
    _fake_sysctl(monkeypatch, {**BYTES_OK, 'net.core.rmem_default': '212992',
                               'net.ipv4.udp_mem': '376188 501588 752376'})

    results = network_utils.check_udp_buffer_settings()

    assert results['rmem_default'] == (False, 212992, RECOMMENDED)
    assert results['udp_mem'][0]


def test_recommendation_is_one_value_for_all_byte_settings():
    # One number for every mode, sized from the measured per-packet kernel cost (see
    # network_utils.RECOMMENDED_UDP_SYSCTLS); the three byte settings must stay equal.
    assert RECOMMENDED == 256 * 1024 * 1024
    assert set(network_utils.RECOMMENDED_UDP_SYSCTLS) == {
        'net.core.rmem_max', 'net.core.rmem_default', 'net.ipv4.udp_rmem_min'}
    assert set(network_utils.RECOMMENDED_UDP_SYSCTLS.values()) == {RECOMMENDED}


def test_udp_buffer_problems_empty_when_recommended(monkeypatch):
    _fake_sysctl(monkeypatch, {**BYTES_OK, 'net.ipv4.udp_mem': '376188 501588 752376'})

    assert network_utils.udp_buffer_problems() == []


def test_udp_buffer_problems_list_each_low_setting(monkeypatch):
    _fake_sysctl(monkeypatch, {**BYTES_OK, 'net.core.rmem_default': '212992',
                               'net.ipv4.udp_mem': '500 1000 9000'})

    problems = network_utils.udp_buffer_problems()

    assert problems == [
        f'rmem_default: current 212992, required >= {RECOMMENDED} (bytes)',
        'udp_mem: current [500, 1000, 9000], required >= [500, 65536, 65536] (pages)',
    ]


def test_fix_hint_applies_now_and_persists():
    hint = network_utils.udp_buffer_fix_hint()

    assert (f'sudo sysctl -w net.core.rmem_max={RECOMMENDED} net.core.rmem_default={RECOMMENDED} '
            f'net.ipv4.udp_rmem_min={RECOMMENDED}') in hint
    assert '/etc/sysctl.d/90-pychfpga-udp.conf' in hint
