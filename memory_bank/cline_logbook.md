# Cline Development Logbook

This is a running log of development work done with Cline on the pychfpga project.

---

## August 21, 2025 - Network Settings Verification Integration

### Task: Integrate Network Configuration Verification

**Objective**: Ensure optimal network settings for high-rate UDP data reception in both pocket correlator and periscope correlator applications.

#### Created Network Utilities Module (`pychfpga/network_utils.py`)

**Features Implemented**:
- **Interface MTU Detection**: Automatically detects network interfaces and checks for jumbo frame support (MTU ≥ 9000)
- **UDP Buffer Verification**: Checks critical UDP buffer settings:
  - `net.core.rmem_max` and `net.core.rmem_default` ≥ 26214400
  - `net.ipv4.udp_mem` values ≥ 26214400
  - `net.ipv4.udp_rmem_min` ≥ 26214400
- **Smart Interface Selection**: Prioritizes interfaces with jumbo frames already enabled, falls back to second interface (typically the desired one)
- **Actionable Error Messages**: Provides exact sysctl commands needed to fix configuration issues
- **Command-line Interface**: Can be used standalone with `--status` flag or `--interface` selection
- **Multiple Detection Methods**: Uses ip/ifconfig commands and /sys/class/net fallbacks for maximum compatibility

#### Integration Points

**Pocket Correlator (`pychfpga/pocket_correlator.py`)**:
- **Pre-connection Verification**: Network settings are checked in the `__init__` method before attempting hardware connection
- **Blocking Behavior**: Raises `RuntimeError` if settings are not properly configured, preventing operation with suboptimal network configuration
- **Clear User Feedback**: Displays detailed error messages and exact fix commands if issues are found

**Periscope Correlator App (`pychfpga/apps/periscope_correlator/app.py`)**:
- **GUI Integration**: Network verification occurs in the `connect_to_board()` method with GUI feedback
- **Warning Dialog**: Shows detailed warning dialog if network settings are suboptimal, but allows user to continue
- **Non-blocking Behavior**: Warns user but continues with connection attempt, suitable for GUI applications

#### Key Features

- **Comprehensive Detection**: Handles various Linux network interface naming conventions and detection methods
- **User-friendly Output**: Provides clear ✓/✗ status indicators and specific fix instructions
- **Robust Error Handling**: Gracefully handles missing commands or interfaces
- **Zero Dependencies**: Uses only standard library modules for maximum compatibility

#### Usage Examples

**Command Line Usage**:
```bash
# Show status of all network interfaces and UDP settings
python -m pychfpga.network_utils --status

# Check specific interface
python -m pychfpga.network_utils --interface eth0

# Default behavior - verify settings and show fix commands if needed
python -m pychfpga.network_utils
```

**Programmatic Usage**:
```python
from pychfpga.network_utils import verify_network_settings

all_ok, errors, fix_commands = verify_network_settings()

if not all_ok:
    print("Network configuration issues found:")
    for error in errors:
        print(f"  - {error}")
    
    print("\nTo fix these issues, run:")
    for cmd in fix_commands:
        print(f"  {cmd}")
else:
    print("✓ All network settings are correctly configured!")
```

#### Implementation Details

**Network Interface Detection Strategy**:
1. **Primary Method**: Uses `ip link show` command to get interface list and MTU values
2. **Fallback Method**: Reads from `/sys/class/net/` directory structure
3. **Interface Selection**: Prioritizes interfaces with jumbo frames enabled, otherwise selects second interface (avoiding loopback)

**UDP Buffer Settings Verification**:
The module checks for optimal UDP buffer sizes required for high-rate data reception:
- **Receive Memory Buffers**: `net.core.rmem_max` and `net.core.rmem_default` set to 26214400 bytes (25MB)
- **UDP Memory Management**: `net.ipv4.udp_mem` configured for high-throughput scenarios
- **Minimum Receive Buffer**: `net.ipv4.udp_rmem_min` ensures adequate per-socket buffering

**Error Handling and Compatibility**:
- Gracefully handles missing system commands (`ip`, `ifconfig`, `sysctl`)
- Provides fallback methods for different Linux distributions
- Returns actionable error messages with exact commands needed for fixes
- Maintains compatibility across different network interface naming conventions

#### Benefits

The network verification ensures that both applications will have optimal UDP reception capabilities for high-rate data streams, preventing packet loss that could compromise correlator data quality. Users receive clear guidance on how to configure their systems properly before attempting hardware connections.

This integration provides:
- **Proactive Problem Prevention**: Issues are caught before attempting data acquisition
- **Clear Remediation**: Users get exact commands to fix configuration problems  
- **Improved Reliability**: Reduces packet loss and data corruption during operation
- **Better User Experience**: Clear feedback instead of mysterious connection failures

**Files Created/Modified**:
- `pychfpga/network_utils.py` - New network utilities module
- `pychfpga/pocket_correlator.py` - Added network verification
- `pychfpga/apps/periscope_correlator/app.py` - Added GUI network verification

---

## Development Notes

- All implementations maintain backward compatibility
- Code follows existing project patterns and conventions
- Comprehensive error handling and user feedback implemented
- Both command-line and GUI applications supported appropriately

--

## September 1 4, 2025 - Nyquist Zone Switching Implementation

### Task: Add Nyquist Zone Support to Periscope Correlator

**Objective**: Enable the periscope correlator app to display data from different Nyquist zones (Zone 1: 0-1600 MHz, Zone 2: 1600-3200 MHz, Zone 3: 3200-4800 MHz) by adding a GUI control that updates plot frequency axes without changing data processing.

**Implementation Details**:

