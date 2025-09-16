#!/usr/bin/env python

""" Performs bench tests of the MGADC08 CHIME ADC Mezzanine board.
"""
import unittest
import time
import numpy as np
import matplotlib
import importlib
# matplotlib.use("tkagg")
from matplotlib import pyplot as plt
import base64
import datetime
import textwrap
import sys
import os
import subprocess
import re

from socket import timeout

# Pypi packages
import pytest
from pytest_html import extras

# McGill packages
from wtl.namespace import NameSpace
from wtl.pytest_xreport import xr, TestMenu
import labpy
import pychfpga

from pychfpga import fpga_array 
from pychfpga.common import run_async, async_to_sync
from pychfpga.hardware.ice.icecore import hw



utils = __import__('ice-qc.utils') #import doens't like the dash in the name
TestUtils = utils.utils.TestUtils

if os.environ.get("PYTEST_PLUGINS") is not None and os.environ.get("PYTEST_PLUGINS") != "":
    os.environ["PYTEST_PLUGINS"] += ",wtl.pytest_xreport"
else:
    os.environ["PYTEST_PLUGINS"] = "wtl.pytest_xreport"

TEST_CONFIG_FILE = './test_config.yaml'

def wrap(obj, width=80):
    return textwrap.fill(str(obj), width)
"""
class TestUtils:
"""

"""
    def open_instrument(self, name):
        "" Open an instrument defined in the instrument list in the config file""
        instr_params = self.cfg.instruments[name].copy()
        class_name = instr_params.pop('labpy_object')
        print(f'calling open instrument on class {class_name} with parameters {instr_params}')
        return labpy.open_instrument(class_name, **instr_params)

    def open_ps(self, name='ps18v', state=True):
        " Open and setup the power supply for the motherboard if is in the instrument list and manual operation is not forced

        Note: the following instance attributes must exist when this method is called: 
            self.ps18v (can be None)
            self.xr
        "

        # if supply is already turned on
        if 'ps18v' in self.cfg.instruments and not self.cfg.get('manual_ps', False):
            self.ps18v = self.open_instruments(name) 
        else:
            self.ps18v = None

        # initialize power supply if we have one
        if self.ps18v:
            # configure the power supply if it is not already powered on
            if self.ps18v.status()['status'] != 'OK' or self.instr.ps18v.status()['power'] < 1:
                self.ps18v.set_output(state=False) #Ensuring power on N5764A is off
                self.ps18v.clear() #Clearing any previous protection
                self.ps18v.set_voltage(voltage=cfg.vlt) #Setting voltage to 18V, power still off
                self.ps18v.set_current_limit(current=cfg.curlmt, ocp=True) #Setting current limit and turning on ocp feature
        return self.ps18v

    def set_ps_output(self, state=False):
        if self.ps18v:
            if state:
                self.xr.input('Press [ENTER] when ready to power ON the Iceboard')
                self.ps18v.set_output(state=True)
            else:
                self.ps18v.set_output(state=False) #Ensuring power on N5764A is off

        else:
            if state:
                self.xr.input('Turn ON power to the Iceboard and press [ENTER]')
            else:
                self.xr.input('Turn OFF power to the Iceboard and press [ENTER]')
"""

