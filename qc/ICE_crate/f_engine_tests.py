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
from pychfpga.fpga_firmware.chfpga import ct_health
from pychfpga.hardware.Agilent_N5764A import AgilentN5764A
import os

if os.environ.get("PYTEST_PLUGINS") is not None and os.environ.get("PYTEST_PLUGINS") != "":
    os.environ["PYTEST_PLUGINS"] += ",wtl.pytest_xreport"
else:
    os.environ["PYTEST_PLUGINS"] = "wtl.pytest_xreport"

TEST_CONFIG_FILE = './test_config.yaml'

class CratePowerSupply:
    """ Bench power supply for the crate, driven by the in-repo N5764A driver.

    This adapter deliberately exposes the same method names and argument names
    that this test suite has always called on its supply object
    (``set_output(state=)``, ``status()``, ``clear()``, ``set_voltage(voltage=)``,
    ``set_current_limit(current=, ocp=)``), so that swapping the underlying
    driver did not require touching any test.

    Two objects sit behind it, both from ``pychfpga/hardware/Agilent_N5764A``:

    - ``self._drv`` (:class:`AgilentN5764A`) provides the output on/off and
      status lifecycle, and the lock that guards state changes;
    - ``self._drv.ps`` (``agilent_N5700``) provides the setpoint programming,
      which the :class:`AgilentN5764A` wrapper does not re-export.

    ``status()['status']`` is the vocabulary of ``agilent_N5700.get_state()``:
    ``'OK'`` (on and regulating), ``'OFF'``, ``'ILIMIT'`` (on, but in current
    limit) or ``'FAULT'``. Note that it never returns ``'ON'``.
    """

    def __init__(self, hostname, port=5025, timeout=0.5):
        self._drv = AgilentN5764A(hostname=hostname, port=port, timeout=timeout)
        self._drv.open()
        self._drv.unlock()  # the driver starts locked and refuses state changes

    def __repr__(self):
        return repr(self._drv)

    def set_output(self, state):
        self._drv.power_enable(bool(state))

    def status(self):
        return self._drv.status()

    def clear(self):
        return self._drv.ps.clear()

    def set_voltage(self, voltage=None):
        return self._drv.ps.set_voltage(voltage=voltage)

    def set_current_limit(self, current=None, ocp=None):
        return self._drv.ps.set_current_limit(current=current, ocp=ocp)


# ---------------------------------------------------------------------------
# Legacy power-supply access, retained for reference - DO NOT re-enable as-is.
#
# Until 2026-09 this suite reached the supply through McGill's out-of-tree
# ``labpy`` package, configured from ``test_config.yaml`` as:
#
#     instruments:
#       ps: {labpy_object: 'agilent_N5700', adapter: 'TCPIP::10.10.10.234'}
#
#     def open_instrument(self, name):
#         instr_params = self.cfg.instruments[name].copy()
#         class_name = instr_params.pop('labpy_object')
#         return labpy.open_instrument(class_name, **instr_params)
#
# and ``open_ps()`` then called ``open_instrument(name)`` for the object now
# built by :class:`CratePowerSupply`.
#
# It was replaced because the suite stopped importing altogether: ``labpy``
# imports ``pymeasure`` 0.10.0, which does ``from pkg_resources import
# parse_version``, and setuptools >= 81 no longer ships ``pkg_resources``.
# Collection therefore failed at ``import labpy`` before any test could run.
# ``labpy`` is not a declared dependency of pychfpga, so nothing pinned it.
#
# The labpy route can be restored by pinning ``setuptools<81`` (or by moving to
# a ``pymeasure`` release that dropped ``pkg_resources``) and reinstating the
# two blocks above. Note that the two drivers do NOT report state identically:
# the labpy object could report ``'ON'``, which the in-repo driver never does.
# ---------------------------------------------------------------------------


