#!/usr/bin/env python

"""Performs tests on the full crate setup."""
import time
import numpy as np
import matplotlib.pyplot as plt
import base64
# from util import NameSpace

import pytest

import labpy
from wtl.namespace import NameSpace
from wtl.pytest_xreport import xr, run_test_menu

# from icecore.tests.xreport import test_report
from pychfpga.fpga_array import FPGAArray
from pychfpga.hardware.interfaces import ipmi_fru

TEST_CONFIG_FILE = './MGK7BP16/test_config.yaml'

# def input(message):
#     key = xr.input(message).lower()
#     assert not key.startswith('q'), 'Test was interrupted by user'
#     return key

# def input_yes_no(message, additional_answers=[]):
#     while True:
#         key = input(message)
#         if key.startswith('y'):
#             return True
#         elif key.startswith('n'):
#             return False
#         elif key in additional_answers:
#             return key
#         print 'Wrong answer. Try again'

class TestMGK7BP16Crate:
    def open_instrument(self, name):
        instr_params = self.cfg.instruments[name].copy()
        class_name = instr_params.pop('labpy_object')
        return labpy.open_instrument(class_name, **instr_params)

    def open_dmm(self):
        self.dmm = self.open_instrument('dmm')
        self.dmm.set_beeper(True)
        self.dmm.display('Ready for','MGK7BP16 tests')
        return self.dmm

    def open_scope(self):
        self.o = self.open_instrument('scope')
        return self.o

    def open_wfg(self):
        self.wfg = self.open_instrument('generator')
        return self.wfg

    @pytest.fixture(autouse=True)
    def setup(self, xr):
        """ Prepare the test for execution.

        This is called before every tests in this class.
        """
        # xr.header('Setting-up')
        assert xr.model and xr.serial, "model or serial number has not been specified"
        self.cfg = xr.config
        # cfg = self.cfg.crate_tests.setup  # config options pertaining to setup

        # xr.input('Turn on the dmm, scope, and function generator, if not already on. Press ENTER to continue (Q:Exit)')

        # self.open_dmm()
        # self.open_scope()
        # self.open_wfg()

        # xr.header('Test results')


    def test_insp(self, xr):
        """
        QC001: Inspection test: visual check of the backplane

        Procedure:

          - Start the inspection test on the computer
          - Type in serial number of tested board.
          - Visually inspect specific points mentioned in test

        """
        xr.header('Inspection test')

        questions = [
            "Does the soldering look okay overall",
            "Do all pins on the the impact connector look present and straight",
            # "Buck sense capacitor added  (100nF)",
            "Molex screws are plastic and NOT metal",
            # "Arm reset cap modified (680pF added between SW4 and SW2)",
            # "heatsink added to buck",  # Not needed
            # "Hand soldered wire added", # old rev?
            ]

        answers = []

        for i, q in enumerate(questions):
            answers.append(xr.input_yes_no(f'{i+1}) {q}', additional_answers=[]))


        pf_table = [["Question", "Status"]] + [[f'{i+1}) {q}', (':red:`FAILED`', ':green:`PASSED`')[answers[i]]] for i,q in enumerate(questions)]
        xr.add_table(pf_table, header=True)


        failed_lines = [str(i+1) for i, ans in enumerate(answers) if not ans]
        if failed_lines:
            print(f"Some tests failed, please address inspection lines {', '.join(failed_lines)}")

        assert not failed_lines, 'Inspection Test failed'

        comments = xr.input("If there are any additional comments you wish to make (e.g. scratches, manufacturing problems), please describe below. (If none, enter 'None'). (Q:Exit) ")


    #def test_power(self, xr):

        """power
        003,
        Test    Result  Pass/Fail
        Input resistance to backplane power     No short - high impedance   Pass
        Load resistance on buck output  6 Ohms  Pass
        Power consumption   17V supply 0.36A giving 6.2W    Pass
        Power consumption   18V supply 0.43A giving 7.7W (6.3W on serial 8)     FAIL - SYNC FANOUT IC with IR Camera at >50 deg C, Slot 4 Sync failiure, Pin 20 of fanout IC 3 Ohms to ground

        Buck output voltage     3.3V    Pass

        011

        5V linear regulator voltage     5V  Pass
        3.3V linear regulator voltage   3.3V    Pass

        """

    def test_impedance(self, xr):
        """
        Impedance test of various components
        """

        xr.input('Turn on the dmm. Press ENTER to continue. (Q:Exit)')

        cfg = self.cfg.crate_tests.impedance
        dmm = self.open_dmm()

        #print(f'cfg = {cfg}')
        test_results = NameSpace()
        passed = False
        failed_test_points = []
        try:
            xr.input('If the backplane is connected to power, turn off the supply and disconnect it from the board. Press ENTER to continue. (Q:Exit)')
            test_results.test_points = NameSpace()
            for tp_name, limits in cfg.test_points:
                limits = NameSpace(limits)
                dmm.display('','Measure %s' % tp_name)
                dmm.select_resistance_measurement()
                while True:
                    dmm.local()
                    xr.input(f"Make sure the dmm cables are at the voltage inputs. Apply the probes to test point '{tp_name}' and press ENTER to measure (Q:Exit)")
                    if limits.delay:
                        time.sleep(limits.delay)
                    result = dmm.get_resistance()
                    if result <= cfg.max_impedance: break
                    print('Impedance is too high. Is the probe really connected?')
                dmm.beep()
                passed = limits.zmin < result < limits.zmax
                test_results.test_points[tp_name] = NameSpace(Z=result, passed=passed)
                if not passed:
                    failed_test_points.append(tp_name)
                    dmm.beep()
                    time.sleep(0.1)
                    dmm.beep()
                print('   %s : %.0f ohms (must be %.0f < impedance < %.0f) ==> %s' % (tp_name, result, limits.zmin, limits.zmax, xr.pass_fail(passed)))
            assert not len(failed_test_points), 'Low impedance on %s' % ','.join(failed_test_points)
            passed = True

        finally:
            test_results.passed = passed
            dmm.display(xr.pass_fail(passed),'Impedance tests')
            # dmm.local()
            xr.save_data(test_results)


    def test_power(self, xr):

        cfg = self.cfg.crate_tests.power

        xr.input('Turn on the dmm. Press ENTER to continue. (Q:Exit)')

        dmm = self.open_dmm()

        #print(f'cfg = {cfg}')
        test_results = NameSpace()
        passed = False

        try:

            xr.input('Turn on the power supply and set it to 17V. Connect it to any of the white 6 pin plastic Molex connectors on the board. Has a green LED lit up? Is the input current around ~0.25-0.35A? If so, press ENTER to continue. If not, reconnect power elsewhere and try again. (Q:Exit)')
            current = xr.input('Read the input current from the power supply and write it here in amps. (Q:Exit)')
            # current = dmm.get_dc_current()
            current = float(current)
            print(f'Current: {current}A')
            power = cfg.voltage * current
            passed_power = cfg.min_power < power < cfg.max_power
            msg = f'Power is {power}W ==> {xr.pass_fail(passed_power)}. Power must be between {cfg.min_power} - {cfg.max_power}W'
            print(msg)
            assert passed_power, msg

            xr.input('Ensure that the dmm cables are at the voltage input/output. Connect the probes of the dmm to 3V3 and GND. Press ENTER to measure the voltage. (Q:Exit)')
            voltage_3V3 = dmm.get_dc_voltage()
            dmm.beep()
            print(f'3V3 Voltage: {voltage_3V3}V')
            passed_3V3 = cfg.min_3V3 < voltage_3V3 < cfg.max_3V3
            msg = f'3V3 Voltage is {voltage_3V3}V ==> {xr.pass_fail(passed_3V3)}. It must be +/- 10%% of 3.3V ({cfg.min_3V3} - {cfg.max_3V3}V)'
            print(msg)
            assert passed_3V3, msg

            for cap_name in cfg.C47:

                xr.input(f'Connect the probes of the dmm to the positive (right) side of C47_{cap_name} and GND. Press ENTER to measure the voltage. (Q:Exit)')
                voltage_2V5 = dmm.get_dc_voltage()
                dmm.beep()
                passed_2V5 = cfg.min_2V5 < voltage_2V5 < cfg.max_2V5
                msg = f'2V5 Voltage is {voltage_2V5}V ==> {xr.pass_fail(passed_2V5)}. It must be +/- 10%% of 2.5V ({cfg.min_2V5} - {cfg.max_2V5}V)'
                print(msg)
                assert passed_2V5, msg

            passed = True  # We get here only if no assert failed

        finally:
            test_results.passed = passed
            xr.save_data(test_results)
            xr.input('Disconnect the backplane from power and turn off the power supply. Turn off the dmm. Press ENTER to conclude the test. (Q:Exit)')


    def test_clk_time_trig(self, xr):
        """ test the Clock, Trig & Time signal path

        Covers: SMAs, fanout chips, fanout chip supplies, backplane connector

        Uses the function generator as a source for the SMA inputs, and scope
        to capture the waveform on the backplane connector using a custom-made
        dongle.

        Notes:
          Clock:
            # dongle needs to be bottom
            # LVPECL
            # must be greater than 0.5V
          Trig:
            # dongle needs to be top
            # LVDS
          Time:
            # dongle needs to be top
            # LVDS
          LVDS:
            # between 0.25V and 0.45V (peak-to-peak)
          LVPECL:
            # between 0.65V and 1.35V (peak-to-peak)

        """

        cfg = self.cfg.crate_tests.clk_time_trig

        # with xr.figcontext(f'Debug image references'):
        #     plt.plot([1,2,3],[2,5,3])
        #     plt.xlabel('Time (s)')
        #     plt.ylabel('Voltage (V)')

        xr.input('Turn on the scope and the function generator. Press ENTER to continue. (Q:Exit)')

        o = self.open_scope()
        wfg = self.open_wfg()

        o.set_channel(1)
        o.set_coupling('DC', 1)
        o.set_impedance(50, 1)
        o.set_voltage_trigger(0)
        o.set_probe(1, 1)

        wfg.set_function('SQU')
        wfg.set_amplitude(2.5, offset = 1.25)
        wfg.set_frequency(10e6)

        #print(f'cfg = {cfg}')
        xr.input('Turn on the power supply and set it to 17V. Connect it to any of the white 6 pin plastic Molex connectors on the board. Has a green LED lit up? Is the input current around ~0.25-0.35A? If so, press ENTER to continue. If not, reconnect power elsewhere and try again. (Q:Exit)')

        test_results = NameSpace()
        passed = False

        try:

            test_results.test_slots = NameSpace()
            for signal_name in cfg.test:
                failed_tests = []

                xr.input(f'Attach the {signal_name} dongle cable to channel 1 of the scope. Press ENTER to continue. (Q:Exit)')
                xr.input(f'Attach the function generator to {signal_name} with a BNC to SMA (male output) connector. Press ENTER to continue. (Q:Exit)')

                if signal_name == 'Clock':
                    dongle_location = 'bottom'
                    vmin = cfg.min_LVPECL
                    vmax = cfg.max_LVPECL
                    range_msg = f'Valid range is {vmin} - {vmax}V (LVPECL)'
                else:
                    dongle_location = 'top'
                    vmin = cfg.min_LVDS
                    vmax = cfg.max_LVDS
                    range_msg = f'Valid range is {vmin} - {vmax}V (LVDS)'

                for slot_name in cfg.test_inputs:
                    xr.input(f'Attach the dongle cable to the *{dongle_location}* dongle connector at {slot_name}. Press ENTER to record the output peak-to-peak amplitude. (Q=Exit)')
                    output = o.get_waveform()
                    p2p = max(output) - min(output) # peak-to-peak voltage
                    passed = vmin < p2p < vmax
                    test_results.test_slots[slot_name] = NameSpace(Test = signal_name, Slot = slot_name, Amplitude=p2p, passed=passed)
                    msg = f'Measured {signal_name} {slot_name} = {p2p} V ==> {xr.pass_fail(passed)}. {range_msg}'
                    print(msg)
                    if not passed:
                        failed_tests.append(slot_name)

                    with xr.figcontext(f'{signal_name} - {slot_name}'):
                        time = o.get_time()
                        plt.scatter(time, output, s = 5)
                        plt.xlabel('Time (s)')
                        plt.ylabel('Voltage (V)')

                xr.input(f'Remove the {signal_name} dongle cable from the backplane and the scope. Press ENTER to continue. (Q:Exit)')

                assert not failed_tests, f"{signal_name} signal failed on slot(s): {failed_tests}"
                passed = True
        finally:
            test_results.passed = passed
            xr.save_data(test_results)
            xr.input('Disconnect the function generator from the backplane SMA. \n'
                     'Turn off the backplane power supply and press ENTER. (Q:Exit)')


    def test_sync(self, xr):
        """ Test the SYNC signal fanout
        Measurements are made on the chip output with a Hi-Z probe.

        Sync voltage on the output of the chip should be >2V because there is no termination on the LVPECL driver.

        i.e. the pins of U2 (1-4) should have >2V
        """
        cfg = cfg = self.cfg.crate_tests.sync

        xr.input('Turn on the scope and the function generator. Press ENTER to continue. (Q:Exit)')

        o = self.open_scope()
        wfg = self.open_wfg()

        o.set_channel(1)
        o.set_coupling('DC', 1)
        o.set_impedance(1e6, 1)
        o.set_voltage_trigger(0)
        o.set_probe(channel = 1, gain = 10)
        wfg.set_function('SQU')
        wfg.set_amplitude(2.5, offset = 1.25)
        wfg.set_frequency(10e6)

        xr.input('Turn on the power supply and set it to 17V. Connect it to any of the white 6 pin plastic Molex connectors on the board. Has a green LED lit up? Is the input current around ~0.25-0.35A? If so, press ENTER to continue. If not, reconnect power elsewhere and try again. (Q:Exit)')
        xr.input('Connect a jumper at Short to En. - a red LED should light up. (Q:Exit)')
        xr.input('Connect the function generator to Sync with a BNC to SMA (male output) connector. Connect the high impedance probe to channel 1 of the scope and *set the probe to 10X on its side*. (Q:Exit)')

        test_results = NameSpace()
        passed = False
        failed_tests = []

        try:
            for slot in cfg.slots:
                for pin in cfg.pins:
                    while True:
                        xr.input(f'Touch the probe to {pin} of U2 of {slot}. Press ENTER to measure the peak-to-peak amplitude. (Q:Exit)')
                        output = o.get_waveform()
                        p2p = max(output) - min(output) # peak-to-peak
                        passed = p2p > cfg.min_V
                        msg = f'SYNC on slot {slot} @ U2.{pin} = {p2p} V ==> {xr.pass_fail(passed)}. Valid range is > {cfg.min_V} V'
                        print(msg)
                        if passed or not xr.input_yes_no('Want to try again?'):
                            break
                    if not passed:
                        failed_tests.append(f'Slot{slot}.U2.{pin}')
                    with xr.figcontext(caption = f'SYNC: {slot} - {pin}'):
                        time = o.get_time()
                        plt.scatter(time, output, s = 5)
                        plt.xlabel('Time (s)')
                        plt.ylabel('Voltage (V)')

            assert not failed_tests, f"SYNC signal failed on: {failed_tests}"
            passed = True
        finally:
            test_results.passed = passed
            xr.save_data(test_results)
            xr.input('Disconnect the backplane from power and turn off the power supply. \n'
                     'Disconnect the function generator from Sync. Disconnect the scope. Disconnect the jumper. (Q:Exit)')


    def test_clock_output(self, xr):
        """Test the backplane on-board 10 MHz clock generator output.
        """

        cfg = self.cfg.crate_tests.clock_output

        xr.input('Turn on the scope and the function generator. Press ENTER to continue. (Q:Exit)')

        o = self.open_scope()
        wfg = self.open_wfg()

        #print(f'cfg = {cfg}')

        test_results = NameSpace()
        passed = False

        o.set_channel(1)
        o.set_coupling('DC', 1)
        o.set_impedance(50, 1)
        o.set_voltage_trigger(0)
        o.set_probe(1, 1)

        wfg.set_function('SQU')
        wfg.set_amplitude(2.5, offset = 1.25)
        wfg.set_frequency(10e6)

        xr.input('Turn on the power supply and set it to 17V. Connect it to any of the white 6 pin plastic Molex connectors on the board. Has a green LED lit up? Is the input current around ~0.25-0.35A? If so, press ENTER to continue. If not, reconnect power elsewhere and try again. (Q:Exit)')
        xr.input('Connect a jumper at Short to En. - a red LED should light up. Connect the function generator to Clock with a BNC to SMA (male output) connector. Connect channel 1 of the scope to Xtal Out with a BNC to SMA (male output) connector. (Q:Exit)')

        try:
            xr.input('Press ENTER to measure the output Clock waveform. (Q:Exit)')
            output = o.get_waveform()

            with xr.figcontext(caption = 'Clock Output Test'):
                            time = o.get_time()
                            plt.scatter(time, output, s = 5)
                            plt.xlabel('Time (s)')
                            plt.ylabel('Voltage (V)')

            p2p = max(output) - min(output)
            passed = p2p > cfg.min_V
            msg = f'Clock output is {p2p} Vpp => {xr.pass_fail(passed)}. Valid range is  >{cfg.min_V}V'
            print(msg)
            assert passed, msg
        finally:
            test_results.passed = passed
            xr.save_data(test_results)
            xr.input('Disconnect the backplane from power and turn off the power supply. Disconnect the function generator from Clock. Disconnect the scope. Disconnect the jumper. (Q:Exit)')


    def test_reset_LEDs(self, xr):
        """Test if the various reset LEDs turn on when the corresponding pushbuttons are activated
        """
        xr.input('Turn on the power supply and set it to 17V. Connect it to any of the white 6 pin plastic Molex connectors on the board. Has a green LED lit up? Is the input current around ~0.25-0.35A? If so, press ENTER to continue. If not, reconnect power elsewhere and try again. (Q:Exit)')

        questions = [
            "Press the ARM button near Rsts. Does a green LED light up?",
            "Press the Power button near Rsts. Does a green LED light up?",
            "Press the FPGA button near Rsts. Does a green LED light up?"
            ]

        answers = []

        for i, q in enumerate(questions):
            answers.append(xr.input_yes_no(f'{i+1}) {q}', additional_answers=[]))


        pf_table = [["Question", "Status"]] + [[f'{i+1}) {q}', (':red:`FAILED`', ':green:`PASSED`')[answers[i]]] for i,q in enumerate(questions)]
        xr.add_table(pf_table, header=True)


        failed_lines = [str(i+1) for i, ans in enumerate(answers) if not ans]
        if failed_lines:
            print(f"Some tests failed, please address inspection lines {', '.join(failed_lines)}")

        assert not failed_lines, 'Reset LEDs Test Failed'

    #def test_clock(self, xr):

     #   cfg = self.cfg.crate_tests.clock

      #  ca = FPGAArray(icecrates = xr.params.serial, prog = self.cfg.debug.force_fpga_prog, open = 1)

       # while True:
        #    irigb_source = input('Indicate the source for the IRIG-B signal, 1 for bp_time, 2 for bp_trig, or 3 for both. ')
         #   if irigb_source != '1' and irigb_source != '2' and time_source != '3':
          #      print('Wrong input, try again!')
           # else:
            #    break

        #xr.header('Test-Results')
        #try:
         #   result = NameSpace()

            # Create a set of unique slot numbers
          #  slots = set(ca.ib.slot)

            # Print the list of boards
           # print('Populated Slots:')
            #for (slot, ib) in list(ca.ic[0].slot.items()):
             #   print('Slot %02i: IceBoard SN%s' % (slot, ib.serial))

            #print('\nReference clock Frequencies:')
            #clock = []
            #for ib in ca.ib:
             #   raw_clk_freq = ib.FreqCtr.read_frequency('RAW_CLK')
              #  print('Slot %02i: %.6f MHz' % (ib.slot, raw_clk_freq))
               # clock.append(raw_clk_freq)

            #if irigb_source == '1' or irigb_source == '3':
             #   print('\nTime Readout:')
              #  result.time = []
             #   ca.ib.set_irigb_source('bp_time')
              #  print(ca.ib.get_irigb_time())
             #   for ib in ca.ib:
              #      result.time.append(ib.get_irigb_time(format = 'nano'))

           # if irigb_source == '2' or irigb_source == '3':
            #    print('\nTrig Readout:')
             #   result.trig = []
              #  ca.ib.set_irigb_source('bp_trig')
               # time.sleep(3)
                #print(ca.ib.get_irigb_time())
                #for ib in ca.ib:
                 #   result.trig.append(ib.get_irigb_time(format = 'nano'))

            #assert len(slots) == 16, 'Problem with slot detection.'
            #assert min(clock) > cfg.limits[0] and max(clock) < cfg.limits[1], 'Clock out of bounds!'
            #if irigb_source == '1' or irigb_source == '3':
             #   assert max(np.diff(result.time).tolist()) < cfg.max_time_diff, 'Difference in time signal too great between boards!'
            #if irigb_source == '2' or irigb_source == '3':
             #   assert max(np.diff(result.trig).tolist()) < cfg.max_time_diff, 'Difference in trig signal too great between boards!'

        #finally:
         #   xr.save_data(result)
          #  xr.params.test_locals = locals()


    def test_sensor(self, xr):
        cfg = self.cfg.crate_tests.sensor_test

        ca = FPGAArray(icecrates = xr.params.serial, prog = self.cfg.debug.force_fpga_prog, open = 1)

        xr.header('Test-Results')

        try:
            voltage = ca.ib[0].get_backplane_voltage()
            current = ca.ib[0].get_backplane_current()
            bpTemp1 = ca.ib[0].get_backplane_temperature(ca.ib[0].TEMPERATURE_SENSOR.BP_SLOT1)
            bpTemp16 = ca.ib[0].get_backplane_temperature(ca.ib[0].TEMPERATURE_SENSOR.BP_SLOT16)
            #extTemp1 = ca.ib[0].get_backplane_temperature(ca.ib[0].TEMPERATURE_SENSOR.BP_SLOT1_EXTERN)
            #extTemp16 = ca.ib[0].get_backplane_temperature(ca.ib[0].TEMPERATURE_SENSOR.BP_SLOT16_EXTERN)
            serial = ca.ib[0]._get_backplane_serial()

            print('backplane voltage = ' + repr(voltage))
            print('backplane current = ' + repr(current))
            print('backplane slot1 temp = ' + repr(bpTemp1))
            print('backplane slot16 temp = ' + repr(bpTemp16))
            #print 'backplane slot1 extern temp = ' + repr(extTemp1)
            #print 'backplane slot16 extern temp = ' + repr(extTemp16)
            print('backplane serial number = ' + repr(serial))
            ca.print_iceboard_temperatures()

            assert input_yes_no('\nIs the above data sensical? Answer Y/N\n'), "User detected error in sensory data!"
            assert (voltage >= cfg.voltage_limits[0] and voltage <= cfg.voltage_limits[1]), "Voltage out of bounds!"
            assert (current >= cfg.current_limits[0] and current <= cfg.current_limits[1]), "Current out of bounds!"
            for i in range(0, 16):
                assert ca.ib[i].SYSMON.temperature() <= cfg.temp_limit, "FPGA temperature too high!"
            assert input_yes_no("Can you see all heartbeat led's blinking correctly? Answer Y?N\n"), "Heartbeat led's not functioning properly!"

        finally:
            xr.params.test_locals = locals()


    def test_qsfp(self, xr):
        cfg = self.cfg.crate_tests.qsfp_test

        ca = FPGAArray(icecrates = xr.params.serial, prog = self.cfg.debug.force_fpga_prog, open = 1)

        xr.header('Test-Results')

        try:
            qsfpslots = [0]*16
            for i in range(1, 17):
                if ca.ib[0].is_bp_qsfp_present(i):
                    print("Backplane QSFP module present on slot " + repr(i))
                    qsfpslots[i-1] = 1
                else:
                    print("Backplane QSFP module NOT present on slot " + repr(i))

                if qsfpslots[i-1]:
                    print("QSFP module manufactured by: "+ base64.b64decode(ca.ib[0]._bp_qsfp_eeprom_read_base64(i, 148, 16)).strip() + ". Serial number: " + base64.b64decode(ca.ib[0]._bp_qsfp_eeprom_read_base64(i, 196, 16)).strip() + ".")

            for i in range(0, 16):
                assert qsfpslots[i], "Not all connectors were detected!"

            assert input_yes_no('Were the manufactor and serial number correctly read? Answer Y/N\n'), "User detected error in QSFP readout!"

        finally:
            xr.params.test_locals = locals()


    def test_reset(self, xr):
        cfg = self.cfg.crate_tests.reset_test

        ca = FPGAArray(icecrates = xr.params.serial, prog = self.cfg.debug.force_fpga_prog, open = 1)

        result = NameSpace()
        result.res = {}
        result.onoff = {}
        result.res.off = [False]*16
        result.res.on = [False]*16
        result.onoff.off = [False]*16
        result.onoff.on = [False]*16

        xr.header('Begin Reset Test')
        try:
            for i in range(0, 16, 2):
                print("Resetting arm on board %d." % (i+2))
                ca.ib[i].reset_arm_on_slot(i+2)
                result.res.off[i+1] = not ca.ib[i+1].ping()

            print("Waiting ...")
            time.sleep(cfg.arm_sleep_time)

            for i in range(0, 16, 2):
                result.res.on[i+1] = ca.ib[i+1].ping()

            for i in range(1, 16, 2):
                print("Resetting arm on board %d." % i)
                ca.ib[i].reset_arm_on_slot(i)
                result.res.off[i-1] = not ca.ib[i-1].ping()

            print("Waiting ...")
            time.sleep(cfg.arm_sleep_time)

            for i in range(1, 16, 2):
                result.res.on[i-1] = ca.ib[i-1].ping()

            xr.header('Test-Results')
            for i in range(0, len(result.res.off)):
                print(result.res.off[i] , result.res.on[i])

            for i in range(0, len(result.res.off)):
                assert result.res.off[i], "Iceboard(s) did not turn off properly!"
                assert result.res.on[i], "Iceboard(s) did not turn on properly!"

        finally:
            xr.save_data(result.res)
            xr.params.test_locals = locals()

        xr.header('Begin ON/OFF Test')

        try:
            for i in range(0, 16, 2):
                print("Turning board %d off." % (i+2))
                ca.ib[i].set_power_on_slot(i+2, False)
                result.onoff.off[i+1] = not ca.ib[i+1].ping()
                print("Turning board %d on." % (i+2))
                ca.ib[i].set_power_on_slot(i+2, True)

            print("Waiting ...")
            time.sleep(cfg.pow_sleep_time)

            for i in range(0, 16, 2):
                result.onoff.on[i+1] = ca.ib[i+1].ping()

            for i in range(1, 16, 2):
                print("Turning board %d off." % i)
                ca.ib[i].set_power_on_slot(i, False)
                result.onoff.off[i-1] = not ca.ib[i-1].ping()
                print("Turning board %d on." % i)
                ca.ib[i].set_power_on_slot(i, True)

            print("Waiting ...")
            time.sleep(cfg.pow_sleep_time)

            for i in range(1, 16, 2):
                result.onoff.on[i-1] = ca.ib[i-1].ping()

            xr.header('Test-Results')
            for i in range(0, len(result.onoff.off)):
                print(result.onoff.off[i] , result.onoff.on[i])

            for i in range(0, len(result.onoff.off)):
                assert result.onoff.off[i], "Iceboard(s) did not turn off properly!"
                assert result.onoff.on[i], "Iceboard(s) did not turn on properly!"

        finally:
            xr.save_data(result.onoff)
            xr.params.test_locals = locals()


    def test_bitErrorRate(self, xr):

        # Useful shortcuts
        cfg = self.cfg.crate_tests.bitErrorRate_test

        while True:
            numberOfCrates = input('Do you want to test on 1 or 2 crates (QSFP links require 2 crates, unless connected in a loop)?\nEnter "1" or "2" for respective choices. ')
            if numberOfCrates == '1':
                serials = xr.params.serial
                break

            elif numberOfCrates == '2':
                crate2 = input('What is the serial number of the second crate you want to use for this test? ')
                serials = [xr.params.serial, crate2]
                break

            else:
                print('Wrong input!')

        ca = FPGAArray(icecrates = serials, prog = self.cfg.debug.force_fpga_prog, open = 1)

        xr.header('Begin Testing')
        bad_lanes = []
        result = []
        try:
            # for ib in ca.ib:
            #     ib.set_irigb_source('bp_time')

            # if numberOfCrates == '1':
            #     ca.init_corner_turn(mode = 'shuffle256')
            # else:
            #     ca.init_corner_turn(mode = 'shuffle512')

            link_map = {}
            link_map.update(ca.get_backplane_pcb_link_map())
            link_map.update(ca.get_backplane_qsfp_link_map())
            link_map = {link: gtxes for link, gtxes in list(link_map.items()) if (('int' not in gtxes) and (None not in gtxes))}

            result = ca.get_ber(link_list = link_map, period = cfg.period, print_ = True, tx_init_power=2, tx_power=6, tx_qsfp_power=14, minerrors_toprint=0)
            bp_rate = True
            qsfp_rate = True
            #gpu_rate = True
            bad_lanes = []

            for key in list(result.keys()):
                if key[0] == 'pcb':
                    if result[key] >= cfg.bp_limit:
                        bp_rate = False
                        bad_lanes.append((key, result[key]))
                elif key[0] == 'qsfp':
                    if result[key] >= cfg.qsfp_limit:
                        qsfp_rate = False
                        bad_lanes.append((key, result[key]))
                elif key[0] == 'GPU':
                    #if result[key] >= cfg.gpu_limit:
                    #    gpu_rate = False
                    #    bad_lanes.append((key, result[key]))
                    del result[key]

            xr.header('Test-Results')
            keys = list(result.keys())
            keys.sort()
            for key in keys:
                print(key, round(result[key], 3))

            bad_lanes.sort()

            assert bp_rate, 'Bit Error Rate for Backplane lanes too high!'
            assert qsfp_rate, 'Bit Error Rate for QSFP lanes too high!'
            #assert gpu_rate, 'Bit Error Rate for GPU lanes too high!'

        finally:
            print('Bad Links:')
            for item in bad_lanes:
                print(item)
            xr.save_data(result)
            xr.params.test_locals = locals()

    def test_mezzRamp(self, xr):
        cfg = self.cfg.crate_tests.mezzRamp_test

        ca = FPGAArray(icecrates = xr.params.serial, prog = self.cfg.debug.force_fpga_prog, open = 1)

        result = NameSpace()  # test result container
        board = 0

        xr.header('Test-Results')
        try:
            for ib in ca.ib:
                result[board] = NameSpace()
                result[board].data = []
                result[board].ramp_ok = []

                for mezz in list(ib.mezzanine.values()):
                    print('initializing mezzanine...')
                    mezz.init()

                print('Computing ADC delays for board serial number: ' + ib.serial)
                ib.set_adc_delays(source='default', compute_delays=1, save_delays=False, check_sync_delays=True, check_adc_delays=20, verbose=0, retry=5)

                for mezz in list(ib.mezzanine.values()):

                    delay_table= ib.get_adc_delays()
                    print('Opening data receiver socket')
                    receiver = ib.get_data_receiver()

                    print('Setting up ramp transmission...')
                    ib.set_adcdaq_mode('data')
                    ib.set_data_source('adc')
                    ib.set_adc_mode('ramp')
                    ib.start_data_capture(period=1, source='adc')

                    print('Syncing...')
                    ib.sync()

                    print('Getting data frames...')
                    receiver.read_frames(flush=1, frames=3)  # flush
                    data = receiver.read_frames(1)
                    ib.stop_data_capture()

                    result[board].data.append(data)
                    plt.figure(1)

                    ideal_ramp = (np.arange(2048) - 128).astype(np.int8)

                    ramp_ok = []
                    for ch in range(8):
                        plt.clf()
                        plt.plot(data[ch])
                        xr.insert_plot('Ramp capture for %s SN%s CHANNEL %02i' % (xr.params.model, xr.params.serial, ch))
                        ok = np.all(data[ch] == ideal_ramp)
                        ramp_ok.append(ok)
                        if ok:
                            print('Channel %02i: OK' % (ch+1))
                        else:
                            print('Channel %02i: ERROR!' % (ch+1))

                    result[board].ramp_ok.append(ramp_ok)

                board += 1

            for board in range(16):
                assert all(result[board].ramp_ok), 'One or more channels have ramp errors'

        finally:
            xr.params.test_locals = locals()  # store local variables for interactive debugging
            #receiver.close()
            xr.save_data(result)


if __name__ == '__main__':
    """ Start the menu to run tests in this file."""
    run_test_menu()