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
