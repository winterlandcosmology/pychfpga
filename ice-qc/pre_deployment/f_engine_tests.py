#!/usr/bin/env python

"""
Tests to be run in preparation for deployment of a full F-Engine, including
a fully-populated crate, switch, power supply, GPS, etc.
"""

# Standard packages
import asyncio
import time
import re

# Pypi packages
import pytest
import numpy as np

# External private packages
from wtl.namespace import NameSpace
from wtl.pytest_xreport import xr, TestMenu
import pychfpga
from pychfpga import fpga_array
import labpy
import os

if os.environ.get("PYTEST_PLUGINS") is not None and os.environ.get("PYTEST_PLUGINS") != "":
    os.environ["PYTEST_PLUGINS"] += ",wtl.pytest_xreport"
else:
    os.environ["PYTEST_PLUGINS"] = "wtl.pytest_xreport"

TEST_CONFIG_FILE = './test_config.yaml'

class TestUtils:
    """ Some utility methods common to all tests.
    """

    def open_instrument(self, name):
        instr_params = self.cfg.instruments[name].copy()
        class_name = instr_params.pop('labpy_object')
        return labpy.open_instrument(class_name, **instr_params)

    def open_ps(self, name='ps', voltage=None, current=None):
        """ Opens a power supply and configures it

        The `voltage` and `current` to be programmed can be specified. If `None`, the global values from
        the config file in ``f_engine_tests.global_settings`` will be used.
        """
        # open power supply instrument if it is in the list of instruments and if we don't force manual operation
        if 'ps' in self.cfg.instruments and not self.cfg.get('manual_ps', False):
            self.ps = self.open_instrument(name)
        else:
            self.ps = None

        # initialize power supply if we have one
        print('Setting up power supply')
        if self.ps:
            voltage = voltage if voltage is not None else self.cfg.f_engine_tests.global_settings.ps_voltage
            current = current if current is not None else self.cfg.f_engine_tests.global_settings.ps_current
            self.ps.set_output(state=False) #Ensuring power on N5764A is off
            self.ps.clear() #Clearing any previous protection
            self.ps.set_voltage(voltage=voltage) #Setting voltage, power still off
            self.ps.set_current_limit(current=current, ocp=True) #Setting current limit and turning on ocp feature
        return self.ps

    def crate_init(self, reset_power = True):
        """
        Run fpga_array, initializing the crate. Recover ca object. Power is reset by default.
        """
        if reset_power:
             # Check if self.ps already exists (i.e. ps on from previous test)
             if not self.ps:
                 self.ps = self.open_ps()
             # Check status of power supply:
             ps_status = self.ps.status()['status']
             if ps_status == 'ON' or ps_status == 'OK':
                print(f'Power supply is {ps_status}')
                # Turning off power supply
                print(f'Turning off power supply...')
                self.ps.set_output(state=False) # Force power cycle if power supply is on
                ps_status = self.ps.status()['status']
                print(f'Power supply is now {ps_status}')
                time.sleep(5) # Give it a few seconds before turning back on
             else:
                 print(f'Power supply is {ps_status}')
             # Turn power supply back on:
             print(f'Turning on power supply...')
             self.ps.set_output(state=True)
             delay = self.cfg.f_engine_tests.global_settings.ps_t_sleep
             print(f'Waiting for {delay} seconds to let the boards boot')
             time.sleep(delay) # Sleep to let the crate boot

        fpga_array_params = self.cfg.f_engine_tests.global_settings.fpga_array_params
        ca = fpga_array.FPGAArray(**fpga_array_params)
        ic = ca.ic[0]
        # set crate model and serial number
        self.model = ic.part_number
        self.serial = ic.serial

        if ca.ib:
            return ca

