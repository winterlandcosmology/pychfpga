#!/usr/bin/env python3
"""Monitor Novus NR3620-O/G-HS GNSS lock health from NMEA serial output.

Reads standard NMEA sentences plus Novus proprietary $GPNVS status strings and
prints a one-line lock-health summary about once per second (on each GGA cycle).

Optionally queries a Tektronix MDO scope over TCP SCPI for CH1->CH2 delay.

Typical use::

    python monitor_novus_gps.py
    python monitor_novus_gps.py --changes-only
    python monitor_novus_gps.py --log lock_events.csv
    python monitor_novus_gps.py --scope --scope-ip 10.10.10.85

Requires pyserial for live capture::

    pip install pyserial
"""

from __future__ import annotations

import argparse
import csv
import socket
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Iterator, Optional

try:
    import serial
except ImportError:  # pragma: no cover - optional at import time
    serial = None  # type: ignore[assignment,misc]


GGA_FIX_NAMES = {
    0: "NONE",
    1: "GPS",
    2: "DGPS",
    4: "RTK-FIX",
    5: "RTK-FLOAT",
}

GSA_FIX_NAMES = {1: "NONE", 2: "2D", 3: "3D"}

PNVS13_GNSS_LOCK = {
    0: "UNLOCKED",
    1: "ACQUIRING",
    2: "COARSE",
    3: "LOCKED",
}


