
""" Performs bench tests of the MGADC08 CHIME ADC Mezzanine board.
	Package requirements: reportlab, rst2pdf, pyserial, pyudev (linux only), pyvisa
	Use command "pip install <package>" and "pip install -U https://github.com/hgrecco/pyvisa-py/zipball/master"
    pip install -e git+https://github.com/Eichhoernchen/pybonjour.git#egg=pybonjour
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
import memtest_rs232 as rs232
import subprocess
import shlex

import visa


util.add_paths('../..')  # needed to find icecore
from icecore import XReport as xr
from icecore.tests.xreport import test_report
from icecore.hw import ipmi_fru

util.add_paths('../../..')  # needed to find fpga_array

import fpga_array
from pychfpga.core.icecore import IceBoardPlusHandler

util.add_paths('../../icecore/python/hw')
import ipmi_fru as ipmi_fru
import base64

util.add_paths('../../icecore_ext')
from pychfpga.core.icecore import IceBoardPlusHandler
import fpga_bitstream as fb
from pychfpga.core.icecore_ext import IceBoardExtHandler


import base64
import re

TEST_CONFIG_FILE = './mgk7mb_test_config.yaml'

rm = visa.ResourceManager('@py')

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

def in_range(value, target, pmargin=0.05, amargin=0):
    if( (value < target * ( 1 - pmargin) - amargin) or
        (value > target * ( 1 + pmargin) + amargin)):
            return False
    else:
            return True

class MGK7MBBenchTests(unittest.TestCase):  #
    """
    Perform impedance & power tests on the MGADC08 Mezzanine.
    """
    def setUp(self):
        """ Prepare the test for execution.

        Here, we grab the command line arguments and parse them.
        """
        xr.header('Setting-up')
        self.cfg = util.load_config(TEST_CONFIG_FILE)

        cfg = self.cfg.motherboard_tests.bench  # config options pertaining to setup
        self.instr = util.open_instruments(self.cfg.instruments, cfg.instruments)  # open only instruments listed in cfg.instruments

        self.instr.ps18v.output(state=False, readonly=False) #Ensuring power on N5764A is off
        self.instr.ps18v.clear() #Clearing any previous protection
        self.instr.ps18v.control_voltage(voltage=18, readonly=False) #Setting voltage to 18V, power still off
        self.instr.ps18v.set_current_limit(current=5, ocp=True) #Setting current limit and turning on ocp feature

        xr.header('Test results')

    def tearDown(self):
        self.instr.ps18v.output(state=False, readonly=False) #Ensuring power on N5764A is off

    def insp_test(self):
        """
        QC001: Inspection test: visual check of the board

        Procedure:

          - Start the inspection test on the computer
          - Type in serial number of tested board.
          - Visually inspect specific points mentioned in test

        """
        xr.header('Inspection test')

        questions = ["Is the FPGA heatsink attached? With pushpins holding it firmly in place [Y/N]? ",
             "Is the FPGA heatsink model correct? Pins cut near stiffener [Y/N]? ",
             "Is the board Stiffener installed, and the board is reasonably flat[Y/N]? ",
             "Are the PLL heatsinks attached [Y/N]? ",
             "Does the ARM shield fence look streight [Y/N]? ",
             "Are the dipswitches set correctly [Y/N]? ",
             "Are the jumpers placed correctly [Y/N]? ",
             "Are the 90pin Molex inpact backplane connectors screwed down [Y/N]? ",
             "Are the QSFP and SFP connectors soldered in place [Y/N]? ",
             "Is the patch wire in place and secured [Y/N]? ",
             "Do all buck converter sensors have the additional hand soldered capacitor [Y/N]? ",
             "Please inspect the GTX backplane connector pins on the back of the board (you will need the microscope). " \
                 "Are they all perfect? i.e none of them bent or unusual [Y/N]? ",
             "Do all the FMC power switches look well soldered [Y/N]? "]
        answers = []

        for i in range(0, len(questions)):
            answers.append(input_yes_no(str(i+1)+ ") " + questions[i], additional_answers=[]))

        passed =  True
        for i, ans in enumerate(answers):
            if not ans:
                print "Please address inspection line %i" % (i+1)
                passed = False
        assert passed, 'Inspection Test failed'


        comments = input("If there are any additional comments you wish to make (e.g. scratches, manufacturing problems), please describe below. (If none, enter 'None'): ")
        passed = True

        #Estimate 30 seconds

    def res_test(self):
        """
        QC002: Resistance test: Record resistance at various points on the board

        Procedure:

          - Start the impedance test on the computer
          - Type in serial number of tested board.
          - Clip ground probe of multimeter on specified grounding point
          - Touch each test points indicated by the software and press ENTER
          - Test points:

               - +V       (P18)
               - 12V      (L3)
               - Vadj     (L11)
               - 3.3V     (L1)
               - 5V       (L4)
               - 1V GTX   (L5)
               - 1.2V GTX (L6)
               - 1V Core  (L8)
               - 1.5V     (L7)
               - 1.8V     (L9)

          - Total time: 50 s

        We pass/fail the test only after all measurements are done so we can gather more debugging info.
        """
        xr.header('Resistance test')

        visa_addr = 'TCPIP0::%s::inst0::INSTR' % self.cfg.instruments.dmm['ip_addr']
        print 'Using visa address %s for multimeter' %visa_addr
        dmm_visa = rm.open_resource(visa_addr)
        self.instr.dmm.set_beeper(True)
        self.instr.dmm.display('Ready for','MGK7MB tests')

        # Useful shortcuts
        cfg = self.cfg.motherboard_tests.impedance
        dmm = self.instr.dmm  # Multimeter
        #pss = [self.instr.ps12v, self.instr.ps3v3_2v5]  # Both power supplies
        test_results = NameSpace()

        print '-------------------------------'
        print ' Make Sure that '
        print '   - the power cable is NOT connected on the board under test'
        print '   - the ground clip is attached to the board stiffener'

        #for ps in pss:  # Turn off both power supplies, just to be sure
        #    ps.output_enable(0)

        failed_test_points = []
        passed = False
        try:
            test_results.test_points = NameSpace()
            for tp_name, limits in NameSpace(cfg.test_points).items():
                dmm.display('','Measure %s' % tp_name)
                dmm.select_resistance_measurement()
                dmm.get_resistance() #Dummy measurement to ensure screen updates
                while True:
                    #dmm.local()
                    dmm_visa.write("DIAG:LOCAL")
                    input("Apply probe to test point '%s' and press ENTER to measure (Q=Exit):" % tp_name)
                    if limits.delay:
                        dmm.get_resistance()  # make a dummy measurement
                        time.sleep(limits.delay)
                    result = dmm.get_resistance()
                    time.sleep(1)
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
            #dmm.local()
            dmm_visa.write("DIAG:LOCAL")
            xr.save_data(test_results)

    def powerup_test(self):
        """
        QC003: Power board up: Measure current draw on agilent supply

        Procedure:

          - Start the power test on the computer
          - Type in serial number of tested board.
          - Measure the current draw on the main power

        """
        #Connect to power supply
        #Measure current draw on it
        #Fail test if current draw too high
        #If power draw okay - request user to indicate if any of the 9 power LEDs are turned off.
        #If any of the power LEDs are turned off test has failed.

        xr.header('Powerup test')
        cfg = self.cfg.motherboard_tests.powerup
        self.instr.ps18v.clear() #Clearing any previous protection
        self.instr.ps18v.output(state=False, readonly=False) #Ensuring power on N5764A is off
        self.instr.ps18v.control_voltage(voltage=18, readonly=False) #Setting voltage to 18V, power still off
        self.instr.ps18v.set_current_limit(current=2, ocp=True) #Setting current limit and turning on ocp feature

        while (input_yes_no("Are you ready to apply power to the board? [Y/N]", additional_answers=[]) != True):
            pass;
        self.instr.ps18v.output(state=True, readonly=False)
        self.instr.ps18v.pollstatus(polltime=0.1, runtime=1)
        status = self.instr.ps18v.status()
        self.instr.ps18v.output(state=False, readonly=False) #Ensuring power on N5764A is off
        if (status['status']=='OK'):
            if (status['current'] < cfg.imax) and  ( status['current'] > cfg.imin):
                passed = True
            else:
                passed = False
                print "Measured current is out of range"
                assert False, "Current out of range"
        else:
            print "Fault detected - either current limiting or off, status is:" + status['status']
            passed = False
            assert False, "Fault detected"
        print "Looks like the current draw is in range"
        print "\nBe ready to inspect the 9 power LEDs at the back of the board"
        while (input_yes_no("Are you ready to apply power to the board again? [Y/N]", additional_answers=[]) != True):
            pass;
        self.instr.ps18v.output(state=True, readonly=False)

        response = input_yes_no("Are all 9 of the power LEDs turned on? Y/N]", additional_answers=[])

        if response == True:
            print "Test has passed"
        else:
            input("Which lights failed to light up?")
            #self.instr.ps18v.output(state=False, readonly=False)
            passed = 0
            assert False, "Some of the buck converters have not registered power good"

    #Estimate 30 seconds

    def pll_test(self):
        """
        QC004: Program the onboard PLLs

        Procedure:

          - Start the pll test on the computer
          - Type in serial number of tested board.
          - Attach dongle to PLL 1
          - Board will power up and program PLL 1
          - Atach dongle to PLL 2 (Move brown wire)
          - PLL 2 will be programed and board power disabled.

        """
        #Connect to PLL programmer dongle
        #Power up and program the PLLS
        #Restart the board and ask user if PLL lock lights turned on.

        #Think about adding: Check that Both PLL lock (check LED) when using SMA.
        xr.header('PLL test')
        import cdce620005_pll_QC as cdce
        cfg = self.cfg.motherboard_tests.pll

        print "Please ensure that no flash card is in the board."
        print "Please connect PLL programming dongle to the 6pin header."

        while (input_yes_no("Are you ready to apply power to the board? [Y/N]", additional_answers=[]) != True):
            pass;

        self.instr.ps18v.set_current_limit(current=cfg.ilim, ocp=True)  #We dont need much current
        self.instr.ps18v.output(state=True, readonly=False)

        #cdce.write_pll_reg(cdce.pll1port, 0 , 0) #temporarily make reg 0 on pll 1 wrong

        print "\nComparing PLL1 desired settings with measured settings:"
        meas_pll1_regs=cdce.read_pll1()
        pll1_cmp=cdce.comp_reg(cfg.pll1regs, meas_pll1_regs)

        print "\nComparing PLL2 desired settings with measured settings:"
        meas_pll2_regs=cdce.read_pll2()
        pll2_cmp=cdce.comp_reg(cfg.pll2regs, meas_pll2_regs)

        if (all(v==0 for v in meas_pll1_regs) and all(v==0 for v in meas_pll1_regs)):
            while (input_yes_no("All the registers in both PLLs are 0.  Please ensure that dongle orientated correctly on the program header. Ready to continue? [Y/N]", additional_answers=[]) != True):
                pass;
            input_yes_no("Out of interest was it on the right way? [Y/N]", additional_answers=[])

        if (all(v==0xFFFFFFFF for v in meas_pll1_regs) and all(v==0xFFFFFFFF for v in meas_pll1_regs)):
            while (input_yes_no("All the registers in both PLLs are 0xFFFFFFFF.  Please ensure that dongle is plugged into the program header. Ready to continue? [Y/N]", additional_answers=[]) != True):
                pass;
            input_yes_no("Out of interest was it plugged in? [Y/N]", additional_answers=[])

        if (pll1_cmp==0):
            print "\nDifferences were detected on PLL1 - Programing it"
            cdce.program_pll1(cfg.pll1regs, write_eeprom=True)
            meas_pll1_regs=cdce.read_pll1()
            print "\nComparing settings again:"
            pll1_cmp=cdce.comp_reg(cfg.pll1regs, meas_pll1_regs)
            if pll1_cmp:
                print "PLL1 successfully programed"
            else:
                passed = False
                print "Settings are still wrong - is the dongle orientated correctly on the program header?"
                assert False

        if (pll2_cmp==0):
            print "\nDifferences were detected on PLL2 - Programing it"
            cdce.program_pll2(cfg.pll2regs,write_eeprom=True)
            meas_pll2_regs=cdce.read_pll2()
            print "\nComparing settings again:"
            pll2_cmp=cdce.comp_reg(cfg.pll2regs, meas_pll2_regs)
            if pll_cmp:
                print "PLL2 successfully programed"
            else:
                print "Settings are still wrong - Not sure whats wrong since PLL1 worked, perhaps PLL2 is now locked?"
                passed = False
                assert False

        print "Both PLLs have the correct settings programmed"
        print "Rebooting the board"
        self.instr.ps18v.output(state=False, readonly=False) #Ensuring power on N5764A is off
        self.instr.ps18v.output(state=True, readonly=False) #Turning power back on

        response = input_yes_no("Are both PLL lock lights turned on? (yellow and green next to 6 pin RS232 header) [Y/N]", additional_answers=[])
        if response == True:
            passed = True
        else:
            print "Looks like something has gone wrong - retry the test or check PLL soldering"
            passed = False
            assert False

        self.instr.ps18v.output(state=False, readonly=False) #Ensuring power on N5764A is off
        #Estimate 10 seconds

    def mem_test(self):
        """
        QC005: Perform the ARM memory test

        Procedure:

          - Connect RS232 cable to the board
          - Type in serial number of tested board.
          - Enable board power
          - Interupt boot and start memtest - let run for 5 memory pass runs

        """
        xr.header('Mem test')

        print "Please ensure that a flash card is now plugged into the board."
        print "Please connect RS232 dongle to connector next to the 3x2 LED stack"

        while (input_yes_no("Are you ready to apply power to the board? [Y/N]", additional_answers=[]) != True):
            pass;


        #chmod 777 /dev/ttyUSB* allowed access to screen, probably not the right thing to do!
        #Should probably write another permissions file as for the PLL test
        [ser , rs232dongle_dev] =  rs232.init_rs232()
        print "Found RS232 dongle on port " + rs232dongle_dev

        self.instr.ps18v.output(state=True, readonly=False) #Ensuring power on N5764A is off
        rs232.interupt_boot(ser)
        testpassed = rs232.start_memtest(ser, iterations=5)
        rs232.close_serial(ser)

        self.instr.ps18v.output(state=False, readonly=False) #Ensuring power on N5764A is off

        if testpassed == 1:
            print "The memory is good - all iterations passed"
            passed = 1
        else:
            print "The memory test failed"
            passed = False
            assert False

        #Measured time 90 seconds with 5 iterations (15secs setup, 15secs per iteration)


class MGK7MBNetworkTests(unittest.TestCase):  #
    """
    Perform impedance & power tests on the MGADC08 Mezzanine.
    """
    def setUp(self):
        """ 
        Prepare the test for execution.

        Here, we grab the command line arguments and parse them.

        """
        xr.header('Setting-up')
        self.cfg = util.load_config(TEST_CONFIG_FILE)

        cfg = self.cfg.motherboard_tests.network  # config options pertaining to setup
        self.instr = util.open_instruments(self.cfg.instruments, cfg.instruments)  # open only instruments listed in cfg.instruments

        self.instr.ps18v.output(state=False, readonly=False) #Ensuring power on N5764A is off
        self.instr.ps18v.clear() #Clearing any previous protection
        self.instr.ps18v.control_voltage(voltage=18, readonly=False) #Setting voltage to 18V, power still off
        self.instr.ps18v.set_current_limit(current=5, ocp=True) #Setting current limit and turning on ocp feature

        xr.header('Test results')

    def tearDown(self):
        self.instr.ps18v.output(state=False, readonly=False) #Ensuring power on N5764A is off

    def connect_to_board(self, xr, cfg , questions = True, powerdown = True):

        if questions:
            print "Please ensure that a flash card is now plugged into the board"
            print "Please ensure that the ethernet cable is plugged in to the board"
            while (input_yes_no("Are you ready to apply power to the board? [Y/N]", additional_answers=[]) != True):
                pass;

        if powerdown:
            self.instr.ps18v.output(state=False, readonly=False) #Ensuring power on N5764A is off
            self.instr.ps18v.clear() #Clearing any previous protection
            self.instr.ps18v.control_voltage(voltage=cfg.vlt, readonly=False) #Setting voltage to 18V, power still off
            self.instr.ps18v.set_current_limit(current=cfg.curlmt, ocp=True) #Setting current limit and turning on ocp feature
            self.instr.ps18v.output(state=True, readonly=False) #Turning power on
            print "\nThe board has been powered up.\n"

        #while (input_yes_no("Do the front panel lights indicate that the board is ready? [Y/N]", additional_answers=[]) != True):
        #    pass;

        reload(fpga_array)
        serial = 'iceboard%s.local' %xr.params.serial
        print "Waiting for 'iceboard.local' or '%s' to boot and show up on the network (30 second timeout)" %serial
        ibs = fpga_array.FPGAArray(iceboards="*", open = 0, prog = 0, mdns_timeout=30, ping=1)
        if xr.params.serial in ibs.ib.discover_serial().values():
            print "Found an iceboard with the correct serial number on the network"
            ib_index = ibs.ib.discover_serial().values().index(xr.params.serial)
            ib = ibs.ib[ib_index]
        elif ibs.ib.discover_serial().values().count(None): #Empty serial
            print "Found an iceboard with the no serial number programmed on the network"
            ib_index = ibs.ib.discover_serial().values().index(None)
            if ibs.ib.discover_serial().values().count(None)==1:
                 ib = ibs.ib[ib_index]
            else:
                print "More than one iceboard with no serial number was found"
                assert False, "We don't know which iceboard to connect too"
        else:
            print "We did not find a board with the correct serial number or one with no serial programmed"
            assert False, "IceBoard not found"

        print "Connecting to iceboard at : %s.\n" %ib.hostname

        return (ib, ibs, ib_index)

    def prog_fpga(self, ib):

        bit = fb.FpgaBitstream(self.cfg.fpga_bit_file)
        ib.register_fpga_bitstream(bit)
        ib.set_fpga_bitstream()
        assert ib.is_fpga_programmed()

    def sens_test(self):
        """
        QC007: Read the onboard power sensors and temperature sensors

        Procedure:

          - Start the sensor test on the computer
          - Type in serial number of tested board.
          - Sensor data will be aquired from iceboard.local
          - Read backplane temperature/power sensors

        """
        #Power up board
        #Connect to iceboard.local
        #Get sensor data
        #Turn of board
        #Check if currents/voltages in range
        xr.header('Power sensor test')

        # Useful shortcuts
        cfg = self.cfg.motherboard_tests.sensors
        test_results = NameSpace()
        passed = False

        print "Please ensure that the Mezzanines are NOT mounted on the board for this test"
        (ib, ibs, ib_index) = self.connect_to_board(xr, cfg)

        try:
            #print "Creating hardware map"
            #if xr.params.serial is not None:
            #    serial = 'iceboard%s.local' %xr.params.serial
            #else:
            #    serial = 'iceboard.local'
            #print "The hostname we're looking for is %s " %serial

            #ib = IceBoardPlusHandler(hostname = serial)
            if (ib.ping() == False): #Its likely that the eeprom has never been programmed so hostname = iceboard.lcal
                print "No motherboard found. Trying hostname = iceboard.local"
                ib = IceBoardPlusHandler(iceboards='iceboard.local')

            assert ib.ping(), "No motherboard found. We can't ping it"
            assert not ib.is_mezzanine_present(1), "Mezzanine are NOT supposed to be present for this test, found Mezzanine on Slot 1."
            assert not ib.is_mezzanine_present(2), "Mezzanine are NOT supposed to be present for this test, found Mezzanine on Slot 2."

            print "Reading Iceboard Power, Current, Voltage & Temperature Sensors"
            power, current, voltage, temp = {}, {}, {}, {}

            totalPower = ib.get_motherboard_power()
            power['VCC3V3']     = ib.get_motherboard_power('MOTHERBOARD_RAIL_VCC3V3')
            power['VCC12V0']    = ib.get_motherboard_power('MOTHERBOARD_RAIL_VCC12V0')
            power['VCC5V5']     = ib.get_motherboard_power('MOTHERBOARD_RAIL_VCC5V5')
            power['VCC1V0_GTX'] = ib.get_motherboard_power('MOTHERBOARD_RAIL_VCC1V0_GTX')
            power['VCC1V0']     = ib.get_motherboard_power('MOTHERBOARD_RAIL_VCC1V0')
            power['VCC1V2']     = ib.get_motherboard_power('MOTHERBOARD_RAIL_VCC1V2')
            power['VCC1V5']     = ib.get_motherboard_power('MOTHERBOARD_RAIL_VCC1V5')
            power['VCC1V8']     = ib.get_motherboard_power('MOTHERBOARD_RAIL_VCC1V8')
            power['VADJ']       = ib.get_motherboard_power('MOTHERBOARD_RAIL_VADJ')

            current['VCC3V3']       = ib.get_motherboard_current('MOTHERBOARD_RAIL_VCC3V3')
            current['VCC12V0']      = ib.get_motherboard_current('MOTHERBOARD_RAIL_VCC12V0')
            current['VCC5V5']       = ib.get_motherboard_current('MOTHERBOARD_RAIL_VCC5V5')
            current['VCC1V0_GTX']   = ib.get_motherboard_current('MOTHERBOARD_RAIL_VCC1V0_GTX')
            current['VCC1V0']       = ib.get_motherboard_current('MOTHERBOARD_RAIL_VCC1V0')
            current['VCC1V2']       = ib.get_motherboard_current('MOTHERBOARD_RAIL_VCC1V2')
            current['VCC1V5']       = ib.get_motherboard_current('MOTHERBOARD_RAIL_VCC1V5')
            current['VCC1V8']       = ib.get_motherboard_current('MOTHERBOARD_RAIL_VCC1V8')
            current['VADJ']         = ib.get_motherboard_current('MOTHERBOARD_RAIL_VADJ')

            voltage['VCC3V3']       = ib.get_motherboard_voltage('MOTHERBOARD_RAIL_VCC3V3')
            voltage['VCC12V0']      = ib.get_motherboard_voltage('MOTHERBOARD_RAIL_VCC12V0')
            voltage['VCC5V5']       = ib.get_motherboard_voltage('MOTHERBOARD_RAIL_VCC5V5')
            voltage['VCC1V0_GTX']   = ib.get_motherboard_voltage('MOTHERBOARD_RAIL_VCC1V0_GTX')
            voltage['VCC1V0']       = ib.get_motherboard_voltage('MOTHERBOARD_RAIL_VCC1V0')
            voltage['VCC1V2']       = ib.get_motherboard_voltage('MOTHERBOARD_RAIL_VCC1V2')
            voltage['VCC1V5']       = ib.get_motherboard_voltage('MOTHERBOARD_RAIL_VCC1V5')
            voltage['VCC1V8']       = ib.get_motherboard_voltage('MOTHERBOARD_RAIL_VCC1V8')
            voltage['VADJ']         = ib.get_motherboard_voltage('MOTHERBOARD_RAIL_VADJ')

            temp['POWER']   = ib.get_motherboard_temperature('MOTHERBOARD_TEMPERATURE_POWER')   # between the two 1.0V bucks
            temp['FPGA']    = ib.get_motherboard_temperature('MOTHERBOARD_TEMPERATURE_FPGA')    # near USER SMA
            temp['ARM']     = ib.get_motherboard_temperature('MOTHERBOARD_TEMPERATURE_ARM')     # under the CPU shield
            temp['PHY']     = ib.get_motherboard_temperature('MOTHERBOARD_TEMPERATURE_PHY')     # also under the CPU shield

            test_results.power, test_results.current, test_results.voltage, test_results.temp = power, current, voltage, temp

            print "Total Power: ", totalPower
            for x in power:
                print x, ':   \t', power[x]
            print "\nCurrent:"
            for x in current:
                print x, ':   \t', current[x]
            print "\nVoltage:"
            for x in voltage:
                print x, ':   \t', voltage[x]
            print "\nTemperature:"
            for x in temp:
                print x, ':\t', temp[x]

            moffvoltage = {}
            moffvoltage['VCC3V3']  = ib.get_mezzanine_voltage('MEZZANINE_RAIL_VCC3V3', 1)  ,   ib.get_mezzanine_voltage('MEZZANINE_RAIL_VCC3V3', 2)
            moffvoltage['VCC12V0'] = ib.get_mezzanine_voltage('MEZZANINE_RAIL_VCC12V0', 1) ,   ib.get_mezzanine_voltage('MEZZANINE_RAIL_VCC12V0', 2)
            moffvoltage['VADJ']    = ib.get_mezzanine_voltage('MEZZANINE_RAIL_VADJ', 1)    ,   ib.get_mezzanine_voltage('MEZZANINE_RAIL_VADJ', 2)

            print "\nMezzanine Voltage:(Slot 1, Slot 2) when turned off"
            for x in moffvoltage:
                print x, ':   \t', moffvoltage[x]

            print "\nTurning on power to FMC slots"
            ib.set_mezzanine_power(True,1)
            ib.set_mezzanine_power(True,2)
            p1, p2 = ib.get_mezzanine_power(1), ib.get_mezzanine_power(2)
            assert (p1 and p2), "Mezzanines did not turn on properly! Slot 1: %s, Slot 2: %s" % (p1, p2)

            print "Reading FMC Slot Current & Voltage Sensors"
            mcurrent, mvoltage = {}, {}

            mcurrent['VCC3V3']  = ib.get_mezzanine_current('MEZZANINE_RAIL_VCC3V3', 1)  ,   ib.get_mezzanine_current('MEZZANINE_RAIL_VCC3V3', 2)
            mcurrent['VCC12V0'] = ib.get_mezzanine_current('MEZZANINE_RAIL_VCC12V0', 1) ,   ib.get_mezzanine_current('MEZZANINE_RAIL_VCC12V0', 2)
            mcurrent['VADJ']    = ib.get_mezzanine_current('MEZZANINE_RAIL_VADJ', 1)    ,   ib.get_mezzanine_current('MEZZANINE_RAIL_VADJ', 2)

            mvoltage['VCC3V3']  = ib.get_mezzanine_voltage('MEZZANINE_RAIL_VCC3V3', 1)  ,   ib.get_mezzanine_voltage('MEZZANINE_RAIL_VCC3V3', 2)
            mvoltage['VCC12V0'] = ib.get_mezzanine_voltage('MEZZANINE_RAIL_VCC12V0', 1) ,   ib.get_mezzanine_voltage('MEZZANINE_RAIL_VCC12V0', 2)
            mvoltage['VADJ']    = ib.get_mezzanine_voltage('MEZZANINE_RAIL_VADJ', 1)    ,   ib.get_mezzanine_voltage('MEZZANINE_RAIL_VADJ', 2)

            test_results.mcurrent, test_results.mvoltage = mcurrent, mvoltage

            print "Current:\t(Slot 1, Slot 2)"
            for x in mcurrent:
                print x, ':   \t', mcurrent[x]
            print "\nVoltage:\t(Slot 1, Slot 2)"
            for x in mvoltage:
                print x, ':   \t', mvoltage[x]

            print "Turning off power to FMC slots\n"
            ib.set_mezzanine_power(False,1)
            ib.set_mezzanine_power(False,2)
            p1, p2 = ib.get_mezzanine_power(1), ib.get_mezzanine_power(2)
            assert not (p1 or p2), "Mezzanines did not turn off properly! Slot 1: %s, Slot 2: %s" % (p1, p2)

            #assert max( [x for v in moffvoltage.values() for x in v]) < 0.2, "Mezzanines did not turn off properly!  When 'off' voltage measured is greater than 0.2V"

            values_ok = []
            for x in current:
                values_ok.append(in_range(current[x], cfg.current[x]['nom'],cfg.current[x]['pmargin'], cfg.current[x]['amargin'] ))
                if (values_ok[-1]==False):
                    print "The current on %s is out of range - measured current is: %.3f" %(x, current[x])

            for x in voltage:
                values_ok.append(in_range(voltage[x], cfg.voltage[x]['nom'],cfg.voltage[x]['pmargin'], cfg.voltage[x]['amargin']))
                if (values_ok[-1]==False):
                    print "The voltage on %s is out of range - measured voltage is: %.3f" %(x, voltage[x])

            for x in temp:
                values_ok.append(in_range(temp[x], cfg.temp[x]['nom'],cfg.temp[x]['pmargin'], cfg.temp[x]['amargin']))
                if (values_ok[-1]==False):
                    print "The temperature on %s is out of range - measured temp is: %.3f" %(x, temp[x])

            for x in mcurrent:
                for i in range(2):
                    values_ok.append(in_range(mcurrent[x][i], cfg.fmccurrent[x]['nom'],cfg.fmccurrent[x]['pmargin'], cfg.fmccurrent[x]['amargin']))
                    if (values_ok[-1]==False):
                        print "The mezzanine current on %s is out of range - measured current is: %.3f" %(x, mcurrent[x][i])

            for x in mvoltage:
                for i in range(2):
                    values_ok.append(in_range(mvoltage[x][i], cfg.fmcvoltage[x]['nom'],cfg.fmcvoltage[x]['pmargin'], cfg.fmcvoltage[x]['amargin']))
                    if (values_ok[-1]==False):
                        print "The mezzanine voltage on %s is out of range - measured voltage is: %.3f" %(x, mvoltage[x][i])

            for x in moffvoltage:
                for i in range(2):
                    values_ok.append(in_range(moffvoltage[x][i], cfg.fmcoffvoltage[x]['nom'],cfg.fmcoffvoltage[x]['pmargin'], cfg.fmcoffvoltage[x]['amargin'] ))
                    if (values_ok[-1]==False):
                        print "The mezzanine voltage on %s is out of range - measured votlage is: %.3f" %(x, moffvoltage[x][i])

            assert min(values_ok), "Sensor Values out of bound!"
            passed = True

        finally:
            self.instr.ps18v.output(state=False, readonly=False) # Turn power off
            xr.params.test_locals = locals()
            test_results.passed = passed
            xr.save_data(test_results)

        #Estimate 30 seconds

    def ser_test(self):
        """
        QC006: Program boards EEPROM and Flash with serial number

        Procedure:

          - Start the power sensor test on the computer
          - Type in serial number of tested board.
          - Board will have its serial programmed
          - Reeboot board, check if hostname correctly showes up.

        """
        xr.header('Programming serial number')

        cfg = self.cfg.motherboard_tests.ser_test

        test_results = NameSpace()
        passed = False

        (ib, ibs, ib_index) = self.connect_to_board(xr, cfg)
        question = "Is this a Rev %d board? [Y/N]" %cfg.rev
        if (input_yes_no(question, additional_answers=[])==True):
            rev = "%d" %cfg.rev
        else:
            rev = input("What is the revision of this board? e.g 4 ?")

        ipmi = ipmi_fru.FRU(
                    board=ipmi_fru.Board(
                        mfg_date=datetime.datetime.now(),
                        manufacturer="Winterland",
                        product_name="IceBoard",
                        part_number="MGK7MB",
                        serial_number=xr.params.serial,
                        fru_file=""),
                    product=ipmi_fru.Product(
                        manufacturer="Winterland",
                        product_name="IceBoard",
                        part_number="MGK7MB",
                        product_version=rev,
                        serial_number=xr.params.serial,
                        asset_tag="",
                        fru_file=""),
                    # multi=Multi(...), when it's supported by this code
                    )

        b64_string = base64.b64encode(ipmi.encode())
        ib._motherboard_spi_flash_write_base64(b64_string)
        ib._motherboard_eeprom_write_base64(b64_string)

        #SHOULD DO A MOTHERBOARD EEPROM READ HERE - ICECORE NEEDS UPDATE
        print "Rebooting the board"
        ib.reboot()
        time.sleep(5) #Need to wait long enough for the board to stop pinging after reboot

        (ib, ibs, ib_index) = self.connect_to_board(xr, cfg, questions=False, powerdown = False)

        #If it fails to connect we don't get this far and test ends
        print "The boards IPMI data is as follows:"
        print ib._get_motherboard_ipmi()
        if ib.get_motherboard_serial() == xr.params.serial:
            print "\nThe board has the correct serial number programmed"
            passed = True
        else:
            print "\nThe motherboard reports the following serial: %s which isn't correct" %ib.get_motherboard_serial()
            passed = False
            assert False, "Serial programmed incorectly"
        self.instr.ps18v.output(state=False, readonly=False) # Turn power off

    def i2c_test(self):
        """
        QC008: Check if all I2C devices are present

        Procedure:

          - Start the i2c test on the computerour
          - Type in serial number of tested board.
          - Check if expected devices are present
          - IOexpanders - GPIO/ LEDs
          - Measure temperatures

        """
        xr.header('Checking I2C connectivity')

        check_bp = input_yes_no("Should this test check for backplane I2C devices? [Y/N]", additional_answers=[])

        cfg = self.cfg.motherboard_tests.i2c_test
        test_results = NameSpace()
        passed = False

        (ib, ibs, ib_index) = self.connect_to_board(xr, cfg)
        hostname = ib.hostname

        cmd = "ls /sys/bus/i2c/devices/"
        ssh_cmd = 'ssh -o "StrictHostKeyChecking no" root@%s "%s"' % (hostname, cmd)
        split_cmd = shlex.split(ssh_cmd)

        p = subprocess.Popen(split_cmd, shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        result = p.stdout.readlines()

        print "Listing all the I2C devices found at /sys/bus/i2c/devices on the board:"
        print result

        matrix_names = ["pca9548-1-70", "pca9548-2-71"]
        matrix_addr = ["1-0070\n", "2-0071\n"]

        bus_names = [   "I2C-1", "I2C-2",
                        "I2C0_PCA954X_FMCA","I2C0_PCA954X_FMCB","I2C0_PCA954X_QSFPA","I2C0_PCA954X_QSFPB",
                        "I2C0_PCA954X_SFP","I2C0_PCA954X_SMPS","I2C0_PCA954X_BP","I2C0_PCA954X_GPIO",
                        "I2C1_PCA954X_FMCA","I2C1_PCA954X_FMCB","I2C1_PCA954X_QSFPA","I2C1_PCA954X_QSFPB",
                        "I2C1_PCA954X_SFP","I2C1_PCA954X_SMPS","I2C1_PCA954X_BP","I2C1_PCA954X_GPIO"]
        bus_addr = ["i2c-1\n", "i2c-2\n",
                 "i2c-5\n", "i2c-6\n", "i2c-7\n", "i2c-8\n",
                 "i2c-9\n", "i2c-10\n", "i2c-11\n", "i2c-12\n",
                 "i2c-13\n", "i2c-14\n", "i2c-15\n", "i2c-16\n",
                 "i2c-17\n", "i2c-18\n", "i2c-19\n", "i2c-20\n"]

        power_names = ["VCC12V0","VCC5V5", "VCC3V3",
                       "VADJ","VCC1V8", "VCC1V5",
                       "VCC1V2", "VCC1V0", "VCC1V0_GTX",
                       "FMC_A_VCC3V3", "FMC_A_VCC12V0", "FMC_A_VADJ",
                       "FMC_B_VCC3V3", "FMC_B_VCC12V0", "FMC_B_VADJ"]
        power_addr = ["10-0047\n", "10-0048\n", "10-0049\n",
                      "10-0043\n", "10-004b\n", "10-004c\n",
                      "10-004d\n", "10-004e\n", "10-004f\n",
                      "10-0040\n", "10-0041\n", "10-0042\n",
                      "10-0044\n", "10-0045\n", "10-0046\n"]

        temp_names = [ "POWER", "ARM", "FPGA", "PHY"]
        temp_addr = ["12-0048\n","12-004a\n","12-004b\n","12-004c\n"]

        io_names = ["pca9575_u41", "pca9575_u42", "pca9575_u48", "pca9575_u59"]
        io_addr = ["12-0020\n","12-0021\n","12-0022\n","12-0023\n"]

        eeprom_names = ["Motherboard EEPROM"]
        eeprom_addr = ["12-0057\n"]

        backplane_names = [ "BP_VCC3V3", "BP_Temp1", "BP_Temp2", "Backplane EEPROM"]
        backplane_addr =  ["19-0040\n", "19-004d\n", "19-004e\n", "19-0054\n"]

        i2c_names = matrix_names + bus_names + power_names + temp_names + io_names + eeprom_names
        i2c_addr  = matrix_addr  + bus_addr  + power_addr  + temp_addr  + io_addr  + eeprom_addr

        print "\nParsing the list looking for specific devices"

        passed = True
        for i,j in enumerate(i2c_addr):
            if not(j in result):
                print "Missing I2C device %s with address %s" %(i2c_names[i], i2c_addr[i])
                passed = False

        if (check_bp == True):
            for i,j in enumerate(backplane_addr):
                if not(j in result):
                    print "Missing I2C device %s with address %s" %(backplane_names[i], backplane_addr[i])
                    passed = False
        assert passed, "Missing I2C devices"
        print "All devices are present"

    def clock_test(self):
        """
        QC?: check if board will boot with backplane clock

        Procedure:

          - Start the bp_clock_test test on the computer
          - Move clock select jumper to backplane
          - Validate board boots
          - Check if board reports correct clock source

        """
        xr.header('Clock source test')

        cfg = self.cfg.motherboard_tests.clock_test
        test_results = NameSpace()
        passed = False

        print "\nPlease move the clock source jumper to the Crystal position. "
        (ib, ibs, ib_index) = self.connect_to_board(xr, cfg)
        clock_source = ib.get_clock_source()
        self.instr.ps18v.output(state=False, readonly=False) # Turn power off

        if (clock_source == ib.CLOCK_SOURCE.XTAL):
            print "Clock source is Crystal"
        else :
            print "Looks like the board booted but the board reported a different clock source to that specified: %s" %clock_source
            assert False, "Wrong clock selection detected"


        print "\nPlease move the clock source jumper to the SMA position. "
        print "Please attach an SMA cable between the front panel clock input and backplane clock out SMA"
        while (input_yes_no("Are you ready to power up the board? [Y/N]", additional_answers=[]) != True):
            pass;

        (ib, ibs, ib_index) = self.connect_to_board(xr, cfg, questions = False)
        clock_source = ib.get_clock_source()
        self.instr.ps18v.output(state=False, readonly=False) # Turn power off

        if (clock_source == ib.CLOCK_SOURCE.SMA):
            passed = True
            print "Board successfully booted and board reported that the SMA clock was used"
        else:
            print "Looks like the board booted but the board reported a different clock source to that specified: %s" %clock_source
            passed = False
            assert False, "Wrong clock source detected."

        print "\nPlease move the clock source jumper to the backplane position. "
        while (input_yes_no("Are you ready to power up the board? [Y/N]", additional_answers=[]) != True):
            pass;

        (ib, ibs, ib_index) = self.connect_to_board(xr, cfg, questions = False)
        clock_source = ib.get_clock_source()
        self.instr.ps18v.output(state=False, readonly=False) # Turn power off

        if (clock_source == ib.CLOCK_SOURCE.BP):
            passed = True
            print "Board successfully booted and board reported that the backplane clock was in use"
        else:
            print "Looks like the board booted but the board reported a different clock source to that specified: %s" %clock_source
            passed = False
            assert False, "Wrong clock source detected."

        passed = True
        print "Test passed"

    def fpga_test(self):
        """
        QC009: check if FPGA can be programmed - I don't think we want to run toptest script

        Procedure:

          - Start the fpga test on the computer
          - Type in serial number of tested board.
          - Connect to ARM and send bit file to FPGA
          - Check if Done pin goes high on ARM
          - Monitor power draw
          - Monitor temperatures
          - Read eeprom direct from FPGA to test I2C access

        """
        xr.header('FPGA test')

        cfg = self.cfg.motherboard_tests.fpga_test
        test_results = NameSpace()
        passed = False

        print "Please again ensure that the FPGA heatsink is in place, and with sufficient ventilation"
        print "Please ensure that SFP unit is inserted into board, with an ethernet cable attached."
        (ib, ibs, ib_index) = self.connect_to_board(xr, cfg)

        print 'Before programming the FPGA die temperature is: %.3f C' % ib.get_motherboard_temperature(ib.TEMPERATURE_SENSOR.MB_FPGA_DIE)
        pre_prog_power = ib.get_motherboard_power(ib.RAIL.MB_VCC3V3) + ib.get_motherboard_power(ib.RAIL.MB_VCC12V0) + ib.get_motherboard_power(ib.RAIL.MB_VCC5V5)
        print 'Before programming the boards power consumption is: %.3f W' % pre_prog_power

        self.prog_fpga(ib)
        ib.is_fpga_programmed()

        if(ib.is_voltage_nominal() == True):
            print "\nThe board reports that all the buck regulators have their voltages within 5% tolerance"
        else:
            voltage['VCC3V3']       = ib.get_motherboard_voltage('MOTHERBOARD_RAIL_VCC3V3')
            voltage['VCC12V0']      = ib.get_motherboard_voltage('MOTHERBOARD_RAIL_VCC12V0')
            voltage['VCC5V5']       = ib.get_motherboard_voltage('MOTHERBOARD_RAIL_VCC5V5')
            voltage['VCC1V0_GTX']   = ib.get_motherboard_voltage('MOTHERBOARD_RAIL_VCC1V0_GTX')
            voltage['VCC1V0']       = ib.get_motherboard_voltage('MOTHERBOARD_RAIL_VCC1V0')
            voltage['VCC1V2']       = ib.get_motherboard_voltage('MOTHERBOARD_RAIL_VCC1V2')
            voltage['VCC1V5']       = ib.get_motherboard_voltage('MOTHERBOARD_RAIL_VCC1V5')
            voltage['VCC1V8']       = ib.get_motherboard_voltage('MOTHERBOARD_RAIL_VCC1V8')
            voltage['VADJ']         = ib.get_motherboard_voltage('MOTHERBOARD_RAIL_VADJ')

            for x in voltage:
                values_ok.append(in_range(voltage[x], cfg.voltage[x]['nom'],cfg.voltage[x]['pmargin'], cfg.voltage[x]['amargin']))
                if (values_ok[-1]==False):
                    print "The voltage on %s is out of range - measured voltage is: %.3f" %(x, voltage[x])

            passed = False
            assert False, "One or more of the buck rails reports that its voltage is out of the permitted 5% tollerance margin"

        cookie = hex(ib.fpga_mmi_read(ib.FPGA_CORE_FIRMWARE_COOKIE_ADDR))
        print "\nThe FPGA memory map cookie (address 0) is: %s" % cookie

        if (cookie == '0xbeefface'):
            print "Correct cookie detected - memory map read back looks good"
        else:
            print "Invalid cookie detected - letting test continue for now - although FPGA memory map read back may be problematic"
            while (input_yes_no("Is the jumper J1 shorted? [Y/N]", additional_answers=[]) != True):
                pass;
            passed = False

        after_prog_power =  ib.get_motherboard_power(ib.RAIL.MB_VCC3V3) + ib.get_motherboard_power(ib.RAIL.MB_VCC12V0) + ib.get_motherboard_power(ib.RAIL.MB_VCC5V5)
        print '\nAfter programming the FPGA die temperature is: %.3f C' % ib.get_motherboard_temperature(ib.TEMPERATURE_SENSOR.MB_FPGA_DIE)
        print 'After programming the boards power consumption is: %.3f W' % after_prog_power

        try:
            ib.open()
            ib.is_core_open()
            print "\nDirect FPGA communications established through SFP unit."
        except:
            print "\nCannot communicate directly with the FPGA through the SFP unit."
            print "Remove the SFP unit, clean the contacts and re-run the test."
            print "If that does not work its likely that the FPGA bit file is the incorrect version - use latest on jfcdev"
            passed = False
            assert passed, "\nTest failed"

        print "\nThe firmware operating is version: %s" %ib.get_fpga_firmware_version()

        print "\nAtempting to read the motherboard eeprom directly from the FPGA - this tests connectivity to I2C matrix"
        partnum = ib.hw.read_motherboard_eeprom(40,6)
        if (partnum == 'MGK7MB'):
            print "The part number detected in the eeprom is: %s" %ib.hw.read_motherboard_eeprom(40,6)
        else:
            passed = False
            assert passed, "\nTest failed"

        passed = True

    def mezz_test(self):
        """
        QC0010: Check basic functionality of mezzanine

        Procedure:

          - Start the basic mezzanine test on the computer
          - Type in serial number of tested board.
          - Program FPGA
          - Read Mezzanien eeprom check for hardware suitability
          - Power on Mezzanine
          - test Power Good and PRSNT Line
          - Test SPI communication with ADC to confirm SPI bus connectivity to Mezz

        """
        xr.header('Mezz test')

        cfg = self.cfg.motherboard_tests.mezz_test
        test_results = NameSpace()
        passed = False

        print "Please ensure that the Mezzanines ARE mounted on the board for this test."
        (ib, ibs, ib_index) = self.connect_to_board(xr, cfg)

        assert ib.is_mezzanine_present(1), "Did not find Mezzanine on Slot 1."
        assert ib.is_mezzanine_present(2), "Did not find Mezzanine on Slot 2."

        self.prog_fpga(ib)
        ib.is_fpga_programmed()
        ib.open()

        if not (ib.is_core_open()):
            assert False , "Cannot communicate directly with the FPGA through the SFP unit."
        else:
            print "Communications estabiished with FPGA through SFP unit"

        m1_eeprom = base64.b64decode(ib._mezzanine_eeprom_read_base64(0))
        m2_eeprom = base64.b64decode(ib._mezzanine_eeprom_read_base64(0))
        if re.search('MGADC08',m1_eeprom):
            print "Read Mezzanine 1 eeprom and detected MGADC08"
        else:
            print "Read Mezzanine 1 eeprom and did not find MGADC08 - is mezzanien eeprom programed correctly?"

        if re.search('MGADC08',m2_eeprom):
            print "Read Mezzanine 2 eeprom and detected MGADC08"
        else:
            print "Read Mezzanine 2 eeprom and did not find MGADC08 - is mezzanien eeprom programed correctly?"

        #Turning on Mezzanines since they are MGADC08s
        ib.set_mezzanine_power(True , 1)
        ib.set_mezzanine_power(True , 2)

        p1, p2 = ib.get_mezzanine_power(1), ib.get_mezzanine_power(2)
        assert (p1 and p2), "Mezzanines did not turn on properly! Slot 1: %s, Slot 2: %s" % (p1, p2)

        print "Reading FMC Slot Current & Voltage Sensors"
        mcurrent, mvoltage = {}, {}

        mcurrent['VCC3V3']  = ib.get_mezzanine_current('MEZZANINE_RAIL_VCC3V3', 1)  ,   ib.get_mezzanine_current('MEZZANINE_RAIL_VCC3V3', 2)
        mcurrent['VCC12V0'] = ib.get_mezzanine_current('MEZZANINE_RAIL_VCC12V0', 1) ,   ib.get_mezzanine_current('MEZZANINE_RAIL_VCC12V0', 2)
        mcurrent['VADJ']    = ib.get_mezzanine_current('MEZZANINE_RAIL_VADJ', 1)    ,   ib.get_mezzanine_current('MEZZANINE_RAIL_VADJ', 2)

        mvoltage['VCC3V3']  = ib.get_mezzanine_voltage('MEZZANINE_RAIL_VCC3V3', 1)  ,   ib.get_mezzanine_voltage('MEZZANINE_RAIL_VCC3V3', 2)
        mvoltage['VCC12V0'] = ib.get_mezzanine_voltage('MEZZANINE_RAIL_VCC12V0', 1) ,   ib.get_mezzanine_voltage('MEZZANINE_RAIL_VCC12V0', 2)
        mvoltage['VADJ']    = ib.get_mezzanine_voltage('MEZZANINE_RAIL_VADJ', 1)    ,   ib.get_mezzanine_voltage('MEZZANINE_RAIL_VADJ', 2)

        test_results.mcurrent, test_results.mvoltage = mcurrent, mvoltage

        print "Current:\t(Slot 1, Slot 2)"
        for x in mcurrent:
            print x, ':   \t', mcurrent[x]
        print "\nVoltage:\t(Slot 1, Slot 2)"
        for x in mvoltage:
            print x, ':   \t', mvoltage[x]

        values_ok = []
        for x in mcurrent:
                for i in range(2):
                    values_ok.append(in_range(mcurrent[x][i], cfg.fmccurrent[x]['nom'],cfg.fmccurrent[x]['pmargin'], cfg.fmccurrent[x]['amargin']))
                    if (values_ok[-1]==False):
                        print "The mezzanine current on %s is out of range - measured current is: %.3f" %(x, mcurrent[x][i])

        for x in mvoltage:
            for i in range(2):
                values_ok.append(in_range(mvoltage[x][i], cfg.fmcvoltage[x]['nom'],cfg.fmcvoltage[x]['pmargin'], cfg.fmcvoltage[x]['amargin']))
                if (values_ok[-1]==False):
                    print "The mezzanine voltage on %s is out of range - measured voltage is: %.3f" %(x, mvoltage[x][i])

        assert min(values_ok), "Sensor Values out of bound!"


        #Checking if the mezzanine PG_M2C line is high - its one of the GPIOs that the ARM has access to
        #A bit over kill but I prefer getting the info via the arm and no tubber command exists for it
        #Could have gone via the FPGA instead
        cmd = "cat /sys/class/gpio/FMCA_PG_M2C/value"
        ssh_cmd = 'ssh -o "StrictHostKeyChecking no" root@%s "%s"' % (ib.hostname, cmd)
        split_cmd = shlex.split(ssh_cmd)
        p = subprocess.Popen(split_cmd, shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        result = p.stdout.readlines()
        if re.search("1",result[0]):
            print "Mezzanine 1 - PG_M2C line is detected high - mezzanine reports power good"
        else:
            print "Mezzanine 1 - PG_M2C line is not high - mezzanine did not report good power"
            assert False, "Mezzanine 1 - PG_M2C has problems"

        #Checking if the mezzanine PG_M2C line is high - its one of the GPIOs that the ARM has access to
        #A bit over kill but I prefer getting the info via the arm and no tubber command exists for it
        #Could have gone via the FPGA instead
        cmd = "cat /sys/class/gpio/FMCB_PG_M2C/value"
        ssh_cmd = 'ssh -o "StrictHostKeyChecking no" root@%s "%s"' % (ib.hostname, cmd)
        split_cmd = shlex.split(ssh_cmd)
        p = subprocess.Popen(split_cmd, shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        result = p.stdout.readlines()
        if re.search("1",result[0]):
            print "Mezzanine 2 - PG_M2C line is detected high - mezzanine reports power good"
        else:
            print "Mezzanine 2 - PG_M2C line is not high - mezzanine did not report good power"
            assert False, "Mezzanine 1 - PG_M2C has problems"

        passed = True


    def rmp_test(self):
        """
        QC0011: Check if high speed data lines of mezzanine function correctly

        Procedure:

          - Perform Ramp test
          - Check if spectra correct
          - Monitor FPGA temperature and power usage

        """
        xr.header('Ramp test')
        # Useful shortcuts
        cfg = self.cfg.motherboard_tests.rmp_test
        test_results = NameSpace()
        passed = False
        print "Please ensure mezzanines are loaded, and sufficient cooling for FPGA"
        (ib, ibs, ib_index) = self.connect_to_board(xr, cfg)

        self.prog_fpga(ib)
        ib.is_fpga_programmed()
        ib.open()

        try:
            test_results.data = []
            test_results.ramp_ok = []

            for mezz in ib.mezzanine.values():
                print 'initializing mezzanine...'
                mezz.init()

            print 'Computing ADC delays...'
            ib.set_adc_delays(compute_delays=2, save_delays=False, check_sync_delays=True, check_adc_delays=20, verbose=0, retry=5)
            delay_table = ib.get_adc_delays()
            print delay_table
            print '\nOpening data receiver socket'
            receiver = ib.get_data_receiver()

            print 'Setting up ramp transmission...'
            ib.set_adcdaq_mode('data')
            ib.set_data_source('adc')
            ib.set_adc_mode('ramp')
            ib.start_data_capture(period=1, source='adc')

            print 'Syncing...'
            ib.sync()

            print 'Getting data frames...'
            receiver.read_frames(flush=1, frames=3, verbose =0)  # flush
            data = []
            for i in range(16):
                data.append(receiver.read_frames(frames = 1, verbose =0))
                print len(data[i])
            #receiver.close()
            #ib.stop_data_capture()

            # frames = 1
            # good_frames = 0
            # while (frames!=0 and good_frames<frames):
            #     try:
            #         #print "trying to get a frame"
            #         data = receiver.read_frames(verbose=0)
            #         for chanNum in range(16):
            #             data_list[chanNum,:] = data[channels[chanNum]]
            #         good_frames+=1
            #         #print "got a frame"
            #         if (good_frames % 100) == 0:
            #             print 'Captured {0} frames'.format(good_frames) 
            #     except KeyError:
            #         print "missing a frame, skipping"
            #         print data
            #         missed += 1
            #         pass
            #     except ValueError:
            #         print "got a weird frame... carrying on!"
            #     except:
            #         receiver.close()
            #         ib.stop_data_capture()
            #         raise
            # print "lost {0} to get {1}".format(missed, frames)
            receiver.close()
            ib.stop_data_capture()

            test_results.data.append(data)
            plt.figure(1)

            ideal_ramp = (np.arange(2048) - 128).astype(np.int8)

            ramp_ok = []
            for ch in range(16):
                plt.clf()
                plt.plot(data[ch])
                xr.insert_plot('Ramp capture for %s SN%s CHANNEL %02i' % (xr.params.model, xr.params.serial, ch))
                ok = np.all(data[ch] == ideal_ramp)
                ramp_ok.append(ok)
                if ok:
                    print 'Channel %02i: OK' % (ch+1)
                else:
                    print 'Channel %02i: ERROR!' % (ch+1)
                test_results.ramp_ok.append(ramp_ok)

            assert all(test_results.ramp_ok), 'One or more channels have ramp errors'

            passed = True

        finally:
            ib.close()
            xr.params.test_locals = locals()  # store local variables for interactive debugging
            #receiver.close()
            xr.save_data(test_results)

    def qsfp_test(self):

        """
        QC0012: Check that QSFP is detected and that eeprom can be read

        Procedure:

          - Perform qsfp test
          - Check if device is detected
          - Check eeprom can be read

        """
        cfg = self.cfg.motherboard_tests.qsfp_test
        test_results = NameSpace()
        passed = False
        print "Please ensure QSFP cable is plugged into the motherboard in both ports, and sufficient cooling for FPGA"
        (ib, ibs, ib_index) = self.connect_to_board(xr, cfg)

        self.prog_fpga(ib)
        ib.is_fpga_programmed()
        ib.open()

        xr.header('Test-Results')
        qsfp_info = []
        try:
            qsfp_present = [0, 0]

            for i in [1, 2]:
                if ib.is_qsfp_present(i):
                    print "Motherboard QSFP module present on port %d" %i
                    qsfp_present[i-1] = 1
                else:
                    print "Motherboard QSFP module NOT present on port %d" %i
                assert qsfp_present[i-1], "Not all connectors were detected!"

                print "Resetting Module " + repr(i)
                ib.set_qsfp_gpio(ib.QSFP_GPIO.ResetL, i, False)
                time.sleep(1)
                ib.set_qsfp_gpio(ib.QSFP_GPIO.ResetL, i, True)
                ib.set_qsfp_gpio(ib.QSFP_GPIO.ModSelL, i, False)
                qsfp_info.append(base64.b64decode(ib._qsfp_eeprom_read_base64(i, 148, 16)).strip())
                qsfp_info.append(base64.b64decode(ib._qsfp_eeprom_read_base64(i, 196, 16)).strip())
                print "QSFP module " + repr(i) +" manufactured by: "+ qsfp_info[0] + ". Serial number: " + qsfp_info[1] + ".\n"

            if re.search(cfg.manufacturer,qsfp_info[0]) and re.search(cfg.manufacturer,qsfp_info[2]) and\
               re.search(cfg.serial,qsfp_info[1]) and re.search(cfg.serial,qsfp_info[3]) :
                print "Detected that the cable was manufactured by " + cfg.manufacturer + " and has serial " + cfg.serial + " as indicated in the config file."
                passed = True
            else:
                passed = False
                assert False,"Cable did not read correctly or is not specified correctly in the test config"
        finally:
            xr.params.test_locals = locals()

    def gtx_test(self):
        """
        QC0013: Check if high speed links work

        Procedure:

          - With one slot backplane  - where we already using this?
          - Plug in QSFP loopback
          - Type in serial number of tested board.
          - Program FPGA
          - Check for link integrity
          - monitor FPGA temp

        """
        xr.header('gtx test')
        cfg = self.cfg.motherboard_tests.gtx_test

        test_results = NameSpace()
        passed = False
        print "Please ensure the QSFP cable is plugged into the motherboard in both ports, and sufficient cooling for FPGA"
        print "The motherboard must be plugged into the one slot backplane"
        (ib, ibs, ib_index) = self.connect_to_board(xr, cfg)

        self.prog_fpga(ib)
        ib.is_fpga_programmed()
        ib.open()

        xr.header('Test-Results')
        print "\nMeasuring  gtx error rate over a 20 second period"
        meas_ber = ibs.get_ber(tx_power = 12, print_=True, period = 20)
        print
        print meas_ber
        def searchErrDict(dic):
            if dic == {}:
                yield 0
            for key in dic.keys():
                if type(dic[key]) == type({}):
                    for value in searchErrDict(dic[key]):
                        yield value
                elif type(dic[key]) == type([]):
                    for item in dic[key]:
                        for value in searchErrDict(item):
                            yield value
                else:
                    yield 1

        #finally:
            #xr.save_data(status)
        xr.params.test_locals = locals()


if __name__ == '__main__':
    """ Run the test in this file."""
    v = util.run_tests(TEST_CONFIG_FILE)
    locals().update(v) # bring local variables from the test runner into the current namespace for easier debugging