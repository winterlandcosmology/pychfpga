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
            print(f'PSU status: {status}')

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

             # Check if power supply is on:
             psu_on = self.psu.status()['output_on']

             if psu_on:
                print(f'Power supply is ON')
                # Turning off power supply
                print(f'Turning off power supply...')
                self.psu.control_power(state = False) # Force power cycle if power supply is on
                psu_on = self.psu.status()['output_on']
                if not psu_on:
                    print(f'Power supply is now OFF')
                time.sleep(5) # Give it a few seconds before turning back on
                
             else:
                 print(f'Power supply is OFF')

             # Turn power supply back on:
             print(f'Turning on power supply...')
             self.psu.control_power(state = True)

            # Wait for the boards to boot
             delay = self.cfg.f_engine_tests.global_settings.ps_t_sleep
             print(f'Waiting for {delay} seconds to let the boards boot')
             time.sleep(delay) # Sleep to let the crate boot

        # Program the crate
        fpga_array_params = self.cfg.f_engine_tests.global_settings.fpga_array_params
        ca = asyncio.run(fpga_array.FPGAArray(**fpga_array_params).init())
        # ic = ca.ic[0]
        # # set crate model and serial number
        # self.model = ic.part_number
        # self.serial = ic.serial

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
        - iterate through output of await ca.print_shuffle_status_async(verbose = 1)
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

                print('\n*******************************')
                print(f'Cycle {n+1}/{n_ps_cycles}')
                print('*******************************')

                # Connect to PSU
                self.psu = self.connect_psu()
     
                print('--------------------------------------------')
                print(f'Turning ON power supply')
                print(f'Wait {on_time} seconds')

                # Turn on PSU
                self.psu.control_power(state = True)

                # Keep PSU on for on_time seconds
                time.sleep(on_time)

                # Check PSU status
                try:
                    ps_status = self.psu.status()
                    print(f'PSU status: {ps_status}')
                except Exception as e:
                    print(e)
                    turn_ON_errs.append(f'cycle {n+1}: {e}')
                
                print('--------------------------------------------')
                print(f'Turning OFF power supply')
                print(f'Wait {off_time} seconds')

                # Turn off PSU
                self.psu.control_power(state = False)

                # Keep PSU off for off_time seconds
                time.sleep(off_time)

                # Check PSU status
                try:
                    ps_status = self.psu.status()
                    print(f'PSU status: {ps_status}')
                except Exception as e:
                    turn_OFF_errs.append(f'cycle {n+1}: {e}')
                print('--------------------------------------------')

            assert (not turn_ON_errs and not turn_OFF_errs), f'Power supply connection errors: turning ON errors: {turn_ON_errs}, turning OFF errors: {turn_OFF_errs}'
            passed = True
           

        finally:
            test_results.passed = passed
            xr.save_data(test_results)
            # if this test passed, the ps should already be off; if it didn't, it can't be turned off remotely and has to be turned off manually
            # self.psu.control_power(state=False)    

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
            #self.psu.control_power(state=False) # Turn off power supply
    

    # def test_bperr(self, xr):
    #     """
    #     QC002: Backplane error test: check if there are errors in print_shuffle_status()

    #     Procedure:

    #         - Start the bperr test on the computer
    #         - Power up crate
    #         - After initializing crate, check backplane errors using
    #             await ca.print_shuffle_status_async()

    #     """

    #     xr.header('Backplane Error Test')
    #     cfg = self.cfg.f_engine_tests.backplane_err # not needed
    #     shuffle_mode = self.cfg.f_engine_tests.global_settings.fpga_array_params.mode
    #     n_checks = cfg.n_checks
    #     t_sleep = cfg.t_sleep
    #     test_results = NameSpace()

    #     passed = False
    #     bp_errs = []
    
    #     try:
    #         # Initialize the crate
    #         self.ca = self.crate_init()

    #         for n in range(n_checks):

    #             # Add 1s of sleep time between error gathering in case errors accumulate:
    #             time.sleep(t_sleep)

    #             # Get corner turn engine status:
    #             # info = asyncio.run(self.ca.get_corner_turn_engine_status_async(reset_stats=True)) # reset stats for each check

    #             bp_errs_cycle_n = self.check_bp_errs()

    #             if bp_errs_cycle_n:
    #                 bp_errs.append([f'cycle {n}', bp_errs_cycle_n])

    #         assert not bp_errs, f'Backplane errors present on: {bp_errs}'
        
    #        # if not bp_errs:            
    #         passed = True

    #     finally:
    #         test_results.passed = passed
    #         xr.save_data(test_results)
    #         self.psu.control_power(state=False) # Turn off power supply
    #         #assert not bp_errs, f'Backplane erros present on: {bp_errs}'
            
    def test_sync(self, xr):
        """
        QC003: Sync/IRIG-B test: stress test of crate sync using IRIG-B

        Procedure:

            - Start the sync test on the computer
            - Power up crate
            - After initializing crate, run self.ca.sync_async() in a loop, and
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
                    asyncio.run(self.ca.sync_async())
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
            self.psu.control_power(state = False) # Turn off power supply
  

    def test_adc_clocks(self, xr):
        """
        QC004: ADC clock test: ensure all ADCs get the correct clocks on every channel + power cycling between checks

        Procedure:

          - Start the ADC clock test on the computer
          - Power up crate
            - Iterate through all clocks on all mezzanines on all motherboards, check that they
            are correct within some tolerance set in the config.
            - Repeat for desired number of cycles
          - Power off crate

        """

        xr.header('ADC Clocks Test')
        cfg = self.cfg.f_engine_tests.clock_test
        test_results = NameSpace()

        # integration_period = cfg.integration_period
        n_clock_checks = cfg.n_clock_checks
        percent_accept = cfg.percent_accept
        n_fails_accept = int(percent_accept*n_clock_checks) # Define number of acceptable fails
        check_delay = cfg.check_delay

        # The FreqCtr counts rising clock edges; the most it could miss over a given integration period
        # is 1 edge. Also, FreqCtr measures at 1/2 the rate of the 400 MHz clock coming from the ADC,
        # so it could miss, at most, 2 rising edges.
        # expected_diffs = {-2/integration_period, 0.0, 2/integration_period}
        # failed_clocks = []
        # measured_diffs = []

        # clk_fails = 0
        # adc_err_tags = []
        passed = False

        try:
            # Initialize the crate:
            self.ca = self.crate_init()

            # Iterate through each motherboard. Form a set of unique values of the
            # difference between the measured clock and the expected 200 MHz. To pass,
            # there should only be two values in the set: 0 and 200 MHz/count_time (which
            # is the maximum error, resulting from a missed rising edge) --> is this correct?

            for n in range(n_clock_checks):

                print(f'****************************')
                print(f'ADC clock test iteration {n + 1}/{n_clock_checks}')
                print(f'****************************')

                for i in self.ca.ib:
                    # for clock in range(16):
                    #     print(f'{i}, ADC_CLK{clock}')
                    #     diffs = set(i.FreqCtr.read_frequency(f'ADC_CLK{clock}', integration_period) - 200e6 for _ in range(n_clock_checks))
                    #     # If diffs is not a subset of expected_diffs, i.e. it contains an unexpected value, then append to failed_clocks
                    #     if not diffs.issubset(expected_diffs):
                    #         failed_clocks.append([f'{i}', f'ADC_CLK{clock}', diffs]) 

                    print(f'Checking ADC clocks on: {i}')
                    adc_error = i.check_adc_frequencies('after boot')
                    if adc_error:
                        print(f'ADC clock error on iteration {n}, board {i}, slot {i.slot}')
                        print(i.adc_clk_err_msgs)

                print(f'Delaying for {check_delay} seconds before next check')
                time.sleep(check_delay)
                        
            clk_fails = i.adc_clk_err_ctr
            adc_err_tags = i.adc_clk_err_msgs

            print(f'Total clock fails = {clk_fails}')
            print(f'Recovered clock errors = {adc_err_tags}')

            # assert not failed_clocks, f'ADC clock errors present on: {failed_clocks}'
            # assert not error, f'ADC clock errors present on: {failed_clocks}'
            assert (clk_fails <= n_fails_accept)
            passed = True

        finally:
            test_results.passed = passed
            xr.save_data(test_results)
            self.psu.control_power(state=False) # Turn off power supply

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
                        self.psu.control_power(state=False) # Force power cycle if power supply is on
                        ps_status = self.psu.status()['status']
                        print(f'Power supply is now {ps_status}')
                        time.sleep(5) # Give it a few seconds before turning back on
                    else:
                        print(f'Power supply is {ps_status}')
                    # Turn power supply back on:
                    print(f'Turning on power supply...')
                    self.psu.control_power(state=True)
                    delay = 60
                    print(f'Waiting for {delay} seconds to let the boards boot')
                    time.sleep(delay) # Sleep to let the crate boot

                    # self.ca = self.crate_init()
                    fpga_array_params = cfg.fpga_array_params
                    self.ca = asyncio.run(fpga_array.FPGAArray(**fpga_array_params).init())
                    
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
                self.psu.control_power(state=False)
                print(f'Letting the crate cool down for {t_cycle} seconds before repeating the test')
                time.sleep(t_cycle)


            assert not missing_slot_errors, f'The following slots failed to appear: \n{missing_slot_errors} \n Total of {error_total} errors'

        finally:


            test_results.passed = True
            xr.save_data(test_results)
            self.psu.control_power(state=False) # Turn off power supply



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

        test_results = NameSpace()

        # A fail is defined as a cycle which has one or more errors.

        init_fails = 0
        sync_fails = 0
        clk_fails = 0
        udp_fails = 0
        # mmi_fails = 0
        other_fails = 0

        init_exception_tags = []
        sync_error_tags = []
        adc_err_tags = []
        udp_err_tags = []
        # mmi_err_tags = []
        other_err_tags = []

        passed = False

        try:

            for n in range(n_cycles):
                print(50*'=')
                print(f'Power-cycling test iteration {n + 1}/{n_cycles}')
                print(50*'=')
                
                # Initialize the crate:
                try:
                    self.ca = self.crate_init()
             
                except (RuntimeError, IOError, OSError) as e:
                    
                    print(f'Failed initializing the array because of error {e!r}')
                    
                    init_fails += 1
                    init_exception_tags.append({'Cycle': n, 'exception': repr(e)})
                    
                else:

                    print('Got an IceBoard array')

                    # Synchronize crate in its own try/except
                    # clause to catch any sync errors
                    try:
                        asyncio.run(self.ca.sync_async())
                    except Exception as e:
                        sync_error_tags.append({'Cycle': n,
                                        'ib': i,
                                        'sync_err_msgs': e})

                    try:

                        # Buffer counters for storing temporary errors for each cycle
                        adc_errors_cycle_n = 0
                        udp_errors_cycle_n = 0

                        for i in self.ca.ib:
    
                            # Check counters and error lists:

                            print(f'Checking ADC clocks on: {i}')
                            adc_error = i.check_adc_frequencies('after boot')
                            if adc_error:
                                print(adc_error)
                                adc_errors_cycle_n += 1
                                adc_err_tags.append({'Cycle': n,
                                        'ib': i,
                                        'n_clk_errs': i.adc_clk_err_ctr,
                                        'clk_err_msgs': i.adc_clk_err_msgs})

                            print(f'Checking UDP errors on: {i}')
                            asyncio.run(i.get_fpga_udp_metrics_async())
                            udp_temp_errors = i.get_temperatures()
                            if i.udp_err_ctr != 0:
                                print(i.udp_err_msgs)
                                udp_errors_cycle_n += 1
                                udp_err_tags.append({'Cycle': n,
                                            'ib': i,
                                            'n_udp_errs': i.udp_err_ctr,
                                            'udp_err_msgs': i.udp_err_msgs,
                                            'temps': udp_temp_errors})

                            print(f'Slot {i.slot} got {adc_errors_cycle_n} ADC clock errors, {udp_errors_cycle_n} UDP communication errors')
                            print('')

                        if adc_errors_cycle_n != 0:
                            clk_fails += 1
                            
                        if udp_errors_cycle_n != 0:
                            udp_fails += 1

                        for i in self.ca.ib:
                            # Close UDP communication with the iceboard to prevent errors:
                            print(f'Closing UDP connection to slot {i.slot}')
                            asyncio.run(i.close_async())

                    except Exception as e:
                        print(f'Got other error {e!r} during cycle {n}')
                        other_fails += 1
                        other_err_tags.append({'Cycle': n,
                                            'exception': repr(e)})


                print(50*'-')
                print(f'Total accumulated errors so far at iteration {n + 1}/{n_cycles}')
                print(f'Sync errors = {sync_fails}')
                print(f'ADC clock errors = {clk_fails}')
                print(f'UDP errors = {udp_fails}')
                print(f'init_exceptions = {init_fails}')
                print(f'Other errors = {other_fails}')
                print(50*'-')
                
                # Turn off crate and sleep before turning back on
                # to allow it to cool down:
                print('Turning OFF the crate')
                self.psu.control_power(state = False)
                print(f'Letting the crate cool down for {t_cycle} seconds before repeating the test')
                time.sleep(t_cycle)

            for err in init_exception_tags:
                print(err)

            for sync_err in sync_error_tags:
                print(sync_err)

            for adc_err in adc_err_tags:
                print(adc_err)

            for udp_err in udp_err_tags:
                print(udp_err)

            for other_err in other_err_tags:
                print(other_err)

            # assert (max(init_fails, clk_fails, udp_fails, delay_fails, backplane_fails) <= n_fails_accept), f'ADC errors on: {adc_err_tags}, UDP errors on: {udp_err_tags}, Init exceptions on: {init_exception_tags}, Delay exceptions on: {delay_exception_tags}, Backplane errors on: {backplane_err_tags}'
            assert (max(init_fails, sync_fails, clk_fails, udp_fails, other_fails) <= n_fails_accept), 'Print check above '
            passed = True

        finally:
            test_results.passed = passed
            xr.save_data(test_results)
            self.psu.control_power(state = False) # Turn off power supply


if __name__ == '__main__':
    """ Run the test in this file."""
    v = TestMenu(TEST_CONFIG_FILE).run()
    locals().update(v) # bring local variables from the test runner into the current namespace for easier debugging
