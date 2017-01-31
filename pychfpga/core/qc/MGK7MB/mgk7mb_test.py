
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

TEST_CONFIG_FILE = './mgk7mb_test_config.yaml'

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

        self.instr.dmm.set_beeper(True)
        self.instr.dmm.display('Ready for','MGK7MB tests')

        xr.header('Test results')


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
        print "No test written"
        passed = True

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
        print "No test written"
        passed = True

    def mem_test(self):
        """
        QC005: Perform the ARM memory test

        Procedure:

          - Connect RS232 cable to the board (might need to think about which RS232 port)
          - Type in serial number of tested board.
          - Enable board power
          - Interupt boot and start memtest - let run for 3 memory pass runs
          - Ask user if test passed.

        """
        #Connect to RS232 dongle
        #Power up board
        #Run memtest

        #We may be able to do something like get uboot to test for a dipswitch and automatically run
        #memtest and flash LEDs to indicate test status. This would mean that RS232 is then only needed
        #to confirm/diagnose problems rather than the default stratergy
        xr.header('Mem test')
        print "No test written"
        passed = True

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