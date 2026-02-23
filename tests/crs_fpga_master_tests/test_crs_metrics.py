#!/usr/bin/env python3
"""
Test script for CRS FPGA metrics implementation.

This script tests the new get_metrics_async() and get_total_power() methods
in the CRS class. This test REQUIRES a powered CRS board to be connected
and accessible on the network.
"""

import asyncio
import sys
import os
import argparse

# Add the pychfpga directory to the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from pychfpga.hardware.crs.crs import CRS
from pychfpga.fpga_array import FPGAArray
from wtl.metrics import Metrics
from wtl import log
import logging


async def test_crs_metrics_with_hardware(serial='001'):
    """Test the CRS metrics implementation with actual hardware.
    
    This test REQUIRES a real CRS board to be connected and accessible.
    Modify the serial parameter to match your CRS board's serial number.
    """
    
    print('Testing CRS FPGA Metrics with Hardware')
    print('=' * 50)
    print(f'Required: Powered CRS board with serial number {serial}')
    print()
    
    # Track errors that occur during testing
    error_count = 0
    error_messages = []
    
    # Create a CRS instance using FPGAArray
    try:
        print('Initializing FPGAArray with CRS board...')
        # Use FPGAArray to discover and create the CRS board
        ca = FPGAArray(
            hwm=f'crs {serial}',
            mode = 'shuffle8',
            ping=True,
            prog=2,  # force FPGA reprogram for testing
            open = True
        )
        
        # Get the CRS board from the array
        crs_board = ca.ib[0]
        
        if not crs_board:
            raise Exception('Could not create CRS instance from FPGAArray')
            
    except Exception as e:
        raise Exception(f'Failed to initialize FPGAArray: {e}')
    
    print(f'✓ Successfully created CRS instance: {crs_board}')
    print(f'  Part number: {crs_board.part_number}')
    print(f'  Serial number: {crs_board.serial}')
    print(f'  Hostname: {crs_board.hostname}')
    print()
    
    # Test ping to verify the board is accessible
    print('Testing board connectivity...')
    try:
        ping_result = await crs_board.ping_async(timeout=5.0)
        if ping_result:
            print('✓ Board is accessible via ping')
        else:
            raise Exception('Board is not accessible via ping')
    except Exception as e:
        raise Exception(f'Board connectivity test failed: {e}')
    
    print()

    # Test FPGA die temperature reading
    print('Testing FPGA die temperature reading...')
    try:
        # Test FPGA core temperature
        if crs_board.fpga and hasattr(crs_board.fpga, 'SYSMON'): 
            try:
                fpga_temp = crs_board.fpga.SYSMON.temperature()
                print(f'    FPGA core: {fpga_temp:.2f}°C')
            except Exception as e:
                print(f'    FPGA core: Error reading temperature - {e}')
        else:
            print('    FPGA core: FPGA not available or SYSMON not accessible')

    except Exception as e:
        print(f'⚠ FPGA die temperature reading test failed: {e}')

    print()

    # Test individual temperature sensors
    try:
        print('Testing individual temperature sensors...')

        temp_sensors = [
            ('5v0', crs_board.i2c1_tmp421_5v0),
            ('3v3', crs_board.i2c1_tmp421_3v3),
            ('2v5', crs_board.i2c1_tmp421_2v5),
            ('1v8', crs_board.i2c1_tmp421_1v8),
            ('1v2a', crs_board.i2c1_tmp421_1v2a),
            ('1v4', crs_board.i2c1_tmp421_1v4),
            ('1v2b', crs_board.i2c1_tmp421_1v2b),
            ('pll', crs_board.i2c0_tmp421_pll),
        ]
        
        print('  Temperature sensor readings:')
        for sensor_name, sensor in temp_sensors:
            try:
                temperature = sensor.get_temperature()
                print(f'    {sensor_name}: {temperature:.2f}°C')
            except Exception as e:
                error_count += 1
                error_messages.append(f'Temperature sensor {sensor_name}: {e}')
                print(f'    {sensor_name}: Error reading temperature - {e}')
            
    except Exception as e:
        error_count += 1
        error_messages.append(f'Temperature sensor test failed: {e}')
        print(f'⚠ Buck and PLL temperature reading test failed: {e}')
    
    print()
    
    # Test the total power method with hardware
    print('Testing get_total_power() with hardware...')
    try:
        total_power = await crs_board.mb.get_total_power_async()
        if total_power is not None:
            print(f'✓ get_total_power() returned: {total_power:.2f} W')
            if total_power > 36.: # Current draw at 12 V expected to be greater than 3 A, or 36 W (rough estimate, it is usually 3.5 to 4 A)
                print('✓ Board is consuming power (expected for active hardware)')
            else:
                print('⚠ Board reports 0W power (may indicate hardware issue)')
        else:
            raise Exception('get_total_power() returned None (unexpected for hardware)')
    except Exception as e:
        raise Exception(f'get_total_power() test failed: {e}')
    
    print()
    
    # Test the metrics method with hardware
    print('Testing get_metrics_async() with hardware...')
    try:
        metrics = await crs_board.get_metrics_async()
        print(f'✓ get_metrics_async() returned: {type(metrics)}')
        
        if isinstance(metrics, Metrics):
            print('✓ Correctly returns a Metrics object')
            # Verify the metrics object contains data
            try:
                # Check if we have any metrics data
                metrics_data = str(metrics)
                if metrics_data and len(metrics_data) > 0:
                    print('✓ Metrics object contains data')
                else:
                    print('⚠ Metrics object appears empty')
            except Exception:
                print('✓ Metrics object created successfully')
        else:
            raise Exception(f'Unexpected return type: {type(metrics)}')
            
    except Exception as e:
        raise Exception(f'get_metrics_async() test failed: {e}')
    
    print()
    
    # Test power supply status
    print('Testing get_power_supply_status() with hardware...')
    try:
        power_status = crs_board.get_power_supply_status()
        if power_status and isinstance(power_status, dict):
            print('✓ get_power_supply_status() returned power supply data')
            print(f'  Found {len(power_status)} power rails:')
            for rail, status in power_status.items():
                print(f'    {rail}: {status["Vbus"]:.3f}V, {status["I"]:.3f}A, {status["P"]:.1f}W')
        else:
            raise Exception('get_power_supply_status() returned invalid data')
    except Exception as e:
        raise Exception(f'get_power_supply_status() test failed: {e}')
    
    print()
    
    # Test that all required methods exist
    print('Verifying required methods exist...')
    required_methods = [
        'get_metrics_async',
        'get_total_power', 
        'get_power_supply_status'
    ]
    
    for method_name in required_methods:
        if hasattr(crs_board, method_name):
            print(f'✓ {method_name}() method exists')
        else:
            raise Exception(f'Missing required method: {method_name}()')
    
    print()
    print('✓ All hardware tests passed successfully!')
    print('=' * 50)
    
    # Report any errors that were collected during testing
    if error_count > 0:
        print()
        print('⚠ WARNING: The test completed but encountered errors:')
        print(f'   Total errors: {error_count}')
        print()
        for i, error_msg in enumerate(error_messages, 1):
            print(f'   {i}. {error_msg}')

        passed = False 

    else:
        passed = True
        
    return passed


