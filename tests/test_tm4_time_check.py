import datetime
import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'deployment_scripts'))

import tm4_time_check  # noqa: E402


class FakeTM4:
    """Stand-in for the TM-4 control socket.

    `broadcast` lines are sent on their own; `replies` answers `#13,NN` requests (polling
    mode). Records everything the script sends.
    """

    def __init__(self, broadcast=(), replies=None):
        self.pending = [line.encode() for line in broadcast]
        self.replies = replies or {}
        self.sent = []

    def __call__(self, address, timeout):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def settimeout(self, timeout):
        pass

    def recv(self, size):
        if not self.pending:
            raise socket.timeout()
        return self.pending.pop(0)

    def sendall(self, data):
        self.sent.append(data.decode())
        number = data.decode().strip().split(',')[1]
        if number in self.replies:
            self.pending.append(self.replies[number].encode())


def now_message():
    t = datetime.datetime.now(datetime.timezone.utc)
    return f'#51,{t:%m%d%Y},{t:%H%M%S}\r\n'


def test_valid_time_in_broadcast_mode_passes_without_sending_anything():
    tm4 = FakeTM4(broadcast=[now_message(), '#61,1\r\n#65,0,0,0\r\n'])

    result = tm4_time_check.check(listen_time=0.1, connect=tm4)
    tm4_time_check.evaluate(result)

    assert tm4.sent == []
    assert result['time_valid'] is True
    assert abs(result['offset']) <= 1


def test_polling_mode_requests_only_with_message_13():
    tm4 = FakeTM4(replies={'51': now_message(), '61': '#61,1\r\n', '65': '#65,0,0,0\r\n'})

    result = tm4_time_check.check(listen_time=0.1, connect=tm4)

    assert tm4.sent == ['#13,51\r\n', '#13,61\r\n', '#13,65\r\n']
    tm4_time_check.evaluate(result)


def test_free_running_2035_date_fails_with_the_offset():
    # crate 012, 2026-11-17: the TM-4 ran 3141 d 11:48:14 ahead without an antenna
    tm4 = FakeTM4(broadcast=['#51,06252035,052525\r\n', '#61,0\r\n', '#65,1,1,0\r\n'])

    result = tm4_time_check.check(listen_time=0.1, connect=tm4)
    with pytest.raises(RuntimeError) as e:
        tm4_time_check.evaluate(result)

    assert 'Time Valid' in str(e.value)
    assert 'antenna' in str(e.value)
    assert '+31' in str(e.value)  # off by +3100-odd days
    assert result['tm4_time'].year == 2035


def test_no_reply_fails():
    with pytest.raises(RuntimeError, match='did not report its date and time'):
        tm4_time_check.check(listen_time=0.1, request_timeout=0.1, connect=FakeTM4())


def test_connection_error_fails():
    def refuse(address, timeout):
        raise ConnectionRefusedError('refused')

    with pytest.raises(RuntimeError, match='Cannot connect'):
        tm4_time_check.check(connect=refuse)