class TestUtils:
    """ Some utility methods common to all tests.
    """

    def open_ps(self, name='ps', voltage=None, current=None):
        """ Opens a power supply and configures it

        The `voltage` and `current` to be programmed can be specified. If `None`, the global values from
        the config file in ``f_engine_tests.global_settings`` will be used.
        """
        # Validate the configuration before touching the instrument.
        if self.cfg.get('manual_ps', False):
            raise RuntimeError(
                'manual_ps is set in the test config, so no supply can be driven '
                'remotely, but this test requires one. Clear manual_ps or run the '
                'power cycles by hand.')
        if name not in self.cfg.instruments:
            raise RuntimeError(
                f"No '{name}' entry in the test config 'instruments' section, but this "
                f"test needs the bench supply. Expected e.g. "
                f"instruments: {{{name}: {{hostname: '10.10.1.38'}}}}")

        instr_params = self.cfg.instruments[name].copy()
        voltage = voltage if voltage is not None else self.cfg.f_engine_tests.global_settings.ps_voltage
        current = current if current is not None else self.cfg.f_engine_tests.global_settings.ps_current

        print(f'Setting up power supply at {instr_params.get("hostname")}')
        self.ps = CratePowerSupply(**instr_params)
        self.ps.set_output(state=False)  # Ensuring power on N5764A is off
        self.ps.clear()  # Clearing any previous protection
        self.ps.set_voltage(voltage=voltage)  # Setting voltage, power still off
        self.ps.set_current_limit(current=current, ocp=True)  # Setting current limit and turning on ocp feature
        return self.ps

    def power_off_safe(self):
        """ Turn the crate off without ever raising.

        Called from ``finally`` blocks, where an exception would replace the
        real test failure with a misleading one. A supply that was never opened
        (``self.ps is None``) is not an error here.
        """
        if not getattr(self, 'ps', None):
            return
        try:
            self.ps.set_output(state=False)
        except Exception as e:  # never mask the failure that got us here
            print(f'WARNING: could not turn the crate off ({e!r}) - do this manually')

    def power_cycle(self):
        """ Force the crate through a full off/dwell/on cycle and wait for boot.

        The supply is opened if needed. The off-dwell and boot wait both come
        from the config: the crate needs a minimum idle time with the output
        off before it is turned back on, and its ARM boot takes minutes, so a
        short wait here manufactures spurious initialization failures.
        """
        if not self.ps:
            self.ps = self.open_ps()

        settings = self.cfg.f_engine_tests.global_settings
        off_dwell = settings.ps_off_dwell
        boot_wait = settings.ps_t_sleep

        ps_status = self.ps.status()['status']
        print(f'Power supply is {ps_status}')
        if ps_status == 'FAULT':
            # Clear a latched protection trip, otherwise the output will not enable.
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
        if status['status'] not in ('OK', 'ILIMIT'):
            raise RuntimeError(
                f"Power supply is {status['status']} after being told to turn on "
                f"(measured {status['voltage']} V, {status['current']} A) - the crate "
                f"has no power, so no board can be expected to answer.")

    def crate_init(self, reset_power = True):
        """
        Run fpga_array, initializing the crate. Recover ca object. Power is reset by default.
        """
        if reset_power:
            self.power_cycle()

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
            self.power_off_safe()  # never raise from a teardown

    def test_ps_connection(self, xr):
        """
        QC000: Power supply connection test: the supply must respond to every
        on/off command, and the crate must draw boot current every time it is
        turned on.

        Procedure:

            - Open one connection to the supply and program the setpoints
            - `n_ps_cycles` times: output ON, wait `on_time`, check the supply
              reports 'OK' and that its output current is at least
              `min_boot_current`; output OFF, wait `off_time`, check it reports
              'OFF'

        `on_time` must be longer than the crate boot wait (`ps_t_sleep`): every
        ON then completes a full cold boot of all 16 SD-card Linux boards
        before the power is cut, so the test doubles as `n_ps_cycles` clean
        cold boots. A shorter `on_time` would instead interrupt every boot
        mid-way, which is a corruption risk for the boards' SD cards and tests
        nothing about the crate, so it is rejected up front. The current check
        turns "the supply says OK" into "the crate actually came up": an
        on-but-dead crate draws far less than a booted one.
        """
        xr.header('Power Supply Connection Test')
        cfg = self.cfg.f_engine_tests.ps_connection_test
        n_ps_cycles = cfg.n_ps_cycles
        on_time = cfg.on_time
        off_time = cfg.off_time
        min_boot_current = cfg.min_boot_current
        settings = self.cfg.f_engine_tests.global_settings

        # Validate the cycle timing before touching the supply.
        assert on_time >= settings.ps_t_sleep, (
            f'ps_connection_test.on_time ({on_time} s) is shorter than the crate boot wait '
            f'global_settings.ps_t_sleep ({settings.ps_t_sleep} s): every cycle would cut the '
            f'power to 16 booting boards mid-boot. Set on_time >= ps_t_sleep.')
        assert off_time >= settings.ps_off_dwell, (
            f'ps_connection_test.off_time ({off_time} s) is shorter than the required off-dwell '
            f'global_settings.ps_off_dwell ({settings.ps_off_dwell} s). Set off_time >= ps_off_dwell.')

        test_results = NameSpace()
        test_results.on_readings = []   # per-cycle supply status/voltage/current after the boot wait

        turn_ON_errs = []
        low_current_errs = []
        turn_OFF_errs = []

        passed = False

        try:
            # One connection for the whole test. This used to be re-opened on every
            # cycle, which leaked an instrument object and re-programmed the
            # setpoints 50 times; the loop exercises the output control, not the
            # connection setup.
            self.ps = self.open_ps()
            # open_ps() forces the output off. If the crate was powered when this
            # test started (e.g. a previous run was killed mid-cycle), the first ON
            # below would otherwise follow that OFF with no dwell at all.
            print(f'Output is off; waiting {off_time} seconds before the first cycle')
            time.sleep(off_time)

            for n in range(n_ps_cycles):

                print('\n*******************************')
                print(f'Cycle {n+1}/{n_ps_cycles}', flush=True)
                print('*******************************')

                print('--------------------------------------------')
                print(f'Turning ON power supply')
                print(f'Wait {on_time} seconds for the crate to boot')
                # turn on power supply
                self.ps.set_output(state=True)
                time.sleep(on_time)
                status = self.ps.status()
                test_results.on_readings.append(dict(cycle=n + 1, **status))
                print(f"Power supply is {status['status']}: {status['voltage']} V, {status['current']} A")
                # 'OK' is the only healthy powered state reported by the driver
                # ('ILIMIT' means the output is up but stuck in current limit).
                if status['status'] != 'OK':
                    turn_ON_errs.append(f"cycle {n+1}: {status['status']}")
                elif status['current'] < min_boot_current:
                    low_current_errs.append(
                        f"cycle {n+1}: {status['current']} A after {on_time} s "
                        f"(expected >= {min_boot_current} A from a booted crate)")

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

        Procedure:

            - Start the bperr test on the computer
            - Power up crate
            - Run `n_checks` independent health passes, `t_sleep` apart. Each
              pass clears every monitor, lets them re-accumulate on live
              traffic, then reads the backplane GTX lock and frame monitors,
              both crossbars' per-lane aligner stickies, the CROSSBAR2 packet
              error counters, and confirms the frame counters are advancing.

        Known persistent failure mode: on current shuffle256 bitstreams the
        CROSSBAR2 aligners drop a small fraction of frames indefinitely after
        every bring-up, on scattered GTX-fed lanes, while the links themselves
        stay CRC-clean. That defect makes this test fail on an otherwise
        healthy crate. It is deliberately NOT masked - read the failure list:
        a BLOCK_LOCK=0, a CRC/frame error count, a frame-length variance or a
        static frame counter is a NEW fault and means a real backplane,
        cable or board problem, whereas MISSING_FRAME/BAD_* stickies spread
        thinly across many CROSSBAR2 lanes are the known aligner defect.
        """

        xr.header('Backplane Error Test')
        cfg = self.cfg.f_engine_tests.backplane_err
        n_checks = cfg.n_checks
        t_sleep = cfg.t_sleep
        test_results = NameSpace()

        passed = False
        bp_errs = []

        try:
            # Initialize the crate
            self.ca = self.crate_init()

            for n in range(n_checks):

                # Let the crate run between passes so that errors which only
                # appear under sustained traffic have time to accumulate.
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
            self.power_off_safe()  # never raise from a teardown

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
        cfg = self.cfg.f_engine_tests.sync_test
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
                    # Pass the frame-0 alignment limit explicitly: left at None,
                    # sync() falls back through the provider attribute chain to
                    # the FPGAArray default and then to a hard-coded 20 ns, so
                    # the criterion this test enforces would not be visible here.
                    self.ca.sync(max_sync_time_difference=max_sync_time_difference)
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
            self.power_off_safe()  # never raise from a teardown

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
        init_failures = []

        passed = False
        try:
            for n in range(n_cycles):
                print(f'****************************')
                print(f'Power-cycling test iteration {n + 1}/{n_cycles}')
                print(f'****************************')
                
                # Initialize the crate:
                try:
                    self.ca = self.crate_init(reset_power=True)

                # Anything at all here means this cycle produced no clock
                # measurements. FPGAArray() raises RuntimeError for missing
                # boards, but AssertionError/TypeError/AttributeError also
                # escape the firmware bring-up, so do not narrow this.
                except Exception as e:
                    print(f'Failed initializing the array because of error {e!r}')
                    # A cycle that never booted is a failure of this test: without
                    # recording it, a crate that never comes up leaves failed_boards
                    # empty and the assert below passes.
                    init_failures.append(f'cycle {n + 1}/{n_cycles}: {e!r}')

                else:
                    for i in self.ca.ib:
                        print(f'Checking adc clocks on: {i}')
                        # check_adc_frequencies() returns True on ERROR (its docstring
                        # has the polarity backwards). Use the return value, not
                        # adc_clk_err_ctr, which also counts retries that then healed.
                        error = i.check_adc_frequencies('after boot')
                        if error:
                            failed_boards.append(
                                f'cycle {n + 1} slot {i.slot} ({i!r}): '
                                f'{i.adc_clk_err_msgs[-1] if i.adc_clk_err_msgs else "no message"}')
                        # for clock in range(16):
                        #     print(f'{i}, ADC_CLK{clock}')
                        #     diffs = set(i.FreqCtr.read_frequency(f'ADC_CLK{clock}', integration_period) - 200e6 for _ in range(n_clock_checks))
                        #     # If diffs is not a subset of expected_diffs, i.e. it contains an unexpected value, then append to failed_clocks
                        #     if not diffs.issubset(expected_diffs):
                        #         failed_clocks.append([f'{i}', f'ADC_CLK{clock}', diffs])    

                    

                # self.ps.set_output(state=False)
                time.sleep(delay)

            print(f'{failed_boards=}')
            print(f'{init_failures=}')

            assert not (failed_boards or init_failures), (
                f'ADC clock test failed over {n_cycles} power cycles.\n'
                f'  Cycles that failed to initialize ({len(init_failures)}):\n    '
                + ('\n    '.join(init_failures) or 'none')
                + f'\n  Boards with ADC clock errors ({len(failed_boards)}):\n    '
                + ('\n    '.join(failed_boards) or 'none'))

            passed = True
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


        

    def test_sync_delays(self, xr):
        """
        QC005: Sync delays test: the ADC SYNC alignment must be stable on every board.

        Procedure:

          - Start the sync delays test on the computer
          - Power up crate
          - For each motherboard, compute and apply the SYNC delays, then
            re-measure: `check_sync_delays()` sweeps the ADC pulse pattern
            `trials` times and returns the number of trials in which a rising
            edge moved by more than half an ADC input clock period. Zero means
            the alignment is adequate; anything else is a marginal board.

        This used to print the delays and ask the operator "are there any sync
        delay errors? Y/N". That could not be run unattended, recorded a
        keystroke rather than a measurement, and compared xr.input() against
        the literal 'y'/'n' - which does not strip whitespace, so 'yes' or a
        bare ENTER silently meant "no errors" and the inner loop never exited.
        """

        xr.header('Sync Delays Test')
        cfg = self.cfg.f_engine_tests.sync_delay_test
        trials = cfg.trials
        verbose = cfg.verbose
        test_results = NameSpace()

        sync_delay_errors = []

        # Initialize the crate:
        passed = False
        try:
            self.ca = self.crate_init()

            for i in self.ca.ib:
                print(f'=============================================================')
                print(f'Computing sync delays for {i}')
                try:
                    i.compute_sync_delays(set_sync_delays=True, verbose=verbose)
                except Exception as e:
                    # Record and carry on, so one bad board does not hide the
                    # state of the other fifteen.
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
            self.power_off_safe()  # never raise from a teardown

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
        # cfg.n_refs and cfg.n_fails_accept belong to the superseded per-channel
        # reference-set algorithm kept commented at the end of this test.
        test_results = NameSpace()

        
        
        # Initialize the crate:
        passed = False

        try:
            # input('Turn on the power supply. Press ENTER to continue. (Q:Exit) ')
            self.ca = self.crate_init()


            # Accumulated across ALL boards: these used to be re-created inside the
            # board loop, so the assert below only ever saw the last board.
            spreads = {}
            unstable_channels = []

            for i in self.ca.ib:
                print(f'=============================================================')
                print(f'Checking ADC eye diagrams for {i}')

                # capture_adc_eye_diagram() returns (n_channels, 32, 11) uint8:
                # axis 1 is the IDELAY tap, axis 2 the sample offset within the
                # 11-sample 'pulse' pattern. Unpacking to bits and OR-ing several
                # captures together accumulates every bit that was ever set; a
                # stable channel lights the same bits every time, so the per-(channel,
                # tap) bit count stays at its nominal value. Jitter between captures
                # lights additional offsets and drives the count up - that excess is
                # what max_spread bounds.
                eye_diagram = None
                for n in range(n_checks):
                    capture = np.unpackbits(i.capture_adc_eye_diagram(), axis=2)
                    eye_diagram = capture if eye_diagram is None else np.bitwise_or(eye_diagram, capture)

                spread = np.sum(eye_diagram, axis=2)  # (n_channels, 32 IDELAY taps)
                spreads[i.slot] = spread

                # checks if the ammount of bit smear is larger than expected
                for channel, tap in np.argwhere(spread > max_spread):
                    detail = (f'slot {i.slot} channel {channel} IDELAY tap {tap}: '
                              f'{spread[channel, tap]} bits set over {n_checks} captures '
                              f'(limit {max_spread})')
                    unstable_channels.append(detail)
                    print(f'UNSTABLE: {detail}')

                print(f'Largest bit spread on slot {i.slot} after {n_checks} checks: {spread.max()}')
                    


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

            assert not unstable_channels, (
                f'{len(unstable_channels)} unstable ADC eye channel(s) across '
                f'{len(spreads)} board(s):\n    ' + '\n    '.join(unstable_channels))
            passed = True

        finally:
            test_results.passed = passed
            xr.save_data(test_results)
            self.power_off_safe()  # never raise from a teardown


    def test_network(self, xr):
        """
        QC008: Network test: every crate slot must re-appear on the network
        after each power cycle.

        Procedure:

            - Power cycle the crate `n_cycles` times
            - After each boot, connect to the array and check that every slot
              of every crate is populated
            - Tally the cycles in which one or more boards failed to appear

        This is a discovery/reachability test: it uses the network_test
        fpga_array_params (prog: 0, open: 0), so it only enumerates boards and
        does not program or initialize them.
        """
        xr.header('Network Test')
        cfg = self.cfg.f_engine_tests.network_test

        test_results = NameSpace()
        n_cycles = cfg.n_cycles
        t_cycle = cfg.t_cycle

        passed = False
        missing_slot_errors = []
        error_total = 0

        try:

            for n in range(n_cycles):
                print(f'****************************')
                print(f'Power-cycling test iteration {n + 1}/{n_cycles}')
                print(f'****************************')

                # Initialize the crate:
                try:
                    self.power_cycle()

                    fpga_array_params = cfg.fpga_array_params
                    self.ca = fpga_array.FPGAArray(**fpga_array_params)

                    # Enumerate the CURRENT array. Crate.get_all_registered_instances()
                    # is a process-global registry: after a construction that failed
                    # part-way it still holds the partial hardware map, which does not
                    # correspond to self.ca.
                    missing_slots = {
                        (ic.part_number, ic.serial, ic.crate_number):
                            set(range(1, ic.NUMBER_OF_SLOTS + 1)) - set(ic.slot)
                        for ic in self.ca.ic}

                    if any(missing_slots.values()):
                        missing_slots_str = '\n'.join(
                            '    Crate #{number} ({model} SN{serial}): slots {slots}'.format(
                                number=number,
                                model=model,
                                serial=serial,
                                slots=', '.join(str(s) for s in sorted(slots)))
                            for ((model, serial, number), slots) in missing_slots.items() if slots)
                        print(f'Cycle {n + 1}: the following slots are missing:\n{missing_slots_str}')
                        missing_slot_errors.append((n + 1, missing_slots_str))
                        # Count missing SLOTS, not crates.
                        error_total += sum(len(slots) for slots in missing_slots.values())

                # FPGAArray() raises RuntimeError for missing boards unless
                # ignore_missing_boards is set, so this is the usual detection
                # path, not an exceptional one. Anything else here still means
                # the crate did not come up on the network this cycle.
                except Exception as e:
                    print("*" * 10)
                    print(f'Failed initializing the array because of error {e!r}')
                    missing_slot_errors.append((n + 1, f'    array did not enumerate: {e!r}'))
                    error_total += 1

                print('Turning OFF the crate')
                self.power_off_safe()  # never raise from a teardown
                print(f'Letting the crate cool down for {t_cycle} seconds before repeating the test')
                time.sleep(t_cycle)

            assert not missing_slot_errors, (
                f'Boards failed to appear on {len(missing_slot_errors)} of {n_cycles} '
                f'power cycles ({error_total} missing slot(s) in total):\n'
                + '\n'.join(f'  cycle {cycle}:\n{detail}'
                             for cycle, detail in missing_slot_errors))
            passed = True

        finally:
            test_results.passed = passed
            xr.save_data(test_results)
            self.power_off_safe()  # never raise from a teardown


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

        try:

            for n in range(n_cycles):
                print(f'****************************')
                print(f'Power-cycling test iteration {n + 1}/{n_cycles}')
                print(f'****************************')
                
                # Initialize the crate:
                try:
                    self.ca = self.crate_init()

                # Not narrowed to (RuntimeError, IOError, OSError): AssertionError,
                # TypeError and AttributeError all escape FPGAArray() and the
                # firmware bring-up, and an uncaught one here would abandon the
                # whole run instead of counting one bad cycle.
                except Exception as e:
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
 
                        # Check counters and error lists.
                        # udp_err_ctr is only ever incremented inside
                        # get_fpga_udp_metrics_async(); without this call it is
                        # structurally always 0 and the UDP check below is dead.
                        asyncio.run(i.get_fpga_udp_metrics_async())

                        n_clk_errs = i.adc_clk_err_ctr
                        clk_errs_msgs = i.adc_clk_err_msgs
                        n_udp_errs = i.udp_err_ctr
                        n_mmi_errs = i.mmi.error_counter
                        udp_errs_msgs = i.udp_err_msgs
                        temperatures = i.get_temperatures()
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
                                         'udp_err_msgs': udp_errs_msgs,
                                         'temps': temperatures})

                        if n_mmi_errs != 0:
                            mmi_fails += 1
                            mmi_err_tags.append({'Cycle': n,
                                         'ib': i,
                                         'n_mmi_errs': n_mmi_errs,
                                         'udp_err_msgs': udp_errs_msgs,
                                         'temps': temperatures})

                        # Now set all ADC delays and check for exceptions:
                        try:
                            i.set_adc_delays(compute_delays = 2, save_delays = False, check_sync_delays = 1, check_adc_delays = 20)
                        except Exception as e:
                            # set_adc_delays() also raises TypeError, and
                            # AssertionError can escape the ADC helpers.
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
                self.power_off_safe()  # never raise from a teardown
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

            # Each category is compared against the threshold separately, so a
            # crate is rejected when any single failure mode exceeds it.
            # mmi_fails is included: it used to be counted and then left out of
            # the verdict, so MMI control-plane errors could never fail the test.
            categories = [
                ('crate initialization', init_exception_fails, init_exception_tags),
                ('ADC clock', clk_fails, adc_err_tags),
                ('UDP communication', udp_fails, udp_err_tags),
                ('MMI control plane', mmi_fails, mmi_err_tags),
                ('set_adc_delays', delay_fails, delay_exception_tags),
                ('backplane/corner turn', backplane_fails, backplane_err_tags),
            ]
            over = [(name, count, tags) for name, count, tags in categories if count > n_fails_accept]

            # The failure message has to stand on its own: the console output above
            # is not part of the archived test record.
            assert not over, (
                f'Power cycle test failed over {n_cycles} cycles '
                f'(at most {n_fails_accept} failed cycles allowed per category).\n'
                + 'Counts: ' + ', '.join(f'{name}={count}' for name, count, _ in categories) + '\n'
                + '\n'.join(
                    f'  {name}: {count} failed cycle(s), first {min(len(tags), 5)} shown:\n'
                    + '\n'.join(f'    {tag}' for tag in tags[:5])
                    for name, count, tags in over))
            passed = True  # Yeh, we made it through

        finally:
            test_results.passed = passed
            xr.save_data(test_results)
            self.power_off_safe()  # never raise from a teardown



           
            
    def test_crate_ramp(self, xr):
        """
        QC009: Crate level ramp test: every ADC channel on every board must
        deliver a clean ramp through the firmware ramp checker.

        Procedure:

          - Power up crate
          - For each motherboard, compute and apply the ADC delays, then put
            the ADCs into ramp mode and read the firmware's per-channel
            RAMP_ERR_CTR (`check_ramp_errors`). Any word error means the LVDS
            capture timing into the FPGA's ADCDAQ block is not valid.

        This checks the ADC front end only: it does not exercise the
        backplane, the corner turn or the 10 GbE output, so it is
        complementary to the backplane error test rather than redundant.

        Restored from commit 9fb95415 (2024-08-12); the implementation was
        lost in merge 5fbeddf5 (2025-07-02), which took this file from one
        parent and test_config.yaml from the other, leaving the menu entry
        pointing at a method that no longer existed.
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
            self.power_off_safe()  # never raise from a teardown

    @staticmethod
    def _format_bp_errs(bp_errs, n_checks, max_shown=25):
        """ Render the accumulated health failures into an actionable message.

        The known CROSSBAR2 aligner defect can produce hundreds of entries, so
        the full list is truncated; the counts and the first entries are what
        distinguish a new fault from the known one.
        """
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
        """
        Check for backplane and corner-turn errors on every board of the crate.

        Shared with the power cycle test, which calls this once per cycle.

        The measurement is :func:`ct_health.measure_crate_health`, which is the
        same one used by the firmware suite's crate health test, so both judge
        the crate by identical criteria. It returns a list of human-readable
        problem descriptions; an empty list means the crate is healthy.

        This replaced a hand-rolled walk over
        ``get_corner_turn_engine_status_async(reset_stats=True)``, which could
        not work: that call clears the sticky bits immediately before sampling
        them, and resets the backplane statistics once per lane group, so the
        'qsfp' pass wiped any error the 'pcb' pass had just accumulated. It
        reported a clean crate almost unconditionally.
        """
        return ct_health.measure_crate_health(self.ca.ib)


if __name__ == '__main__':
    """ Run the test in this file."""
    v = TestMenu(TEST_CONFIG_FILE).run()
    locals().update(v) # bring local variables from the test runner into the current namespace for easier debugging
