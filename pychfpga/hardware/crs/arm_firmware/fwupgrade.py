import os
import sys
import time
import socket
import hashlib
import threading
import logging
from functools import partial
from concurrent.futures import ThreadPoolExecutor
from xmodem import XMODEM

log = logging.getLogger(__name__)
BUF_SIZE = 1024
FWDISCO_PORT = 9875
IDLE_TIMEOUT = 10  # seconds with no new upgradeable board before shutdown
done = set()
seen = set()  # serials we've already logged a beacon for, to mute repeat spam
rejected = set()  # serials we've logged a skip reason for, to mute repeat spam

### ALL FILTERS HAVE BEEN REMOVED FOR THIS DEMO ###
# WHITELIST = {'0118', '0110'} # see lines ~266
# BLACKLIST = set()
# fwreq_version = 1
# bootloader = 'u-boot'
# bootloader_version = '0.2'

board_meta = {}
state_lock = threading.Lock()


def parse_semver(version_str:str) -> tuple[int, int]:
    """Parse a dotted version string into a comparable int tuple, e.g. "1.2.3" -> (1, 2, 3)."""
    return tuple(map(int, version_str.split('.')))

def xmodem_send(sock, filename) -> bool:
    def getc(size, timeout=1):
        sock.settimeout(timeout)
        try:
            return sock.recv(size) or None
        except socket.timeout:
            return None

    def putc(data, timeout=1):
        return sock.send(data)  # connected socket: peer is implicit

    modem = XMODEM(getc, putc, mode='xmodem1k') # packet_size = 1024
    with open(filename, 'rb') as stream:
        ret = modem.send(stream)

    return ret

def compare_output(output, expected) -> bool:
    """Return True if expected is set and appears as a substring of output."""
    return expected is not None and expected in output

def send_cmd(sock, cmd: str, timeout=0.1) -> str:
    """Send a command and return the reply, stripped of the echoed command."""
    sock.settimeout(timeout)
    sock.send((cmd +'\n').encode('utf-8'))
    reply = ''
    while True:
        try:
            reply += sock.recv(BUF_SIZE).decode()
        except socket.timeout:
            break
    return reply.replace(cmd, '', 1).strip()

def run_script(sock, script: list[dict]) -> bool:
    """Run each command in the script, checking output against its expected value.

    Logs the command, the board's response, and whether each step passed.
    Board identity is looked up by peer IP (board's netconsole address).
    """
    ip = sock.getpeername()[0]
    serial = board_meta[ip]['serial']
    n = len(script)

    passed = 0
    for i, line in enumerate(script, 1):
        cmd = line.get('cmd')
        expected = line.get('expect')
        timeout = line.get('timeout', 1)

        output = send_cmd(sock, cmd, timeout)

        # steps with no 'expect' (e.g. erase/write) are fire-and-forget: always pass
        if expected is None or compare_output(output, expected):
            log.info('[%s] step %d/%d PASS:\nCOMMAND:\n%s\nOUTPUT:\n%s', serial, i, n, cmd, output)
            passed += 1
        else:
            # surface what went wrong: command, what we wanted, what we got
            log.error(
                '[%s] step %d/%d FAIL: %s\n  expected: %r\n  actual:   %r',
                serial, i, n, cmd, expected, output,
            )
            return False

    return passed == n

