import argparse
import asyncio
import contextlib
import os
import sys
import threading

from pychfpga.fpga_array import FPGAArray

from pychfpga.monitoring_tools.common import AcquisitionThread
from pychfpga.monitoring_tools.common import LatestCaptureState
from pychfpga.monitoring_tools.common import MultiBoardCaptureState
from pychfpga.monitoring_tools.common import StdoutSilencer
from pychfpga.monitoring_tools.common import env_float
from pychfpga.monitoring_tools.common import env_int
from pychfpga.monitoring_tools.frontend import build_waveform_monitor_html
from pychfpga.monitoring_tools.web_server import MonitorServer
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


def describe_board(board):
    crate, slot = board.get_id(default_crate=0, default_slot=0)
    part_number = getattr(board, 'part_number', board.__class__.__name__)
    serial = getattr(board, 'serial', None)
    if serial:
        return f'{part_number} SN{serial} (crate {crate}, slot {slot})'
    return f'{part_number} (crate {crate}, slot {slot})'


def board_state_key(board):
    crate, slot = board.get_id(default_crate=0, default_slot=0)
    return f'{int(crate)}:{int(slot)}'


def board_state_label(board):
    crate, slot = board.get_id(default_crate=0, default_slot=0)
    serial = getattr(board, 'serial', None)
    if serial is not None:
        return f'mb {serial} (crate {crate}, slot {slot})'
    part_number = getattr(board, 'part_number', board.__class__.__name__)
    return f'{part_number} (crate {crate}, slot {slot})'


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


def _board_selector_candidates(board):
    candidates = set()

    get_string_id = getattr(board, 'get_string_id', None)
    if callable(get_string_id):
        candidates.add(get_string_id().lower())

    hostname = getattr(board, 'hostname', None)
    if hostname:
        candidates.add(str(hostname).strip().lower())

    part_number = str(getattr(board, 'part_number', '') or '').strip().lower()
    serial = getattr(board, 'serial', None)
    if serial is not None:
        serial_text = str(serial).strip().lower()
        serial_short = serial_text.lstrip('0') or serial_text
        candidates.update({serial_text, serial_short})
        if part_number:
            candidates.update({
                f'{part_number} {serial_text}',
                f'{part_number} {serial_short}',
                f'{part_number}_sn{serial_text}',
                f'{part_number}_sn{serial_short}',
            })
        if part_number == 'mgk7mb':
            candidates.update({
                normalize_hwm(serial_text).lower(),
                normalize_hwm(serial_short).lower(),
            })

    return {candidate for candidate in candidates if candidate}


def resolve_monitor_board(fpgas, selector=None):
    all_boards = list(fpgas.ib)
    ice_boards = [board for board in all_boards if getattr(board, 'part_number', None) == 'MGK7MB']

    if selector in (None, '', False):
        if not ice_boards:
            raise RuntimeError('No ICE boards are present in the FPGA array.')
        if len(ice_boards) > 1:
            raise RuntimeError('Multiple ICE boards are present; fpga.live_monitor.iceboard is required.')
        return ice_boards[0]

    if isinstance(selector, (tuple, list, dict)):
        matches = list(fpgas.get_iceboards([selector]))
    elif isinstance(selector, str):
        normalized = selector.strip().lower()
        matches = [board for board in all_boards if normalized in _board_selector_candidates(board)]
    else:
        raise TypeError('fpga.live_monitor.iceboard must be a string, tuple/list or dict selector.')

    unique_matches = []
    for board in matches:
        if board not in unique_matches:
            unique_matches.append(board)

    if not unique_matches:
        raise RuntimeError(f'Live monitor board selector {selector!r} did not match any FPGA board.')
    if len(unique_matches) > 1:
        board_labels = ', '.join(describe_board(board) for board in unique_matches)
        raise RuntimeError(f'Live monitor board selector {selector!r} matched multiple boards: {board_labels}')

    return unique_matches[0]


def resolve_monitor_boards(fpgas):
    boards = [board for board in fpgas.ib if getattr(board, 'part_number', None) == 'MGK7MB']
    boards.sort(key=lambda board: board.get_id(default_crate=0, default_slot=0))
    return boards


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
        state = LatestCaptureState(INPUT_NAMES)
        config = build_connection_config(iceboard_id=iceboard_id, mode=mode)
        fpga_array = FPGAArray(**config)
        board = fpga_array.ib[0]
        board.set_adc_delays()
        board.start_data_capture(period=capture_period_s, source='adc')

        for channel in board.chan:
            channel.SCALER.STATS_FRAME_COUNT = 390625

        receiver = board.get_data_receiver(verbose=0)
        stop_event = threading.Event()
        acquisition_thread = AcquisitionThread(
            reader=receiver.read_raw_frames,
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


class RawAcqAdcSnapshotPoller:
    def __init__(self, board_targets, state, poll_interval_ms=DEFAULT_POLL_INTERVAL_MS):
        self.board_targets = list(board_targets)
        self.state = state
        self.poll_interval_s = max(float(poll_interval_ms) / 1000.0, 0.05)
        self.tasks = []

    async def start(self):
        if self.tasks:
            return

        for target in self.board_targets:
            task = asyncio.create_task(
                self.run_target(target),
                name=f"fpga-master-adc-monitor-poller-{target['key']}",
            )
            self.tasks.append(task)

    async def stop(self):
        if not self.tasks:
            return

        for task in self.tasks:
            task.cancel()

        for task in self.tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

        self.tasks = []

    async def run_target(self, target):
        board = target['board']
        crate, slot = board.get_id(default_crate=0, default_slot=0)
        while True:
            try:
                snapshot = await target['raw_acq_client'].get_adc_snapshot(crate=crate, slot=slot)
                if snapshot.get('has_data'):
                    self.state.update(
                        board_key=target['key'],
                        timestamp=snapshot['timestamp'],
                        data=snapshot['data'],
                        counts=snapshot['counts'],
                        label=target['label'],
                    )
                elif snapshot.get('error'):
                    self.state.set_error(target['key'], snapshot['error'], label=target['label'])
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.state.set_error(target['key'], exc, label=target['label'])

            await asyncio.sleep(self.poll_interval_s)


class FpgaMasterAdcMonitor:
    def __init__(self, board_targets, host=DEFAULT_HOST, port=DEFAULT_PORT, poll_interval_ms=DEFAULT_POLL_INTERVAL_MS):
        if not board_targets:
            raise ValueError('At least one board target is required for the fpga_master ADC monitor.')

        self.board_targets = list(board_targets)
        self.state = MultiBoardCaptureState(INPUT_NAMES)
        for target in self.board_targets:
            self.state.register_board(target['key'], target['label'])

        self.poller = RawAcqAdcSnapshotPoller(
            board_targets=self.board_targets,
            state=self.state,
            poll_interval_ms=poll_interval_ms,
        )
        board_count = len(self.board_targets)
        description = (
            f'Live 16-channel ADC waveforms for {board_count} ICE board'
            f"{'s' if board_count != 1 else ''} selected by fpga_master. "
            'Use the board selector to switch between boards without starting another monitor.'
        )
        self.server = MonitorServer(
            state=self.state,
            index_html=build_adc_monitor_html(
                poll_interval_ms=poll_interval_ms,
                description=description,
            ),
            host=host,
            port=port,
        )

    async def start(self):
        url = await self.server.start()
        try:
            await self.poller.start()
        except Exception:
            await self.server.stop()
            raise
        return url

    async def stop(self):
        await self.poller.stop()
        await self.server.stop()


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
