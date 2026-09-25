#!/usr/bin/env python3
"""List CRS boards on the network from UDP discovery beacons.

Sends a BEACONREQ broadcast, then listens for FWREQ/TCPIPE beacons. The
default is a curses live table (autosized columns, last-seen times, rows
greyed out after 10s). ``--dump`` collects beacons and prints a fitted
table once.

    python -m pychfpga.hardware.crs.discover
    python -m pychfpga.hardware.crs.discover --dump
    python -m pychfpga.hardware.crs.discover --dump -t 10
"""

from __future__ import annotations

import argparse
import curses
import select
import socket
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from pychfpga.hardware.crs.crs_discovery import (  # noqa: E402
    CRSBeacon,
    DEFAULT_BIND,
    DEFAULT_DISCOVERY_PORT,
    _usable_field,
    request_beacon,
)

DEFAULT_TIMEOUT_S = 3.0
BEACON_REQUEST_INTERVAL_S = 2.0
STALE_AFTER_S = 10.0
LIVE_POLL_S = 0.05
LIVE_AGE_UPDATE_S = 1.0
LIVE_HEADER_Y = 1
LIVE_DATA_TOP = 2
COLUMN_GAP = 2

COLUMN_HEADERS = (
    "bootloader",
    "version",
    "ip",
    "motherboard",
    "crate",
    "slot",
    "other",
)

LIVE_COLUMN_HEADERS = (
    "bootloader",
    "version",
    "ip",
    "first seen",
    "last seen",
    "motherboard",
    "crate",
    "slot",
    "other",
)

# Fixed streaming-mode widths. Last column is unpadded. Cells longer than
# the default overflow rather than being truncated.
DEFAULT_COLUMN_WIDTHS = (
    24,  # bootloader  (e.g. "tcpipe v0.1-12-gabcdef")
    8,   # version     ("TCPIPE/1")
    15,  # ip          (IPv4)
    20,  # motherboard ("t0 CRS SN0037 rev 4")
    18,  # crate       ("CCR SN011 rev 1")
    4,   # slot
    0,   # other
)

# Beacon fields already shown in dedicated columns.
_SHOWN_FIELD_KEYS = frozenset({
    "bootloader",
    "bootloader-version",
    "manufacturer",
    "product",
    "revision",
    "serial",
    "backplane-part",
    "backplane-model",
    "backplane-serial",
    "backplane-slot",
    "backplane-revision",
    "backplane-manufacturer",
    "crate-part",
    "crate-model",
    "crate-serial",
    "crate-slot",
    "crate-manufacturer",
    "crate-revision",
    "chassis-part",
    "chassis-model",
    "chassis-serial",
    "chassis-manufacturer",
    "chassis-revision",
})

# Short display names for long manufacturer strings in the live/dump tables.
MANUFACTURER_ALIASES = {
    "t0.technology": "t0*",
    "t0 technology": "t0*",
}


class BoardRecord:
    """One discovered board plus when its beacon was first and last received."""

    def __init__(self, beacon):
        now_mono = time.monotonic()
        now_wall = time.time()
        self.first_seen_mono = now_mono
        self.first_seen_wall = now_wall
        self.beacon = beacon
        self.last_seen_mono = now_mono
        self.last_seen_wall = now_wall

    def update(self, beacon):
        self.beacon = beacon
        self.last_seen_mono = time.monotonic()
        self.last_seen_wall = time.time()

    @property
    def age(self):
        return time.monotonic() - self.last_seen_mono

    @property
    def first_age(self):
        return time.monotonic() - self.first_seen_mono

    @property
    def stale(self):
        return self.age > STALE_AFTER_S


def _open_beacon_socket(port=DEFAULT_DISCOVERY_PORT, bind=DEFAULT_BIND, timeout=0.2):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind((bind, port))
    sock.settimeout(timeout)
    return sock


def _beacon_key(beacon):
    return beacon.board_serial or beacon.address


def _parse_beacon(data, addr):
    try:
        return CRSBeacon(data, address=addr[0])
    except (UnicodeDecodeError, ValueError):
        return None