class TestMGADC08Bench(TestUtils):
    """
    Perform impedance & power tests on the MGADC08 Mezzanine.
    """

    @pytest.fixture(autouse=True)
    def setUp(self, xr, instruments):

        """ Prepare the test for execution. This fixture is executed automatically before each test.
        """
        xr.header('Setting-up')
        self.cfg = xr.config  # get the test config NameSpace
        self.params = xr.params
        
        self.instruments = instruments
        

        # pre-define instrument variable. We'll load them only as needed by the tests.
        # self.ps = None

        # self.ps3v3_2v5 = self.open_instrument('ps3v3_2v5')

        # self.dmm = self.open_instrument('dmm')

        self.ps16v = None
        self.ps12v = None
        self.ps = None
        yield  # pass control to the test and return
        if self.ps16v:
            self.ps16v.close()
        if self.ps12v:
            self.ps12v.close()
        if self.ps:
            self.ps.close()



        """
        xr.header('Setting-up')
        self.cfg = xr.config
        cfg = self.cfg.bench_tests.setup  # config options pertaining to setup
        self.dmm = self.open_instrument('dmm')

        self.dmm.set_beeper(True)
        self.dmm.display('Ready for','MGADC08 tests')

        # Disable power supply outputs
        #self.adc_ps_output_enable(False)

        # Set-up power supply voltages and current limits
        self.rails = NameSpace()
        for rail_name, rail_info in list(cfg.rails.items()):
            instr = self.open_instrument(rail_info.instr_name)
            instr.output_enable(False)
            print('Configuring rail %s to %.3fV@%.3fA' % (rail_name, rail_info.voltage, rail_info.current_limit))
            instr.set_voltage(rail.output, rail.voltage)
            instr.set_current(rail.output, rail.current_limit)
            self.rails[rail_name] = instr
        xr.header('Test results')"""

    ##def adc_ps_output_enable(self, status):
    #
     #   for instr in self.cfg.rails.values():
     #       instr.output_enable(status)

    def test_impedance(self, xr):
        """
        QC001: Power supply Impedance: checks for power supply shorts

        Procedure:

          - Start the impedance test on the computer
          - Type in serial number of tested board.
          - Clip ground probe of multimeter on specified grounding point
          - Touch each test points indicated by the software and press ENTER
          - Test points:

               - J7-2 (12V)
               - J7-3 (2.5V)
               - J7-4 (3.3V)
               - 1V8 test point
               - 3V3 test point

          - Total time: 20 s

        We pass/fail the test only after all measurements are done so we can gather more debugging info.
        """
        # Useful shortcuts
        cfg = self.cfg.bench_tests.impedance
        dmm = self.open_instrument('dmm') # Multimeter

        dmm.set_beeper(True)
        test_results = NameSpace()

        print('-------------------------------')
        print(' Make Sure that ')
        print('   - the board under test is NOT connected on the motherboard')
        print('   - the power cable is NOT connected on the board under test')
        print()
        print(' Connect the Quick SMA to any of the 10 SMA inputs to provide a ground for the impedance measurements')

        #self.adc_ps_output_enable(False)

        failed_test_points = []
        passed = False
        try:
            test_results.test_points = NameSpace()
            for tp_name, limits in cfg.test_points:
                limits = NameSpace(limits)
                dmm.display('','Measure %s' % tp_name)
                dmm.select_resistance_measurement()
                while True:
                    dmm.local()
                    xr.input("Apply probe to test point '%s' and press ENTER to measure (Q=Exit):" % tp_name)
                    if limits.delay:
                        time.sleep(limits.delay)
                    result = dmm.get_resistance()
                    if result <= cfg.max_impedance: break
                    print('Impedance is too high. Is the probe really connected?')
                dmm.beep()
                passed = result > limits.zmin
                test_results.test_points[tp_name] = NameSpace(Z=result, passed=passed)
                if not passed:
                    failed_test_points.append(tp_name)
                    dmm.beep()
                    time.sleep(0.1)
                    dmm.beep()
                print('   %s : %.0f ohms (must be more than %.0f ohms) ==> %s' % (tp_name, result, limits.zmin, xr.pass_fail(passed)))
            assert not len(failed_test_points), 'Low impedance on %s' % ','.join(failed_test_points)
            passed = True
        finally:
            test_results.passed = passed
            dmm.display(xr.pass_fail(passed),'Impedance tests')
            dmm.local()
            xr.save_data(test_results)


    def test_smoke(self, xr):
        """
        QC002: Smoke test: tests currents and voltages

            - Connect power cable to mezzanine
            - Start current tests on the computer.
            - The computer will set-up the power-supply, enable the outputs, and measure currents. The power will turn off immediately if there is any problem.
            - The computer will ask if the 3.3V LED (DS7) is ON. Answer.
            - Touch multimeter probe on the test points indicated by the software and press ENTER
            - Test points:

                - 1V8 test point
                - 3V3 test point

            - Total time: 15 s
        """
        # Useful shortcuts
        cfg = self.cfg.bench_tests.smoke_test
        dmm = self.open_instrument('dmm')  # Multimeter

        self.ps12v = self.open_instrument('ps12v')
        ps3v3_2v5 = self.open_instrument('ps3v3_2v5')

        self.pss = [self.ps12v, ps3v3_2v5] #dual power supply
        
        ps_config = NameSpace(self.cfg.bench_tests.setup.rails)
        self.ps12v.set_output(voltage=ps_config.vcc12v.voltage, current=ps_config.vcc12v.voltage)

       
        ps3v3_2v5.set_voltage(output=1, voltage=ps_config.vcc3v3.voltage)
        ps3v3_2v5.set_current(output=1, current=ps_config.vcc3v3.voltage)

        ps3v3_2v5.set_voltage(output=2, voltage=ps_config.vcc2v5.voltage)
        ps3v3_2v5.set_current(output=2, current=ps_config.vcc2v5.voltage)


        #self.adc_ps_output_enable(False)

        test_results = NameSpace()  # container for the test results to be saved in the test report

        passed = False
        try:
            print('\n-------------------------------')
            print('Connect the power cable to the MGADC08, connect the SMA cable to one of the front ports for better ground.')
            xr.input("Press ENTER to measure current (Q=Exit):")

            self.ps12v.set_output(state=True)
            ps3v3_2v5.output_enable(True)


            # Measure currents on the power supplies
            #test_results.rails = NameSpace()  # Namespace to store all rail results
            #for rail_name, rail_info in cfg.rails.items():
            #    instr = self.rails[rail_name]
            #    V = instr.get_voltage(rail_info.output)
            #    I = instr.get_current(rail_info.output)
            #    print('Rail %s: %.3fV@%.3fA,  limits = %s' % (rail_name, V, I, rail_info))
            #    test_results.rails[rail_name] = NameSpace(V=V, I=I)  # Namespace to store this rail results
            #    print(test_results.rails[rail_name])
            #    print(test_results)
            #    if V < rail_info.vmin or V > rail_info.vmax or I < rail_info.imin or I > rail_info.imax:
            #        # stop immediately as soon as we fail one of these tests. We can't go further anyway. This will powewr off the supplies
            #        assert False, 'Inadequate current or voltage on rail %s' % rail_name

            #dmm.display('','Check power LED')
            power_led_state = xr.input_yes_no('Is the power LED turned ON [Y/N]?')
            test_results.power_led_state = power_led_state
            assert power_led_state, 'Power LED is not turned ON. Something is wrong. Aborting.'

            # Measure voltages on test points
            failed_rails = []
            test_results.test_points = NameSpace()  # Namespace to store all rail results
            for tp_name, limits in cfg.test_points: 
                limits = NameSpace(limits)
                dmm.display('','Measure %s' % tp_name)
                dmm.select_voltage_measurement()
                while True:
                    dmm.local() # Allow the multimeter to update its display in real time
                    xr.input("Apply probe to test point '%s' and press ENTER to measure (Q=Exit):" % tp_name)
                    result = dmm.get_dc_voltage()
                    if result >= cfg.min_test_voltage: break
                    print('Voltage too low. Is the probe well connected? Try again.')
                dmm.beep()
                passed = not (result < limits.vmin or result > limits.vmax)
                test_results.test_points[tp_name] = NameSpace(V=result, passed=passed)
                if not passed:
                    failed_rails.append(tp_name)
                    dmm.beep()
                    time.sleep(0.1)
                    dmm.beep()
                print('   %s : %.3g volts (must be between %.3f - %.3f) ==> %s' % (tp_name, result, limits.vmin, limits.vmax, xr.pass_fail(passed)))


            assert not len(failed_rails), 'Inadequate voltage or current on %s' % ','.join(failed_rails)
            passed = True
        finally: # Always execute this, whatever happens
            #self.adc_ps_output_enable(False)

            self.ps12v.set_output(state=False)
            ps3v3_2v5.output_enable(False)

            test_results.passed = passed
            dmm.display(xr.pass_fail(passed), 'Power-up tests')
            dmm.local()
            xr.save_data(test_results)
            print()
            print('------------------------------------------------')
            xr.input('Disconnect power cable from the mezzanine and press ENTER')

