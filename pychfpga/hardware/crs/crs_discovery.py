"""CRS network discovery through U-Boot and tcpipe beacons."""

import asyncio
import logging
import socket
import threading
import time
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

from pychfpga.hardware import Crate, Motherboard


logger = logging.getLogger(__name__)

DEFAULT_DISCOVERY_PORT = 9875
DEFAULT_BIND = "0.0.0.0"
DEFAULT_UBOOT_CMD = (
    "setenv autostart yes; "
    "wget {url} ${loadaddr} && "
    "bootelf -p ${loadaddr}"
)


class CRSBeacon:
    def __init__(self, data, address=None):
        """Parse a FWREQ or TCPIPE beacon.

        The base protocol is four whitespace-separated fields:
        ``KIND version identifier serial``. Extra metadata is accepted as
        ``key=value`` tokens using the same TXT field names as mDNS where possible.
        """
        parts = data.decode("utf-8").strip().split()
        if len(parts) < 4 or parts[0] not in ("FWREQ", "TCPIPE"):
            raise ValueError("not a CRS discovery beacon")
        fields = {}
        for token in parts[4:]:
            if "=" not in token:
                continue
            key, value = token.split("=", 1)
            fields[key.strip().lower().replace("_", "-")] = value.strip()
        self.kind = parts[0]
        self.version = parts[1]
        self.identifier = parts[2]
        self.serial = parts[3]
        self.fields = fields
        self.address = address
        self.board_part = self.get_any_field(("motherboard-part", "board-part", "model")) or ('CRS' if self.kind in ("FWREQ", "TCPIPE") else None) # patch until the beacon includes the board part number
        self.board_serial = self.get_any_field(("motherboard-serial", "serial")) or self.serial
        self.crate_part = self.get_any_field(("backplane-part", "crate-part"))
        self.crate_serial = self.get_any_field(("backplane-serial", "crate-serial"))
        self.crate_number = _to_int(self.get_any_field(("backplane-number", "crate-number")))
        self.slot = _to_int(self.get_any_field(("backplane-slot", "crate-slot", "slot")))

    def get_any_field(self, names):
        for name in names:
            if (value := self.fields.get(name)) is not None:
                return value
        return None


async def discover_crs(boards=None, crates=None, timeout=2, **kwargs):
    """Discover CRS boards and crates from UBOOT/TCPIPE UDP beacons."""
    if kwargs:
        logger.info(f"discover_crs  keyword arguments {kwargs} will be ignored")
    await CRSDiscovery(board_targets=boards or [], crate_targets=crates or [], timeout=timeout).run()

async def set_platform_firmware(boards=None, required_version=None, firmware_pathname=None, **kwargs):
    """Load tcpipe firmware on specified CRS boards
    
    Parameters:
        boards (list): List of boards to load the platform firmware on.
        required_version (str): Required version of the platform firmware.
        firmware_pathname (str): Path to the platform firmware ELF file.

        **kwargs: Additional keyword arguments (ignored).
    Returns:
        None
    """
    if not boards:
        return # nothing to discover
    if not firmware_pathname:
        raise ValueError("firmware_pathname is required")
    if kwargs:
        logger.info(f"{self!r}: set_platform_firmware  keyword arguments {kwargs} will be ignored")
    await CRSArmFirmwareUploader(boards=boards or [], required_version=required_version, firmware_pathname=firmware_pathname).run()