def collect_beacons(
        timeout, port=DEFAULT_DISCOVERY_PORT, bind=DEFAULT_BIND, on_new=None):
    """Listen for CRS discovery beacons and return one beacon per board serial.

    ``on_new(beacon)`` is called the first time each board serial is seen.
    Later beacons for the same serial replace the stored entry but are not
    reported again.
    """
    sock = _open_beacon_socket(port=port, bind=bind, timeout=0.2)
    found = {}
    last_request = 0.0
    deadline = time.monotonic() + timeout
    try:
        while True:
            now = time.monotonic()
            if now >= deadline:
                break
            if now - last_request >= BEACON_REQUEST_INTERVAL_S:
                request_beacon(port=port)
                last_request = now
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            beacon = _parse_beacon(data, addr)
            if beacon is None:
                continue
            key = _beacon_key(beacon)
            is_new = key not in found
            found[key] = beacon
            if is_new and on_new is not None:
                on_new(beacon)
    finally:
        sock.close()
    return list(found.values())


def _board_sort_key(beacon):
    serial = str(beacon.board_serial or "")
    crate = str(beacon.crate_serial or "")
    slot = beacon.slot if isinstance(beacon.slot, int) else -1
    return (crate, slot, serial, beacon.address or "")


def _ip_sort_key(addr):
    text = str(addr or "")
    try:
        parts = tuple(int(p) for p in text.split("."))
        if len(parts) == 4:
            return (0, parts)
    except ValueError:
        pass
    return (1, text)


def _column_sort_key(record, col):
    name = LIVE_COLUMN_HEADERS[col] if 0 <= col < len(LIVE_COLUMN_HEADERS) else ""
    if name == "ip":
        return _ip_sort_key(record.beacon.address)
    if name == "first seen":
        return (record.first_seen_mono,)
    if name == "last seen":
        return (record.last_seen_mono,)
    if name == "slot":
        slot = _crate_slot_value(record.beacon)
        return (0, slot) if slot is not None else (1, 0)
    cells = live_columns(record)
    value = cells[col] if 0 <= col < len(cells) else ""
    return (str(value).casefold(),)


def _header_labels(sort_col=None, reverse=False):
    mark = "↓" if reverse else "↑"
    labels = []
    for i, name in enumerate(LIVE_COLUMN_HEADERS):
        if i == sort_col:
            labels.append(f"{name} {mark}")
        else:
            labels.append(name)
    return tuple(labels)


def _column_x_spans(widths, gap=COLUMN_GAP):
    """Return (start, end) x ranges for each column, last column unbounded."""
    spans = []
    x = 0
    for i, width in enumerate(widths):
        start = x
        if i < len(widths) - 1:
            end = x + max(width, 0) + gap
            x = end
        else:
            end = None
        spans.append((start, end))
    return spans


def _column_at_x(x, widths, gap=COLUMN_GAP):
    if x < 0:
        return None
    for i, (start, end) in enumerate(_column_x_spans(widths, gap=gap)):
        if end is None:
            return i if x >= start else None
        if start <= x < end:
            return i
    return None


def _clean_text(value):
    """Strip non-printable bytes (e.g. ``^A^F`` in EEPROM revision) and placeholders."""
    if value is None:
        return None
    text = "".join(c for c in str(value) if c.isprintable()).strip()
    return _usable_field(text)


def _is_uboot(beacon):
    name = (_clean_text(beacon.fields.get("bootloader")) or beacon.kind or "").lower()
    name = name.replace("_", "-")
    return name in {"u-boot", "uboot", "fwreq"} or name.startswith("u-boot")


def _crate_slot_value(beacon):
    """Board slot from EEPROM ``slot=`` (Crate Slot, else Backplane Slot)."""
    if isinstance(beacon.slot, int) and beacon.slot > 0:
        return beacon.slot
    return None


def _crate_slot_text(beacon):
    slot = _crate_slot_value(beacon)
    return str(slot) if slot is not None else "-"


def _serial_text(serial):
    """Return ``SNxxx`` using the stored serial digits, or None if missing."""
    text = _clean_text(serial)
    if text is None:
        return None
    if text.upper().startswith("SN"):
        text = text[2:]
        if not text:
            return None
    return f"SN{text}"