#### Nyquist Zone Frequency Ranges
Based on sampling rate of 3200 MHz (FS_HZ = 3.2 GHz):
- **Zone 1**: 0 MHz to 1600 MHz (first Nyquist zone)
- **Zone 2**: 1600 MHz to 3200 MHz (second Nyquist zone, aliased/reversed)
- **Zone 3**: 3200 MHz to 4800 MHz (third Nyquist zone)

#### Changes Made to `pychfpga/apps/periscope_correlator/app.py`:

1. **Added Nyquist Zone Selector UI**:
   - Added combo box in toolbar with zone options
   - Connected to `_on_nyquist_zone_changed()` handler

2. **Implemented Frequency Axis Calculation**:
   - `_calculate_frequency_axis(zone)` method handles zone-specific frequency mapping
   - Zone 2 properly handles frequency reversal due to aliasing
   - Zone 3 shifts frequency range appropriately

3. **Dynamic Frequency Updates**:
   - `_update_all_frequency_axes(zone)` updates both regular and super FFT axes
   - `current_nyquist_zone` tracks active zone
   - All plot methods use zone-adjusted frequency axes

4. **Real-time Zone Switching**:
   - Immediate plot updates when zone changes
   - No impact on data capture or signal processing
   - Status bar feedback for user confirmation

**Key Features**:
- ✅ GUI control for zone selection
- ✅ Proper frequency axis calculation for all zones
- ✅ Real-time plot updates without data processing changes
- ✅ Support for all plot types (spectra, waterfall, lag, super FFT)
- ✅ Correct handling of Zone 2 aliasing (frequency reversal)

**Files Modified**:
- `pychfpga/apps/periscope_correlator/app.py` - Main implementation

---


## February 22, 2026 - CRS FPGA Metrics Implementation

### Task: Add FPGA Metrics Support to CRS Platform

**Objective**: Complete the FPGA metrics implementation for the CRS platform by implementing the `get_metrics_async()` method and `get_total_power()` method to provide comprehensive hardware monitoring data.

**Current State Analysis**:
- CRS class has extensive I2C device setup with power monitoring sensors (ina231_list)
- Hardware monitoring infrastructure is in place but metrics methods are incomplete
- `get_metrics_async()` method exists but only calls parent class without adding CRS-specific data
- `get_total_power()` method is missing entirely

**Implementation Plan**:

#### Complete Hardware Monitoring (FPGA Metrics)

**Key Components to Implement**:

1. **Complete `get_metrics_async()` method**:
   - Add CRS-specific power rail monitoring from `i2c1_ina231_list`
   - **Include temperature sensor data** 
   - Add PLL status and reference clock information
   - Include FPGA core temperature from `self.fpga.SYSMON.temperature()`
   - Add QSFP status and GPIO information

2. **Implement `get_total_power()` method**:
   - Calculate total power consumption from all monitored rails
   - Sum power from all entries in `i2c1_ina231_list`
   - Return total power in watts as float

3. **Enhanced Metrics Categories**:
   - **Power Metrics**: Individual rail voltages, currents, and power consumption
   - **Temperature Metrics**: Board temperatures from tmp421 sensors
   - **FPGA Metrics**: Core temperature, PLL status, reference clock selection
   - **Interface Metrics**: QSFP presence, GPIO states

**Technical Details**:

**Power Monitoring Infrastructure**:
```python
# Existing power sensor configuration in CRS class
self.i2c1_ina231_list = {
    'vbp': dict(device=self.i2c1_ina231_vbp, rshunt=0.01, imax=10),
    '0v85a': dict(device=self.i2c1_ina231_0v85a, rshunt=0.01, imax=40),
    '0v85b': dict(device=self.i2c1_ina231_0v85b, rshunt=0.01, imax=40),
    '5v0': dict(device=self.i2c1_ina231_5v0, rshunt=0.01, imax=16),
    '3v3': dict(device=self.i2c1_ina231_3v3, rshunt=0.01, imax=16),
    '2v5': dict(device=self.i2c1_ina231_2v5, rshunt=0.01, imax=16),
    '1v8': dict(device=self.i2c1_ina231_1v8, rshunt=0.01, imax=16),
    '1v2a': dict(device=self.i2c1_ina231_1v2a, rshunt=0.01, imax=16),
    '1v4': dict(device=self.i2c1_ina231_1v4, rshunt=0.01, imax=16),
    '1v2b': dict(device=self.i2c1_ina231_1v2b, rshunt=0.01, imax=16),
}
```

**Expected Metrics Output**:
- Individual rail metrics: voltage, current, power, shunt voltage
- Total system power consumption
- Temperature readings from multiple sensors
- FPGA core temperature
- PLL reference clock status
- QSFP module presence and status

**Integration Points**:
- Metrics will integrate with existing pychfpga monitoring infrastructure
- Compatible with existing Metrics class from `wtl.metrics`
- Will work with fpga_array and fpga_master for system-wide monitoring

**Files to Modify**:
- `pychfpga/hardware/crs/crs.py` - Complete metrics implementation

### Task: Complete FPGA Metrics Support for CRS Platform

**Objective**: Implement comprehensive FPGA metrics support for the CRS platform including `get_metrics_async()` method, `get_total_power_async()` method, and hardware initialization.

**Status**: ✅ **COMPLETED**

**Implementation Summary**:

#### 1. Core Methods Implemented
- **`get_metrics_async()`**: Comprehensive hardware monitoring including power rails, temperature sensors, PLL status, QSFP status, and GPIO states
- **`get_total_power_async()`**: Reads from 'vbp' rail only (backplane voltage) for accurate total power measurement
- **`open_hw_async()`**: Complete I2C device initialization for all sensors and interfaces

#### 2. Hardware Monitoring Coverage
- **Power**: All 10 power rails with voltage, current, power, and shunt voltage
- **Temperature**: 8 temperature sensors plus FPGA core temperature
- **PLL**: Active input tracking and reference clock information  
- **QSFP**: Module presence and control signals
- **GPIO**: QSFP control interface states