def _to_int(value):
    """Tries to convert a value to an integer if possible.
    
    Parameters:
        value (str): The value to convert.
    Returns:
        int: The integer value, or the original value if it is not a valid integer.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return value

def _match_token(target, value, match_integers=False):
    """Check if a value matches a target.
    
    Parameters:
        target (str): Target token. Can be "*", in which case the value is always matched.
        value (str): Value token. Can be None, in which case the target is not matched.
        match_integers (bool): Whether to do integer matching in addition to case-insensitive string matching.
    Returns:
        bool: True if the target matches the value, False otherwise.
    """
    if target == "*":
        return True
    if value is None:
        return False
    if str(target).upper() == str(value).upper():
        return True
    if match_integers:
        return _to_int(target) == _to_int(value)
    return False


class CRSDiscovery:
    discovery_port = DEFAULT_DISCOVERY_PORT
    bind = DEFAULT_BIND
    uboot_cmd = DEFAULT_UBOOT_CMD

    def __init__(
            self,
            board_targets,
            crate_targets,
            timeout):

        mb_classes = ()
        self.board_targets = _normalize_targets(board_targets)
        self.crate_targets = _normalize_targets(crate_targets)
        self.timeout = timeout
        self.discovered_serials = set()
        self.discovered_crates = []
        self.seen_serials = set()


    async def run(self):
        loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.bind((self.bind, self.discovery_port))
            sock.setblocking(False)

            deadline = self._initial_deadline()
            while True:
                if self._complete():
                    break
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    break
                wait_time = min(remaining, 0.5) if remaining is not None else 0.5
                try:
                    data, addr = await asyncio.wait_for(loop.sock_recvfrom(sock, 4096), wait_time)
                except asyncio.TimeoutError:
                    continue
                try:
                    beacon = CRSBeacon(data, address=addr[0])
                except (UnicodeDecodeError, ValueError):
                    logger.debug(f"Ignoring non-CRS discovery packet from {addr[0]}:{addr[1]}")
                    continue
                self._process_beacon(beacon)
        finally:
            sock.close()
        self._raise_if_incomplete()

    def _process_beacon(self, beacon):
        """Process a CRS beacon.

        Returns the kind of the beacon that was processed, or None if the beacon was ignored.
        """
        if not self._matches_requested_targets(beacon):
            logger.debug(f"{self!r}: Ignoring {beacon.kind} beacon from serial {beacon.board_serial} (not in {self.board_targets} or {self.crate_targets})")
            return None

        self.seen_serials.add(beacon.board_serial)
        if beacon.kind == "FWREQ":
            self._update_hardware_map(beacon, tcpipe_version=None)
            self.discovered_serials.add(beacon.board_serial)
            self._record_discovered_crate(beacon)
            logger.info(f"{self!r}: {beacon.board_serial} discovered in U-Boot at {beacon.address}")
            return beacon.kind

        if beacon.kind == "TCPIPE":
            self._update_hardware_map(beacon, tcpipe_version=beacon.version)
            self.discovered_serials.add(beacon.board_serial)
            self._record_discovered_crate(beacon)
            logger.info(f"{self!r}: {beacon.board_serial} ready at {beacon.address} with tcpipe {beacon.version}")
            return beacon.kind
        return None

    def _initial_deadline(self):
        if self._has_explicit_targets() or self.timeout is None:
            return None
        return time.monotonic() + self.timeout

    def _matches_requested_targets(self, beacon):
        if not self.board_targets and not self.crate_targets:
            return True
        board_id = (beacon.board_part, beacon.board_serial)
        crate_id = (beacon.crate_part, beacon.crate_serial)
        return (
            any(_target_matches(target, board_id) for target in self.board_targets)
            or any(_target_matches(target, crate_id) for target in self.crate_targets)
        )

    def _complete(self):
        if not self._has_explicit_targets():
            return False
        return not self._missing_explicit_board_targets() and not self._missing_explicit_crate_targets()

    def _has_explicit_targets(self):
        return any(target[0] != "*" and target[1] != "*" for target in self.board_targets + self.crate_targets)

    def _missing_explicit_board_targets(self):
        explicit_board_targets = [
            (model, serial) for model, serial in self.board_targets
            if model != "*" and serial != "*"]
        return [
            (model, serial) for model, serial  in explicit_board_targets
            if serial not in self.discovered_serials]

    def _missing_explicit_crate_targets(self):
        explicit_crate_targets = [
            target for target in self.crate_targets
            if target[0] != "*" and target[1] != "*"
        ]
        return [
            target for target in explicit_crate_targets
            if not any(_target_matches(target, discovered) for discovered in self.discovered_crates)
        ]

    def _record_discovered_crate(self, beacon):
        if beacon.crate_part and beacon.crate_serial:
            crate_id = (beacon.crate_part, beacon.crate_serial)
            if crate_id not in self.discovered_crates:
                self.discovered_crates.append(crate_id)

    def _raise_if_incomplete(self):
        missing = self._missing_explicit_board_targets() + self._missing_explicit_crate_targets()
        if not missing:
            return
        raise RuntimeError(
            "CRS discovery is incomplete: "
            f"missing={missing}, fwreq_seen={sorted(self.seen_serials)}, "
            f"discovered={sorted(self.discovered_serials)}")

    def _update_hardware_map(self, beacon, tcpipe_version):
        crate = self._update_crate(beacon)
        board_part = beacon.board_part
        board_cls = Motherboard.get_class_by_ipmi_part_number(board_part.upper())
        mb = Motherboard.get_unique_instance(
            new_class=board_cls,
            serial=beacon.board_serial,
            hostname=beacon.address)
        mb.tcpipe_version = tcpipe_version
        if crate is not None or beacon.slot is not None:
            mb.update_instance(slot=beacon.slot, crate=crate)
        return mb

    def _update_crate(self, beacon):
        if not (beacon.crate_part or beacon.crate_serial or beacon.crate_number is not None):
            return None
        crate_cls = None
        if beacon.crate_part:
            crate_cls = Crate.get_class_by_ipmi_part_number(beacon.crate_part)
            if crate_cls is None:
                logger.warning(f"{self!r}: Ignoring crate with unsupported part number {beacon.crate_part}")
                return None
        return Crate.get_unique_instance(
            new_class=crate_cls,
            serial=beacon.crate_serial,
            crate_number=beacon.crate_number)



class CRSArmFirmwareUploader:
    """Upload the platform firmware to the specified boards."""
    discovery_port = DEFAULT_DISCOVERY_PORT
    bind = DEFAULT_BIND
    uboot_cmd = DEFAULT_UBOOT_CMD

    def __init__(self, boards, required_version, firmware_pathname=None):
        self.required_tcpipe_version = required_version
        self.boards = list(boards)
        self.firmware_pathname = firmware_pathname
        self.target_serials = set()
        self.ready_serials = set()
        self._http_server = None

    async def run(self):


        # Find the boards that are not running the expected tcpipe version
        self.target_serials = {str(board.serial) for board in self.boards if board.tcpipe_version != self.required_tcpipe_version}
        if not self.target_serials:
            return

        loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.bind((self.bind, self.discovery_port))
            sock.setblocking(False)
            while self.target_serials - self.ready_serials:
                try:
                    data, addr = await loop.sock_recvfrom(sock, 4096)
                except asyncio.CancelledError:
                    raise
                try:
                    beacon = CRSBeacon(data, address=addr[0])
                except (UnicodeDecodeError, ValueError):
                    logger.debug(f"Ignoring non-CRS discovery packet from {addr[0]}:{addr[1]}")
                    continue
                self._process_beacon(sock, beacon, addr)
        finally:
            sock.close()
            if self._http_server is not None:
                self._http_server.stop()

 
    def _process_beacon(self, sock, beacon, addr):
        if not any(_match_token(target_serial, beacon.board_serial, match_integers=True) for target_serial in self.target_serials):
            logger.debug(f"{self!r}: Ignoring {beacon.kind} beacon from serial {beacon.board_serial} (not in {self.target_serials})")
            return
        if beacon.kind == "FWREQ":
            command = self._get_firmware_upload_command(beacon)
            sock.sendto(command.encode("utf-8"), addr)
            logger.info(f"{self!r}: Sent TCPipe firmware {self.firmware_pathname} to {beacon.board_part}_SN{beacon.board_serial} @ {addr[0]} ")
            return
        if beacon.kind == "TCPIPE":
            expected = self.required_tcpipe_version
            if expected is not None and beacon.version != expected:
                raise RuntimeError(
                    f"CRS {beacon.board_serial} at {beacon.address} is running "
                    f"tcpipe {beacon.version}; expected {expected}")
            self._update_board(beacon)
            self.ready_serials.add(self._target_serial_for(beacon.board_serial))

    def _target_serial_for(self, serial):
        for target in self.target_serials:
            if _match_token(target, serial, match_integers=True):
                return target
        return serial

    def _update_board(self, beacon):
        board = next(
            board for board in self.boards
            if _match_token(str(board.serial), beacon.board_serial, match_integers=True))
        board.hostname = beacon.address
        board.tcpipe_version = beacon.version

    def _get_firmware_upload_command(self, beacon):
        # Check the firmware pathname is valid
        if self.firmware_pathname is None:
            raise ValueError("firmware_pathname is required")
        firmware = Path(self.firmware_pathname)
        if not firmware.is_file():
            raise FileNotFoundError(f"Platform firmware ELF not found: {firmware}")

        # Make sure the HTTP firmware server is running 
        http_server = self._ensure_http_server(firmware)


        host = self._iface_address_for(beacon.address)
        # Send the command to fetch the execute the firmware
        url = f"http://{host}:{http_server.port}/{firmware.name}"
        cmd = self.uboot_cmd.replace("{url}", url)
        return f"FWRESP {beacon.version}\n\n{cmd}"

    def _ensure_http_server(self, firmware):
        """Ensure the HTTP firmware server is running and return it.
        
        Parameters:
            firmware (Path): The firmware file to serve.
        Returns:
            _FirmwareHTTPServer: The HTTP firmware server.
        """
        if self._http_server is None:
            self._http_server = _FirmwareHTTPServer(firmware.parent, firmware.name, self.bind)
            self._http_server.start()
        return self._http_server

    def _iface_address_for(self, remote_host):
        """Return the IP address of the interface that is used to connect to the remote host."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect((remote_host, 1))
            return sock.getsockname()[0]
        finally:
            sock.close()


