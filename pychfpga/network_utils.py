"""
Network utilities for verifying system settings required for UDP data reception.

This module provides functions to check if the system is properly configured
for receiving high-rate UDP data streams without packet loss.
"""

import subprocess
import re
import os
import sys
from typing import Dict, List, Tuple, Optional
from pychfpga.mdns_discovery import mdns_resolve


def get_network_interfaces() -> List[str]:
    """
    Get list of available network interfaces.
    
    Returns:
        List of interface names
    """
    try:
        # Use ip command if available (modern Linux)
        result = subprocess.run(['ip', 'link', 'show'], 
                              capture_output=True, text=True, check=True)
        interfaces = []
        for line in result.stdout.split('\n'):
            if ': ' in line and not line.strip().startswith(' '):
                # Extract interface name from lines like "2: eno1: <BROADCAST,MULTICAST,UP,LOWER_UP>"
                parts = line.split(': ')
                if len(parts) >= 2:
                    interface = parts[1].split('@')[0]  # Remove @vlan info if present
                    if interface not in ['lo']:  # Skip loopback
                        interfaces.append(interface)
        return interfaces
    except (subprocess.CalledProcessError, FileNotFoundError):
        # Fallback to /sys/class/net
        try:
            interfaces = []
            net_dir = '/sys/class/net'
            if os.path.exists(net_dir):
                for interface in os.listdir(net_dir):
                    if interface != 'lo':  # Skip loopback
                        interfaces.append(interface)
            return interfaces
        except:
            # Last resort - common interface names
            # return ['eth0', 'eno1', 'enp0s3', 'wlan0']
            pass


def check_interface_mtu(interface: str) -> Tuple[bool, int]:
    """
    Check if the specified interface has jumbo frames enabled (MTU >= 9000).
    
    Args:
        interface: Network interface name (e.g., 'eno1')
        
    Returns:
        Tuple of (is_jumbo_enabled, current_mtu)
    """
    try:
        # Try ip command first
        result = subprocess.run(['ip', 'link', 'show', interface], 
                              capture_output=True, text=True, check=True)
        # Look for mtu in output like "mtu 9000"
        mtu_match = re.search(r'mtu (\d+)', result.stdout)
        if mtu_match:
            mtu = int(mtu_match.group(1))
            return mtu >= 9000, mtu
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    
    try:
        # Fallback to ifconfig
        result = subprocess.run(['ifconfig', interface], 
                              capture_output=True, text=True, check=True)
        # Look for MTU in output like "MTU:9000" or "mtu 9000"
        mtu_match = re.search(r'[Mm][Tt][Uu]:?\s*(\d+)', result.stdout)
        if mtu_match:
            mtu = int(mtu_match.group(1))
            return mtu >= 9000, mtu
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    
    try:
        # Try reading from /sys/class/net
        mtu_file = f'/sys/class/net/{interface}/mtu'
        if os.path.exists(mtu_file):
            with open(mtu_file, 'r') as f:
                mtu = int(f.read().strip())
                return mtu >= 9000, mtu
    except:
        pass
    
    # Could not determine MTU
    return False, 0


def get_interface_for_ip(target_ip: str) -> Optional[str]:
    """
    Determine which network interface can reach the target IP.
    Uses 'ip route get' command to find the appropriate interface.
    
    Args:
        target_ip: Target IP address to check routing for
        
    Returns:
        Interface name that can reach the IP, or None if not found
    """
    try:
        # Use ip route get to find which interface would be used to reach the IP
        result = subprocess.run(['ip', 'route', 'get', target_ip], 
                              capture_output=True, text=True, check=True)
        
        # Look for "dev <interface>" in output like:
        # "192.168.1.100 dev eno1 src 192.168.1.10 uid 1000"
        dev_match = re.search(r'dev\s+(\S+)', result.stdout)
        if dev_match:
            interface = dev_match.group(1)
            print(f'Found interface {interface} can reach {target_ip}')
            return interface
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    
    try:
        # Fallback: try route command
        result = subprocess.run(['route', 'get', target_ip], 
                              capture_output=True, text=True, check=True)
        
        # Look for "interface:" line in route output
        interface_match = re.search(r'interface:\s*(\S+)', result.stdout)
        if interface_match:
            interface = interface_match.group(1)
            print(f'Found interface {interface} can reach {target_ip}')
            return interface
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    
    print(f'Could not determine interface for IP {target_ip}')
    return None


