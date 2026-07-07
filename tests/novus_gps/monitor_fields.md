# Novus GPS monitor — field reference

This document describes every field produced by
[`monitor_novus_gps.py`](monitor_novus_gps.py) when run with `--log`, on the
one-line status output, or both.

The monitor reads NMEA 0183 from a **Novus NR3620-O/G-HS** (default:
`/dev/ttyUSB4` @ 115200 baud). It emits one summary row per GPS second (~1 Hz),
when the next `$GNGGA` sentence marks the end of a complete NMEA cycle.
Optionally, with `--scope`, it also queries a **Tektronix MDO** oscilloscope for
CH1→CH2 delay over TCP SCPI (port 4000).

Example status line:

```
2026-06-30T182211Z | LOCKED | GGA:GPS/12s HDOP0.98 | GSA:3D | GLL:A | NVS7:lock=A sats=28 err=0x00 pps_d=+2 freq_d=+3 dac=425605 | NVS10:stab=1 disc=0 type=OCXO pps=+10ns avg=+5ns freq_var=30 | NVS22:lock=A | SCOPE:ch1_ch2=+102.3ns
```

---

## Timestamps

### `utc` (status line prefix)

Receiver time for the completed NMEA cycle.

| Aspect | Detail |
|--------|--------|
| **Source** | `$GNGGA` field 1 (hhmmss.sss); date from `$GNZDA` when present |
| **Format** | `YYYY-MM-DDThhmmss.sssZ` if date known; otherwise `hhmmss.sssZ` |
| **Meaning** | UTC time stamp reported by the GNSS receiver for that 1 s epoch |
| **Use** | Align events to GPS time; compare with scope/IRIG-B logs |

If `$GNZDA` has not yet been received, the host UTC time is used as a fallback.

### `host_iso` (CSV only)

Host wall-clock time when the status row was emitted, ISO 8601 with timezone
(e.g. `2026-06-30T18:22:11.106511+00:00`).

Use this column to correlate with external measurements (scope, FPGA IRIG-B
logs, etc.) that use the PC clock.

### `host_unix` (CSV only)

Same instant as `host_iso`, as Unix epoch seconds with microsecond resolution
(e.g. `1782843731.106511`).

---

## Overall lock summary

### `summary` — `LOCKED` / `DEGRADED` / `UNLOCKED`

Composite health label derived in software from several indicators. It is **not**
a single hardware flag on the Novus unit.

| Value | Condition (any of the “unlocked” checks forces `UNLOCKED`; else see below) |
|-------|-------------------------------------------------------------------------------|
| **UNLOCKED** | GGA fix quality = 0; or GSA fix = 1 (none); or GLL = `V`; or NVS7 lock = `V`; or NVS1 lock = `V`; or NVS22 lock = `V`; or NVS13 GNSS lock level = 0 |
| **LOCKED** | GGA quality > 0, GSA fix 2D/3D, Novus NVS7/NVS1 lock `A` (or absent), NVS13 loop lock = 1 (or absent), NVS13 GNSS level = 3 (or absent) |
| **DEGRADED** | Neither fully locked nor clearly unlocked (mixed or incomplete data) |

On the NR3620-HS, the front-panel GNSS LED also reflects Kalman/OCXO loop
health, which may disagree briefly with `summary`.

---

## Standard NMEA fields

### `GGA` — `$GNGGA` (Global Positioning System Fix Data)

Shown as `GGA:<quality>/<sats>s HDOP<hdop>`.

#### `gga_quality` / fix type label

NMEA field 6 — GPS quality indicator.

| Value | Label shown | Meaning |
|-------|---------------|---------|
| 0 | `NONE` | No fix / invalid |
| 1 | `GPS` | Standard GPS fix (SPS) |
| 2 | `DGPS` | Differential GPS |
| 4 | `RTK-FIX` | RTK fixed |
| 5 | `RTK-FLOAT` | RTK float |
| other | numeric | Other NMEA 4.x quality codes |

**Primary lock indicator:** `0` means the receiver has no valid position fix.

#### `gga_sats`

NMEA field 7 — number of satellites used in the navigation solution (not
necessarily the same as “in view”).

#### `gga_hdop`

NMEA field 8 — horizontal dilution of precision. Lower is better; values above
~2–3 suggest weaker geometry.

### `GSA` — `$GNGSA` (DOP and active satellites)

Shown as `GSA:<fix>`.

#### `gsa_fix`

NMEA field 2 — fix type for the constellation block last parsed.

| Value | Label | Meaning |
|-------|-------|---------|
| 1 | `NONE` | No fix |
| 2 | `2D` | 2D fix |
| 3 | `3D` | 3D fix |

The receiver outputs multiple `$GNGSA` sentences (one per GNSS constellation).
The monitor keeps the **last** GSA sentence processed in each 1 s cycle, which is
usually sufficient because fix type is consistent across constellations when
locked.

