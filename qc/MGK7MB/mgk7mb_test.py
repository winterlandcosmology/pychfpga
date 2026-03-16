
""" Class and methods used for the testing of the MGK7MB FPGA motherboard, a.k.a ICEBoard.
pytest and the wtl.pytest-xreport plugin  is used for performing the tests and reporting the results. 
"""

# Standard packages
import os
import time
import base64
import datetime
import subprocess
import shlex
import re

# Pypi packages
import pytest
import numpy as np
import matplotlib.pyplot as plt

from prettytable import PrettyTable, ORGMODE 

# External private packages

from wtl.namespace import NameSpace
from wtl.pytest_xreport import xr, TestMenu
from wtl.pytest_xreport.xreport import XReport
import labpy
import pychfpga

from pychfpga import fpga_array 
from pychfpga.common import run_async, async_to_sync
from pychfpga.hardware.interfaces import ipmi_fru

from pychfpga.fpga_firmware import FPGABitstream

# local packages
from memtest_rs232 import MemTestRS232
import cdce620005_pll_QC as cdce620005 

utils = __import__('ice-qc.utils') #import doens't like the dash in the name
TestUtils = utils.utils.TestUtils

if os.environ.get("PYTEST_PLUGINS") is not None and os.environ.get("PYTEST_PLUGINS") != "":
    os.environ["PYTEST_PLUGINS"] += ",wtl.pytest_xreport"
else:
    os.environ["PYTEST_PLUGINS"] = "wtl.pytest_xreport"

TEST_CONFIG_FILE = './test_config.yaml'

def in_range(value, target, pmargin=0.05, amargin=0):
    if( (value < target * ( 1 - pmargin) - amargin) or
        (value > target * ( 1 + pmargin) + amargin)):
            return False
    else:
            return True

"""
class TestUtils:
    " Some utility methods common to all tests. 
    "
    def open_instrument(self, name):
        " Looks up the instrument name in the instrument table in configuration file and open it with the parameters specified in the table.  
        "
        instr_params = self.cfg.instruments[name].copy()
        class_name = instr_params.pop('labpy_object')
        return labpy.open_instrument(class_name, **instr_params)

    def open_ps(self, name='ps16v', voltage=None, current=None):
        " Opens a power supply and configures it

        The `voltage` and `current` to be programmed can be specified. If `None`, the global values from
        the config file in ``motherboard_tests.global_settings`` will be
        used.
        "
        
        # open power supply instrument if it is in the list of instruments and if we don't force manual operation /self.cfg.get('manual_ps', False)/
        if True and name in self.cfg.instruments:
            print("Tried to connect")
            self.ps = self.open_instrument(name)
        else:
            self.ps = None


        # initialize power supply if we have one
        if name == 'ps18v' and self.ps:
            voltage = voltage if voltage is not None else self.cfg.motherboard_tests.global_settings.ps_voltage
            current = current if current is not None else self.cfg.motherboard_tests.global_settings.ps_current
            self.ps.set_output(state=False) #Ensuring power on N5764A is off
            self.ps.clear() #Clearing any previous protection
            self.ps.set_voltage(voltage=voltage) #Setting voltage to 18V, power still off
            self.ps.set_current_limit(current=current, ocp=True) #Setting current limit and turning on ocp feature


        if name == 'ps16v' and self.ps:
            voltage = voltage if voltage is not None else self.cfg.motherboard_tests.global_settings.ps_voltage
            current = current if current is not None else self.cfg.motherboard_tests.global_settings.ps_current
            self.ps.set_output(state=False)
            #self.ps.set_current_limit(current=current)


        return self.ps
        

      """ 




