"""CRS FWREQ/TCPIPE beacon parsing and BEACONREQ (stdlib + ifaddr)."""

import ipaddress
import socket

import ifaddr

DEFAULT_DISCOVERY_PORT = 9875
DEFAULT_BIND = "0.0.0.0"
BEACON_REQUEST_PAYLOAD = b"BEACONREQ"

_broadcast_addresses = []


def _get_broadcast_addresses():
    """Broadcast addresses for all interfaces (cached)."""
    if _broadcast_addresses:
        return _broadcast_addresses

    seen = set()
    for adapter in ifaddr.get_adapters():
        for ip in adapter.ips:
            if not ip.is_IPv4:
                continue
            try:
                network = ipaddress.IPv4Interface(
                    f"{ip.ip}/{ip.network_prefix}",
                ).network
            except ValueError:
                continue
            if network.is_loopback:
                continue
            baddr = str(network.broadcast_address)
            if baddr not in seen:
                seen.add(baddr)
                _broadcast_addresses.append(baddr)
    return _broadcast_addresses


def request_beacon(ip_addr=None, port=DEFAULT_DISCOVERY_PORT):
    """Send BEACONREQ so CRS boards emit a discovery beacon immediately."""
    addrs = [ip_addr] if ip_addr is not None else _get_broadcast_addresses()

    for addr in addrs:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.sendto(BEACON_REQUEST_PAYLOAD, (addr, port))


def _normalize_header_key(key):
    return key.strip().lower().replace("_", "-").replace(" ", "-")


_PLACEHOLDER_FIELD_VALUES = frozenset({
    "", "unknown", "?", "none", "n/a", "null", "uninitialized",
})


def _usable_field(value):
    """Return a stripped field value, or None if it is empty or a placeholder."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in _PLACEHOLDER_FIELD_VALUES:
        return None
    if all(ord(c) >= 0x7F for c in text):
        return None
    return text


def parse_fwreq_datagram(data, kind="FWREQ"):
    """Parse a discovery datagram into version, headers, and body."""
    text = data.decode("utf-8")
    head, _, body = text.partition("\n\n")
    lines = head.split("\n")

    status = lines[0]
    if not status.startswith(f"{kind}/"):
        raise ValueError(f"not a {kind} message: {status!r}")
    version = int(status.split("/", 1)[1])

    headers = {}
    for line in lines[1:]:
        if not line.strip():
            continue
        key, _, value = line.partition(":")
        headers[key.strip()] = value.strip()

    return version, headers, body


def _to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


class CRSBeacon:
    def __init__(self, data, address=None):
        """Parse a structured FWREQ/1 or TCPIPE/1 beacon."""
        text = data.decode("utf-8")
        if text.startswith("FWREQ/"):
            self._parse_header_beacon(data, address, "FWREQ")
        elif text.startswith("TCPIPE/"):
            self._parse_header_beacon(data, address, "TCPIPE")
        else:
            raise ValueError("not a CRS discovery beacon")

    def _parse_header_beacon(self, data, address, kind):
        version, headers, _body = parse_fwreq_datagram(data, kind=kind)
        fields = {_normalize_header_key(k): v for k, v in headers.items()}

        manufacturer = headers.get("Manufacturer", "unknown")
        product = headers.get("Product", "unknown")
        revision = headers.get("Revision", "?")
        serial = headers.get("Serial", "unknown")
        bootloader_version = fields.get("bootloader-version")

        self.kind = kind
        self.version = (
            bootloader_version
            if kind == "TCPIPE" and bootloader_version
            else str(version)
        )
        self.identifier = f"{manufacturer}-{product}-rev{revision}"
        self.serial = serial
        self.fields = fields
        self.address = address
        self.board_part = product.upper()
        self.board_serial = serial
        self.backplane_part = self.get_any_field(("backplane-part", "backplane-model"))
        self.backplane_serial = self.get_any_field(("backplane-serial",))
        self.backplane_revision = self.get_any_field(("backplane-revision",))
        self.backplane_manufacturer = self.get_any_field(("backplane-manufacturer",))
        self.backplane_slot = _to_int(self.get_any_field(("backplane-slot",)))
        self.chassis_part = self.get_any_field(("chassis-part", "chassis-model"))
        self.chassis_serial = self.get_any_field(("chassis-serial",))
        self.chassis_manufacturer = self.get_any_field(("chassis-manufacturer",))
        self.chassis_revision = self.get_any_field(("chassis-revision",))
        self.crate_part = self.get_any_field(("crate-part", "crate-model"))
        self.crate_serial = self.get_any_field(("crate-serial",))
        self.crate_manufacturer = self.get_any_field(("crate-manufacturer",))
        self.crate_revision = self.get_any_field(("crate-revision",))
        if self.crate_part is None:
            self.crate_part = self.chassis_part
        if self.crate_serial is None:
            self.crate_serial = self.chassis_serial
        if self.crate_manufacturer is None:
            self.crate_manufacturer = self.chassis_manufacturer
        if self.crate_revision is None:
            self.crate_revision = self.chassis_revision
        self.crate_number = _to_int(self.get_any_field(("crate-number", "backplane-number")))
        self.crate_slot = _to_int(self.get_any_field(("crate-slot",)))
        if isinstance(self.crate_slot, int) and self.crate_slot > 0:
            self.slot = self.crate_slot
        elif isinstance(self.backplane_slot, int) and self.backplane_slot > 0:
            self.slot = self.backplane_slot
        else:
            self.slot = None

    def get_any_field(self, names):
        for name in names:
            value = _usable_field(self.fields.get(name))
            if value is not None:
                return value
        return None