### `GLL` — `$GNGLL` (Geographic position)

Shown as `GLL:<flag>`.

#### `gll_valid`

NMEA field 7 — data validity.

| Value | Meaning |
|-------|---------|
| `A` | Valid |
| `V` | Invalid — navigation receiver warning |

---

## Novus proprietary `$GPNVS` fields

Novus status strings use the talker ID `$GPNVS`. String ID is the first numeric
field after `GPNVS`. Reference: Novus NR3626 / NR2310 programmer guides (NR3620
uses the same family of strings).

### `NVS7` — `$GPNVS,7` status bytes

Primary Novus timing/GNSS health string for the OCXO-disciplined reference.

| Status key | CSV column | Description |
|------------|------------|-------------|
| `lock` | `pnvs7_lock` | GPS/GNSS lock: `A` = valid, `V` = not valid |
| `sats` | *(status only)* | Satellites in view (greater of GPS or GNSS count) |
| `err` | `pnvs7_error` | Error byte, hex (`0x00` = no error) |
| `pps_d` | `pnvs7_pps_diff` | PPS difference, last count in **clock cycles** (±999) |
| `freq_d` | `pnvs7_freq_diff` | Frequency difference, last count in clock cycles |
| `dac` | *(status only)* | OCXO DAC value (integer, scale 1/2²⁰ of full range) |

`pps_d` and `freq_d` are useful for spotting disciplining stress before a full
lock loss. They are **not** nanoseconds; conversion depends on the internal
counter clock.

### `NVS10` — `$GPNVS,10` PPS alignment / stabilizer

Shown when any NVS10 field has been received.

| Status key | CSV column | Description |
|------------|------------|-------------|
| `stab` | *(status only)* | PPS stabilizer enabled: `0` = off, `1` = on |
| `disc` | `pnvs10_disc_gps` | PPS disciplining to GPS: `0` = off, `1` = actively synchronized |
| `type` | *(status only)* | PPS output type: `OCXO` (0) = synthesized OCXO PPS, `GPS` (1) = GPS PPS |
| `pps` | `pnvs10_pps_diff` | Instantaneous PPS difference vs GPS, **nanoseconds** (±250) |
| `avg` | `pnvs10_pps_avg` | Averaged PPS difference, **nanoseconds** |
| `freq_var` | `pnvs10_freq_var` | Frequency variance, clock cycles per loop period |

When the stabilizer is active (`stab=1`), the unit steers the OCXO-derived PPS
rather than hard-aligning every second; `pps` / `avg` then track residual
misalignment.

### `NVS13` — `$GPNVS,13` discipline selection

Shown only when `pnvs13_gnss_lock ≥ 0` or `pnvs13_loop_lock ≥ 0`.

| Status key | CSV column | Description |
|------------|------------|-------------|
| `gnss` | `pnvs13_gnss_lock` | GNSS lock level: `UNLOCKED` (0), `ACQUIRING` (1), `COARSE` (2), `LOCKED` (3) |
| `loop` | `pnvs13_loop_lock` | Frequency loop: `0` = acquiring, `1` = locked |
| `src` | *(status only)* | Current discipline source: `0` = GNSS, `1` = 10 MHz in, `2` = optical, `3` = holdover |

Not all NR3620 firmware builds emit `$GPNVS,13` every second; columns may stay
at `-1` in CSV.

### `NVS22` — `$GPNVS,22` (HS extended status)

| Status key | CSV column | Description |
|------------|------------|-------------|
| `lock` | `pnvs22_lock` | Extended lock flag: `A` / `V` (HS-specific; complements NVS7) |

Only the lock character is parsed; remaining NVS22 fields (temperature, frequency
error, status word) are ignored by the monitor.

### `NVS1` — `$GPNVS,1` fault bytes

Shown only when `$GPNVS,1` is present in the stream (not all NR3620 configs emit
it every second).

| Status key | CSV column | Description |
|------------|------------|-------------|
| `lock` | *(not in CSV)* | GPS lock: `A` / `V` |
| `ch_fault` | *(not in CSV)* | Channel fault byte, hex OR-mask |
| `ps_fault` | *(not in CSV)* | Power-supply fault byte, hex OR-mask |
| `err` | *(not in CSV)* | Error message byte, hex OR-mask |

---

## Oscilloscope field (`--scope`)

### `SCOPE:ch1_ch2` / `scope_delay_ns`

| Aspect | Detail |
|--------|--------|
| **Source** | Tektronix MDO, immediate measurement `MEASUrement:IMMed:VALue?` |
| **Setup** | DELAY from CH1 → CH2, both rising edges, 50 % reference level (zero-cross on symmetric sine) |
| **Units** | Nanoseconds in status line; raw float in CSV (`scope_delay_ns`) |
| **Sign** | Positive = CH2 edge occurs after CH1 edge |
| **Invalid** | Omitted or `?` if scope unreachable or Tek invalid value (≥ 1e30) |

