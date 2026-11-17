#!/usr/bin/env python3
"""Check that the site TM-4 GPS clock outputs the correct UTC time.

The TM-4 (Spectrum Instruments) feeds the IRIG-B time that the crate's boards use for the
array-wide SYNC and for the `irigb_time` stamps in the correlator data. It takes its time only
from GPS: there is no command to set its date or time by hand (the user time bias, message #06,
is limited to +-99,999 ns). Without an antenna it free-runs from whatever date it holds, which
gave a 2035 IRIG-B date on crate 012 from 2026-11-16.

This script only reads. It connects to the TM-4 control port (through the NPort serial server),
collects the date/time (#51), the timing status (#61) and the alarms (#65) from the broadcast
stream, and requests any missing message with #13 (answered only in polling mode). It never
sends a command that changes the unit's settings.

Exits with an error if the time is not valid, differs from this host's UTC by more than
--max-offset seconds, or the unit does not answer. This host's clock is assumed correct (NTP).
"""

import argparse
import datetime
import socket
import sys
import time

DEFAULT_HOST = '10.10.8.1'  # NPort serial server in front of the TM-4 (commit 9548d880)
DEFAULT_PORT = 1001
MAX_OFFSET = 1.0  # s; the TM-4 reports whole seconds
MESSAGES = ('51', '61', '65')  # date/time, timing status, alarm status


def read_messages(sock, wanted, deadline, messages):
    """Read `#NN,...` lines from `sock` into `messages` until all of `wanted` are seen or `deadline`."""
    buffer = ''
    while not all(m in messages for m in wanted) and time.time() < deadline:
        sock.settimeout(max(deadline - time.time(), 0.01))
        try:
            data = sock.recv(4096)
        except socket.timeout:
            break
        if not data:
            break
        buffer += data.decode('ascii', errors='replace')
        *lines, buffer = buffer.replace('\r', '\n').split('\n')
        for line in lines:
            line = line.strip()
            if line.startswith('#') and ',' in line:
                number, *fields = line[1:].split(',')
                messages[number] = (fields, time.time())
    return messages


def check(host=DEFAULT_HOST, port=DEFAULT_PORT, listen_time=2.0, request_timeout=3.0,
          connect=socket.create_connection):
    """Read the TM-4 time and status and compare the time with this host's UTC.

    Returns a dict with the TM-4 UTC time, this host's UTC time, the offset in seconds
    (TM-4 minus host), the Time Valid flag and the alarms. Raises RuntimeError if the TM-4
    cannot be reached or does not report its date and time.
    """
    try:
        sock = connect((host, port), timeout=request_timeout)
    except OSError as e:
        raise RuntimeError(f'Cannot connect to the TM-4 at {host}:{port}: {e}')
    with sock:
        # Broadcast mode: the messages arrive on their own
        messages = read_messages(sock, MESSAGES, time.time() + listen_time, {})
        # Polling mode: request what is missing (#13 changes no setting)
        for number in MESSAGES:
            if number not in messages:
                sock.sendall(f'#13,{number}\r\n'.encode('ascii'))
                read_messages(sock, (number,), time.time() + request_timeout, messages)

    if '51' not in messages:
        raise RuntimeError(f'The TM-4 at {host}:{port} did not report its date and time (#51).')
    (date, hms), received_at = messages['51']
    tm4_time = datetime.datetime(int(date[4:8]), int(date[0:2]), int(date[2:4]),
                                 int(hms[0:2]), int(hms[2:4]), int(hms[4:6]),
                                 tzinfo=datetime.timezone.utc)
    host_time = datetime.datetime.fromtimestamp(received_at, datetime.timezone.utc)
    time_valid = bool(int(messages['61'][0][0])) if '61' in messages else None
    alarms = None
    if '65' in messages:
        coast, antenna, clock = (bool(int(v)) for v in messages['65'][0][:3])
        alarms = dict(coast=coast, antenna=antenna, clock_10MHz=clock)
    return dict(tm4_time=tm4_time, host_time=host_time,
                offset=(tm4_time - host_time).total_seconds(),
                time_valid=time_valid, alarms=alarms)


def format_offset(seconds):
    sign = '-' if seconds < 0 else '+'
    seconds = abs(round(seconds))
    days, rest = divmod(seconds, 86400)
    return f'{sign}{days} d {rest // 3600:02d}:{rest % 3600 // 60:02d}:{rest % 60:02d} ({sign}{seconds} s)'


def evaluate(result, max_offset=MAX_OFFSET):
    """Raise RuntimeError describing the problem if the TM-4 time cannot be trusted."""
    problems = []
    if result['time_valid'] is False:
        problems.append('the TM-4 does not have Time Valid (no GPS time)')
    elif result['time_valid'] is None:
        problems.append('the TM-4 did not report its timing status (#61)')
    if abs(result['offset']) > max_offset:
        problems.append(f"its time is off by {format_offset(result['offset'])}")
    if result['alarms'] and result['alarms']['antenna']:
        problems.append('the antenna alarm is set')
    if problems:
        raise RuntimeError(
            'TM-4 time is not usable: ' + '; '.join(problems) + '. '
            'The TM-4 cannot be set by hand: connect the GPS antenna and wait for Time Valid. '
            'Until then the IRIG-B time (and irigb_time in correlator files) is wrong; '
            'correct it afterwards from the host ctime.')


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--host', default=DEFAULT_HOST, help=f'TM-4 control port host (default {DEFAULT_HOST})')
    parser.add_argument('--port', type=int, default=DEFAULT_PORT, help=f'TCP port (default {DEFAULT_PORT})')
    parser.add_argument('--max-offset', type=float, default=MAX_OFFSET,
                        help=f'Largest accepted offset from this host, in seconds (default {MAX_OFFSET})')
    args = parser.parse_args()
    try:
        result = check(args.host, args.port)
        print(f"TM-4 UTC:  {result['tm4_time']:%Y-%m-%d %H:%M:%S}")
        print(f"Host UTC:  {result['host_time']:%Y-%m-%d %H:%M:%S}")
        print(f"Offset:    {format_offset(result['offset'])}")
        print(f"Time Valid: {result['time_valid']}")
        print(f"Alarms:    {result['alarms']}")
        evaluate(result, args.max_offset)
    except RuntimeError as e:
        sys.exit(str(e))
    print('TM-4 time OK')


if __name__ == '__main__':
    main()
