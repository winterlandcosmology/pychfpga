#!/usr/bin/env python

"""
Tests to be run in preparation for deployment of a full F-Engine, including
a fully-populated crate, switch, power supply, GPS, etc.
"""

# Standard packages
import asyncio
import time

# Pypi packages
import pytest
import numpy as np

# External private packages
from wtl.namespace import NameSpace
from wtl.pytest_xreport import xr, TestMenu
import pychfpga
from pychfpga import fpga_array
from pychfpga.fpga_firmware.chfpga import ct_health
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

    def power_off_safe(self):
        """Turn the crate off without masking the test failure that reached a finally block."""
        if not getattr(self, 'ps', None):
            return
        try:
            self.ps.set_output(state=False)
        except Exception as e:
            print(f'WARNING: could not turn the crate off ({e!r}) - do this manually')

    def power_cycle(self):
        """Force a full off/dwell/on cycle and wait for the ARM boards to boot.

        ``ps_off_dwell`` is the minimum time the output stays off. ``ps_t_sleep``
        is the boot wait; a shorter wait races the SD-card Linux boot and
        manufactures initialization failures.
        """
        if not self.ps:
            self.ps = self.open_ps()

        settings = self.cfg.f_engine_tests.global_settings
        off_dwell = settings.ps_off_dwell
        boot_wait = settings.ps_t_sleep

        ps_status = self.ps.status()['status']
        print(f'Power supply is {ps_status}')
        if ps_status == 'FAULT':
            print('Power supply is in FAULT: clearing protection')
            self.ps.clear()
        if ps_status != 'OFF':
            print('Turning off power supply...')
            self.ps.set_output(state=False)
            print(f"Power supply is now {self.ps.status()['status']}")

        print(f'Waiting {off_dwell} seconds with the output off before turning it back on')
        time.sleep(off_dwell)
        print('Turning on power supply...')
        self.ps.set_output(state=True)
        print(f'Waiting for {boot_wait} seconds to let the boards boot')
        time.sleep(boot_wait)

        status = self.ps.status()
        print(f'Power supply after boot: {status}')
        # labpy can report 'ON'. The in-repo N5764A driver reports 'OK' or 'ILIMIT'.
        if status['status'] not in ('OK', 'ON', 'ILIMIT'):
            raise RuntimeError(
                f"Power supply is {status['status']} after being told to turn on "
                f"- the crate has no power, so no board can be expected to answer.")

    def crate_init(self, reset_power = True):
        """
        Run fpga_array, initializing the crate. Recover ca object. Power is reset by default.
        """
        if reset_power:
            self.power_cycle()

        fpga_array_params = self.cfg.f_engine_tests.global_settings.fpga_array_params
        ca = asyncio.run(fpga_array.FPGAArray(**fpga_array_params).init())
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
        - iterate through output of await ca.print_shuffle_status_async(verbose = 1)
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
        QC000: Power supply connection test: the supply must answer every
        on/off command, and the crate must draw boot current each time it is on.

        ``on_time`` must be at least ``ps_t_sleep``, so every cycle finishes a
        cold boot instead of cutting power to boards that are still booting.
        """
        xr.header('Power Supply Connection Test')
        cfg = self.cfg.f_engine_tests.ps_connection_test
        n_ps_cycles = cfg.n_ps_cycles
        on_time = cfg.on_time
        off_time = cfg.off_time
        min_boot_current = cfg.min_boot_current
        settings = self.cfg.f_engine_tests.global_settings

        assert on_time >= settings.ps_t_sleep, (
            f'ps_connection_test.on_time ({on_time} s) is shorter than the crate boot wait '
            f'global_settings.ps_t_sleep ({settings.ps_t_sleep} s). Set on_time >= ps_t_sleep.')
        assert off_time >= settings.ps_off_dwell, (
            f'ps_connection_test.off_time ({off_time} s) is shorter than the required off-dwell '
            f'global_settings.ps_off_dwell ({settings.ps_off_dwell} s). Set off_time >= ps_off_dwell.')

        test_results = NameSpace()
        test_results.on_readings = []

        turn_ON_errs = []
        low_current_errs = []
        turn_OFF_errs = []

        passed = False

        try:
            # One connection for the whole test. Re-opening every cycle leaked an
            # instrument and reprogrammed the setpoints on each pass.
            self.ps = self.open_ps()
            print(f'Output is off; waiting {off_time} seconds before the first cycle')
            time.sleep(off_time)

            for n in range(n_ps_cycles):

                print('\n*******************************')
                print(f'Cycle {n+1}/{n_ps_cycles}', flush=True)
                print('*******************************')

                print('--------------------------------------------')
                print(f'Turning ON power supply')
                print(f'Wait {on_time} seconds for the crate to boot')
                self.ps.set_output(state=True)
                time.sleep(on_time)
                status = self.ps.status()
                test_results.on_readings.append(dict(cycle=n + 1, **status))
                print(f"Power supply is {status['status']}: {status.get('voltage')} V, {status.get('current')} A")
                # labpy may report 'ON'. The in-repo N5764A driver reports 'OK'.
                if status['status'] not in ('ON', 'OK'):
                    turn_ON_errs.append(f"cycle {n+1}: {status['status']}")
                elif status.get('current') is None:
                    low_current_errs.append(f"cycle {n+1}: supply status has no current reading")
                elif status['current'] < min_boot_current:
                    low_current_errs.append(
                        f"cycle {n+1}: {status['current']} A after {on_time} s "
                        f"(expected >= {min_boot_current} A from a booted crate)")

                print('--------------------------------------------')
                print(f'Turning OFF power supply')
                print(f'Wait {off_time} seconds')
                self.ps.set_output(state=False)
                time.sleep(off_time)
                ps_status = self.ps.status()['status']
                print(f'Power supply is {ps_status}')
                if ps_status != 'OFF':
                    turn_OFF_errs.append(f'cycle {n+1}: {ps_status}')
                print('--------------------------------------------')

            assert not (turn_ON_errs or low_current_errs or turn_OFF_errs), (
                f'Power supply / crate power-up problems over {n_ps_cycles} cycles:\n'
                f'  supply not OK after turn-on ({len(turn_ON_errs)}): {turn_ON_errs or "none"}\n'
                f'  crate drew too little current after boot ({len(low_current_errs)}): '
                f'{low_current_errs or "none"}\n'
                f'  supply not OFF after turn-off ({len(turn_OFF_errs)}): {turn_OFF_errs or "none"}')
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
            passed = True
        finally:
            test_results.passed = passed
            xr.save_data(test_results)
            #self.ps.set_output(state=False) # Turn off power supply
    

    def test_bperr(self, xr):
        """
        QC002: Backplane error test: the backplane links and both shuffle
        crossbars must be error-free on every board.

        Each pass is ``ct_health.measure_crate_health``: clear the monitors,
        let them re-accumulate, then read lock, CRC, aligner stickies, packet
        errors, and whether the frame counters advance. The old walk of
        ``get_corner_turn_engine_status_async(reset_stats=True)`` cleared the
        stickies immediately before sampling them, so it reported a clean
        crate almost unconditionally.
        """

        xr.header('Backplane Error Test')
        cfg = self.cfg.f_engine_tests.backplane_err
        n_checks = cfg.n_checks
        t_sleep = cfg.t_sleep
        test_results = NameSpace()

        passed = False
        bp_errs = []

        try:
            self.ca = self.crate_init()

            for n in range(n_checks):
                time.sleep(t_sleep)
                errs = self.check_bp_errs()
                print(f'Health pass {n + 1}/{n_checks}: {len(errs)} problem(s)')
                if errs:
                    bp_errs.append((n + 1, errs))

            assert not bp_errs, self._format_bp_errs(bp_errs, n_checks)
            passed = True

        finally:
            test_results.passed = passed
            xr.save_data(test_results)
            self.power_off_safe()
            
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
        max_sync_time_difference = cfg.max_sync_time_difference
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
                    # None would fall through to the FPGAArray default and then a
                    # hard-coded 20 ns, so the limit this test enforces would not
                    # be the one in the config.
                    asyncio.run(self.ca.sync_async(max_sync_time_difference=max_sync_time_difference))
                except Exception as e:
                    exception_fails += 1
                    exception_tags.append([f'Cycle {n}', repr(e)])
                else:
                    sync_counter += 1

            assert sync_counter == n_syncs, (
                f'{exception_fails} of {n_syncs} array syncs failed '
                f'(limit {max_sync_time_difference} ns):\n    '
                + '\n    '.join(f'{cycle}: {err}' for cycle, err in exception_tags))
            passed = True
        finally:
            test_results.passed = passed
            xr.save_data(test_results)
            self.power_off_safe()

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
        QC005: Sync delays test: the ADC SYNC alignment must be stable on every board.

        ``check_sync_delays`` sweeps the ADC pulse pattern ``trials`` times and
        returns how many trials a rising edge moved by more than half an ADC
        clock. Zero is a pass. This used to ask the operator to judge a printout.
        """

        xr.header('Sync Delays Test')
        cfg = self.cfg.f_engine_tests.sync_delay_test
        trials = cfg.trials
        verbose = cfg.verbose
        test_results = NameSpace()

        sync_delay_errors = []

        passed = False
        try:
            self.ca = self.crate_init()

            for i in self.ca.ib:
                print(f'=============================================================')
                print(f'Computing sync delays for {i}')
                try:
                    i.compute_sync_delays(set_sync_delays=True, verbose=verbose)
                except Exception as e:
                    print(f'compute_sync_delays failed on slot {i.slot}: {e!r}')
                    sync_delay_errors.append(
                        f'slot {i.slot} ({i!r}): could not compute SYNC delays: {e!r}')
                    continue

                jumps = i.check_sync_delays(trials=trials, verbose=verbose)
                print(f'Slot {i.slot}: {jumps} phase jump(s) over {trials} trials')
                if jumps:
                    sync_delay_errors.append(
                        f'slot {i.slot} ({i!r}): {jumps} phase jump(s) over {trials} trials '
                        f'- the SYNC alignment is marginal on this board')

            assert not sync_delay_errors, (
                f'SYNC delay problems on {len(sync_delay_errors)} board(s):\n    '
                + '\n    '.join(sync_delay_errors))
            passed = True

        finally:
            test_results.passed = passed
            xr.save_data(test_results)
            self.power_off_safe()

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
                    self.ca = asyncio.run(fpga_array.FPGAArray(**fpga_array_params).init())
                    
                    ic = self.ca.ic
                    from pychfpga.hardware import Crate
                    missing_slots = {
                        (ic.part_number, ic.serial, ic.crate_number): set(range(1, ic.NUMBER_OF_SLOTS + 1)) - set(ic.slot)
                        for ic in Crate.get_all_registered_instances()}
                    
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



           
            
    def test_crate_ramp(self, xr):
        """
        QC009: Crate level ramp test: every ADC channel on every board must
        deliver a clean ramp through the firmware ramp checker.

        This checks the ADC front end only. It does not exercise the
        backplane, the corner turn, or the 10 GbE output.
        """
        xr.header('Ramp Test')
        cfg = self.cfg.f_engine_tests.ramp_test
        n_checks = cfg.n_checks
        test_results = NameSpace()

        ramps_ok = []
        failed_boards = []
        errors = []

        passed = False
        try:
            self.ca = self.crate_init()

            for ice in self.ca.ib:
                ice.set_adc_delays(compute_delays=2, save_delays=False, check_sync_delays=True,
                                   check_adc_delays=20, verbose=0, retry=5)
                num_mismatch = ice.check_ramp_errors(trials=n_checks)
                ramps_ok.append(not num_mismatch)
                if num_mismatch:
                    failed_boards.append(ice)
                    errors.append(num_mismatch)

            for failed, error in zip(failed_boards, errors):
                print(f"There were {error} word errors on board {failed}")

            assert all(ramps_ok), (
                f'Ramp errors on {len(failed_boards)} of {len(ramps_ok)} board(s) '
                f'over {n_checks} trial(s):\n    '
                + '\n    '.join(f'slot {failed.slot} ({failed!r}): {error} word error(s)'
                                 for failed, error in zip(failed_boards, errors)))
            passed = True

        finally:
            test_results.passed = passed
            xr.save_data(test_results)
            self.power_off_safe()

    @staticmethod
    def _format_bp_errs(bp_errs, n_checks, max_shown=25):
        """Render accumulated health failures. Truncate a systemic fault so the message stays readable."""
        total = sum(len(errs) for _, errs in bp_errs)
        lines = [f'Backplane/corner-turn errors in {len(bp_errs)} of {n_checks} health '
                 f'pass(es), {total} problem(s) in total.']
        for check_number, errs in bp_errs:
            lines.append(f'  pass {check_number}: {len(errs)} problem(s)')
            for detail in errs[:max_shown]:
                lines.append(f'    {detail}')
            if len(errs) > max_shown:
                lines.append(f'    ... and {len(errs) - max_shown} more')
        return '\n'.join(lines)

    def check_bp_errs(self):
        """Backplane and corner-turn health, shared with the power-cycle test.

        ``measure_crate_health`` is the same measurement the shuffle256 firmware
        test uses. This suite has no running event loop, so the coroutine is
        driven with ``asyncio.run``, like the other async board calls here.
        """
        return asyncio.run(ct_health.measure_crate_health(self.ca.ib))


if __name__ == '__main__':
    """ Run the test in this file."""
    v = TestMenu(TEST_CONFIG_FILE).run()
    locals().update(v) # bring local variables from the test runner into the current namespace for easier debugging
