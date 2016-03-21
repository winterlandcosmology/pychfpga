
""" Performs bench tests of the MGADC08 CHIME ADC Mezzanine board.
"""
import unittest
import time
import numpy as np
import matplotlib.pyplot as plt
import base64
import util
import datetime
from util import NameSpace
import textwrap

util.add_paths('../..')  # needed to find icecore
from icecore import XReport as xr
from icecore.tests.xreport import test_report
from icecore.hw import ipmi_fru

util.add_paths('../../..')  # needed to find fpga_array
from fpga_array import FPGAArray

TEST_CONFIG_FILE = './mgadc08_test_config.yaml'

def wrap(obj, width=80):
    return textwrap.fill(str(obj), width)

def input(message):
    key = xr.input(message).lower()
    assert not key.startswith('q'), 'Test was interrupted by user'
    return key

def input_yes_no(message, additional_answers=[]):
    while True:
        key = input(message)
        if key.startswith('y'):
            return True
        elif key.startswith('n'):
            return False
        elif key in additional_answers:
            return key
        print 'Wrong answer. Try again'

class MGADC08BenchTests(unittest.TestCase):  #
    """
    Perform impedance & power tests on the MGADC08 Mezzanine.
    """
    def setUp(self):
        """ Prepare the test for execution.

        Here, we grab the command line arguments and parse them.
        """
        xr.header('Setting-up')
        self.cfg = util.load_config(TEST_CONFIG_FILE)
        cfg = self.cfg.bench_tests.setup  # config options pertaining to setup
        self.instr = util.open_instruments(self.cfg.instruments, cfg.instruments)  # open only instruments listed in cfg.instruments

        self.instr.dmm.set_beeper(True)
        self.instr.dmm.display('Ready for','MGADC08 tests')

        # Disable power supply outputs
        self.instr.ps12v.output_enable(0)
        self.instr.ps3v3_2v5.output_enable(0)

        # Set-up power supply voltages and current limits
        self.rails = NameSpace()
        for rail_name, rail in cfg.rails.items():
            ps = self.instr[rail.ps]
            print 'Configuring rail %s to %.3fV@%.3fA' % (rail_name, rail.voltage, rail.current_limit)
            ps.set_voltage(rail.output, rail.voltage)
            ps.set_current(rail.output, rail.current_limit)
            self.rails[rail_name] = NameSpace(ps=ps, output=rail.output)
        xr.header('Test results')


    def impedance_test(self):
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
        dmm = self.instr.dmm  # Multimeter
        pss = [self.instr.ps12v, self.instr.ps3v3_2v5]  # Both power supplies
        test_results = NameSpace()

        print '-------------------------------'
        print ' Make Sure that '
        print '   - the board under test is NOT connected on the motherboard'
        print '   - the power cable is NOT connected on the board under test'
        print
        print ' Connect the Quick SMA to CH1 SMA input to provide a ground for the impedance measurements'

        for ps in pss:  # Turn off both power supplies, just to be sure
            ps.output_enable(0)

        failed_test_points = []
        passed = False
        try:
            test_results.test_points = NameSpace()
            for tp_name, limits in NameSpace(cfg.test_points).items():
                dmm.display('','Measure %s' % tp_name)
                dmm.select_resistance_measurement()
                while True:
                    dmm.local()
                    input("Apply probe to test point '%s' and press ENTER to measure (Q=Exit):" % tp_name)
                    if limits.delay:
                        dmm.get_resistance()  # make a dummy measurement
                        time.sleep(limits.delay)
                    result = dmm.get_resistance()
                    if result <= cfg.max_impedance: break
                    print 'Impedance is too high. Is the probe really connected?'
                dmm.beep()
                passed = result > limits.zmin
                test_results.test_points[tp_name] = NameSpace(Z=result, passed=passed)
                if not passed:
                    failed_test_points.append(tp_name)
                    dmm.beep()
                    dmm.beep()
                print '   %s : %.0f ohms (must be more than %.0f ohms) ==> %s' % (tp_name, result, limits.zmin, xr.pass_fail(passed))
            assert not len(failed_test_points), 'Low impedance on %s' % ','.join(failed_test_points)
            passed = True
        finally:
            test_results.passed = passed
            dmm.display(xr.pass_fail(passed),'Impedance tests')
            dmm.local()
            xr.save_data(test_results)


    def smoke_test(self):
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
        dmm = self.instr.dmm  # Multimeter
        pss = [self.instr.ps12v, self.instr.ps3v3_2v5]  # Both power supplies


        test_results = NameSpace()  # container for the test results to be saved in the test report

        for ps in pss:  # Turn off both power supplies, just to be sure
            ps.output_enable(0)

        passed = False
        try:
            xr.input("Connect power cable to MGADC08 and press ENTER to measure current (Q=Exit):")

            for ps in pss: # Turn on both power supplies
                ps.output_enable(1)

            # Measure currents on the power supplies
            test_results.rails = NameSpace()  # Namespace to store all rail results
            for rail_name, limits in cfg.rails.items():
                rail = self.rails[rail_name]
                V = rail.ps.get_voltage(rail.output)
                I = rail.ps.get_current(rail.output)
                print 'Rail %s: %.3fV@%.3fA,  limits = %s' % (rail_name, V, I, limits)
                test_results.rails[rail_name] = NameSpace(V=V, I=I)  # Namespace to store this rail results
                print test_results.rails[rail_name]
                print test_results
                if V < limits.vmin or V > limits.vmax or I < limits.imin or I > limits.imax:
                    # stop immediately as soon as we fail one of these tests. We can't go further anyway. This will powewr off the supplies
                    assert False, 'Inadequate current or voltage on rail %s' % rail_name

            dmm.display('','Check power LED')
            power_led_state = input_yes_no('Is the power LED turned ON [Y/N]?')
            test_results.power_led_state = power_led_state
            assert power_led_state, 'Power LED is not tuned ON. Something is wrong. Aborting.'

            # Measure voltages on test points
            failed_rails = []
            test_results.test_points = NameSpace()  # Namespace to store all rail results
            for tp_name, limits in NameSpace(cfg.test_points).items(): # Convert list to (ordered) namespace. limits will also be a NameSpace.
                dmm.display('','Measure %s' % tp_name)
                dmm.select_voltage_measurement()
                while True:
                    dmm.local() # Allow the multimeter to update its display in real time
                    input("Apply probe to test point '%s' and press ENTER to measure (Q=Exit):" % tp_name)
                    result = dmm.get_dc_voltage()
                    if result >= cfg.min_test_voltage: break
                    print 'Voltage too low. Is the probe well connected? Try again.'
                dmm.beep()
                passed = not (result < limits.vmin or result > limits.vmax)
                test_results.test_points[tp_name] = NameSpace(V=result, passed=passed)
                if not passed:
                    failed_rails.append(tp_name)
                    print '   %s : %.3g volts (must be between %.3f - %.3f) ==> %s' % (tp_name, result, limits.vmin, limits.vmax, xr.pass_fail(passed))
                    dmm.beep()
                    dmm.beep()

            assert not len(failed_rails), 'Inadequate voltage or current on %s' % ','.join(failed_rails)
            passed = True
        finally: # Always execute this, whatever happens
            for ps in pss:  # Turn off both power supplies
                ps.output_enable(0)
            test_results.passed = passed
            dmm.display(xr.pass_fail(passed), 'Power-up tests')
            dmm.local()
            xr.save_data(test_results)
            print
            print '------------------------------------------------'
            input('Disconnect power cable from the mezzanine and press ENTER')