@dataclass
class GpsState:
    """Accumulated receiver state from the latest NMEA cycle."""

    utc_time: str = ""
    utc_date: str = ""  # YYYY-MM-DD when known

    gga_quality: int = -1
    gga_sats: int = -1
    gga_hdop: float = -1.0

    gsa_fix: int = -1

    gll_valid: str = ""

    pnvs1_lock: str = ""
    pnvs1_sats: int = -1
    pnvs1_channel_fault: str = ""
    pnvs1_ps_fault: str = ""
    pnvs1_error: str = ""

    pnvs7_lock: str = ""
    pnvs7_sats: int = -1
    pnvs7_error: str = ""
    pnvs7_freq_diff: Optional[int] = None
    pnvs7_pps_diff: Optional[int] = None
    pnvs7_freq_corr: Optional[int] = None
    pnvs7_dac: Optional[int] = None

    pnvs10_stab_en: int = -1
    pnvs10_disc_gps: int = -1
    pnvs10_pps_type: int = -1
    pnvs10_pps_diff: Optional[int] = None
    pnvs10_pps_avg: Optional[int] = None
    pnvs10_freq_var: Optional[int] = None

    pnvs13_gnss_lock: int = -1
    pnvs13_loop_lock: int = -1
    pnvs13_disc_src: int = -1

    pnvs22_lock: str = ""

    scope_delay_ns: Optional[float] = None

    host_time: float = field(default_factory=time.time)

    def lock_summary(self) -> str:
        """Overall lock health label for change detection."""
        if self._is_unlocked():
            return "UNLOCKED"
        if self._is_fully_locked():
            return "LOCKED"
        return "DEGRADED"

    def _is_unlocked(self) -> bool:
        if self.gga_quality == 0:
            return True
        if self.gsa_fix == 1:
            return True
        if self.gll_valid == "V":
            return True
        if self.pnvs7_lock == "V":
            return True
        if self.pnvs1_lock == "V":
            return True
        if self.pnvs22_lock == "V":
            return True
        if self.pnvs13_gnss_lock == 0:
            return True
        return False

    def _is_fully_locked(self) -> bool:
        gga_ok = self.gga_quality > 0
        gsa_ok = self.gsa_fix in (2, 3)
        novus_ok = self.pnvs7_lock in ("", "A") and self.pnvs1_lock in ("", "A")
        loop_ok = self.pnvs13_loop_lock in (-1, 1)
        gnss_lvl_ok = self.pnvs13_gnss_lock in (-1, 3)
        return gga_ok and gsa_ok and novus_ok and loop_ok and gnss_lvl_ok

    def change_key(self) -> tuple:
        """Fields tracked for --changes-only output."""
        return (
            self.lock_summary(),
            self.gga_quality,
            self.gga_sats,
            self.gsa_fix,
            self.gll_valid,
            self.pnvs1_lock,
            self.pnvs7_lock,
            self.pnvs7_error,
            self.pnvs7_pps_diff,
            self.pnvs10_disc_gps,
            self.pnvs10_pps_diff,
            self.pnvs10_pps_avg,
            self.pnvs13_gnss_lock,
            self.pnvs13_loop_lock,
            self.pnvs22_lock,
            _scope_delay_change_key(self.scope_delay_ns),
        )

    def utc_label(self) -> str:
        if self.utc_date and self.utc_time:
            return f"{self.utc_date}T{self.utc_time}Z"
        if self.utc_time:
            return f"{self.utc_time}Z"
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    def format_status_line(self) -> str:
        gga_q = GGA_FIX_NAMES.get(self.gga_quality, str(self.gga_quality))
        gsa = GSA_FIX_NAMES.get(self.gsa_fix, str(self.gsa_fix))
        gll = self.gll_valid or "?"

        nvs7 = " ".join(
            part
            for part in (
                f"lock={self.pnvs7_lock or '?'}",
                f"sats={_fmt_int(self.pnvs7_sats)}",
                f"err={self.pnvs7_error or '?'}",
                f"pps_d={_fmt_signed(self.pnvs7_pps_diff)}",
                f"freq_d={_fmt_signed(self.pnvs7_freq_diff)}",
                f"dac={_fmt_int(self.pnvs7_dac)}",
            )
            if part.split("=", 1)[1] not in ("?", "")
        )

        pps_type = {0: "OCXO", 1: "GPS"}.get(self.pnvs10_pps_type, str(self.pnvs10_pps_type))
        nvs10 = " ".join(
            part
            for part in (
                f"stab={_fmt_int(self.pnvs10_stab_en)}",
                f"disc={_fmt_int(self.pnvs10_disc_gps)}",
                f"type={pps_type}",
                f"pps={_fmt_signed(self.pnvs10_pps_diff, 'ns')}",
                f"avg={_fmt_signed(self.pnvs10_pps_avg, 'ns')}",
                f"freq_var={_fmt_int(self.pnvs10_freq_var)}",
            )
            if not part.endswith("=?") and not part.endswith("=-1")
        )

        parts = [
            self.utc_label(),
            self.lock_summary(),
            f"GGA:{gga_q}/{_fmt_int(self.gga_sats)}s HDOP{_fmt_float(self.gga_hdop)}",
            f"GSA:{gsa}",
            f"GLL:{gll}",
        ]
        if nvs7:
            parts.append(f"NVS7:{nvs7}")
        if nvs10:
            parts.append(f"NVS10:{nvs10}")
        if self.pnvs13_gnss_lock >= 0 or self.pnvs13_loop_lock >= 0:
            gnss = PNVS13_GNSS_LOCK.get(self.pnvs13_gnss_lock, str(self.pnvs13_gnss_lock))
            parts.append(
                f"NVS13:gnss={gnss} loop={_fmt_int(self.pnvs13_loop_lock)} src={_fmt_int(self.pnvs13_disc_src)}"
            )
        if self.pnvs22_lock:
            parts.append(f"NVS22:lock={self.pnvs22_lock}")
        if self.pnvs1_lock:
            parts.append(
                f"NVS1:lock={self.pnvs1_lock} ch_fault={self.pnvs1_channel_fault or '0x00'} "
                f"ps_fault={self.pnvs1_ps_fault or '0x00'} err={self.pnvs1_error or '0x00'}"
            )
        if self.scope_delay_ns is not None:
            parts.append(f"SCOPE:ch1_ch2={_fmt_scope_delay(self.scope_delay_ns)}")
        return " | ".join(parts)


def _fmt_int(value: int | None) -> str:
    if value is None or value < 0:
        return "?"
    return str(value)


