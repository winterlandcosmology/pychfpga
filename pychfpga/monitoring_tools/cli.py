import argparse
import os

from pychfpga.monitoring_tools.common import env_float
from pychfpga.monitoring_tools.common import env_int


DEFAULT_CAPTURE_PERIOD_S = env_float('ADC_MONITOR_PERIOD_S', 0.5)
DEFAULT_HOST = os.environ.get('ADC_MONITOR_HOST', '0.0.0.0')
DEFAULT_PORT = env_int('ADC_MONITOR_PORT', 8000)
DEFAULT_POLL_INTERVAL_MS = env_int('ADC_MONITOR_POLL_MS', 500)
DEFAULT_MODE = os.environ.get('ADC_MONITOR_MODE', 'corr16')


def build_parser():
    parser = argparse.ArgumentParser(prog='adc', description='ADC monitoring tools')
    subparsers = parser.add_subparsers(dest='command', required=True)

    monitor = subparsers.add_parser('monitor', help='Start the ADC waveform monitor web server.')
    monitor.add_argument('iceboard_id', help='IceBoard ID, for example 257.')
    monitor.add_argument('--verbose', action='store_true', help='Do not suppress the receiver packet printouts.')
    monitor.add_argument('--host', default=DEFAULT_HOST, help=f'HTTP bind host. Default: {DEFAULT_HOST}.')
    monitor.add_argument('--port', type=int, default=DEFAULT_PORT, help=f'HTTP bind port. Default: {DEFAULT_PORT}.')
    monitor.add_argument(
        '--capture-period-s',
        type=float,
        default=DEFAULT_CAPTURE_PERIOD_S,
        help=f'ADC capture period in seconds. Default: {DEFAULT_CAPTURE_PERIOD_S}.',
    )
    monitor.add_argument(
        '--poll-interval-ms',
        type=int,
        default=DEFAULT_POLL_INTERVAL_MS,
        help=f'Browser polling interval in milliseconds. Default: {DEFAULT_POLL_INTERVAL_MS}.',
    )
    monitor.add_argument(
        '--mode',
        default=DEFAULT_MODE,
        help=f'FPGA array mode. Default: {DEFAULT_MODE}.',
    )

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == 'monitor':
        from pychfpga.monitoring_tools.adc_monitor import run_adc_monitor

        run_adc_monitor(
            iceboard_id=args.iceboard_id,
            verbose=args.verbose,
            host=args.host,
            port=args.port,
            capture_period_s=args.capture_period_s,
            poll_interval_ms=args.poll_interval_ms,
            mode=args.mode,
        )
