import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'deployment_scripts'))

import ps_power_off  # noqa: E402
import ps_power_on  # noqa: E402


def make_supply(enabled=False, state='OFF', connect_error=None):
    """Return a stand-in for the AgilentN5764A class and the list of state-changing calls it records."""
    calls = []

    class FakeSupply:
        port = 5025

        def __init__(self, hostname, timeout=0.5):
            self.ps = self  # the scripts reach the setpoint commands through ps.ps
            self.enabled = enabled
            self.state = state

        def open(self):
            if connect_error:
                raise connect_error

        def status(self):
            return {'status': self.state, 'voltage': 20.0 * self.enabled, 'current': 14.0 * self.enabled}

        def is_enabled(self):
            return self.enabled

        def unlock(self):
            pass

        def clear(self):
            calls.append('clear')

        def set_voltage(self, voltage):
            calls.append(('set_voltage', voltage))

        def set_current_limit(self, current, ocp):
            calls.append(('set_current_limit', current, ocp))

        def power_on(self):
            calls.append('power_on')
            self.enabled, self.state = True, 'OK'

        def power_off(self):
            calls.append('power_off')
            self.enabled, self.state = False, 'OFF'

    return FakeSupply, calls


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(ps_power_on.time, 'sleep', lambda s: None)


def test_power_on_leaves_a_running_supply_unchanged(monkeypatch):
    supply, calls = make_supply(enabled=True, state='OK')
    monkeypatch.setattr(ps_power_on, 'AgilentN5764A', supply)
    assert ps_power_on.power_on('ps.invalid') is False
    assert calls == []


def test_power_on_rejects_an_enabled_supply_in_fault(monkeypatch):
    supply, calls = make_supply(enabled=True, state='FAULT')
    monkeypatch.setattr(ps_power_on, 'AgilentN5764A', supply)
    with pytest.raises(RuntimeError, match='ps_power_off.py'):
        ps_power_on.power_on('ps.invalid')
    assert calls == []


def test_power_on_programs_the_setpoints_then_turns_on(monkeypatch, no_sleep):
    supply, calls = make_supply()
    monkeypatch.setattr(ps_power_on, 'AgilentN5764A', supply)
    assert ps_power_on.power_on('ps.invalid') is True
    assert calls == [('set_voltage', 20), ('set_current_limit', 76, True), 'power_on']


def test_power_off_reports_an_unreachable_supply_as_runtime_error(monkeypatch):
    # Callers that retry power_off on failure need a connection error to raise, not exit
    supply, calls = make_supply(enabled=True, state='OK', connect_error=ConnectionRefusedError('refused'))
    monkeypatch.setattr(ps_power_off, 'AgilentN5764A', supply)
    with pytest.raises(RuntimeError, match='Cannot connect'):
        ps_power_off.power_off('ps.invalid')
    assert calls == []


@pytest.mark.parametrize('dwell, sleeps', [(True, [ps_power_off.OFF_DWELL]), (False, [])])
def test_power_off_waits_the_off_dwell_only_when_asked(monkeypatch, dwell, sleeps):
    supply, calls = make_supply(enabled=True, state='OK')
    monkeypatch.setattr(ps_power_off, 'AgilentN5764A', supply)
    slept = []
    monkeypatch.setattr(ps_power_off.time, 'sleep', slept.append)
    ps_power_off.power_off('ps.invalid', dwell=dwell)
    assert calls == ['power_off']
    assert slept == sleeps