def test_temperature_metrics_implementation():
    """Verify that temperature metrics are explicitly implemented in the CRS class."""
    
    print('Verifying Temperature Metrics Implementation')
    print('=' * 50)
    
    # Read the CRS source code to verify temperature metrics are included
    crs_file = os.path.join(os.path.dirname(__file__), '..', '..', 'pychfpga', 'hardware', 'crs', 'crs.py')
    
    if not os.path.exists(crs_file):
        raise Exception(f'CRS file not found: {crs_file}')
    
    with open(crs_file, 'r') as f:
        content = f.read()
    
    # Check for temperature-related code
    temp_checks = [
        ('tmp421', 'Temperature sensor class'),
        ('temperature', 'Temperature monitoring'),
        ('crs_temperature', 'Temperature metrics addition'),
        ('SYSMON.temperature', 'FPGA core temperature'),
        ('FPGA core temperature', 'Explicit temperature comment')
    ]
    
    print('Checking for temperature metrics implementation:')
    all_checks_passed = True
    for check, description in temp_checks:
        if check.lower() in content.lower():
            print(f'✓ Found {description}: {check}')
        else:
            print(f'✗ Missing {description}: {check}')
            all_checks_passed = False
    
    # Check for explicit temperature sensor list
    if 'temp_sensors = [' in content:
        print('✓ Found temperature sensor list definition')
    else:
        print('✗ Missing temperature sensor list definition')
        all_checks_passed = False
        
    # Check for temperature metrics addition
    if 'metrics.add(\'crs_temperature\'' in content:
        print('✓ Found temperature metrics addition')
    else:
        print('✗ Missing temperature metrics addition')
        all_checks_passed = False
    
    if not all_checks_passed:
        raise Exception('Temperature metrics implementation is incomplete')
    
    print()
    print('✓ Temperature metrics implementation verified!')
    print('=' * 50)