#### 3. Test Suite Features
- **Hardware-required testing**: Absolutely requires powered CRS board
- **Error tracking system**: Collects and reports temperature sensor errors at test end
- **Fixed argument parsing**: Works with ipython usage (`-- --serial` syntax)
- **Comprehensive verification**: Tests all hardware components

#### 4. Problem Resolution
- **Solved**: "AttributeError: 'ina230' object has no attribute 'current_lsb'" by proper I2C device initialization

#### 5. Files Modified
- `pychfpga/hardware/crs/crs.py`: Added hardware initialization and metrics methods
- `tests/crs_fpga_master_tests/test_crs_metrics.py`: Complete rewrite with error tracking

#### 6. Current Status
✅ **FPGA die temperature and total power complete**
⚠️ **Individual temperature sensors**: individual temperatures of i2c devices reporting errors, needs to be debugged
⚠️ **Next Step**: Integrate with `fpga_master.py` and test system-wide monitoring

**Usage**:
```bash
# Test with hardware
python3 tests/crs_fpga_master_tests/test_crs_metrics.py --serial 0080 

---

## February 23, 2026 - CRS Metrics Integration Planning

### Task: Integrate CRS Metrics into FPGA Master

**Objective**: Integrate the existing CRS platform metrics into the fpga_master monitoring system by creating a unified metrics API that combines CRS hardware monitoring with existing FPGA firmware metrics.

**Current State**:
- CRS platform has comprehensive hardware monitoring implemented in `crs.py`
- `get_metrics_async()` method provides power, temperature, PLL, and QSFP metrics
- `get_total_power_async()` method provides total power consumption
- Need to integrate with existing `fpga_array.py` and `fpga_master.py` metrics collection

**Implementation Plan**:
1. **Update CRS metrics pattern**: Modify `_get_motherboard_metrics_async()` to return `(info, metrics)` tuple like iceboard.py
2. **Create CRS metrics API**: Add `get_crs_metrics_async()` method to `chfpga.py` following existing patterns
3. **Integrate with fpga_array**: Modify `get_fpga_metrics_async()` to include CRS metrics collection
4. **System-wide exposure**: Ensure metrics are accessible through existing fpga_master endpoints

**Key Integration Points**:
- CRS metrics will be accessible through existing fpga_master monitoring endpoints
- Platform detection with helpful debugging error messages
- Backward compatibility with non-CRS platforms
- Comprehensive hardware monitoring including power, temperature, PLL status, and QSFP status

**Files to Modify**:
- `pychfpga/hardware/crs/crs.py` - Update metrics pattern
- `pychfpga/fpga_firmware/chfpga/chfpga.py` - Add CRS metrics API
- `pychfpga/fpga_array.py` - Integrate CRS metrics collection

**Expected Outcome**: Complete system-wide monitoring that provides both FPGA firmware status and CRS hardware platform health through unified metrics endpoints.

---

## February 23, 2026 - CRS Metrics Implementation & FPGA Error Fixes

### Task: Complete CRS Metrics Integration and Fix FPGA Attribute Errors

**Objective**: Implement comprehensive CRS metrics API in chfpga.py and fix critical AttributeError issues in FPGA communication methods.

**Implementation Summary**:

#### 1. CRS Metrics API Implementation
- **Added `get_crs_metrics_async()` method** to `chfpga.py` following existing patterns
- **Platform detection** with helpful error messages for debugging
- **ARM-based metrics collection** separate from firmware-dependent metrics
- **Two-tier metrics system**: ARM-only metrics (always available) and firmware metrics (when FPGA programmed). Fixed the issues from yesterday where all CRS metrics were going to be published together, which was incorrect. Now the split between ARM and FPGA metrics is cleaner.
- **Integration with fpga_array.py** via modified `get_fpga_metrics_async()` method

#### 2. FPGA Error Fixes
- **Fixed `clear_fpga_udp_errors()` method**: Added support for both FPGAMmi (has `error_counter`) and TCPipe_BSB_MMI (has `recv_counter`)
- **Fixed `get_crossbar_metrics_async()` method**: Corrected crossbar object access from `self.CROSSBAR` to `self.CT.CROSSBAR` (corner-turn engine)
- **Enhanced error handling** with proper try-catch blocks and logging

#### 3. Test Suite Updates
- **Updated `test_crs_metrics.py`** to work with new `(info, metrics)` tuple format
- **Added comprehensive error tracking** for temperature sensor debugging
- **Fixed argument parsing** for ipython usage (`-- --serial` syntax)

#### 4. Key Features Implemented
- **Comprehensive hardware monitoring**: Power rails, temperature sensors, PLL status, QSFP status
- **FPGA firmware metrics**: Core temperature, UDP communication errors, crossbar metrics
- **System-wide integration**: Metrics accessible through existing fpga_master endpoints
- **Backward compatibility**: Non-CRS platforms unaffected

#### 5. Files Modified
- `pychfpga/fpga_firmware/chfpga/chfpga.py`: Added CRS metrics API and fixed FPGA errors
- `pychfpga/fpga_array.py`: Integrated CRS metrics collection
- `pychfpga/hardware/crs/crs.py`: Updated metrics pattern to return `(info, metrics)` tuple
- `tests/crs_fpga_master_tests/test_crs_metrics.py`: Updated test format and error tracking

#### 5. Current Status
✅ **CRS metrics API complete and FPGA errors fixed**
**Next Steps**: Test integration with fpga_master and verify system-wide monitoring functionality
**Metrics that need to be added**: Need to introduce ADC (e.g., overrange and overvoltage from GPIO) metrics

**Usage**:
```python
# CRS metrics accessible through existing fpga_master endpoints
metrics = await fpga_master.get_metrics_async()
# Returns comprehensive hardware and firmware monitoring data
```

---

## October 9, 2026 - QC f_engine_tests Fixes After Branch Merge

### Task: Verify `qc/CRS_crate/f_engine_tests.py` against merged API and fix breakages

**Objective**: After merging another developer's long-running branch, confirm the QC tests still work (correct API calls) and fix anything that would prevent them from running.

**Fixes Applied**:
- **psucontrol dependency**: pinned `pyproject.toml` to `git+https://github.com/t0/psucontrol.git@main` and reinstalled into `conda_venv312` (stale cached wheel had no `api.py`). Note: `@main` is mutable; an exact-SHA pin would be more reproducible.
- **PSU API rename** (main blocker): `status()` → `get_status()`, `control_power(state=X)` → `set_power(X)` across all live call sites; passed configured `timeout` into `PSUController(...)`.
- **connect_psu**: raises a clear `RuntimeError` on failure instead of returning `None` (avoids later `NoneType` AttributeError).
- **test_power_cycle**: removed undefined `'ib': i` in the sync-exception tag; added missing `sync_fails += 1` so the pass/fail assertion counts sync failures.
- **Crate API**: `Crate.get_all_instances()` → `get_all_registered_instances()` (test_network).
- **test_network cleanup**: `open_ps()` → `connect_psu()`; use boolean `['output_on']` key (no `['status']`); removed dead `ic = self.ca.ic`; fixed `tuple(n, s)` → `(n, s)`.