class TestMGK7MBBench(TestUtils):
    """
    Perform basic Iceboards tests on the bench, without network connectivity.
    """

    @pytest.fixture(autouse=True)
    def setup(self, xr, instruments):
        """ Prepare the test for execution. This fixture is executed automatically before each test.
        """
        xr.header('Setting-up')
        self.cfg = xr.config  # get the test config NameSpace
        # pre-define instrument variable. We'll load them only as needed by the tests.

        self.instruments = instruments #creates the shared instrument dictionary object for all tests

        # self.dmm = None

        self.ps = None
        yield  # pass control to the test and return
        if self.ps:
            self.ps.set_output(state=False)
            self.ps.close()
        # turn off power supply
        # if self.ps:
        #     self.ps.set_output(state=False) #Ensuring power on N5764A is off

    def test_insp(self, xr):
        """
        QC001: Inspection test: visual check of the board

        Procedure:

          - Start the inspection test on the computer
          - Type in serial number of tested board.
          - Visually inspect specific points mentioned in test

        """
        xr.header('Inspection test')
        

        questions = self.cfg.motherboard_tests.inspection.questions
        answers = []

        for i, question in enumerate(questions):
            answers.append(xr.input_yes_no(f"{i+1}) {question}", additional_answers=[]))

        passed =  True
        for i, ans in enumerate(answers):
            if not ans:
                print(f"Please address inspection line {i+1}")
                passed = False
        assert passed, 'Inspection Test failed'


        comments = xr.input("If there are any additional comments you wish to make (e.g. scratches, manufacturing problems), please describe below. (If none, enter 'None'): ")
        passed = True

        #Estimate 30 seconds

    def test_res(self, xr):
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
        xr.header('Impedance test')
        cfg = self.cfg.motherboard_tests.impedance
        
        dmm = self.open_instrument('dmm')
        
        dmm.set_beeper(True)
        dmm.display('Ready for','MGK7MB tests')

        test_results = NameSpace()

        print('-------------------------------')
        print(' Make Sure that ')
        print('   - the backplane is attached to the board')
        print('   - the power cable is NOT connected to the board under test')
        print('   - the ground clip is attached to the board stiffener')

        #for ps in pss:  # Turn off both power supplies, just to be sure
        #    ps.output_enable(0)

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
                    print('Optional: Press the blue (SHIFT/Local) button on the multimeter so see real-time resistance measurements')
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
            #dmm.local()
            xr.save_data(test_results)

    def test_powerup(self, xr):
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
        self.ps = self.open_instrument('ps16v')

        manual_ps = not self.ps

        print('\n-------------------------------')
        print('Connect the power cable to the one slot backplane.')
        print('Make sure there is no mezzanine & SD card: these will cause the board to draw additional current')
        print(' We expect the board to use about 0.83A without fan, or 0.90A with fan')

        if manual_ps:
            print()
            print('Power ON the board, NOTE THE CURRENT on power supply, power OFF the board.\n ')

            while True:
                current = xr.input('What current did you measure (just a number, no units)?')
                try:
                    current = float(current)
                    break
                except ValueError:
                    print('Just numbers e.g.: 0.88')

            current_good = current < cfg.imax and  current > cfg.imin
            assert current_good, "Current is out of range"
        else:
            while (xr.input_yes_no("Are you ready to apply power to the board? [Y/N]", additional_answers=[]) != True):
                pass

            self.ps.set_output(state=True)
            self.ps.pollstatus(polltime=0.1, runtime=1)
            
            status = self.ps.status()
            self.ps.set_output(state=False) #Ensuring power on N5764A is off
            if (status['status']=='OK'):
                if (status['current'] < cfg.imax) and ( status['current'] > cfg.imin):
                    passed = True
                else:
                    passed = False
                    print("Measured current is out of range")
                    assert False, "Current out of range"
            else:
                print("Fault detected - either current limiting or off, status is:" + status['status'])
                passed = False
                assert False, "Fault detected"

        print("Looks like the current draw is in range")

        print("\nBe ready to inspect the 9 power LEDs at the back of the board")

        if manual_ps:
            print('Apply power to the board')
        else:
            while (xr.input_yes_no("Are you ready to apply power to the board again? [Y/N]", additional_answers=[]) != True):
                pass
            self.ps.set_output(state=True)

        response = xr.input_yes_no("Are all 9 of the power LEDs turned on? Y/N]", additional_answers=[])
        if manual_ps:
            xr.input('Turn OFF power and press ENTER')

        if self.ps:
            self.ps.set_output(state=False)
        if response == True:
            print("Test has passed")
        else:
            xr.input("Which lights failed to light up?")
            assert False, "Some of the buck converters have not registered power good"


    def test_pll(self, xr):
        """
        QC004: Program the onboard PLLs

        Procedure:

          - Start the pll test on the computer
          - Type in serial number of tested board.
          - Attach dongle to PLL 1
          - Board will power up and program PLL 1
          - Atach dongle to PLL 2 (Move brown wire)
          - PLL 2 will be programed and board power disabled.


        Note:

            - Requires access rights to the USB port. Ways to do this:

                sudo usermod -a -G  dialout qcadmin  # add user to the group to which the port is assigned
        """
        #Connect to PLL programmer dongle
        #Power up and program the PLLS
        #Restart the board and ask user if PLL lock lights turned on.

        #Think about adding: Check that Both PLL lock (check LED) when using SMA.
        xr.header('PLL test')

        cfg = self.cfg.motherboard_tests.pll
        self.ps = self.open_instrument('ps16v')

        manual_ps = not self.ps

        print('\n-------------------------------')
        print("Please ensure that NO flash card is in the board (if the ARM boots, it will interfere with the SPI signals to the PLLs).")
        print("Please make sure power is OFF and connect PLL programming dongle to the 6pin header (the ground (black) wire should be on the side of the SFP connector).")

        if manual_ps:
            xr.input('Turn power supply ON and press ENTER')
        else:
            xr.input("Press [ENTER] when ready to apply power to the board")
            self.ps.set_output(state=True)

        try:
            pll1 = cdce620005.CDCE620005(cfg.ftdi_url, port=0) # instantiate PLL1
            pll2 = cdce620005.CDCE620005(cfg.ftdi_url, port=1) # instantiate PLL2
        except Exception:
            print('Error opening serial port using the USB FTDI cable. '
                  'Please ensure that the USB permissions has been set '
                  'according to pyftdi install instructions.')
            raise

        #cdce.write_pll_reg(cdce.pll1port, 0 , 0) #temporarily make reg 0 on pll 1 wrong

        print("\nComparing PLL1 desired settings with measured settings:")
        meas_pll1_regs = pll1.read_pll()
        pll1_cmp = cdce620005.comp_reg(cfg.pll1regs, meas_pll1_regs)

        print("\nComparing PLL2 desired settings with measured settings:")
        meas_pll2_regs = pll2.read_pll()
        pll2_cmp = cdce620005.comp_reg(cfg.pll2regs, meas_pll2_regs)

        if (all(v==0 for v in meas_pll1_regs) and all(v==0 for v in meas_pll1_regs)):
            while (xr.input_yes_no("All the registers in both PLLs are 0.  Please ensure that dongle orientated correctly on the program header. Ready to continue? [Y/N]", additional_answers=[]) != True):
                pass
            xr.input_yes_no("Out of interest was it on the right way? [Y/N]", additional_answers=[])

        if (all(v==0xFFFFFFFF for v in meas_pll1_regs) and all(v==0xFFFFFFFF for v in meas_pll1_regs)):
            while (xr.input_yes_no("All the registers in both PLLs are 0xFFFFFFFF.  Please ensure that dongle is plugged into the program header. Ready to continue? [Y/N]", additional_answers=[]) != True):
                pass
            xr.input_yes_no("Out of interest was it plugged in? [Y/N]", additional_answers=[])

        if pll1_cmp == 0:
            print("\nDifferences were detected on PLL1 - Programing it")
            pll1.program_pll(cfg.pll1regs, write_eeprom=True)
            time.sleep(2)
            meas_pll1_regs = pll1.read_pll()
            print("\nComparing settings again:")
            pll1_cmp = cdce620005.comp_reg(cfg.pll1regs, meas_pll1_regs)
            if pll1_cmp:
                print("PLL1 successfully programed")
            else:
                passed = False
                print("Settings are still wrong - is the dongle orientated correctly on the program header?")
                assert False

        if pll2_cmp ==0:
            print("\nDifferences were detected on PLL2 - Programing it")
            pll2.program_pll(cfg.pll2regs,write_eeprom=True)
            time.sleep(2)
            meas_pll2_regs = pll2.read_pll()
            print("\nComparing settings again:")
            pll2_cmp = cdce620005.comp_reg(cfg.pll2regs, meas_pll2_regs)
            if pll2_cmp:
                print("PLL2 successfully programed")
            else:
                print("Settings are still wrong - Not sure whats wrong since PLL1 worked, perhaps PLL2 is now locked?")
                passed = False
                assert False

        print("Both PLLs have the correct settings programmed")


        if manual_ps:
            xr.input('Turn power supply OFF and back ON and press ENTER')
        else:
            print("Rebooting the board")
            self.ps.set_output(state=False)  # Ensuring power on N5764A is off
            time.sleep(1) 
            self.ps.set_output(state=True)  # Turning power back on

        response = xr.input_yes_no("Are both PLL lock lights turned on? (yellow and green next to 6 pin RS232 header) [Y/N]", additional_answers=[])
        if response == True:
            passed = True
        else:
            print("Looks like something has gone wrong - retry the test or check PLL soldering")
            passed = False

        if manual_ps:
            xr.input('Turn power supply OFF and press ENTER. You can then disconnect the PLL dongle: it is no longer needed for this board')

        if self.ps:
            self.ps.set_output(state=False)

        assert passed
        #self.instr.ps.set_output(state=False) #Ensuring power on N5764A is off
        #Estimate 10 seconds

    def test_mem(self, xr):
        """
        QC005: Perform the ARM memory test

        Procedure:
          - Connect RS232 cable to the board
          - Type in serial number of tested board.
          - Enable board power
          - Interupt boot and start memtest - let run for 5 memory pass runs

        """
        
        cfg = self.cfg.motherboard_tests.mem_test
        self.ps = self.open_instrument('ps16v')

        manual_ps = not self.ps

        xr.header('Mem test')

        print('\n-------------------------------')
        print("Please ensure board is powered OFF.")
        print("Please ensure that a flash card is plugged into the board.")
        print("Please connect RS232 dongle to connector next to the 3x2 LED stack.")
        print("This is the lower row of 3 pins, with the red wire towards the LEDs.")


        try:
            ser = MemTestRS232(cfg.ftdi_url)
        except Exception:
            print('Error opening serial port using the USB FTDI cable. '
                  'Please ensure that the USB permissions has been set '
                  'according to pyftdi install instructions.')
            raise

        print(f"Found RS232 dongle on port {ser.dev}")

        if manual_ps:
            xr.input('press ENTER and *then* turn power supply ON')
        else:
            xr.input("Press [ENTER] when ready to turn power ON")
            self.ps.set_output(state=True)

        ser.interupt_boot()
        testpassed = ser.start_memtest(iterations=5)
        ser.close()

        
        if testpassed == 1:
            print("The memory is good - all iterations passed")
            passed = True
        else:
            print("The memory test failed")
            passed = False
        
        if manual_ps:
            xr.input('Power OFF the board and press ENTER. You can then disconnect the RS232 dongle: it is no longer needed with this board')

        if self.ps:
            self.ps.set_output(state=False)
 
        assert passed, "Memory test failed"

        #Measured time 90 seconds with 5 iterations (15secs setup, 15secs per iteration)