def _fmt_float(value: float) -> str:
    return "?" if value < 0 else f"{value:.2f}"


def _fmt_signed(value: Optional[int], suffix: str = "") -> str:
    if value is None:
        return "?"
    sign = "+" if value >= 0 else ""
    return f"{sign}{value}{suffix}"


def _fmt_scope_delay(delay_ns: Optional[float]) -> str:
    if delay_ns is None:
        return "?"
    sign = "+" if delay_ns >= 0 else ""
    return f"{sign}{delay_ns:.1f}ns"


def _scope_delay_change_key(delay_ns: Optional[float]) -> Optional[int]:
    """Quantize scope delay for --changes-only (1 ns resolution)."""
    if delay_ns is None:
        return None
    return int(round(delay_ns))


class TekScopeSCPI:
    """Minimal Tektronix scope control over raw TCP SCPI (port 4000)."""

    def __init__(self, host: str, port: int = 4000, timeout: float = 5.0) -> None:
        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._sock.settimeout(timeout)
        self._buf = b""

    def close(self) -> None:
        self._sock.close()

    def write(self, cmd: str) -> None:
        self._sock.sendall(f"{cmd}\n".encode("ascii"))

    def query(self, cmd: str) -> str:
        self.write(cmd)
        return self._readline()

    def _readline(self) -> str:
        while b"\n" not in self._buf:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("scope connection closed")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return line.decode("ascii", errors="replace").strip()

    def setup_ch1_to_ch2_delay(self) -> str:
        """Configure immediate DELAY measurement CH1 -> CH2. Returns *IDN?."""
        idn = self.query("*IDN?")
        self.write("*CLS")
        for cmd in (
            "MEASUrement:IMMed:TYPe DELAY",
            "MEASUrement:IMMed:SOUrce1 CH1",
            "MEASUrement:IMMed:SOUrce2 CH2",
            "MEASUrement:IMMed:DELay:EDGE1 RISE",
            "MEASUrement:IMMed:DELay:EDGE2 RISE",
            "MEASUrement:IMMed:DELay:DIRection FORWARDS",
            "MEASUrement:REFLevel:METHod PERCent",
            "MEASUrement:REFLevel:PERCent:MID 50",
        ):
            self.write(cmd)
        self.query("*OPC?")
        return idn

    def read_delay_ns(self) -> Optional[float]:
        """Return CH1->CH2 delay in nanoseconds, or None if invalid."""
        raw = self.query("MEASUrement:IMMed:VALue?")
        value = raw.split(",")[0].strip()
        try:
            seconds = float(value)
        except ValueError:
            return None
        if abs(seconds) >= 1e30:
            return None
        return seconds * 1e9


def open_scope(host: str, port: int, timeout: float) -> TekScopeSCPI:
    scope = TekScopeSCPI(host, port, timeout=timeout)
    idn = scope.setup_ch1_to_ch2_delay()
    print(f"Scope connected: {idn}", file=sys.stderr)
    return scope


def _parse_int(text: str) -> int:
    text = text.strip()
    if not text:
        return -1
    return int(text)


def _parse_float(text: str) -> float:
    text = text.strip()
    if not text:
        return -1.0
    return float(text)


def _parse_optional_int(text: str) -> Optional[int]:
    text = text.strip()
    if not text:
        return None
    return int(text)


def _split_sentence(line: str) -> Optional[list[str]]:
    line = line.strip()
    if not line or line[0] not in "$!":
        return None
    star = line.rfind("*")
    body = line[1:star] if star > 0 else line[1:]
    return body.split(",")


def _zda_to_date(fields: list[str]) -> str:
    # $GNZDA,time,day,month,year,...
    if len(fields) < 5:
        return ""
    day, month, year = fields[2], fields[3], fields[4]
    if not (day and month and year):
        return ""
    return f"{year}-{int(month):02d}-{int(day):02d}"