**Files Modified**:
- `qc/CRS_crate/f_engine_tests.py`
- `pyproject.toml` (psucontrol pin)

**Status**: ✅ Verified with `python -m py_compile`. Full end-to-end run still requires live crate + PSU hardware.

---

## September 18, 2026 - Power-Cycle Test Iteration Watchdog

### Task: Stop a hardware freeze from stalling `test_power_cycle`

**Objective**: A (currently undiagnosed) hardware problem intermittently freezes an iteration of the power cycle test, hanging the whole run. Rather than diagnosing it now, bound each iteration in time: if it takes too long, assume a hardware issue, kill the current cycle and move on to the next iteration.

**Why a signal-based watchdog**: The freeze occurs *inside* a blocking call (e.g. `asyncio.run(...)` on an FPGA transaction), so control never returns and simply measuring elapsed time afterwards cannot help. A `SIGALRM` timer fires *while* the call is blocked and raises an exception that unwinds it. `asyncio.wait_for` was rejected as it would require rewriting every `asyncio.run` call site and would not cover synchronous code; threads were rejected since Python cannot force-kill one.

#### Changes to `qc/CRS_crate/f_engine_tests.py`

- **`IterationTimeout(RuntimeError)`**: new exception raised when an iteration exceeds its time limit.
- **`TestUtils.iteration_watchdog(timeout, label)`**: a `@contextmanager` that arms `signal.alarm(timeout)`, raises `IterationTimeout` from the SIGALRM handler, and always disarms the timer and restores the previous handler on exit (so an alarm can never leak into the next iteration or into teardown). `timeout <= 0` disables it, and it degrades to a no-op with a warning if SIGALRM cannot be armed (non-main thread / non-Unix).
- **`test_power_cycle`**: the hardware-facing part of each iteration (crate init + sync + per-board ADC/UDP checks) is wrapped in the watchdog. A new `except IterationTimeout` handler logs the event, increments `timeout_fails`, records `timeout_err_tags` (cycle + elapsed seconds), and lets the loop continue to the next iteration.
- **`except IterationTimeout: raise` guards** added ahead of the pre-existing broad `except Exception` handlers (init, sync, checks, board close) and inside `_psu_call`, so the watchdog exception is never swallowed or retried past its deadline.
- **Duration tracking**: `iter_durations` records every iteration; the per-cycle summary prints the current/mean/max duration and a `Timed-out (frozen) iterations` counter, and the end-of-test report prints mean/min/max.
- **Pass/fail**: `timeout_fails` is included in the final `max(...) <= n_fails_accept` assertion, and `timeout_fails` / `timeout_err_tags` / `iter_durations` are saved to `test_results`.

**Deliberately not done**: no board-close cleanup after a timeout. The board is presumed wedged (closing it could hang again), and the PSU power-off at the end of the iteration — which runs *outside* the watchdog, so it is never interrupted — tears down the crate and those connections anyway.

#### Changes to `qc/CRS_crate/test_config.yaml`

- New `f_engine_tests.p_cycle_test.iter_timeout: 300` (seconds). Covers crate init + sync + checks, excludes the `t_cycle` cool-down. `0` disables the watchdog.

**Files Modified**:
- `qc/CRS_crate/f_engine_tests.py`
- `qc/CRS_crate/test_config.yaml`

**Validation**: `python -m py_compile` clean; YAML parses. `git diff -w` confirms the only removed lines are a reflowed comment and the old assert (the large diff is re-indentation from the new `with` block). Verified off-hardware with two scripts: a unit-level check that the watchdog interrupts a frozen `asyncio.run`, restores the handler, leaks no alarm, and is disabled at `timeout=0`; and an end-to-end simulation of the real `test_power_cycle` with mocked hardware where iteration 2 of 4 freezes — it was aborted after ~2 s, counted once, and all 4 iterations ran.

**Limitations**: SIGALRM is Unix-only and main-thread-only (fine for this CLI/pytest harness; it no-ops with a warning otherwise). Resolution is 1 second (`signal.alarm` takes an int). The 300 s default should be re-tuned against observed healthy iteration times on the bench. The underlying hardware freeze remains undiagnosed by design.

---

## September 22, 2026 - Oversized QC Report Recovery and XML_PARSE_HUGE Fix

### Task: Make the SN011 summary reflect the 1000-cycle power-cycle test, and fix the underlying report-size limit