class MGADC08CarrierTests():  #

    def setUp(self):
        # xr.summary.pass
        xr.header('Setting-up')
        self.cfg = util.load_config(xr.params.config_file)
        self.model = xr.params.model
        self.serial = xr.params.serial
        self.slot = self.cfg.carrier_tests.setup.fmc_slot
        xr.header('Testing...')
        # testing function will now begin

    def eeprom_test(self):
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
        instr = util.open_instruments(self.cfg.instruments, cfg.instruments)  # open only instruments listed in cfg.instruments
        dmm = instr.dmm
        dmm.display('EEPROM tests', '%s SN%s' % (self.model, self.serial))

        print '-------------------------------'
        print ' Make Sure that '
        print '   -!!! the power cable is NOT connected on the board under test. This will probably damage the motherboard.'
        print
        print ' 1- Connect the mezzanine to the motherboard extension cable'
        print " 2- Power the motherboard if it's not already powered. Wait for the initialization sequence to finish"

        tr = NameSpace() # test results container
        passed = False
        try:

            a = FPGAArray(**cfg.fpga_array)
            ib = a.ib[0]
            print
            print 'Testing Mezzanine EEPROM with %r' % ib
            # Check if PRSNT line is help low
            tr.is_mezzanine_present = ib.is_mezzanine_present(self.slot)
            print
            print 'PRSNT line says that the Mezzanine is present: %s' % bool(tr.is_mezzanine_present)
            assert tr.is_mezzanine_present, 'Mezzanine was not detected on FMC slot %i' % self.slot

            # Attempt to access the EEPROM
            # The EEPROM has 128 kBytes of data and requires 17 bits of addressing
            # 16 bits are provided in the data payload, and the 17th bit is in the I2C address, therefore creating 2 pages.
            # We check if the EEPROM responds to both addresses
            eeprom = [ib.hw._fmca_eeprom, ib.hw._fmcb_eeprom][self.slot-1]
            tr.is_eeprom_i2c_responding = [eeprom.is_present(page) for page in (0, 1)]
            print
            print 'EEPROM is responding to I2C addressing for [page 0, page 1]: %s' % bool(tr.is_eeprom_i2c_responding)
            assert all(tr.is_eeprom_i2c_responding), 'The Mezzanine EEPROM did not respond to I2C addressing on both of its pages.'

            # Attempt to read 32 characters of the EEPROM contents
            # We don't fail on this. This is just for additional info
            print
            try:
                tr.eeprom_contents_from_fpga = ib.hw.read_mezzanine_eeprom(self.slot, 0, 32).decode('utf-8', 'ignore')
                print 'EEPROM content read by FPGA (first 32 characters) is: %s' % tr.eeprom_contents_from_fpga
            except Exception as e:
                print 'Failed to read EEPROM with the FPGA  due to exception: %r' % e

            # Attempt to read the EEPROM contents using the ARM. This might not work on older versions (<V6.1) of the ARM firmware
            # We don't fail on this. This is just for additional info
            print
            try:
                tr.eeprom_contents_from_arm = base64.decodestring(ib._mezzanine_eeprom_read_base64(self.slot)).decode('utf-8', 'ignore')
                print 'EEPROM content read by ARM is:'
                print wrap(repr(tr.eeprom_contents_from_arm))
            except Exception as e:
                print 'Failed to read EEPROM with ARM  due to exception: %r' % e

            # Attempt to auto-detect the mezzanine type to see if the EEPROM already programmed
            print
            print 'Discovering Mezzanine'
            ib.discover_mezzanines()
            mezz = ib.mezzanine.get(self.slot, None)
            print '    Mezzanine is %r:' % mezz

            if mezz:
                # If a Mezzanine is discovered, it must have valid IPMI data.
                eeprom_data = self._mezzanine_eeprom_read(self.slot)
                ipmi = mezz.decode_eeprom(eeprom_data)  # returns either a Tuber IPMI or a Python IPMI
                tr.old_ipmi = repr(ipmi)
                print
                print 'The board IPMI information found in its EEPROM is'
                print '-------------'
                print wrap(ipmi)
                print '-------------'

                #   If the model/serial  do not match what we expect, we can overrite with a new IPMI block
                if any([
                    ipmi.board.part_number != self.model,
                    ipmi.product.part_number != self.model,
                    ipmi.product.serial_number != self.serial,
                    ipmi.board.serial_number != self.serial]):
                    print
                    print ' *** ATTENTION ***'
                    print ' The board model and serial number found in its EEPROM do not match the expected values.'
                    assert cfg.overrite, 'EEPROM is already programmed with model and serial numbers that to not match the expected values. Ovewriting is not allowed by the configuration file.'
                    answer = input_yes_no('Do you want to proceed and overrite the EEPROM? All existing data (including MULTI fields) will be lost on that board.')
                    assert answer, 'The EEPROM was *NOT* reprogrammed. Aborting test.'
                    create_new_ipmi = True
                    write_ipmi = True
                else:
                    print 'The board model and serial number found on the EEPROM match the expected values'
                    create_new_ipmi = False
                    if not isinstance(ipmi, ipmi_fru.FRU):
                        print ' *** NOTE ***'
                        print 'The IPMI data was read by the ARM and might not include MULTI fields that may be on the EEPROM. Writing will be disabled to avoid losing that information'
                        write_ipmi = False
                    else:
                        print 'The IPMI data was the old McGill format and can be re-written in the standard IPMI format, with any additional information stored in MULTI fields. '
                        write_ipmi = input_yes_no('Do you want to proceed and refresh the EEPROM contents with the new IPMI format?')


            else:  # Mezzanine not detected, we assume the EEPROM is not programmed
                # Figure out the revision number based on serial number and the table in the config file
                create_new_ipmi = True
                write_ipmi = True
                rev = None
                for rev_number, rev_limits in NameSpace(cfg.revision_table).items():
                    if rev_limits.sn_min <= int(self.serial) <= rev_limits.sn_max:
                        rev = rev_number
                        break
                assert rev is not None, 'Could not determine the revision number based on serial number'

            if create_new_ipmi:
                print
                print 'Based on the serial number, the revision number will be:' , rev
                ipmi = ipmi_fru.FRU(
                    board=ipmi_fru.Board(
                        mfg_date=datetime.datetime.now(),
                        manufacturer="Winterland",
                        product_name="McGill Mezzanine",
                        part_number=self.model,
                        serial_number=self.serial,
                        fru_file=""),
                    product=ipmi_fru.Product(
                        manufacturer="Winterland",
                        product_name="McGill Mezzanine",
                        part_number=self.model,
                        product_version=str(rev),
                        serial_number=self.serial,
                        asset_tag="",
                        fru_file=""),
                    # multi=Multi(...), when it's supported by this code
                    )


            tr.write_ipmi = write_ipmi
            tr.new_ipmi = repr(ipmi)
            if write_ipmi:
                encoded_ipmi = ipmi.encode()  # allow_write=false if this is not a python IPMI object with .encode()
                print
                print 'The new IPMI data of the board will be:'
                print '-------------'
                print wrap(ipmi)
                print '-------------'

                print
                print 'Writing IPMI data (%i bytes) to EEPROM...' % len(encoded_ipmi)
                ib._mezzanine_eeprom_write_base64(self.slot, base64.b64encode(encoded_ipmi), 0)
                print 'EEPROM WRITTEN with data'

                # read back eeprom
                read_back = base64.b64decode(ib._mezzanine_eeprom_read_base64(self.slot))
                print
                print 'Read back %i bytes from EEPROM.'
                tr.read_back = read_back = read_back[:len(encoded_ipmi)]
                assert read_back == encoded_ipmi, 'IPMI Data that was read back from mezzanine EEPROM does not match the written data.'
                print 'Data is ok'
            else:
                print
                print 'You decided not to write new data. Readback check is skipped.'
            passed = True
        finally:
            xr.params.test_locals = locals()  # store local variables for interactive debugging
            tr.passed = passed
            xr.save_data(tr)
            dmm.display(xr.pass_fail(passed), 'EEPROM tests')
            dmm.local()

    def _get_iceboard(self, **kwargs):
            a = FPGAArray(**kwargs)
            assert len(a.ib), 'No Iceboard was found with parameters %s' % kwargs
            assert len(a.ib) == 1, 'One than one Iceboard was found with parameters %s' % kwargs
            ib = a.ib[0]
            print
            print 'Testing Mezzanine Power with %r' % ib
            ib.discover_mezzanines()
            mezz = ib.mezzanine.get(self.slot, None)
            assert mezz, 'No Mezzanine was detected'

            # Check model & serial
            # The ARM updateed its IPMI cache when we wrote the EEPROM, so this is up to date
            model = mezz.__ipmi_part_number__
            serial = mezz.serial
            print '    Expected Mezzanine is Model %s SN%s:' % (self.model, self.serial)
            print '    Installed Mezzanine is Model %s SN%s:' % (model, serial)
            xr.params.model = model
            assert model.lower() == self.model.lower() , 'The Mezzanine currently under test does not have the correct model number (expected %s, got %s)' % (self.model, model)

            if self.serial is None:
                xr.params.serial = serial  # pass the new serial to the menu system so we can update it
                self.serial = serial

            assert serial.lower() == self.serial.lower(), 'The Mezzanine currently under test does not have the correct model and serial numbers (expected %s SN%s, got %s SN%s)' % (self.model, self.serial, model, serial)

            return ib, mezz

    def power_test(self):
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
        cfg = self.cfg.carrier_tests.power_test

        pg_gpio = {
            1: 'FMCA_PG_M2C',
            2: 'FMCB_PG_M2C'
            }

        tr = NameSpace() # test results container
        ib, mezz = (None, None)  # in case we fail finding boards
        passed = False
        try:
            ib, mezz = self._get_iceboard(**cfg.fpga_array)

            # Power down mezzanine to get a baseline
            print
            print 'Turning mezzanine power OFF (just in case)'
            ib.set_mezzanine_power(False, self.slot)
            time.sleep(0.5)

            # Check that board power is off. The ARM checks this by looking at the voltage on the 12V_EN output.
            tr.first_power_off_state = ib.get_mezzanine_power(self.slot)
            assert not tr.first_power_off_state, 'The ARM refused to turn OFF the Mezzanine power !'

            # Check that Power Good goes down when board is powered off to make sure we are not stuck to 0
            # For this to work, the GPIO should not have its internal pull up/downs enabled. This set-up is done by the FPGA hw module.
            assert ib.hw._gpio_power.read_reg(4) == 0, 'Pullups/pulldown are enabled on the IceBoard IOExpander. The Mezzanine Power Good signal cannot be read properly.'
            tr.post_power_pg = ib.hw._gpio.read(pg_gpio[self.slot])
            assert not tr.post_power_pg, 'The Power good line is ON even if the board is OFF!'
            print 'Power good line is OFF as expected'


            # Turn Mezzanine ON
            print
            print 'Turning mezzanine power ON'
            ib.set_mezzanine_power(True, self.slot)

            # Check mezzanine voltages and currents
            print
            print 'Checking mezzanine currents and voltages'
            time.sleep(cfg.pow_stab_time)  # wait for the voltages to stabilize
            mezz_rails = NameSpace([  # Use NameSpace and list to preserve order
                ('vadj', ib.RAIL.MEZZ_VADJ),
                ('vcc3v3', ib.RAIL.MEZZ_VCC3V3),
                ('vcc12v', ib.RAIL.MEZZ_VCC12V0)
                ])
            tr.rails = NameSpace()
            for rail_name, limits in cfg.rails.items():
                V = ib.get_mezzanine_voltage(mezz_rails[rail_name], self.slot)
                I = ib.get_mezzanine_current(mezz_rails[rail_name], self.slot)
                print 'Rail %s: %.3fV@%.3fA,  limits = %s' % (rail_name, V, I, limits)
                tr.rails[rail_name] = NameSpace(V=V, I=I)  # Namespace to store this rail results
                if V < limits.vmin or V > limits.vmax or I < limits.imin or I > limits.imax:
                    # stop immediately as soon as we fail one of these tests. We can't go further anyway. This will powewr off the supplies
                    assert False, 'Inadequate current or voltage on rail %s. Powering down.' % rail_name

            # Check that Power Good is ON
            tr.post_power_pg = ib.hw._gpio.read(pg_gpio[self.slot])
            assert tr.post_power_pg, 'The Power good line did not turn on!'
            print 'Power good line is ON as expected'

            passed = True
        finally:
            xr.params.test_locals = locals()  # store local variables for interactive debugging
            tr.passed = passed
            print
            print 'Test ended. Turning mezzanine power OFF'
            if ib:
                ib.set_mezzanine_power(False, self.slot)
            tr.final_power_off_state = ib.get_mezzanine_power(self.slot)
            print 'ARM reports that Mezz power is %s' % bool(tr.final_power_off_state)
            print 'ARM reports that Mezz power is %s' % bool(ib.get_mezzanine_power(self.slot))
            print 'GPIO OUT0 reg is', bin(ib.hw._gpio_power.read_reg(10))

            xr.save_data(tr)
            # dmm.display(xr.pass_fail(passed), 'EEPROM tests')
            # dmm.local()

    def spi_pll_test(self):
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
        The computer will ask if the ADC and MGT PLL Lock LEDs is turned on

        Total time: 20 s
        """
        cfg = self.cfg.carrier_tests.spi_pll_test

        tr = NameSpace() # test results container
        ib, mezz = (None, None)  # in case we fail finding boards
        passed = False
        try:

            ib, mezz = self._get_iceboard(**cfg.fpga_array)
            ib.set_mezzanine_power(True, self.slot)
            time.sleep(0.5)

            # test IO Expander
            print
            io = mezz.IOExpander
            mezz.spi_reset()  # All ports reset, in read only so we don't damage anything
            for bit in range(8):
                v = 1 << bit
                io.write(io.REG_OLATA, v)
                r = io.read(io.REG_OLATA)
                print '   Wrote 0x%02x, read 0x%02x' % (v, r)
                assert r==v, 'Could not read back correct byte from Mezzanine IO Expander'
                mezz.spi_reset()
                r = io.read(io.REG_OLATA)
                print '    after Reset, read 0x%02x' % (r)
                assert io.read(io.REG_OLATA)==0, 'Mezzanine SPI Reset did not reset the IO Expander registers. '
            print '   IO Expander communication OK'
            mezz.init() # reinitialize mezzanine so other tests will work

            # Check PCB temperature
            print
            print ' Measuring PCB temperature'
            tr.pcb_temp = []
            mezz.AmbTemp.get_temperature() # discard first value after power on, just to be sure. it might be wrong.
            for i in range(cfg.pcb_temp.iterations):
                temp = mezz.AmbTemp.get_temperature()
                tr.pcb_temp.append(temp)
                print '   PCB temperature is %f' % temp
                assert cfg.pcb_temp.tmin <= temp <= cfg.pcb_temp.tmax, 'PCB temperature out of range'

            # Check ADC temperature
            print
            print ' Measuring PCB temperature'
            tr.adc_temp = []
            for i in range(cfg.adc_temp.iterations):
                temp0 = mezz.ADC.get_temperature(0)
                temp1 = mezz.ADC.get_temperature(1)
                tr.adc_temp.append({'ADC0': temp0, 'ADC1': temp1})
                print '   ADC temperatures are ADC0=%f, ADC1=%f' % (temp0, temp1)
                assert cfg.adc_temp.tmin <= temp0 <= cfg.adc_temp.tmax, 'ADC0 temperature out of range'
                assert cfg.adc_temp.tmin <= temp1 <= cfg.adc_temp.tmax, 'ADC1 temperature out of range'

            # Check ADC SPI by reading its CHIP ID
            print
            print 'Reading ADC CHIP ID over SPI'
            tr.adc_chip_id = []
            for i, adc in enumerate(mezz.ADC):
                chip_id = adc.chip_id
                tr.adc_chip_id.append(chip_id)
                print '   ADC%i CHIP id is 0x%04x' % (i, chip_id)
                assert chip_id in cfg.valid_adc_chip_id, 'chip ID read from the ADC is wrong'


            # Check ADC reset
            print
            print 'Testing ADC reset'
            for i, adc in enumerate(mezz.ADC):
                adc.write(adc.REG_CHANNEL_SELECT, 0)
                assert adc.read(adc.REG_CHANNEL_SELECT) == 0, 'Cannot set ADC register to zero'
                adc.write(adc.REG_CHANNEL_SELECT, 1)
                assert adc.read(adc.REG_CHANNEL_SELECT) == 1, 'Cannot set ADC register to 1'
                mezz.adc_reset()
                assert adc.read(adc.REG_CHANNEL_SELECT) == 0, 'ADC reset did not set ADC register back to zero'
                print '  ADC%i reset is OK' % i


            # Test LED blinking (interactive)
            print
            print 'Testing LED blinking'
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

                answer = input_yes_no('Did you see the 4 Mezzanine LEDS blink [Q=Quit, Y=Yes, N=No, R=Repeat]:', ['r'])
                if answer == 'r':
                    continue
                break

            # ADC PLL lock test
            mezz.init()  # reset ADC  to make sure we have the right frequency divider ratio of 2
            resolution = 2./cfg.pll_gate_time * 8
            err_max = max(cfg.pll_freq_err_max*1e6, resolution) + 1
            print
            print 'Testing ADC PLL lock'
            for i in range(cfg.pll_iterations):
                for freq in cfg.pll_frequencies:
                    print '   Locking PLL at %f MHz' % freq
                    mezz.ADC_PLL.init(freq, gate_time=cfg.pll_gate_time)
                    read_adc_freq = ib.FreqCtr.read_frequency('ADC_CLK0', gate_time=0.1) / 1e6
                    read_pll_freq = read_adc_freq * 8
                    freq_err = read_pll_freq - freq
                    print '      ADC clock Frequency: %0.6f MHz (x4 = %0.6f MHz, err=%0.0f Hz (max=%0.0f Hz))' % (read_adc_freq, read_pll_freq, freq_err*1e6, err_max)
                    assert abs(freq_err*1e6) < err_max, 'PLL is not locked at the right frequency'
                    print '      Lock is OK!'

            print
            print 'Testing if all ADC clocks can be read'
            freq = 1600
            mezz.ADC_PLL.init(freq)
            mezz.ADC_PLL.init(freq)
            for i in range(8):
                read_adc_freq = ib.FreqCtr.read_frequency('ADC_CLK%i' % i, gate_time=cfg.pll_gate_time) / 1e6
                read_pll_freq = read_adc_freq * 8
                freq_err = read_pll_freq - freq
                resolution = 2./cfg.pll_gate_time * 8
                print '      Channel %02i clock Frequency: %0.6f MHz (x4 = %0.6f MHz, err=%0.0f Hz (max=%0.0f Hz))' % (i, read_adc_freq, read_pll_freq, freq_err*1e6, err_max)
                assert abs(freq_err*1e6) < err_max, 'Invalid clock signal in ADC%i' % i
            print '      All ADC clocks are OK!'


            if cfg.test_mgt_pll:
                # MGT PLL lock test
                print
                print 'Testing MGT PLL lock'
                for i in range(cfg.pll2_iterations):
                    for freq in cfg.pll2_frequencies:
                        print '   Locking MGT PLL at %f MHz' % freq
                        locked = mezz.MGT_PLL.init(freq, verbose=0, wait_for_lock=0)
                        locked = mezz.MGT_PLL.init(freq, verbose=0, wait_for_lock=1, CP_CURRENT=0x20)
                        for j in range(2):
                            read_freq = ib.FreqCtr.read_frequency('FMCA_MGT_PLL_REFCLK%i' %i, gate_time=0.1) / 1e6
                            freq_err = read_freq - freq
                            print '      MGT PLL output %i clock Frequency: %0.6f MHz (err=%0.6f Hz)' % (i, read_freq, freq_err*1e6)
                            assert abs(freq_err*1e6) < err_max, 'MGT PLL is not locked at the right frequency'
                        print '      Lock is OK!'

            passed = True
        finally:
            xr.params.test_locals = locals()  # store local variables for interactive debugging
            tr.passed = passed
            print
            print 'Test ended. Turning mezzanine power OFF'
            if ib:
                ib.set_mezzanine_power(False, self.slot)
            xr.save_data(tr)

    def set_adc_delays(self, ib):
        # Timing for ADCs. Calculate proper offsets for this board.
        trial = 0
        while True:
            delay_table, stuck_bits, bitposgood = ib.compute_adc_delay_offsets(channels=range(8))
            print "Computed delay table:"
            for ch, dt in delay_table.items():
                print '   Channel %02i: %s' % (ch, dt)

            print "Stuck bit flags"
            for ch, dt in stuck_bits.items():
                print '   Channel %02i: Stuck bits %s' % (ch, dt)

            print 'Bit positions validity'
            for ch, dt in bitposgood.items():
                print '   Channel %02i: Bit position good %s' % (ch, dt)

            stuck_ok = not any(stuck_bits.values())
            bitpos_ok = all(all(v) for v in bitposgood.values())
            if stuck_ok and bitpos_ok:
                break
            trial += 1
            assert trial < 6, 'Could not compute ADC delays'
            print 'Could not compute ADC delays. Retrying...'
        # Set ADC delays
        ib.set_adc_delays(delay_table)
        return delay_table

    def ramp_test(self):
        cfg = self.cfg.carrier_tests.ramp_test

        tr = NameSpace()  # test results container
        ib, mezz = (None, None)  # in case we fail finding boards
        passed = False
        r = None
        try:

            ib, mezz = self._get_iceboard(**cfg.fpga_array)
            ib.set_mezzanine_power(True, self.slot)
            time.sleep(0.5)
            mezz.init()

            r = ib.get_data_receiver()

            delay_table = self.set_adc_delays(ib)

            # Begin Ramp test
            ib.set_adcdaq_mode('data')
            ib.set_data_source('adc')
            ib.set_adc_mode('ramp')
            ib.start_data_capture(period=1, source='adc')
            ib.sync()
            r.read_frames(flush=1, frames=3)  # flush
            data = r.read_frames(1)

            tr.data = data
            plt.figure(1)

            ideal_ramp = (np.arange(2048) - 128).astype(np.int8)

            ramp_ok = []
            for ch in range(8):
                plt.clf()
                plt.plot(data[ch])
                xr.insert_plot('Ramp capture for %s SN%s CHANNEL %02i' % (self.model, self.serial, ch))
                ok = np.all(data[ch] == ideal_ramp)
                ramp_ok.append(ok)
                if ok:
                    print 'Channel %02i: OK' % (ch+1)
                else:
                    print 'Channel %02i: ERROR!' % (ch+1)


            assert all(ramp_ok), 'One or more channels have ramp errors'

        finally:
            xr.params.test_locals = locals()  # store local variables for interactive debugging
            tr.passed = passed
            print
            print 'Test ended. Turning mezzanine power OFF'
            if r:
                r.close()
            if ib:
                ib.set_mezzanine_power(False, self.slot)
            xr.save_data(tr)



    def s11_test(self):
        """
        Runs S11 tests.


        Total time: 20 s
        """
        cfg = self.cfg.carrier_tests.s11_test
        dummy_instr = cfg.dummy_instruments

        if not dummy_instr:
            instr = util.open_instruments(self.cfg.instruments, cfg.instruments)  # open only instruments listed in cfg.instruments
            na = instr.na
        else:
            class DummyNA(object):
                def command(*args, **kwargs): return
                def get_s_params(self, *args, **kwargs):
                    freqs = np.linspace(100e6, 1000e6, 400)
                    s11_data = freqs*0 -0.0001
                    return freqs, (s11_data, )
                def plot_s_params(self, freqs, s11_data, title='', **kwargs):
                    plt.plot(freqs, s11_data)
                    plt.title(title)
            na = DummyNA()

        tr = NameSpace() # test results container
        ib, mezz = (None, None)  # in case we fail finding boards
        passed = False
        tr.s11_data = NameSpace()
        tr.freq_resp = NameSpace()
        r = None
        try:

            ib, mezz = self._get_iceboard(**cfg.fpga_array)
            ib.set_mezzanine_power(True, self.slot)
            time.sleep(0.5)
            mezz.init()

            r = ib.get_data_receiver()

            for adc in mezz.ADC:
                adc.set_trim(cfg.adc_trim_value)

            passed_s11 = []
            passed_fr = []

            frame_transmission_period = 0.1
            tr.delay_table = self.set_adc_delays(ib)
            ib.set_adcdaq_mode('data')
            ib.set_data_source('adc')
            ib.set_adc_mode('data')
            ib.start_data_capture(period=frame_transmission_period, source='adc')
            ib.sync()

            fr_freqs = cfg.freqs
            power_level = cfg.power_level
            fr_f = [x[0] for x in cfg.expected_frequency_response]
            fr_a = [x[1] for x in cfg.expected_frequency_response]
            full_scale_response = ((np.sin(np.arange(2048) / 2048. * 10 * 2 * np.pi) + 1) / 2 * 255 - 128).astype(np.int8)

            for channel in range(8):
                # --------------------------------
                #   S11 Test
                # --------------------------------
                print
                print 'Testing CHANNEL %i' % (channel + 1)
                input('Connect cable to ***CHANNEL %i*** SMA and press [ENTER] or [Q] to abort.' % (channel + 1))

                while True:
                    freqs, (s11_data, ) = na.get_s_params(['S11'])
                    tr.s11_data[channel] = (freqs, s11_data)
                    plt.figure(1)
                    plt.clf()
                    na.plot_s_params(freqs, s11_data, title='%s SN%s Channel %02i S11' % (self.model, self.serial, channel), xscale='lin', plot_phase=False)
                    ix = np.where(np.logical_and(freqs>=400e6, freqs<=800e6))
                    ff = freqs[ix]
                    dd = 20 * np.log10(np.abs(s11_data[ix]))
                    plt.plot([400e6, 800e6], [cfg.s11_max]*2, 'r-')  # plot the limit
                    xr.insert_plot()
                    print '   Worst case return loss is %0.1f dB. Limit is %0.1d dB' % (max(dd), cfg.s11_max)
                    if all(dd <= cfg.s11_max):
                        passed_s11.append(True)
                        print ' PASSED'
                        break
                    answer = input_yes_no('S11 is not good. Do you want to try again [Y/N] or quit [Q]?' )
                    if answer:
                        continue
                    passed_s11.append(False)

                # --------------------------------
                #   Frequency response Test
                # --------------------------------
                print
                print 'Measuring analog frequency reponse of channel'

                na.command('CWFREQ 10 MHz') # kick the network analyser in CW mode early
                na.command('POWE %f DB' % power_level)  # should we wait for the power to stabilize?
                r.read_frames(flush=1, frames=3)  # flush

                ampl = []
                fr_ok = []
                resp = NameSpace(freq=[], data=[], dbfs=[])
                for f in fr_freqs:
                    print ('   CHANNEL %02i, Sinawave %7.3f MHz @ %f dBm' % (channel+1, f, power_level)),
                    na.command('CWFREQ %f MHz' % f)
                    print '.',
                    # time.sleep(frame_transmission_period)
                    r.read_frames(flush=True, frames=3)  # let the new data propagate
                    print '.',
                    trial = 0
                    while True:
                        data = r.read_frames(cfg.number_of_frames)
                        if channel in data:
                            break
                        assert trial < 3, 'Did not receive data from the board.'
                        trial += 1

                            # answer = input_yes_no('Did not receive data from the board. Want to try again [Y] or quit [Q]?' )
                            # assert answer, 'Interrupting test upon user request because of missing data'
                        # else:
                    data = data[channel].astype(float)
                            # break
                    resp.freq.append(f)
                    resp.data.append(data)
                    a = 10 * np.log10(data.var() / full_scale_response.var())  # in dBFS
                    resp.dbfs.append(a)
                    if  min(fr_f) <= f <=max(fr_f):
                        expected_a = np.interp(f, fr_f, fr_a)
                        ok = a > expected_a
                    else:
                        ok = True
                        expected_a = None
                    fr_ok.append(ok)
                    print 'Response = %0.3f dBFS%s' % (a, ', expected %0.3f dBFS (%s)' % (expected_a, ['ERROR!','OK'][ok]) if expected_a is not None else '')
                    plt.figure(2)
                    plt.clf()
                    plt.plot(data)
                    plt.ylim(-128, 128)
                    plt.title('CHANNEL %02i, Sinawave %f MHz @ %f dBm' % (channel, f, power_level))
                    # xr.insert_plot()
                    ampl.append(a)  # 8044 = approximare

                if all(fr_ok):
                    print 'PASSED'
                else:
                    print 'FAILED'

                passed_fr.append(all(fr_ok))
                tr.freq_resp[channel] = resp
                print
                print 'Frequency response'
                plt.figure(2)
                plt.clf()
                plt.plot(fr_freqs, ampl, 'b.-', fr_f, fr_a, 'r-')
                plt.ylabel('Response [dB Full Scale]')
                plt.xlabel('Frequency [MHz]')
                plt.grid(1)
                plt.title('CHANNEL %02i Frequency respsonse, Input power =  %f dBm' % (channel, power_level))
                xr.insert_plot()
                assert fr_ok, 'Did not pass the frequency response'

            assert all(passed_s11), 'Some of the input have too much return loss'
            assert all(passed_fr), 'Some of the frequency responses are wrong'

            passed = True
        finally:
            xr.params.test_locals = locals()  # store local variables for interactive debugging
            tr.passed = passed
            print
            print 'Test ended. Turning mezzanine power OFF'
            print 'Disconnect the mezzanine if you are finished with it'
            if r:
                r.close()
            if ib:
                ib.set_mezzanine_power(False, self.slot)
            xr.save_data(tr)
            # na.close()

if __name__ == '__main__':
    """ Run the test in this file."""
    import mgadc08_bench_tests
    reload(mgadc08_bench_tests)
    v = util.run_tests(TEST_CONFIG_FILE)
    locals().update(v) # bring local variables from the test runner into the current namespace for easier debugging