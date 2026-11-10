#!/usr/bin/env python3
"""Turn on the Agilent N5764A bench supply that powers the crate.

Programs the crate setpoints (20 V, 76 A current limit with over-current
protection) and enables the output, following the sequence the ICE-crate QC
suite uses (TestUtils.open_ps and power_cycle in qc/ICE_crate/f_engine_tests.py).
A latched protection fault is cleared first. If the output is already on and
regulating, nothing is changed.

The boards take 2-3 minutes to boot after power-on: wait that long before
connecting to the crate.
"""

import argparse
import sys
import time
from pathlib import Path

# Load the in-repo N5764A driver as a standalone package. It only needs the
# standard library, so the supply can be driven on a host where the full
# pychfpga environment (wtl packages, numpy, ...) is not installed.
sys.path.append(str(Path(__file__).resolve().parents[1] / 'pychfpga' / 'hardware'))
from Agilent_N5764A import AgilentN5764A  # noqa: E402

DEFAULT_HOST = '10.10.5.1'
PS_VOLTAGE = 20  # V, as global_settings.ps_voltage in qc/ICE_crate/test_config.yaml
PS_CURRENT = 76  # A, the N5764A rating, as global_settings.ps_current there


def describe(status):
    return f"{status['status']}, {status['voltage']:.2f} V, {status['current']:.2f} A"


def power_on(host):
    """Turn the supply at `host` on.

    Returns True if the output was turned on, False if it was already on and
    was left unchanged. Raises RuntimeError if the crate cannot be powered.
    """
    ps = AgilentN5764A(hostname=host)
    try:
        ps.open()
    except OSError as e:
        raise RuntimeError(f'Cannot connect to the supply at {host}:{ps.port}: {e}') from e
    print(f'Connected to {ps}')

    # Validate the current state before changing anything: an enabled output is
    # either already powering the crate or in a state this script does not recover.
    status = ps.status()
    if ps.is_enabled():
        if status['status'] in ('OK', 'ILIMIT'):
            print(f'Output is already on, leaving it unchanged: {describe(status)}')
            return False
        raise RuntimeError(f'Output is enabled but the supply reports {describe(status)}. '
                           f'Run ps_power_off.py, then retry.')

    ps.unlock()  # the driver starts locked and refuses state changes
    if status['status'] == 'FAULT':
        print('Supply is in FAULT: clearing the latched protection')
        ps.ps.clear()
    ps.ps.set_voltage(voltage=PS_VOLTAGE)
    ps.ps.set_current_limit(current=PS_CURRENT, ocp=True)
    print(f'Turning on the output at {PS_VOLTAGE} V with a {PS_CURRENT} A limit')
    ps.power_on()

    time.sleep(2)  # let the output ramp up before judging its state
    status = ps.status()
    if status['status'] not in ('OK', 'ILIMIT'):
        raise RuntimeError(f'Supply is {describe(status)} after being told to turn on: the crate has no power.')
    print(f'Supply is on: {describe(status)}')
    return True


def main():
    parser = argparse.ArgumentParser(description='Turn on the crate bench supply (Agilent N5764A).')
    parser.add_argument('--host', default=DEFAULT_HOST,
                        help=f'supply IP address or hostname (default: {DEFAULT_HOST})')
    args = parser.parse_args()

    try:
        if power_on(args.host):
            print('The boards take 2-3 minutes to boot before the crate can be connected to.')
    except RuntimeError as e:
        sys.exit(str(e))


if __name__ == '__main__':
    main()