def _identity_text(part, serial, revision=None, manufacturer=None,
                  expected=None, missing=""):
    """Format manufacturer, model, serial, and revision.

    Present fields are always included. Empty fields become ``missing`` only
    when they are listed in ``expected``; unexpected empty fields are omitted.
    """
    rev = _clean_text(revision)
    manufacturer = _clean_text(manufacturer)
    slots = (
        ("manufacturer",
         MANUFACTURER_ALIASES.get(manufacturer.casefold(), manufacturer)
         if manufacturer else None),
        ("part", _clean_text(part)),
        ("serial", _serial_text(serial)),
        ("revision", f"rev {rev}" if rev else None),
    )
    if not any(value for _, value in slots):
        return ""
    expected = set(expected) if expected else set()
    bits = []
    for name, value in slots:
        if value:
            bits.append(value)
        elif missing and name in expected:
            bits.append(missing)
    return " ".join(bits)


def _motherboard_text(beacon):
    """Format manufacturer, product, serial, and revision as stored on the beacon."""
    bits = []
    manufacturer = _clean_text(beacon.fields.get("manufacturer"))
    if manufacturer:
        bits.append(MANUFACTURER_ALIASES.get(manufacturer.casefold(), manufacturer))
    part = _clean_text(beacon.board_part)
    if part:
        bits.append(part)
    serial = _serial_text(beacon.board_serial)
    if serial:
        bits.append(serial)
    revision = _clean_text(beacon.fields.get("revision"))
    if revision:
        bits.append(f"rev {revision}")
    return " ".join(bits) if bits else "-"


def _bootloader_text(beacon):
    name = _clean_text(beacon.fields.get("bootloader")) or beacon.kind.lower()
    version = _clean_text(beacon.fields.get("bootloader-version"))
    if version:
        return f"{name} {version}"
    return name


def _protocol_text(beacon):
    """Beacon dialect and protocol version (``FWREQ/1``, ``TCPIPE/1``)."""
    if beacon.kind == "FWREQ":
        return f"{beacon.kind}/{beacon.version}"
    return f"{beacon.kind}/1"


def _other_text(beacon):
    parts = []
    bp = _identity_text(
        beacon.backplane_part,
        beacon.backplane_serial,
        beacon.backplane_revision,
        manufacturer=beacon.backplane_manufacturer,
        expected=("manufacturer", "part", "serial", "revision"),
        missing="?",
    )
    if bp:
        parts.append(f"(BP: {bp})")
    chassis = _identity_text(
        beacon.chassis_part,
        beacon.chassis_serial,
        beacon.chassis_revision,
        manufacturer=beacon.chassis_manufacturer,
        expected=("part", "serial"),
        missing="?",
    )
    if chassis:
        parts.append(f"(Chassis: {chassis})")
    for key, value in beacon.fields.items():
        if key in _SHOWN_FIELD_KEYS:
            continue
        text = _clean_text(value)
        if not text:
            continue
        parts.append(f"{key}={text}")
    return " ".join(parts) if parts else "-"


