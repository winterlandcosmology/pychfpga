#!/usr/bin/env python

"""
Tests to be run in preparation for deployment of a full F-Engine, including
a fully-populated crate, switch, power supply, GPS, etc.
"""

# Standard packages
import asyncio
import time
import re
import signal
import socket
from contextlib import contextmanager

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


class PSUError(RuntimeError):
    """Raised for any PSU-related failure during crate power control.

    Subclasses ``RuntimeError`` so that existing ``except RuntimeError``
    handlers still catch it, while allowing callers to distinguish PSU
    failures from genuine FPGA programming / array-init errors via
    ``isinstance(e, PSUError)``.
    """


class IterationTimeout(RuntimeError):
    """Raised when a single test iteration exceeds its allotted wall-clock time.

    A frozen iteration is assumed to be caused by a hardware problem (the
    crate/board stops responding and a blocking call never returns). Rather
    than diagnosing it here, the watchdog aborts the stuck iteration so the
    test can power-cycle and continue with the next one.

    Subclasses ``RuntimeError`` so it is caught by the existing broad
    ``except Exception`` handlers, while remaining distinguishable via
    ``isinstance(e, IterationTimeout)``.
    """


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
            self.psu = PSUController(hostname = psu_ip, timeout = psu_timeout)
            status = self.psu.get_status()
            print(f'PSU status: {status}')

        except Exception as e:
            print(f'Failed to connect to {psu_ip}: ', e)
            self.psu = None
            raise PSUError(f'Failed to connect to PSU at {psu_ip}: {e!r}') from e

        print('Successfully connected with PSU.')
        return self.psu

    def _psu_call(self, fn, *args, retries=3, backoff=5, **kwargs):
        """Call a PSU control function with retries and backoff.

        Each PSU call (get_status/set_power) opens a fresh HTTP connection to
        the PSU's small embedded web server. Under rapid power cycling this can
        transiently exhaust the server's limited sockets, producing ReadTimeout
        or "No route to host" errors. Retrying with a delay between attempts
        gives the server time to recover before we declare a hard failure.

        Raises:
            PSUError: if all attempts fail.
        """
        last_exc = None
        for attempt in range(1, retries + 1):
            try:
                return fn(*args, **kwargs)
            except IterationTimeout:
                # The iteration watchdog fired while we were talking to the
                # PSU. Let it through: retrying here would keep the test alive
                # past the deadline it is supposed to enforce.
                raise
            except Exception as e:
                last_exc = e
                print(f'PSU call {getattr(fn, "__name__", fn)!s} failed '
                      f'(attempt {attempt}/{retries}): {e!r}')
                if attempt < retries:
                    time.sleep(backoff)
        raise PSUError(f'PSU call {getattr(fn, "__name__", fn)!s} failed after '
                       f'{retries} attempts: {last_exc!r}') from last_exc

    @contextmanager
    def iteration_watchdog(self, timeout, label='Operation'):
        """Abort the enclosed block if it takes longer than `timeout` seconds.

        This guards against hardware problems that freeze an iteration: when a
        board stops responding, a blocking call (e.g. `asyncio.run(...)` on an
        FPGA transaction) can hang forever and stall the whole test run.
        Simply measuring elapsed time after the fact cannot help, because
        control never comes back. Instead we arm a SIGALRM timer that fires
        *while* the blocking call is in progress and raises `IterationTimeout`
        from the signal handler, which unwinds the stuck call so the caller can
        move on to the next iteration.

        Args:
            timeout: Watchdog period in seconds. Values <= 0 (or None) disable
                the watchdog, preserving the previous behaviour of waiting
                indefinitely.
            label: Optional text included in the exception message to identify
                which iteration timed out.

        Yields:
            None. Control returns to the caller with the watchdog armed.

        Raises:
            IterationTimeout: if the block is still running when the timer
                expires.

        Notes:
            SIGALRM is Unix-only and can only be delivered to the main thread,
            which is how these QC tests are run (a CLI menu / plain pytest
            session). If the watchdog cannot be armed (non-main thread or a
            platform without SIGALRM) it degrades gracefully to a no-op and
            prints a warning rather than breaking the test.
            Resolution is 1 second, since `signal.alarm()` takes an integer.
        """

        timeout = int(timeout) if timeout else 0

        if timeout <= 0 or not hasattr(signal, 'SIGALRM'):
            # Watchdog disabled (or unsupported platform): run unguarded.
            yield
            return

        def _on_timeout(signum, frame):
            raise IterationTimeout(
                f'{label} exceeded its {timeout} s time limit and was aborted '
                f'(assuming a hardware problem froze the test)')

        try:
            previous_handler = signal.signal(signal.SIGALRM, _on_timeout)
        except ValueError as e:
            # signal.signal() only works in the main thread.
            print(f'Warning: could not arm the iteration watchdog ({e!r}); '
                  f'running without a time limit')
            yield
            return

        signal.alarm(timeout)
        try:
            yield
        finally:
            # Always disarm the timer and restore the previous handler, so a
            # pending alarm can never fire during the next iteration or during
            # test teardown.
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous_handler)

    def crate_init(self, reset_power = True):
        """
        Run fpga_array, initializing the crate. Recover ca object. Power is reset by default.
        """

        if reset_power:
             
             # Check if self.psualready exists (i.e. ps on from previous test)
             if not self.psu:
                 self.psu = self.connect_psu()

             # All PSU control below is wrapped so that any failure is re-raised
             # as a RuntimeError. Callers (e.g. test_power_cycle) can then
             # distinguish PSU-related failures (RuntimeError) from genuine FPGA
             # programming / array-init errors.
             try:
                 # Check if power supply is on:
                 psu_on = self._psu_call(self.psu.get_status)['output_on']

                 if psu_on:
                    print(f'Power supply is ON')
                    # Turning off power supply
                    print(f'Turning off power supply...')
                    self._psu_call(self.psu.set_power, False) # Force power cycle if power supply is on
                    print(f'Power supply turned OFF')
                    time.sleep(5) # Give it a few seconds before turning back on

                 else:
                     print(f'Power supply is OFF')

                 # Turn power supply back on:
                 print(f'Turning on power supply...')
                 self._psu_call(self.psu.set_power, True)
             except PSUError:
                 raise
             except Exception as e:
                 raise PSUError(f'PSU control failed during crate power cycle: {e!r}') from e

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
    
    0) Test connection to power supply

    1) Test board initialization:
        - all 16 boards found in hwm
        - all FPGAs programmed
        - crate must sync

    2) Test IRIG-B synchronization
        - initialize crate
        - config-defined number of sync tries

    3) Test ADC clocks
        - initialize crate
        - iterate through all motherboards and mezzanines, check that
          clocks are 200 MHz +/- tolerance (set by integration time)

    4) Test all above tests with many power cycles
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

        # Turn off power supply. Guard against a PSU that has become
        # unreachable so a teardown failure cannot abort the whole test
        # harness (an unguarded exception in fixture teardown propagates).
        if self.psu:
            try:
                self._psu_call(self.psu.set_power, False)
            except Exception as e:
                print(f'Warning: failed to turn off PSU during teardown: {e!r}')

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

                if n == 0:
                    # On the first iteration, check to make sure the PSU is off. If it is on, turn it off.

                    # Connect to PSU
                    self.psu = self.connect_psu()

                    psu_on = self.psu.get_status()['output_on']

                    if psu_on:
                        print(f'Power supply is ON')
                        # Turning off power supply
                        print(f'Turning off power supply...')
                        self.psu.set_power(False) # Force power cycle if power supply is on
                        psu_on = self.psu.get_status()['output_on']
                        if not psu_on:
                            print(f'Power supply is now OFF')
                    else:
                        print(f'Power supply is OFF')

                print('\n*******************************')
                print(f'Cycle {n+1}/{n_ps_cycles}')
                print('*******************************')

                # Connect to PSU
                self.psu = self.connect_psu()
     
                print('--------------------------------------------')
                print(f'Turning ON power supply')
                print(f'Wait {on_time} seconds')

                # Turn on PSU
                self.psu.set_power(True)

                # Keep PSU on for on_time seconds
                time.sleep(on_time)

                # Check PSU status
                try:
                    ps_status = self.psu.get_status()
                    print(f'PSU status: {ps_status}')
                    psu_on = self.psu.get_status()['output_on']
                    if not psu_on:
                        turn_ON_errs.append(f'cycle {n+1}: PSU failed to turn on')
                except Exception as e:
                    print(e)
                    turn_ON_errs.append(f'cycle {n+1}: {e}')
                
                print('--------------------------------------------')
                print(f'Turning OFF power supply')
                print(f'Wait {off_time} seconds')

                # Turn off PSU
                self.psu.set_power(False)

                # Keep PSU off for off_time seconds
                time.sleep(off_time)

                # Check PSU status
                try:
                    ps_status = self.psu.get_status()
                    print(f'PSU status: {ps_status}')
                    psu_on = self.psu.get_status()['output_on']
                    if psu_on:
                        turn_OFF_errs.append(f'cycle {n+1}: PSU failed to turn off')
                except Exception as e:
                    turn_OFF_errs.append(f'cycle {n+1}: {e}')
                print('--------------------------------------------')

            assert (not turn_ON_errs and not turn_OFF_errs), f'Power supply connection errors: turning ON errors: {turn_ON_errs}, turning OFF errors: {turn_OFF_errs}'
            passed = True
           

        finally:
            test_results.passed = passed
            xr.save_data(test_results)

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
            self.ca = self.crate_init()
            # If we made it to this point, FPGAs programmed, all boards present in hwm
            # and crate has synced (see test_config.yaml - parameters passed to FPGAArray
            # force all these to be present to return self.ca
            assert self.ca.ib, f'ib object is {self.ca.ib}'
            passed = True
        finally:
            test_results.passed = passed
            xr.save_data(test_results)
            self.psu.set_power(False) # Turn off power supply
    
            
    def test_sync(self, xr):
        """
        QC002: Sync/IRIG-B test: stress test of crate sync using IRIG-B

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
            if self.psu:
                self.psu.set_power(False) # Turn off power supply
  

    def test_adc_clocks(self, xr):
        """
        QC003: ADC clock test: ensure all ADCs get the correct clocks on every channel + power cycling between checks

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

            assert (clk_fails <= n_fails_accept)
            passed = True

        finally:
            test_results.passed = passed
            xr.save_data(test_results)
            if self.psu:
                self.psu.set_power(False) # Turn off power supply


    def test_power_cycle(self, xr):
        """
        QC004: Power cycle test: cycle crate initializations, logging ADC clock and UDP errors.

        Procedure:

          - Start the power cycle test on the computer
          - Power up crate
          - Cycle through crate initialization n_cycles times, count
            some relevant errors
          - Each iteration is guarded by a watchdog (`iter_timeout` in the
            config): a cycle that takes too long is assumed to be frozen by a
            hardware problem, is aborted, and the test moves on to the next one

        """

        xr.header('Power Cycle Test')
        cfg = self.cfg.f_engine_tests.p_cycle_test
        n_cycles = cfg.n_cycles
        percent_accept = cfg.percent_accept
        n_fails_accept = int(percent_accept*n_cycles) # Define number of acceptable fails
        t_cycle = cfg.t_cycle

        # Maximum wall-clock time allowed for the work of a single iteration
        # (crate init + sync + per-board checks). This excludes the t_cycle
        # cool-down and the PSU power-off, which are done outside the watchdog.
        # A missing or <= 0 value disables the watchdog.
        iter_timeout = cfg.get('iter_timeout', 0)

        test_results = NameSpace()

        # A fail is defined as a cycle which has one or more errors.

        init_fails = 0
        sync_fails = 0
        clk_fails = 0
        udp_fails = 0
        # mmi_fails = 0
        other_fails = 0
        psu_fails = 0
        timeout_fails = 0

        # Number of consecutive cycles that failed on PSU communication. If the
        # PSU becomes permanently unreachable, nothing can recover, so we abort
        # the test early rather than grinding through every remaining cycle.
        consecutive_psu_fails = 0
        MAX_CONSECUTIVE_PSU_FAILS = 5

        init_exception_tags = []
        sync_error_tags = []
        adc_err_tags = []
        udp_err_tags = []
        # mmi_err_tags = []
        other_err_tags = []
        psu_err_tags = []
        timeout_err_tags = []

        # Duration (in seconds) of the guarded part of every iteration, so slow
        # cycles can be spotted in the report even when they do not time out.
        iter_durations = []

        passed = False

        try:

            for n in range(n_cycles):
                print(50*'=')
                print(f'Power-cycling test iteration {n + 1}/{n_cycles}')
                print(50*'=')

                # Track whether this cycle hit a PSU communication failure so we
                # can detect a permanently-unreachable PSU and abort early.
                psu_failed_this_cycle = False

                if iter_timeout > 0:
                    print(f'Iteration time limit: {iter_timeout} s')

                t_start = time.monotonic()

                # Everything below (crate init, sync and the per-board checks)
                # talks to the hardware, so any of it can hang forever if a
                # board stops responding. The watchdog bounds the whole block
                # and raises IterationTimeout, caught just below, so a frozen
                # cycle is abandoned instead of stalling the entire test run.
                try:
                    with self.iteration_watchdog(
                            iter_timeout,
                            label=f'Power-cycle iteration {n + 1}/{n_cycles}'):

                        # Initialize the crate:
                        try:
                            self.ca = self.crate_init()

                        except IterationTimeout:
                            # Watchdog fired: let it reach the handler below
                            # instead of being logged as an init failure.
                            raise

                        except Exception as e:
                            # PSU-related failures are raised as PSUError (see crate_init /
                            # connect_psu) and are counted separately. Anything else is a
                            # genuine FPGA programming / array-init failure.
                            if isinstance(e, PSUError):
                                print(f'PSU error while power cycling the crate: {e!r}')
                                psu_fails += 1
                                psu_failed_this_cycle = True
                                psu_err_tags.append({'Cycle': n, 'exception': repr(e)})
                            else:
                                print(f'Failed initializing the array because of error {e!r}')
                                init_fails += 1
                                init_exception_tags.append({'Cycle': n, 'exception': repr(e)})

                        else:

                            print('Got an FPGA array')

                            # Synchronize crate in its own try/except
                            # clause to catch any sync errors
                            try:
                                asyncio.run(self.ca.sync_async())
                            except IterationTimeout:
                                raise
                            except Exception as e:
                                sync_fails += 1
                                sync_error_tags.append({'Cycle': n,
                                                'sync_err_msgs': repr(e)})

                            try:

                                # Buffer counters for storing temporary errors for each cycle
                                adc_errors_cycle_n = 0
                                udp_errors_cycle_n = 0

                                for i in self.ca.ib:

                                    # Reset error counters for each cycle
                                    i.adc_clk_err_ctr = 0
                                    i.adc_clk_err_msgs = []
                                    i.udp_err_ctr = 0
                                    i.udp_err_msgs = []
    
                                    # Check counters and error lists
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

                            except IterationTimeout:
                                raise

                            except Exception as e:
                                print(f'Got other error {e!r} during cycle {n}')
                                other_fails += 1
                                other_err_tags.append({'Cycle': n,
                                                    'exception': repr(e)})

                            finally:
                                # Close UDP communication with each iceboard to
                                # prevent errors. This is cleanup (not a sub-test),
                                # so it runs regardless of check outcome and each
                                # board is closed independently so one failure does
                                # not skip the rest or count as a test error.
                                for i in self.ca.ib:
                                    try:
                                        print(f'Closing UDP connection to slot {i.slot}')
                                        asyncio.run(i.close_async())
                                    except IterationTimeout:
                                        raise
                                    except Exception as e:
                                        print(f'Failed to close UDP connection to slot {i.slot}: {e!r}')

                except IterationTimeout as e:
                    # The iteration froze, most likely because of a hardware
                    # problem. We deliberately do not diagnose it here: record
                    # it, then let the loop power-cycle the crate and carry on
                    # with the next iteration.
                    elapsed = time.monotonic() - t_start
                    print(f'TIMEOUT: {e}')
                    print(f'Abandoning iteration {n + 1}/{n_cycles} after '
                          f'{elapsed:.1f} s and moving on to the next one')
                    timeout_fails += 1
                    timeout_err_tags.append({'Cycle': n,
                                             'elapsed_s': round(elapsed, 1),
                                             'exception': repr(e)})

                    # No board cleanup here on purpose: the board is presumed
                    # wedged, so closing it could hang again, and the PSU
                    # power-off at the end of this iteration tears down the
                    # crate (and therefore those connections) anyway.

                iter_duration = time.monotonic() - t_start
                iter_durations.append(iter_duration)

                print(50*'-')
                print(f'Total accumulated errors so far at iteration {n + 1}/{n_cycles}')
                print(f'Iteration duration = {iter_duration:.1f} s '
                      f'(mean {np.mean(iter_durations):.1f} s, '
                      f'max {max(iter_durations):.1f} s)')
                print(f'Sync errors = {sync_fails}')
                print(f'ADC clock errors = {clk_fails}')
                print(f'UDP errors = {udp_fails}')
                print(f'init_exceptions = {init_fails}')
                print(f'Other errors = {other_fails}')
                print(f'PSU errors = {psu_fails}')
                print(f'Timed-out (frozen) iterations = {timeout_fails}')
                print(50*'-')
                
                # Turn off crate and sleep before turning back on
                # to allow it to cool down:
                print('Turning OFF the crate')
                try:
                    if not self.psu:
                        raise PSUError('PSU is not connected')
                    self._psu_call(self.psu.set_power, False)
                except Exception as e:
                    print(f'Failed to turn OFF the crate during cycle {n}: {e!r}')
                    psu_fails += 1
                    psu_failed_this_cycle = True
                    psu_err_tags.append({'Cycle': n, 'exception': repr(e)})

                # Track consecutive PSU failures and abort early if the PSU
                # appears permanently unreachable (no chance of recovery).
                if psu_failed_this_cycle:
                    consecutive_psu_fails += 1
                    if consecutive_psu_fails >= MAX_CONSECUTIVE_PSU_FAILS:
                        print(f'Aborting power-cycle test after {consecutive_psu_fails} '
                              f'consecutive PSU failures: the PSU appears unreachable '
                              f'and cannot recover.')
                        break
                else:
                    consecutive_psu_fails = 0

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

            for psu_err in psu_err_tags:
                print(psu_err)

            for timeout_err in timeout_err_tags:
                print(timeout_err)

            if iter_durations:
                print(f'Iteration durations (s): mean {np.mean(iter_durations):.1f}, '
                      f'min {min(iter_durations):.1f}, max {max(iter_durations):.1f}, '
                      f'over {len(iter_durations)} iteration(s)')

            # assert (max(init_fails, clk_fails, udp_fails, delay_fails, backplane_fails) <= n_fails_accept), f'ADC errors on: {adc_err_tags}, UDP errors on: {udp_err_tags}, Init exceptions on: {init_exception_tags}, Delay exceptions on: {delay_exception_tags}, Backplane errors on: {backplane_err_tags}'
            assert (max(init_fails, sync_fails, clk_fails, udp_fails, other_fails, psu_fails, timeout_fails) <= n_fails_accept), 'Print check above '
            passed = True

        finally:
            test_results.passed = passed
            test_results.timeout_fails = timeout_fails
            test_results.timeout_err_tags = timeout_err_tags
            test_results.iter_durations = iter_durations
            xr.save_data(test_results)
            if self.psu:
                try:
                    self._psu_call(self.psu.set_power, False) # Turn off power supply
                except Exception as e:
                    print(f'Warning: failed to turn off PSU during teardown: {e!r}')


if __name__ == '__main__':
    """ Run the test in this file."""
    v = TestMenu(TEST_CONFIG_FILE).run()
    locals().update(v) # bring local variables from the test runner into the current namespace for easier debugging
