import argparse
import asyncio
import os
import sys
import threading

from pychfpga.fpga_array import FPGAArray

from pychfpga.monitoring_tools.common import AcquisitionThread
from pychfpga.monitoring_tools.common import LatestCaptureState
from pychfpga.monitoring_tools.common import StdoutSilencer
from pychfpga.monitoring_tools.common import env_float
from pychfpga.monitoring_tools.common import env_int
from pychfpga.monitoring_tools.frontend import build_waveform_monitor_html
from pychfpga.monitoring_tools.web_server import run_monitor_server


DEFAULT_CAPTURE_PERIOD_S = env_float('ADC_MONITOR_PERIOD_S', 0.5)
DEFAULT_HOST = os.environ.get('ADC_MONITOR_HOST', '0.0.0.0')
DEFAULT_PORT = env_int('ADC_MONITOR_PORT', 8000)
DEFAULT_POLL_INTERVAL_MS = env_int('ADC_MONITOR_POLL_MS', 500)
DEFAULT_MODE = os.environ.get('ADC_MONITOR_MODE', 'corr16')
INPUT_NAMES = [f'A{i + 1}' for i in range(8)] + [f'B{i + 1}' for i in range(8)]


def console_printer(stream):
    def emit(message):
        stream.write(f'{message}\n')
        stream.flush()

    return emit


def normalize_hwm(iceboard_id):
    value = str(iceboard_id).strip()
    lowered = value.lower()
    if lowered.startswith(('mb ', 'mgk7mb', 'crs', 'zcu111')) or any(ch.isspace() for ch in value):
        return value
    return f'mb {value}'


def build_connection_config(iceboard_id, mode):
    return {
        'hwm': normalize_hwm(iceboard_id),
        'prog': 1,
        'mode': mode,
    }


def build_adc_monitor_html(poll_interval_ms, description=None):
    description = description or (
        'Live 16-channel ADC waveforms served directly from the acquisition host. '
        'This page polls the latest capture and keeps only the newest frame.'
    )
    return build_waveform_monitor_html(
        title='ADC Monitor',
        description=description,
        poll_interval_ms=poll_interval_ms,
        trace_range_label='Range -128..127',
        columns=4,
        reverse_within_rows=True,
        color_by_row=True,
    )


class StandaloneAdcMonitorResources:
    def __init__(self, fpga_array, board, receiver, state, stop_event, acquisition_thread):
        self.fpga_array = fpga_array
        self.board = board
        self.receiver = receiver
        self.state = state
        self.stop_event = stop_event
        self.acquisition_thread = acquisition_thread

    @classmethod
    def create(cls, iceboard_id, capture_period_s, mode):
        config = build_connection_config(iceboard_id=iceboard_id, mode=mode)
        fpga_array = FPGAArray(**config)
        board = fpga_array.ib[0]
        channels = list(range(min(len(board.chan), len(INPUT_NAMES))))
        state = LatestCaptureState(INPUT_NAMES[:len(channels)])
        board.set_adc_delays()

        offset_binary_encoding = False
        if board.CORR:
            offset_binary_encoding = bool(board.CORR.REQUIRES_OFFSET_BINARY_ENCODING)

        # The standalone monitor uses one receiver for all ADC channels, so keep
        # the receiver-relevant channelizer settings homogeneous before capture.
        board.set_channelizer(
            data_source='adc',
            funcgen_left_shift=0,
            funcgen_right_shift=0,
            fft_bypass=True,
            reset_chan=True,
            channels=channels,
        )
        board.set_scaler_eight_bit(scaler_eight_bit=True, prober_user_flags=False, channels=channels)
        board.set_offset_binary_encoding(offset=offset_binary_encoding, channels=channels)
        board.set_scaler_output_modes(bypass_data_type=0, cap_data_type=0, channels=channels)
        board.configure_data_capture(
            period=capture_period_s, source='adc', channels=channels)
        board.start_data_capture()

        for channel in board.chan:
            channel.SCALER.STATS_FRAME_COUNT = 390625

        receiver = board.get_data_receiver(verbose=0)

        def read_latest_adc_frame():
            ts, data, counts = receiver.read_raw_frames(ncap=1, source=0, chan_first=False)
            return ts[0], data[0], counts[0]

        stop_event = threading.Event()
        acquisition_thread = AcquisitionThread(
            reader=read_latest_adc_frame,
            state=state,
            stop_event=stop_event,
            retry_delay_s=1.0,
            name='adc-monitor-acquisition',
        )
        acquisition_thread.start()
        return cls(fpga_array, board, receiver, state, stop_event, acquisition_thread)

    def close(self, console_print):
        self.stop_event.set()

        receiver_socket = getattr(self.receiver, 'socket', None)
        if receiver_socket is None:
            receiver_socket = getattr(getattr(self.receiver, 'pr', None), 'sock', None)
        if receiver_socket is not None:
            try:
                receiver_socket.close()
            except OSError:
                pass

        try:
            self.board.stop_data_capture()
        except Exception as exc:
            console_print(f'Warning: failed to stop data capture cleanly: {exc}')

        self.acquisition_thread.join(timeout=2.0)
        if self.acquisition_thread.is_alive():
            console_print('Warning: acquisition thread did not exit within 2 seconds.')