def _age_text(age_s):
    """Integer age: seconds, minutes, ``1h23``, or ``2d3h23``."""
    age = max(0, int(age_s))
    if age < 60:
        return f"{age}s"
    if age < 3600:
        return f"{age // 60}m"
    minutes = (age // 60) % 60
    hours = (age // 3600) % 24
    days = age // 86400
    if days:
        if hours and minutes:
            return f"{days}d{hours}h{minutes}"
        if hours:
            return f"{days}d{hours}h"
        if minutes:
            return f"{days}d{minutes}m"
        return f"{days}d"
    if minutes:
        return f"{hours}h{minutes}"
    return f"{hours}h"


def _seen_text(wall, age_s):
    clock = time.strftime("%H:%M:%S", time.localtime(wall))
    return f"{clock} ({_age_text(age_s)})"


def _first_seen_text(record):
    return _seen_text(record.first_seen_wall, record.first_age)


def _last_seen_text(record):
    return _seen_text(record.last_seen_wall, record.age)


def board_columns(beacon):
    """Return the aligned-table cells for one board."""
    return (
        _bootloader_text(beacon),
        _protocol_text(beacon),
        beacon.address or "-",
        _motherboard_text(beacon),
        _identity_text(
            beacon.crate_part,
            beacon.crate_serial,
            beacon.crate_revision,
            manufacturer=beacon.crate_manufacturer,
            expected=("manufacturer", "part", "serial", "revision"),
            missing="?",
        ),
        _crate_slot_text(beacon),
        _other_text(beacon),
    )


def live_columns(record):
    """Return live-table cells, inserting first/last seen after the IP column."""
    cols = board_columns(record.beacon)
    return cols[:3] + (_first_seen_text(record), _last_seen_text(record)) + cols[3:]


def format_row(row, widths=DEFAULT_COLUMN_WIDTHS):
    """Format one table row with the given column widths."""
    cells = []
    for i, cell in enumerate(row):
        text = str(cell)
        width = widths[i] if i < len(widths) else 0
        if i < len(row) - 1 and width:
            text = text.ljust(width)
        cells.append(text)
    return "  ".join(cells).rstrip()


def fitted_column_widths(rows, headers=COLUMN_HEADERS, pad_last=False):
    """Return column widths that fit headers and every row.

    By default the last column is left unpadded (width 0).
    """
    table = [tuple(headers)] + [tuple(row) for row in rows]
    widths = [
        max(len(str(row[i])) for row in table)
        for i in range(len(headers))
    ]
    if not pad_last:
        widths[-1] = 0
    return widths


def constrain_widths(widths, term_width, gap=COLUMN_GAP):
    """Expand the last column to fill the terminal; never shrink other columns.

    If the fitted row is wider than the terminal, the line is clipped on
    the right when it is drawn.
    """
    widths = [max(0, int(w)) for w in widths]
    if not widths:
        return widths
    gaps = gap * max(0, len(widths) - 1)
    leftover = term_width - (sum(widths[:-1]) + gaps)
    widths[-1] = max(widths[-1], max(0, leftover))
    return widths


def format_table(rows, headers=COLUMN_HEADERS, widths=None):
    """Return a left-aligned, two-space-separated table including a header."""
    rows = [tuple(row) for row in rows]
    if widths is None:
        widths = fitted_column_widths(rows, headers=headers)
    lines = [format_row(headers, widths)]
    lines.extend(format_row(row, widths) for row in rows)
    return "\n".join(lines)


def _clip_cell(text, width):
    if width <= 0:
        return ""
    if len(text) <= width:
        return text.ljust(width)
    if width <= 1:
        return text[:width]
    return text[: width - 1] + "~"


def _format_clipped_row(row, widths, gap=COLUMN_GAP):
    cells = [
        _clip_cell(str(cell), widths[i] if i < len(widths) else 0)
        for i, cell in enumerate(row)
    ]
    return (" " * gap).join(cells).rstrip()


def _addstr(stdscr, y, x, text, attr=0):
    """Overwrite one line in place. Never write into the last column (wraps/scrolls)."""
    max_y, max_x = stdscr.getmaxyx()
    if y < 0 or y >= max_y or x >= max_x - 1:
        return
    if x < 0:
        text = text[-x:]
        x = 0
    limit = max(0, max_x - x - 1)
    text = text[:limit]
    try:
        stdscr.move(y, x)
        stdscr.clrtoeol()
        if text:
            stdscr.insstr(y, x, text, attr)
    except curses.error:
        pass


def _ovrstr(stdscr, y, x, text, attr=0):
    """Overwrite characters at (y, x) without shifting the rest of the line."""
    max_y, max_x = stdscr.getmaxyx()
    if y < 0 or y >= max_y or x >= max_x - 1:
        return
    if x < 0:
        text = text[-x:]
        x = 0
    text = text[: max(0, max_x - x - 1)]
    if not text:
        return
    try:
        stdscr.addstr(y, x, text, attr)
    except curses.error:
        pass


def _init_live_colors():
    """Return ``(stale_attr, green_attr)`` for the live table."""
    curses.start_color()
    try:
        curses.use_default_colors()
    except curses.error:
        pass
    stale_attr = curses.A_DIM
    green_attr = curses.A_BOLD
    try:
        if curses.COLORS >= 256:
            curses.init_pair(1, 244, -1)
            curses.init_pair(2, 82, -1)
        else:
            curses.init_pair(1, curses.COLOR_WHITE, -1)
            curses.init_pair(2, curses.COLOR_GREEN, -1)
        stale_attr = curses.color_pair(1) | curses.A_DIM
        green_attr = curses.color_pair(2)
    except curses.error:
        pass
    return stale_attr, green_attr


def _row_attr(record, stale_attr, green_attr, selected=False):
    if record.stale:
        attr = stale_attr
    elif not _is_uboot(record.beacon):
        attr = green_attr
    else:
        attr = 0
    if selected:
        attr |= curses.A_REVERSE
    return attr


def _ordered_records(records, sort_col=None, reverse=False):
    recs = list(records.values())
    if sort_col is None:
        recs.sort(key=lambda rec: _board_sort_key(rec.beacon), reverse=reverse)
    else:
        recs.sort(
            key=lambda rec: (_column_sort_key(rec, sort_col),
                             _board_sort_key(rec.beacon)),
            reverse=reverse)
    return recs


def _ordered_keys(records, sort_col=None, reverse=False):
    return [
        _beacon_key(rec.beacon)
        for rec in _ordered_records(records, sort_col, reverse)
    ]


def _move_selection(records, selected_key, delta, sort_col=None, reverse=False):
    keys = _ordered_keys(records, sort_col, reverse)
    if not keys:
        return None
    if selected_key in keys:
        index = keys.index(selected_key)
    else:
        index = 0
    index = max(0, min(len(keys) - 1, index + delta))
    return keys[index]


def _ensure_visible(selected, scroll, n_visible):
    if n_visible <= 0:
        return 0
    if selected < scroll:
        return selected
    if selected >= scroll + n_visible:
        return selected - n_visible + 1
    return scroll


def _draw_live(stdscr, records, stale_attr, green_attr,
               scroll=0, selected_key=None, deadline=None,
               sort_col=None, sort_reverse=False):
    max_y, max_x = stdscr.getmaxyx()
    empty_widths = [len(h) for h in LIVE_COLUMN_HEADERS]
    if max_y < 3 or max_x < 8:
        return 0, 0, 0, selected_key, empty_widths
    cols = max(1, max_x - 1)

    ordered = _ordered_records(records, sort_col, sort_reverse)
    keys = [_beacon_key(rec.beacon) for rec in ordered]
    stale_n = sum(1 for rec in ordered if rec.stale)
    remaining = ""
    if deadline is not None:
        left = max(0.0, deadline - time.monotonic())
        remaining = f"  {left:.0f}s left"

    data_top = LIVE_DATA_TOP
    footer_y = max_y - 1
    n_visible = max(0, footer_y - data_top)
    n_rows = len(ordered)
    if not keys:
        selected_key = None
        selected = 0
        scroll = 0
    else:
        if selected_key not in keys:
            selected_key = keys[0]
        selected = keys.index(selected_key)
        scroll = _ensure_visible(selected, scroll, n_visible)
        scroll = max(0, min(scroll, max(0, n_rows - n_visible)))

    status = (
        f" CRS discovery  {n_rows} board"
        f"{'s' if n_rows != 1 else ''}"
        f"{f' ({stale_n} stale)' if stale_n else ''}"
        f"  grey > {STALE_AFTER_S:g}s  green=tcpipe"
        f"  click header to sort  q quit  ↑↓ select{remaining} "
    )
    _addstr(stdscr, 0, 0, status.ljust(cols), curses.A_REVERSE)

    headers = _header_labels(sort_col, sort_reverse)
    rows = [live_columns(rec) for rec in ordered]
    widths = constrain_widths(
        fitted_column_widths(rows, headers=headers, pad_last=True),
        cols,
    )
    _addstr(stdscr, LIVE_HEADER_Y, 0, "", 0)
    x = 0
    for i, label in enumerate(headers):
        width = widths[i] if i < len(widths) else 0
        if i < len(headers) - 1:
            cell = _clip_cell(str(label), width) + (" " * COLUMN_GAP)
        else:
            cell = str(label)
        attr = curses.A_BOLD
        if i == sort_col:
            attr |= curses.A_REVERSE
        _ovrstr(stdscr, LIVE_HEADER_Y, x, cell, attr)
        x += len(cell)

    if not ordered:
        _addstr(stdscr, data_top, 0, "Waiting for beacons...", curses.A_DIM)
        next_y = data_top + 1
    else:
        visible = ordered[scroll:scroll + n_visible]
        for i, rec in enumerate(visible):
            index = scroll + i
            line = _format_clipped_row(rows[index], widths)
            selected = keys[index] == selected_key
            if selected:
                line = line.ljust(cols)
            _addstr(
                stdscr, data_top + i, 0, line,
                _row_attr(rec, stale_attr, green_attr, selected=selected),
            )
        next_y = data_top + len(visible)

    if n_rows > n_visible:
        lo = scroll + 1
        hi = scroll + min(n_visible, n_rows - scroll)
        _addstr(
            stdscr, footer_y, 0,
            f"  {lo}-{hi} of {n_rows}  ↑↓ select  PgUp/PgDn  click header to sort",
            curses.A_REVERSE,
        )
        next_y = footer_y
    elif next_y <= footer_y:
        try:
            stdscr.move(footer_y, 0)
            stdscr.clrtoeol()
        except curses.error:
            pass

    for y in range(next_y, footer_y if n_rows > n_visible else max_y):
        try:
            stdscr.move(y, 0)
            stdscr.clrtoeol()
        except curses.error:
            pass
    stdscr.refresh()
    return n_rows, n_visible, scroll, selected_key, widths


def _drain_beacons(sock, records):
    """Ingest queued UDP beacons. Return True if displayed fields changed."""
    changed = False
    while True:
        try:
            data, addr = sock.recvfrom(4096)
        except (BlockingIOError, InterruptedError, socket.timeout):
            return changed
        except OSError:
            return changed
        beacon = _parse_beacon(data, addr)
        if beacon is None:
            continue
        key = _beacon_key(beacon)
        rec = records.get(key)
        if rec is None:
            records[key] = BoardRecord(beacon)
            changed = True
            continue
        prev = rec.beacon
        rec.update(beacon)
        if (prev.kind, prev.address, prev.fields) != (
                beacon.kind, beacon.address, beacon.fields):
            changed = True


def _live_loop(stdscr, sock, timeout=None, port=DEFAULT_DISCOVERY_PORT):
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.keypad(True)
    stdscr.scrollok(False)
    stdscr.idlok(False)
    stdscr.leaveok(True)
    try:
        curses.mousemask(curses.ALL_MOUSE_EVENTS)
    except curses.error:
        pass
    stale_attr, green_attr = _init_live_colors()
    sock.setblocking(False)

    records = {}
    scroll = 0
    selected_key = None
    sort_col = None
    sort_reverse = False
    header_widths = [len(h) for h in LIVE_COLUMN_HEADERS]
    n_visible = 0
    last_request = 0.0
    last_age_draw = 0.0
    dirty = True
    deadline = None if timeout is None else time.monotonic() + timeout
    click_mask = (
        getattr(curses, "BUTTON1_CLICKED", 0)
        or getattr(curses, "BUTTON1_PRESSED", 0)
    )

    while True:
        now = time.monotonic()
        if deadline is not None and now >= deadline:
            break

        while True:
            ch = stdscr.getch()
            if ch == -1:
                break
            if ch in (ord("q"), ord("Q"), 27):
                return
            if ch == curses.KEY_RESIZE:
                dirty = True
            elif ch in (curses.KEY_UP, ord("k")):
                selected_key = _move_selection(
                    records, selected_key, -1, sort_col, sort_reverse)
                dirty = True
            elif ch in (curses.KEY_DOWN, ord("j")):
                selected_key = _move_selection(
                    records, selected_key, 1, sort_col, sort_reverse)
                dirty = True
            elif ch in (curses.KEY_PPAGE, ord("b")):
                selected_key = _move_selection(
                    records, selected_key, -max(1, n_visible),
                    sort_col, sort_reverse)
                dirty = True
            elif ch in (curses.KEY_NPAGE, ord(" "), ord("f")):
                selected_key = _move_selection(
                    records, selected_key, max(1, n_visible),
                    sort_col, sort_reverse)
                dirty = True
            elif ch in (curses.KEY_HOME, ord("g")):
                keys = _ordered_keys(records, sort_col, sort_reverse)
                selected_key = keys[0] if keys else None
                dirty = True
            elif ch in (curses.KEY_END, ord("G")):
                keys = _ordered_keys(records, sort_col, sort_reverse)
                selected_key = keys[-1] if keys else None
                dirty = True
            elif ch == curses.KEY_MOUSE:
                try:
                    _id, x, y, _z, bstate = curses.getmouse()
                except curses.error:
                    continue
                if bstate & getattr(curses, "BUTTON4_PRESSED", 0):
                    selected_key = _move_selection(
                        records, selected_key, -3, sort_col, sort_reverse)
                    dirty = True
                elif bstate & getattr(curses, "BUTTON5_PRESSED", 0):
                    selected_key = _move_selection(
                        records, selected_key, 3, sort_col, sort_reverse)
                    dirty = True
                elif click_mask and (bstate & click_mask):
                    if y == LIVE_HEADER_Y:
                        col = _column_at_x(x, header_widths)
                        if col is not None:
                            if col == sort_col:
                                sort_reverse = not sort_reverse
                            else:
                                sort_col = col
                                sort_reverse = False
                            dirty = True
                    else:
                        keys = _ordered_keys(records, sort_col, sort_reverse)
                        index = scroll + (y - LIVE_DATA_TOP)
                        if 0 <= index < len(keys):
                            selected_key = keys[index]
                            dirty = True

        if now - last_request >= BEACON_REQUEST_INTERVAL_S:
            try:
                request_beacon(port=port)
            except OSError:
                pass
            last_request = now

        ready, _, _ = select.select([sock], [], [], LIVE_POLL_S)
        if ready and _drain_beacons(sock, records):
            dirty = True

        if now - last_age_draw >= LIVE_AGE_UPDATE_S:
            dirty = True
            last_age_draw = now

        if dirty:
            n_rows, n_visible, scroll, selected_key, header_widths = _draw_live(
                stdscr, records, stale_attr, green_attr,
                scroll=scroll, selected_key=selected_key, deadline=deadline,
                sort_col=sort_col, sort_reverse=sort_reverse)
            dirty = False


def run_live(timeout=None, port=DEFAULT_DISCOVERY_PORT, bind=DEFAULT_BIND):
    """Run the curses live table until quit (or ``timeout``)."""
    sock = _open_beacon_socket(port=port, bind=bind, timeout=0.05)
    try:
        curses.wrapper(
            lambda stdscr: _live_loop(stdscr, sock, timeout=timeout, port=port))
    finally:
        sock.close()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="List CRS boards on the network with their beacon fields.")
    parser.add_argument(
        "-t", "--timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help="How long to listen (default: until quit; with --dump, "
             f"{DEFAULT_TIMEOUT_S:g}s)",
    )
    parser.add_argument(
        "-d", "--dump",
        action="store_true",
        help="Print the board list once and exit instead of the live table",
    )
    parser.add_argument(
        "-s", "--sort",
        action="store_true",
        help="Same as --dump (print a sorted fitted table and exit)",
    )
    parser.add_argument(
        "-l", "--live",
        action="store_true",
        help="Curses live table (default). Rows not seen for "
             f"{STALE_AFTER_S:g}s are greyed out; non-u-boot rows are green. "
             "click a column header to sort, click again to reverse "
             "(q to quit)",
    )
    args = parser.parse_args(argv)
    dump = args.dump or args.sort
    if args.live and dump:
        parser.error("--live cannot be combined with --dump/--sort")

    timeout = args.timeout
    if timeout is None:
        timeout = DEFAULT_TIMEOUT_S if dump else None
    elif timeout <= 0:
        parser.error("timeout must be greater than 0")

    if not dump:
        if not sys.stdout.isatty():
            print(
                "live view requires a terminal; use --dump to print the list",
                file=sys.stderr,
            )
            return 1
        try:
            run_live(timeout=timeout)
        except OSError as exc:
            print(
                f"Cannot bind UDP :{DEFAULT_DISCOVERY_PORT} ({exc})",
                file=sys.stderr,
            )
            return 1
        except KeyboardInterrupt:
            return 130
        return 0

    print(
        f"Listening for CRS beacons for {timeout:g}s "
        f"(UDP port {DEFAULT_DISCOVERY_PORT})...",
        file=sys.stderr,
        flush=True,
    )

    try:
        boards = collect_beacons(timeout)
    except OSError as exc:
        print(
            f"Cannot bind UDP :{DEFAULT_DISCOVERY_PORT} ({exc})",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        print(file=sys.stderr)
        return 130

    if not boards:
        print("No boards found.")
        return 0

    boards = sorted(boards, key=_board_sort_key)
    print(format_table(board_columns(beacon) for beacon in boards))
    print()
    print(f"Found {len(boards)} board{'s' if len(boards) != 1 else ''}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
