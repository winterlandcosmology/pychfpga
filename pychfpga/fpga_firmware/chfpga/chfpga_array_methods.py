"""Array-wide methods for :class:`pychfpga.fpga_firmware.chfpga.chfpga.chFPGA`."""

import asyncio
from pychfpga.common import Ccoll
import matplotlib.pyplot as plt
import datetime
import re
import pickle
import os
import time

import numpy as np

from pychfpga.common.array_methods import ArrayMethods
from pychfpga.common.raw_frame_receiver import RawFrameReceiver
from pychfpga.common.state import State
from pychfpga.common.ucorr_frame_receiver import UCorrFrameReceiver
from pychfpga.common.udp_packet_receiver import UDPPacketReceiver
from pychfpga.common.tcp_packet_receiver import TCPPacketReceiver


# Pipeline compensation after an integer-second IRIG target so ADC frames
# start on the second. The old default delay was ``2 - 0.006556800``; the 2 s
# pad is gone and this offset remains.
IRIG_SYNC_PIPELINE_OFFSET_S = -0.006556800
IRIG_SYNC_MIN_SLACK_S = 0.05
IRIG_SYNC_DUMMY_ARM_DELAY_S = 300
IRIG_SYNC_POLL_LEAD_S = 0.020


class chFPGAArrayMethods(ArrayMethods):
    """Array-wide methods for chFPGA firmware arrays.

    FPGAArray instantiates this provider once the array is hardware-mapped and
    every motherboard shares the chFPGA firmware. Methods operate on
    ``self.ib`` / ``self.ic`` (the array motherboard and crate collections).

    :meth:`array_init_async` is the firmware-level init (channelizers, backplane,
    sync, operational mode). FPGA bitstream programming and MMI open are owned
    by :class:`~pychfpga.fpga_array.FPGAArray`, not this class.
    """



    def __init__(self, fpga_array):
        """Bind this provider to ``fpga_array`` and initialize sync/capture state.

        Parameters:

            fpga_array (FPGAArray): The array this provider serves.
        """
        super().__init__(fpga_array)
        self.perf = fpga_array.perf
        self._sync_timestamp = None
        self._sync_timestamps = None
        self._sync_start_time = None
        self._sync_method = None
        self._sync_master = None
        self._max_sync_time_difference = 100
        self._mode = None
        self._tx_power = None
        self._packet_receiver = None
        self._data_recv = None
        self._corr_recv = None
        self._raw_data_config = None
        self._corr_data_config = None


    #####################################################
    # Getter methods
    #####################################################

    def get_sync_timestamp(self):
        """Return the Frame-0 IRIG-B time from the last successful array sync.

        Returns:

            The first board's IRIG timestamp (``format='raw'``) after the last
            successful :meth:`sync_async` in an IRIG-B method, or ``None`` if
            that sync has not run (or used a non-IRIG method).
        """
        return self._sync_timestamp

    def get_sync_timestamps(self):
        """Return per-board Frame-0 IRIG timestamps from the last IRIG-B sync.

        Returns:

            A collection of per-board IRIG timestamps (``format='raw'``), or
            ``None`` if an IRIG-B :meth:`sync_async` has not completed.
        """
        return self._sync_timestamps

    def get_sync_start_time(self):
        """Return the IRIG comparator time(s) programmed during the last sync.

        Returns:

            The value returned by ``set_irigb_trigger_time_async`` for the last
            IRIG-B :meth:`sync_async` (a single time, or a per-board list when
            ``delay`` was a list). ``None`` until an IRIG-B sync has armed.
        """
        return self._sync_start_time

    def get_sync_method(self):
        """Return the array sync method last passed to :meth:`set_sync_method_async`.

        Returns:

            str or None: One of ``'irig-b'``, ``'trig'``, ``'local'`` (or an
            alias), or ``None`` if unset.
        """
        return self._sync_method

    def get_operational_mode(self):
        """Return the operational mode last set on this array.

        Returns:

            str or None: Mode string such as ``'shuffle8'`` or ``'corr16'``, or
            ``None`` if :meth:`set_operational_mode_async` has not run.
        """
        return self._mode

    #####################################################
    # Initialization and configuration methods
    #####################################################

    async def array_init_async(
            self,
            *,
            # platform_firmware_pathname=None,
            mode=None,
            bitfile=None,
            prog=None,
            init=None,
            if_ip=None,
            fpga_ip_addr_fn=None,
            udp_retries=10,
            sync_method=None,
            sync_source=None,
            sync_master=None,
            sync_master_source=None,
            sync_master_output=None,
            max_sync_time_difference=100,
            adc_mode=0,
            adc_bandwidth=2,
            frames_per_packet=2,
            chan8_channel_map=None,
            sampling_frequency=None,
            tx_power=None,
            integration_period=None,
            autocorr_only=None,
            corner_turn_bad_links=None,
            corner_turn_bin_priority=None,
            corner_turn_remap_level=0,
            **kwargs):
        """Initialize chFPGA firmware after FPGAArray has programmed and opened the FPGAs.

        This is firmware-level init (init >= 3): channelizers / CT engines,
        backplane, sync method, then operational mode (which itself ends with an
        array :meth:`sync_async`). Bitstream programming, MMI open, and the
        program/open retry loop belong to :class:`~pychfpga.fpga_array.FPGAArray`.

        If the array has no motherboards, this returns immediately.

        Parameters:
            mode (str): Operational mode to apply at init >= 3. Required when
                ``init >= 3``. See :meth:`set_operational_mode_async` for
                platform-specific names (``shuffle8``, ``corr16``, …). Stored
                as the default for later mode changes.
            bitfile: Accepted for FPGAArray kwargs compatibility. Unused
                here; FPGAArray pops it before calling this method.
            prog: Same as ``bitfile``.
            if_ip: Used by FPGAArray while opening MMI, not here.
            fpga_ip_addr_fn: Same as ``if_ip``.
            udp_retries: Popped by FPGAArray before calling this method.
            init (int or None): Firmware init level. ``None`` becomes ``3``
                if ``mode`` is set, else ``0``. Values below 3 skip firmware
                init (FPGAArray may already have programmed and opened the
                FPGAs). Values of 3 or more run ``init_fpga`` on every board,
                crate ``init()``, then sync method and operational mode.
            sync_method: Forwarded to :meth:`set_sync_method_async`. If
                ``None`` at init >= 3, inferred as ``'irig-b'`` when every
                board is in a crate, or ``'local'`` when none are. A mixed
                crate/no-crate array logs a warning and leaves the method
                unset unless ``sync_source`` is also given.
            sync_source: Forwarded to :meth:`set_sync_method_async`.
            sync_master: Forwarded to :meth:`set_sync_method_async`.
            sync_master_source: Forwarded to :meth:`set_sync_method_async`.
            sync_master_output: Forwarded to :meth:`set_sync_method_async`.
            max_sync_time_difference (float): Maximum allowed Frame-0 IRIG
                spread in nanoseconds, stored for later :meth:`sync_async`
                checks. Default 100 ns.
            adc_mode (int): ADC chip mode passed to each board's
                ``init_fpga_async``. Default 0 (normal data).
            adc_bandwidth (int): ADC analog bandwidth setting passed to
                ``init_fpga_async``. Default 2.
            frames_per_packet: Forwarded to
                :meth:`set_operational_mode_async` at init >= 3.
            chan8_channel_map: Forwarded to
                :meth:`set_operational_mode_async` at init >= 3.
            tx_power: Forwarded to :meth:`set_operational_mode_async`.
            integration_period: Forwarded to
                :meth:`set_operational_mode_async`.
            autocorr_only: Forwarded to :meth:`set_operational_mode_async`.
            corner_turn_bad_links: Forwarded to
                :meth:`set_operational_mode_async`.
            corner_turn_bin_priority: Forwarded to
                :meth:`set_operational_mode_async`.
            corner_turn_remap_level: Forwarded to
                :meth:`set_operational_mode_async`.
            sampling_frequency (float): ADC sampling frequency in Hz, passed
                to ``init_fpga_async``. ``None`` uses the platform/mode
                default.
            **kwargs: Extra arguments forwarded to each board's
                ``init_fpga_async`` (firmware ``init_async``), e.g.
                ``processing_frequency``, ``reference_frequency``,
                ``data_width``.

        Returns:
            None

        Notes:

            - ``get_ber()`` before shuffle/mode init will lead to errors (see
              :meth:`set_operational_mode_async`).
            - All boards must have the same corner-turn engine type (or none);
              that is checked when the operational mode is applied.
        """
        self.ib = self.fpga_array.ib
        self.ic = getattr(self.fpga_array, 'ic', None)

        self._max_sync_time_difference = max_sync_time_difference
        self._mode = mode
        self._tx_power = tx_power

        if not self.ib:
            return

        if init is None:
            init = 3 if mode else 0

        if init >= 3:
            self.logger.info(f'{self.fpga_array!r}: Initializing FPGA firmware')
            self.perf.start('FPGA firmware init', description='Init level 3a: Initialize channelizers and CT/X engines')
            await self.ib.init_fpga_async(
                adc_mode=adc_mode,
                adc_bandwidth=adc_bandwidth,
                sampling_frequency=sampling_frequency,
                **kwargs)

        if init >= 3:
            self.logger.info(f'{self.fpga_array!r}: Init level 3b: Initializing Backplane')
            self.perf.start('Backplane init', description='Initialize backplanes')
            if self.ic:
                self.ic.init()

        if init >= 3:
            if not mode:
                raise RuntimeError(
                    f'An operational mode must be specified for init={init}')

            # Infer the sync method if not explicitly specified
            if sync_method is None:
                if all(self.ib.crate):  # Use IRIG-B sync method if all boards are in crates
                    sync_method = 'irig-b'
                    self.logger.info(
                        f'{self.fpga_array!r}: sync_method is not specified but was '
                        f'automatically set to "irig-b" since all boards are in crates')
                elif not any(self.ib.crate):  # Use local sync method if no boards are in crates
                    sync_method = 'local'
                    self.logger.info(
                        f'{self.fpga_array!r}: sync_method is not specified but was '
                        f'automatically set to "local" since no board is in a crate')
                else:
                    self.logger.warning(
                        f'{self.fpga_array!r}: Some motherboards are in crates and '
                        f'some are not. `sync_method` is not set.')

            # Set the sync method if it is explicitly specified or if the sync source is specified
            if sync_method or sync_source:
                self.logger.info(
                    f'{self.fpga_array!r}: Setting SYNC method to {sync_method}')
                self.perf.start('SYNC method', description='Init level 3c: Configure sync method')
                await self.set_sync_method_async(
                    method=sync_method,
                    source=sync_source,
                    master=sync_master,
                    master_source=sync_master_source,
                    master_output=sync_master_output)

            # Set the operational mode
            self.perf.start('Operational mode', description='Init level 3d: Set operational mode and sync')
            await self.set_operational_mode_async(
                mode=mode,
                frames_per_packet=frames_per_packet,
                chan8_channel_map=chan8_channel_map,
                tx_power=tx_power,
                integration_period=integration_period,
                autocorr_only=autocorr_only,
                corner_turn_bad_links=corner_turn_bad_links,
                corner_turn_bin_priority=corner_turn_bin_priority,
                corner_turn_remap_level=corner_turn_remap_level)
    
    async def set_operational_mode_async(self,
                             mode,
                             frames_per_packet=1,
                             send_flags=True,
                             chan8_channel_map=list(range(8)),
                             tx_power=None,
                             integration_period=16384,
                             autocorr_only=False,
                             corner_turn_bad_links=None,
                             corner_turn_bin_priority=None,
                             corner_turn_remap_level=0,
                             ):
        """Set the operational mode of the array.

        Configures the corner-turn engine on every board, optionally starts
        firmware correlators, then runs a full array :meth:`sync_async`.

        Prerequisites:

            - FPGAs are programmed and opened, and :meth:`array_init_async`
              (or equivalent ``init_fpga``) has run so channelizers and CT
              engines exist.
            - Every board must have the same CT engine class (BCT or UCT).
            - Crate boards must use the backplane clock
              (:meth:`check_clock_source` raises otherwise).
            - Do not call ``get_ber()`` before this; it leaves shuffle links
              in a state that breaks mode init.

        Parameters:

            mode (str): Operational mode. Platform-specific:

                IceBoard:

                    - ``'chan4'``: Corner-turn engine is bypassed and raw 4-bit
                      data from 4 channelizers is sent directly to the 4 10G
                      Ethernet links.
                    - ``'chan8'``: Corner-turn engine is bypassed and raw 8-bit
                      data from 8 channelizers is sent directly to the 8 10G
                      Ethernet links.
                    - ``'shuffle16'``: Channelize and shuffle within each
                      IceBoard; no inter-board shuffle. For single-board
                      operation or independent boards.
                    - ``'shuffle128'``: Shuffle within a crate across 8 boards
                      (128 channels). Data leaves via IceBoard QSFP+; no
                      inter-crate shuffle.
                    - ``'shuffle256'``: Shuffle within a crate across 16 boards
                      (256 channels).
                    - ``'shuffle512'``: Shuffle between crate pairs (crate N
                      and N+1, N even) to make a 32-board (512-channel)
                      correlator feed.
                    - ``'corr16'``: Corner-turn engine is hard-wired to feed
                      16 channels to the on-FPGA correlator.

                CRS:

                    - ``'shuffle8'``: Channelize 8 inputs and send through the
                      UPACK programmable bin packetizer to the 100G CGE
                      offload link.
                    - ``'corr8'``: Feed 8 channels to the on-FPGA correlator.
                    - ``'corr32'``: Shuffle 4 boards on the same backplane into
                      a 32-input correlator.
                    - ``'corr64'``: Shuffle 8 boards on two backplanes (SFP
                      cables between boards) into a 64-input correlator.

                ZCU111:

                    - ``'corr4'``: Feed 4 channels to the on-FPGA correlator.

                ``None`` or falsy reuses the mode stored by
                :meth:`array_init_async`.

            frames_per_packet (int): Frames combined into one output packet.
                Limited by FPGA buffering.

            send_flags (bool): If True, scaler and frame flags are appended
                after the data block.

            chan8_channel_map (list of int): Channel order for ``chan8`` (and
                related CT setup). Default is ``list(range(8))``.

            tx_power (dict): GTX transmit power levels. ``None`` reuses the
                value stored at array init.

            integration_period (int): For ``corr4`` / ``corr8`` / ``corr16`` /
                ``corr32`` / ``corr64`` only: correlator integration length in
                frames. Falsy skips starting the correlator.

            autocorr_only (bool): For correlator modes: if True, only
                autocorrelation products are sent.

            corner_turn_bad_links (list of tuples): CT outputs
                ``[(crate, slot, links), ...]`` that should preferably not be
                assigned frequency bins.

            corner_turn_bin_priority (list of int): Frequency bins in
                assignment order (most desirable first). Lowest-priority bins
                are steered toward bad links when the CT engine allows it.

            corner_turn_remap_level (int): How aggressively bins are remapped:
                0 = none, 1 = 3rd crossbar only, 2 = crossbars 2 and 3,
                3 = all crossbars.

        Returns:

            None
        """
        # use defaults that were set during initialization unless overriden
        mode = mode or self._mode
        tx_power = tx_power or self._tx_power
        if mode is not None:
            self._mode = mode
        if tx_power is not None:
            self._tx_power = tx_power
        self.logger.info(f'{self.fpga_array!r}: Setting operational mode to {mode}')
        self.perf.start('clock check', subtask=True, description='Crate vs backplane clock')
        await self.check_clock_source()

        # Get the class of the corner-turn engine across all boards (None if no CT engine)
        ct_classes = {(type(ct) if ct else None) for ct in self.ib.CT}  # get a set of all different CT engine types
        if len(ct_classes) > 1:
            raise RuntimeError(f'{self.fpga_array!r}: All boards must have either have no corner-turn engine or the same type of corner-turn engine')
        ct_class = ct_classes.pop()

        self.perf.start('mode setup', description='Crossbars / correlator / corner-turn')
        do_sync = False

        # bypass FFT and scaler in raw ADC streaming modes
        if mode in ('chan4', 'chan8'):
            self.ib.set_fft_bypass(True)
            self.ib.set_scaler_bypass(True)

        # initialize the corner-turn engine
        if ct_class is None:
            raise RuntimeError(f'{self.fpga_array!r}: No corner-turn engine available to initialize')
        await ct_class.init_corner_turn_array(
            self.fpga_array,
            mode=mode,
            frames_per_packet=frames_per_packet,
            send_flags=send_flags,
            tx_power=tx_power,
            chan8_channel_map=chan8_channel_map,
            bad_links=corner_turn_bad_links,
            bin_priority=corner_turn_bin_priority,
            remap_level=corner_turn_remap_level,
            sync=False)

        # start the correlator in correlator modes if integration period is specified
        if mode in ('corr4', 'corr8', 'corr16', 'corr32', 'corr64') and integration_period:
            self.ib.start_correlator(integration_period=integration_period, autocorr_only=autocorr_only)

        do_sync = True
        if do_sync:
            self.perf.start('sync', description='IRIG-B wait plus trigger RPCs')
            await self.sync_async()
        self.perf.stop()

    IRIGB_SYNC_METHODS = ('irig-b', 'irigb', 'distributed_time')
    TRIG_SYNC_METHODS = ('trig',  'centralized_soft_trigger')
    LOCAL_SYNC_METHODS = ('local', 'local_soft_trigger')

    async def set_sync_method_async(self, method='irig-b', source=None, master=None, master_source=None, master_output=0):
        """Set the global syncing method and configure every board for it.

        The boards start their sync process on the next rising edge of the
        10 MHz reference after the trigger event. That event should occur
        between two reference-clock edges so every board latches the same
        edge.

        Call this before :meth:`sync_async`. For ``'bp_gpio_int'`` as
        ``source``, a master board is required (GPIO_INT is enabled as an
        output only on that board).

        Parameters:

            method (str): Synchronization method:

                - ``'irig-b'`` (aliases ``'irigb'``, ``'distributed_time'``):
                  Every board syncs when the target IRIG-B time is reached.
                  ``source`` is the IRIG-B decoder input (see motherboard
                  ``set_irigb_source_async``). Default source is ``'bp_time'``.

                - ``'trig'`` (alias ``'centralized_soft_trigger'``): Every
                  board syncs on the rising edge of ``source`` (a REFCLK sync
                  source; see ``set_sync_source``). Default source is
                  ``'bp_time'``. The master board's ``generate_sync()`` is
                  used later by :meth:`sync_async`.

                - ``'local'`` (alias ``'local_soft_trigger'``): Each board
                  generates its own software SYNC when :meth:`sync_async` is
                  called. This resets each pipeline but does not align boards
                  or support multi-board corner-turn modes.

            source (str): Sync-signal source. For IRIG-B, an IRIG-B source
                name; for ``trig``, a REFCLK source name. Ignored for
                ``local``.

            master (Motherboard, str, int, or None): Board that generates the
                time or trigger signal:

                - ``None``: no master is set up.
                - Motherboard object: use that board.
                - str: board with that serial number (include leading zeros).
                - int: board in that slot (1-based).

            master_source (str): Source routed to the master's user output
                (``set_user_output_source`` on the master).

            master_output (int): User output index on the master that carries
                the sync signal. Default 0.

        Returns:

            None
        """
        if master:
            if isinstance(master, str):
                master = self.ib.get(serial=master)
            elif isinstance(master, int):
                master = self.ib.get(slot=master)
            master.set_user_output_source(source=master_source, output=master_output or 0)
            if source == 'bp_gpio_int':
                # Enable GPIO_INT as an output only on the master board
                # TODO: JFC: This code should be moved to lower-level methods
                self.ib.GPIO.BP_GPIO_INT_EN = 0
                master.GPIO.BP_GPIO_INT_EN = 1

        self._sync_master = master
        self._sync_method = method

        if source == 'bp_gpio_int' and not master:
            raise RuntimeError('A master board should be specified when bp_gpio_int is used as a source')

        if method in self.IRIGB_SYNC_METHODS: # REFCLK trigs on the output of the IRIG-B time comparator
            source = source or 'bp_time'
            self.ib.set_sync_source('irigb')
            await self.ib.set_irigb_source_async(source)
        elif method in self.TRIG_SYNC_METHODS: # REFCLK trigs directly on the rising edge of the specified source signal
            source = source or 'bp_time'
            self.ib.set_sync_source(source)
        elif method in self.LOCAL_SYNC_METHODS:
            self.ib.set_sync_source('local')
            self.ib.sync()
        else:
            raise ValueError(f"Unknown syncing method '{method}'")


    async def sync_async(self, delay=0, check=True, align_to_seconds=True, max_trials=3, max_sync_time_difference=None):
        """Generate a SYNC event across the whole array.

        Uses the method configured by :meth:`set_sync_method_async`. On
        success, scaler overflow flags are cleared on every board.

        IRIG-B path: after FPGA programming the decoder can take ~1 s to see a
        full second. This method refuses to arm if any board has no valid
        IRIG. A dummy arm far in the future
        (:data:`IRIG_SYNC_DUMMY_ARM_DELAY_S`) measures real array arm time
        (RPC, switch, load); slack is ``max(2 * t_arm, IRIG_SYNC_MIN_SLACK_S)``.
        With ``align_to_seconds``, the trigger base is the next integer second
        at least that slack in the future, then
        :data:`IRIG_SYNC_PIPELINE_OFFSET_S` plus ``delay`` are applied so ADC
        frames start on that second.

        Parameters:

            delay (float or list): Extra seconds added after the chosen
                trigger base. With ``align_to_seconds`` this is applied after
                the next safe integer second (and after
                :data:`IRIG_SYNC_PIPELINE_OFFSET_S`). A list is a per-board
                offset. Default 0: do not skip extra seconds.

            check (bool): If True, read SYNC counters on every board to
                confirm each counter advanced by 1. Automatically disabled
                (with a warning) if any board's firmware lacks REFCLK sync
                logic.

            align_to_seconds (bool): If True (IRIG-B only), align the trigger
                base to the next safe integer second as described above. If
                False, arm ``slack`` seconds after the current IRIG time with
                no pipeline offset.

            max_trials (int): Times to retry the whole SYNC on failure.
                Default 3. The last exception is re-raised after the final
                trial.

            max_sync_time_difference (float or None): For IRIG-B, maximum
                allowed Frame-0 time spread in nanoseconds. ``None`` uses the
                value stored by :meth:`array_init_async` (default 100 ns).

        Returns:

            None
        """

        # Don't try to check the SYNC status if we don't have a SYNC engine (REFCLK) in the firmware of all boards

        if check and not all(list(mb.REFCLK for mb in self.ib)):
            check = False
            self.logger.warning(f'{self!r}: SYNC will not be checked as not all firmware have SYNC logic')

        trial = 0
        while True:
            try:
                if check:
                    sync_ctr_before = self.ib.get_sync_counter()

                self._sync_timestamps = []
                self._sync_timestamp = None

                if self._sync_method in self.TRIG_SYNC_METHODS:
                    self._sync_master.generate_sync()


                elif self._sync_method in self.IRIGB_SYNC_METHODS:
                    # Validity gate: after FPGA programming the decoder can take
                    # ~1 s to see a full second. Fail before arming if any board
                    # has no valid IRIG. Source is selected during FPGA open so
                    # this is usually already valid.
                    # Times here use get_irigb_time's datetime (latch + 1 s);
                    # set_irigb_trigger_time programs the comparator 1 s earlier.
                    self.perf.start('irig_validity', subtask=True, description='Wait for decoder recent on all boards')
                    try:
                        t_valid0 = time.time()
                        dt = max(await self.ib.get_irigb_time_async(
                            trig=True, format='datetime', noerror=False))
                        self.logger.info(f'{self!r}: IRIG validity read took {time.time() - t_valid0:0.3f} s')
                        self.perf.start('irig_dummy', description='Dummy arm to measure slack')
                        t0 = time.time()
                        # Dummy arm far in the future so it cannot fire; measures
                        # real array arm time (RPC path, switch, load).
                        await self.ib.set_irigb_trigger_time_async(
                            dt, IRIG_SYNC_DUMMY_ARM_DELAY_S)
                        t_arm = time.time() - t0
                        slack = max(2 * t_arm, IRIG_SYNC_MIN_SLACK_S)
                        self.logger.info(f'{self!r}: Dummy IRIG arm took {t_arm:0.3f} s; '
                                         f'slack={slack:0.3f} s')
                        t_now = max(await self.ib.get_irigb_time_async(format='datetime'))
                        if align_to_seconds:
                            deadline = t_now + datetime.timedelta(seconds=slack)
                            second = deadline.replace(microsecond=0)
                            if deadline.microsecond:
                                second += datetime.timedelta(seconds=1)
                            base = second
                            pipeline = IRIG_SYNC_PIPELINE_OFFSET_S
                            self.logger.info(
                                f'{self!r}: Next safe IRIG second is '
                                f'{second.isoformat()} '
                                f'(t_now={t_now.isoformat()})')
                        else:
                            base = t_now + datetime.timedelta(seconds=slack)
                            pipeline = 0.0

                        t_prog0 = time.time()
                        if isinstance(delay, (list, tuple)):
                            extras = list(delay)
                            max_extra = max(extras)
                            totals = [pipeline + dly for dly in extras]
                            for mb, total in zip(self.ib, totals):
                                self.logger.info(
                                    f'{self!r}: Triggering SYNC on {mb!r} '
                                    f'{total:0.9f} s after {base.isoformat()}')
                            self._sync_start_time = sync_time = await asyncio.gather(*[
                                fpga.set_irigb_trigger_time_async(base, total)
                                for fpga, total in zip(self.ib, totals)])
                            target_dt = base + datetime.timedelta(
                                seconds=pipeline + max_extra)
                        else:
                            extra = float(delay)
                            total = pipeline + extra
                            self.logger.info(
                                f'{self!r}: Triggering SYNC {total:0.9f} s '
                                f'after {base.isoformat()}')
                            self._sync_start_time = sync_time = (
                                await self.ib.set_irigb_trigger_time_async(
                                    base, total))
                            target_dt = base + datetime.timedelta(seconds=total)
                        t_prog = time.time() - t_prog0
                        self.logger.info(
                            f'{self!r}: Final IRIG arm took {t_prog:0.3f} s; '
                            f'target={target_dt.isoformat()}')

                        self.perf.start('irig_wait', description='Sleep until comparator target')
                        irig_until_target = (target_dt - t_now).total_seconds()
                        sleep_s = irig_until_target - t_prog - IRIG_SYNC_POLL_LEAD_S
                        if sleep_s > 0:
                            self.logger.info(
                                f'{self!r}: Sleeping {sleep_s:0.3f} s '
                                f'until IRIG target')
                            await asyncio.sleep(sleep_s)
                        t_poll0 = time.time()
                        while True:
                            still_waiting = await self.ib.is_irigb_before_trigger_time_async()
                            if not any(still_waiting):
                                break
                            if time.time() - t_poll0 > slack + 1:
                                raise RuntimeError(
                                    'Timout while waiting for the IRIG-B-based '
                                    'SYNC to complete')
                            await asyncio.sleep(0.1)
                        await asyncio.sleep(0.1)  # make sure the sync sequence has time to finish
                    finally:
                        if self.perf._current[-1] != 'sync':
                            self.perf.stop()
                elif self._sync_method in self.LOCAL_SYNC_METHODS:
                    self.ib.sync()
                else:
                    valid_methods = ', '.join((self.IRIGB_SYNC_METHODS[0], self.TRIG_SYNC_METHODS[0], self.LOCAL_SYNC_METHODS[0]))
                    raise ValueError(f"Unknown syncing method '{self._sync_method}'. Valid methods are {valid_methods}")

                # Check if all boards of the array have sync'ed by looking at the sync counter
                if check:
                    sync_ctr_after = self.ib.get_sync_counter()
                    # self.logger.info(f'{self!r}: The SYNC counter before is:\n{sync_ctr_before}')
                    # self.logger.info(f'{self!r}: The SYNC counter after boards is:\n{sync_ctr_after}')

                    bad_mb = [mb for i, mb in enumerate(self.ib) if (sync_ctr_after[i] - sync_ctr_before[i]) & 0xf != 1]
                    if bad_mb:
                        bad_boards_str = ','.join(repr(mb) for mb in bad_mb)
                        raise RuntimeError(f'The following IceBoards did not SYNC properly: {bad_boards_str}')

                if self._sync_method in self.IRIGB_SYNC_METHODS:
                    self._sync_timestamps = ts = await self.ib.get_irigb_time_async(
                        trig=False, format='raw', noerror=False)
                    self._sync_timestamp = ts[0]

                    delta_ts = max(ts.nano) - min(ts.nano)
                    irigb_times = '\n'.join(
                        f'         {mb!r}: {ts[i].isoformat()} '
                        f'({ts[i].nano} ns since epoch, '
                        f'{ts[i].nano - sync_time[i].nano} ns after sync)'
                        for i, mb in enumerate(self.ib))
                    self.logger.info(f'{self!r}: The IRIG-B time for Frame 0 on all boards is:\n{irigb_times}')
                    self.logger.info(f'{self!r}: The maximum Frame 0 time difference is {delta_ts} ns')
                    max_sync_time_difference = (
                        max_sync_time_difference or self._max_sync_time_difference)
                    if delta_ts > max_sync_time_difference:
                        raise RuntimeError(f'The Frame 0 time difference of {delta_ts} '
                                           f'exceeds the maximum limit of {max_sync_time_difference}')
                for mb in self.ib:
                    mb.reset_scaler_overflow_flags()

                break # we made it all the way through successfulle, so exit the trial loop

            except Exception as e:
                trial += 1
                if trial >= max_trials:
                    raise RuntimeError(f'SYNC failed after {trial} trials. The last exception was:\n{e!r}')
                self.logger.warning(f'{self!r}: SYNC failed on trial {trial}/{max_trials} '
                                    f'due to the following error. Will retry.\n{e!r}')


    #####################################################
    # Clock management methods
    #####################################################

    async def check_clock_source(self):
        """Check that crate membership and backplane clock selection agree.

        To make sure that data acquisition and transmission run at the same
        rate, refuse to operate if a board that is in a crate is not using the
        backplane clock.

        A board that is not in a crate but is using the backplane clock only
        logs a warning (crate discovery may have failed).

        Returns:

            None
        """
        clock_sources = await self.ib.get_iceboard_clock_source_async()
        bad_clock_source_without_crate = [i for i, source in enumerate(clock_sources) if not self.ib[i].crate and source == 'CLOCK_SOURCE_BP']
        bad_clock_source_with_crate = [i for i, source in enumerate(clock_sources) if self.ib[i].crate and source != 'CLOCK_SOURCE_BP']

        if bad_clock_source_without_crate:
            msg = ', '.join(f'{self.ib[i]!r}=>{clock_sources[i]}' for i in bad_clock_source_without_crate)
            self.logger.warning(f'The following IceBoards are in not in crates but are configured to use the backplane clock: {msg}')

        if bad_clock_source_with_crate:
            msg = ', '.join(f'{self.ib[i]!r}=>{clock_sources[i]}' for i in bad_clock_source_with_crate)
            raise RuntimeError(f'The following IceBoards are in crates but are not configured to use the backplane clock: {msg}')

    #####################################################
    # Raw capture and Correlator engine configuration methods
    #####################################################

    async def start_data_capture_async(
            self,
            *args,
            sync=None,
            send_delay=None,
            send_delay_per_slot=None,
            **kwargs,
    ):
        """Start data capture on all channelizers of all boards in the array.

        Forwards arguments to each board's
        :meth:`~pychfpga.fpga_firmware.chfpga.chfpga.chFPGA.start_data_capture`.
        This array method captures the ``sync`` keyword itself, so it is not
        forwarded: each board therefore uses that firmware method's default
        (a local per-board sync). Prefer not to pass ``sync=True`` here while
        UCAP is already flooding the host — a full IRIG :meth:`sync_async`
        can time out MMI.

        Parameters:
            *args: Passed through to each board's ``start_data_capture``
                (period, frames_per_burst, channels, source, mode, offset, …).
            **kwargs: Same as ``*args``. Do not pass ``sync`` if you intend
                to control the board-local sync; this wrapper steals that name.
            send_delay_per_slot (int or float): When set, each board's ``send_delay`` is
                ``send_delay + int(slot * send_delay_per_slot)`` in ADC frames (UCAP clamped to
                65535). When omitted, the same ``send_delay`` is used on every board.
            sync (bool): If true, run a full array :meth:`sync_async` after
                every board has started capture.

        Returns:
            None
        """
        for b in self.ib:
            if send_delay is not None or send_delay_per_slot is not None:
                crate,slot = b.get_id()
                dly = (send_delay or 0) + int((slot or 0) * (send_delay_per_slot or 0))
            else:
                dly = send_delay
            b.start_data_capture(*args, send_delay=dly, **kwargs)
        if sync:
            await self.sync_async()

    async def start_correlators_async(self, integration_period, autocorr_only=False, no_accum=False,
                                      verbose=0):
        """Start all firmware correlators in the array.

        Calls each board's ``start_correlator``.

        Parameters:

            integration_period (int): Number of frames to integrate before
                sending correlated products.

            autocorr_only (bool): If True, only autocorrelation products are
                sent.

            no_accum (bool): If True, do not accumulate across frames; return
                the product from the last frame of the integration period.

            verbose (int): Verbosity passed to each board's
                ``start_correlator``.

        Returns:

            None
        """
        for mb in self.ib:
            mb.start_correlator(integration_period=integration_period,
                                autocorr_only=autocorr_only,
                                no_accum=no_accum,
                                verbose=verbose)
            await asyncio.sleep(0)

 

    #####################################################
    # Data pipeline reset methods
    #####################################################

    async def reset_corr_async(self, delay=0.1):
        """Pulse the correlator / corner-turn reset on every board.

        Asserts ``set_corr_reset(1)`` on the array, waits, then deasserts.
        That GPIO line resets both the corner-turn engine and the correlator.

        Parameters:

            delay (float): Intended pulse width in seconds. Currently unused;
                the wait is always 0.1 s.

        Returns:

            None
        """
        self.ib.set_corr_reset(1)
        await asyncio.sleep(0.1)
        self.ib.set_corr_reset(0)

    async def reset_gpu_links_async(self, board_ids=None):
        """Reset the GPU/QSFP links for the boards specified in ``board_ids``.

        Parameters:

            board_ids (list of tuple/dict): Board descriptors as
                ``(crate_number)`` or ``(crate_number, slot_number)`` tuples,
                or ``{crate: crate_number}`` / ``{crate: crate_number,
                slot: slot_number}`` dicts. Crate and slot may be ``'*'`` or
                Null to match every instance. ``None`` means every board in
                the array.

        Returns:

            list: Board IDs (from ``mb.get_id()``) that were actually reset.

        Notes:

            It is not possible to reset just one QSFP or a specific lane.
            All lanes of both QSFPs on a selected board are reset.
        """
        ibs = self.fpga_array.get_iceboards(board_ids)
        for mb in ibs:
            mb.reset_gpu_links()
            await asyncio.sleep(0)
        return ([mb.get_id() for mb in ibs])

    #####################################################
    # Data pipeline configuration methods
    #####################################################

    async def set_channelizers_async(self, channels=None, local_sync=None, sync=True, **kwargs):
        """Configure all channelizers in the array.

        Forwards keyword arguments to each board's
        :meth:`~pychfpga.fpga_firmware.chfpga.chfpga.chFPGA.set_channelizer`
        (ADC mode, data source, FFT/scaler bypass, gains, …). Array-wide
        ``local_sync`` is rejected: this method always passes
        ``local_sync=False`` to each board and optionally runs one array
        :meth:`sync_async` afterwards so boards stay aligned.

        Parameters:

            channels: Channel subset forwarded to each board's
                ``set_channelizer``. ``None`` means that board's default
                channels.

            local_sync: Must be ``None``. Passing a value raises
                ``RuntimeError``.

            sync (bool): If True (default), run :meth:`sync_async` after all
                boards are configured. Required for PWM / pipeline state that
                is referenced to Frame 0.

            **kwargs: Other ``set_channelizer`` arguments (``data_source``,
                ``fft_bypass``, ``gains``, …).

        Returns:

            None
        """
        if local_sync is not None:
            raise RuntimeError('local_sync parameter is not supported on array-wide set_channelizer call')
        for mb in self.ib:
            mb.set_channelizer(**kwargs, channels=channels, local_sync=False)
            await asyncio.sleep(0)
        if sync:
            await self.sync_async()

    def set_noise_injection(
            self,
            board,
            enable=False,
            offset=0,
            high_time=8388608,
            period=16777216,
            local_sync=True,
            output='bp_sma'):
        """Configure the noise-injection gating (PWM) signal on one board.

        When ``enable`` is True, the PWM generator is routed to ``output``
        (default backplane SMA). A sync is needed after PWM changes so the
        waveform is referenced to Frame 0; ``local_sync=True`` does that on
        this board only — use a later array :meth:`sync_async` if other boards
        must stay aligned.

        Parameters:

            board: Target board: serial string (with leading zeros),
                motherboard object, or ``(crate, slot)`` id as accepted by
                :meth:`~pychfpga.fpga_array.FPGAArray.get_iceboard`. Falsy
                (``None``, ``''``) skips setup and returns immediately.

            enable (bool): If True, enable PWM and route it to ``output``.

            offset (int): Frames to wait after Frame 0 before the first pulse.

            high_time (int): High time of the PWM waveform, in frames.
                Default 8388608.

            period (int): PWM period in frames. Default 16777216
                (so default duty cycle is 50%).

            local_sync (bool): If True, issue a local software sync on the
                target board after programming PWM.

            output (str): User output that drives the noise-injection
                hardware (``set_user_output_source``). Default ``'bp_sma'``.

        Returns:

            None
        """
        if not board:
            return

        mb = self.get_iceboard(board)

        if enable:
            mb.set_user_output_source('pwm', output=output)

        mb.set_pwm(enable=enable, offset=offset, high_time=high_time,
                   period=period, local_sync=local_sync)


    async def set_offset_binary_encoding_async(self, offset_encoding_enabled):
        """Enable or disable offset-binary encoding on every board.

        Parameters:

            offset_encoding_enabled (bool): Passed to each board's
                ``set_offset_binary_encoding``.

        Returns:

            None
        """
        self.logger.warning(f'{self.fpga_array!r}: set_offset_binary_encoding() is called. This should be removed')  # ***JFC
        for mb in self.ib:
            mb.set_offset_binary_encoding(offset_encoding_enabled)
            await asyncio.sleep(0)

    async def set_adc_delays_async(self, **kwargs):
        """Set ADC delays on every open motherboard in the array.

        Calls each board's ``set_adc_delays`` with the same arguments (load
        or compute tap delays, sync delays, optional save-to-file). Boards
        whose FPGA MMI is not open are skipped with a warning.

        Parameters:

            **kwargs: Forwarded to
                :meth:`~pychfpga.fpga_firmware.chfpga.chfpga.chFPGA.set_adc_delays`
                (``source``, ``compute_delays``, ``save_delays``,
                ``check_adc_delays``, ``retry``, …).

        Returns:

            None
        """

        for mb in self.ib:
            if mb.is_open():
                self.logger.debug(f"{self.fpga_array!r}: Setting ADC delays")
                mb.set_adc_delays(**kwargs)
                await asyncio.sleep(0)
            else:
                self.logger.warning(f"{self.fpga_array!r}: Communication with FPGA is not initialized. Cannot set ADC delays")

    def set_adc_mask(self, value):
        """AND-mask every ADC sample byte on every channel of every board.

        Writes ``ADCDAQ.BYTE_MASK`` on each channelizer. Used to force bits
        low (e.g. for ramp/pattern tests).

        Parameters:

            value (int): 8-bit mask. Each ADC sample byte is ANDed with this
                value.

        Returns:

            None
        """
        for mb in self.ib:
            for ant in mb.ANT:
                ant.ADCDAQ.BYTE_MASK = value

    async def get_user_output_state_async(self, board, output='bp_sma'):
        """Get the current state of a user output and the source driving it.

        Used by fpga_master to report noise-injection PWM status. Resolves
        ``board`` with :meth:`~pychfpga.fpga_array.FPGAArray.get_iceboard`.

        Parameters:
            board (tuple or list or str or Motherboard): Target board:
                ``(crate, slot)``, serial/hostname string, or motherboard
                object. Falsy skips hardware reads and returns only
                ``board``, ``output``, and ``output_source=None``.
            output (str): User-output name the noise-injection hardware is
                wired to (used to check whether PWM is actually routed there).
                Default ``'bp_sma'``.

        Returns:
            dict: Mapping with keys ``board``, ``output``, ``output_source``,
            ``pwm_offset``, ``pwm_high_time``, ``pwm_period``,
            ``pwm_enabled``, ``user_bit0``, and ``user_bit1``. PWM times are
            in frames. ``pwm_enabled`` is True when the PWM generator is not
            in reset. ``user_bit0`` / ``user_bit1`` matter when
            ``output_source`` is one of those static bits.
        """
        status = dict(board=board,
                      output=output,
                      output_source=None)
        if not board:
            return status

        mb = self.get_iceboard(board)
        output_source = mb.get_user_output_source(output)
        offset, high_time, period, reset = mb.get_pwm()
        user_bit0, user_bit1 = mb.get_user_bits()
        status.update(
            output_source=output_source,
            pwm_offset=offset,
            pwm_high_time=high_time,
            pwm_period=period,
            pwm_enabled=not reset,
            user_bit0=user_bit0,
            user_bit1=user_bit1)
        return status

    def get_stream_id_map(self):
        """Return the stream ID of every channel in the array, keyed by channel id.

        Merges each board's ``get_stream_id_map()``. Logs a warning if IDs are
        not unique (raw capture then cannot demux by stream ID).

        Returns:

            dict: ``{channel_id: stream_id}`` where ``channel_id`` is a
            ``(crate, slot, channel)`` tuple.
        """

        stream_id_map = {}
        for mb in self.ib:
            stream_id_map.update(mb.get_stream_id_map())

        if len(stream_id_map) != len(set(stream_id_map.values())):
            self.logger.warning(f'{self!r}: Stream IDs are not unique!')

        return stream_id_map
        
    #####################################################
    # Gain management methods
    #####################################################

    async def load_gains_async(self, bank=0, gain_folder='/home/chime/ch_acq/gains'):
        """Load digital-gain files for every board of the array.

        If a board-specific file is missing, ``default_gains.pkl`` in
        ``gain_folder`` is used. If that is also missing, that board's entry
        is ``None`` and a warning is logged.

        This does not program the FPGAs. Pass the result to
        :meth:`set_gains_async`.

        Parameters:

            bank (int): Unused. Kept for call-site compatibility.

            gain_folder (str): Directory containing per-board gain pickles and
                optional ``default_gains.pkl``.

        Returns:

            dict: ``{board_id: gains}`` where ``board_id`` is
            ``mb.get_id()`` (typically ``(crate, slot)``) and ``gains`` is the
            dict returned by that board's ``load_gains``, or ``None``.
        """

        # get the default gains, just in case we need them
        try:
            default_gains_filename = os.path.join(gain_folder, 'default_gains.pkl')  # filename of the default gains
            with open(default_gains_filename, 'rb') as f:
                default_gains = pickle.load(f)
        except IOError:
            default_gains = None

        array_gains = {}
        for mb in self.ib:
            board_id = mb.get_id()
            self.logger.debug(f'{self.fpga_array!r}: Reading digital gains for (crate,slot)={board_id}')
            board_gains = mb.load_gains(folder=gain_folder) or default_gains
            await asyncio.sleep(0)

            if not board_gains:
                self.logger.warning(f'{self.fpga_array!r}: Neither board-specific gain file not default '
                                    f'gain file was found for (crate,slot)={board_id}')
                array_gains[board_id] = None
            else:
                array_gains[board_id] = board_gains
        return (array_gains)

    async def save_gains_async(self, gains, gain_folder='/home/chime/ch_acq/gains'):
        """Write digital-gain files for the boards present in ``gains``.

        Does not program the FPGAs. Channel-keyed maps are regrouped with
        :meth:`group_gains_per_board_id` first.

        Parameters:

            gains (dict): Either ``{(crate, slot, channel): (glin, glog), ...}``
                or ``{(crate, slot): {channel: (glin, glog), ...}, ...}``.

            gain_folder (str): Directory in which each board's ``save_gains``
                writes its pickle.

        Returns:

            None
        """

        # make sure we have a board-id-based gain table
        gains = self.group_gains_per_board_id(gains)
        for board_id, board_gains in gains.items():
            mb = self.fpga_array.get_iceboard(board_id)
            mb.save_gains(gains=board_gains, folder=gain_folder)

    async def get_gains_async(self, bank=0, use_cache=True):
        """Return the digital gains programmed in ``bank`` for every channel.

        Parameters:

            bank (int): Gain bank to read.

            use_cache (bool): If True, allow the software-cached tables
                (much faster than reading back from the FPGAs).

        Returns:

            dict: ``{chan_id: (glin, glog), ...}`` where:

                - ``chan_id`` is ``(crate, slot, channel)`` from
                  ``mb.get_id(ch)``
                - ``glin`` is an array of 1024 ``(int16 + 1j*int16)`` linear
                  gains (or the full floating-point table if that mode is on)
                - ``glog`` is the post-scaler (extra gain ``2**glog``); ``-1``
                  means floating-point gains and ``glog`` is unused
        """
        gains = {}
        for mb in self.ib:
            for ch, ch_gains in mb.get_gains(bank=bank, use_cache=use_cache):
                gains[mb.get_id(ch)] = ch_gains
            await asyncio.sleep(0)
        return (gains)

    async def get_gain_timestamps_async(self, bank=0):
        """Return the unix time at which each channel's gains were last set.

        Parameters:

            bank (int): Gain bank whose timestamps are read.

        Returns:

            dict: ``{chan_id: timestamp, ...}`` where ``chan_id`` is
            ``(crate, slot, channel)`` and ``timestamp`` is a ``time.time()``
            value, or ``None`` if that channel's gains were never set.
        """
        timestamps = {}
        for mb in self.ib:
            for ch, timestamp in mb.get_gain_timestamps(bank=bank):
                timestamps[mb.get_id(ch)] = timestamp
            await asyncio.sleep(0)
        return (timestamps)

    async def set_gains_async(self, gains, bank=-1, when='now', gain_timestamps=None):
        """Set on-FPGA post-FFT digital gains for the given boards/channels.

        Channel-keyed maps are regrouped with :meth:`group_gains_per_board_id`.
        Gains are written with ``when=None`` on each board (no per-board bank
        switch). If this method's ``when`` is not ``None``, one array
        :meth:`switch_gains_async` then activates the bank.

        Parameters:

            gains (dict): Either:

                - ``{board_id: {chan: (glin, glog), ...}, ...}``
                - ``{chan_id: (glin, glog), ...}``

                where ``board_id`` is ``(crate, slot)``, ``chan_id`` is
                ``(crate, slot, channel)``, ``glin`` is 1024 complex linear
                gains, and ``glog`` is the post-scaler (gain ``2**glog``).

            bank (int): Bank to write. ``-1`` or ``None`` writes the inactive
                bank (which can later be activated with
                :meth:`switch_gains_async`).

            when (int, str or None): When to make the new table active:

                - ``'now'`` or a negative integer: activate immediately.
                - ``None``: write the bank but do not switch.
                - a non-negative integer: activate at that frame number.

            gain_timestamps (dict or float or None): Unix timestamps with the
                same key layout as ``gains``, or a single timestamp applied to
                every channel. ``None`` means "now" on each board.

        Returns:

            None
        """
        # make sure we have a board-id-based gain table
        gains = self.group_gains_per_board_id(gains)

        # format the timestamps dictionary identically to gains
        if isinstance(gain_timestamps, dict):
            gain_timestamps = self.group_gains_per_board_id(gain_timestamps)

        # set the gains for each board
        for board_id, g in gains.items():
            gain_timestamp = gain_timestamps[board_id] if isinstance(gain_timestamps, dict) else gain_timestamps
            mb = self.fpga_array.get_iceboard_from_id(board_id)
            self.logger.debug(f'{self.fpga_array!r}: Setting digital gains for (crate,slot)={board_id} ({mb.get_formatted_id()})')
            mb.set_gains(gain=g, bank=bank, when=None, gain_timestamp=gain_timestamp)
            await asyncio.sleep(0)

        if when is not None:
            await self.switch_gains_async(bank=bank, when=when)

    def group_gains_per_board_id(self, channel_based_gains):
        """Convert a channel-id gain table into a board-id gain table.

        Parameters:

            channel_based_gains (dict): ``{channel_id: gains, ...}`` where
                ``channel_id`` is a 3-element ``(crate, slot, channel)``
                tuple, or already a board-id table.

        Returns:

            dict: ``{board_id: {channel: gains, ...}, ...}``. Keys that are
            not 3-element tuples/lists are copied unchanged, so a board-id
            table or a mixed dict can be passed through.
        """
        board_based_gains = {}
        for id_, gains in channel_based_gains.items():
            if isinstance(id_, (tuple, list)) and len(id_) == 3:
                board_based_gains.setdefault(tuple(id_[:-1]), {})[id_[-1]] = gains
            else:
                board_based_gains[id_] = gains
        return board_based_gains

    def get_next_gain_bank(self):
        """Return the currently unused scaler gain bank of the first channel.

        Reads channel 0 of the first board only. The caller must ensure no
        gain switch happens between this call and the subsequent write.

        Returns:

            int: Inactive bank number (0 or 1) of that first channel, not a
            per-channel map.
        """
        return self.ib[0].get_next_gain_bank()[0]

    async def switch_gains_async(self, bank=-1, when='now'):
        """Switch every channelizer to gain ``bank`` at time ``when``.

        Parameters:

            bank (int): Target bank. ``-1`` or ``None`` selects each
                channelizer's currently unused bank.

            when (str, int or None): ``'now'`` switches immediately; ``None``
                does not switch; an integer is the frame number at which the
                switch occurs (synchronized gain switching).

        Returns:

            None
        """
        self.ib.switch_gains(bank=bank, when=when)



   #####################################################
    # Data receiver management methods
    #####################################################

    async def get_data_socket_async(self, port_number=None):
        """Open one UDP data socket and program every board to send to it.

        Uses board 0 to bind (or reuse) the socket, then
        ``set_data_socket_async`` on every board so the whole array shares
        one host UDP port. Prefer :meth:`get_packet_receiver_async` /
        :meth:`get_data_receiver_async` for capture.

        Parameters:

            port_number (int or None):

                - ``None``: bind the destination port currently programmed in
                  the FPGA; if that port is 0, behave as ``port_number=0``.
                - ``0``: bind an OS-assigned port and program it into the FPGA.
                - non-zero: bind that port (raises if already in use).

        Returns:

            socket.socket: The shared UDP socket.
        """
        sock = await self.ib[0].get_data_socket_async(port_number=port_number)
        for b in self.ib:
            await b.set_data_socket_async(sock)
        return sock

    def get_tcp_data_sockets(self, port_number=None, rcvbuf=8 * 1024 * 1024, force=False):
        """Return TCP sockets connected to each board's data listen port.

        Delegates to each board's :meth:`~chFPGA.get_tcp_data_socket` (cached there).
        The board listens on a fixed well-known port; no TCPipe register is programmed.

        Parameters
        ----------
        port_number : int or None
            Board listen port. ``None`` uses :attr:`TCPipe.DEFAULT_TCP_DATA_PORT`.
        rcvbuf : int
            ``SO_RCVBUF`` size so the kernel can absorb bursts between
            ``receive_packets`` / process calls.
        force : bool
            If True, discard cached sockets and reconnect (needed after control
            TCP reconnect closes DATA_TCP on the board).

        Returns
        -------
        list[socket.socket]
            One connected TCP socket per board in ``self.ib`` order.
        """
        return [
            b.get_tcp_data_socket(port_number=port_number, rcvbuf=rcvbuf, force=force)
            for b in self.ib
        ]

    async def get_packet_receiver_async(
        self, port_number=None, *, tcp=None, rcvbuf=8 * 1024 * 1024, force=False
    ):
        """Return the shared packet receiver (UDP or TCP), creating it if needed.

        Only one transport is active. If a receiver already exists, it is reused
        unless ``tcp`` explicitly requests the other transport, in which case a
        ``RuntimeError`` is raised.

        Parameters:
            port_number (int or None): For UDP: ``None`` opens the FPGA
                destination port (or behaves as ``0`` if that port is zero);
                ``0`` binds a random OS port and programs it into the FPGA;
                non-zero binds that port (raises if already in use). For TCP:
                board listen port (``None`` uses the default).
            tcp (bool or None): If ``True``, use
                :class:`~pychfpga.common.tcp_packet_receiver.TCPPacketReceiver`.
                If ``False``, use
                :class:`~pychfpga.common.udp_packet_receiver.UDPPacketReceiver`.
                If ``None`` (default), reuse the existing receiver's
                transport, or UDP if none exists.
            rcvbuf (int): TCP ``SO_RCVBUF`` when ``tcp=True``.
            force (bool): If True, discard the cached receiver (and reconnect
                TCP data sockets). Needed after control TCP drops, which
                closes DATA_TCP on the board.

        Returns:
            UDPPacketReceiver or TCPPacketReceiver: Shared packet receiver.
        """
        if force:
            close = getattr(self._packet_receiver, "close", None)
            if callable(close):
                close()  # stop the background rx thread before dropping a UDP receiver
            self._packet_receiver = None
        if self._packet_receiver is not None:
            have_tcp = isinstance(self._packet_receiver, TCPPacketReceiver)
            if tcp is not None and have_tcp != bool(tcp):
                have = "TCP" if have_tcp else "UDP"
                want = "TCP" if tcp else "UDP"
                raise RuntimeError(
                    f"{self!r}: packet receiver is already {have}, cannot switch to {want}"
                )
            return self._packet_receiver

        want_tcp = bool(tcp)
        if want_tcp:
            socks = self.get_tcp_data_sockets(
                port_number=port_number, rcvbuf=rcvbuf, force=True
            )
            self._packet_receiver = TCPPacketReceiver(socks)
        else:
            sock = await self.get_data_socket_async(port_number=port_number)
            self._packet_receiver = UDPPacketReceiver(sock)
        return self._packet_receiver

    async def get_data_receiver_async(self, port_number=None, tcp=None, force=False, **kwargs):
        """Returns a raw data receiver for the whole array.

        A packet receiver and associated socket(s) will be created if none
        already exist, otherwise the existing ones will be reused.
        The correlator packet receiver and the raw data receiver share the same
        packet receiver (UDP or TCP — not both).

        The receiver binds ``config_getter=self.get_raw_data_config`` at first
        creation; later calls reuse the receiver and refresh config via the getter
        on each ``process_udp_packets``.

        Parameters:
            port_number (int): Port to use if a socket has not already been
                allocated. For UDP (``tcp=False``): ``None`` opens the FPGA
                destination port (or behaves as ``0`` if that port is zero);
                ``0`` binds a random OS port and programs it into the FPGA;
                non-zero binds that port (raises if already in use). For TCP
                (``tcp=True``): board listen port; ``None`` uses the default
                TCPipe TCP data port.
            tcp (bool or None): If ``True``, use TCP
                (:class:`~pychfpga.common.tcp_packet_receiver.TCPPacketReceiver`).
                If ``False``, use UDP
                (:class:`~pychfpga.common.udp_packet_receiver.UDPPacketReceiver`).
                If ``None`` (default), reuse the existing packet receiver's
                transport, or UDP if none exists.
            force (bool): If ``True``, discard cached packet/data receivers
                and reconnect TCP data sockets (call on array reinit).
            **kwargs: Forwarded to
                :class:`~pychfpga.common.raw_frame_receiver.RawFrameReceiver`
                when creating the receiver (e.g. ``verbose``,
                ``ring_buffer_size``).

        Returns:
            RawFrameReceiver: Array raw-data receiver.
        """
        if force:
            self._data_recv = None
            self._packet_receiver = None

        if not self._data_recv:
            pr = await self.get_packet_receiver_async(port_number=port_number, tcp=tcp, force=force)
            self._data_recv = RawFrameReceiver(
                pr, config_getter=self.get_raw_data_config, **kwargs
            )
        else:
            # Ensure an existing packet receiver matches the requested transport.
            await self.get_packet_receiver_async(port_number=port_number, tcp=tcp)
            self._data_recv.current_row = 0
        return self._data_recv



    async def get_corr_receiver_async(self, port_number=None, tcp=None, force=False, **kwargs):
        """Return a correlator frame receiver for the whole array.

        A packet receiver and associated socket will be created if none already
        exist, otherwise the existing ones will be reused. The correlator and
        raw data receivers share the same packet receiver.

        The receiver binds ``config_getter=self.get_corr_data_config`` at first
        creation; later calls reuse the receiver and refresh config via the
        getter on each packet-processing call.

        Parameters:
            port_number (int or None): Port for the packet receiver if one
                has not already been allocated. ``None`` uses the FPGA
                destination port (or behaves as ``0`` if that is zero);
                ``0`` binds a random OS port and programs it into the FPGA;
                non-zero binds that port (raises if already in use).
            tcp (bool or None): If ``True``, use TCP. If ``False``, use UDP.
                If ``None`` (default), reuse the existing packet receiver's
                transport, or UDP if none exists.
            force (bool): If ``True``, discard cached correlator/packet
                receivers and reconnect.
            **kwargs: Forwarded to
                :class:`~pychfpga.common.ucorr_frame_receiver.UCorrFrameReceiver`
                when creating the receiver (e.g. ``verbose``, ``pre_func``).

        Returns:
            UCorrFrameReceiver: Array correlator receiver.
        """
        if force:
            self._corr_recv = None
            self._packet_receiver = None

        if not self._corr_recv:
            pr = await self.get_packet_receiver_async(
                port_number=port_number, tcp=tcp, force=force
            )
            self._corr_recv = UCorrFrameReceiver(
                pr, config_getter=self.get_corr_data_config, **kwargs
            )
        else:
            await self.get_packet_receiver_async(port_number=port_number, tcp=tcp)
            self._corr_recv.current_row = 0
        return self._corr_recv

    async def get_system_config_async(self, basic=False):
        """Concurrently get system-configuration info for each FPGA.

        Parameters:

            basic (bool): Forwarded to each board's ``get_system_config_async``.
                If True, a reduced subset is returned.

        Returns:

            dict: ``{board_id: config, ...}`` keyed by ``mb.get_id()``.
        """
        configs = await self.ib.get_system_config_async(basic=basic)
        return dict(zip(self.ib.get_id(), configs))

    async def get_funcgen_buffer_async(self):
        """Return the function-generator waveform buffers for every channel.

        Walks crates (``self.ic``) so only boards that are in a crate are
        included. The buffer matches the function-generator output if that
        source is selected, or the channelizer output if FFT and scaler are
        bypassed.

        Returns:

            dict: Merged per-board ``get_funcgen_buffer()`` maps (typically
            keyed by channel id).
        """
        ch_out = {}
        for cr in self.ic:
            for mb in cr.slot.values():
                ch_out.update(mb.get_funcgen_buffer())
                await asyncio.sleep(0)
        return ch_out

    #####################################################
    # FPGA metrics methods
    #####################################################


    def reset_fpga_stats(self):
        """Reset FFT overflow counters on every board.

        Returns:

            None
        """
        for mb in self.ib:
            mb.reset_fft_overflow_count()

    # def get_monitoring_info(self):
    #     return self.ib.index_by(lambda mb: mb.get_id()).get_status()

    async def get_arm_metrics_async(self, metrics):
        """Append ARM-accessible backplane and motherboard metrics to ``metrics``.

        Includes:

            - Backplane metrics, as measured from one board in each crate
            - Motherboard hardware metrics (voltages, temperatures), including
              mezzanine voltage/current
            - FPGA UDP MMI metrics, for boards that have an ``fpga`` object

        Parameters:

            metrics (Metrics): Object to extend in place.

        Returns:

            None. The caller’s ``metrics`` object is mutated.
        """

        # Crate metrics
        self.logger.debug(f'{self.fpga_array!r}: Getting Motherboard backplane hardware metrics')
        for cr in self.ic:
            slot, mb = list(cr.slot.items())[0]
            metrics += await mb.get_backplane_metrics_async()

        # Motherboard metrics
        self.logger.debug(f'{self.fpga_array!r}: Getting Motherboard temperature & power supply metrics')
        m = await self.ib.get_metrics_async()
        metrics += m
        self.logger.debug(f'{self.fpga_array!r}: Got {len(m)} Motherboard temperature & power supply metrics')
        fpga_ibs = Ccoll(mb for mb in self.ib if mb.fpga)
        if fpga_ibs:
            metrics += await fpga_ibs.get_fpga_udp_metrics_async()

    async def get_fpga_metrics_async(self, metrics, reset=True):
        """Append FPGA firmware status metrics across the array to ``metrics``.

        Includes corner-turn link (BP shuffle) stats, crossbar stats, and
        channelizer stats (all over the FPGA UDP/MMI path). If the last
        successful sync was IRIG-B, also adds per-board frame-time deltas
        relative to board 0 and to the stored sync timestamps.

        Parameters:

            metrics (Metrics): Object to extend in place.

            reset (bool): If True, reset BP-shuffle and channelizer stat
                counters after reading. Crossbar stats are always read with
                ``reset=False``.

        Returns:

            Metrics: The same ``metrics`` object, after the FPGA samples
            have been added.
        """

        self.logger.debug(f'{self.fpga_array!r}: Getting corner-turn links metrics (over FPGA UDP link)')
        metrics += await self.ib.get_bp_shuffle_metrics_async(reset=reset)
        self.logger.debug(f'{self.fpga_array!r}: Getting corner-turn crossbars metrics (over FPGA UDP link)')
        metrics += await self.ib.get_crossbar_metrics_async(reset=False)
        self.logger.debug(f'{self.fpga_array!r}: Getting channelizer metrics (over FPGA UDP link)')
        metrics += await self.ib.get_channelizer_metrics_async(reset=reset)
        self.logger.debug(f'{self.fpga_array!r}: Finished gathering FPGA/backplane metrics')

        # Backplane GTX
        # Errors, signal level

        # Command errors

        sync_timestamps = self._sync_timestamps
        if sync_timestamps:
            for i, mb in enumerate(self.ib):
                try:
                    fn, ts = await mb.capture_frame_time_async(format='raw')
                    if not i:
                        fn0, ts0 = (fn, ts)
                    crate, slot = mb.get_id()
                    slot = mb.slot - 1
                    metrics.add('fpga_time_delta', ts.nano - ts0.nano, crate=crate, slot=slot)
                    metrics.add('fpga_frame_number_delta', fn - fn0, crate=crate, slot=slot)
                    metrics.add('fpga_time_error',
                                ts.nano - (sync_timestamps[i].nano + fn * 2560),
                                crate=crate, slot=slot)
                    metrics.add('fpga_sync_time_delta',
                                sync_timestamps[i].nano - sync_timestamps[0].nano,
                                crate=crate, slot=slot)
                    metrics.add('fpga_sync_time_integer_second_offset',
                                (sync_timestamps[i].nano % 1000000000) - 1000000000,
                                crate=crate, slot=slot)
                except RuntimeError:
                    self.logger.error(f'{mb!r}: Timeout while capturing frame time')

        return metrics

    
    #####################################################
    # Configuration management methods
    #####################################################

    def update_config(self):
        """Refresh each board's firmware config tree and return board configs.

        Returns:

            list: Each board's ``config`` State after ``chFPGA.update_config``.
        """
        return [mb.update_config() for mb in self.ib]

    def get_raw_data_config(self):
        """Build (or reuse) the array-wide raw-capture config State.

        Aggregates every board's firmware config. The result is cached and
        returned unchanged until any board's ``fpga.config`` has changed
        (``has_changed`` / ``ack`` with context ``('get_raw_data_config', self)``).
        Static fields (sample size, frame length, …) are filled only on first
        build and must be common across the array.

        All boards must use the same capture engine (``UCAP`` or ``PROBER``).
        Stream IDs across every active channel of every board must be unique
        or this raises ``ValueError`` (the host demuxes bursts by stream ID).

        Returns:

            State: Array-wide raw config, including ``source``,
            ``channels``, ``stream_ids``, ``frames_per_burst``,
            ``subframes_per_frame``, ``target_mode``, ``fft_bin_map``, and
            ``capture_format[0/1]`` (funcgen vs scaler) and, on an ICE
            ``CT_LEVEL`` 3 board, ``capture_format[2]`` (corner-turn probe).
            ``fmt_is_ct_data`` is true only on that third entry. ``packet_cookie``
            is taken from the boards.

        Raises:

            RuntimeError: No boards in the array.
            ValueError: Mixed/unknown capture engine, or duplicate stream IDs.
        """
        boards = list(self.ib)
        if not boards:
            raise RuntimeError(
                f'{self.fpga_array!r}: No boards available to build data config'
            )

        ctx = ('get_raw_data_config', self)  # Context object used to identify config changes that matter to this function  

        # Return the existing config if it exists and none of the boards have changed their configs since the last time
        if self._raw_data_config is not None:  
            if not any(mb.fpga.config.has_changed(context=ctx) for mb in boards):
                return self._raw_data_config

        fpgas = [mb.fpga for mb in boards]

        # Get a list of updated configs for each board (i.e update derived parameters if needed)
        cfgs = [fpga.update_config() for fpga in fpgas]


        # If the config object doesn't exist, build one and include the static parameters that won't change (so we don't have to fetch and check them every time)
        if self._raw_data_config is None:
            bytes_per_frame = State.get_common_attr(cfgs, "bytes_per_frame")
            subframes_per_frame = State.get_common_attr(cfgs, "subframes_per_frame")
            self._raw_data_config = State(
                # Static parameters that won't change (so we don't have to fetch and check them every time)
                bytes_per_sample=State.get_common_attr(cfgs, "adc_bytes_per_sample"),
                samples_per_frame=State.get_common_attr(cfgs, "samples_per_frame"),
                fft_bins_per_word=State.get_common_attr(cfgs, "fft_bins_per_word"),
                capture_bits_per_word=State.get_common_attr(cfgs, "capture_bits_per_word"),
                bytes_per_frame=bytes_per_frame,
                subframes_per_frame=subframes_per_frame,
                bytes_per_subframe=bytes_per_frame // subframes_per_frame,
                packet_cookie=list(State.get_common_attr(cfgs, "packet_cookie")),
            )
            self._raw_data_config.capture_format = [State(), State()]

        # Get the type of capture engine so we will gather the raw data config based in the correct parameters. Must be the same for every board.
        capture_engine = State.get_common_attr(cfgs, "capture_engine")

        if capture_engine == "UCAP":
            ucap_cfgs = [fpga.config.ucap for fpga in fpgas]
            active_channels = State.get_common_attr(ucap_cfgs, "active_channels")  # list of *local* channel numbers handled by each board (e.g CRS boards all have channels 0-7) 


            # Build a list of stream ids for every channel of every board. Unicity is required for proper capture and is tested below.
            stream_ids = tuple(
                int(fpga.config.ucap.ucap_stream_ids[ch])
                for fpga in fpgas for ch in active_channels
            )
            # Test for unicity of stream ids across the array.
            if len(stream_ids) != len(set(stream_ids)):
                raise ValueError(f'{self.fpga_array!r}: stream_ids are not unique across the array: {list(stream_ids)}')

            # Gather all engine-dependent parameters in a dict
            source=State.get_common_attr(ucap_cfgs, "source")
            frames_per_burst=State.get_common_attr(ucap_cfgs, "frames_per_burst")
            target_mode=State.get_common_attr(ucap_cfgs, "mode")
            # Static on UCAP, but the local is only set when building _raw_data_config
            # the first time. Re-read it on every refresh so update() below is bound.
            subframes_per_frame = State.get_common_attr(ucap_cfgs, "subframes_per_frame")

        elif capture_engine == "PROBER":
            active_channels = State.get_common_attr(cfgs, "channels")
            prober_cfgs = [
                fpga.chan[ch].PROBER.config
                for fpga in fpgas for ch in active_channels
            ]
            stream_ids = tuple(
                int(fpga.chan[ch].PROBER.config.stream_id)
                for fpga in fpgas for ch in active_channels
            )

            source=State.get_common_attr(prober_cfgs, "source")
            frames_per_burst=State.get_common_attr(prober_cfgs, "frames_per_burst")
            subframes_per_frame=State.get_common_attr(prober_cfgs, "subframes_per_frame")
            target_mode=0

        else:
            raise ValueError(
                f'{self.fpga_array!r}: unknown capture engine {capture_engine!r}'
            )

        funcgen_cfgs = [ch.FUNCGEN.config for fpga in fpgas for ch in fpga.get_channelizers(active_channels)]
        scaler_cfgs = [ch.SCALER.config for fpga in fpgas for ch in fpga.get_channelizers(active_channels)]
        fft_cfgs = [ch.FFT.config for fpga in fpgas for ch in fpga.get_channelizers(active_channels)]

        fft_bin_map=State.get_common_attr(fft_cfgs, "bin_ix_map") # get the reverse bin map, i.e. indices into a native bins vector

        # Test for unicity of stream ids across the array.
        if len(stream_ids) != len(set(stream_ids)):
            raise ValueError(f'{self.fpga_array!r}: stream_ids are not unique across the array: {list(stream_ids)}')

        self._raw_data_config.update(
            source=source,
            channels=active_channels,
            stream_ids=stream_ids,
            frames_per_burst=frames_per_burst, 
            subframes_per_frame=subframes_per_frame,
            target_mode=target_mode,
            fft_bin_map=fft_bin_map,
        )
        self._raw_data_config.capture_format[0] = State.get_common_attr(funcgen_cfgs, "capture_format")
        self._raw_data_config.capture_format[1] = State.get_common_attr(scaler_cfgs, "capture_format")

        # If we have a CT capture engine, set the format flag on that engien so we know how to decode it
        if len(self._raw_data_config.capture_format) >= 2:
            ct_cfgs = [fpga.CT_PROBER.config for fpga in fpgas]
            self._raw_data_config.capture_format[2]=State.get_common_attr(ct_cfgs, "capture_format")


        # Acknowledge the configs of each board so we don't trigger an update if none of the boards have changed their configs since the last time
        for mb in boards:
            mb.fpga.config.ack(context=ctx)
        return self._raw_data_config

    def get_corr_data_config(self):
        """Assemble array-wide correlator config from firmware CORR States.

        Cached until any board's CORR config ``has_changed`` for context
        ``('get_corr_data_config', self)``. Common fields (``NCHAN``,
        ``NBINS``, ``bin_map``, ``prod_map``, ``firmware_integ_period``, …)
        must match across boards. ``READOUT_BINS`` is per-board (one clkbin
        per local correlator) and is concatenated in board order so its
        length matches the array-wide ``NCORRS`` axis of ``bin_map``.

        Returns:

            State or None: Aggregated correlator config, or ``None`` if no
            board has a CORR engine.

        Raises:

            RuntimeError: No boards in the array.
            ValueError: Some but not all boards have a correlator.
        """
        boards = list(self.ib)
        if not boards:
            raise RuntimeError(
                f'{self.fpga_array!r}: No boards available to build data config'
            )

        ctx = ('get_corr_data_config', self)
        corrs_cfgs = [mb.fpga.CORR.update_config() for mb in boards if mb.fpga.CORR is not None]

        if not corrs_cfgs:
            return None
        if len(corrs_cfgs) != len(boards):
            raise ValueError(f'{self.fpga_array!r}: correlator is missing on some boards')

        # If the config object doesn't exist, build an empty one
        if self._corr_data_config is None:
            self._corr_data_config = State()

        # Return the existing config if it exists and none of the boards have changed their configs since the last time
        if not any(cfg.has_changed(context=ctx) for cfg in corrs_cfgs):
            return self._corr_data_config

        # List of fields that must be identical on every board
        corr_fields = (
            "NCHAN",
            "NCLK",
            "NDECIM",
            "CT_LEVEL",
            "NCORRS",
            "NPROD_PER_CMAC",
            "NCMAC",
            "NBINS",
            "NPROD_PER_ARRAY",
            "firmware_integ_period",
            "bin_map",
            "prod_map",
            "conj_map",
        )
        # Update config with the common attributes
        self._corr_data_config.update(**{
            attr: State.get_common_attr(corrs_cfgs, attr)
            for attr in corr_fields})
        # COncatenate the readout bins for each board into a single tuple
        self._corr_data_config.update(
            READOUT_BINS=tuple(rb for cfg in corrs_cfgs for rb in cfg.READOUT_BINS)
        )
        # Acknowledge the configs of each board so we don't trigger an update if none of the boards have changed their configs since the last time
        for mb in boards:
            mb.fpga.CORR.config.ack(context=ctx)
        return self._corr_data_config


    #####################################################
    # Firmware update methods
    #####################################################

    def _update_arm_firmware(self, image_filename, power_cycle=True):
        """Update ARM SD-card firmware on every board.

        The image must be bzip2-compressed. Boards typically need a power
        cycle afterwards (the power-cycle path here is commented out).

        Parameters:

            image_filename (str): Path to the compressed ARM image.

            power_cycle (bool): Unused (power-cycle code is commented out).

        Returns:

            None
        """
        self.ib._update_arm_firmware(image_filename, delay=120)
        # if self.ps and power_cycle:
        #     self.ps.unlock()
        #     self.ps.power_cycle(delay=4)

    #####################################################
    # Test method
    #####################################################

    def set_test_pattern(self):
        """Drive every crate board with a slot/channel identity test pattern.

        For each board in each crate, channel ``ch`` is programmed as function
        ``'ab'`` with ``a = (slot - 1) << 4`` and ``b = ch << 4``, then the
        data source is set to ``'funcgen'``. Ice-style 16-channel layout.
        Requires ``self.ic`` (crate map).

        Returns:

            None
        """
        for cr in self.ic:
            for (slot, mb) in cr.slot.items():
                for ch in range(16):
                    mb.set_funcgen_function('ab', a=(slot - 1) << 4, b=ch << 4, channels=[ch])
                mb.set_data_source('funcgen')

    def test_sync(self):
        """Exercise backplane SYNC from the configured master board.

        Sets every board's REFCLK sync source to ``'bp'``, then ten times
        pulses a local sync on ``_sync_master`` and checks that each board's
        SYNC counter advanced by 1. Prints PASS/FAIL per board and overall.

        Prerequisites:

            :meth:`set_sync_method_async` must have set a master
            (``self._sync_master``). Boards must have REFCLK sync logic.

        Returns:

            None
        """
        c = list(self.ib)
        sync_board = self._sync_master

        sync_ctr = np.zeros(len(c), dtype=int)
        for i, bb in enumerate(c):
            bb.REFCLK.set_sync_source('bp')  # bb.REFCLK.SLAVE=1
            sync_ctr[i] = bb.REFCLK.SYNC_CTR

        fail = 0
        for test_number in range(10):
            print(f'Trial # {test_number + 1}: Sending SYNC pulse from Slot {sync_board.slot:02i} (Motherboard SN{sync_board.serial})')
            sync_board.REFCLK.local_sync()
            for i, bb in enumerate(c):
                new_sync_ctr = bb.REFCLK.SYNC_CTR
                diff = (new_sync_ctr - sync_ctr[i]) & 0xF
                sync_ctr[i] = bb.REFCLK.SYNC_CTR
                fail += bool(diff != 1)
                print(f'    Slot {bb.slot:02d} (Motherboard SN{bb.serial}): Sync counter = {new_sync_ctr:2d}, diff = {diff:2d} => {("FAILED!", "PASS")[diff == 1]}')
            time.sleep(0.2)
        if fail:
            print('SYNC Test has FAILED!')
        else:
            print('SYNC Test has PASSED!')


    def print_fmc_power(self):
        """Print FMCA/FMCB 12 V, 3.3 V, and VADJ voltage/current/power per board.

        Returns:

            None
        """
        sensor_list = ['FMCA_12V0', 'FMCA_3V3', 'FMCA_VADJ',
                       'FMCB_12V0', 'FMCB_3V3', 'FMCB_VADJ']

        for b in self.ib:
            for sensor in sensor_list:
                (voltage, current, power) = b.hw.get_power(sensor)[sensor]
                if voltage is not None:
                    print(f'{voltage:0.1f}V@{current:0.2f}A={power:0.1f}W ', end=' ')
                else:
                    print('None                 ', end=' ')
            print()

    def print_iceboard_temperatures(self):
        """Print FPGA-die temperature for every board as a crate/slot table.

        Returns:

            None
        """
        sensor = self.ib[0].TEMPERATURE_SENSOR.MB_FPGA_DIE
        self.print_iceboard_table(
            lambda mb: f'{mb.get_motherboard_temperature(sensor):3.1f}',
            row_labels='FPGA Die Temp')

    def print_iceboard_power(self):
        """Print total motherboard power (W) for every board as a crate/slot table.

        Returns:

            None
        """
        self.print_iceboard_table(lambda mb: f'{mb.get_total_power():0.1f}')


    def print_adc_frequencies(self):
        """Print the frequency of the clocks coming out of ADC chips 0, 4, 8, 12.

        Returns:

            None
        """

        for mb in self.ib:
            freqs = ', '.join(f"{mb.FreqCtr.read_frequency(f'ADC_CLK{c}', gate_time=0.001) / 1000000.0:7.3f}" for c in (0,4,8,12))
            print(f'{mb!r}: {freqs}')

    def print_temperatures(self):
        """Print FPGA SYSMON temperature for every board, sorted by slot.

        Returns:

            None
        """
        t = [(b.slot, b.serial, b.SYSMON.temperature()) for b in self.ib]
        t.sort()
        for (slot, serial_number, fpga_temp) in t:
            print(f'Slot {int(slot):2} (SN{serial_number}): FPGA {fpga_temp:2.1f} C')

    def print_iceboard_info(self):
        """Gather motherboard metrics and print them as a crate/slot table.

        Returns:

            None
        """
        if not self.ib:
            print('There are no IceBoards in the array')
            return
        info = [i for (i, _) in self.ib._get_motherboard_metrics_async()]
        keys = '\n'.join(info[0].keys())
        data = {mb: ('\n'.join(info[i].values())) for i, mb in enumerate(self.ib)}
        self.print_iceboard_table(data, row_labels=keys)

    def print_net_length_map(self):
        """Print per-crate backplane RX net-length matrices in metres.

        For each crate, cells are TX slot (rows) vs RX slot (columns), using
        ``get_matching_tx`` / ``get_rx_net_length``. Requires ``self.ic``.

        Returns:

            None
        """
        for cr in self.ic:
            err_map = [[None] * cr.NUMBER_OF_SLOTS for _ in range(cr.NUMBER_OF_SLOTS)]
            for slot, mb in cr.slot.items():
                for lane in range(16):
                    rx_lane = (slot, lane)
                    tx_lane = cr.get_matching_tx(rx_lane)
                    err_map[rx_lane[0] - 1][tx_lane[0] - 1] = f'{cr.get_rx_net_length(rx_lane) / 1000:0.1f}'
            row_labels = [f'Tx S{int(mb.slot):02} SN{mb.serial}' for mb in list(cr.slot.values())]
            col_labels = [f'Rx S{int(mb.slot):02}\nSN{mb.serial}' for mb in list(cr.slot.values())]
            corner_label = f'{cr.get_string_id()}\nCrate #{cr.crate_number}'
            self.print_table(err_map, row_labels=row_labels, col_labels=col_labels, corner_label=corner_label)

    def print_iceboard_qsfp(self):
        """Print QSFP serial numbers for every board as a crate/slot table.

        Returns:

            None
        """
        self.print_iceboard_table(lambda mb: '\n'.join(mb.hw.qsfp.get_serial_number().map(str)), grid=1)

    def print_frame_info(self):
        """Print shuffle-lane stream ID and frame-LSB captured in HEADER_CAPTURE.

        This is an Ice-era debug helper: it programs ``HEADER_CAPTURE_*``
        MMIO on *this* object (not on each motherboard) and dumps 16 lanes.
        It is not a general array method.

        Returns:

            None
        """
        ts = []
        sid = []

        # get 8 bits of stream ID
        self.HEADER_CAPTURE_DATA_SEL = 0
        self.HEADER_CAPTURE_EN = 1
        self.HEADER_CAPTURE_EN = 0
        for i in range(16):
            self.HEADER_CAPTURE_LANE_SEL = i
            sid.append(self.HEADER_CAPTURE_DATA)

        # get lsb of timestamp
        self.HEADER_CAPTURE_DATA_SEL = 1
        self.HEADER_CAPTURE_EN = 1
        self.HEADER_CAPTURE_EN = 0
        for i in range(16):
            self.HEADER_CAPTURE_LANE_SEL = i
            ts.append(self.HEADER_CAPTURE_DATA)

        for i in range(len(ts)):
            print(f'Lane {int(i):02}: Stream ID=0x{sid[i]:02x}, Frame = 0x{ts[i]:02x} (delta = {int(ts[i] - ts[0])})')

    def plot_crate_temperatures(self, figure_number=1):
        """Plot FPGA-die temperature vs slot for every crate.

        Parameters:

            figure_number (int): Matplotlib figure number to reuse. Default 1.

        Returns:

            None
        """
        sensor = self.ib[0].TEMPERATURE_SENSOR.MB_FPGA_DIE

        plt.figure(figure_number)
        plt.clf()
        # plt.hold(1)
        for cr in self.ic:
            mb = Ccoll(cr.slot.values())
            t = mb.get_motherboard_temperature(sensor)
            s = mb.slot
            avg_temp = np.average(t)
            h = plt.plot(s, t, label=cr.get_string_id())
            plt.plot([min(s), max(s)], [avg_temp] * 2, ':', color=h[0].get_color(), lw=2)
            print(f'{cr.get_string_id()}: {avg_temp:f}degC')
        plt.legend(loc='best')
        plt.xlabel('Slot number')
        plt.ylabel('FPGA Die temperature [degC]')
        plt.grid(1)
        plt.title('FPGA die temperatrures for multiple crates')


