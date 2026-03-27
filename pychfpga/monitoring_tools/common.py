import os
import socket
import threading
import time

import numpy as np


def env_flag(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.lower() in {'1', 'true', 'yes', 'on'}


def env_float(name, default):
    value = os.environ.get(name)
    if value is None:
        return default
    return float(value)


def env_int(name, default):
    value = os.environ.get(name)
    if value is None:
        return default
    return int(value)


def resolve_access_host(bind_host):
    if bind_host not in {'0.0.0.0', '::'}:
        return bind_host

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(('8.8.8.8', 80))
            return probe.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return '127.0.0.1'


class StdoutSilencer:
    def __init__(self, enabled):
        self.enabled = enabled
        self._saved_stdout_fd = None

    def __enter__(self):
        if not self.enabled:
            return self

        self._saved_stdout_fd = os.dup(1)
        devnull_fd = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull_fd, 1)
        os.close(devnull_fd)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._saved_stdout_fd is None:
            return False

        os.dup2(self._saved_stdout_fd, 1)
        os.close(self._saved_stdout_fd)
        self._saved_stdout_fd = None
        return False


class LatestCaptureState:
    def __init__(self, input_names):
        self.input_names = list(input_names)
        self.lock = threading.Lock()
        self.timestamp = None
        self.updated_at = None
        self.data = None
        self.counts = None
        self.error = None

    def update(self, timestamp, data, counts):
        array_data = np.asarray(data, dtype=np.int8)
        array_counts = np.asarray(counts, dtype=np.int64)
        with self.lock:
            self.timestamp = int(timestamp)
            self.updated_at = time.time()
            self.data = array_data
            self.counts = array_counts
            self.error = None

    def clear_error(self):
        with self.lock:
            self.error = None

    def set_error(self, exc):
        message = exc if isinstance(exc, str) else f'{exc.__class__.__name__}: {exc}'
        with self.lock:
            self.error = message

    def serialize(self, board_key=None):
        with self.lock:
            timestamp = self.timestamp
            updated_at = self.updated_at
            data = self.data
            counts = self.counts
            error = self.error

        return {
            'input_names': self.input_names,
            'boards': [],
            'selected_board': board_key,
            'board_label': None,
            'has_data': data is not None,
            'timestamp': timestamp,
            'updated_at': updated_at,
            'age_s': None if updated_at is None else time.time() - updated_at,
            'counts': [] if counts is None else [int(value) for value in counts.tolist()],
            'data': [] if data is None else data.tolist(),
            'error': error,
        }


class MultiBoardCaptureState:
    def __init__(self, input_names):
        self.input_names = list(input_names)
        self.lock = threading.Lock()
        self.board_order = []
        self.board_entries = {}

    def _ensure_entry(self, board_key, label=None):
        board_key = str(board_key)
        entry = self.board_entries.get(board_key)
        if entry is None:
            entry = dict(
                key=board_key,
                label=label or board_key,
                timestamp=None,
                updated_at=None,
                data=None,
                counts=None,
                error=None,
            )
            self.board_entries[board_key] = entry
            self.board_order.append(board_key)
        elif label:
            entry['label'] = label
        return entry

    def register_board(self, board_key, label):
        with self.lock:
            self._ensure_entry(board_key, label=label)

    def update(self, board_key, timestamp, data, counts, label=None):
        array_data = np.asarray(data, dtype=np.int8)
        array_counts = np.asarray(counts, dtype=np.int64)
        with self.lock:
            entry = self._ensure_entry(board_key, label=label)
            entry['timestamp'] = int(timestamp)
            entry['updated_at'] = time.time()
            entry['data'] = array_data
            entry['counts'] = array_counts
            entry['error'] = None

    def clear_error(self, board_key):
        with self.lock:
            entry = self._ensure_entry(board_key)
            entry['error'] = None

    def set_error(self, board_key, exc, label=None):
        message = exc if isinstance(exc, str) else f'{exc.__class__.__name__}: {exc}'
        with self.lock:
            entry = self._ensure_entry(board_key, label=label)
            entry['error'] = message

    def serialize(self, board_key=None):
        with self.lock:
            board_order = list(self.board_order)
            entries = {
                key: dict(
                    key=value['key'],
                    label=value['label'],
                    timestamp=value['timestamp'],
                    updated_at=value['updated_at'],
                    data=value['data'],
                    counts=value['counts'],
                    error=value['error'],
                )
                for key, value in self.board_entries.items()
            }

        if board_key is not None:
            board_key = str(board_key)

        selected_key = None
        if board_key in entries:
            selected_key = board_key
        else:
            for candidate in board_order:
                if entries[candidate]['data'] is not None:
                    selected_key = candidate
                    break
            if selected_key is None and board_order:
                selected_key = board_order[0]

        selected_entry = entries.get(selected_key)
        selected_updated_at = None if selected_entry is None else selected_entry['updated_at']
        selected_counts = None if selected_entry is None else selected_entry['counts']
        selected_data = None if selected_entry is None else selected_entry['data']
        selected_timestamp = None if selected_entry is None else selected_entry['timestamp']
        selected_error = None if selected_entry is None else selected_entry['error']
        selected_label = None if selected_entry is None else selected_entry['label']

        boards = []
        now = time.time()
        for key in board_order:
            entry = entries[key]
            updated_at = entry['updated_at']
            boards.append({
                'key': key,
                'label': entry['label'],
                'has_data': entry['data'] is not None,
                'timestamp': entry['timestamp'],
                'age_s': None if updated_at is None else now - updated_at,
                'error': entry['error'],
            })

        return {
            'input_names': self.input_names,
            'boards': boards,
            'selected_board': selected_key,
            'board_label': selected_label,
            'has_data': selected_data is not None,
            'timestamp': selected_timestamp,
            'updated_at': selected_updated_at,
            'age_s': None if selected_updated_at is None else now - selected_updated_at,
            'counts': [] if selected_counts is None else [int(value) for value in selected_counts.tolist()],
            'data': [] if selected_data is None else selected_data.tolist(),
            'error': selected_error,
        }


class AcquisitionThread(threading.Thread):
    def __init__(self, reader, state, stop_event, retry_delay_s=1.0, name='monitor-acquisition'):
        super().__init__(name=name, daemon=True)
        self.reader = reader
        self.state = state
        self.stop_event = stop_event
        self.retry_delay_s = retry_delay_s

    def run(self):
        while not self.stop_event.is_set():
            try:
                timestamp, data, counts = self.reader()
            except Exception as exc:
                if self.stop_event.is_set():
                    break
                self.state.set_error(exc)
                time.sleep(self.retry_delay_s)
                continue

            self.state.update(timestamp, data, counts)