class NmeaParser:
    """Incremental parser for Novus NR3620 NMEA output."""

    def __init__(self) -> None:
        self.state = GpsState()
        self._have_cycle = False

    def feed_line(self, line: str) -> Optional[GpsState]:
        """Parse one NMEA line.

        Returns state when a full one-second NMEA cycle completed (next GGA seen).
        """
        fields = _split_sentence(line)
        if not fields:
            return None

        sentence = fields[0]
        msg = sentence[2:] if len(sentence) > 2 else ""

        emit = False
        snapshot: Optional[GpsState] = None
        if msg == "GGA" and len(fields) >= 8:
            if self._have_cycle:
                snapshot = GpsState(**asdict(self.state))
            self._parse_gga(fields)
            self._have_cycle = True
        elif msg == "GSA" and len(fields) >= 3:
            self._parse_gsa(fields)
        elif msg == "GLL" and len(fields) >= 8:
            self._parse_gll(fields)
        elif msg == "ZDA" and len(fields) >= 5:
            self.state.utc_date = _zda_to_date(fields)
            if fields[1]:
                self.state.utc_time = fields[1]
        elif fields[0].endswith("PNVS"):
            self._parse_pnvs(fields[1:])

        if snapshot is None:
            return None
        snapshot.host_time = time.time()
        return snapshot

    def flush(self) -> Optional[GpsState]:
        """Return the in-progress cycle when the input stream ends."""
        if not self._have_cycle:
            return None
        state = GpsState(**asdict(self.state))
        state.host_time = time.time()
        return state

    def _parse_gga(self, fields: list[str]) -> None:
        if fields[1]:
            self.state.utc_time = fields[1]
        self.state.gga_quality = _parse_int(fields[6]) if len(fields) > 6 else -1
        self.state.gga_sats = _parse_int(fields[7]) if len(fields) > 7 else -1
        self.state.gga_hdop = _parse_float(fields[8]) if len(fields) > 8 else -1.0

    def _parse_gsa(self, fields: list[str]) -> None:
        self.state.gsa_fix = _parse_int(fields[2])

    def _parse_gll(self, fields: list[str]) -> None:
        if fields[5]:
            self.state.utc_time = fields[5]
        self.state.gll_valid = fields[6].strip().upper() if len(fields) > 6 else ""

    def _parse_pnvs(self, fields: list[str]) -> None:
        if not fields:
            return
        sid = fields[0].strip()
        if sid == "1" and len(fields) >= 8:
            if fields[1]:
                self.state.utc_time = fields[1]
            self.state.pnvs1_lock = fields[3].strip().upper()
            self.state.pnvs1_sats = _parse_int(fields[4])
            self.state.pnvs1_channel_fault = fields[5].strip()
            self.state.pnvs1_ps_fault = fields[6].strip()
            self.state.pnvs1_error = fields[7].strip()
        elif sid == "7" and len(fields) >= 11:
            if fields[1]:
                self.state.utc_time = fields[1]
            self.state.pnvs7_lock = fields[3].strip().upper()
            self.state.pnvs7_sats = _parse_int(fields[4])
            self.state.pnvs7_error = fields[5].strip()
            self.state.pnvs7_freq_diff = _parse_optional_int(fields[6])
            self.state.pnvs7_pps_diff = _parse_optional_int(fields[7])
            self.state.pnvs7_freq_corr = _parse_optional_int(fields[8])
            self.state.pnvs7_dac = _parse_optional_int(fields[9])
        elif sid == "10" and len(fields) >= 8:
            self.state.pnvs10_stab_en = _parse_int(fields[1])
            self.state.pnvs10_disc_gps = _parse_int(fields[2])
            self.state.pnvs10_pps_type = _parse_int(fields[3])
            self.state.pnvs10_pps_diff = _parse_optional_int(fields[4])
            self.state.pnvs10_pps_avg = _parse_optional_int(fields[5])
            if len(fields) > 11:
                self.state.pnvs10_freq_var = _parse_optional_int(fields[11])
        elif sid == "13" and len(fields) >= 8:
            self.state.pnvs13_disc_src = _parse_int(fields[2])
            self.state.pnvs13_gnss_lock = _parse_int(fields[3])
            self.state.pnvs13_loop_lock = _parse_int(fields[7])
        elif sid == "22" and len(fields) >= 2:
            self.state.pnvs22_lock = fields[1].strip().upper()