class TestMGK7MBNetwork(TestUtils): 
    """
    Perform functional tests on a  MGK7MB motherboard connected to the network.
    """

    @pytest.fixture(autouse=True)
    def setup(self, xr, instruments):
        """
        Prepare the test for execution.

        Here, we grab the command line arguments and parse them.

        """
        xr.header('Setting-up')
        self.xr = xr
        self.cfg = xr.config
        self.params = xr.params  # xr.params is a mutable objects, so self.params points to the same object 

        assert self.params.model, 'Need a model number' 
        assert self.params.serial, 'Need a serial number to either find an existing board a program a new one'
        
        self.instruments = instruments

        self.ps = None
        yield
        if self.ps:
            print('.')
            # self.ps.set_output(state=False)
            # self.ps.close()
        xr.header('Tearing down')
        # make sure the power suply is of if it was used in the test
        
        #if self.ps:
        #    self.ps.set_output(state=False)

    def connect_to_board(self, questions = True, power_up = True, power_down=False, program = False, open_fpga=False, without_serial=False):
        """

        Parameters: 
            power_down (bool): power down the board before powering it up

            power_up (bool): power up the board

            without_serial (bool): if True, boards without serial numbers and advertised as 'iceboard.local' will be returned if we can't find one with the target serial number.

        """
        self.ps = self.open_instrument('ps16v')

        manual_ps = not self.ps
        if questions:
            print('\n-------------------------------')
            print("Please ensure that a flash card is plugged into the board")
            print("Please ensure that the ethernet cable is plugged in to the board")

        if manual_ps:
            if power_down and power_up:
                self.xr.input('Please turn power OFF and then back ON and press ENTER')
            else:
                if power_down:
                    self.xr.input('Please turn power OFF and press ENTER')
                if power_up:
                    self.xr.input('Please turn power supply ON, wait for a single green light and press ENTER')
        else:
            if power_down:
                self.ps.set_output(state=False)
                time.sleep(3)
            if power_up:
                self.xr.input("Press [ENTER] when ready to power the board")
                self.ps.set_output(state=True)
                time.sleep(2) #give the board time to initialise

        
        #print(fpga_array.IceBoard._class_registry)

        # If we expect to program a blank new board, first quickly check if a board with the specified  serial number exists over mDNS, then look for a generic 'iceboard.local' board.
        # Since we just powered up the board, it might take some time to find it, so we continuously check for both programmed and unprogrammed boards.  
        for count in range(60):
            # Try to find an Iceboard already configured with the target serial 
            print(f'Trial {count+1}/60: looking for iceboard{self.params.serial}.local')

            #THIS HAD TO BE MODIFIED TO FIT THE NEW VERSION
            #ip = pychfpga.mdns_resolve(f'iceboard{self.params.serial}.local', timeout=2)

            ip = pychfpga.mdns_discovery.mdns_resolve(f'iceboard{self.params.serial}.local', timeout=2)
           
            
            if ip:
                ca = fpga_array.FPGAArray(f'MGK7MB {ip}', ping=1, ignore_missing_boards=True)
                if ca.ib:
                    break
            # hwm = f'{self.params.model} {self.params.serial}'
            # ca = fpga_array.FPGAArray(hwm, mdns_timeout=1, ping=1)  # don't wait too long in case the board is not configured
            # if ca.ib:
            #     break
            # no, so if requested, try to find an unprogrammed iceboard
            if without_serial:
                print(f'Trial {count+1}/30: looking for iceboard.local')
                ip = pychfpga.mdns_discovery.mdns_resolve('iceboard.local', timeout=2)
                if ip:
                    ca = fpga_array.FPGAArray(f'MGK7MB {ip}', ping=1, ignore_missing_boards=True)
                    if ca.ib:
                        break
        else:  # if we reach the end of the loop without break
            assert False, "Board was not found on the network. Check the cabling, and power cycle again."

        assert len(ca.ib) == 1, "More than one iceboard with no serial number was found"
        ib = ca.ib[0]

        if ib.serial:
            assert ib.serial == self.params.serial, 'Serial number mismatch' # Should not happen
            print(f"Found an iceboard with the correct serial number on the network")
        else:
            print("Found an iceboard with the no serial number programmed on the network")

        print(ib)

        # Monkey-patch the iceboar dobject to add async functions used in our tests 
        ib.set_fpga_bitstream = async_to_sync(ib.set_fpga_bitstream_async)

        # Add synchronous versions of the async tuber commands for convenience
        ib.is_fpga_programmed = async_to_sync(ib.tuber_is_fpga_programmed_async)
        ib.get_clock_source = async_to_sync(ib.tuber_get_clock_source_async)
        ib.is_mezzanine_present = async_to_sync(ib.tuber_is_mezzanine_present_async)
        ib.get_motherboard_power = async_to_sync(ib.tuber_get_motherboard_power_async)
        ib.get_motherboard_current = async_to_sync(ib.tuber_get_motherboard_current_async)
        ib.get_motherboard_voltage = async_to_sync(ib.tuber_get_motherboard_voltage_async)
        ib.get_motherboard_temperature = async_to_sync(ib.tuber_get_motherboard_temperature_async)
        ib.get_mezzanine_voltage = async_to_sync(ib.tuber_get_mezzanine_voltage_async)
        ib.set_mezzanine_power = async_to_sync(ib.tuber_set_mezzanine_power_async)
        ib.get_mezzanine_power = async_to_sync(ib.tuber_get_mezzanine_power_async)
        ib.get_mezzanine_current = async_to_sync(ib.tuber_get_mezzanine_current_async)
        ib._mezzanine_eeprom_read_base64 = async_to_sync(ib._tuber_mezzanine_eeprom_read_base64_async)
        ib.is_voltage_nominal = async_to_sync(ib.tuber_is_voltage_nominal_async)
        ib._motherboard_spi_flash_write_base64 = async_to_sync(ib._tuber_motherboard_spi_flash_write_base64_async)
        ib._motherboard_eeprom_write_base64 = async_to_sync(ib._tuber_motherboard_eeprom_write_base64_async)
        ib._get_motherboard_ipmi = async_to_sync(ib._tuber_get_motherboard_ipmi_async)
        ib.get_motherboard_serial = async_to_sync(ib.tuber_get_motherboard_serial_async)
        ib.fpga_mmi_read = async_to_sync(ib.fpga_core_reg_spi_read_async) 
        ib._qsfp_eeprom_read_base64 = async_to_sync(ib._tuber_qsfp_eeprom_read_base64_async)



        

        if program:
            self.prog_fpga(ib)


        
        if open_fpga:
            self.open_fpga(ib)


        return (ib, ca)

    def prog_fpga(self, ib):

        print("Programming FPGA. This takes about 20 seconds...")
        ib.set_fpga_bitstream(firmware_mode = self.cfg.fpga_firmware_mode, force=False)
        assert ib.is_fpga_programmed(), 'FPGA has not programmed'

    def open_fpga(self, ib):
        #Currying function to include ip address lambda function
        ip_fn = lambda a, b, c, d: (a, b, 3, d)
        run_async(ib.open_fpga_async(verbose=1, fpga_ip_addr_fn = ip_fn)) 
        print(f"FPGA: {ib.fpga}")

    def get_qfsp_info(self, qsfp):
        """
        Reads and formats qsfp info, returns manufacturer and serial number
        """

        info = qsfp.get_info()        

        mfg = info['VendName'].decode('ascii').rstrip() #data is right paded with withespaces
        serial = info['VenSN'].decode('ascii').rstrip()

        return (mfg, serial)


    def run_arm_system_command(self, hostname, cmd):
        ssh_cmd = f'ssh -o "KexAlgorithms=diffie-hellman-group14-sha1" -o "HostKeyAlgorithms=+ssh-dss" -o "StrictHostKeyChecking=no" root@{hostname} "{cmd}"'
        split_cmd = shlex.split(ssh_cmd)
        p = subprocess.Popen(split_cmd, shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='ascii')
        result = p.stdout.readlines()
        return result

    def test_sens(self, xr):
        """
        QC007: Read the onboard power sensors and temperature sensors

        Procedure:

          - Start the sensor test on the computer
          - Type in serial number of tested board.
          - Sensor data will be acquired from iceboard.local
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

        print('\n-------------------------------')
        print("Please ensure that the Mezzanines are NOT mounted on the board for this test")
        ib, _ = self.connect_to_board(questions=self.cfg.ready_check)

        try:
            assert not ib.is_mezzanine_present(1), "Mezzanine are NOT supposed to be present for this test, found Mezzanine on Slot 1."
            assert not ib.is_mezzanine_present(2), "Mezzanine are NOT supposed to be present for this test, found Mezzanine on Slot 2."

            print("\nReading Iceboard Power, Current, Voltage & Temperature Sensors")
            power, current, voltage, temp = {}, {}, {}, {}

            totalPower = ib.get_motherboard_power()
            
            
            """
            Was previously:

            power['VCC3V3']     = ib.get_motherboard_power('MOTHERBOARD_RAIL_VCC3V3')
            current['VCC3V3']       = ib.get_motherboard_current('MOTHERBOARD_RAIL_VCC3V3')
            voltage['VCC3V3']       = ib.get_motherboard_voltage('MOTHERBOARD_RAIL_VCC3V3')

            For each element in the sensor list. Info moved to test_config.yaml 

            """
 
            table = PrettyTable(['Sensor', 'Power [W]', 'Voltage [V]', 'Current [A]']) #table object for nice printing
            temp_table = PrettyTable(['Sensor', 'Temp [C]'])

            for sensor  in cfg.sensor_names:
                name = sensor['name']

                power[name]   = ib.get_motherboard_power(sensor['board_name'])
                current[name] = ib.get_motherboard_current(sensor['board_name'])
                voltage[name] = ib.get_motherboard_voltage(sensor['board_name'])

                table.add_row([name, power[name], voltage[name], current[name]])


            temp['POWER']   = ib.get_motherboard_temperature('MOTHERBOARD_TEMPERATURE_POWER')   # between the two 1.0V bucks
            temp['FPGA']    = ib.get_motherboard_temperature('MOTHERBOARD_TEMPERATURE_FPGA')    # near USER SMA
            temp['ARM']     = ib.get_motherboard_temperature('MOTHERBOARD_TEMPERATURE_ARM')     # under the CPU shield
            temp['PHY']     = ib.get_motherboard_temperature('MOTHERBOARD_TEMPERATURE_PHY')     # also under the CPU shield
            
            
            for x in temp:
                temp_table.add_row([x, temp[x]])

            table.set_style(ORGMODE)
            temp_table.set_style(ORGMODE)
            print(table)
            print(temp_table)

            test_results.power, test_results.current, test_results.voltage, test_results.temp = power, current, voltage, temp

            moffvoltage = {}
            moffvoltage['VCC3V3']  = ib.get_mezzanine_voltage('MEZZANINE_RAIL_VCC3V3', 1)  ,   ib.get_mezzanine_voltage('MEZZANINE_RAIL_VCC3V3', 2)
            moffvoltage['VCC12V0'] = ib.get_mezzanine_voltage('MEZZANINE_RAIL_VCC12V0', 1) ,   ib.get_mezzanine_voltage('MEZZANINE_RAIL_VCC12V0', 2)
            moffvoltage['VADJ']    = ib.get_mezzanine_voltage('MEZZANINE_RAIL_VADJ', 1)    ,   ib.get_mezzanine_voltage('MEZZANINE_RAIL_VADJ', 2)

            print("\nMezzanine Voltage:(Slot 1, Slot 2) when turned off")
            for x in moffvoltage:
                print(x, ':   \t', moffvoltage[x])

            print("\nTurning on power to FMC slots")
            ib.set_mezzanine_power(True,1)
            ib.set_mezzanine_power(True,2)
            time.sleep(0.1)
            p1, p2 = ib.get_mezzanine_power(1), ib.get_mezzanine_power(2)
            assert (p1 and p2), "Mezzanines did not turn on properly! Slot 1: %s, Slot 2: %s" % (p1, p2)

            print("Reading FMC Slot Current & Voltage Sensors")
            mcurrent, mvoltage = {}, {}

            mcurrent['VCC3V3']  = ib.get_mezzanine_current('MEZZANINE_RAIL_VCC3V3', 1)  ,   ib.get_mezzanine_current('MEZZANINE_RAIL_VCC3V3', 2)
            mcurrent['VCC12V0'] = ib.get_mezzanine_current('MEZZANINE_RAIL_VCC12V0', 1) ,   ib.get_mezzanine_current('MEZZANINE_RAIL_VCC12V0', 2)
            mcurrent['VADJ']    = ib.get_mezzanine_current('MEZZANINE_RAIL_VADJ', 1)    ,   ib.get_mezzanine_current('MEZZANINE_RAIL_VADJ', 2)

            mvoltage['VCC3V3']  = ib.get_mezzanine_voltage('MEZZANINE_RAIL_VCC3V3', 1)  ,   ib.get_mezzanine_voltage('MEZZANINE_RAIL_VCC3V3', 2)
            mvoltage['VCC12V0'] = ib.get_mezzanine_voltage('MEZZANINE_RAIL_VCC12V0', 1) ,   ib.get_mezzanine_voltage('MEZZANINE_RAIL_VCC12V0', 2)
            mvoltage['VADJ']    = ib.get_mezzanine_voltage('MEZZANINE_RAIL_VADJ', 1)    ,   ib.get_mezzanine_voltage('MEZZANINE_RAIL_VADJ', 2)

            test_results.mcurrent, test_results.mvoltage = mcurrent, mvoltage

            print("Current:\t(Slot 1, Slot 2)")
            for x in mcurrent:
                print(x, ':   \t', mcurrent[x])
            print("\nVoltage:\t(Slot 1, Slot 2)")
            for x in mvoltage:
                print(x, ':   \t', mvoltage[x])

            print("Turning off power to FMC slots\n")
            ib.set_mezzanine_power(False,1)
            ib.set_mezzanine_power(False,2)
            time.sleep(0.1)
            p1, p2 = ib.get_mezzanine_power(1), ib.get_mezzanine_power(2)
            assert not (p1 or p2), "Mezzanines did not turn off properly! Slot 1: %s, Slot 2: %s" % (p1, p2)

            #assert max( [x for v in moffvoltage.values() for x in v]) < 0.2, "Mezzanines did not turn off properly!  When 'off' voltage measured is greater than 0.2V"

            values_ok = []
            for x in current:
                values_ok.append(in_range(current[x], cfg.current[x]['nom'],cfg.current[x]['pmargin'], cfg.current[x]['amargin'] ))
                if (values_ok[-1]==False):
                    print("The current on %s is out of range - measured current is: %.3f" %(x, current[x]))

            for x in voltage:
                values_ok.append(in_range(voltage[x], cfg.voltage[x]['nom'],cfg.voltage[x]['pmargin'], cfg.voltage[x]['amargin']))
                if (values_ok[-1]==False):
                    print("The voltage on %s is out of range - measured voltage is: %.3f" %(x, voltage[x]))

            for x in temp:
                values_ok.append(in_range(temp[x], cfg.temp[x]['nom'],cfg.temp[x]['pmargin'], cfg.temp[x]['amargin']))
                if (values_ok[-1]==False):
                    print("The temperature on %s is out of range - measured temp is: %.3f" %(x, temp[x]))

            for x in mcurrent:
                for i in range(2):
                    values_ok.append(in_range(mcurrent[x][i], cfg.fmccurrent[x]['nom'],cfg.fmccurrent[x]['pmargin'], cfg.fmccurrent[x]['amargin']))
                    if (values_ok[-1]==False):
                        print("The mezzanine current on %s is out of range - measured current is: %.3f" %(x, mcurrent[x][i]))

            for x in mvoltage:
                for i in range(2):
                    values_ok.append(in_range(mvoltage[x][i], cfg.fmcvoltage[x]['nom'],cfg.fmcvoltage[x]['pmargin'], cfg.fmcvoltage[x]['amargin']))
                    if (values_ok[-1]==False):
                        print("The mezzanine voltage on %s is out of range - measured voltage is: %.3f" %(x, mvoltage[x][i]))

            for x in moffvoltage:
                for i in range(2):
                    values_ok.append(in_range(moffvoltage[x][i], cfg.fmcoffvoltage[x]['nom'],cfg.fmcoffvoltage[x]['pmargin'], cfg.fmcoffvoltage[x]['amargin'] ))
                    if (values_ok[-1]==False):
                        print("The turn off mezzanine voltage on %s is out of range - measured voltage is: %.3f" %(x, moffvoltage[x][i]))

            assert min(values_ok), "Sensor Values out of bound!"
            passed = True

        finally:
            #self.instr.ps.set_output(state=False) # Turn power off
            self.params.test_locals = locals()
            test_results.passed = passed
            xr.save_data(test_results)

        #Estimate 30 seconds

    def test_ser(self, xr):
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

        ib, _ = self.connect_to_board(questions=True, without_serial=True) # include boards without serial number in search

        question = "Is this a Rev %d board? [Y/N]" %cfg.rev
        if(self.cfg.ready_check):
            if (xr.input_yes_no(question, additional_answers=[])==True):
                rev = "%d" %cfg.rev
            else:
                rev = xr.input("What is the revision of this board? e.g 4 ?")
        else:
            print("Taking board rev from config file since ready_check is 0")
            rev = "%d" %cfg.rev
        serial_number = self.params.serial
        #serial_number = '0398'
        ipmi = ipmi_fru.FRU(
                    board=ipmi_fru.Board(
                        mfg_date=datetime.datetime.now(),
                        manufacturer="Winterland",
                        product_name="IceBoard",
                        part_number="MGK7MB",
                        serial_number=serial_number,
                        fru_file=""),
                    product=ipmi_fru.Product(
                        manufacturer="Winterland",
                        product_name="IceBoard",
                        part_number="MGK7MB",
                        product_version=rev,
                        serial_number=serial_number,
                        asset_tag="",
                        fru_file=""),
                    # multi=Multi(...), when it's supported by this code
                    )

        print("Programming the EEPROM with the board info (model, serial number etc.)")
        b64_string = base64.b64encode(ipmi.encode())
        ib._motherboard_spi_flash_write_base64(b64_string)
        ib._motherboard_eeprom_write_base64(b64_string)

        #SHOULD DO A MOTHERBOARD EEPROM READ HERE - ICECORE NEEDS UPDATE
        print("Power cycling the board")
        ib, _ = self.connect_to_board(power_down=True, questions=self.cfg.ready_check)

        #If it fails to connect we don't get this far and test ends
        print("The boards IPMI data is as follows:")
        print(ib._get_motherboard_ipmi())
        if ib.get_motherboard_serial() == self.params.serial:
            print("\nThe board has the correct serial number programmed")
            passed = True
        else:
            print("\nThe motherboard reports the following serial: %s which isn't correct" %ib.get_motherboard_serial())
            passed = False
            assert False, "Serial programmed incorrectly"
        #self.instr.ps.set_output(state=False) # Turn power off

    #def test_i2c(self, xr):

    # def _test_i2c(self, xr):
    #     """
    #     QC008: Check if all I2C devices are present

    #     Procedure:

    #       - Start the i2c test on the computer
    #       - Connect to the iceboard with specified serial number.
    #       - Check if expected devices are present

    #     Not done:
    #       - IOexpanders - GPIO/ LEDs
    #       - Measure temperatures

    #     """
    #     xr.header('Checking I2C devices')
    #     if(self.cfg.ready_check):
    #         check_bp = xr.input_yes_no("Should this test check for backplane I2C devices? [Y/N]", additional_answers=[])
    #     else:
    #         check_bp = False 

    #     cfg = self.cfg.motherboard_tests.i2c_test
    #     passed = False

    #     (ib, ibs) = self.connect_to_board(questions=self.cfg.ready_check)

    #     print("Requesting all the I2C devices found at /sys/bus/i2c/devices on the board")
    #     cmd = "ls /sys/bus/i2c/devices/"
    #     results = self.run_arm_system_command(ib.hostname, cmd)

        
        
    #     motherboard_names = {}
    #     for name in cfg.motherboard_names:
    #         motherboard_names.update(cfg.motherboard_names[name])

    #     if check_bp:
    #         motherboard_names.update(cfg.backplane_names)

    #     print("\nParsing the list looking for specific devices")
        

    
    #     passed = True
    #     def check_names(names):
    #         nonlocal passed

    #         for name in names:
    #             addr = names[name]
    #             if addr + '\n' not in results:
    #                 print(f"   {name}:{addr}: Missing I2C device")
    #                 passed = False
    #             else:
    #                 print(f"   {name}:{addr}: OK")


    #     check_names(motherboard_names)
        

    #     assert passed, "Missing I2C devices"
    #     print("All devices are present")

    def test_i2c(self, xr):
        """
        QC008: Check if all I2C devices are present

        Procedure:

          - Start the i2c test on the computer
          - Connect to the iceboard with specified serial number.
          - Check if expected devices are present

        Not done:
          - IOexpanders - GPIO/ LEDs
          - Measure temperatures

        """

        
        xr.header('Checking I2C devices')
        (ib, _) = self.connect_to_board(questions=self.cfg.ready_check)
        print("Requesting all the I2C devices found at /sys/bus/i2c/devices on the board")
        cmd = "ls /sys/bus/i2c/devices/"

        i2c_devices = self.run_arm_system_command(ib.hostname, cmd)

        if(self.cfg.ready_check):
            check_bp = xr.input_yes_no("Should this test check for backplane I2C devices? [Y/N]", additional_answers=[])
        else:
            check_bp = False 

        cfg = self.cfg.motherboard_tests.i2c_test

        #load expected values from yaml file
        motherboard_names = {}
        passed = True

        for name in cfg.motherboard_names:
            motherboard_names.update(cfg.motherboard_names[name])

        if check_bp:
            motherboard_names.update(cfg.backplane_names)
        
        #compare to 
        for name in motherboard_names:
            addr = motherboard_names[name]
            if addr + '\n' not in i2c_devices:
                print(f"   {name}:{addr}: Missing I2C device")
                passed = False
            else:
                    print(f"   {name}:{addr}: OK")  

        assert passed, 'Missing I2C devices'
        print('All devices present')
        


           
    def test_clock(self, xr):
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

        print("Please power OFF the board, move the clock source jumper to the CRYSTAL position")
        (ib, ibs) = self.connect_to_board(questions=False)
        clock_source = ib.get_clock_source()
        #self.instr.ps.set_output(state=False) # Turn power off

        if (clock_source == ib.CLOCK_SOURCE.XTAL):
            print("Clock source is Crystal")
        else :
            print("Looks like the board booted but the board reported a different clock source to that specified: %s" %clock_source)
            assert False, "Wrong clock selection detected"


        print("\nPlease turn OFF the board, and move the clock source jumper to the SMA position. ")
        print("Then attach an SMA cable between the front panel clock input and backplane clock out SMA")

        (ib, ibs) = self.connect_to_board(questions = False)
        clock_source = ib.get_clock_source()
        #self.instr.ps.set_output(state=False) # Turn power off

        if (clock_source == ib.CLOCK_SOURCE.SMA):
            passed = True
            print("Board successfully booted and board reported that the SMA clock was used")
        else:
            print("Looks like the board booted but the board reported a different clock source to that specified: %s" %clock_source)
            passed = False
            assert False, "Wrong clock source detected."

        print("\nPlease turn OFF the board and move the clock source jumper to the BACKPLANE position. ")
        # print("When done, power ON the board and press ENTER")

        (ib, ibs) = self.connect_to_board(questions = False)
        clock_source = ib.get_clock_source()
        #self.instr.ps.set_output(state=False) # Turn power off

        if (clock_source == ib.CLOCK_SOURCE.BP):
            passed = True
            print("Board successfully booted and board reported that the backplane clock was in use")
        else:
            print("Looks like the board booted but the board reported a different clock source to that specified: %s" %clock_source)
            passed = False
            assert False, "Wrong clock source detected."

        passed = True
        print("Test passed")

    def test_fpga(self, xr):
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
        cfg2 = self.cfg.motherboard_tests.sensors
        test_results = NameSpace()
        passed = False

        (ib, ibs) = self.connect_to_board(questions=self.cfg.ready_check)

        print('\n-------------------------------')
        print("Please again ensure that the FPGA heatsink is in place, and with sufficient ventilation")
        print("Please ensure that SFP unit is inserted into board, with an ethernet cable attached.")
        print("Connect an SMA cable from the backplace Time input (grouped with the 4 other SMA) to the SMA A connector on the iceboard.")

        print('\n-------------------------------')
        

        

        
        print('Before programming the FPGA die temperature is: %.3f C' % ib.get_motherboard_temperature(ib.TEMPERATURE_SENSOR.MB_FPGA_DIE))
        pre_prog_power = ib.get_motherboard_power(ib.RAIL.MB_VCC3V3) + ib.get_motherboard_power(ib.RAIL.MB_VCC12V0) + ib.get_motherboard_power(ib.RAIL.MB_VCC5V5)
        print('Before programming the boards power consumption is: %.3f W' % pre_prog_power)

        self.prog_fpga(ib)

        if(ib.is_voltage_nominal() == True):
            print("\nThe board reports that all the buck regulators have their voltages within 5% tolerance")
        else:
            voltage={}
            voltage['VCC3V3']       = ib.get_motherboard_voltage('MOTHERBOARD_RAIL_VCC3V3')
            voltage['VCC12V0']      = ib.get_motherboard_voltage('MOTHERBOARD_RAIL_VCC12V0')
            voltage['VCC5V5']       = ib.get_motherboard_voltage('MOTHERBOARD_RAIL_VCC5V5')
            voltage['VCC1V0_GTX']   = ib.get_motherboard_voltage('MOTHERBOARD_RAIL_VCC1V0_GTX')
            voltage['VCC1V0']       = ib.get_motherboard_voltage('MOTHERBOARD_RAIL_VCC1V0')
            voltage['VCC1V2']       = ib.get_motherboard_voltage('MOTHERBOARD_RAIL_VCC1V2')
            voltage['VCC1V5']       = ib.get_motherboard_voltage('MOTHERBOARD_RAIL_VCC1V5')
            voltage['VCC1V8']       = ib.get_motherboard_voltage('MOTHERBOARD_RAIL_VCC1V8')
            voltage['VADJ']         = ib.get_motherboard_voltage('MOTHERBOARD_RAIL_VADJ')
            values_ok=[]
            for x in voltage:
                values_ok.append(in_range(voltage[x], cfg2.voltage[x]['nom'],cfg2.voltage[x]['pmargin'], cfg2.voltage[x]['amargin']))
                if (values_ok[-1]==False):
                    print("The voltage on %s is out of range - measured voltage is: %.3f" %(x, voltage[x]))

            passed = False
            print("Check that all the hand soldered buck caps are in place")
            assert False, "One or more of the buck rails reports that its voltage is out of the permitted 5% tollerance margin"

        #possible name change 
        cookie = hex(ib.fpga_mmi_read(ib.FPGA_CORE_FIRMWARE_COOKIE_ADDR))
        print("\nThe FPGA memory map cookie (address 0) is: %s" % cookie)

        if (cookie == '0xbeefface'):
            print("Correct cookie detected - memory map read back looks good")
        else:
            print("Invalid cookie detected - letting test continue for now - although FPGA memory map read back may be problematic")
            while (xr.input_yes_no("Is the jumper J1 shorted? [Y/N]", additional_answers=[]) != True):
                pass;
            passed = False

        after_prog_power =  ib.get_motherboard_power(ib.RAIL.MB_VCC3V3) + ib.get_motherboard_power(ib.RAIL.MB_VCC12V0) + ib.get_motherboard_power(ib.RAIL.MB_VCC5V5)
        print('\nAfter programming the FPGA die temperature is: %.3f C' % ib.get_motherboard_temperature(ib.TEMPERATURE_SENSOR.MB_FPGA_DIE))
        print('After programming the boards power consumption is: %.3f W' % after_prog_power)

        try:
            self.open_fpga(ib)
            #ib._is_core_open
            print("\nDirect FPGA communications established through SFP unit.")
        except:
            print("\nCannot communicate directly with the FPGA through the SFP unit.")
            print("Remove the SFP unit, clean the contacts and re-run the test.")
            print("If that does not work its likely that the FPGA bit file is the incorrect version - use latest on jfcdev")
            assert False, "\nError establishing UDP communications with the FPGA"
        

        #print("\nThe firmware operating is version: %s" %ib.get_fpga_firmware_timestamp_sync())

        #I think this function doesn't exist anymore, ask about new version
        #print("\nThe firmware operating is version: %s" %async_to_sync(ib.get_fpga_firmware_timestamp()))

        print("\nAttempting to read the motherboard eeprom directly from the FPGA - this tests connectivity to I2C matrix")

        #I think the message length might have change since this threw an error
        partnum = ib.read_motherboard_eeprom(40,6)
        print(f"The part number detected in the eeprom is: {partnum}")

        assert partnum == b'MGK7MB', "Error reading the motherboard EEPROM through the FPGA I2C interface"

        print('Testing IRIG-B')
        ib.set_user_output_source('irigb_gen','sma_a')
        print('\nTime Readout:')
        #ib.set_irigb_source('bp_time')
        run_async(ib.set_irigb_source_async('bp_time'))

        print(run_async(ib.get_irigb_time_async()))
        #self.instr.ps.set_output(state=False) # Turn power off

        passed = True

    def test_mezz(self, xr):
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

        print('\n-------------------------------')
        print("Please ensure that the Mezzanines ARE mounted on the board for this test.")
        (ib, ibs) = self.connect_to_board(questions=True, program=True, open_fpga=True)

        assert ib.is_mezzanine_present(1), "Did not find Mezzanine on Slot 1."
        assert ib.is_mezzanine_present(2), "Did not find Mezzanine on Slot 2."

        #self.prog_fpga(ib)
        #ib.open_sync()

        #if not (ib.is_core_open()):
        #    assert False , "Cannot communicate directly with the FPGA through the SFP unit."
        #else:
        #    print("Communications established with FPGA through SFP unit")

        for mezz in (1,2):
            eeprom = base64.b64decode(ib._mezzanine_eeprom_read_base64(mezz))
            if re.search(b'MGADC08',eeprom):
                print(f"Read Mezzanine {mezz} EEPROM and detected MGADC08")
            else:
                print(f"Read Mezzanine {mezz} eeprom and did not find MGADC08 - is mezzanien eeprom programed correctly?")
            ib.set_mezzanine_power(True , mezz)

        time.sleep(0.1)
        p1, p2 = ib.get_mezzanine_power(1), ib.get_mezzanine_power(2)
        assert (p1 and p2), "Mezzanines did not turn on properly! Slot 1: %s, Slot 2: %s" % (p1, p2)

        print("Reading FMC Slot Current & Voltage Sensors")
        mcurrent, mvoltage = {}, {}

        mcurrent['VCC3V3']  = ib.get_mezzanine_current('MEZZANINE_RAIL_VCC3V3', 1)  ,   ib.get_mezzanine_current('MEZZANINE_RAIL_VCC3V3', 2)
        mcurrent['VCC12V0'] = ib.get_mezzanine_current('MEZZANINE_RAIL_VCC12V0', 1) ,   ib.get_mezzanine_current('MEZZANINE_RAIL_VCC12V0', 2)
        mcurrent['VADJ']    = ib.get_mezzanine_current('MEZZANINE_RAIL_VADJ', 1)    ,   ib.get_mezzanine_current('MEZZANINE_RAIL_VADJ', 2)

        mvoltage['VCC3V3']  = ib.get_mezzanine_voltage('MEZZANINE_RAIL_VCC3V3', 1)  ,   ib.get_mezzanine_voltage('MEZZANINE_RAIL_VCC3V3', 2)
        mvoltage['VCC12V0'] = ib.get_mezzanine_voltage('MEZZANINE_RAIL_VCC12V0', 1) ,   ib.get_mezzanine_voltage('MEZZANINE_RAIL_VCC12V0', 2)
        mvoltage['VADJ']    = ib.get_mezzanine_voltage('MEZZANINE_RAIL_VADJ', 1)    ,   ib.get_mezzanine_voltage('MEZZANINE_RAIL_VADJ', 2)

        test_results.mcurrent, test_results.mvoltage = mcurrent, mvoltage

        print("Current:\t(Slot 1, Slot 2)")
        for x in mcurrent:
            print(x, ':   \t', mcurrent[x])
        print("\nVoltage:\t(Slot 1, Slot 2)")
        for x in mvoltage:
            print(x, ':   \t', mvoltage[x])

        values_ok = []
        for x in mcurrent:
                for i in range(2):
                    values_ok.append(in_range(mcurrent[x][i], cfg.fmccurrent[x]['nom'],cfg.fmccurrent[x]['pmargin'], cfg.fmccurrent[x]['amargin']))
                    if (values_ok[-1]==False):
                        print("The mezzanine current on %s is out of range - measured current is: %.3f" %(x, mcurrent[x][i]))

        for x in mvoltage:
            for i in range(2):
                values_ok.append(in_range(mvoltage[x][i], cfg.fmcvoltage[x]['nom'],cfg.fmcvoltage[x]['pmargin'], cfg.fmcvoltage[x]['amargin']))
                if (values_ok[-1]==False):
                    print("The mezzanine voltage on %s is out of range - measured voltage is: %.3f" %(x, mvoltage[x][i]))

        assert min(values_ok), "Sensor Values out of bound!"


        #Checking if the mezzanine PG_M2C line is high - its one of the GPIOs that the ARM has access to
        #A bit over kill but I prefer getting the info via the arm and no tubber command exists for it
        #Could have gone via the FPGA instead
        """
        def check_power_good(cmd, mezz_number):
            print(ib.hostname, cmd)
            result = self.run_arm_system_command(ib.hostname, cmd)

            print(result)
            if re.search("1",result[0]):
                print(f"Mezzanine {mezz_number} - PG_M2C line is detected high - mezzanine reports power good")
            else:
                print(f"Mezzanine {mezz_number} - PG_M2C line is not high - mezzanine did not report good power")
                assert False, f"Mezzanine {mezz_number} - PG_M2C has problems"

        check_power_good("cat /sys/class/gpio/FMCA_PG_M2C/value", 1)
        check_power_good("cat /sys/class/gpio/FMCB_PG_M2C/value", 2)"""

        FMCA_power = ib._gpio.read('FMCA_PG_M2C')
        FMCB_power = ib._gpio.read('FMCB_PG_M2C')

        if FMCA_power == 1:
              print(f"Mezzanine A - PG_M2C line is detected high - mezzanine reports power good")
        else:
            print(f"Mezzanine A - PG_M2C line is not high - mezzanine did not report good power")
            assert False, f"Mezzanine A- PG_M2C has problems"

        if FMCB_power == 1:
              print(f"Mezzanine B - PG_M2C line is detected high - mezzanine reports power good")
        else:
            print(f"Mezzanine B - PG_M2C line is not high - mezzanine did not report good power")
            assert False, f"Mezzanine A- PG_M2C has problems"





    def test_ramp(self, xr):
        """
        QC0011: Check if high speed data lines of mezzanine function correctly

        Procedure:

          - Perform Ramp test
          - Check if spectra correct
          - Monitor FPGA temperature and power usage

        """
        xr.header('Ramp test')

        eth_if = self.cfg.motherboard_tests.global_settings.eth_interface  # config options pertaining to setup

        stat_command = ['ifconfig', eth_if]
        x = subprocess.check_output(stat_command)
        m = re.search(b'mtu 9000', x)
        if (m == None ):
            print("Need to change the ethernet port MTU setting. Please enter password when asked.")
            os.system(f'sudo ifconfig {eth_if} mtu 9000')
        

        # Useful shortcuts
        cfg = self.cfg.motherboard_tests.rmp_test
        test_results = NameSpace()
        passed = False

        print('\n-------------------------------')
        print("Please ensure mezzanines are loaded, and sufficient cooling for FPGA")
        (ib, ibs) = self.connect_to_board(questions=self.cfg.ready_check, program=True, open_fpga=True)

        run_async(ib.fpga.init_async()) #temp fix

        try:
            test_results.data = []
            test_results.ramp_ok = []

            for mezz in list(ib.mezzanine.values()):
                print('initializing mezzanine...')
                mezz.init()

            print('Computing ADC delays...')

            
            ib.set_adc_delays(compute_delays=2, save_delays=False, check_sync_delays=True, check_adc_delays=20, verbose=0, retry=5)
            delay_table = ib.get_adc_delays()
            print(delay_table)
            print('\nOpening data receiver socket')

            print(ib.CAPTURE_TYPE)
            receiver = ib.get_data_receiver()

            print('Setting up ramp transmission...')
            ib.set_adcdaq_mode('data')
            ib.set_data_source('adc')
            ib.set_adc_mode('ramp')
            ib.start_data_capture(period=1, source='adc')

            print('Syncing...')
            ib.sync()

            print('Getting data frames...')
            receiver.read_raw_frames(flush=1)  # flush
            data = []
            framenum = 0
            maxcount=40
            i = 0;
            foundone=0
            while i < maxcount and foundone == 0:
                data.append(receiver.read_raw_frames()[1])
                if len(data[i]) == 16:
                    framenum = i
                    foundone = 1
                print(len(data[i]))
                i=i+1

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
            
            #receiver.close()
            #ib.stop_data_capture()

            test_results.data.append(data)
            plt.figure(1)

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
            xr.save_data(test_results)

    def test_qsfp(self, xr):

        """
        QC0012: Check that the QSFP cables are detected over I2C and that their eeprom can be read

        Procedure:

          - Perform qsfp test
          - Check if device is detected
          - Check eeprom can be read

        """
        cfg = self.cfg.motherboard_tests.qsfp_test
        test_results = NameSpace()
        passed = False

        print('\n-------------------------------')
        print("Please ensure QSFP cable is plugged into the motherboard in both ports, and sufficient cooling for FPGA")
        (ib, _) = self.connect_to_board(questions=self.cfg.ready_check, program=True, open_fpga=True)

        # self.prog_fpga(ib)
        # ib.open_sync()

        xr.header('Test-Results')

        qsfp_ports = ib.qsfp
        assert len(qsfp_ports) == 2, f"Not all connectors were detected! Detected qsfp port: {qsfp_ports}"

        for port, qsfp in enumerate(qsfp_ports):
            
            try:
                if qsfp.is_present():
                
                    print(f"Motherboard QSFP module present on port {port}")
                    print(f"Resetting Module {port}")


                (manufacturer, serial) = self.get_qfsp_info(qsfp)
                print(f"QSFP cable {port} manufactured by {manufacturer}. Serial number: {serial}")
                assert manufacturer in cfg.manufacturer, f"Cannot find string '{manufacturer}' in expected values. I2C read error?"

            finally:
                self.params.test_locals = locals()



        
        
    
        

    def test_gtx(self, xr):
        """
        QC0013: Check if high speed links work

        Procedure:

          - With one slot backplane  
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

        print('\n-------------------------------')
        print("Please ensure the QSFP cable is plugged into the motherboard in both ports, and sufficient cooling for FPGA")
        print("The motherboard must be plugged into the one slot backplane")
        (ib, ibs) = self.connect_to_board(program=True, questions=self.cfg.ready_check, open_fpga=True)

        #print("Calling ib.open()")
        #ib.open_sync()
        #run_async(ib.fpga.init_async())
        ib.i2c.select_bus('BP')
        
        #ibs.ic[0]._gpio_ctrl.init(cfg0_def=0xFF, cfg1_def=0xFF) 

        xr.header('Test-Results')

        print("\nMeasuring  gtx error rate over a 20 second period ")
        meas_ber1 = run_async(ibs.get_ber(tx_power = cfg.tx_power, print_ = 0, period = 20))
        #print meas_ber1

        bp_rate = True
        qsfp_rate = True
        gpu_rate = True
        bad_lanes = []

        print((cfg.bp_limit))
        print((type(cfg.bp_limit)))

        for key in list(meas_ber1.keys()):
            if key[0] == 'pcb':
                if meas_ber1[key] >= cfg.bp_limit:
                    print("Exceeded limits")
                    bp_rate = False
                    bad_lanes.append((key, meas_ber1[key]))
            elif key[0] == 'qsfp':
                if meas_ber1[key] >= cfg.qsfp_limit:
                    qsfp_rate = False
                    bad_lanes.append((key, meas_ber1[key]))
            elif key[0] == 'GPU':
                if meas_ber1[key] >= cfg.gpu_limit:
                    gpu_rate = False
                    bad_lanes.append((key, meas_ber1[key]))
                #del meas_ber1[key]

        keys = list(meas_ber1.keys())
        keys.sort()
        for key in keys:
            print(key, meas_ber1[key])

        if bad_lanes:
            print("\nBad lanes:")
            bad_lanes.sort()
            for lane in bad_lanes:
                print(lane)

        assert bp_rate, 'Bit Error Rate for Backplane lanes too high!'
        assert qsfp_rate, 'Bit Error Rate for QSFP lanes too high!'
        assert gpu_rate, 'Bit Error Rate for GPU lanes too high!'

        print("\nMeasuring  gtx error rate over a 600 second period")
        meas_ber2 = run_async(ibs.get_ber(tx_power = cfg.tx_power, print_ = 0, period = 600))

        bp_rate = True
        qsfp_rate = True
        gpu_rate = True
        bad_lanes = []

        for key in list(meas_ber2.keys()):
            if key[0] == 'pcb':
                if meas_ber2[key] >= cfg.bp_limit:
                    bp_rate = False
                    bad_lanes.append((key, meas_ber2[key]))
            elif key[0] == 'qsfp':
                if meas_ber2[key] >= cfg.qsfp_limit:
                    qsfp_rate = False
                    bad_lanes.append((key, meas_ber2[key]))
            elif key[0] == 'GPU':
                if meas_ber2[key] >= cfg.gpu_limit:
                    gpu_rate = False
                    bad_lanes.append((key, meas_ber2[key]))
                #del meas_ber2[key]

        keys = list(meas_ber2.keys())
        keys.sort()
        for key in keys:
            print(key, meas_ber2[key])

        if bad_lanes:
            print("\nBad lanes:")
            bad_lanes.sort()
            for lane in bad_lanes:
                print(lane)

        assert bp_rate, 'Bit Error Rate for Backplane lanes too high!'
        assert qsfp_rate, 'Bit Error Rate for QSFP lanes too high!'
        assert gpu_rate, 'Bit Error Rate for GPU lanes too high!'

        #finally:
            #xr.save_data(status)
        self.params.test_locals = locals()

if __name__ == '__main__':
    # util.add_paths('..')  # needed to pychfpga
    """ Run the test in this file."""
    #v = TestMenu(TEST_CONFIG_FILE, use_git=True, git_repo_path="~/iceboard/iceqc/iceqc").run()
    v = TestMenu(TEST_CONFIG_FILE).run()
    locals().update(v) # bring local variables from the test runner into the current namespace for easier debugging
