# Cline Development Logbook

This is a running log of development work done with Cline on the pychfpga project.

---

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