This measures **physical** alignment at the scope probes (e.g. 10 MHz clock
zero-cross vs PPS), which may differ from Novus-reported `pps_d` / `pps` due to
cabling, FPGA, and IRIG-B downstream logic.

---

## CSV-only column

### `status_line`

Full text of the printed one-line summary for that row (duplicate of stdout).

---

## Fields parsed but not shown

These are read from NMEA into internal state but **not** included in the status
line or CSV columns.

| Internal field | Source | Why omitted |
|----------------|--------|-------------|
| `pnvs7_freq_corr` | `$GPNVS,7` field 10 | Frequency correction slice (DAC bits/s); redundant with `dac` for monitoring |
| `pnvs7_sats` | `$GPNVS,7` | Shown on status line only (`sats=`); not a separate CSV column |
| `pnvs1_sats` | `$GPNVS,1` | Satellite count from fault string; NVS7 sats preferred |
| `pnvs10_stab_en` | `$GPNVS,10` | Shown on status line only (`stab=`) |
| `pnvs10_pps_type` | `$GPNVS,10` | Encoded as `type=OCXO`/`GPS` on status line only |
| `pnvs13_disc_src` | `$GPNVS,13` | Shown on status line only (`src=`) when NVS13 block present |

---

## NMEA / Novus data not parsed

The monitor **ignores** the following data that the NR3620 may still transmit.
None of it appears in logs unless you extend the parser.

### Standard NMEA sentences (not handled)

| Sentence | Typical content not logged |
|----------|----------------------------|
| `$GNGGA` | Latitude, longitude, altitude, geoid separation, differential age |
| `$GNGSA` | PRN list (fields 3–14), PDOP, HDOP, VDOP per constellation |
| `$GNGLL` | Latitude, longitude |
| `$GNZDA` | Local time zone offset (fields 5–6) |
| `$GPGSV`, `$GAGSV`, `$GLGSV`, `$GBGSV`, `$GQGSV` | Satellites in view: PRN, elevation, azimuth, SNR (C/N₀) |
| `$GNRMC`, `$GNVTG`, `$GNGNS`, … | Position/velocity/time if enabled |

### `$GPNVS` string IDs not parsed

The NR3620-HS commonly emits additional proprietary strings. Examples from
typical captures:

| String ID | Typical content (per Novus manual) |
|-----------|-------------------------------------|
| **9** | Measured 10 MHz frequency, DAC voltage, loop period, antenna current, sine RMS (HS format) |
| **17** | Additional loop / variance telemetry |
| **23**, **24** | Small telemetry tuples (offset/drift-related) |
| **26**, **28**, **29** | Allan deviation / stability estimates (e×10ⁿ format) |
| **27** | Timing/state counters (large integer fields) |
| **3** | Power-supply rail voltages |
| **5** | Temperature / fan / sensor readings |
| **8** | Event log status (discipline counter, GPS lock achieved, time alignment mode) |

### `$GPNVS,7` fields not parsed

| Field | Content |
|-------|---------|
| Date (`mmddyy`) | Used only indirectly via other time fields |
| Power-supply voltages (fields 12–13) | `+4.38`, `-4.22` V rails in example captures |

### `$GPNVS,10` fields not parsed

Beyond the six values shown, the HS string can include PPS average count,
threshold, pull-cal factor, active time cal, variance threshold, stable-mode
post-warmup, slope indicator, slope cal factor, slope distance, and additional
trailing fields (e.g. `8000`, `50.0`, `51.8` in long HS captures).

### `$GPNVS,22` fields not parsed

All fields after the lock character: mode flags, temperatures, frequency
measurements, DAC fractional value, drift, 16-bit status word (`0x0000`).

---

## Change detection (`--changes-only`)

A row is printed/logged only when any of these change:

`summary`, `gga_quality`, `gga_sats`, `gsa_fix`, `gll_valid`, `pnvs1_lock`,
`pnvs7_lock`, `pnvs7_error`, `pnvs7_pps_diff`, `pnvs10_disc_gps`,
`pnvs10_pps_diff`, `pnvs10_pps_avg`, `pnvs13_gnss_lock`, `pnvs13_loop_lock`,
`pnvs22_lock`, and `scope_delay_ns` (rounded to 1 ns).

Fields shown on the status line but **excluded** from change detection include
`gga_hdop`, `pnvs7_freq_diff`, `pnvs10_freq_var`, `dac`, `stab`, and `type`.

---

## Related commands

```bash
# ~1 Hz log with scope delay
python3 tests/novus_gps/monitor_novus_gps.py --scope --log out.csv

# Log only on meaningful changes
python3 tests/novus_gps/monitor_novus_gps.py --scope --changes-only --log events.csv
```
