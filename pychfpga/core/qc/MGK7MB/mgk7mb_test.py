
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
import memtest_rs232 as rs232

import visa  #run "pip install pyvisa"   and  "pip install -U https://github.com/hgrecco/pyvisa-py/zipball/master"


util.add_paths('../..')  # needed to find icecore
from icecore import XReport as xr
from icecore.tests.xreport import test_report
from icecore.hw import ipmi_fru

util.add_paths('../../..')  # needed to find fpga_array
from fpga_array import FPGAArray

TEST_CONFIG_FILE = './mgk7mb_test_config.yaml'

rm = visa.ResourceManager('@py')
dmm_visa = rm.open_resource('TCPIP0::192.168.0.130::inst0::INSTR')

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

class MGK7MBTests(unittest.TestCase):  #
    """
    Perform impedance & power tests on the MGADC08 Mezzanine.
    """
    def setUp(self):
        """ Prepare the test for execution.

        Here, we grab the command line arguments and parse them.
        """
        xr.header('Setting-up')
        self.cfg = util.load_config(TEST_CONFIG_FILE)
        cfg = self.cfg.motherboard_tests.setup  # config options pertaining to setup
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
        print '   - the ground clip is attached to the board stifferner'

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

        while (input_yes_no("Please ensure that no flash card is in the board. Ready to continue? [Y/N]", additional_answers=[]) != True):
            pass;
        while (input_yes_no("Please connect PLL programming dongle to the 6pin header. Ready to continue? [Y/N]", additional_answers=[]) != True):
            pass;
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

        while (input_yes_no("Please ensure that a flash card is now plugged into the board. Ready to continue? [Y/N]", additional_answers=[]) != True):
            pass;
        while (input_yes_no("Please connect RS232 dongle to connector next to the 3x2 LED stack. Ready to continue? [Y/N]", additional_answers=[]) != True):
            pass;
        while (input_yes_no("Are you ready to apply power to the board? [Y/N]", additional_answers=[]) != True):
            pass;


        #chmod 777 /dev/ttyUSB* allowed access to screen, probably not the right thing to do!
        #Should probably write another permissions file as for the PLL test
        [ser , rs232dongle_dev] =  rs232.init_rs232()
        print "Found RS232 dongle on port" + rs232dongle_dev

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

    def sens_test(self):
        """
        QC006: Read the onboard power sensors and temperature sensors

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
        print "No test written"
        passed = True

        #Estimate 30 seconds

    def ser_test(self):
        """
        QC007: Program boards EEPROM and Flash with serial number

        Procedure:

          - Start the power sensor test on the computer
          - Type in serial number of tested board.
          - Board will have its serial programmed
          - Reeboot board, check if hostname correctly showes up.

        """
        xr.header('Programming serial number')
        print "No test written"
        passed = True

    def i2c_test(self):
        """
        QC008: Check if all I2C devices are present

        Procedure:

          - Start the i2c test on the computer
          - Type in serial number of tested board.
          - Check if expected devices are present
          - IOexpanders - GPIO/ LEDs
          - Check if board thinks its in a backplane or not
          - Measure temperatures
          - With no mezzanines present - enable disable power - read voltages

        """
        xr.header('Programming serial number')
        print "No test written"
        passed = True

    def fpga_test(self):
        """
        QC009: check if FPGA can be programmed - I don't think we want to run toptest script

        Procedure:

          - Start the fpga test on the computer
          - Type in serial number of tested board.
          - Connect to ARM and send bit file to FPGA
          - Check if Done pin goes high on ARM
          - Monitor current draw from FPGA I2C bus
          - Validate that SFP Present and FAULT line can be read (tests SFP & IO_Extender connectivity)

        """
        xr.header('FPGA test')
        print "No test written"
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
          - check that SYNC stops clock and that RESET restores ADC registers and IO Expanders registers
          - Test SPI communication with ADC to confirm SPI bus connectivity to Mezz
          - Check M2C reference clock frequencies (both).

        """
        xr.header('Mezz test')
        print "No test written"
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
        print "No test written"
        passed = True

    def gtx_test(self):
        """
        QC0012: Check if high speed links work

        Procedure:

          - With one slot backplane  - where we already using this?
          - Plug in QSFP loopback
          - Type in serial number of tested board.
          - Program FPGA
          - Check for link integrity
          - monitor FPGA temp

        """
        xr.header('gtx test')
        print "No test written"
        passed = True


if __name__ == '__main__':
    """ Run the test in this file."""
    v = util.run_tests(TEST_CONFIG_FILE)
    locals().update(v) # bring local variables from the test runner into the current namespace for easier debugging