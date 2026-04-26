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
