#!/usr/bin/env python

"""
Tests to be run in preparation for deployment of a full F-Engine, including
a fully-populated crate, switch, power supply, GPS, etc.
"""

# Standard packages
import asyncio
import time
import re
import socket

# Pypi packages
import pytest
import numpy as np

# External private packages
from wtl.namespace import NameSpace
from wtl.pytest_xreport import xr, TestMenu
import pychfpga
from pychfpga import fpga_array
from psucontrol.api import PSUController
import os

if os.environ.get("PYTEST_PLUGINS") is not None and os.environ.get("PYTEST_PLUGINS") != "":
    os.environ["PYTEST_PLUGINS"] += ",wtl.pytest_xreport"
else:
    os.environ["PYTEST_PLUGINS"] = "wtl.pytest_xreport"

TEST_CONFIG_FILE = './test_config.yaml'

class TestUtils:
    """ 
    Some utility methods common to all tests.
    """

    def connect_psu(self):

        # Load PSU IP from config file
        instr_params = self.cfg.instruments['psu'].copy()
        psu_ip = instr_params.pop('ip')
        psu_timeout = instr_params.pop('timeout')
        url = f'http://{psu_ip}/psu'

        # Verify connection by checking status of PSU
        try:
            self.psu = PSUController(hostname = psu_ip)
            status = self.psu.status()

        except Exception as e:
            print(f'Failed to connect to {psu_ip}: ', e)

        if self.psu:
            print('Successfully connected with PSU.')
            return self.psu

    def crate_init(self, reset_power = True):
        """
        Run fpga_array, initializing the crate. Recover ca object. Power is reset by default.
        """

        if reset_power:
             
             # Check if self.psualready exists (i.e. ps on from previous test)
             if not self.psu:
                 self.psu = self.connect_psu()

            # # FIXME
            #  # Check if power supply is on:
            #  # CHECK OUTPUT HERE
            #  psu_on = self.psu

            # # FIXME
            #  if ps_status == 'ON' or ps_status == 'OK':
            #     print(f'Power supply is {ps_status}')
            #     # Turning off power supply
            #     print(f'Turning off power supply...')
            #     self.psu.set_output(state=False) # Force power cycle if power supply is on
            #     ps_status = self.psu.status()['status']
            #     print(f'Power supply is now {ps_status}')
            #     time.sleep(5) # Give it a few seconds before turning back on
                
            # # FIXME
            #  else:
            #      print(f'Power supply is {ps_status}')

             # Turn power supply back on:
             print(f'Turning on power supply...')
             self.psu.control_power(state = True)

            # Wait for the boards to boot
             delay = self.cfg.f_engine_tests.global_settings.ps_t_sleep
             print(f'Waiting for {delay} seconds to let the boards boot')
             time.sleep(delay) # Sleep to let the crate boot

        # Program the crate
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

    5) Power cycle
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
        self.psu = None
        self.model = None # should be set by the test
        self.serial = None # should be set by the test
        
        yield  # pass control to the test and return

        # Turn off power supply
        if self.psu:
            self.psu.control_power(state = False) 

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
                
                # Connect to PSU
                self.psu = self.connect_psu()

                print('\n*******************************')
                print(f'Cycle {n+1}/{n_ps_cycles}')
                print('*******************************')
     
                print('--------------------------------------------')
                print(f'Turning ON power supply')
                print(f'Wait {on_time} seconds')

                # Turn on PSU
                self.psu.set_output(state=True)

                # Keep PSU on for on_time seconds
                time.sleep(on_time)

                # Check PSU status
                try:
                    ps_status = self.psu.status()
                except Exception as e:
                    print(e)
                    turn_ON_errs.append(f'cycle {n+1}: {e}')
                
                print('--------------------------------------------')
                print(f'Turning OFF power supply')
                print(f'Wait {off_time} seconds')

                # Turn off PSU
                self.psu.set_output(state=False)

                # Keep PSU off for off_time seconds
                time.sleep(off_time)

                # Check PSU status
                try:
                    ps_status = self.psu.status()
                except Exception as e:
                    turn_OFF_errs.append(f'cycle {n+1}: {e}')
                print('--------------------------------------------')

            assert (not turn_ON_errs and not turn_OFF_errs), f'Power supply connection errors: turning ON errors: {turn_ON_errs}, turning OFF errors: {turn_OFF_errs}'
            passed = True
           

        finally:
            test_results.passed = passed
            xr.save_data(test_results)
            # if this test passed, the ps should already be off; if it didn't, it can't be turned off remotely and has to be turned off manually
            # self.psu.set_output(state=False)    

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
            #self.psu.set_output(state=False) # Turn off power supply
    

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

                if bp_errs_cycle_n:
                    bp_errs.append([f'cycle {n}', bp_errs_cycle_n])

            assert not bp_errs, f'Backplane errors present on: {bp_errs}'
        
           # if not bp_errs:            
            passed = True

        finally:
            test_results.passed = passed
            xr.save_data(test_results)
            self.psu.set_output(state=False) # Turn off power supply
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
            self.psu.set_output(state=False) # Turn off power supply

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

                    

                # self.psu.set_output(state=False)
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
            # self.psu.set_output(state=False) # Turn off power supply


        

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
            self.psu.set_output(state=False) # Turn off power supply

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
                    if not self.psu:
                        self.psu= self.open_ps()
                    # Check status of power supply:
                    ps_status = self.psu.status()['status']
                    if ps_status == 'ON' or ps_status == 'OK':
                        print(f'Power supply is {ps_status}')
                        # Turning off power supply
                        print(f'Turning off power supply...')
                        self.psu.set_output(state=False) # Force power cycle if power supply is on
                        ps_status = self.psu.status()['status']
                        print(f'Power supply is now {ps_status}')
                        time.sleep(5) # Give it a few seconds before turning back on
                    else:
                        print(f'Power supply is {ps_status}')
                    # Turn power supply back on:
                    print(f'Turning on power supply...')
                    self.psu.set_output(state=True)
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
                self.psu.set_output(state=False)
                print(f'Letting the crate cool down for {t_cycle} seconds before repeating the test')
                time.sleep(t_cycle)


            assert not missing_slot_errors, f'The following slots failed to appear: \n{missing_slot_errors} \n Total of {error_total} errors'

        finally:


            test_results.passed = True
            xr.save_data(test_results)
            self.psu.set_output(state=False) # Turn off power supply



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
                self.psu.set_output(state=False)
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
            self.psu.set_output(state=False) # Turn off power supply



           
            
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