def upgrade_firmware(sock, filename, md5) -> bool:
    """
    Send firmware over xmodem, then flash and reset the board. Returns True on success.
    sock must already be connected to the board's netconsole peer.
    """

    # drain stale console output (echo replies, 'ZynqMP>' prompt) before loadx,
    # so xmodem doesn't read it as handshake bytes
    sock.settimeout(0.5)
    while True:
        try:
            sock.recv(BUF_SIZE)
        except socket.timeout:
            break

    sock.send(b'loadx\n')  # the board waits to receive the payload
    try:
        ok = xmodem_send(sock, filename)
        if not ok:
            return False
        # flush loadx's post-transfer banner ('\x06## Total Size...ZynqMP>') so the
        # first script command reads its own reply, not this leftover
        sock.settimeout(0.5)
        while True:
            try:
                sock.recv(BUF_SIZE)
            except socket.timeout:
                break
        filesize = os.path.getsize(filename)
        erase_size = hex(-(-filesize//0x20000)*0x20000) # round up to nearest integer number of erase blocks
    except FileNotFoundError as e:
        log.error('%s', e)
        return False

    script = [
        {
            "cmd": "md5sum $loadaddr $filesize",
            "expect": md5,
            "timeout": 2
        },
        {
            "cmd": "sf probe 0:0",
            "expect": "erase size 128 KiB",
            "timeout": 1
        },
        {
            "cmd": "sf erase 0x0 " + erase_size,
            "expect": "Erased: OK",
            "timeout": 10
        },
        {
            "cmd": "sf write $loadaddr 0x0 $filesize",
            "expect": "Written: OK",
            "timeout": 10
        },
        {
            "cmd": "reset",
            "expect": "resetting",
            "timeout": 0.1
        }
    ]
    return run_script(sock, script)

def handle_crs(crs_ip, crs_port, filename, md5):
    """Reply to one board, connect with netconsole, and upgrade its firmware."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as thread_sock:
        thread_sock.settimeout(30)
        thread_sock.bind(('0.0.0.0', 0))
        ncinport = thread_sock.getsockname()[1]
        ncoutport = ncinport

        # connect to the board's netconsole peer: kernel filters out datagrams
        # from any other host and resolves the local source IP (ncip) for us,
        # so no separate probe socket is needed
        thread_sock.connect((crs_ip, ncinport))
        ncip = thread_sock.getsockname()[0]

        # netconsole will redirect nc ip and ports to where we tell it to;
        # the beacon ack goes to the board's FWREQ source port (crs_port)
        fwresponse = f'FWRESP/1\nncip:{ncip}\nncinport:{ncinport}\nncoutport:{ncoutport}\n\n'.encode('utf-8')
        thread_sock.sendto(fwresponse, (crs_ip, crs_port))
        return upgrade_firmware(thread_sock, filename, md5)

def log_worker_result(serial, future):
    """Done-callback: surface a crashed worker's traceback, else log the upgrade result.

    Runs once the worker finishes, so future.exception()/result() never block.
    An exception here means OUR code broke; a False result is an upgrade that
    failed (already detailed inline by script_runner).
    """
    exc = future.exception()
    if exc is not None:
        log.error('[%s] worker crashed: %r', serial, exc, exc_info=exc)
    else:
        log.info('[%s] %s', serial, 'upgraded' if future.result() else 'upgrade failed')

def parse_fwreq(data: bytes):
    """Parse an FWREQ datagram into (protocol version, header dict, body); raise ValueError if malformed."""

    text = data.decode('utf-8')
    head, _, body = text.partition('\n\n')
    lines = head.split('\n')

    status = lines[0]
    if not status.startswith('FWREQ/'):
        raise ValueError(f'not an FWREQ message: {status!r}')
    version = int(status.split('/', 1)[1])

    headers = {}
    for line in lines[1:]:
        if not line.strip():
            continue
        key, _, value = line.partition(':')
        headers[key.strip()] = value.strip()

    return version, headers, body

def main(filename='./boot.bin'):
    """Listen for CRS beacons and dispatch a worker to upgrade each eligible board."""

    try:
        with open(filename, 'rb') as f:
            md5 = hashlib.file_digest(f, 'md5').hexdigest()  # computed once, shared across all worker threads
    except FileNotFoundError:
        log.critical('Firmware file %r not found, exiting...', filename)
        sys.exit(1)

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        # short timeout so the loop ticks ~1/s even while handled/rejected boards
        # keep beaconing (a long timeout would never fire and we'd never exit)
        sock.settimeout(1)
        sock.bind(('0.0.0.0', FWDISCO_PORT))

        log.info('Listening on port %d for CRS boards...', FWDISCO_PORT)

        futures = []
        last_new = time.monotonic()  # reset only when we dispatch a real upgrade

        with ThreadPoolExecutor(max_workers=100) as pool:
            while True:
                # Exit once no NEW upgradeable board has appeared for IDLE_TIMEOUT
                # and every upgrade we started has finished. Checked every
                # iteration (not just on socket timeout) because handled/rejected
                # boards beacon faster than the recv timeout, so recvfrom never
                # actually times out -- the idle clock is wall-clock, not socket.
                if (time.monotonic() - last_new >= IDLE_TIMEOUT
                        and all(f.done() for f in futures)):
                    log.info('No new boards for %ds and all upgrades done, exiting...', IDLE_TIMEOUT)
                    break

                try:
                    data, (ip, port) = sock.recvfrom(BUF_SIZE)
                except socket.timeout:
                    continue  # no packet this tick; loop back to re-check idle

                try:
                    log.debug('%s', data.decode())
                    crs_fwreq_version, headers, _ = parse_fwreq(data)
                except (ValueError, UnicodeDecodeError) as e:
                    log.warning('Ignoring malformed request: %s', e)
                    continue

                crs_serial = headers.get('Serial')
                crs_bootloader = headers.get('Bootloader').strip('"')
                crs_bootloader_version = headers.get('Bootloader Version', '0.0').strip('"')

                # blacklisted boards beacon forever; log each serial's first beacon only
                if crs_serial not in seen:
                    seen.add(crs_serial)
                    # if crs_serial in BLACKLIST:
                    #     log.info('Ignoring blacklisted CRS board %s at %s:%d', crs_serial, ip, port)
                    # else:
                    #     log.info('Found a CRS board %s at %s:%d', crs_serial, ip, port)
                    log.info('Found a CRS board %s at %s:%d', crs_serial, ip, port)
                
                if (crs_serial in done
                    # or crs_serial in BLACKLIST
                    # or crs_serial not in WHITELIST
                    # or crs_fwreq_version != fwreq_version
                    ):
                    continue

                # log the reason once per serial, so a board that will never
                # qualify doesn't spam the same rejection on every beacon
                # if crs_bootloader != bootloader:
                #     if crs_serial not in rejected:
                #         rejected.add(crs_serial)
                #         log.info('[%s] skipped: bootloader %r != expected %r',
                #                  crs_serial, crs_bootloader, bootloader)
                #     continue

                # if parse_semver(crs_bootloader_version) >= parse_semver(bootloader_version):
                #     if crs_serial not in rejected:
                #         rejected.add(crs_serial)
                #         log.info('[%s] skipped: bootloader version %s already >= %s',
                #                  crs_serial, crs_bootloader_version, bootloader_version)
                #     continue

                done.add(crs_serial)
                last_new = time.monotonic()  # new upgrade started: restart idle clock
                with state_lock:
                    board_meta[ip] = {'serial': crs_serial, 'ip': ip}
                future = pool.submit(handle_crs, ip, port, filename, md5)
                future.add_done_callback(partial(log_worker_result, crs_serial))
                futures.append(future)


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='Upgrade firmware on CRS boards over the network.')
    parser.add_argument('firmware', nargs='?', default='./boot.bin',
                        help='path to the firmware file (default: ./boot.bin)')
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(message)s',
        datefmt='%H:%M:%S',
    )

    # The xmodem lib logs each board-echoed byte as an ERROR while waiting for the
    # handshake NAK/'C'; these are benign retries (a real failure returns False and
    # is reported by our own logging). Mute everything below CRITICAL.
    logging.getLogger('xmodem.XMODEM').setLevel(logging.CRITICAL)

    main(args.firmware)