class TestMGADC08Carrier(TestUtils):


    def check_mtu(self):
        stat_command = ['ifconfig', 'enp4s0']
        x = subprocess.check_output(stat_command).decode()
        m = re.search('mtu 9000', x)
        if (m == None ):
            print("\nNeed to change the ethernet port MTU setting. Please enter password when asked.")
            os.system('sudo ifconfig eno1 mtu 9000')

    @pytest.fixture(autouse=True)
    def setUp(self, extra, xr, instruments):
        # xr.summary.pass
        self.xr = xr  # used by other methods
        xr.header('Setting-up')
        self.cfg = xr.config
        self.params = xr.params
        # print(f'cfg={self.cfg}')
        cfg = self.cfg.carrier_tests.setup
        # Expected model and serial of the mezzanine under test (if known)
        self.model = xr.params.model
        self.serial = xr.params.serial
        self.fmc_slot = cfg.fmc_slot

        if cfg.check_mtu:
            self.check_mtu()

        print('\n-------------------------------')
        print('  - Make sure the mezzanine is mounted to the iceboard (or connected via the extension cable), and the iceboard is properly set up (see handbook).')
        print("  - The mezzanine must NOT have a power cable connected directly to it. Connect 16v power supply to backplane")

        self.instruments = instruments #creates the shared instrument dictionary object for all tests
        self.ps = self.open_instrument('ps16v')
        self.ps.set_output(voltage=cfg.vlt, current=cfg.curlmt)

        #if 'dmm' in self.cfg.instruments:
        #    self.dmm = self.open_instrument('dmm')
        #else:
        #    self.dmm = None
        #self.dmm = None


        


        xr.header('Testing...')
        # testing function will now begin

        yield # now give back control to proceed to the test

        if self.ps:
            self.ps.close()
        # This is executed once the test is done

        # pass the model & possibly updated serial back to the parameters object
        xr.params.model = self.model
        xr.params.serial = self.serial
        xr.header('Cleaning up...')




    def dmm_display(self,dmm, line1='', line2=''):
        if dmm:
            dmm.display(line1, line2)



    def _get_iceboard(self, **kwargs):
            FPGA_IP_SET_FUNCTION = '(a,b,3,d)'
            
            try:
                print('Connecting to the IceBoard...')
                a = fpga_array.FPGAArray(**kwargs, fpga_ip_addr_fn=FPGA_IP_SET_FUNCTION, mdns_timeout=1)
            except RuntimeError:  # if we can't find the board
                a = None

            if not a: # turn on the supply and try again if we did not find the board 
                self.ps.set_output(state=True)
                self.xr.input('Press [ENTER] once board has booted and front LED is green')
                
                print('Searching for the IceBoard for up to 70 seconds...')
                a = fpga_array.FPGAArray(**kwargs, fpga_ip_addr_fn=FPGA_IP_SET_FUNCTION, mdns_timeout=70)

            assert len(a.ib), 'No Iceboard was found with parameters %s' % kwargs
            assert len(a.ib) == 1, 'One than one Iceboard was found with parameters %s' % kwargs
            ib = a.ib[0]
            print()
            print('Testing Mezzanine Power with %r' % ib)
            # ib.discover_mezzanines()
            mezz = ib.mezzanine.get(self.fmc_slot, None)
            assert mezz, 'No Mezzanine was detected'

            # Check & update model & serial
            # The ARM updateed its IPMI cache when we wrote the EEPROM, so this is up to date
            model = mezz.part_number
            serial = mezz.serial
            print('    Expected Mezzanine is Model %s SN%s:' % (self.model, self.serial))
            print('    Installed Mezzanine is Model %s SN%s:' % (model, serial))
            # self.xr.params.model = model
            assert model.lower() == self.model.lower() , 'The Mezzanine currently under test does not have the correct model number (expected %s, got %s)' % (self.model, model)
            print(f'Mezz serial is {serial}, self.serial={self.serial!r}')
            if self.serial is None:
                print(f'Assigning serial {serial}')
                self.serial = serial
            else:
                assert serial.lower() == self.serial.lower(), 'The Mezzanine currently under test does not have the correct model and serial numbers (expected %s SN%s, got %s SN%s)' % (self.model, self.serial, model, serial)

            run_async(ib.set_fpga_bitstream_async(firmware_mode = 'corr16', force=False)) #needs to be added to the open iceboard funct
            ip_fn = lambda a, b, c, d: (a, b, 3, d)
            run_async(ib.open_fpga_async(verbose=1, fpga_ip_addr_fn = ip_fn)) 


            return ib, mezz


    def test_eeprom(self, xr):
        """
        Runs Mezzanine EEPROM test and programming.

        - The software connects to the IceBoard
        - The test will automatically:

            - Detects the mezzanine presence (PRSNT Line)
            - Detect the Mezzanine EEPROM and read existing EEPROM contents if it's already programmed
            - Program the EEPROM with model and serial number.

        No user intervention is needed unless the EEPROM is already programmed, in which case confirmation is required to overwrite.
        Total time: 3 s
        """
        cfg = self.cfg.carrier_tests.eeprom_test
        dmm = self.open_instrument('dmm')

        self.dmm_display(dmm, 'EEPROM tests', '%s SN%s' % (self.model, self.serial))

        tr = NameSpace() # test results container
        passed = False
        try:
            # ib, mezz =  self._get_iceboard(**cfg.fpga_array)
            FPGA_IP_SET_FUNCTION = '(a,b,3,d)'
            
            try:
                print('Connecting to the IceBoard...')
                a = fpga_array.FPGAArray(**cfg.fpga_array, fpga_ip_addr_fn=FPGA_IP_SET_FUNCTION, mdns_timeout=1)
            except RuntimeError:  # if we can't find the board
                a = None

            if not a: # turn on the supply and try again if we did not find the board 
                self.ps.set_output(state=True)
                self.xr.input('Press [ENTER] once board has booted and front LED is green')
                
                print('Searching for the IceBoard for up to 70 seconds...')
                a = fpga_array.FPGAArray(**cfg.fpga_array, fpga_ip_addr_fn=FPGA_IP_SET_FUNCTION, mdns_timeout=70)

            assert len(a.ib), 'No Iceboard was found with parameters %s' % kwargs
            assert len(a.ib) == 1, 'One than one Iceboard was found with parameters %s' % kwargs
            ib = a.ib[0]

            run_async(ib.set_fpga_bitstream_async(firmware_mode = 'corr16', force=False)) #needs to be added to the open iceboard funct
            ip_fn = lambda a, b, c, d: (a, b, 3, d)
            run_async(ib.open_fpga_async(verbose=1, fpga_ip_addr_fn = ip_fn)) 

            print()
            print('Testing Mezzanine EEPROM with %r' % ib)
            # Check if PRSNT line is help low
            tr.is_mezzanine_present = run_async(ib.tuber_is_mezzanine_present_async(self.fmc_slot))
            print()
            print('PRSNT line says that the Mezzanine is present: %s' % bool(tr.is_mezzanine_present))
            assert tr.is_mezzanine_present, 'Mezzanine was not detected on FMC slot %i' % self.fmc_slot

            # Attempt to access the EEPROM
            # The EEPROM has 128 kBytes of data and requires 17 bits of addressing
            # 16 bits are provided in the data payload, and the 17th bit is in the I2C address, therefore creating 2 pages.
            # We check if the EEPROM responds to both addresses

            
            eeprom = [ib._fmca_eeprom, ib._fmcb_eeprom][self.fmc_slot-1]
            tr.is_eeprom_i2c_responding = [eeprom.is_present(page) for page in (0, 1)]
            print()
            print('EEPROM is responding to I2C addressing for [page 0, page 1]: %s' % bool(tr.is_eeprom_i2c_responding))
            assert all(tr.is_eeprom_i2c_responding), 'The Mezzanine EEPROM did not respond to I2C addressing on both of its pages.'

            # Attempt to read 32 characters of the EEPROM contents
            # We don't fail on this. This is just for additional info
            print()
            try:
                tr.eeprom_contents_from_fpga = ib.hw.read_mezzanine_eeprom(self.fmc_slot, 0, 32).decode('utf-8', 'ignore')
                print('EEPROM content read by FPGA (first 32 characters) is: %s' % tr.eeprom_contents_from_fpga)
            except Exception as e:
                print('Failed to read EEPROM with the FPGA  due to exception: %r' % e)

            # Attempt to read the EEPROM contents using the ARM. This might not work on older versions (<V6.1) of the ARM firmware
            # We don't fail on this. This is just for additional info
            print()
            try:
                tr.eeprom_contents_from_arm = run_async(ib._mezzanine_eeprom_read_async(self.fmc_slot))
                print('EEPROM content read by ARM is:')
                print(wrap(repr(tr.eeprom_contents_from_arm)))
            except Exception as e:
                print('Failed to read EEPROM with ARM  due to exception: %r' % e)

            # Attempt to auto-detect the mezzanine type to see if the EEPROM already programmed
            print()
            print('Discovering Mezzanine')
            run_async(ib.discover_mezzanines_async())
            mezz = ib.mezzanine.get(self.fmc_slot, None)
            print('    Mezzanine is %r:' % mezz)

            if mezz:
                # If a Mezzanine is discovered, it must have valid IPMI data.
                eeprom_data = run_async(ib._mezzanine_eeprom_read_async(self.fmc_slot))
                ipmi = mezz.decode_eeprom(eeprom_data)  # returns either a Tuber IPMI or a Python IPMI
                is_mcgill_format = (eeprom_data[0] == 0x0d)
                tr.old_ipmi = repr(ipmi)
                print()
                print('The board IPMI information found in its EEPROM is')
                print('-------------')
                print(wrap(ipmi))
                print('-------------')

                #   If the model/serial  do not match what we expect, we can overrite with a new IPMI block
                if any([
                    ipmi.board.part_number != self.model,
                    ipmi.product.part_number != self.model,
                    ipmi.product.serial_number != self.serial,
                    ipmi.board.serial_number != self.serial]):
                    print()
                    print(' *** ATTENTION ***')
                    print(' The board model and serial number found in its EEPROM do not match the expected values.')
                    assert cfg.overwrite, 'EEPROM is already programmed with model and serial numbers that do not match the expected values. Overwriting is not allowed by the configuration file.'
                    answer = xr.input_yes_no('Do you want to proceed and overwrite the EEPROM? All existing data (including MULTI fields) will be lost on that board.')
                    assert answer, 'The EEPROM was *NOT* reprogrammed. Aborting test.'
                    create_new_ipmi = True
                    write_ipmi = True
                    rev = None
                    for rev_number, rev_limits in list(NameSpace(cfg.revision_table).items()):
                        if rev_limits.sn_min <= int(self.serial) <= rev_limits.sn_max:
                            rev = rev_number
                            break
                    assert rev is not None, 'Could not determine the revision number based on serial number'

                else:
                    print('The board model and serial number found on the EEPROM match the expected values')
                    create_new_ipmi = False
                    if is_mcgill_format:
                        print('The IPMI data was the old McGill format and can be re-written in the standard IPMI format, with any additional information stored in MULTI fields. ')
                        write_ipmi = xr.input_yes_no('Do you want to proceed and refresh the EEPROM contents with the new IPMI format?')
                    else:
                        print(' *** NOTE ***')
                        print('The IPMI data was read by the ARM and might not include MULTI fields that may be on the EEPROM. Writing will be disabled to avoid losing that information')
                        write_ipmi = False


            else:  # Mezzanine not detected, we assume the EEPROM is not programmed
                # Figure out the revision number based on serial number and the table in the config file
                create_new_ipmi = True
                write_ipmi = True
                rev = None
                for rev_number, rev_limits in list(NameSpace(cfg.revision_table).items()):
                    if rev_limits.sn_min <= int(self.serial) <= rev_limits.sn_max:
                        rev = rev_number
                        break
                assert rev is not None, 'Could not determine the revision number based on serial number'

            if create_new_ipmi:
                if rev < 10:
                    strev = '0'+str(rev)
                print()
                print('Based on the serial number, the revision number will be:' , rev)
                ipmi = hw.ipmi_fru.FRU(
                    board=hw.ipmi_fru.Board(
                        mfg_date=datetime.datetime.now(),
                        manufacturer="Winterland",
                        product_name="McGill Mezzanine",
                        part_number=self.model,
                        serial_number=self.serial,
                        fru_file=""),
                    product=hw.ipmi_fru.Product(
                        manufacturer="Winterland",
                        product_name="McGill Mezzanine",
                        part_number=self.model,
                        product_version=strev,
                        serial_number=self.serial,
                        asset_tag="",
                        fru_file=""),
                    # multi=Multi(...), when it's supported by this code
                    )


            tr.write_ipmi = write_ipmi
            tr.new_ipmi = repr(ipmi)
            if write_ipmi:
                encoded_ipmi = ipmi.encode()  # allow_write=false if this is not a python IPMI object with .encode()
                print()
                print('The new IPMI data of the board will be:')
                print('-------------')
                print(wrap(ipmi))
                print('-------------')

                print()
                print('Writing IPMI data (%i bytes) to EEPROM...' % len(encoded_ipmi))
                run_async(ib._tuber_mezzanine_eeprom_write_base64_async(self.fmc_slot, base64.b64encode(encoded_ipmi), 0))
                print('EEPROM WRITTEN with data')

                # read back eeprom
                read_back = run_async(ib._mezzanine_eeprom_read_async(self.fmc_slot))
                print()
                print('Read back %i bytes from EEPROM.')
                tr.read_back = read_back = read_back[:len(encoded_ipmi)]
                assert read_back == encoded_ipmi, 'IPMI Data that was read back from mezzanine EEPROM does not match the written data.'
                print('Data is ok')
            else:
                print()
                print('You decided not to write new data. Readback check is skipped.')
            passed = True
        finally:
            xr.params.test_locals = locals()  # store local variables for interactive debugging
            tr.passed = passed
            xr.save_data(tr)
            self.dmm_display(dmm, xr.pass_fail(passed), 'EEPROM tests')

    def test_power(self, xr):
        """
        Runs Mezzanine power test on the IceBoard.

        - The software connects to the IceBoard
        - The test will automatically:

            - Power up the mezzanine
            - Measure and check voltages and currents into the mezzanine
            - Validate the Power Good (PG_M2C) from the mezzanine

        No user intervention is needed
        Total time: 3 s
        """
        cfg = self.cfg.carrier_tests.test_power
        

        pg_gpio = {
            1: 'FMCA_PG_M2C',
            2: 'FMCB_PG_M2C'
            }

        tr = NameSpace() # test results container
        ib, mezz = (None, None)  # in case we fail finding boards
        passed = False
        try:
            ib, mezz = self._get_iceboard(**cfg.fpga_array)
            run_async(ib.fpga.init_async())

            # Power down mezzanine to get a baseline
            print()
            print('Turning mezzanine power OFF (just in case)')
            run_async(mezz.set_mezzanine_power_async(False))
            time.sleep(0.5)

            # Check that board power is off. The ARM checks this by looking at the voltage on the 12V_EN output.
            tr.first_power_off_state = run_async(ib.tuber_get_mezzanine_power_async(self.fmc_slot))

            assert not tr.first_power_off_state, 'The ARM refused to turn OFF the Mezzanine power !'

            # Check that Power Good goes down when board is powered off to make sure we are not stuck to 0
            # For this to work, the GPIO should not have its internal pull up/downs enabled. This set-up is done by the FPGA hw module.
            #assert ib._gpio_power.read_reg(4) == 0, 'Pullups/pulldown are enabled on the IceBoard IOExpander. The Mezzanine Power Good signal cannot be read properly.'

            #tr.post_power_pg = ib.hw._gpio.read(pg_gpio[self.fmc_slot])
        
            #assert not tr.post_power_pg, 'The Power good line is ON even if the board is OFF!'

            power_led_state = xr.input_yes_no('Is the bottom power LED turned off for the mezzanine under test [Y/N]?')
            assert power_led_state, 'The Power good line is ON even if the board is OFF!'

            print('Power good line is OFF as expected')


            # Turn Mezzanine ON
            print()
            print('Turning mezzanine power ON')
            run_async(mezz.set_mezzanine_power_async(True))

            # Check mezzanine voltages and currents
            print()
            print('Checking mezzanine currents and voltages')
            time.sleep(cfg.pow_stab_time)  # wait for the voltages to stabilize
            mezz_rails = dict([  
                ('vadj', ib.RAIL.MEZZ_VADJ),
                ('vcc3v3', ib.RAIL.MEZZ_VCC3V3),
                ('vcc12v', ib.RAIL.MEZZ_VCC12V0)
                ])
            tr.rails = NameSpace()
            for rail_name, limits in list(cfg.rails.items()):
                V = run_async(ib.tuber_get_mezzanine_voltage_async(mezz_rails[rail_name], self.fmc_slot))
                I = run_async(ib.tuber_get_mezzanine_current_async(mezz_rails[rail_name], self.fmc_slot))
                print(f'Rail {rail_name}: {V:.3f}V@{I:.3f}A,  limits = {limits}')
                tr.rails[rail_name] = NameSpace(V=V, I=I)  # Namespace to store this rail results
                if V < limits.vmin or V > limits.vmax or I < limits.imin or I > limits.imax:
                    # stop immediately as soon as we fail one of these tests. We can't go further anyway. This will powewr off the supplies
                    assert False, 'Inadequate current or voltage on rail %s. Powering down.' % rail_name

            # Check that Power Good is ON
            tr.post_power_pg = ib._gpio.read(pg_gpio[self.fmc_slot])
            assert tr.post_power_pg, 'The Power good line did not turn on!'
            print('Power good line is ON as expected')

            passed = True
        finally:
            xr.params.test_locals = locals()  # store local variables for interactive debugging
            tr.passed = passed
            print()
            print('Test ended. Turning mezzanine power OFF')

            if mezz:
                run_async(mezz.set_mezzanine_power_async(False))
                tr.final_power_off_state = run_async(mezz.get_mezzanine_power_async())
                print('ARM reports that Mezz power is %s' % bool(tr.final_power_off_state))
                print('ARM reports that Mezz power is %s' % bool(run_async(mezz.get_mezzanine_power_async())))
            if ib:
                print('GPIO OUT0 reg is', bin(ib._gpio_power.read_reg(10)))

            xr.save_data(tr)

    def test_spi_pll(self, xr):
        """
        Runs Mezzanine SPI test using the IceBoard.

        SPI Tests

            - IO Expander (including blinking user LEDs)
            - PCB Temperature sensor
            - ADC temperature sensors (2x)
            - ADC chips (2x) (read chip ID & silicon revision. Do a dummy write)
            - Test IOExpander reset
            - Test ADC SPI Reset

        PLL Tests

            - Check the presence and frequency of the 10 MHz reference clock from the Mezzanine
            - Program the ADC PLL to generate the following frequencies:

                - 1600 MHz

            - Check the ADC output clock frequency
            - Send SYNC signal
            - Check if ADC output clock stops on both ADCs
            - Program the MGT PLL to generate the following frequencies:

                - 156.25 MHz

            - Check the MGT PLL lock line status
            - Check the MGT output clock frequency

        The computer will ask if the USER LEDs are blinking.

        Total time: 20 s
        """
        cfg = self.cfg.carrier_tests.spi_pll_test

        tr = NameSpace() # test results container
        ib, mezz = (None, None)  # in case we fail finding boards
        passed = False
        try:
            

            ib, mezz = self._get_iceboard(**cfg.fpga_array)

            #run_async(ib.fpga.init_async())


            run_async(mezz.set_mezzanine_power_async(True))
            time.sleep(0.5)

            # test IO Expander
            print()
            io = mezz.IOExpander
            mezz.spi_reset()  # All ports reset, in read only so we don't damage anything
            for bit in range(8):
                v = 1 << bit
                io.write(io.REG_OLATA, v)
                r = io.read(io.REG_OLATA)
                print('   Wrote 0x%02x, read 0x%02x' % (v, r))
                assert r==v, 'Could not read back correct byte from Mezzanine IO Expander'
                mezz.spi_reset()
                r = io.read(io.REG_OLATA)
                print('    after Reset, read 0x%02x' % (r))
                assert io.read(io.REG_OLATA)==0, 'Mezzanine SPI Reset did not reset the IO Expander registers. '
            print('   IO Expander communication OK')
            run_async(mezz.init()) # reinitialize mezzanine so other tests will work

            # Check PCB temperature
            print()
            print(' Measuring PCB temperature')
            tr.pcb_temp = []
            mezz.AmbTemp.get_temperature() # discard first value after power on, just to be sure. it might be wrong.
            for i in range(cfg.pcb_temp.iterations):
                temp = mezz.AmbTemp.get_temperature()
                tr.pcb_temp.append(temp)
                print('   PCB temperature is %f' % temp)
                assert cfg.pcb_temp.tmin <= temp <= cfg.pcb_temp.tmax, 'PCB temperature out of range'

            # Check ADC temperature
            print()
            print(' Measuring PCB temperature')
            tr.adc_temp = []
            for i in range(cfg.adc_temp.iterations):
                temp0 = mezz.ADC.get_temperature(0)
                temp1 = mezz.ADC.get_temperature(1)
                tr.adc_temp.append({'ADC0': temp0, 'ADC1': temp1})
                print('   ADC temperatures are ADC0=%f, ADC1=%f' % (temp0, temp1))
                #assert cfg.adc_temp.tmin <= temp0 <= cfg.adc_temp.tmax, 'ADC0 temperature out of range' #TODO: add back after tests
                #assert cfg.adc_temp.tmin <= temp1 <= cfg.adc_temp.tmax, 'ADC1 temperature out of range'

            # Check ADC SPI by reading its CHIP ID
            print()
            print('Reading ADC CHIP ID over SPI')
            tr.adc_chip_id = []
            for i, adc in enumerate(mezz.ADC):
                chip_id = adc.chip_id
                tr.adc_chip_id.append(chip_id)
                print('   ADC%i CHIP id is 0x%04x' % (i, chip_id))
                assert chip_id in cfg.valid_adc_chip_id, 'chip ID read from the ADC is wrong'


            # Check ADC reset
            print()
            print('Testing ADC reset')
            for i, adc in enumerate(mezz.ADC):
                adc.write(adc.REG_CHANNEL_SELECT, 0)
                assert adc.read(adc.REG_CHANNEL_SELECT) == 0, 'Cannot set ADC register to zero'
                adc.write(adc.REG_CHANNEL_SELECT, 1)
                assert adc.read(adc.REG_CHANNEL_SELECT) == 1, 'Cannot set ADC register to 1'
                run_async(mezz.set_mezzanine_reset_async(state=False))
                #assert adc.read(adc.REG_CHANNEL_SELECT) == 0, 'ADC reset did not set ADC register back to zero'
                print('  ADC%i reset is OK' % i)


            # Test LED blinking (interactive)
            print()
            print('Testing LED blinking')
            io.LED0 = 0
            io.LED2 = 0
            io.LED3 = 0
            xr.input('Press [Enter] to blink the 4 Mezzanine LEDs')
            while True:
                io.LED0 = 1
                time.sleep(cfg.blink_delay)
                io.LED1_PLL2_RESET = 1
                time.sleep(cfg.blink_delay)
                io.LED2 = 1
                time.sleep(cfg.blink_delay)
                io.LED3 = 1
                time.sleep(cfg.blink_delay)
                io.LED0 = 0
                time.sleep(cfg.blink_delay)
                io.LED1_PLL2_RESET = 0
                time.sleep(cfg.blink_delay)
                io.LED2 = 0
                time.sleep(cfg.blink_delay)
                io.LED3 = 0
                time.sleep(cfg.blink_delay)

                answer = xr.input_yes_no('Did you see the 4 Mezzanine LEDs blink [Q=Quit, Y=Yes, N=No, R=Repeat]:', ['r'])
                if answer == 'r':
                    continue
                break

            ch = 8 if self.fmc_slot==2 else 0 # base channel number for that mezanine
            # ADC PLL lock test
            run_async(mezz.init())  # reset ADC  to make sure we have the right frequency divider ratio of 2
            resolution = 2./cfg.pll_gate_time * 8
            err_max = max(cfg.pll_freq_err_max*1e6, resolution) + 1
            print()
            print('Testing ADC PLL lock')
            for i in range(cfg.pll_iterations):
                for freq in cfg.pll_frequencies:
                    print('   Locking PLL at %f MHz' % freq)
                    #mezz.ADC_PLL.init(freq, gate_time=cfg.pll_gate_time)
                    mezz.ADC_PLL.init(freq)
                    read_adc_freq = ib.FreqCtr.read_frequency(f'ADC_CLK{ch}', gate_time=0.1) / 1e6
                    read_pll_freq = read_adc_freq * 8
                    freq_err = read_pll_freq - freq
                    print('      ADC clock Frequency: %0.6f MHz (x4 = %0.6f MHz, err=%0.0f Hz (max=%0.0f Hz))' % (read_adc_freq, read_pll_freq, freq_err*1e6, err_max))
                    assert abs(freq_err*1e6) < err_max, 'PLL is not locked at the right frequency'
                    print('      Lock is OK!')

            print()
            print('Testing if all ADC clocks can be read')
            freq = 1600
            mezz.ADC_PLL.init(freq)
            mezz.ADC_PLL.init(freq)
            for i in range(8):
                read_adc_freq = ib.FreqCtr.read_frequency(f'ADC_CLK{ch+i}', gate_time=cfg.pll_gate_time) / 1e6
                read_pll_freq = read_adc_freq * 8
                freq_err = read_pll_freq - freq
                resolution = 2./cfg.pll_gate_time * 8
                print('      Channel %02i clock Frequency: %0.6f MHz (x4 = %0.6f MHz, err=%0.0f Hz (max=%0.0f Hz))' % (i, read_adc_freq, read_pll_freq, freq_err*1e6, err_max))
                assert abs(freq_err*1e6) < err_max, 'Invalid clock signal in ADC%i' % i
            print('      All ADC clocks are OK!')


            if cfg.test_mgt_pll:
                # MGT PLL lock test
                print()
                print('Testing MGT PLL lock')
                for i in range(cfg.pll2_iterations):
                    for freq in cfg.pll2_frequencies:
                        print('   Locking MGT PLL at %f MHz' % freq)
                        locked = mezz.MGT_PLL.init(freq, verbose=0, wait_for_lock=0)
                        locked = mezz.MGT_PLL.init(freq, verbose=0, wait_for_lock=1, CP_CURRENT=0x20)
                        for j in range(2):
                            read_freq = ib.FreqCtr.read_frequency('FMCA_MGT_PLL_REFCLK%i' %i, gate_time=0.1) / 1e6
                            freq_err = read_freq - freq
                            print('      MGT PLL output %i clock Frequency: %0.6f MHz (err=%0.6f Hz)' % (i, read_freq, freq_err*1e6))
                            assert abs(freq_err*1e6) < err_max, 'MGT PLL is not locked at the right frequency'
                        print('      Lock is OK!')

            passed = True
        finally:
            xr.params.test_locals = locals()  # store local variables for interactive debugging
            tr.passed = passed
            print()
            print('Test ended. Turning mezzanine power OFF')
            if mezz:
                run_async(mezz.set_mezzanine_power_async(False))
            xr.save_data(tr)

    def set_adc_delays(self, ib):
        # Timing for ADCs. Calculate proper offsets for this board.
        trial = 0
        while True:
            delay_table, stuck_bits, bitposgood, problem = ib.compute_adc_delay_offsets(channels=list(range(8)))
            print("Computed delay table:")
            for ch, dt in list(delay_table.items()):
                print('   Channel %02i: %s' % (ch, dt))

            print("Stuck bit flags")
            for ch, dt in list(stuck_bits.items()):
                print('   Channel %02i: Stuck bits %s' % (ch, dt))

            print('Bit positions validity')
            for ch, dt in list(bitposgood.items()):
                print('   Channel %02i: Bit position good %s' % (ch, dt))

            stuck_ok = not any(stuck_bits.values())
            bitpos_ok = all(all(v) for v in list(bitposgood.values()))
            if stuck_ok and bitpos_ok:
                break
            trial += 1
            assert trial < 6, 'Could not compute ADC delays'
            print('Could not compute ADC delays. Retrying...')
        # Set ADC delays
        ib.set_adc_delays(delay_table)
        return delay_table

    def test_ramp(self, xr):
        cfg = self.cfg.carrier_tests.ramp_test
        plt.ion()

        tr = NameSpace()  # test results container
        ib, mezz = (None, None)  # in case we fail finding boards
        passed = False
        receiver = None
        test_results = NameSpace()

        message = 'Make sure 2 mezzanines are mounted on the board.\nIf not: quit test, power down board, mount second mezzanine and launch test again.\nPress [Enter] to continue or Q[uit]: '
        assert not xr.input(message).lower().startswith('q'), 'Test was interrupted by user'

        try:
            test_results.data = []
            test_results.ramp_ok = []

            print('Opening link to IceBoard')
            ib, mezz = self._get_iceboard(**cfg.fpga_array)

            print('initializing mezzanine...')
            run_async(ib.fpga.init_async()) 
            mezz.init()

            print('Computing ADC delays...')
            ib.set_adc_delays(compute_delays=2, save_delays=False, check_sync_delays=True, check_adc_delays=20, verbose=0, retry=5)
            delay_table = ib.get_adc_delays()