def iter_nmea_lines(stream: IO[bytes] | IO[str]) -> Iterator[str]:
    for raw in stream:
        if isinstance(raw, bytes):
            line = raw.decode("ascii", errors="replace")
        else:
            line = raw
        line = line.strip()
        if line:
            yield line


def open_serial(port: str, baud: int, timeout: float):
    if serial is None:
        raise SystemExit(
            "pyserial is required for live capture. Install with: pip install pyserial"
        )
    return serial.Serial(port=port, baudrate=baud, timeout=timeout)


def _state_csv_row(state: GpsState, status_line: str) -> dict:
    return {
        "host_iso": datetime.fromtimestamp(state.host_time, tz=timezone.utc).isoformat(),
        "host_unix": f"{state.host_time:.6f}",
        "utc": state.utc_label(),
        "summary": state.lock_summary(),
        "gga_quality": state.gga_quality,
        "gga_sats": state.gga_sats,
        "gga_hdop": state.gga_hdop,
        "gsa_fix": state.gsa_fix,
        "gll_valid": state.gll_valid,
        "pnvs7_lock": state.pnvs7_lock,
        "pnvs7_error": state.pnvs7_error,
        "pnvs7_pps_diff": state.pnvs7_pps_diff,
        "pnvs7_freq_diff": state.pnvs7_freq_diff,
        "pnvs7_dac": state.pnvs7_dac,
        "pnvs10_disc_gps": state.pnvs10_disc_gps,
        "pnvs10_pps_diff": state.pnvs10_pps_diff,
        "pnvs10_pps_avg": state.pnvs10_pps_avg,
        "pnvs10_freq_var": state.pnvs10_freq_var,
        "pnvs13_gnss_lock": state.pnvs13_gnss_lock,
        "pnvs13_loop_lock": state.pnvs13_loop_lock,
        "pnvs22_lock": state.pnvs22_lock,
        "scope_delay_ns": state.scope_delay_ns,
        "status_line": status_line,
    }


def _emit_state(
    state: GpsState,
    *,
    scope: Optional[TekScopeSCPI],
    changes_only: bool,
    last_key: Optional[tuple],
    csv_writer: Optional[csv.DictWriter],
    log_file,
) -> Optional[tuple]:
    if scope is not None:
        try:
            state.scope_delay_ns = scope.read_delay_ns()
        except (OSError, ConnectionError, socket.timeout) as exc:
            print(f"Scope read failed: {exc}", file=sys.stderr)
            state.scope_delay_ns = None

    key = state.change_key()
    if changes_only and key == last_key:
        return last_key

    status_line = state.format_status_line()
    print(status_line, flush=True)

    if csv_writer is not None:
        csv_writer.writerow(_state_csv_row(state, status_line))
        log_file.flush()
    return key


