#!/usr/bin/env python3
"""Turn off the Agilent N5764A bench supply that powers the crate.

After turning the output off, waits out the crate's off-dwell before returning,
so running ps_power_on.py right after this script is a safe power cycle.
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
OFF_DWELL = 8  # s, as global_settings.ps_off_dwell in qc/ICE_crate/test_config.yaml; must stay >= 5 s


def describe(status):
    return f"{status['status']}, {status['voltage']:.2f} V, {status['current']:.2f} A"


def main():
    parser = argparse.ArgumentParser(description='Turn off the crate bench supply (Agilent N5764A).')
    parser.add_argument('--host', default=DEFAULT_HOST,
                        help=f'supply IP address or hostname (default: {DEFAULT_HOST})')
    args = parser.parse_args()

    ps = AgilentN5764A(hostname=args.host)
    try:
        ps.open()
    except OSError as e:
        sys.exit(f'Cannot connect to the supply at {args.host}:{ps.port}: {e}')
    print(f'Connected to {ps}')

    if not ps.is_enabled():
        print(f'Output is already off: {describe(ps.status())}')
        return

    ps.unlock()  # the driver starts locked and refuses state changes
    print('Turning off the output')
    ps.power_off()
    status = ps.status()
    if ps.is_enabled():
        sys.exit(f'Supply still reports its output enabled after being told to turn off: {describe(status)}')
    print(f'Supply is off: {describe(status)}')

    print(f'Waiting the {OFF_DWELL} s off-dwell so the crate can be powered on again right away')
    time.sleep(OFF_DWELL)


if __name__ == '__main__':
    main()