**Objective**: The 1000-cycle `test_power_cycle` run (43 h, 2026-09-18) PASSED, but the `.pdf`/`.rst`/`.xml` summaries at `~/qc_test_results/CRS_crate/CRS_crate_SN011/` still showed the earlier 2026-09-11 run, which had FAILED. Bring the summaries up to date without reading the 79 MB XML / 85 MB HTML in full, then fix the root cause in the reporting plugin.

#### Root Cause

The run produced a 79,195,600-byte JUnit XML whose captured log lives in just two text nodes: `<system-out>` ≈ 54.8 MB and `<system-err>` ≈ 24.4 MB (759,033 lines, 314,043 of them `INFO`). libxml2 caps a single text node at 10 MB, so `TestCases.load_test_cases()` raised:

```
XMLSyntaxError: Resource limit exceeded: Text node too long, try XML_PARSE_HUGE
```

Critically, that method globs `data/*.xml` and parses **every** file in one loop with no error handling, so the single oversized file aborted collection of *all* of the unit's results — hence the silently stale summaries. Note the `.html` report is not consumed by the summary generator at all, which is why it stayed valid.

#### Part 1 - Recovery: `qc/tools/condense_xreport_xml.py` (new)

A standalone, stdlib-only CLI that rewrites an oversized report into a small but still valid JUnit XML:

- Streams the input line by line, so memory use is independent of file size; only retained lines are accumulated.
- Copies the markup-bearing lines byte-for-byte (only 3 in this file: lines 1, 445015, 759033), so every `<testsuites>`/`<testsuite>`/`<testcase>` attribute — pass/fail state, timestamps, durations — is preserved exactly.
- Keeps: a banner stating how many lines were dropped and where the full log lives, the head of the set-up log, the first iteration verbatim, the per-iteration error tallies of **every** iteration, every ERROR/CRITICAL/traceback/timeout line, and the final results. Tunable via `--log-head`, `--err-tail`, `--keep-first-iterations`.
- `verify()` re-parses the output with ElementTree before returning, so the tool cannot leave behind a file that breaks the loader.

Result: **79,195,600 → 322,129 bytes (0.41 %) in 1.3 s**, 759,033 → 10,330 lines, retaining all 1000 iteration banners, all 1000 tally blocks, the 11 `{'Cycle': ...}` error tags, the 9 `IterationTimeout` records and the final duration stats.

Because the loader globs `*.xml`, the original was renamed `...test_power_cycle.raw_xml` (not deleted, byte count re-verified) and the digest written to the original `.xml` path. Summaries were then regenerated through the production path — `TestMenu(...).update_summary_report()`, since menu key `R` is dead in xreport 1.2 (`Unsupported action in this version of Xreport`).

**Outcome**: all five required tests now show PASS, with `test_power_cycle` at `2026-09-18 17:55:16`. Summaries went from stale 9.06 MB/1800 pp to 1.86 MB/418 pp (PDF), 6.64 → 1.32 MB (RST), 6.82 → 1.32 MB (XML), generated in under 60 s. The previous summaries were kept as `*.bak`.

#### Part 2 - Root-cause fix in `wtl.pytest-xreport` (separate repo: `~/wtl.pytest-xreport`, branch `jfc/dev`, tag 1.2)

Applied to `wtl/pytest_xreport/xreport.py` (+26/-2):

- **`XML_PARSER = lxml.objectify.makeparser(huge_tree=True)`** at module level, and `lxml.objectify.parse(file_, parser=XML_PARSER)` at the single parse site in `TestCases.load_test_cases()`. `huge_tree=True` **is** lxml's binding for libxml2's `XML_PARSE_HUGE`; there is no separate flag or constant to set, so setting both would be redundant — this is documented in the code comment. The parser is built once, not per file, since it is reusable and sits in a loop.
- **Per-file `except lxml.etree.XMLSyntaxError`** around the parse, which logs a warning and `continue`s. This is the other half of the bug: without it, one unreadable file discards the results of every other test for that unit and the summary silently shows stale data. The catch is deliberately narrow so real bugs still surface.

Confirmed this is the only parse site in the package; the sole other lxml use is `lxml.etree.tostring()` in `save_xml()`, which is serialization and unaffected by parser limits.

**Validation** (against the real 79 MB file, lxml 6.0.2 / libxml2 2.14.6):

| Check | Result |
|---|---|
| Shipped parser on the 79 MB file | reproduces the exact reported error, `line 66704, column 181` |
| Patched parser, same file | parses in **0.1 s**, 79,002,107 chars, `failures=0`, PASS |
| `TestCases` on a folder mixing the oversized file, normal files and a deliberately corrupt file | oversized file loads; corrupt file only warns; all other results still collected |
| Full 5-file data folder, unabridged | 0.1 s, all 5 PASS |
| `save_xml()` / `generate_test_summary()` → RST | 0.5 s / **7.2 s** (76 MB RST) |

The package's own `tests/` suite fails with `ModuleNotFoundError: No module named 'wtl.namespace'`, but `git stash` confirmed it fails identically on the unmodified clone — pre-existing namespace-package shadowing, not a regression (those tests are also cookiecutter leftovers for a `--foo`/`bar` fixture this plugin does not have).

**Known remaining limitation — `rst2pdf`, not parsing**: PDF rendering time is superlinear, roughly 3× per doubling (0.5 MB → 0.5 s; 1 MB → 1.1 s; 2 MB → 2.9 s; 4 MB → 9.0 s). A full 76 MB summary was killed at **9 minutes / 20.7 GB RSS and still climbing**. RST assembly itself is fine (80 MB in 6.9 s at 0.4 GB), so the bottleneck is purely rst2pdf. Also note `rST.write()` always appends the format implied by the output filename extension, so a `.pdf` target renders a PDF regardless of `output_formats`. Until this is addressed, oversized runs still need the condenser to produce a PDF.