def run_monitor(
    stream: IO[bytes] | IO[str],
    *,
    changes_only: bool = False,
    log_path: Optional[Path] = None,
    interval: float = 0.0,
    scope: Optional[TekScopeSCPI] = None,
) -> int:
    parser = NmeaParser()
    last_key: Optional[tuple] = None
    last_emit = 0.0
    log_file = None
    csv_writer = None

    if log_path is not None:
        log_file = log_path.open("w", newline="", encoding="utf-8")
        fieldnames = [
            "host_iso",
            "host_unix",
            "utc",
            "summary",
            "gga_quality",
            "gga_sats",
            "gga_hdop",
            "gsa_fix",
            "gll_valid",
            "pnvs7_lock",
            "pnvs7_error",
            "pnvs7_pps_diff",
            "pnvs7_freq_diff",
            "pnvs7_dac",
            "pnvs10_disc_gps",
            "pnvs10_pps_diff",
            "pnvs10_pps_avg",
            "pnvs10_freq_var",
            "pnvs13_gnss_lock",
            "pnvs13_loop_lock",
            "pnvs22_lock",
            "scope_delay_ns",
            "status_line",
        ]
        csv_writer = csv.DictWriter(log_file, fieldnames=fieldnames)
        csv_writer.writeheader()

    try:
        for line in iter_nmea_lines(stream):
            state = parser.feed_line(line)
            if state is None:
                continue

            now = time.time()
            if interval > 0 and (now - last_emit) < interval:
                continue
            last_emit = now

            key = _emit_state(
                state,
                scope=scope,
                changes_only=changes_only,
                last_key=last_key,
                csv_writer=csv_writer,
                log_file=log_file,
            )
            if key is not None:
                last_key = key

        state = parser.flush()
        if state is not None:
            key = _emit_state(
                state,
                scope=scope,
                changes_only=changes_only,
                last_key=last_key,
                csv_writer=csv_writer,
                log_file=log_file,
            )
            if key is not None:
                last_key = key
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
    finally:
        if log_file is not None:
            log_file.close()
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Monitor Novus NR3620-O/G-HS GNSS lock health from NMEA output.",
    )
    parser.add_argument(
        "--port",
        default="/dev/ttyUSB4",
        help="Serial port for live capture (default: /dev/ttyUSB4)",
    )
    parser.add_argument(
        "--baud",
        type=int,
        default=115200,
        help="Serial baud rate (default: 115200)",
    )
    parser.add_argument(
        "--file",
        type=Path,
        help="Read NMEA from a text file instead of serial (for replay/testing)",
    )
    parser.add_argument(
        "--changes-only",
        action="store_true",
        help="Print only when lock-related fields change",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=0.0,
        help="Minimum seconds between status lines (0 = emit on each GGA, ~1 Hz)",
    )
    parser.add_argument(
        "--log",
        type=Path,
        help="Append-friendly CSV log with host UTC timestamp for IRIG-B correlation",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=1.0,
        help="Serial read timeout in seconds (default: 1.0)",
    )
    scope_group = parser.add_argument_group("scope")
    scope_group.add_argument(
        "--scope",
        action="store_true",
        help="Query Tek scope for CH1->CH2 delay over TCP SCPI",
    )
    scope_group.add_argument(
        "--scope-ip",
        default="10.10.10.85",
        help="Scope IP address (default: 10.10.10.85)",
    )
    scope_group.add_argument(
        "--scope-port",
        type=int,
        default=4000,
        help="Scope TCP SCPI port (default: 4000)",
    )
    scope_group.add_argument(
        "--scope-timeout",
        type=float,
        default=5.0,
        help="Scope TCP timeout in seconds (default: 5.0)",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    scope = None
    if args.scope:
        try:
            scope = open_scope(args.scope_ip, args.scope_port, args.scope_timeout)
        except OSError as exc:
            raise SystemExit(f"Failed to connect to scope at {args.scope_ip}:{args.scope_port}: {exc}") from exc

    try:
        if args.file is not None:
            with args.file.open("r", encoding="ascii", errors="replace") as fh:
                return run_monitor(
                    fh,
                    changes_only=args.changes_only,
                    log_path=args.log,
                    interval=args.interval,
                    scope=scope,
                )

        ser = open_serial(args.port, args.baud, args.timeout)
        print(
            f"Listening on {args.port} @ {args.baud} baud "
            f"({'changes only' if args.changes_only else 'periodic ~1 Hz'})",
            file=sys.stderr,
        )
        with ser:
            return run_monitor(
                ser,
                changes_only=args.changes_only,
                log_path=args.log,
                interval=args.interval,
                scope=scope,
            )
    finally:
        if scope is not None:
            scope.close()


if __name__ == "__main__":
    raise SystemExit(main())