class TestPreDeploymentCrate(TestUtils):
    """
    List of tests:

    0?) Visual inspection?
    
    0) Check connection to power supply

    1) Check board initialization:
        - all 16 boards found in hwm
        - all FPGAs programmed
        - crate must sync
    2) Check backplane errors (requires above to pass to be able to run)
        - initialize crate
        - iterate through output of await ca.print_shuffle_status(verbose = 1)
        - maybe save the verbose = 2 one as well?
    3) Force IRIG-B syncs
        - initialize crate
        - config-defined number of sync tries
    4) Check ADC clocks
        - initialize crate
        - iterate through all motherboards and mezzanines, check that
          clocks are 200 MHz +/- tolerance (set by integration time)
    5) Force ADC sync delay calculations
        - intialize crate
        - visual inspection
    6) Check ADC eye diagrams
        - initialize crate
        - visual inspection
    7) Power cycle
        - initialize crate N times, sleep for t seconds in between
        - scrape clock error and udp error metrics, add a counter for each
    """

    @pytest.fixture(autouse=True)
    def setup(self, xr):
        """ Prepare the test for execution. This fixture is executed automatically before each test.
        """
        xr.header('Setting-up')
        self.cfg = xr.config  # get the test config NameSpace
        # pre-define instrument variable. We'll load them only as needed by the tests.
        self.ps = None
        self.model = None # should be set by the test
        self.serial = None # should be set by the test
        
        yield  # pass control to the test and return

        # pass the model and serial number we discoverd back to XReport so the test result files can be named appropriately
        # xr.params.model = self.model
        # xr.params.serial = self.serial

        # turn off power supply
        if self.ps:
            self.ps.set_output(state=False) #Ensuring power on N5764A is off

    def test_ps_connection(self, xr):
        """
        QC000: Power supply connection test: ensure power supply responds to commands consistently.

        Procedure:
            
            - Start the power supply connection test on the computer
            - Open connection to power supply
            - If test fails, then power supply stops turning on/off
        """
        xr.header('Power Supply Connection Test')
        cfg = self.cfg.f_engine_tests.ps_connection_test
        n_ps_cycles = cfg.n_ps_cycles
        on_time = cfg.on_time
        off_time = cfg.off_time

        test_results = NameSpace()

        turn_ON_errs = []
        turn_OFF_errs = []

        passed = False

        try:

            for n in range(n_ps_cycles):
                
                # open connection to power supply
                #********************************************************************************************************************************
                #if this test fails, place this line outside of the for loop - maybe opening the connection everytime is the source of the problem
                #***********************************************v = TestMenu(TEST_CONFIG_FILE).run()*********************************************************************************
                self.ps = self.open_ps()


                print('\n*******************************')
                print(f'Cycle {n+1}/{n_ps_cycles}')
                print('*******************************')

                          
                print('--------------------------------------------')
                print(f'Turning ON power supply')
                print(f'Wait {on_time} seconds')
                # turn on power supply
                self.ps.set_output(state=True)
                time.sleep(on_time)
                ps_status = self.ps.status()['status']
                print(f'Power supply is {ps_status}')
                if ps_status != 'ON' and ps_status != 'OK':
                    turn_ON_errs.append(f'cycle {n+1}: {ps_status}')
                
                print('--------------------------------------------')
                print(f'Turning OFF power supply')
                print(f'Wait {off_time} seconds')
                # turn off power supply
                self.ps.set_output(state=False)
                time.sleep(off_time)
                ps_status = self.ps.status()['status']
                print(f'Power supply is {ps_status}')
                if ps_status != 'OFF':
                    turn_OFF_errs.append(f'cycle {n+1}: {ps_status}')
                print('--------------------------------------------')

            assert (not turn_ON_errs and not turn_OFF_errs), f'Power supply connection errors: turning ON errors: {turn_ON_errs}, turning OFF errors: {turn_OFF_errs}'
            passed = True
           

        finally:
            test_results.passed = passed
            xr.save_data(test_results)
            # if this test passed, the ps should already be off; if it didn't, it can't be turned off remotely and has to be turned off manually
            # self.ps.set_output(state=False)    

    def test_init(self, xr):
        """
        QC001: Crate initialization test: ensure crate properly intializes

        Procedure:

          - Start the crate init test on the computer
          - Power up crate
          - If init ok, test passed:
            - all 16 boards found in hwm
            - all FPGAs programmed
            - crate must sync

        """

        xr.header('Crate Initialization Test')
        cfg = self.cfg.f_engine_tests.crate_init # not used
        test_results = NameSpace()

        # Initialize the crate:
        passed = False
        try:
            # input('Turn on the power supply. Press ENTER to continue. (Q:Exit) ')
            self.ca = self.crate_init()
            # If we made it to this point, FPGAs programmed, all boards present in hwm
            # and crate has synced (see test_config.yaml - parameters passed to FPGAArray
            # force all these to be present to return self.ca
            assert self.ca.ib, f'ib object is {self.ca.ib}'
        finally:
            test_results.passed = passed
            xr.save_data(test_results)
            #self.ps.set_output(state=False) # Turn off power supply
    

    def test_bperr(self, xr):
        """
        QC002: Backplane error test: check if there are errors in print_shuffle_status()

        Procedure:

            - Start the bperr test on the computer
            - Power up crate
            - After initializing crate, check backplane errors using
                await ca.print_shuffle_status()

        """

        xr.header('Backplane Error Test')
        cfg = self.cfg.f_engine_tests.backplane_err # not needed
        shuffle_mode = self.cfg.f_engine_tests.global_settings.fpga_array_params.mode
        n_checks = cfg.n_checks
        t_sleep = cfg.t_sleep
        test_results = NameSpace()

        passed = False
        bp_errs = []
    
        try:
            # Initialize the crate
            self.ca = self.crate_init()

            for n in range(n_checks):

                # Add 1s of sleep time between error gathering in case errors accumulate:
                time.sleep(t_sleep)

                # Get corner turn engine status:
                # info = asyncio.run(self.ca.get_corner_turn_engine_status_async(reset_stats=True)) # reset stats for each check

                bp_errs_cycle_n = self.check_bp_errs()
               # bp_errs.append([f'cycle {n}', bp_errs_cycle_n])
               # cycle_errs.append(bp_errs_cycle_n)
                if bp_errs_cycle_n:
                    bp_errs.append([f'cycle {n}', bp_errs_cycle_n])
               # bp_errs.append(bp_errs_cycle_n)
                # # In shuffle256 mode, there should be no errors on any lanes on any subsystems on any motherboard.

                # if shuffle_mode == 'shuffle256':
                #     for slot in np.arange(16)+1:
                #         subsystems = info[0]['slots'][slot]['subsystems']
                #         for ss in subsystems:
                #             idx = list(subsystems[ss]['lanes'].keys())
                #             for lane in idx:
                #                 lane_info = subsystems[ss]['lanes'][lane]
                #                 status = lane_info['status']
                #                 label = lane_info['label']
                #                 fields = lane_info['fields']
                #                 # print(slot, ss, label, fields, status)
                #                 if status == True:
                #                     # if status == True, append info (True --> error)
                #                     bp_errs.append([slot, ss, lane, status, label ,fields])

                # # In shuffle128 mode, it is expected that there will be errors associated with the slots that are not present.
                # # We need to iterate through the subsystems and determine what needs to be checked systematically, hence the
                # # many specific 'if' statements.

                # if shuffle_mode == 'shuffle128':
                #     # First, need to identify which Tx/Rx pairs are expected to fail due to having only 8 boards:
                #     slots = [i.slot-1 for i in self.ca.ib] # Subtract 1 to get 0-base
                #     all_slots = np.arange(16) # 0-base
                #     missing_slots = list(set(slots) ^ set(all_slots)) # Using XOR operator ^ to get slots uncommon to both lists
                #     for slot in slots:
                #         subsystems = info[0]['slots'][slot+1]['subsystems']
                #         cb2_ignore_lanes = [] # Needed to tell 'CB2 FRAME #' which lanes to check.
                #         for ss in subsystems:
                #             idx = list(subsystems[ss]['lanes'].keys())
                #             for lane in idx:
                #                 lane_info = subsystems[ss]['lanes'][lane]
                #                 status = lane_info['status']
                #                 label = lane_info['label']
                #                 fields = lane_info['fields']

                #                 if ss == 'BP PCB':
                #                     # Need to check the Tx/Rx pairs to see whether any are associated with a
                #                     # slot number that is not present (i.e. one without a board).
                #                     tx = fields['Tx']
                #                     rx = fields['Rx']
                #                     tx = [int(s) for s in re.findall(r'\b\d+\b', f'{tx}')] # convert pairs to lists of ints
                #                     rx = [int(s) for s in re.findall(r'\b\d+\b', f'{rx}')]
                #                     if len(list(set(tx) & set(slots))) == 2 and len(list(set(rx) & set(slots))) == 2:
                #                         # We use the '&' operator to get the slot numbers common to both tx or rx and slots.
                #                         # If the len of both lists is 2, then both the Tx and Rx pairs are from valid slots,
                #                         # and we can check their status:
                #                         if status == True:
                #                             bp_errs.append([slot, ss, lane, status, label ,fields])

                #                 if ss == 'CB2 ALIGN':
                #                     # Here, we need to check whether there is an 'IGNORE' in the field. If not,
                #                     # check if there's an error. If yes, append the lane number to cb2_ignore_lanes
                #                     # so that we know which lanes to check when we go through the 'CB2 FRAME #' subsystem
                #                     if 'IGNORE' not in list(fields.keys()) and status == True:
                #                         bp_errs.append([slot, ss, lane, status, label ,fields])
                #                     if 'IGNORE' in list(fields.keys()):
                #                         cb2_ignore_lane.append(lane)

                #                 if ss == 'CB2 FRAME #':
                #                     # Check the lanes with no 'IGNORE' in 'CB2 ALIGN'
                #                     if lane not in cb2_ignore_lanes and status == True:
                #                         bp_errs.append([slot, ss, lane, status, label ,fields])

                #                 # The other subsystems are 'BP QSFP', 'CB2 BIN SEL', 'CB2 ALIGN', 'CB2 FRAME #', and 'CB3 BIN SEL'.
                #                 # None of these should have any errors on any lane, so we can just check if all lanes are good:

                #                 else:
                #                     if status == True:
                #                         bp_errs.append([slot, ss, lane, status, label ,fields])

            assert not bp_errs, f'Backplane errors present on: {bp_errs}'
        
           # if not bp_errs:            
            passed = True

        finally:
            test_results.passed = passed
            xr.save_data(test_results)
            self.ps.set_output(state=False) # Turn off power supply
            #assert not bp_errs, f'Backplane erros present on: {bp_errs}'
            
    def test_sync(self, xr):
        """
        QC003: Sync/IRIG-B test: stress test of crate sync using IRIG-B

        Procedure:

            - Start the sync test on the computer
            - Power up crate
            - After initializing crate, run self.ca.sync() in a loop, and
              only pass if it's able to successfully sync N times (e.g. N = 100)

        """

        xr.header('Crate Sync Test')
        cfg = self.cfg.f_engine_tests.sync_test # not needed
        test_results = NameSpace()

        passed = False
        n_syncs = cfg.n_syncs
        sync_counter = 0
        exception_fails = 0
        exception_tags = []

        # Initialize the crate
        try:
            # xr.input('Turn on the power supply. Press ENTER to continue. (Q:Exit) ')
            self.ca = self.crate_init()
            for n in range(n_syncs):
                print(f'Sync {n+1}')
                try:
                    self.ca.sync()
                    # If no exception to the above, increment counter:
                    # sync_counter += 1
                except Exception as e:
                    exception_fails += 1
                    exception_tags.append([f'Cycle {n}', repr(e)])
                else:
                    sync_counter += 1

            assert sync_counter == n_syncs, f'Sync fails observed on: {exception_tags}'
            passed = True
        finally:
            test_results.passed = passed
            xr.save_data(test_results)
            self.ps.set_output(state=False) # Turn off power supply

    def test_adc_clocks(self, xr):
        """
        QC004: ADC clock test: ensure all ADCs get the correct clocks on every channel.

        Procedure:

          - Start the ADC clock test on the computer
          - Power up crate
          - Iterate through all clocks on all mezzanines on all motherboards, check that they
            are 200 MHz +/- some tolerance set in the config.

        """

        xr.header('ADC Clocks Test')
        cfg = self.cfg.f_engine_tests.clock_test
        test_results = NameSpace()

        integration_period = cfg.integration_period
        n_clock_checks = cfg.n_clock_checks
        n_cycles = cfg.n_cycles
        delay = cfg.powerup_delay

        # The FreqCtr counts rising clock edges; the most it could miss over a given integration period
        # is 1 edge. Also, FreqCtr measures at 1/2 the rate of the 400 MHz clock coming from the ADC,
        # so it could miss, at most, 2 rising edges.
        expected_diffs = {-2/integration_period, 0.0, 2/integration_period}
        failed_clocks = []
        failed_boards = []
        measured_diffs = []

        passed = False
        try:
            for n in range(n_cycles):
                print(f'****************************')
                print(f'Power-cycling test iteration {n + 1}/{n_cycles}')
                print(f'****************************')
                
                # Initialize the crate:
                try:
                    self.ca = self.crate_init(reset_power=True)

                    # if len(self.ca) < 16:
                    #     while True:
                    #         pass
                
                
                except (RuntimeError, IOError, OSError) as e:
                    
                    print(f'Failed initializing the array because of error {e!r}')
                    
                    
                else:
                    for i in self.ca.ib:
                        print(f'Checking adc clocks on: {i}')
                        error = i.check_adc_frequencies('after boot')
                        if error:
                            failed_boards.append(i)
                        # for clock in range(16):
                        #     print(f'{i}, ADC_CLK{clock}')
                        #     diffs = set(i.FreqCtr.read_frequency(f'ADC_CLK{clock}', integration_period) - 200e6 for _ in range(n_clock_checks))
                        #     # If diffs is not a subset of expected_diffs, i.e. it contains an unexpected value, then append to failed_clocks
                        #     if not diffs.issubset(expected_diffs):
                        #         failed_clocks.append([f'{i}', f'ADC_CLK{clock}', diffs])    

                    

                # self.ps.set_output(state=False)
                time.sleep(delay)

            print(f'{failed_boards=}')

            assert not failed_boards, f'ADC clock errors present on: {failed_boards}'
            
            
            # passed = True
            # Initialize the crate:
            # self.ca = self.crate_init()

            # Iterate through each motherboard. Form a set of unique values of the
            # difference between the measured clock and the expected 200 MHz. To pass,
            # there should only be two values in the set: 0 and 200 MHz/count_time (which
            # is the maximum error, resulting from a missed rising edge) --> is this correct?

            # for i in self.ca.ib:
            #     for clock in range(16):
            #         print(f'{i}, ADC_CLK{clock}')
            #         diffs = set(i.FreqCtr.read_frequency(f'ADC_CLK{clock}', integration_period) - 200e6 for _ in range(n_clock_checks))
            #         # If diffs is not a subset of expected_diffs, i.e. it contains an unexpected value, then append to failed_clocks
            #         if not diffs.issubset(expected_diffs):
            #             failed_clocks.append([f'{i}', f'ADC_CLK{clock}', diffs])    
            # assert not failed_clocks, f'ADC clock errors present on: {failed_clocks}'
            # passed = True

        finally:
            test_results.passed = passed
            xr.save_data(test_results)
            # self.ps.set_output(state=False) # Turn off power supply


        

    def test_adc_clocks_cycled(self, xr):
        """
        QC004: ADC clock test: ensure all ADCs get the correct clocks on every channel + power cycling between checks

        Procedure:

          - Start the ADC clock test on the computer
          - Power up crate
          - Iterate through all clocks on all mezzanines on all motherboards, check that they
            are 200 MHz +/- some tolerance set in the config.

        """

        xr.header('ADC Clocks Test')
        cfg = self.cfg.f_engine_tests.clock_test
        test_results = NameSpace()

        integration_period = cfg.integration_period
        n_clock_checks = cfg.n_clock_checks

        # The FreqCtr counts rising clock edges; the most it could miss over a given integration period
        # is 1 edge. Also, FreqCtr measures at 1/2 the rate of the 400 MHz clock coming from the ADC,
        # so it could miss, at most, 2 rising edges.
        expected_diffs = {-2/integration_period, 0.0, 2/integration_period}
        failed_clocks = []
        measured_diffs = []

        import pyvisa
        rm = pyvisa.ResourceManager()
        # rm.list_resources()

        adapter = 'TCPIP::10.10.10.206::INSTR'
        scope = rm.open_resource(adapter)  
        
        if scope: 
            print(scope.query('*IDN?'))
            scope.write('MEASUrement:IMMed:SOUrce1 CH1')
            scope.write('MEASUrement:IMMed:TYPe FREQuency')
            



        passed = False
        try:
            # Initialize the crate:
            self.ca = self.crate_init()

            # Iterate through each motherboard. Form a set of unique values of the
            # difference between the measured clock and the expected 200 MHz. To pass,
            # there should only be two values in the set: 0 and 200 MHz/count_time (which
            # is the maximum error, resulting from a missed rising edge) --> is this correct?

            for i in self.ca.ib:
                # for clock in range(16):
                #     print(f'{i}, ADC_CLK{clock}')
                #     diffs = set(i.FreqCtr.read_frequency(f'ADC_CLK{clock}', integration_period) - 200e6 for _ in range(n_clock_checks))
                #     # If diffs is not a subset of expected_diffs, i.e. it contains an unexpected value, then append to failed_clocks
                #     if not diffs.issubset(expected_diffs):
                #         failed_clocks.append([f'{i}', f'ADC_CLK{clock}', diffs])    
                error = i.check_adc_frequencies('after boot')
                if scope and error: 
                    for n in range(10):
                        freq  = scope.query('MEASUREMENT:IMMED:Value?')
                        print("")

            # assert not failed_clocks, f'ADC clock errors present on: {failed_clocks}'
            assert not error, f'ADC clock errors present on: {failed_clocks}'
            passed = True

        finally:
            test_results.passed = passed
            xr.save_data(test_results)
            self.ps.set_output(state=False) # Turn off power supply

    def test_sync_delays(self, xr):
        """
        QC005: Sync delays test: ensure ADC sync delays have no errors or issues

        Procedure:

          - Start the sync delays test on the computer
          - Power up crate
          - Iterate through each motherboard, and visually inspect the sync delays.

        """

        xr.header('Sync Delays Test')
        cfg = self.cfg.f_engine_tests.sync_delay_test
        test_results = NameSpace()

        sync_delay_errors = []

        # Initialize the crate:
        passed = False
        try:
            # input('Turn on the power supply. Press ENTER to continue. (Q:Exit) ')
            self.ca = self.crate_init()

            for i in self.ca.ib:
                while True:
                    xr.input(f'Press ENTER to compute sync delays for {i}. (Q:Exit) ')
                    i.compute_sync_delays()
                    errs_present = xr.input('Are there any sync delay errors? Answer Y/N. (Q:Exit) ')

                    if errs_present == 'y':
                        sync_delay_errors.append(i)

                    print_again = xr.input('Would you like to see the sync delays again? Answer Y/N. (Q:Exit) ')

                    if print_again == 'n':
                        break

            assert not sync_delay_errors, f'Sync delay errors present on: {sync_delay_errors}'
            passed = True

        finally:
            test_results.passed = passed
            xr.save_data(test_results)
            self.ps.set_output(state=False) # Turn off power supply

    def test_adc_eye(self, xr):
        """
        QC006: ADC eye test: check ADC eye diagrams to ensure stability.

        Procedure:

          - Start the adc eye test on the computer
          - Power up crate
          - Inspect the adc eye diagrams.

        """

        xr.header('ADC Eye Test')
        cfg = self.cfg.f_engine_tests.adc_eye_test
        n_checks = cfg.n_checks
        max_spread = cfg.max_spread
        n_refs = cfg.n_refs
        n_fails_accept = cfg.n_fails_accept # number of allowable fails. if exceeded, board/channel pair fails.
        test_results = NameSpace()

        
        
        # Initialize the crate:
        passed = False

        try:
            # input('Turn on the power supply. Press ENTER to continue. (Q:Exit) ')
            self.ca = self.crate_init()


            for i in self.ca.ib:
                spreads = []
                unstable_channels = []

                print(f'=============================================================')
                print(f'Checking ADC eye diagrams for {i}')

                # converts the eye diagram back to a bitwise representation then takes n_check samples
                # that it then ORs accross, an ideal eye diagram would have 8 bits at the end if all the
                # bits stayed identical accross samples

                eye_diagram = np.zeros((16, 32, 11), np.uint8)
                eye_diagram = np.unpackbits(eye_diagram, axis=2)

                for n in range(n_checks):
                    try:
                        eye_diagram_capture = i.capture_adc_eye_diagram()
                        eye_diagram_capture = np.unpackbits(eye_diagram_capture, axis=2)

                        eye_diagram = np.bitwise_or(eye_diagram, eye_diagram_capture)
                    finally:
                        break
                        

                spread = np.sum(eye_diagram, axis=2)
                spreads.append(spread)

                # checks if the ammount of bit smear is larger than expected

                for unstable_channel in np.argwhere(spread > max_spread):
                    unstable_channels.append(unstable_channel)
                    print(f'Channel {unstable_channel[0]} line {unstable_channel[1]} is unstable')

                print(f'Largest bit spread after {n_refs} checks: {spread.max()}')
                    


                # unstable_channel = np.argwhere(spread > max_spread)

                # if np.any(unstable_channel):
                #     unstable_channels.append(unstable_channel)
                
                # for i, spread in enumerate(spreads):
                    
                    
            

                # for channel in range(16):
                #     print(f'=============================================================')
                #     print(f'Checking ADC eye diagrams for {i}, ADC channel {channel}')

                #     ref_nz_idx = [] # 'reference non-zero indices', checks where non-zero values are in eye diagram

                    # Gather non-zero indices for a few reference eye diagrams. n_refs should be large enough
                    # to capture any small jitters. If there really are weird, large jitters, they should move
                    # around enough that they're not capture by the reference diagrams. If it's just small jitters,
                    # they should be captured by the reference diagrams.

                

                    

                    # for n in range(n_refs):
                    #     ref_d = i.capture_adc_eye_diagram(channels=[channel])[0] # sample reference diagram
                    #     nz_idx = np.where(ref_d != 0) # non-zero indices
                    #     for j in range(len(nz_idx[0])):
                    #         pair = [nz_idx[0][j], nz_idx[1][j]] # generates index pairs
                    #         if pair not in ref_nz_idx:
                    #             ref_nz_idx.append(pair) # if pair not already in ref_nz_idx, add it

                    # ref_nz_idx = set(tuple(x) for x in ref_nz_idx) # change ref_nz_idx to a set so we can use issubset

                    # # Now iterate through n_checks more diagrams to check stability
                    # fails_counter = 0
                    # for n in range(n_checks):
                    #     d = i.capture_adc_eye_diagram(channels=[channel])[0] # grab a diagram
                    

                    #     nz_idx = np.where(d != 0)
                    #     pairs = []
                    #     for j in range(len(nz_idx[0])):
                    #         pairs.append([nz_idx[0][j], nz_idx[1][j]]) # add every pair to pairs
                    #     pairs = set(tuple(x) for x in pairs) # change pairs to set

                    #     # If pairs is not a subset of ref_nz_idx, i.e. the channel is unstable,
                    #     # add the motherboard index and channel number to unstable_channels if not already present
                    #     if not pairs.issubset(ref_nz_idx):
                    #         print(f'Fail on diagram {n+1}')
                    #         print(f'Pairs \n  {pairs}')
                    #         print(f'Nzindex \n {nz_idx}')
                    #         print(f'Diff: \n {pairs - ref_nz_idx}')
                    #         print(f'Capured \n{d}')

                    #         fails_counter += 1
                    #         if (fails_counter > n_fails_accept and n == n_checks-1):
                    #             # If accetpable fails is exceeded, and we've reached the last diagram check, append the channel:
                    #             unstable_channels.append([f'{i}', f'Channel {channel}', f'{fails_counter} fails'])
            
            print()

            assert not unstable_channels, f'Unstable channels: {unstable_channels}'
            passed = True

        finally:
            test_results.passed = passed
            xr.save_data(test_results)
            self.ps.set_output(state=False) # Turn off power supply


    def test_network(self, xr):
        xr.header('Network Test')
        cfg = self.cfg.f_engine_tests.network_test

        test_results = NameSpace()
        n_cycles = cfg.n_cycles
        t_cycle = cfg.t_cycle

        missing_slot_errors = []
        error_total = 0

        try:

            for n in range(n_cycles):
                print(f'****************************')
                print(f'Power-cycling test iteration {n + 1}/{n_cycles}')
                print(f'****************************')
                
                # Initialize the crate:
                try:
                    if not self.ps:
                        self.ps = self.open_ps()
                    # Check status of power supply:
                    ps_status = self.ps.status()['status']
                    if ps_status == 'ON' or ps_status == 'OK':
                        print(f'Power supply is {ps_status}')
                        # Turning off power supply
                        print(f'Turning off power supply...')
                        self.ps.set_output(state=False) # Force power cycle if power supply is on
                        ps_status = self.ps.status()['status']
                        print(f'Power supply is now {ps_status}')
                        time.sleep(5) # Give it a few seconds before turning back on
                    else:
                        print(f'Power supply is {ps_status}')
                    # Turn power supply back on:
                    print(f'Turning on power supply...')
                    self.ps.set_output(state=True)
                    delay = 60
                    print(f'Waiting for {delay} seconds to let the boards boot')
                    time.sleep(delay) # Sleep to let the crate boot

                    # self.ca = self.crate_init()
                    fpga_array_params = cfg.fpga_array_params
                    self.ca = fpga_array.FPGAArray(**fpga_array_params)
                    
                    ic = self.ca.ic
                    from pychfpga.hardware import Crate
                    missing_slots = {
                        (ic.part_number, ic.serial, ic.crate_number): set(range(1, ic.NUMBER_OF_SLOTS + 1)) - set(ic.slot)
                        for ic in Crate.get_all_instances()}
                    
                    # for i in boards_to_ping:
                    #     if i not in self.ca.ib.values():
                    #         # print('********************************')
                    #         print(f'Failed to connect to board {i}')

                    if any(missing_slots.values()):
                        missing_slots_str = '\n'.join(
                            '    Crate #{number} ({model} SN{serial}): slots {slots}'.format(
                                number=number,
                                model=model,
                                serial=serial,
                                slots=', '.join(str(s) for s in slots))
                            for ((model, serial, number), slots) in missing_slots.items() if slots)
                        print(f'{self!r}: The following slots are missing:\n{missing_slots_str}')
                        missing_slot_errors.append(tuple(n, missing_slots_str))
                        error_total += len(missing_slots)
                    

                except (RuntimeError, IOError, OSError) as e:
                    print("*"*10)
                    print(f'Failed initializing the array because of error {e!r}')
                
            

                    
                print('Turning OFF the crate')
                self.ps.set_output(state=False)
                print(f'Letting the crate cool down for {t_cycle} seconds before repeating the test')
                time.sleep(t_cycle)


            assert not missing_slot_errors, f'The following slots failed to appear: \n{missing_slot_errors} \n Total of {error_total} errors'

        finally:


            test_results.passed = True
            xr.save_data(test_results)
            self.ps.set_output(state=False) # Turn off power supply



    def test_power_cycle(self, xr):
        """
        QC007: Power cycle test: cycle crate initializations, logging ADC clock and UDP errors.

        Procedure:

          - Start the power cycle test on the computer
          - Power up crate
          - Cycle through crate initialization n_cycles times, count
            some relevant errors

        """

        xr.header('Power Cycle Test')
        cfg = self.cfg.f_engine_tests.p_cycle_test
        n_cycles = cfg.n_cycles
        percent_accept = cfg.percent_accept
        n_fails_accept = int(percent_accept*n_cycles) # Define number of acceptable fails
        t_cycle = cfg.t_cycle
        # run_all_tests = cfg.run_all_tests
        # minutes = cfg.t_pause_crate_init

        test_results = NameSpace()

        # A fail is defined as a cycle which has one or more errors.

        init_exception_fails = 0
        init_exception_tags = []

        clk_fails = 0
        udp_fails = 0
        mmi_fails = 0

        adc_err_tags = []
        udp_err_tags = []
        mmi_err_tags = []

        delay_fails = 0
        delay_exception_tags = []

        backplane_fails = 0
        backplane_err_tags = []

        passed = False
        


        adc_clks = []



        try:

            for n in range(n_cycles):
                print(f'****************************')
                print(f'Power-cycling test iteration {n + 1}/{n_cycles}')
                print(f'****************************')
                
                # Initialize the crate:
                try:
                    self.ca = self.crate_init()
             
                except (RuntimeError, IOError, OSError) as e:
                    
                    print(f'Failed initializing the array because of error {e!r}')
                    
                    init_exception_fails += 1
                    init_exception_tags.append({'Cycle': n, 'exception': repr(e)})
                    
                else:
                    # Run the backplane test first (otherwise set_adc_delays makes things too busy and can cause issues)
                    print('Checking backplane errors...')
                    bp_errs = self.check_bp_errs()
                    if len(bp_errs) > 0:
                        print('Got the following backplane errors:')
                        print('bp_errs: ', bp_errs)
                        backplane_fails += 1
                        backplane_err_tags.append({'Cycle': n,
                                                  'ib': "none",
                                                  'bp_errs': bp_errs})

                    print('Got an IceBoard array')
                    for i in self.ca.ib:
 
                        # Check counters and error lists:
                        n_clk_errs = i.adc_clk_err_ctr
                        clk_errs_msgs = i.adc_clk_err_msgs
                        n_udp_errs = i.udp_err_ctr 
                        n_mmi_errs = i.mmi.error_counter
                        udp_errs_msgs = i.adc_clk_err_msgs
                        udp_temp_errors= i.get_temperatures()
                        print(f'Slot {i.slot} got {n_clk_errs} ADC clock errors, {n_udp_errs} UDP communication errors')

                        # Increment error counters if needed, and append relevant
                        # information to err_tags:
                        if n_clk_errs != 0:
                            clk_fails += 1
                            adc_err_tags.append({'Cycle': n,
                                         'ib': i,
                                         'n_clk_errs': n_clk_errs,
                                         'clk_err_msgs': clk_errs_msgs})
                        if n_udp_errs != 0:
                            udp_fails += 1
                            udp_err_tags.append({'Cycle': n,
                                         'ib': i,
                                         'n_udp_errs': n_udp_errs,
                                         'clk_err_msgs': clk_errs_msgs,
                                         'temps': udp_temp_errors})
                            
                        if n_mmi_errs != 0:
                            mmi_fails += 1
                            udp_err_tags.append({'Cycle': n,
                                         'ib': i,
                                         'n_mmi_errs': n_udp_errs,
                                         'clk_err_msgs': clk_errs_msgs,
                                         'temps': udp_temp_errors})

                        # Now set all ADC delays and check for exceptions:
                        try:
                            i.set_adc_delays(compute_delays = 2, save_delays = False, check_sync_delays = 1, check_adc_delays = 20)
                        except (RuntimeError, IOError, OSError) as e:
                            print(f'set_adc_delays failed because of error {e!r}')
                            delay_fails += 1
                            delay_exception_tags.append({'Cycle': n,
                                                    'ib': i,
                                                    'exception': repr(e)})

                    for i in self.ca.ib:
                        # Close UDP communication with the iceboard to prevent errors:
                        print(f'Closing UDP connection to slot {i.slot}')
                        asyncio.run(i.close_async())

                print(f'Errors so far at iteration {n + 1}/{n_cycles}: ADC clk errors={clk_fails}, UDP errors={udp_fails}')
                print(f'init_exceptions={init_exception_fails}, delay_exceptions={delay_fails}, backplane errors={backplane_fails}')
                
                # Turn off crate and sleep before turning back on
                # to allow it to cool down:
                print('Turning OFF the crate')
                self.ps.set_output(state=False)
                print(f'Letting the crate cool down for {t_cycle} seconds before repeating the test')
                time.sleep(t_cycle)

            for adc_err in adc_err_tags:
                print(adc_err)

            for udp_err in udp_err_tags:
                print(udp_err)

            for mmi_err in mmi_err_tags:
                print(mmi_err)

            for err in init_exception_tags:
                print(err)

            for err in delay_exception_tags:
                print(err)

            for err in backplane_err_tags:
               print(err)

            # assert (max(init_exception_fails, clk_fails, udp_fails, delay_fails, backplane_fails) <= n_fails_accept), f'ADC errors on: {adc_err_tags}, UDP errors on: {udp_err_tags}, Init exceptions on: {init_exception_tags}, Delay exceptions on: {delay_exception_tags}, Backplane errors on: {backplane_err_tags}'
            assert (max(init_exception_fails, clk_fails, udp_fails, delay_fails, backplane_fails) <= n_fails_accept), 'Print check above '
            passed = True  # Yeh, we made it through

        finally:
            test_results.passed = passed
            xr.save_data(test_results)
            self.ps.set_output(state=False) # Turn off power supply



           
            
    def check_bp_errs(self):
        """
        Check for backplane errors. This has been made into its own function so it can be called
        in both the backplane error test as well as the power cycle test.

        """
        bp_errs = []
        shuffle_mode = self.cfg.f_engine_tests.global_settings.fpga_array_params.mode

        # Get corner turn engine status:
        info = asyncio.run(self.ca.get_corner_turn_engine_status_async(reset_stats=True)) # reset stats for each check
        

        #self.ca._print_shuffle_status(info, grid=True)

        

        # In shuffle256 mode, there should be no errors on any lanes on any subsystems on any motherboard.

        if shuffle_mode == 'shuffle256':
            for slot in np.arange(16)+1:
                subsystems = info[0]['slots'][slot]['subsystems']
                for ss in subsystems:
                    idx = list(subsystems[ss]['lanes'].keys())
                    for lane in idx:
                        lane_info = subsystems[ss]['lanes'][lane]
                        status = lane_info['status']
                        label = lane_info['label']
                        fields = lane_info['fields']
                        # print(slot, ss, label, fields, status)
                        if status == True:
                            # if status == True, append info (True --> error)
                            bp_errs.append([slot, ss, lane, status, label ,fields])

        # In shuffle128 mode, it is expected that there will be errors associated with the slots that are not present.
        # We need to iterate through the subsystems and determine what needs to be checked systematically, hence the
        # many specific 'if' statements.

        if shuffle_mode == 'shuffle128':
            # First, need to identify which Tx/Rx pairs are expected to fail due to having only 8 boards:
            slots = [i.slot-1 for i in self.ca.ib] # Subtract 1 to get 0-base
            all_slots = np.arange(16) # 0-base
            missing_slots = list(set(slots) ^ set(all_slots)) # Using XOR operator ^ to get slots uncommon to both lists
            for slot in slots:
                subsystems = info[0]['slots'][slot+1]['subsystems']
                cb2_ignore_lanes = [] # Needed to tell 'CB2 FRAME #' which lanes to check.
                for ss in subsystems:
                    idx = list(subsystems[ss]['lanes'].keys())
                    for lane in idx:
                        lane_info = subsystems[ss]['lanes'][lane]
                        status = lane_info['status']
                        label = lane_info['label']
                        fields = lane_info['fields']

                        if ss == 'BP PCB':
                            # Need to check the Tx/Rx pairs to see whether any are associated with a
                            # slot number that is not present (i.e. one without a board).
                            tx = fields['Tx']
                            rx = fields['Rx']
                            tx = [int(s) for s in re.findall(r'\b\d+\b', f'{tx}')] # convert pairs to lists of ints
                            rx = [int(s) for s in re.findall(r'\b\d+\b', f'{rx}')]
                            if len(list(set(tx) & set(slots))) == 2 and len(list(set(rx) & set(slots))) == 2:
                                # We use the '&' operator to get the slot numbers common to both tx or rx and slots.
                                # If the len of both lists is 2, then both the Tx and Rx pairs are from valid slots,
                                # and we can check their status:
                                if status == True:
                                    bp_errs.append([slot, ss, lane, status, label ,fields])

                        if ss == 'CB2 ALIGN':
                            # Here, we need to check whether there is an 'IGNORE' in the field. If not,
                            # check if there's an error. If yes, append the lane number to cb2_ignore_lanes
                            # so that we know which lanes to check when we go through the 'CB2 FRAME #' subsystem
                            if 'IGNORE' not in list(fields.keys()) and status == True:
                                bp_errs.append([slot, ss, lane, status, label ,fields])
                            if 'IGNORE' in list(fields.keys()):
                                cb2_ignore_lanes.append(lane)

                        if ss == 'CB2 FRAME #':
                            # Check the lanes with no 'IGNORE' in 'CB2 ALIGN'
                            if lane not in cb2_ignore_lanes and status == True:
                                bp_errs.append([slot, ss, lane, status, label ,fields])

                        # The other subsystems are 'BP QSFP', 'CB2 BIN SEL', 'CB2 ALIGN', 'CB2 FRAME #', and 'CB3 BIN SEL'.
                        # None of these should have any errors on any lane, so we can just check if all lanes are good:

                        else:
                            if status == True:
                                bp_errs.append([slot, ss, lane, status, label ,fields])

        return bp_errs


if __name__ == '__main__':
    """ Run the test in this file."""
    v = TestMenu(TEST_CONFIG_FILE).run()
    locals().update(v) # bring local variables from the test runner into the current namespace for easier debugging