**Deliberately not done**: lowering the pychfpga log level during the cycle loop was considered and rejected — the INFO output is wanted, as it carries the detail needed when parsing errors after a long run. `f_engine_tests.py` was therefore left untouched and the report-size problem is handled at the reporting layer instead.

**Files Modified**:
- `qc/tools/condense_xreport_xml.py` (new), `qc/tools/README.txt` (new)
- `~/wtl.pytest-xreport/wtl/pytest_xreport/xreport.py` (separate repo, uncommitted)

**Status**: ✅ Summaries up to date and verified; `huge_tree` fix verified end to end against real data. Open follow-ups: commit/push the xreport change, and decide how to bound PDF generation (literal-block size cap in `generate_test_summary()`, a size threshold that skips the PDF, or continued use of the condenser).

---

## September 24, 2026 - ADC quick-check packet-loss investigation

### Task: Diagnose "dropped packets" in `f-engine-tools` `quick_check_adc.ipynb`

**Objective**: User reported heavy packet loss running
`f-engine-tools/notebooks/general_notebooks/quick_check_adc.ipynb` despite correct UDP sysctls and
jumbo frames. Investigate root cause and fix.

**Setup**: Host `twiddlebug`; crate under test = CRC4 SN011 (McGill), 16 boards on `192.168.88.x`,
received on **`ens1f0`** (Intel `ixgbe`, 10G, `192.168.88.17/24`). A *separate* crate lives on
`enp6s0` (Realtek r8169, 2.5G, `10.10.10.x`) and was excluded per the user. Capture engine is
**UCAP** (mode 0 = 2 frames/burst, all 8 ADC channels). Data path: notebook
`get_data_receiver_async()` -> threaded `UDPPacketReceiver` -> `RawFrameReceiver.read_raw_frames()` ->
`RawDataPacketProcessor` assembly.

#### Findings

1. **`ignore_missing_boards` vs `ignore_missing_slots` (notebook fix).** `FPGAArray(...)` parameter
   is `ignore_missing_boards`; the notebook passed `ignore_missing_slots`, which is swallowed by
   `**kwargs` and silently no-ops, so the "missing slots" `RuntimeError` still fired for <16 boards.
   Fixed notebook to compute `ignore_missing_boards = n_boards != 16` and pass the correct kwarg.

2. **"50% loss" at 8 boards was a reporting bug, not real loss (notebook fix).**
   `read_raw_frames(split=True)` returns **per-frame** counts (each row = one frame,
   max = `NUMBER_OF_SUBFRAMES` = 4), and rows are already doubled (`ncap * FRAMES_PER_BURST`). The
   old formula divided by `4*2 = 8`, so a perfect capture read as exactly 50%. Fixed to
   `percent_packets_captured_adc = 100 * c_adc / r.NUMBER_OF_SUBFRAMES`. With `flush=True` + the fixed
   formula, **8 boards capture at a consistent 100%**. Denominator rule: `split=True` -> divide by
   `NUMBER_OF_SUBFRAMES` (4); `split=False` -> divide by `FRAMES_PER_BURST * NUMBER_OF_SUBFRAMES` (8).

3. **16-board loss is real and upstream of the host (~16.6%/burst).** Live run: every UDP batch =
   854 packets, all passing filters (`match=854 after_ts_filter=854`); a full 16-board burst =
   `packets_per_timestamp` = 128 streams x 2 frames x 4 subframes = **1024**, so 854/1024 = 83.4%
   (~16.6% lost/burst). Assembler carryover pins at `receive_window`=3 (rows held open for packets
   that never arrive) - the fingerprint of genuine on-wire loss. **NIC ring counters did not move**
   during capture (`rx_missed_errors` stayed 3460; `rx_no_buffer_count`/`rx_dropped`/
   `rx_no_dma_resources` = 0) -> host NIC is not dropping; loss is at the switch/wire.


4. **Burst structure (pcap `/home/ih/ens1f0_dump0.pcap`).** 19 bursts spaced **exactly 100.0 ms**
   (= `burst_rate=0.1`); all 16 boards start each burst within **~100-160 us** (effectively
   simultaneous). Per burst ~ **8.4 MB delivered in ~150 us**, then idle ~99.85 ms. Average rate
   ~ **590 Mbit/s** (fits in 1G). Frames are all 8244 B - **no frames >9000, no IP fragmentation**
   (jumbo path clean).

5. **Corrected "1G vs 10G" understanding.** Not an average-bandwidth problem (avg ~590 Mbit/s fits
   1G). Loss occurs when the **switch egress buffer toward the host** overflows during the ~150 us
   simultaneous 16-board microburst. Limiting resource = **switch egress buffer depth**, not host
   link headline speed; 10G *helps* (drains the switch queue ~10x faster). Old 1G success was almost
   certainly because fewer boards (e.g. 4-8) and/or non-aligned bursts meant the switch never had to
   buffer 16 simultaneous streams (4 boards ~ 2 MB/burst fits; 16 boards ~ 8.4 MB does not). The
   earlier phrasing "16 boards saturate the 10G link" was imprecise.

6. **Concurrent second receiver (contention).** `ss -p` showed our capture socket
   `192.168.88.17:48746` (pid 234956, user `ih`) plus two full sockets `:50018`/`:52980`
   (pid 1386495, user **`jfcliche`**) - another user's kernel capturing the SAME crate concurrently,
   compounding the microburst loss. No processes were killed (they belong to another user).

#### Changes applied
- **Notebook** `quick_check_adc.ipynb`: board-count cell now sets `ignore_missing_boards = n_boards
  != 16` and passes the correct kwarg; percentage cell now `100*c_adc/(r.NUMBER_OF_SUBFRAMES)`; using
  `read_raw_frames(..., split=True, flush=True)`.