def run_adc_monitor(iceboard_id, verbose=False, host=DEFAULT_HOST, port=DEFAULT_PORT, capture_period_s=DEFAULT_CAPTURE_PERIOD_S, poll_interval_ms=DEFAULT_POLL_INTERVAL_MS, mode=DEFAULT_MODE):
    suppress_stdout = not verbose
    console_stream = sys.stderr if suppress_stdout else sys.stdout
    console_print = console_printer(console_stream)
    resources = None

    with StdoutSilencer(enabled=suppress_stdout):
        try:
            resources = StandaloneAdcMonitorResources.create(
                iceboard_id=iceboard_id,
                capture_period_s=capture_period_s,
                mode=mode,
            )
            asyncio.run(
                run_monitor_server(
                    state=resources.state,
                    index_html=build_adc_monitor_html(poll_interval_ms=poll_interval_ms),
                    host=host,
                    port=port,
                    console_print=console_print,
                    service_name='ADC monitor',
                )
            )
        except KeyboardInterrupt:
            pass
        except Exception as exc:
            console_print(f'ADC monitor failed to start: {exc}')
            raise
        finally:
            if resources is not None:
                resources.close(console_print)


def build_parser(prog='adc_monitor'):
    parser = argparse.ArgumentParser(description='Serve the ADC waveform monitor over HTTP.', prog=prog)
    parser.add_argument('iceboard_id', help='IceBoard ID, for example 257.')
    parser.add_argument('--verbose', action='store_true', help='Do not suppress the receiver packet printouts.')
    parser.add_argument('--host', default=DEFAULT_HOST, help=f'HTTP bind host. Default: {DEFAULT_HOST}.')
    parser.add_argument('--port', type=int, default=DEFAULT_PORT, help=f'HTTP bind port. Default: {DEFAULT_PORT}.')
    parser.add_argument(
        '--capture-period-s',
        type=float,
        default=DEFAULT_CAPTURE_PERIOD_S,
        help=f'ADC capture period in seconds. Default: {DEFAULT_CAPTURE_PERIOD_S}.',
    )
    parser.add_argument(
        '--poll-interval-ms',
        type=int,
        default=DEFAULT_POLL_INTERVAL_MS,
        help=f'Browser polling interval in milliseconds. Default: {DEFAULT_POLL_INTERVAL_MS}.',
    )
    parser.add_argument(
        '--mode',
        default=DEFAULT_MODE,
        help=f'FPGA array mode. Default: {DEFAULT_MODE}.',
    )
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    run_adc_monitor(
        iceboard_id=args.iceboard_id,
        verbose=args.verbose,
        host=args.host,
        port=args.port,
        capture_period_s=args.capture_period_s,
        poll_interval_ms=args.poll_interval_ms,
        mode=args.mode,
    )