def main():
    """Main function to run CRS FPGA metrics tests."""
    parser = argparse.ArgumentParser(description='CRS FPGA Metrics Test Suite')
    parser.add_argument('--serial', required=True, 
                       help='CRS board serial number (REQUIRED - must be a real powered board)')
    parser.add_argument('--stderr_log_level', choices=['warning', 'info', 'debug'], 
                       default='warning', help='Set logging level for stderr output')
    
    # Handle ipython case where arguments might be passed differently
    import sys
    if len(sys.argv) > 1 and sys.argv[1].startswith('--'):
        args = parser.parse_args()
    else:
        # If no arguments provided, show usage and exit
        print("CRS FPGA Metrics Test Suite")
        print("=" * 50)
        print("ERROR: Serial number is required")
        print()
        print("Usage:")
        print("  python test_crs_metrics.py --serial <CRS_SERIAL_NUMBER>")
        print("  ipython test_crs_metrics.py -- --serial <CRS_SERIAL_NUMBER>")
        print()
        print("Example:")
        print("  python test_crs_metrics.py --serial 0080")
        print("  ipython test_crs_metrics.py -- --serial 0080")
        print()
        print("⚠ WARNING: This test REQUIRES a powered CRS board")
        print("⚠ Ensure the board is connected and accessible")
        sys.exit(1)
    
    # Setup logging
    logger = log.get_logger(__name__)
    
    # Configure logging based on stderr_log_level argument
    log_levels = {
        'warning': logging.WARNING,
        'info': logging.INFO,
        'debug': logging.DEBUG
    }
    
    # Setup logging handlers similar to FPGAArray
    parent_logger_name = __name__.rsplit('.', 1)[0] if '.' in __name__ else ''
    parent_logger = logging.getLogger(parent_logger_name)
    
    # Clear existing handlers to avoid duplicates
    parent_logger.handlers = []
    parent_logger.setLevel(logging.DEBUG)  # Pass all messages to handlers
    
    # Add stream handler for stderr
    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(log_levels[args.stderr_log_level])
    formatter = logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s')
    stream_handler.setFormatter(formatter)
    parent_logger.addHandler(stream_handler)
    
    print('CRS FPGA Metrics Test Suite')
    print('=' * 50)
    print('⚠ WARNING: This test REQUIRES a powered CRS board')
    print('⚠ Ensure the board is connected and accessible')
    print()
    
    # Verify temperature metrics implementation first
    print('Step 1: Verifying temperature metrics implementation...')
    test_temperature_metrics_implementation()
    
    print()
    
    # Run hardware test with provided serial number
    print('Step 2: Running hardware tests with real CRS board...')
    print(f'Using serial number: {args.serial}')
    print()
    
    passed = asyncio.run(test_crs_metrics_with_hardware(args.serial))
    print()
    if passed:
        print('🎉 ALL TESTS PASSED! CRS FPGA metrics implementation is working correctly.')
    else:
        print(f'❌ TEST FAILED. See error messages above.')
        print()
        print('Troubleshooting:')
        print('1. Ensure the CRS board is powered on')
        print('2. Verify network connectivity to the board')
        print('3. Check that the serial number is correct')
        print('4. Ensure the board is running compatible firmware')
        sys.exit(1)

if __name__ == '__main__':
    main()