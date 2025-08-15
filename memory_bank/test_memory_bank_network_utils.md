# Network Settings Verification Integration

Network settings verification has been successfully integrated into both the pocket correlator and periscope correlator applications. Here's what was implemented:

## Created Network Utilities Module (`pychfpga/network_utils.py`)

- **Interface MTU Detection**: Automatically detects network interfaces and checks for jumbo frame support (MTU ≥ 9000)
- **UDP Buffer Verification**: Checks critical UDP buffer settings:
  - `net.core.rmem_max` and `net.core.rmem_default` ≥ 26214400
  - `net.ipv4.udp_mem` values ≥ 26214400
  - `net.ipv4.udp_rmem_min` ≥ 26214400
- **Smart Interface Selection**: Prioritizes interfaces with jumbo frames already enabled, falls back to second interface (typically the desired one)
- **Actionable Error Messages**: Provides exact sysctl commands needed to fix configuration issues
- **Command-line Interface**: Can be used standalone with `--status` flag or `--interface` selection
- **Multiple Detection Methods**: Uses ip/ifconfig commands and /sys/class/net fallbacks for maximum compatibility

## Integration Points

### Pocket Correlator (`pychfpga/pocket_correlator.py`)

- **Pre-connection Verification**: Network settings are checked in the `__init__` method before attempting hardware connection
- **Blocking Behavior**: Raises `RuntimeError` if settings are not properly configured, preventing operation with suboptimal network configuration
- **Clear User Feedback**: Displays detailed error messages and exact fix commands if issues are found

### Periscope Correlator App (`pychfpga/apps/periscope_correlator/app.py`)

- **GUI Integration**: Network verification occurs in the `connect_to_board()` method with GUI feedback
- **Warning Dialog**: Shows detailed warning dialog if network settings are suboptimal, but allows user to continue
- **Non-blocking Behavior**: Warns user but continues with connection attempt, suitable for GUI applications

## Key Features

- **Comprehensive Detection**: Handles various Linux network interface naming conventions and detection methods
- **User-friendly Output**: Provides clear ✓/✗ status indicators and specific fix instructions
- **Robust Error Handling**: Gracefully handles missing commands or interfaces
- **Zero Dependencies**: Uses only standard library modules for maximum compatibility

## Usage Examples

### Command Line Usage

```bash
# Show status of all network interfaces and UDP settings
python -m pychfpga.network_utils --status

# Check specific interface
python -m pychfpga.network_utils --interface eth0

# Default behavior - verify settings and show fix commands if needed
python -m pychfpga.network_utils
```

### Programmatic Usage

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

## Implementation Details

### Network Interface Detection Strategy

1. **Primary Method**: Uses `ip link show` command to get interface list and MTU values
2. **Fallback Method**: Reads from `/sys/class/net/` directory structure
3. **Interface Selection**: Prioritizes interfaces with jumbo frames enabled, otherwise selects second interface (avoiding loopback)

### UDP Buffer Settings Verification

The module checks for optimal UDP buffer sizes required for high-rate data reception:

- **Receive Memory Buffers**: `net.core.rmem_max` and `net.core.rmem_default` set to 26214400 bytes (25MB)
- **UDP Memory Management**: `net.ipv4.udp_mem` configured for high-throughput scenarios
- **Minimum Receive Buffer**: `net.ipv4.udp_rmem_min` ensures adequate per-socket buffering

### Error Handling and Compatibility

- Gracefully handles missing system commands (`ip`, `ifconfig`, `sysctl`)
- Provides fallback methods for different Linux distributions
- Returns actionable error messages with exact commands needed for fixes
- Maintains compatibility across different network interface naming conventions

## Benefits

The network verification ensures that both applications will have optimal UDP reception capabilities for high-rate data streams, preventing packet loss that could compromise correlator data quality. Users receive clear guidance on how to configure their systems properly before attempting hardware connections.

This integration provides:
- **Proactive Problem Prevention**: Issues are caught before attempting data acquisition
- **Clear Remediation**: Users get exact commands to fix configuration problems  
- **Improved Reliability**: Reduces packet loss and data corruption during operation
- **Better User Experience**: Clear feedback instead of mysterious connection failures