class _FirmwareHTTPServer:
    def __init__(self, firmware_dir, firmware_name, bind, on_download=None):
        self._stop_event = threading.Event()
        self._ready = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            args=(Path(firmware_dir), firmware_name, bind, on_download),
            daemon=True)
        self.port = None

    def start(self):
        self._thread.start()
        self._ready.wait()

    def stop(self):
        self._stop_event.set()
        self._thread.join(timeout=2)

    def _run(self, firmware_dir, firmware_name, bind, on_download):
        class Handler(SimpleHTTPRequestHandler):
            def log_message(self, fmt, *args):
                logger.debug("CRS firmware HTTP %s %s", self.address_string(), fmt % args)

            def do_GET(self):
                super().do_GET()
                if on_download is not None and self.path.lstrip("/") == firmware_name:
                    on_download(self.client_address[0])

        httpd = HTTPServer(
            (bind, 0),
            lambda *args: Handler(*args, directory=str(firmware_dir)))
        self.port = int(httpd.server_address[1])
        self._ready.set()
        httpd.timeout = 1.0
        try:
            while not self._stop_event.is_set():
                httpd.handle_request()
        finally:
            httpd.server_close()


def _normalize_targets(targets):
    """Normalize a list of targets to a list of tuples of (model, serial).

     
    Parameters:
        targets (list): List of targets to normalize.

    Returns:
        list: List of tuples of (model, serial), where model can be "*", and serial is forced into a string and can also be "*".
    """
    if targets == "*":
        return [("*", "*")]
    return [(model or "*", str(serial)) for model, serial in (targets or [])]


def _target_matches(target, value):
    """Check if a target matches a value.
    
    Parameters:
        target (tuple): Tuple of (model, serial).
        value (tuple): Tuple of (model, serial).
    Returns:
        bool: True if the target matches the value, False otherwise.
    """
    target_model, target_serial = target
    value_model, value_serial = value
    if value_serial is None:
        return False
    return (
        _match_token(target_model, value_model)
        and _match_token(target_serial, value_serial, match_integers=True)
    )