def check_udp_buffer_settings() -> Dict[str, Tuple[bool, int, int]]:
    """
    Check UDP buffer memory settings.
    
    Returns:
        Dictionary with keys: 'rmem_max', 'rmem_default', 'udp_mem', 'udp_rmem_min'
        Values are tuples of (is_sufficient, current_value, required_value)
    """
    required_values = {
        'rmem_max': 26214400,
        'rmem_default': 26214400,
        'udp_rmem_min': 26214400
    }
    
    results = {}
    
    def get_sysctl_value(param_name: str) -> Optional[str]:
        """Get sysctl value using multiple methods."""
        # Method 1: Try different sysctl command locations
        sysctl_paths = ['sysctl', '/sbin/sysctl', '/usr/sbin/sysctl']
        for sysctl_cmd in sysctl_paths:
            try:
                result = subprocess.run([sysctl_cmd, '-n', param_name], 
                                      capture_output=True, text=True, check=True)
                return result.stdout.strip()
            except (subprocess.CalledProcessError, FileNotFoundError):
                continue
        
        # Method 2: Read directly from /proc/sys
        try:
            proc_path = f"/proc/sys/{param_name.replace('.', '/')}"
            with open(proc_path, 'r') as f:
                return f.read().strip()
        except (OSError, IOError):
            pass
        
        return None
    
    # Check individual sysctl values
    for param, required in required_values.items():
        sysctl_name = f'net.core.{param}' if param.startswith('rmem') else f'net.ipv4.{param}'
        value_str = get_sysctl_value(sysctl_name)
        
        if value_str is not None:
            try:
                current = int(value_str)
                results[param] = (current >= required, current, required)
            except ValueError:
                results[param] = (False, 0, required)
        else:
            results[param] = (False, 0, required)
    
    # Check udp_mem (has 3 values)
    value_str = get_sysctl_value('net.ipv4.udp_mem')
    if value_str is not None:
        try:
            # Handle both space and tab separated values
            udp_mem_values = [int(x) for x in value_str.split()]
            # All three values should be >= 26214400
            required_udp_mem = 26214400
            is_sufficient = all(val >= required_udp_mem for val in udp_mem_values)
            results['udp_mem'] = (is_sufficient, udp_mem_values, [required_udp_mem] * 3)
        except ValueError:
            results['udp_mem'] = (False, [0, 0, 0], [26214400, 26214400, 26214400])
    else:
        results['udp_mem'] = (False, [0, 0, 0], [26214400, 26214400, 26214400])
    
    return results


def find_best_interface(serial_number: str = None) -> Optional[str]:
    """
    Find the best network interface for hardware communication.
    If serial_number is provided, discovers the CRS board and finds the interface that can reach it.
    Otherwise, prioritizes interfaces with jumbo frames already enabled.
    
    Args:
        serial_number: Serial number of CRS board to discover (e.g., '0016')
        
    Returns:
        Interface name or None if no suitable interface found
    """
    # If serial number provided, try to discover CRS board first
    if serial_number:
        print(f'Looking for CRS board with serial number {serial_number}...')
        hostname = f'crs{serial_number.zfill(4)}.local'  # Ensure 4-digit format like crs0016.local
        
        try:
            ip_address = mdns_resolve(hostname, timeout=3)
            if ip_address:
                print(f'Found CRS board {hostname} at {ip_address}')
                interface = get_interface_for_ip(ip_address)
                if interface:
                    print(f'Using interface {interface} to reach CRS board')
                    print('')
                    return interface
                else:
                    print(f'Could not determine interface for CRS board at {ip_address}')
            else:
                print(f'CRS board {hostname} not found via mDNS')
        except Exception as e:
            print(f'Error during CRS board discovery: {e}')
        
        print('Falling back to standard interface detection...')
    
    # Standard logic: get available interfaces
    interfaces = get_network_interfaces()
    
    # First, look for interfaces with jumbo frames already enabled
    for interface in interfaces:
        is_jumbo, mtu = check_interface_mtu(interface)
        if is_jumbo:
            print(f'Using interface: {interface}')
            print('')
            return interface
    
    # If no jumbo-enabled interfaces, return the first available,
    # which is usually the one we want
    if interfaces:
        print('No interfaces found with jumbo frames enabled.')
        print(f'Assuming the first interface is best: {interfaces[0]}')
        print('')
        return interfaces[0] 
    
    return None