- **Host NIC tuning on `ens1f0`** (absorbs what reaches the host; does NOT fix upstream loss):
  RX ring 512 -> **4096** (`ethtool -G ens1f0 rx 4096`); coalescing `rx-usecs` 1 -> **50**
  (`ethtool -C ens1f0 rx-usecs 50`); flow control (802.3x PAUSE) confirmed RX on / TX on. Persisted
  via systemd oneshot **`/etc/systemd/system/nic-tuning-ens1f0.service`** (enabled); re-apply with
  `sudo systemctl restart nic-tuning-ens1f0.service`. Result: NIC-ring drops eliminated; 16-board
  loss unchanged (upstream/switch). UDP sysctls (rmem_max/default = 100 MB) and MTU 9000 were already
  correct.

#### Recommended remedies for 16-board capture (in order)
1. Ensure no other user (e.g. `jfcliche`) is capturing the same crate concurrently while testing.
2. **Capture in two groups of 8 boards** (8 proven lossless), then stitch - reliable software fix now.
3. Switch-side: verify PAUSE is honored toward `192.168.88.17` and/or deeper egress buffers.
4. **Per-slot transmit stagger/delays** so 16 boards don't burst simultaneously - proper long-term
   fix, needs UCAP firmware support (`offset`/`send_delay` are PROBER-only today).
5. Faster/bonded host link (25G, or 2x10G LACP) - definitive hardware fix.

#### Open questions / TODO
- **[OPEN] Why `c_adc` showed max 8 in some earlier runs despite always `split=True`.** The
  "split=False sums to 8" theory is wrong (user never changed split/mode). Likely
  `r.NUMBER_OF_SUBFRAMES` was **8** (not 4) in a prior firmware/config state
  (`subframes_per_frame` comes from the capture `State`). ACTION: log `r.NUMBER_OF_SUBFRAMES` and
  `r.FRAMES_PER_BURST` live per configuration to confirm 4/2 for UCAP mode 0.
- **[OPEN] Per-slot transmit delays for UCAP** - investigate a firmware-exposed per-board start-frame
  offset (equivalent to PROBER `offset`/`send_delay`) to stagger the 16-board microburst at source.
- **[OPEN] Library hardening for unknown kwargs** - decide whether `FPGAArray` should raise on
  unrecognized kwargs (e.g. `ignore_missing_slots`) instead of silently swallowing them; complicated
  by legitimate `**kwargs` pass-through to `array_init_async` -> firmware `init_async`
  (`processing_frequency`, `reference_frequency`, `data_width`, legacy `open`). Not applied.
- **[OPEN] Quantify switch buffer / confirm PAUSE honored** on the switch port toward the host.
- **[OPEN] Orphaned capture sockets** across kernel restarts (multiple full UDP sockets bound to
  `192.168.88.17`). Consider closing sockets on receiver teardown.

#### Useful diagnostics
- Per-burst completeness: compare `match=`/`after_ts_filter=` vs full burst = 1024 (16 boards).
- NIC drops: `ethtool -S ens1f0 | grep -E 'rx_missed|rx_no_buffer|rx_dropped|rx_no_dma'`.
- Kernel UDP drops: `nstat | grep -i UdpRcvbufErrors`; `ss -u -a -n` (Recv-Q).
- Socket owners: `sudo ss -u -a -n -p | grep 192.168.88.17`.
- pcap: `tshark -r ens1f0_dump0.pcap -q -z conv,udp` / frame-length / timing analysis.

**Status**: 8-board capture verified lossless (reporting bug + `ignore_missing_boards` fixed);
host NIC tuning applied and persisted. 16-board residual loss confirmed as upstream switch-egress
tail-drop from simultaneous microbursts (not host-side) - remedies above pending.


---

## October 1, 2026 - Pocket Correlator async rewrite (new firmware + receivers)

### Task: Make `pychfpga/pocket_correlator.py` fully asynchronous and align it with the new firmware, channelizer-config API, and `common/` receiver classes

**Objective**: Upgrade `POCKET_CORRELATOR` to use the new async array/channelizer/correlator APIs,
delete obsolete `bin_map` / `OUTPUT_SOURCE_SEL` logic, modernize raw and correlated data capture via
the `common/` receivers, and surface per-capture health info (ADC flags, UCAP overrun, packet loss).

#### Construction (now fully async)
- Construct with `pc = await POCKET_CORRELATOR('crs 0016', prog=1)` (IPython/Jupyter) or
  `asyncio.run(POCKET_CORRELATOR(...).init())` in scripts. `__init__` only stashes kwargs;
  `init_async` + `__await__` + `init` do the real work.
- Builds the array via `await FPGAArray(..., mode='corr8')`; gets `self.r` (raw) and `self.cr`
  (correlator) receivers from `get_data_receiver_async()` / `get_corr_receiver_async()` (shared
  packet receiver; raw vs. correlator packets distinguished by header).
- Added **invariant channelizer class constants** applied in every capture: `FFT_SHIFT`,
  `SCALER_ROUNDING_MODE`, `SCALER_EIGHT_BIT`, `SCALER_SYMMETRIC_SATURATION`,
  `OFFSET_BINARY_ENCODING`. Only `data_source` / `fft_bypass` / `scaler_bypass` / `data_type` vary
  per capture. `set_default_channelizers()` restores the correlator-ready state.
- UCAP mode 0 by default, but robust to other modes via `npols_for_mode()` and
  `n_frames_per_burst(mode)` (no hardcoded frames-per-burst / NPOLS).