#
            print('\nOpening data receiver socket')
            receiver = ib.get_data_receiver()

            print('Setting up ramp transmission...')
            # Begin Ramp test
            ib.set_adcdaq_mode('data')
            ib.set_data_source('adc')
            ib.set_adc_mode('ramp')
            ib.start_data_capture(period=1, source='adc')

            """
            print('Syncing...')
            ib.sync()

            print('Getting data frames, capturing until a full length frame (len 17) recieved...')
            r.read_frames(flush=1, frames=3, verbose =1)  # flush
            data = []
            framenum = 0
            maxcount=40
            i = 0;
            foundone=0
            while i < maxcount and foundone == 0:
                data.append(r.read_frames(frames = 1, verbose = 0))
                if len(data[i]) == 17:
                    framenum = i
                    foundone = 1
                print(len(data[i]))
                i=i+1;


            # for i in range(8):
            #     assert 17==len(data[i]), "Some frames have incorrect length!"

            print('\nFrame length OK\n')

            r.close()
            ib.stop_data_capture()

            tr.data = data
            plt.figure(1)
            wm = plt.get_current_fig_manager()
            wm.window.wm_geometry("-0+0")

            ideal_ramp = (np.arange(2048) - 128).astype(np.int8)

            ramp_ok = []
            for ch in range(8):
                plt.clf()
                plt.plot(data[framenum][ch])
                plt.pause(0.0001)
                xr.insert_plot('Ramp capture for %s SN%s CHANNEL %02i' % (self.model, self.serial, ch))
                ok = np.all(data[framenum][ch] == ideal_ramp)
                ramp_ok.append(ok)
                if ok:
                    print('Channel %02i: OK' % (ch+1))
                else:
                    print('Channel %02i: ERROR!' % (ch+1))


            assert all(ramp_ok), 'One or more channels have ramp errors'

        finally:
            plt.close('all')
            xr.params.test_locals = locals()  # store local variables for interactive debugging
            tr.passed = passed
            print()
            print('Test ended. Turning mezzanine power OFF')
            if r:
                r.close()
            if ib:
                for m in ib.mezzanine.values():
                    run_async(m.set_mezzanine_power_async(False))
                # run_async(mezz.set_mezzanine_power_async(False, 2 if self.fmc_slot==1 else 1))
            xr.save_data(tr)
            """

            print('Syncing...')
            ib.sync()

            print('Getting data frames...')
            receiver.read_raw_frames(flush=1)  # flush
            data = []
            framenum = 0
            maxcount=40
            i = 0
            foundone=0
            while i < maxcount and foundone == 0:
                data.append(receiver.read_raw_frames()[1])
                if len(data[i]) == 16:
                    framenum = i
                    foundone = 1
                print(len(data[i]))
                i=i+1



            test_results.data.append(data)
            plt.figure(1)
            wm = plt.get_current_fig_manager()
            wm.window.wm_geometry("-0+0")

            ideal_ramp = (np.arange(2048) - 128).astype(np.int8)

            print(data)


            ramp_ok = []
            for ch in range(16):
                plt.clf()
                plt.plot(data[framenum][ch])
                xr.insert_plot('Ramp capture for %s SN%s CHANNEL %02i' % (self.params.model, self.params.serial, ch))
                ok = np.all(data[framenum][ch] == ideal_ramp)
                ramp_ok.append(ok)
                if ok:
                    print('Channel %02i: OK' % (ch+1))
                else:
                    print('Channel %02i: ERROR!' % (ch+1))
                test_results.ramp_ok.append(ramp_ok)

            assert all(test_results.ramp_ok), 'One or more channels have ramp errors'

            passed = True

        finally:
            #ib.close()
            self.params.test_locals = locals()  # store local variables for interactive debugging
            #receiver.close()
            tr.passed = passed
            print()
            print('Test ended. Turning mezzanine power OFF')
            if ib:
                for m in ib.mezzanine.values():
                    run_async(m.set_mezzanine_power_async(False))
                # run_async(mezz.set_mezzanine_power_async(False, 2 if self.fmc_slot==1 else 1))
            xr.save_data(tr)

    def set_mezzanine_power(self, state, slot=None):
        run_async(self.ib.mezzanine[slot or self.fmc_slot].set_mezzanine_power_async(state))

    def test_s11(self, xr):
        """
        Runs S11 tests.


        Total time: 20 s
        """
        cfg = self.cfg.carrier_tests.s11_test
        dummy_instr = cfg.dummy_instruments
        plt.ion()

        # Open Network Analyzer
        if 'na' not in cfg.instruments:
            class DummyNA(object):
                #why is this here? Wouldn't this give us garbage if we fail to connect?
                def command(*args, **kwargs): return
                def get_s_params(self, *args, **kwargs):
                    freqs = np.linspace(100e6, 1000e6, 400)
                    s11_data = freqs*0 -0.0001
                    return freqs, (s11_data, )
                def plot_s_params(self, freqs, s11_data, title='', **kwargs):
                    plt.plot(freqs, s11_data)
                    plt.title(title)
            na = DummyNA()
        else:
            na = self.open_instrument('na')

        tr = NameSpace() # test results container
        ib, mezz = (None, None)  # in case we fail finding boards
        passed = False
        tr.s11_data = NameSpace()
        tr.freq_resp = NameSpace()
        r = None
        try:

            print("Connecting to Iceboard")
            ib, mezz = self._get_iceboard(**cfg.fpga_array)
            
            run_async(ib.fpga.init_async())
            run_async(mezz.set_mezzanine_power_async(True))
            


            r = ib.get_data_receiver()

            if cfg.adc_trim_value is not None:
                for adc in mezz.ADC:
                    adc.set_trim(cfg.adc_trim_value)

            passed_s11 = []
            passed_fr = []

            print("Computing ADC delays")

            frame_transmission_period = 0.1
            ib.set_adc_delays(compute_delays=2, save_delays=False, check_sync_delays=True, check_adc_delays=20, verbose=0, retry=5)

            tr.delay_table = ib.get_adc_delays()
            ib.set_adcdaq_mode('data')
            ib.set_data_source('adc')
            ib.set_adc_mode('data')

            print("Starting data capture & SYNC")

            ib.start_data_capture(period=frame_transmission_period, source='adc')
            ib.sync()


            fr_freqs = cfg.freqs
            power_level = cfg.power_level
            fr_f = [x[0] for x in cfg.expected_frequency_response]
            fr_a = [x[1] for x in cfg.expected_frequency_response]
            full_scale_response = ((np.sin(np.arange(2048) / 2048. * 10 * 2 * np.pi) + 1) / 2 * 255 - 128).astype(np.int8)

            print("Starting channel-by-channel tests")

            for channel in cfg.channels:
                # --------------------------------
                #   Frequency Response Test
                # --------------------------------
                print()
                print('Measuring analog frequency reponse of channel')
                print(f'Testing ADC board  CHANNEL CH{channel}')
                xr.input(f'Connect cable to ***CHANNEL CH{(channel)}*** SMA and press [ENTER] or [Q] to abort.')
                plt.close('all')

                logical_channel = (channel - 1) + (8 if self.fmc_slot==2 else 0)
                
                #logical_channel = 12
                # make sure we generate a signal on port 1
                na.command('S21')
                na.command('CONT')
         
                while True:
                    # na.command('CWFREQ 10 MHz') # kick the network analyser in CW mode early
                    # na.command('POWE %f DB' % power_level)  # should we wait for the power to stabilize?
                    na.set_cw_source(freq=10e6, power=power_level)
                    r.read_raw_frames(flush=True)  # flush

                    ampl = []
                    fr_ok = []
                    resp = NameSpace(freq=[], data=[], dbfs=[])
                    for f in fr_freqs:
                            print('   CHANNEL CH%i, Sinewave %7.3f MHz @ %f dBm' % (channel, f, power_level)),
                            # na.command('CWFREQ %f MHz' % f)
                            for i in range(5):
                                try:
                                    na.set_cw_source(freq=f*1e6)
                                    break
                                except:
                                    print(f"Couldn't communicate with the network analyzer, trying again ({i+2}/5)")
                            print('.', end='')
                            # time.sleep(frame_transmission_period)
                            r.read_raw_frames(flush=True)  # let the new data propagate

                            print('.', end='')
                            
                            
                            trial = 0
                            for frame in range(cfg.number_of_frames):
                                #data = r.read_frames(cfg.number_of_frames)
                                while True:
                                    _, scaler, _ = r.read_raw_frames(flush=False) #reads one frame of buffer 

                                    channels_received = len(scaler)
                                    print(f"Got packets from {channels_received} channels")
                                    if channels_received == 16:
                                        break   
                                    
                                    assert trial < 20, 'Did not receive data from board' 

                                    time.sleep(0.5)
                                    trial += 1

                                
                            
                            data = scaler[logical_channel].astype(float)

                            # print "Got %i samples" % len(data)
                                    # break
                            resp.freq.append(f)
                            resp.data.append(data)

                            
                            a = 10 * np.log10(data.var() / full_scale_response.var())  # in dBFS, takes frame matrix and computes its variance 
                            resp.dbfs.append(a)
                            if  min(fr_f) <= f <=max(fr_f):
                                expected_a = np.interp(f, fr_f, fr_a)
                                # print(f'a={a}, expected_a={expected_a}')
                                ok = bool(a > expected_a)
                            else:
                                ok = True
                                expected_a = None
                            fr_ok.append(ok)
                            print('Response = %0.3f dBFS%s' % (a, ', expected %0.3f dBFS (%s)' % (expected_a, ['FAILED','PASSED'][ok]) if expected_a is not None else ''))
                            '''plt.figure(2)
                            plt.clf()
                            plt.plot(data)
                            plt.ylim(-128, 128)
                            plt.title('CHANNEL %02i, Sinewave %f MHz @ %f dBm' % (channel+1, f, power_level))
                            plt.pause(0.0001)
                            xr.insert_plot()'''
                            ampl.append(a)  # 8044 = approximare

                    print()
                    print('Frequency response')
                    if True:
                        fig = plt.figure(3)
                        #fig.canvas.get_tk_widget().configure(takefocus=False)
                        # plt.ion()
                        # plt.show(block=False)
                        wm = plt.get_current_fig_manager()
                        wm.window.wm_geometry("-0-0")
                        plt.clf()
                        plt.plot(fr_freqs, ampl, 'b.-', fr_f, fr_a, 'r-')
                        plt.ylabel('Response [dB Full Scale]')
                        plt.xlabel('Frequency [MHz]')
                        plt.grid(1)
                        plt.title(f'{self.model} SN{self.serial} CHANNEL CH{channel} Frequency respsonse, Input power =  {power_level} dBm')
                        # plt.ion()
                        plt.pause(0.0001)
                        # plt.draw()

                        # fig.canvas.get_tk_widget().configure(takefocus=False)
                        plt.draw()
                        #plt.show(block=False)
                        xr.insert_plot()

                    if all(fr_ok):
                        print('PASSED')
                    else:
                        print('FAILED')

                    if all(fr_ok):
                        break
                    else:
                        ans = xr.input_yes_no('Frequency reponse test failed. Do you want to check connections and retry [Y/N or Quit=Q]?')
                        if not ans:
                            break
                passed_fr.append(all(fr_ok))
                tr.freq_resp[channel] = resp

                # assert fr_ok, 'Did not pass the frequency response'



                # --------------------------------
                #   S11 Test
                # --------------------------------

                while True:

                    freqs, (s11_data, ) = na.get_s_params(['S11'])
                    tr.s11_data[channel] = (freqs, s11_data)
                    plt.figure(1)
                    wm = plt.get_current_fig_manager()
                    wm.window.wm_geometry("-0+0")
                    plt.clf()
                    na.plot_s_params(freqs, s11_data, title='%s SN%s Channel CH%i S11' % (self.model, self.serial, channel), xscale='lin', plot_phase=False)
                    ix = np.where(np.logical_and(freqs>=400e6, freqs<=800e6))
                    ff = freqs[ix]
                    dd = 20 * np.log10(np.abs(s11_data[ix]))
                    plt.plot([400e6, 800e6], [cfg.s11_max]*2, 'r-')  # plot the limit
                    plt.pause(0.0001)
                    xr.insert_plot()
                    print('Worst case return loss is %0.1f dB. Limit is %0.1d dB' % (max(dd), cfg.s11_max))
                    if all(dd <= cfg.s11_max):
                        passed_s11.append(True)
                        print(' PASSED')
                        break
                    answer = xr.input_yes_no('S11 is not good. Do you want to try again [Y/N] or quit [Q]?' )
                    if answer:
                        continue
                    else:
                        passed_s11.append(False)
                        break



            # plt.close('all')

            assert all(passed_s11), 'Some of the input have too much return loss'
            assert all(passed_fr), 'Some of the frequency responses are wrong'

            passed = True
        finally:
            plt.close('all')
            xr.params.test_locals = locals()  # store local variables for interactive debugging
            tr.passed = passed
            print()
            print('Test ended. Turning mezzanine power OFF')
            print('Disconnect the mezzanine if you are finished with it')
            #if r:
            #    r.close()
            if mezz:
                run_async(mezz.set_mezzanine_power_async(False))
            xr.save_data(tr)

            #if self.ps:
            #    self.ps.set_output(state=False)
            # na.close()

if __name__ == '__main__':
    """ Run the test in this file."""
    v = TestMenu(TEST_CONFIG_FILE).run()
    locals().update(v)


    #import mgadc08_bench_tests
    #importlib.reload(mgadc08_bench_tests)
    #run_test_menu()
    #locals().update(v) # bring local variables from the test runner into the current namespace for easier debugging