def verify_network_settings(interface: str = None) -> Tuple[bool, List[str], List[str]]:
    """
    Verify all network settings required for UDP data reception.
    
    Args:
        interface: Specific interface to check. If None, finds best available.
        
    Returns:
        Tuple of (all_ok, error_messages, fix_commands)
    """
    errors = []
    fix_commands = []
    
    # Determine interface to check
    if interface is None:
        interface = find_best_interface()
        if interface is None:
            errors.append("No network interfaces found")
            return False, errors, fix_commands
    
    # Check interface MTU
    is_jumbo, current_mtu = check_interface_mtu(interface)
    if not is_jumbo:
        errors.append(f"Interface {interface} MTU is {current_mtu}, should be >= 9000 for jumbo frames")
        fix_commands.append(f"sudo ifconfig {interface} mtu 9000")
    
    # Check UDP buffer settings
    buffer_settings = check_udp_buffer_settings()
    
    for param, (is_ok, current, required) in buffer_settings.items():
        if not is_ok:
            if param == 'udp_mem':
                current_str = ' '.join(map(str, current))
                required_str = ' '.join(map(str, required))
                errors.append(f"UDP memory setting net.ipv4.udp_mem is [{current_str}], should be [{required_str}]")
                fix_commands.append(f"sudo sysctl -w net.ipv4.udp_mem='{required_str}'")
            else:
                sysctl_name = f'net.core.{param}' if param.startswith('rmem') else f'net.ipv4.{param}'
                errors.append(f"UDP buffer setting {sysctl_name} is {current}, should be >= {required}")
                fix_commands.append(f"sudo sysctl -w {sysctl_name}={required}")
    
    all_ok = len(errors) == 0
    return all_ok, errors, fix_commands


def show_network_status(interface: str = None) -> None:
    """
    Print current network configuration status.
    
    Args:
        interface: Specific interface to check. If None, checks all.
    """
    print("Network Configuration Status")
    print("=" * 40)
    
    if interface:
        interfaces = [interface]
    else:
        interfaces = get_network_interfaces()
    
    # Check interfaces
    print("\nNetwork Interfaces:")
    for iface in interfaces:
        is_jumbo, mtu = check_interface_mtu(iface)
        status = "✓ JUMBO" if is_jumbo else "✗ standard"
        print(f"  {iface}: MTU {mtu} ({status})")
    
    # Check UDP buffer settings
    print("\nUDP Buffer Settings:")
    buffer_settings = check_udp_buffer_settings()
    
    for param, (is_ok, current, required) in buffer_settings.items():
        status = "✓" if is_ok else "✗"
        if param == 'udp_mem':
            current_str = ' '.join(map(str, current))
            required_str = ' '.join(map(str, required))
            print(f"  {status} net.ipv4.udp_mem: [{current_str}] (required: [{required_str}])")
        else:
            sysctl_name = f'net.core.{param}' if param.startswith('rmem') else f'net.ipv4.{param}'
            print(f"  {status} {sysctl_name}: {current} (required: >= {required})")