#### Removed obsolete logic
- All `UCAP.OUTPUT_SOURCE_SEL` usage, `make_bin_map` / `make_inv_bin_map`, `set_ucap_mode`,
  `n_frames_per_capture`, `run_async`, the old `u_receiver` / `corr_receiver`, manual even/odd FFT
  stitching, and `NCORR_PROD`. Bin ordering (ascending frequency) and fft_2x deinterleaving are now
  done by the receivers.

#### Raw capture methods (async, with health checks)
- New shared private helper `_configure_and_capture()` implements the
  `f-engine-tools/useful_scripts/capture_data.py` recipe (set_channelizers_async(sync=False) ->
  start_data_capture_async -> sync_async -> UCAP overrun check -> reset/read ADC flags ->
  read_raw_frames -> dropped-packet check).
- `read_adc_frames`, `read_fft_frames`, `read_post_gain_frames`, `read_scaler_frames` are now async,
  return `(data, info)`, and accept `check_adc_flags` / `check_ucap_overrun` / `check_packets` /
  `raise_on_issue` (opt-in raise so the user can still inspect imperfect data). Ported check helpers:
  `check_ucap_overrun`, `reset_adc_flags`, `read_adc_flags`, `check_dropped_packets`. Added
  `adc_rms_power_dBm()` (uses `np.std`, matching `snapshot_signal_quality.py`).

#### Gains
- `compute_gains` (async) retries on ADC flags using the returned `info`, sets gains in natural
  order, reads back a **single floating-point gain value per bin**, returns
  `(d_fft, d_fft_mean_mag, gain, d_sclr)`. `save_gains` writes one `gain` dataset (float64).

#### Correlated data
- `configure_corr` (async) uses `start_correlators_async`, caches `NPROD` / `NDECIM` /
  `firmware_integ_period` / `expected_packets`, builds `self.prod` from `cr.get_ij_to_prod_map()`
  (upper-triangle; verified 36/36 unique products for corr8), and `self.good_bin_mask`.
- `read_corr_frames` (async) restores correlator-ready channelizers only when a prior raw capture
  changed them, pulses `UCORR.OVERRUN_RESET`, and returns
  `(integ_ts, d_corr, count, sat, info)` - the **real hardware timestamp** (`integ_ts`), not a host
  `time.time()` stamp. Packet loss checked against `cr.expected_packets`.

#### observe()
- Async; awaits all captures. New-format gain load with back-compat for old `gain_lin`/`gain_log`.
  Uses `firmware_integ_period` for integration-time math. HDF5 now includes
  `index_map/good_bin_mask`, `stats/ucap_overrun`, and `vis.attrs['expected_packets']`; the `time`
  dataset now stores `integ_ts`. Fixed pre-existing bugs: ADC overrange read from `ADC_OV` (now
  `ADC_OR`), inconsistent bit ordering (now via shared `read_adc_flags`), `sat_buf[n] = sat` (now
  `sat[0]`), and removed the unused `early_timestamp_error`.

#### CLI
- Replaced `create_pc()` with `parse_args()` + `async def main()` run under `asyncio.run`, adding an
  `--observe` path (`--data_path`, `--obs_tag`, `--digital_gains_path`). An object created inside
  `asyncio.run` can't be reused after the loop closes, so interactive use is documented as awaiting
  the class directly.

#### Call sites
- Updated `tests/crs_corr_tests/pocket_correlator/test_pocket_correlator_observe.py` to
  `asyncio.run(main())` with `await POCKET_CORRELATOR(...)` / `await pc.observe(...)`.

#### Verification (offline only)
- `python -m py_compile` passes for the module and the test script; module imports cleanly;
  `observe` and `read_corr_frames` confirmed to be coroutines; product-map inversion verified
  (36 unique upper-triangle products) against `UCORR.get_ij_to_prod_map(8)` in the
  `pychfpga_venv312` env. **Not run on hardware** - board-level capture/visibility behavior still
  needs a run on a CRS board. (`pytest` is not installed in `pychfpga_venv312`.)

### Follow-up TODOs (deferred)

- **`save_gains()` should read the active gains itself.** It currently requires the caller to pass
  the `gain` array. A cleaner design would have `save_gains()` read the currently-active gains
  directly via `SCALER.get_gain_table(bank)` (looping over channels) rather than relying on the
  caller to pass them, removing a chance for the saved gains to disagree with what is on the FPGA.
- **Digital gains are now stored as a single floating-point value per bin** (dataset `gain`, shape
  `(NPOLS, NBINS)`, natural/ascending frequency order), replacing the old split `gain_lin` /
  `gain_log` datasets. Offline loaders must be updated accordingly.
- **Add helpers to process the correlator `integ_ts`.** `read_corr_frames()` now returns the real
  hardware integration timestamp (`integ_ts`) instead of a host `time.time()` stamp. It would be
  useful to add methods to convert `integ_ts` (frame counts) into wall-clock / UTC time, and to
  compute per-result integration midpoints.
- **IMPORTANT / REQUIRED: `integ_ts` is only a frame count and is NOT yet an absolute time.** To make
  it physically meaningful we must capture an absolute reference for frame 0 (``frame0_time``),
  ideally derived from a GNSS receiver / PPS disciplining the crate sync, and combine it with the
  frame period to turn `integ_ts` into true UTC. This is essential for science-ready data (correct
  fringe/phase vs. time). Plan to add a `frame0_time` acquisition path and store it in the HDF5 files
  alongside `integ_ts`. For users without a GNSS receiver, offer an *optional, clearly-labelled
  less-accurate fallback* that reads frame-0 time from the control machine's clock at sync time.
  Not implemented yet — do this before trusting observe() timestamps.
- **`apps/periscope_correlator/app.py` still uses the old synchronous POCKET_CORRELATOR API**
  (sync construction, `pc.ucap.OUTPUT_SOURCE_SEL`, `pc.corr_receiver.socket`, old gain/return
  signatures). It needs its own independent async overhaul (deferred, out of scope for this pass).

