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
    """Array-wide methods for chFPGA firmware arrays."""

    def __init__(self, fpga_array):
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

    def get_sync_timestamp(self):
        """Frame-0 IRIG-B time after the last successful sync, or None."""
        return self._sync_timestamp

    def get_sync_timestamps(self):
        """Per-board timestamps from the last sync, or None."""
        return self._sync_timestamps

    def get_sync_start_time(self):
        """Trigger time(s) programmed during the last IRIG-B sync."""
        return self._sync_start_time

    def get_sync_method(self):
        """Current array sync method string, or None if unset."""
        return self._sync_method

    def get_operational_mode(self):
        """Current operational mode string, or None if unset."""
        return self._mode

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
        """Initialize chFPGA firmware after the hardware map is complete."""
        self.ib = self.fpga_array.ib
        self.ic = getattr(self.fpga_array, 'ic', None)

        self._max_sync_time_difference = max_sync_time_difference
        self._mode = mode
        self._tx_power = tx_power

        if not self.ib:
            return

        if mode:
            if init is None:
                init = 3
            if prog is None:
                prog = 2
        else:
            init = 0

        # try programming the FPGA and establishing communicaiton with it up to 3 times
        max_trials = 3
        for trial in range(1, max_trials + 1): 

            if init >= 1:
                self.logger.info(f'{self.fpga_array!r}: Configuring FPGAs...')
                self.perf.start('FPGA programming', description='Init Level 1/prog: Upload bitstreams')
                if prog:
                    await self.ib.set_fpga_bitstream_async(mode, force=(prog > 1), bitfile_override=bitfile)
            else:
                break # nothing to do, exit the trial loop

            if init >= 2:
                self.perf.start('FPGA open', description='Init level 2: Open MMI and PLL_SYNC')
                try:
                    await self.ib.open_fpga_async(
                            udp_retries=udp_retries,
                            fpga_ip_addr_fn=fpga_ip_addr_fn,
                            interface_ip_addr=if_ip,
                            irigb_source=sync_source)
                    break  # it worked, exit the trial loop
                except IOError:
                    self.logger.warning(f'{self.fpga_array!r}: Error while initializing core firmware on trial {trial}/{max_trials}.')
                    if trial == max_trials:
                        raise IOError(
                            f'{self.fpga_array!r}: Unable to initializing FPGA core firmware '
                            f'after {trial} trials. Giving up.')
                    else:
                        prog = 2  # force reprogramming the FPGA on next trial
                        self.logger.warning(f'{self.fpga_array!r}: Reprogramming FPGA and trying again.')
            else:
                break # We skipped this step, so let's assume it worked, exit the trial loop

        # Now that we have a firmware object, update the array methods list with the methods provided by this firmware.  
        self.fpga_array.get_array_methods()

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
    

    async def sync_async(self, delay=0, check=True, align_to_seconds=True, max_trials=3, max_sync_time_difference=None):
        """Generate a SYNC event across the whole array.

        Parameters:

        delay (float or list): Extra seconds added after the chosen trigger
            base. With ``align_to_seconds`` this is applied after the next
            safe integer second (and after
            :data:`IRIG_SYNC_PIPELINE_OFFSET_S`). A list is a per-board
            offset. Default 0: do not skip extra seconds.

        check (bool): If True, the method will read the SYNC
            counters on every board to confirm that the SYNC really happened
            everywhere.

        align_to_seconds (bool): if True, the trigger base is the smallest
            integer second at least ``slack`` in the future (``slack`` from a
            dummy arm of the array), then the pipeline offset and ``delay``
            are applied. ADC frames start on that second.
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

                # elif self.sync_method == 'centralized_time_trigger':
                #     dt = self.sync_master.get_irigb_time_sync()
                #     print('Triggering SYNC at ', dt.isoformat())
                #     self.sync_master.set_irigb_trigger_time_sync(dt, delay=delay)
                #     t0 = time.time()
                #     while self.sync_master.is_irigb_before_trigger_time_sync():
                #         if time.time() - t0 > delay + 1:
                #             raise RuntimeError('Timout while waiting for the IRIG-B-based SYNC to complete')

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
                # elif self.sync_method == 'external':
                #     # Wait until one of the board sees a trig. We assume all
                #     # of them will have triggered by the time that is checked
                #     # later.
                #     t0 = time.time()
                #     while all([mb.REFCLK.SYNC_CTR == sync_ctr_before[i] for i, mb in enumerate(self.ib)]):
                #         if time.time() - t0 > 5 + 1:
                #             raise RuntimeError('Timout while waiting for the external sync signal')
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


    async def check_clock_source(self):
        """Check that crate membership and backplane clock selection agree.

        To make sure that the data acquisition and transmission will be done
        at the same rate, refuse to operate if
          - an Iceboard that is in a crate is not using the backplane clock
          - an iceboard that is not in a crate is using the backplane clock

        We do not raise an exception if we are using the backplane clock
        without crate in case the crate information is not discovered.
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
        """ Set the operational mode of the array.


        Parameters:

            mode (str): operational mode string. Platform-specific:

                - IceBoard:
                
                    - 'chan4': Corner-turn engine is bypassed and raw 4-bit data from 4
                      channelizers is sent directly to the 4 10G Ethernet links.

                    - 'chan8': Corner-turn engine is bypassed and raw 8-bit data from 8
                      channelizers is sent directly to the 8 10G Ethernet links.

                    - 'shuffle16': Acquire, channelize and shuffle data within
                      each Iceboard individually and send the data through the
                      IceBoard QSFP+ ports. There is no data shuffling between
                      boards. This is good for single board operation (or an array
                      of boards operating independently)

                    - 'shuffle128': Acquire, channelize and shuffle data within a
                      crate to create a 8-board (128-channel) correlator. The
                      shuffled data is sent through the IceBoard QSFP+ ports.
                      There is no shuffling between crates.

                    - 'shuffle256': Acquire, channelize and shuffle data within a
                      crate to create a 16-board (256-channel) correlator. The
                      shuffled data is sent through the IceBoard QSFP+ ports.
                      There is no shuffling between crates.

                    - 'shuffle512': Acquire, channelize and shuffle data between pair of
                    crates to create a 32-board (512-channel) correlator. The shuffled
                    data is sent through the IceBoard QSFP+ ports. The pairing of crates
                    is based on the crate number: Crate N and N+1 form a pair, whereas N
                    is a even number.

                    - 'corr16': The corner-turn engine is hard-wired to feed the 16 channels to the
                    internal firmware correlator.

                - CRS:
                    - 'shuffle8': Acquire and channelize data from 8 channelizers and send the data 
                      through the UPACK programmable bin packetizer and down to the 100G CGE offload link.

                    - 'corr8': The corner-turn engine is hard-wired to feed the 8 channels to the
                    internal firmware correlator.

                    - 'corr32': The corner-turn engine is hard-wired to shuffle data between 4 boards on the same backplane and feed the data to a 32-input correlator.

                    - 'corr64': The corner-turn engine is hard-wired to shuffle data between 8 boards on two backplanes with the help of SFP cables between boards,  and feed the data to a 64-input correlator.

                - ZCU111:

                    - 'corr4': The corner-turn engine is hard-wired to feed the 4 channels to the
                    internal firmware correlator.

            frames_per_packet (int): Number of frames to combine in a single
                packet. Is limited by the amount of buffering space inside the
                FPGA.

            send_flags (bool): If True, the Scaler and Frame flags will be
                sent after the data block.

            tx_power (dict): Power levels to be set on the GTXes.

            integration_period (int): (for ``corr`` modes only):
                Sets the integration period (in frames) of the firmware
                correlator.

            autocorr_only (bool): (for ``corr`` mored only. If True, only the
                autocorrelation products are sent.

            corner_turn_bad_links (list of tuples): List of corner turn engine
                outputs links [(crate, slot, links), ...] that should
                preferably NOT be assigned any frequency bins if possible.

            corner_turn_bin_priority (list of int): List of frequency bins
                that are to be assigned to the corner turn output, in order of
                priority (most desirable frequency first). The lowest priority
                bins will be assigned to the bad links as much as possible,
                depending on the constraints of the corner turn engine
                flexibility.

            corner_turn_remap_level (int): Sets the aggressivness of the remapping by
                selecting how many crossbar levels are involved. 0:no
                remapping, 1: 3rd crossbar only, 2: crossbars 2 and 3, 3: all
                crossbars.



        Notes:

            Having called get_ber() before initializing the shuffle will lead to errors!

        # data_width : Data width of each Re and Im component of the channelizer output
        # enable_gpu_link : Enables the GPU link transmission

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

    
    def set_tx_power(self, default_power=(5, 10), lane_group=None, exceptions=[], index=0):
        """ Set the power level of the corner-turn engine GTX transmitters.

        Parameters:

            default_power (int or tuple): power level to use for all gtx that
                are not exception list. An int is interpreted as a
                single-element tuple. In the case where multiple power values
                are to be specified, the value within the tuple is selected by
                `index`.

            lane_group (str): lane group for which the power is set ('pcb' or
                'qsfp').

            exceptions (list): list of the lane-specific power level exceptin, in the form:

                [((crate, slot, lane), power_tuple), ...}

            index (int): used to select which value within a power tuple, list
                or map will be used to set power.
        """
        if isinstance(default_power, int):
            default_power = (default_power, )

        exceptions = {tuple(node_id): power_tuple for node_id, power_tuple in (exceptions or [])}
        self.logger.info(f'{self.fpga_array!r}: Setting GTX power for lane group {lane_group} to power index {index}')
        self.logger.debug(f'{self.fpga_array!r}:    Default power is {default_power}')
        self.logger.debug(f'{self.fpga_array!r}:    Power exceptions are {exceptions}')

        for mb in self.ib:
            bp = mb.CT.BP_SHUFFLE
            power_tuples = [(lane, exceptions.get(mb.get_id(lane), default_power)[index])
                            for lane, gtx in enumerate(bp.get_gtx(lane_group=lane_group)) if gtx]
            self.logger.debug(f'{self.fpga_array!r}: setting Tx power for {mb} {lane_group} links')
            bp.set_tx_power(power_tuples, lane_group)

    
    def set_test_pattern(self):
        for cr in self.ic:
            for (slot, mb) in cr.slot.items():
                for ch in range(16):
                    mb.set_funcgen_function('ab', a=(slot - 1) << 4, b=ch << 4, channels=[ch])
                mb.set_data_source('funcgen')



    # ---------------------
    # -- Correlator methods
    # ---------------------

    async def get_data_socket_async(self, port_number=None):
        """Async variant of :meth:`get_data_socket`."""
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

        Parameters
        ----------
        port_number : int or None
            For UDP: 
                - If ``None``, attempts to open a socket at the destination port currently programmed in the FPGA. If that port is zero, act as if ``port_number`` =0.
                - If zero, open a socket at a random  (OS-provided) port, and set the corresponding destination port in the FPGA.
                - If non-zero, get a socket bound to the specified port. An exception will be raised if that port is already used by another program.
            For TCP: board listen port (``None`` = default).
        tcp : bool or None
            If ``True``, use :class:`~pychfpga.common.tcp_packet_receiver.TCPPacketReceiver`.
            If ``False``, use :class:`~pychfpga.common.udp_packet_receiver.UDPPacketReceiver`.
            If ``None`` (default), reuse the existing receiver's transport, or
            UDP if none exists.
        rcvbuf : int
            TCP ``SO_RCVBUF`` when ``tcp=True``.
        force : bool
            If True, discard the cached receiver (and reconnect TCP data sockets).
            Needed after control TCP drops, which closes DATA_TCP on the board.

        Returns
        -------
        UDPPacketReceiver or TCPPacketReceiver
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
        """ Returns a raw data receiver for the whole array.

        A packet receiver and associated socket(s) will be created if none
        already exist, otherwise the existing ones will be reused.
        The correlator packet receiver and the raw data receiver share the same
        packet receiver (UDP or TCP — not both).

        The receiver binds ``config_getter=self.get_raw_data_config`` at first
        creation; later calls reuse the receiver and refresh config via the getter
        on each ``process_udp_packets``.


        Parameters:

            port_number (int): Specifies the port to use if a socket has not already been allocated for the packet receiver.
                - For UDP (``tcp=False``):
                    - If ``None``, attempts to open a socket at the destination port currently programmed in the FPGA. If that port is zero, act as if ``port_number`` =0.
                    - If zero, open a socket at a random (OS-provided) port, and set the corresponding destination port in the FPGA.
                    - If non-zero, get a socket bound to the specified port. An exception will be raised if that port is already used by another program.
                - For TCP (``tcp=True``): board listen port; ``None`` uses the default TCPipe TCP data port.
            tcp (bool or None): If ``True``, use TCP
                (:class:`~pychfpga.common.tcp_packet_receiver.TCPPacketReceiver`).
                If ``False``, use UDP
                (:class:`~pychfpga.common.udp_packet_receiver.UDPPacketReceiver`).
                If ``None`` (default), reuse the existing packet receiver's
                transport, or UDP if none exists.
            force (bool): If ``True``, discard cached packet/data receivers and reconnect
                TCP data sockets (call on array reinit).
            **kwargs: Forwarded to :class:`~pychfpga.common.raw_frame_receiver.RawFrameReceiver`
                when creating the receiver (e.g. ``verbose``, ``ring_buffer_size``).

        Returns:

            :class:`~pychfpga.common.raw_frame_receiver.RawFrameReceiver`
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

    async def start_data_capture_async(self, *args, sync=None, **kwargs):
        """Starts data capture on all channelizers of all boards in the array.

        After per-board setup, pulses ``UCAP.USER_RESET`` across the array so
        capture engines share the same burst-phase on the (already IRIG-aligned)
        frame counter. Without that, array ``stream_ids`` never co-complete on
        one timestamp and ``read_raw_frames`` can stall.

        Args:
            sync: If true, run a full array ``sync_async()`` after capture setup.
                Prefer the UCAP reset alignment for raw capture; a full sync
                while UCAP is flooding the host can time out MMI.
        """
        for b in self.ib:
            b.start_data_capture(*args, **kwargs)
        if sync:
            await self.sync_async()

    async def get_corr_receiver_async(self, port_number=None, tcp=None, force=False, **kwargs):
        """Return a correlator frame receiver for the whole array.

        A packet receiver and associated socket will be created if none already
        exist, otherwise the existing ones will be reused. The correlator and
        raw data receivers share the same packet receiver.

        The receiver binds ``config_getter=self.get_corr_data_config`` at first
        creation; later calls reuse the receiver and refresh config via the
        getter on each packet-processing call.

        Parameters
        ----------
        port_number : int or None
            Port for the packet receiver if one has not already been allocated.
            - If ``None``, use the FPGA destination port (or behave as ``0`` if that is zero).
            - If zero, bind a random OS port and program it into the FPGA.
            - If non-zero, bind that port (raises if already in use).
        tcp : bool or None
            If ``True``, use TCP. If ``False``, use UDP. If ``None`` (default),
            reuse the existing packet receiver's transport, or UDP if none exists.
        force : bool
            If ``True``, discard cached correlator/packet receivers and reconnect.
        **kwargs
            Forwarded to :class:`~pychfpga.common.ucorr_frame_receiver.UCorrFrameReceiver`
            when creating the receiver (e.g. ``verbose``, ``pre_func``).

        Returns
        -------
        UCorrFrameReceiver
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

    async def start_correlators_async(self, integration_period, autocorr_only=False, no_accum=False,
                                      bandwidth_limit=0.5e9, verbose=0):
        """
        Start all the correlators in the array.


        Parameters:

            integration_period (32-bit int): Number of frames to integrate before sending the
               correlated products.

            autocorr_only (bool): When 'True', the correlator will only send the autocorrelation
               products

            no_accum (bool): When 'True', the correlator does not accumulate products from multiple
                frames. It just returns the product from the last frame on the integration period.

            bandwidth_limit (float): Maximum acceptable data bandwidth that each correlator can
               produce, in bits/s. Default is 0.5 Gbps/number_of_boards. An exception will be raised if the correlator parameters are to make the
               data exceed this bandwidth in order to avoid operating in a high packet loss environment

        """
        for mb in self.ib:
            mb.start_correlator(integration_period=integration_period,
                                autocorr_only=autocorr_only,
                                no_accum=no_accum,
                                bandwidth_limit=bandwidth_limit/len(self.ib),
                                verbose=verbose)
            await asyncio.sleep(0)

    async def set_offset_binary_encoding_async(self, offset_encoding_enabled):
        """
        """
        self.logger.warning(f'{self.fpga_array!r}: set_offset_binary_encoding() is called. This should be removed')  # ***JFC
        for mb in self.ib:
            mb.set_offset_binary_encoding(offset_encoding_enabled)
            await asyncio.sleep(0)

    async def load_gains_async(self, bank=0, gain_folder='/home/chime/ch_acq/gains'):
        """ Returns the gains from the gain files associated with every board of the array.

        If a gain file is not found for a specific board, the default gains
        found in the file 'default_gains.pkl' will be used for that board.

        If a gain file nor a default gains file are found, the gain data for
        that board will be `None` and a warning will be logged.

        Parameters:

            bank (int): Unused

            gain_folder (str): FOnder in which the gain files are located.

        Returns:

            The gain map in the format ``{board_id: gains}``, where
            ``board_id`` is a two-element tuple uniquely identifying the board
            (taken from the board's get_id()), and ``gains`` is a dict
            containing the digital gains to be applied to the channels on that
            board (from the board's load_gains(...))

        Note:

            This method does not set the digital gains in the FPGAs; it only
            loads them from the files. See `set_gains` to set the gains using
            the dict returned by this method.
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
        """ Save the gains.

        Parameters:

            gains (): Gains in the format

                {(crate,slot,channel):(glin, glog), ...} or {(crate, slot):{channel:(glin,glog),...},...}

            gain_folder (str): Folder in which the gain files are located.

        Returns:

            The gain map in the format ``{board_id: gains}``, where
            ``board_id`` is a two-element tuple uniquely identifying the board
            (taken from the board's get_id()), and ``gains`` is a dict
            containing the digital gains to be applied to the channels on that
            board (from the board's load_gains(...))

        Note:

            This method does not set the digital gains in the FPGAs; it only
            loads them from the files. See `set_gains` to set the gains using
            the dict returned by this method.
        """

        # make sure we have a board-id-based gain table
        gains = self.group_gains_per_board_id(gains)
        for board_id, board_gains in gains.items():
            mb = self.fpga_array.get_iceboard(board_id)
            mb.save_gains(gains=board_gains, folder=gain_folder)

    async def get_gains_async(self, bank=0, use_cache=True):
        """ Return the digital gains programmed in the specified bank for all channels of all boards of the array.

        Parameters:

            `bank`: gain bank from which the gains are read.

            `use_cache`: True to allow the software-cached value to be used
                (much faster the reading back from the FPGAs)

        Returns:

            Digital gains, in a channel-indexed dict, in the format::

                {chan_id: (glin, glog), ...}

                where:
                     ``chan_id`` is a (crate, slot, channel) tuple that uniquely identifies a channel
                     ``crate`` = int or str or None
                     ``slot`` = int or str
                     ``glin` = array of 1024 (int16 + 1j* int16) linear gain components
                     ``glog`` = post-scaler factor (applies additional gain of 2**glog to all bins)

        """
        gains = {}
        for mb in self.ib:
            for ch, ch_gains in mb.get_gains(bank=bank, use_cache=use_cache):
                gains[mb.get_id(ch)] = ch_gains
            await asyncio.sleep(0)
        return (gains)

    async def get_gain_timestamps_async(self, bank=0):
        """
        Returns the timestamp at which the gains for each channel of thewas set.

        Parameters:

            bank (int): gain bank from which to get the gains timestamp.

        Returns:

            Timestamp of the digital gains, in a channel-indexed dict, in the format::

                {chan_id: timestamp, ...}

                where:
                     ``chan_id`` is a (crate, slot, channel) tuple that uniquely identifies a channel
                     ``crate`` = int or str or None
                     ``slot`` = int or str
                     ``timestamp`` = is a time.time() value. None if the gain was not set.
        """
        timestamps = {}
        for mb in self.ib:
            for ch, timestamp in mb.get_gain_timestamps(bank=bank):
                timestamps[mb.get_id(ch)] = timestamp
            await asyncio.sleep(0)
        return (timestamps)

    async def set_gains_async(self, gains, bank=-1, when='now', gain_timestamps=None):
        """ Set the on-board post-FFT digital gains for specified board/channel.

        Parameters:

            gains (dict): dictionary of gains specified as either:

                - ``{board_id: {chan: (glin, glog), ...}, ...}``
                - ``{chan_id: (glin, glog), ...}``

                where:

                - ``board_id`` is a (crate, slot) tuple that uniquely identifies a board

                - ``channel_id`` is a (crate, slot, channel) tuple that uniquely identifies a channel

                - ``crate`` int or str or None

                - ``slot`` int or str

                - ``glin`` array of 1024 ``(int16 + 1j* int16)`` linear gain components

                - ``glog`` post-scaler factor (applies additional gain of ``2**glog`` to all bins)

            bank (int): gain bank in which the gains are written. If `bank` =-1 or is None, gains are
                    written in the inactive bank (which can be activated later using set_gain_bank()).

            when (int, str or None): Specifies when the new gain table must be made active:

                    - If `when` is the string 'now' or a negative integer, the target gains
                      are made active immediately.

                    - If `when` is None, the gains are written in the specified
                      bank but the bank switching is not activated.

                    - If `when` is an integer, the gains will be activated starting on the target
                      timestamp specified by `when`.

            gain_timestamps (dict): dictionary of unix timestamps with same key format as `gains` or
                   single unix timestamp that is applied to all channels.

                   If not provided, then defaults to current time.

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
        """ Convert a channel_id based gain table into a board_id-based gain table.

        Parameters:

            channel_based_bains (dict): dict containing channel-based gain
                entries in the format {channel_id: gains, ...}, where
                ``channel_id`` is a 3-element (crate, slot, channel)tuple

        Returns:
            A board_id-based gain table in the format {board_id: {channel:gains,...},...}.

        Note:
            - Entries that are not a 3-element tuples are left untouched,
              allowing a board-id based table or another dict to be passed.

        """
        board_based_gains = {}
        for id_, gains in channel_based_gains.items():
            if isinstance(id_, (tuple, list)) and len(id_) == 3:
                board_based_gains.setdefault(tuple(id_[:-1]), {})[id_[-1]] = gains
            else:
                board_based_gains[id_] = gains
        return board_based_gains

    async def compute_gains_async(
            self,
            enable=True,
            slots=None,
            noise_injection=None,
            gain_folder='/home/chime/ch_acq/gains'):
        """
        *** NOT FUNCTIONAL ***
        Should be updated to use the new GainCalc object

        Compute the gains of the SCALER module so that the conversion of the FFT output
        to (4+4) bit complex values stays within range for the current signal conditions.

        This method will have to be rewritten to use data obtained over REST-based raw data receivers.



        """

        # Make sure gain calculation is enabled
        if not enable:
            return

        # Try to import gain computation module
        try:
            from pychfpga import calculate_gains
        except ImportError:
            self.logger.error('Could not import calculate_gains. Missing timestream_receiver in path?')
            return

        # Setup noise injection using noise injection parameters that are specific to the gain calculation operation.
        if noise_injection is not None:
            for source_name, source_params in noise_injection.items():
                if source_params.board:
                    self.logger.debug(f"Setting gain computation noise injection for "
                                      f"source '{source_name}' with parameters {source_params}")
                    self.set_noise_injection(local_sync=True, **source_params)

        # Loop over boards and compute gains
        self.logger.info("Computing SCALER gains.")
        for mb in self.ib:
            ch_id = mb.get_id()
            # crate, slot_0based = ch_id[0], ch_id[1]
            if (slots is None) or slots[ch_id[0]][ch_id[1]]:
                await calculate_gains.calculate_gains_async(mb, gain_folder=gain_folder)

    def get_next_gain_bank(self):
        """
        Return the next gain bank number to be used (i.e. the currently unused
        bank number of channel 0 of the first board of the array).


        It is the responsability of the user to make sure that no gain switch will occur once this
        method is called. .

        Returns:
            The bank number of the currently inactive bank of the first channel of the first board of
            the array (not the inactive bank for every channel of every boards).


        """
        self.ib[0].get_next_gain_bank()[0]

    async def switch_gains_async(self, bank=-1, when='now'):
        self.ib.switch_gains(bank=bank, when=when)

    # def set_synchronized_gain_switching_mode(self, enable):
    #     """ Enable or disable synchronized gain switching for all boards of the array. """
    #     self.ib.set_synchronized_gain_switching(enable=enable)

    # def set_next_gain_bank(self, bank):
    #     """ Sets the next gain bank to use on all channelizers of the array.

    #     The switch will be done imeediately or not, depending on the gain switching mode (see
    #     set_synchronized_gain_switching_mode())
    #     """
    #     self.ib.set_next_gain_bank(bank=bank)

    # def set_gain_switch_frame_number(self, frame):
    #     """
    #     Sets the frame number at which the new gain bank (set by set_next_gain_bank()) will be used.

    #     Is used only if synchronized gain switching mode is enabled(see
    #     set_synchronized_gain_switching_mode())

    #     """
    #     self.ib.set_gain_switch_frame_number(frame=frame)

    async def _async(self, delay=0.1):
        self.ib.set_corr_reset(1)
        await asyncio.sleep(0.1)
        self.ib.set_corr_reset(0)

    async def reset_gpu_links_async(self, board_ids=None):
        """ Reset the GPU links for the boards specified in `board_ids`

        Parameters:

            board_ids (list of tuple/dict): List of board descriptors in the
                form of (crate_number) or (crate_number, slot_number) tuples
                or {crate:crate_number} / {crate:crate_number,
                slot:slot_number} dicts. Crate and slot number can be  '*' or
                Null to match every instance.

        Returns:

            List of board_ids that were actually resetted.

        Notes:

            - It is not possible to selectively reset just one QSFP or a
              specific lane of a QSFP. All lanes for both QSFPs are reset.

        """
        ibs = self.fpga_array.get_iceboards(board_ids)
        for mb in ibs:
            mb.reset_gpu_links()
            await asyncio.sleep(0)
        return ([mb.get_id() for mb in ibs])

    async def get_system_config_async(self, basic=False):
        """ Concurrently gets the system configuration info for each FPGA """
        configs = await self.ib.get_system_config_async(basic=basic)
        return dict(zip(self.ib.get_id(), configs))

    def get_frequency_map(self, format='l:cscb'):
        """
        Returns a map describing the content (crate, slot, channel, bin) of
        every packet at the output of the corner turn engine.

        This map is obtained by passing the channelizer identity map through
        the shuffle map.
        """

        return self.get_shuffle_output(self.get_chan_identity_map(format=format))

    def get_chan_identity_map(self, format='l:cscb'):
        """
        Return an identity map that describes the origin of each of the 1024
        samples contained in the channelizer output packets.


        Returns:

            The map as a dict:

                {(crate, slot, channel): [sample_id0, ... sample_id1023]}

            where:

                (crate, slot, lane) tuple is the key from get_channel_ids(),
                   which derives its (crate, slot) from mb.get_id(). ``crate`
                   and ``slot`` could therefore be non-numeric if there is no
                   backplane or if a crate number is not assigned to a crate.

                ``sample_id`` describes the channelizer output samples. It varies depending on `format`:

                    "l:cscb": sample_id[bin_number] =  (crate, slot, channel, bin_number), where
                       ``crate``, ``slot``, ``channel`` are the same as in the
                       key, and can be non-numeric. ``bin_number`` is an integer from 0 to 1023.

                    "l:cc": sample_id[:] = global_chan_number, which is an integer representing the global crate, slot
                        and channel. It is the same for all bins. crate, slot
                        and channel must be numeric (there must be a crate and
                        crate number).

                    "l:cb": sample_id[bin_number] = (global_chan_number, bin_number).
                        See abive for ``global_chan_number``. ``bin_number`` is an integer from 0 to 1023. This tuple
                        uniquely represent every sample of every output in the
                        array.

                    "l:cb": sample_id[bin_number] = bin_number.
                        ``bin_number`` is an integer from 0 to 1023. This list is the same for every channel.


        This map can be propagated through the shuffle map (see
        `apply_shuffle_map` method) to obtain the contents of the output of
        the corner-turn engine.
        """
        ch_out = {}
        for mb in self.ib:
            for (crate, slot, lane) in mb.get_channel_ids():

                if format == 'l:cscb':  # Unique (crate, slot, local_channel)
                    ch_out[(crate, slot, lane)] = [(crate, slot, lane, bin) for bin in range(1024)]
                elif format == 'l:cc':  # Non-unique global channel numbers (repeated for each bin)
                    if not all(isinstance(i, int) for i in (crate, slot, lane)):
                        raise RuntimeError('get_channel_identity_map requires numeric crate and slot numbers')
                    ch_out[(crate, slot, lane)] = [crate * 256 + slot * 16 + lane for bin in range(1024)]
                elif format == 'l:cb':  # Unique (global channel, lane) tuple
                    if not all(isinstance(i, int) for i in (crate, slot, lane)):
                        raise RuntimeError('get_channel_identity_map requires numeric crate and slot numbers')
                    ch_out[(crate, slot, lane)] = [(crate * 256 + slot * 16 + lane, bin) for bin in range(1024)]
                elif format == 'l:bb':  # Non unique bin_number (repeated for each channel)
                    ch_out[(crate, slot, lane)] = [bin for bin in range(1024)]
        return ch_out

    async def get_funcgen_buffer_async(self):
        """ Return the waveform produced by the function generator.

        This will correspond to the function generator output if it is
        configured to send its buffer, and will correspond to the channelizer
        output if the FFT and SCALER are bypassed.
        """
        ch_out = {}
        for cr in self.ic:
            for mb in cr.slot.values():
                ch_out.update(mb.get_funcgen_buffer())
                await asyncio.sleep(0)
        return ch_out

    def get_shuffle_output(self, chan_map):
        """
        Takes the channelizer data map `chan_data` and propagates it through the
        shuffle stages as they are currently configured in the FPGA, and return the resulting data.

        ``chan_data`` is a dictionary of the format {(crate_number, slot, channel_number): [1024 elements],...}

        The elements describing the channel contents can by of any type (complex number, channel & bin tuple, etc.)

        """

        # Get first crossbar map
        # Channels are converted to (crate_number, slot, input_number)
        # Output lane id is converted to (crate_number, slot, lane)

        cb1_out = {}
        for mb in self.ib:
            # extract channels for this motherboard only
            cb1_in = {ch: chan_map[(crate, slot, ch)] for (crate, slot, ch) in mb.get_channel_ids()}
            for lane, data in mb.CROSSBAR.map(cb1_in).items():
                cb1_out[mb.get_id(lane)] = data

        # Apply pcb shuffling
        pcb_shuffle_out = {}
        for cr in self.ic:
            pcb_link_map = cr.get_pcb_link_map()
            for (rx_slot, rx_lane), (tx_slot, tx_lane) in pcb_link_map.items():
                (crate, ) = cr.get_id()
                pcb_shuffle_out[(crate, rx_slot - 1, rx_lane)] = (
                    cb1_out[(crate, tx_slot - 1, tx_lane)] if (crate, tx_slot - 1, tx_lane) in cb1_out
                    else dict(data=[None] * 1024))

        # Apply CROSSBAR2
        cb2_out = {}
        for mb in self.ib:
            (crate, slot) = mb.get_id()
            # extract channels for this motherboard only
            cb2_in = {lane: pcb_shuffle_out[(crate, slot, lane)]
                      for lane in range(mb.BP_SHUFFLE.NUMBER_OF_PCB_LANES)}
            for lane, data in mb.CROSSBAR2.map(cb2_in).items():
                cb2_out[(crate, slot, lane)] = data

        # Apply QSFP shuffling
        qsfp_shuffle_out = {}
        for mb in self.ib:
            (crate, slot) = mb.get_id()
            bypass = mb.BP_SHUFFLE.BYPASS_QSFP_SHUFFLE
            number_of_qsfp_lanes = mb.BP_SHUFFLE.NUMBER_OF_QSFP_LANES
            for rx_lane in range(number_of_qsfp_lanes):
                if bypass:
                    qsfp_shuffle_out[(crate, slot, rx_lane)] = cb2_out[(crate, slot, rx_lane)]
                else:
                    crate_offset = rx_lane * 2 // number_of_qsfp_lanes
                    qsfp_shuffle_out[(crate, slot, rx_lane)] = cb2_out.get(
                        (crate ^ crate_offset, slot, rx_lane), dict(data=[None] * 2048))

        # Apply CROSSBAR3
        cb3_out = {}
        for mb in self.ib:
            (crate, slot) = mb.get_id()
            # extract channels for this motherboard only
            cb_in = {lane: qsfp_shuffle_out[(crate, slot, lane)]
                     for lane in range(mb.BP_SHUFFLE.NUMBER_OF_QSFP_LANES)}
            for lane, data in mb.CROSSBAR3.map(cb_in).items():
                cb3_out[(crate, slot, lane)] = data

        return cb3_out

    # def get_map(self):
    #     cb1_map = self.CROSSBAR.get_map()
    #     shuffle_map
    #     cb2_map = self.CROSSBAR2.get_map()
    #     return cb1_map

    def test_sync(self):
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

    # def soft_sync(self, sync_board):
    #     """ Synchronize all boards"""
    #     boards = list(self.ib)
    #     print('Masking ADC data before sync')
    #     for mb in boards:
    #         for ant in mb.ANT:
    #             ant.ADCDAQ.BYTE_MASK = 0

    #     print('Initiating global sync')
    #     sync_board.REFCLK.local_sync()

    #     print('Unmasking ADC data')
    #     for mb in boards:
    #         for ant in mb.ANT:
    #             ant.ADCDAQ.BYTE_MASK = 255

    # def time_soft_sync(self, sync_board, delay):
    #     """ Synchronize all boards"""

    #     boards = list(self.ib)
    #     print 'Masking ADC data before sync'
    #     for mb in boards:
    #         for ant in mb.ANT:
    #             ant.ADCDAQ.BYTE_MASK = 0

    #     # Get current time
    #     current_time = sync_board.get_irigb_time()
    #     print 'Setting IRIG-B sync after %d seconds' %delay
    #     # Send sync pulse delay seconds in the future
    #     sync_board.set_irigb_trigger_time_sync(current_time, delay)

    #     print 'Unmasking ADC data'
    #     for mb in boards:
    #         for ant in mb.ANT:
    #             ant.ADCDAQ.BYTE_MASK = 255

    # def irigb_sync(self, delay):
    #     """ Synchronize all boards"""

    #     # Get current time
    #     current_time = self.ib[0].get_irigb_time()
    #     print 'Setting IRIG-B sync after %d seconds' %delay
    #     for mb in self.ib:
    #         mb.set_irigb_trigger_time_sync(current_time, delay)

    def print_temperatures(self):
        t = [(b.slot, b.serial, b.SYSMON.temperature()) for b in self.ib]
        t.sort()
        for (slot, serial_number, fpga_temp) in t:
            print(f'Slot {int(slot):2} (SN{serial_number}): FPGA {fpga_temp:2.1f} C')

    def set_adc_mask(self, value):
        for mb in self.ib:
            for ant in mb.ANT:
                ant.ADCDAQ.BYTE_MASK = value

    def print_fmc_power(self):
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

    def detect_backplane_links(self, tx_power=7, print_=True):
        """ Setup all boards on the array to send test pattern over the
        backplane link and detect  from which slot/lane every board is
        receiving data.

        The test is performed only on IceBoards that are installed in crates
        and whose FPGA has been programmed and initialized. Other boards are
        ignored.

        Parameters:

            tx_power (int):

            print_ (bool):


        For now, this test works only if all boards are in a single crate.
        """
        # select only boards on crates and that are open
        array = Ccoll(mb for mb in self.ib if mb.is_open() and mb.crate and mb.slot)

        if not array:
            raise RuntimeError('There are no boards that are opened() AND connected in a crate')

        # Check if the board are all on a single crate
        if len(set(array.crate)) != 1:
            raise RuntimeError(f'Sorry, this method currently can work on only one crate. The currently active boards span multiple crates {list(set(array.crate))}')

        active_slots = [mb.slot for mb in array]
        if len(set(active_slots)) != len(active_slots):
            raise SystemError('Slot numbers are not unique!')

        print('Setting Transmitted ID')
        for mb in array:
            mb.BP_SHUFFLE.TX_DATA_MSB = 0xFF00 + mb.slot
            for gtx_number, g in enumerate(mb.BP_SHUFFLE.gtx):
                g.SOURCE_SEL = 1  # 0:Send TXDATA , 1: SEND 10G Ethernet test packet
                g.LOOPBACK = 0
                g.TXPRBSSEL = 0
                g.RXPRBSSEL = 0
                g.TXDIFFCTRL = tx_power
                g.TX_DATA_LSB = gtx_number + 1
                g.TXHEADER = 1
                g.CAPTURE_ENABLE = 1
                g.TXPRECURSOR = 0b00000  # DFE cannot compensate pre-cursor
                g.TXPOSTCURSOR = 0b00000
                g.RXLPMEN = 0  # Go to DFE mode instead of LPM
                # g.RXMONITORSEL = 1 # 1=AGC, 2=UL, 3=VP loop
                # g.RX_DEBUG_CFG = 0b1011 << 2
                # g.DMONITOR_CFG1 = 0
                # g.DMONITOR_CFG0 = (0b1<<15) | (0x0080 <<1) | 1
                # Configure DMONITOR to read the AGC gain
                # g.DMONITOR_SELECT = 1
                # g.PCS_RSVD_ATTR_BIT6 = 1
                # old=g.read_drp(0x6f)
                # g.write_drp(0x6f,old | 1<<6)
                # g.RXDFEOVRD=1
                # g.write_drp(0x1d, 0x00ea)

        print('Resetting the GTXes')
        for mb in array:
            for g in mb.BP_SHUFFLE.gtx:
                g.RXDFELPMRESET = 1
                g.RXDFELPMRESET = 0
            time.sleep(0.1)

        print('Checking received data')
        link_list = []
        link_matrix = [[None] * 17 for x in range(17)]
        serial_number = ['N/A'] * 17
        for mb in array:
            # print 'Slot %i' % (mb.slot)
            dest_slot = mb.slot
            serial_number[dest_slot - 1] = mb.serial
            # JM: Fixed this below bc was getting an error. JF please check
            for gtx_number, g in enumerate(mb.BP_SHUFFLE.gtx[0:mb.BP_SHUFFLE.NUMBER_OF_PCB_LINKS]):
                dest_lane = gtx_number + 1
                dest = (dest_slot, dest_lane)
                expected_source = mb.crate.get_matching_tx(dest)
                source_present = expected_source[0] in active_slots
                for trial in range(3):
                    rxdata = g.get_rxdata()
                    # print '   Slot %i Lane %i received %08X' % (    mb.slot, lane+1,  rxdata)
                    source_slot = int((rxdata >> 8) & 0xFF)
                    source_lane = int((rxdata) & 0xFF)
                    source = (source_slot, source_lane)
                    source_valid = (rxdata >> 16) == 0xFFFF
                    maybe = (rxdata != 0x55555555) and (rxdata != 0xAAAAAAAA)
                    if source_valid:
                        break

                if source_valid:
                    print(f'Slot {int(dest_slot):2} Lane {int(dest_lane):2} is receiving data from Slot {int(source_slot):2} Lane {int(source_lane):2} (received word = 0x{rxdata:08X}, RXMONITOROUT= {g.RXMONITOR:x}, DMONITOROUT={g.DMONITOROUT:x})')
                    link_matrix[dest_slot][dest_lane] = f'S{int(source_slot):02}L{int(source_lane):02}'
                elif maybe:
                    print(f'Slot {int(dest_slot):2} Lane {int(dest_lane):2} is receiving some data but cannot determine source (received word = 0x{rxdata:08X}, RXMONITOROUT= {g.RXMONITOR:x}, DMONITOROUT={g.DMONITOROUT:x})')
                    link_matrix[dest_slot][dest_lane] = 'S??L??'
                else:
                    link_matrix[dest_slot][dest_lane] = '  ()  '

                if not source_valid or not source_present or source != expected_source:
                    if source_present:
                        link_matrix[dest_slot][dest_lane] += '/S{}L{}  '.format(*expected_source)
                    else:
                        link_matrix[dest_slot][dest_lane] += '/(S{}L{})'.format(*expected_source)

                link_list.append((source, dest))
        if print_:
            # Print a slot map
            print('Slot-> ' + ' '.join([f'{int(slot + 1):<15d}' for slot in range(16)]))
            print('S/N -> ' + ' '.join([f'{sn:15}' for sn in serial_number]))
            print('Lane   ' + ' '.join([f'{"-" * 15:15}' for _ in range(16)]))

            for dest_lane in range(1, 16):
                print(f'{int(dest_lane):6} ', end=' ')
                for dest_slot in range(1, 17):
                    print(f'{link_matrix[dest_slot][dest_lane]:15}', end=' ')
                print()

        return link_list

    def get_backplane_pcb_link_map(self):
        """ Return a dictionary that lists all the backplane PCB links and
        their corresponding GTXes for every boards in the array.

        The list covers every transmitter and receivers on the borads that are
        currently in the array. Each GTX therefore has two entries, one in
        which it is the transmitter, and one in which it is the reciever. The
        connectivity is resolved by relying on the connectivity information
        that is probiced by the IceCrate object, so there is no need for
        additional resolving.

        Parameters:

            None

        Returns:
            A dict, in the format:

            {('pcb', (tx_crate, tx_slot, tx_lane), (rx_crate, rx_slot, rx_lane)): (tx_gtx_instance, rx_gtx_instance)}
        """
        link_map = {}
        for mb in self.ib:
            link_map.update(mb.BP_SHUFFLE.get_link_map('pcb'))
        return link_map

    def get_backplane_qsfp_links(self):
        """ Get the list of backplane QSFP links and resolve their
        connectivity to return a list of lane connectivity.

        We achieve this by getting the list that matches the QSFP-connected
        GTXes to a cable link ID by calling each crate's get_qsfp_links(),
        which returns a list in the format:

            [('BP_QSFP', (crate_id, iceboard_slot, gtx_index), None, cable_link_id), ...]


        Returns:

            A resolved, motherboard- and lane-oriented connectivity list in the format:

                [('BP_QSFP', (tx_crate, tx_iceboard_slot, tx_lane), (rx_crate, rx_iceboard_slot, rx_lane) ), ...]

            The list refers to logical lane numbers (including the internal
            bypass lanes). Note that the internal (bypass) links are not added
            to the result.


        """
        # Combine TX and RX link dicts from all crates
        raw_links = [link for cr in self.ic
                     for qsfp_link_list in cr.get_qsfp_links()
                     for link in qsfp_link_list]

        for cr in self.ic:
            raw_links += cr.get_qsfp_links()

        # Make a map of crates objects indexed by crate_number
        crate_map = {crate.get_id()[0]: crate for crate in self.ic}

        # Visit each link and find the attached nodes
        links = []
        for (link_type, tx_id, rx_id, link_id) in raw_links:
            # If the second node is not already known, search all the links
            # for a corresponding half-link with the same link_id
            if rx_id is None:
                matching_nodes = [nid1 for (lt, nid1, nid2, lid) in raw_links
                                  if lt == link_type and nid1 != tx_id and nid2 is None and lid == link_id]
                if len(matching_nodes) == 1:
                    rx_id = matching_nodes[0]
            if tx_id is None or rx_id is None:
                continue
            (source_crate, source_slot, source_lane) = tx_id
            (dest_crate, dest_slot, dest_lane) = rx_id
            ic0 = crate_map[source_crate]
            ic1 = crate_map[dest_crate]
            if (source_slot not in ic0.slot) or (dest_slot not in ic1.slot):
                continue
            bp0 = ic0.slot[source_slot].BP_SHUFFLE
            bp1 = ic1.slot[dest_slot].BP_SHUFFLE
            # Append entry. We convert from gtx index to logical lane #
            links.append((
                link_type,
                (source_crate, source_slot, source_lane + bp0.NUMBER_OF_QSFP_DIRECT_LANES),
                (dest_crate, dest_slot, dest_lane + bp1.NUMBER_OF_QSFP_DIRECT_LANES)))

        return links

    def get_backplane_qsfp_link_map(self, resolve=True):
        """ Return a dictionary that maps the backplane QSFP links to
        corresponding GTX transmitter and receiver instances.

        Returns:
           A dict, in the format:

            {('qsfp', (tx_crate, tx_slot, tx_lane), (rx_crate, rx_slot, rx_lane)): (tx_gtx_instance, rx_gtx_instance)}

        """
        qsfp_link_map = {}
        for mb in self.ib:
            qsfp_link_map.update(mb.BP_SHUFFLE.get_link_map('qsfp'))

        if not resolve:
            return qsfp_link_map

        # Ask each board a map that describe how each logical link is connected to the backplane links
        bp_to_logical_link_map = {}
        for mb in self.ib:
            bp_to_logical_link_map.update(mb.BP_SHUFFLE.get_bp_to_logical_link_map('qsfp'))

        # Ask each crate the map that matches backplane links to cable ids
        # (this takes time: we need to read the cable identification)
        bp_to_cable_map = {}
        for cr in self.ic:
            bp_to_cable_map.update(cr.get_qsfp_cable_map())

        # Create a map that matches each cable id to a list of corresponding
        # logical link ids (there should be 2 for each link)
        cable_to_link_map = {}  # {cable_id: (crate, slot, logical_lane)}
        for (bp_id, cable_id) in bp_to_cable_map.items():
            cable_to_link_map.setdefault(cable_id, []).append(bp_to_logical_link_map[('qsfp', bp_id)])

        # Crate a map that matches each logical link id with another logical link id.
        link_map = {}
        for cable_id, link_ids in cable_to_link_map.items():
            if len(link_ids) == 1:
                self.logger.warning(f'Only one end of a QSFP cable is connected; Cable ID {cable_id} connects only to {link_ids[0]}. The link will be ignored.')
            elif len(link_ids) > 2:
                raise RuntimeError(f'A QSFP cable connects to more than 2 links. Something is wrong. Cable ID {cable_id} connects only to {link_ids}.')
            elif len(link_ids) == 2:
                link_map[link_ids[0][1]] = link_ids[1][1]
                link_map[link_ids[1][1]] = link_ids[0][1]
        print(link_map)

        # Resolve each unresolved link.
        resolved_qsfp_link_map = {}
        for (link_type, tx_id, rx_id), (tx_gtx, rx_gtx) in qsfp_link_map.items():
            if not tx_id and rx_id and rx_id in link_map:
                tx_id = link_map[rx_id]
                tx_gtx, _ = qsfp_link_map[('qsfp', tx_id, None)]
            elif not rx_id and tx_id and tx_id in link_map:
                rx_id = link_map[tx_id]
                _, rx_gtx = qsfp_link_map[('qsfp', None, rx_id)]
            resolved_qsfp_link_map[link_type, tx_id, rx_id] = (tx_gtx, rx_gtx)

        return resolved_qsfp_link_map

    def get_gpu_link_map(self):
        """
        Return a dictionary that matches the GPU links to the GTX Rx and Tx
        instances when connected with a loopback cable.

        Returns:
           A dict, in the format:

            {('GPU', (tx_crate, tx_slot, tx_lane), (rx_crate, rx_slot, rx_lane)): (tx_gtx_instance, rx_gtx_instance)}
        """
        link_map = {}
        for mb in self.ib:
            crate_id = mb.get_crate_id()
            slot = mb.slot
            n_lanes = len(mb.GPU.gtx)
            # For the 8-lane 10G links, lanes 0-4 are looped back to 5-8. For 4-lane 100G link, lanes are looped back on themselves.
            for tx_lane in range(n_lanes):
                rx_lane = mb.GPU.LOOPBACK_LANE_MAP[tx_lane]
                link = ('GPU', (crate_id, slot, tx_lane), (crate_id, slot, rx_lane))
                tx = mb.GPU.gtx[tx_lane]
                rx = mb.GPU.gtx[loopback_lanes[tx_lane]]
                link_map[link] = (tx, rx)
        return link_map

    def get_link_map(self, links=None, gtx_only=False):
        """
        Return a dictionary that describes the corner-turn and loopbacked GPU
        links and the corresponding Tx and RX GTXes.


        Is used by `get_ber()` to identify the links on which Bit Error Rate (BER) tests will be performed.

        Parameters:

            links (str or list of str): select only the link type specified as
                a string or the link types specified in a list of strings.

        Returns:

            A dict, in the format::

                {(link_type, (tx_crate, tx_slot, tx_lane), (rx_crate, rx_slot, rx_lane)): (tx_gtx_instance, rx_gtx_instance)}

            `link_type` is either "BP", "BP_QSFP" or "GPU"
        """
        link_map = {}
        link_map.update(self.get_backplane_pcb_link_map())
        link_map.update(self.get_backplane_qsfp_link_map())
        link_map.update(self.get_gpu_link_map())  # loopback links

        if isinstance(links, str):
            link_map = {link: gtxes for link, gtxes in link_map.items() if link[0] == links}
        elif links is not None:
            link_map = {link: gtxes for link, gtxes in link_map.items() if link in links}

        # Remove links that do not exist or have no GTX (direct lanes)
        if gtx_only:
            link_map = {link: gtxes for link, gtxes in link_map.items() if (('int' not in gtxes) and (None not in gtxes))}

        # link_list.sort(key=lambda (lt, (sc, ss, sl), (dc, ds, dl)): ss * 16 + ds)
        return link_map


    async def get_ber_async(self, link_list=None, period=0.1, tx_power=12, print_=True, tx_init_power=7, tx_qsfp_power=15, minerrors_toprint=0):

        links = self.get_link_map(link_list, gtx_only=True)

        for link, (source_gtx, dest_gtx) in links.items():
            # First, make sure we can get errors by setting the wrong RX PRBS Sequence
            if source_gtx is None or dest_gtx is None or source_gtx == 'int' or dest_gtx == 'int':
                continue
            source_gtx.select()
            source_gtx.TXPRBSSEL = 4

            dest_gtx.select()
            dest_gtx.RXPRBSCNTRESET = 1
            dest_gtx.RXPRBSSEL = 3
            dest_gtx.RXPRBSCNTRESET = 0

            t0 = time.time()
            while True:
                if dest_gtx.get_prbs_error_ctr():
                    break
                if time.time() - t0 > 1:
                    raise SystemError('Cannot detect errors even with the wrong sequence! '
                                      'Are the links connected as expected?')

        # Perform BER test on a single list, to be run in parallel below
        async def one_link_ber_async(link):
            (link_type, (sc, ss, sl), (dc, ds, dl)), (source_gtx, dest_gtx) = link

            dest_gtx.select()
            dest_gtx.RXPRBSSEL = 4
            source_gtx.select()
            source_gtx.TXPRBSSEL = 4
            source_gtx.TXDIFFCTRL = tx_init_power  # Setting initial power level

            dest_gtx.select()
            dest_gtx.RXDFELPMRESET = 1  # Retting the Reciever DFE
            await asyncio.sleep(0.5)
            dest_gtx.RXDFELPMRESET = 0

            source_gtx.select()
            if (link_type == 'pcb'):
                source_gtx.TXDIFFCTRL = tx_power
            else:
                source_gtx.TXDIFFCTRL = tx_qsfp_power

            # if print_:
            #    print 'Measuring BER for link %s' % (link[0],),
            #    print source_gtx.TXDIFFCTRL
            dest_gtx.select()
            dest_gtx.RXPRBSCNTRESET = 1  # Resetting errors
            await asyncio.sleep(0.5)
            dest_gtx.RXPRBSCNTRESET = 0

            await asyncio.sleep(period)

            cnt = dest_gtx.ERR_CTR
            err = (float(cnt) * 16) / (period * 10e9)
            err_max = (float(cnt) * 16 + 1) / (period * 10e9)

            # source_gtx.TXPRBSSEL = 0  #Setting PRBS to 0 (data path)
            # dest_gtx.TXPRBSSEL = 0  #Setting PRBS to 0 (data path)
            if print_ and (cnt > minerrors_toprint):
                print(f'{link[0]!r} BER = {err:1.1e} ({cnt} errors, BER<{err_max:1.1e})')
            self.fpga_array.print_flush()
            return err

        # Run BER test on each link in parallel
        ber_table = await asyncio.gather(*[one_link_ber_async((link, gtxes)) for link, gtxes in links.items()])
        return dict(zip(links.keys(), ber_table))

    async def get_ber_vs_power_async(self, links, max_power, period=0.1):

        # links = self.get_link_map(links, gtx_only=True)
        # links = self.scan_links(array, tx_power = max_power)

        power = list(range(0, max_power + 1))
        data = {}
        for tx_power in power:
            e = await self.get_ber_async(links, period=period, tx_power=tx_power)
            for (link, ber) in e.items():
                if link in data:
                    data[link][0].append(tx_power)
                    data[link][1].append(ber)
                else:
                    data[link] = [[tx_power], [ber]]
        return data

    async def plot_ber_vs_power_async(self, data=None, period=0.1, **kwargs):

        if isinstance(data, str):
            data = await self.get_ber_vs_power_async(links=data, period=period, **kwargs)

        for link, (tx_power, ber) in data.items():
            print(f"{link:>20} {','.join([f'{b:6.1g}' for b in ber])}")
        plt.figure(1)
        plt.clf()

        for link, (tx_power, ber) in sorted(data.items(), key=str):
            plt.semilogy(tx_power, (np.array(ber) + 1e-12), label=f'Link {link}')
        plt.title(f'BER of links as a function of TX power (period={period:0.1f} s)')
        plt.xlabel('TX power (0-15)')
        plt.ylabel('BER')
        leg = plt.legend(loc='best', fontsize='small', markerscale=3, framealpha=0.6, shadow=True)
        plt.setp(leg.get_lines(), linewidth=2)  # make legend lines thicker so we can see the color better
        plt.grid(1)

    def get_eye_matrix(self, h_step=10, v_step=40):
        link_map = self.detect_backplane_links()
        eye_matrix = {}

        for link in link_map:
            ((from_slot, from_lane), (to_slot, to_lane)) = link
            print(f"###### running from slot {int(from_slot)} lane {int(from_lane)} to slot {int(to_slot)} lane {int(to_lane)} #######")
            gtx = self.ib.get(slot=to_slot).BP_SHUFFLE.gtx[to_lane - 1]
            e = gtx.get_eye_diagram(list(range(-32, 32, h_step)), list(range(-127, 128, v_step)))
            eye_matrix[link] = e
        return eye_matrix

    @staticmethod
    def plot_eye_matrix(eye_matrix):
        plt.figure(1)
        plt.clf()

        source_slots = [ss for ((ss, sl), (ds, dl)) in list(eye_matrix.keys())]
        dest_slots = [ds for ((ss, sl), (ds, dl)) in list(eye_matrix.keys())]
        slots = sorted(set(source_slots + dest_slots))

        # slot_map = {slot: ix for (ix, slot) in enumerate(slots)}
        slot_map = {x + 1: x for x in range(16)}

        (fig, ax) = plt.subplots(
            len(slot_map), len(slot_map),
            sharex=True, sharey=True, subplot_kw={'axis_bgcolor': 'black'})
        fig.subplots_adjust(wspace=0, hspace=0)
        fig.suptitle('Backplane 10Gbps mesh eye diagrams\n TX slot #: left, Rx slot #: bottom')

        for (slot, ix) in slot_map.items():
            # Bottom images
            a = ax[ix, 0]
            a.tick_params(labelsize=6)
            a.set_ylabel(f"TX S{int(slot):02}", fontsize=10)
            # Left images
            a = ax[len(slot_map) - 1, ix]
            a.tick_params(labelsize=6)
            a.set_xlabel(f"RX S{int(slot):02}")
            plt.setp(a.xaxis.get_majorticklabels(), rotation=70)
            # Diagonal images
            a = ax[ix, ix]
            a.patch.set_color('black')

        for (((ss, sl), (ds, dl)), eye) in eye_matrix.items():
            a = ax[slot_map[ss], slot_map[ds]]
            a.imshow(
                np.log10(eye.ber_map + 1e-12),
                origin='lower', extent=(-32, 32, -128, 128), aspect='auto', vmin=-12, vmax=1)
        plt.draw()

    def print_crossbar2_frame_info(self, reset_stats=False, grid=True, width=180):
        slots = Ccoll(self.ib, self.ib.slot)  # Get iceboards indexed by slot number

        crate = set(slots.crate)
        if len(crate) == 1:
            crate = crate.pop()
        else:
            raise RuntimeError(f'Sorry, this method currently can work on one and only one crate. The currently active boards either have no crates or span multiple crates {list(set(slots.crate))}')

        slot_range = list(range(1, 17))
        lane_range = list(range(16))
        slot_labels = [slots[s].serial if s in list(slots.keys()) else 'N/A' for s in slot_range]

        captured_source = {}
        computed_source = {}
        valid_source = {}
        info = {}
        expected_bp_frame_size = 37
        for dest_slot in slot_range:
            if dest_slot in slots.keys():
                cb = slots[dest_slot].CROSSBAR2
                bp = slots[dest_slot].BP_SHUFFLE
                bin_sel = cb.BIN_SEL[0]
                shuffle_to_bin_sel_lane_map = cb.get_reverse_lane_map()
                remapped_direct_lane = shuffle_to_bin_sel_lane_map[0]
                if reset_stats:
                    bp.reset_stats()
                stream_ids = bin_sel.capture_stream_id()
                frame_number = bin_sel.capture_frame_number()
                frame_number = [f - frame_number[remapped_direct_lane] for f in frame_number]
                captured_source = {remapped_dest_lane: (((s >> 4) & 0xF) + 1, (s & 0xF))
                                   for remapped_dest_lane, s in enumerate(stream_ids)}
                computed_source = {shuffle_to_bin_sel_lane_map[dest_lane]: crate.get_matching_tx((dest_slot, dest_lane))
                                   for dest_lane in lane_range}
                valid_source = {shuffle_to_bin_sel_lane_map[dest_lane]: v
                                for dest_lane, v in enumerate(cb.get_lane_monitor('INPUT_DETECT'))}
                missing_frame = {shuffle_to_bin_sel_lane_map[dest_lane]: v
                                 for dest_lane, v in enumerate(cb.get_lane_monitor('MISSING_FRAME'))}
                align_fifo_overflow = {shuffle_to_bin_sel_lane_map[dest_lane]: v
                                       for dest_lane, v in enumerate(cb.get_lane_monitor('ALIGN_FIFO_OVERFLOW'))}
                rx_errors = {shuffle_to_bin_sel_lane_map[dest_lane]: v
                             for dest_lane, v in enumerate(bp.get_rx_lane_monitor('ERROR_CTR'))}
                rx_fifo_overflow = {shuffle_to_bin_sel_lane_map[dest_lane]: v
                                    for dest_lane, v in enumerate(bp.get_rx_lane_monitor('FIFO_OVERFLOW'))}
                rx_max_frame_length = {shuffle_to_bin_sel_lane_map[dest_lane]: v
                                       for dest_lane, v in enumerate(bp.get_rx_lane_monitor('MAX_FRAME_LENGTH'))}

            else:
                captured_source = {dest_lane: None for dest_lane in lane_range}
                computed_source = {dest_lane: crate.get_matching_tx((dest_slot, dest_lane)) for dest_lane in lane_range}
                valid_source = {dest_lane: False for dest_lane in lane_range}
                missing_frame = {dest_lane: False for dest_lane in lane_range}
                align_fifo_overflow = {dest_lane: False for dest_lane in lane_range}
                rx_fifo_overflow = {dest_lane: False for dest_lane in lane_range}
                rx_errors = {dest_lane: 0 for dest_lane in lane_range}
                rx_max_frame_length = {dest_lane: expected_bp_frame_size for dest_lane in lane_range}
                frame_number = None

            info[dest_slot] = {}
            for remapped_dest_lane in lane_range:
                cap_src = captured_source[remapped_dest_lane]
                comp_src = computed_source[remapped_dest_lane]
                valid_src = valid_source[remapped_dest_lane]
                data_expected = not ((comp_src[0] not in slots.keys()) or (dest_slot not in slots.keys()))
                cell = ' ' if (cap_src and cap_src == comp_src and valid_src) or not data_expected else '!'
                cell += ('------' if not cap_src or not data_expected
                         else '--??--' if not valid_src else f'S{cap_src[0]:02d}L{cap_src[1]:02d}')
                cell += f'/S{comp_src[0]:02d}L{comp_src[1]:02d}'
                if frame_number is not None and data_expected and any(frame_number):
                    cell += f' F={frame_number[remapped_dest_lane]:02x}'
                if data_expected:
                    if missing_frame[remapped_dest_lane]:
                        cell += '\n  MISSING FRAMES'
                    if align_fifo_overflow[remapped_dest_lane]:
                        cell += '\n  ALIGN FIFO OVERFLOW'
                    if rx_fifo_overflow[remapped_dest_lane]:
                        cell += '\n  RX FIFO OVERFLOW'
                    if rx_errors[remapped_dest_lane]:
                        cell += f'\n  RX ERR = {int(rx_errors[remapped_dest_lane])}'
                    if rx_max_frame_length[remapped_dest_lane] != expected_bp_frame_size:
                        cell += f'\n  RX FRAME = {int(rx_max_frame_length[remapped_dest_lane])} Words'
                info[dest_slot][remapped_dest_lane] = cell

        print('Post-remap, crossbar2 bin selector input lane indentification')

        corner_label = 'Slot->\nS/N ->\n\\|/Lane'
        col_labels = [f'{int(slot_range[i])}\n{slot_labels[i]}' for i in range(len(slot_range))]
        row_labels = lane_range
        self.print_table(info,
                         row_labels=row_labels, col_labels=col_labels, corner_label=corner_label,
                         line_sep=grid, max_width=width)

    def reset_crossbar_stats(self):
        """ Reset error statistics for the crossbar, crossbar2, and crossbar3.
        """

        for mb in self.ib:
            for cb in [mb.CROSSBAR, mb.CROSSBAR2, mb.CROSSBAR3]:
                if cb:  # make sure the crossbar exists in this firmware
                    cb.reset_stats()

    def reset_bp_shuffle_stats(self):
        """ Reset error statistics for the backplane shuffle.
        """

        for mb in self.ib:
            if mb.BP_SHUFFLE:  # makesure we have a shuffle block in this firmware
                mb.BP_SHUFFLE.reset_stats()

    async def get_corner_turn_engine_status_async(self, reset_stats=False):
        """ Gather info on the status of the corner-turn engine for all boards of the array installed in crates.

        The information includes status info on every lane of  the following corner turn subsystems:

            - Every lane (16) of the backplane PCB shuffle (shuffle between boards in a crate)
            - Every lane (8)  of the backplane QSFP shuffle (shuffle between crates)
            - Every lane (16) of crossbar 2
            - Every lane (8) of crossbar 3

        Parameters:

            reset_stats (bool): if True, the statistics on the corner turn subsystems will be reset before being measured.

        Returns:

            A list of dict containing the corner-turn engine status flags for each crate, each dist following the schema::

                "cr": <crate_object>: # Crate object
                "slots":
                    slot_number1: # Slot number as found in Motherboard.slot (int)
                        "mb": <mb_object> # Motherbpard object
                        "serial": <serial_number>  # Motherboard Serial number, as found in Motherboard.serial (str)
                        "subsystems":
                            subsystem1: # Subsystem name (str)
                                "status": <subsystem_status>  # Subsystem aggregate status for all lanes (bool)
                                "lanes":
                                    lane_number1:  # Lane number (int)
                                        "status": Lane status (True if there are any flags) (bool)
                                        "label": Full length label for the lane (subsystem & lane) (str)
                                        "fields":
                                            "Tx": # Tuple identifying the transmitter (tuple)
                                            "Rx": # Tuple identifying the receiver (tuple)
                                            field1_name: <field1_value> # field status. Field name is a str. Field value can be anything
                                            field2_name: <field2_value>
                                        ...
                                    lane_number2:
                                    ...
                            subsystem2:
                        ...
                    slot_number2:
                    ...

        Example:

            ::

                info[0]['slots'][2]['subsystems']['BP PCB']['status']
                info[0]['slots'][2]['subsystems']['BP PCB']['lanes'][3]['status']

            Print the summary of every lanes of every subsystem for every slot::

                info = await ca.get_corner_turn_engine_status_async(reset_stats=True)
                [f"Crate {c} Slot {slot_number} {sub_name} { [lane['status'] for lane in sub['lanes'].values()]}"
                   for c in info
                   for slot_number, slot in c['slots'].items()
                   for sub_name, sub in slot['subsystems'].items()]

            Get the status of the first crate as a dict ``{slot_number:{subsystem:status, ...}, ...}``::

                status =  {slot_number:{sub_name:sub['status'] for sub_name, sub in slot['subsystems'].items()}
                           for c in info[0] for slot_number, slot in c['slots'].items()}
        """
        info = []

        for crate_obj in self.ic:
            # initialize the dictionary the will contain the infor for each slot in the current crate
            crate = {}  # {crate_object: slot_dict}
            info.append(crate)
            slot_range = list(range(1, crate_obj.NUMBER_OF_SLOTS + 1)) or [None]

            crate['cr'] = crate_obj
            # gather info for each currently populated slot
            crate['slots'] = {}
            for slot_number in slot_range:
                crate['slots'][slot_number] = slot = {}
                slot['mb'] = None
                slot['serial'] = None
                slot['subsystems'] = {}
                if slot_number not in crate_obj.slot:
                    continue
                # initialize the dict for each lane
                mb = crate_obj.slot[slot_number]
                slot['mb'] = mb  # motherboard object in this slot
                slot['serial'] = mb.serial  # motherboard object in this slot
                # Gather status from the backplane PCB and QSFP links
                for lane_group in mb.BP_SHUFFLE.lane_group_names:
                    subsystem_name = f'BP {lane_group.upper()}'
                    slot['subsystems'][subsystem_name] = subsystem = {}  # {lane:status_dict, ...}
                    subsystem['lanes'] = {}
                    if reset_stats:
                        mb.BP_SHUFFLE.reset_stats()
                    # ee: list of N error status dicts  [lane0_errors, lane1 errors, ...]
                    status_dicts = await mb.BP_SHUFFLE.get_bp_rx_status(lane_group)
                    for lane_number, status_dict in enumerate(status_dicts):
                        subsystem['lanes'][lane_number] = lane = {}
                        lane['status'] = bool(status_dict) # True if there were any error flags returned for this lane
                        lane['label'] = f'{subsystem_name} L{lane_number:02d}'
                        if lane_group == 'pcb':  # if verbose, add the matching TX (slot,lane) for each rx lane on that slot
                            status_dict['Tx'] = f'{mb.crate.get_matching_tx((slot_number, lane_number))}'
                            status_dict['Rx'] = f'{(slot_number, lane_number)}'
                        lane['fields'] = status_dict
                        # print(f'added {subsystem_name} {lane_number} = {status_dict}')
                    # generate a subsystem status summary
                    subsystem['status'] = any(status_dict['status'] for status_dict in subsystem['lanes'].values())

                    # errs.append(ee)

                # Gather status from the crossbars
                for subsystem_prefix, cb in (('CB2', mb.CROSSBAR2), ('CB3', mb.CROSSBAR3)):
                    if reset_stats:
                        cb.reset_stats()

                    # Get the status flags. These are returned as a list of dict, one dict per lane.
                    align_status_dicts = await cb.get_align_status()
                    frame_status_dicts = await cb.get_frame_alignment_status()
                    bin_sel_status_dicts = await cb.get_bin_sel_status()
                    for subsystem_suffix, status_dicts in (('ALIGN', align_status_dicts),
                                                           ('FRAME #', frame_status_dicts),
                                                           ('BIN_SEL', bin_sel_status_dicts)):
                        subsystem_name = f'{subsystem_prefix} {subsystem_suffix}'
                        slot['subsystems'][subsystem_name] = subsystem = {}  # {lane:status_dict, ...}
                        subsystem['lanes'] = {}
                        for lane_number, status_dict in enumerate(status_dicts):
                            subsystem['lanes'][lane_number] = lane = {}
                            lane['status'] = bool(status_dict) # True if there were any error flags returned for this lane
                            lane['label'] = f'{subsystem_name} L{lane_number:02d}'
                            lane['fields'] = status_dict
                        # generate a subsystem status summary
                        subsystem['status'] = any(status_dict['status'] for status_dict in subsystem['lanes'].values())
        return info

    def _print_shuffle_status(self, info, verbose=1, grid=False) -> None:
        """ Formats and prints the status information of the corner-turn engine


        Parameters:

            info (list[dict]): output of get_corner_turn_engine_status_async that we wish to print

            reset_stats (bool): if True, the statistics on the corner turn subsystems will be reset before being measured.

            verbose (int): Determine how much status information is returned

                verbose=0: Will provide the '-' or 'ERR' for each subsystem  depending on whether there are any errors on any lane in the subsystem

                verbose=1: Will provide '-' or 'ERR' for each lane of the sybsystem depending on whether there are any errors on for each lane in the subsystem

                verbose=2: Will provide detailed status info on each lane of the subsystem in the form of a dict. Will also add the coordinates of the Tx/Rx pairs involved in backplane links.

            grid (bool): If true, line separators will be used

        """
        for crate in info:
            # Print the table
            corner_label = 'Slot->\nS/N ->\n\\|/Lane'
            # slot_labels = ['SN%s' % slot_dict['mb'].serial if slot_dict['mb'] else 'N/A' for slot_dicts in info[crate].values()]
            col_labels = [f"SN{slot['serial']}\n{slot_number}" for slot_number, slot in crate['slots'].items()]
            # row_labels = ['BP PCB Rx\nBP QSFP Rx\nCB2 FIFO\nCB2 ALIGN\nCB2 FRAMEnCB3 FIFO\nCB3 ALIGN\nCB3 FRAME\n']
            row_labels = []
            data = []

            slots = list(crate['slots'].values())
            first_slot = [slot for slot in slots if slot['mb']][0]
            if not verbose:
                # verbose = 0. We print summary status info only for each subsystem
                row_labels = list(first_slot['subsystems'].keys())
                data = [[('-', 'ERR')[subsystem['status']] for subsystem in slot['subsystems'].values()] for slot in slots]
            else:
                # verbose != 0 : We print info on each lane
                row_labels = [lane['label'] for subsystem in first_slot['subsystems'].values() for lane in subsystem['lanes'].values()]
                # create an 2-dimensional array of lane dicts
                all_lanes = [[lane for subsystem in slot['subsystems'].values()
                                   for lane in subsystem['lanes'].values()
                            ] for slot in slots]
                if verbose == 1:
                    # verbose = 1: we print summary info for each lane
                    data = [[('-', 'ERR')[lane['status']] for lane in lanes]
                            for lanes in all_lanes]
                elif verbose > 1:
                    # verbose > 1: we print all status info for each lane
                    data = [['\n'.join(f'{key}={value}' for key, value in lane['fields'].items())
                             for lane in lanes] for lanes in all_lanes]

            # for label, lanes in [('BP PCB Rx', 16), ('BP QSFP Rx', 8), ('CB2 ALIGN', 16),
            #                      ('CB2 FRAME #', 16), ('CB2 BIN_SELs', 2), ('CB3 ALIGN', 8),
            #                      ('CB3 FRAME #', 8), ('CB3 BIN SELs', 8)]:
            #     if verbose and lanes:
            #         row_labels += ['%s L%02i' % (label, lane) for lane in range(lanes)]
            #     else:
            #         row_labels += [label]
            # return info
            # print 'row_labels=', row_labels
            # print 'col_labels=', col_labels
            # print 'data=', info
            self.print_table(
                data, row_labels=row_labels, col_labels=col_labels,
                corner_label=corner_label, line_sep=grid)

    async def print_shuffle_status_async(self, reset_stats=False, verbose=1, grid=False):
        """ Prints status information of the corner-turn engine


        Parameters:

            reset_stats (bool): if True, the statistics on the corner turn subsystems will be reset before being measured.

            verbose (int): Determine how much status information is returned

                verbose=0: Will provide the '-' or 'ERR' for each subsystem  depending on whether there are any errors on any lane in the subsystem

                verbose=1: Will provide '-' or 'ERR' for each lane of the sybsystem depending on whether there are any errors on for each lane in the subsystem

                verbose=2: Will provide detailed status info on each lane of the subsystem in the form of a dict. Will also add the coordinates of the Tx/Rx pairs involved in backplane links.

            grid (bool): If true, line separators will be used

        """

        info = await self.get_corner_turn_engine_status_async(reset_stats=reset_stats)
        self._print_shuffle_status(info, verbose=verbose, grid=grid)



    def print_iceboard_temperatures(self):
        sensor = self.ib[0].TEMPERATURE_SENSOR.MB_FPGA_DIE
        self.print_iceboard_table(
            lambda mb: f'{mb.get_motherboard_temperature(sensor):3.1f}',
            row_labels='FPGA Die Temp')

    def print_iceboard_power(self):
        self.print_iceboard_table(lambda mb: f'{mb.get_total_power():0.1f}')

    def reset_fpga_stats(self):
        """ Resets FFT overflow count for all boards in FPGA array.
        """
        for mb in self.ib:
            mb.reset_fft_overflow_count()

    # def get_monitoring_info(self):
    #     return self.ib.index_by(lambda mb: mb.get_id()).get_status()

    async def get_arm_metrics_async(self, metrics):
        """ Get the monitoring information on the backplanes & boards that are accessible from the ARM.

        Includes:

            - Backplane metrics, as measured from one board in each crate
            - Motherboard hardware metrics (voltages, temperatures), which also includes mezzanines voltage/current.

        Returns:
            A :class:`Metrics` object.
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
        """ Get the monitoring information on the FPGA firmware status across the array.

        Includes:

            - Backplane receiver/transmitter status with packet statistics for both the PCB and QSFP links.

        Returns:
            A :class:`Metrics` object.
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

    def print_iceboard_info(self):
        """ Gather metrics from the iceboards and print them in a nicely formatted table """
        if not self.ib:
            print('There are no IceBoards in the array')
            return
        info = [i for (i, _) in self.ib._get_motherboard_metrics_async()]
        keys = '\n'.join(info[0].keys())
        data = {mb: ('\n'.join(info[i].values())) for i, mb in enumerate(self.ib)}
        self.print_iceboard_table(data, row_labels=keys)

    def print_rx_err_map(
            self,
            icecrates=None,
            reset_stats=0,
            delay=-5,
            tx_power=None,
            tx_precursor=None,
            tx_postcursor=None,
            lpm=None,
            dfe_reset=False,
            stop_on_errors=2,
            verbose=1):
        """
        Test the backplane PCB and QSFP link operation. Allows various
        parameters of the GTX transceivers to be changed to measure their
        effect.

        Parameters:

            icecrates (IceCrate Ccoll): List of icecrates on which the test is
                done. If `None`, all icecrates are initialized/tested.

            reset_stats (bool): Reset the statistics counters. Default is false.

            delay (int):

            tx_power (int): transmit power to be set on the GTX. Ranges from
                0-15. If ``None``, TX power levels ar enot changed.

            tx_precursor: None,

            tx_postcursor: None

            lpm (bool): If true, enables Low Power Mode in the GTX. Requires a dfe_reset.

            dfe_reset (bool): If true, resets the Dynamix Feedback Equalizer
                to allow it to find a new equalization solution. Is needed
                when power levels or operaiton mode are changed

            stop_on_errors : =2,

            verbose=1
        """
        if icecrates is None:
            icecrates = self.ic

        ibs = Ccoll.chain(*icecrates.slot.item_values())
        gtx = Ccoll.chain(*ibs.BP_SHUFFLE.gtx)

        time_without_error = [None] * len(icecrates)
        has_errors = [False] * len(icecrates)

        if lpm is not None:
            gtx.RXLPMEN = lpm

        if tx_power is not None:
            ibs.BP_SHUFFLE.set_tx_power(tx_power)

        if tx_precursor is not None:
            gtx.TXPRECURSOR = tx_precursor

        if tx_postcursor is not None:
            gtx.TXPOSTCURSOR = tx_postcursor

        if dfe_reset:
            gtx.reset_rx_equalizer()
            time.sleep(0.1)

        if reset_stats:
            self.ib.BP_SHUFFLE.reset_stats()

        t0 = time.time()

        finished = False
        try:
            while not finished:
                time.sleep(abs(delay))
                dt = time.time() - t0
                print('At', time.asctime(), f'({datetime.timedelta(seconds=dt)} seconds since the method call)')
                for i, cr in enumerate(icecrates):
                    err_map = [[None] * cr.NUMBER_OF_SLOTS for _ in range(cr.NUMBER_OF_SLOTS)]
                    # print  cr.slot.values()
                    worst_err = 0
                    worst_det = 1
                    for slot, mb in cr.slot.items():
                        errs, det = mb.BP_SHUFFLE.get_rx_lane_monitor(['ERROR_CTR', 'FRAME_DETECT'], lane_group='pcb')
                        worst_err = max(worst_err, max(errs))
                        worst_det = min(worst_det, min(det))

                        if not has_errors[i]:
                            time_without_error[i] = dt

                        if (any(errs) or not all(det)):
                            has_errors[i] = True

                        for lane in range(16):
                            rx_lane_id = (slot, lane)
                            tx_lane = mb.BP_SHUFFLE.get_matching_tx_node_id(rx_lane_id)
                            err_map[rx_lane_id[0] - 1][tx_lane[0] - 1] = (
                                errs[lane] if errs[lane] else '!DET' if not det[lane] else '-')
                            # print '%s -> %s = %i' % (tx_lane, rx_lane_id, errs[lane])
                    row_labels = [f'Tx S{int(mb.slot):02} SN{mb.serial}' for mb in cr.slot.values()]
                    col_labels = [f'Rx S{int(mb.slot):02}\nSN{mb.serial}' for mb in cr.slot.values()]
                    corner_label = f'{cr.get_string_id()}\nCrate #{cr.crate_number}'
                    err_or_frames = (
                        'No Frames!' if not worst_det else f'{worst_err} errors'
                    )
                    print(
                        f'    {cr.get_string_id()}: {err_or_frames:<10} '
                        f'{datetime.timedelta(seconds=int(time_without_error[i]))} without errors '
                        f"{'so far' if not has_errors[i] else ''}"
                    )
                    if verbose:
                        self.print_table(
                            err_map, row_labels=row_labels, col_labels=col_labels,
                            corner_label=corner_label)
                if (stop_on_errors == 1 and any(has_errors)) or (stop_on_errors > 1 and all(has_errors)):
                    break
        except KeyboardInterrupt:
            pass

    def print_net_length_map(self):
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
        self.print_iceboard_table(lambda mb: '\n'.join(mb.hw.qsfp.get_serial_number().map(str)), grid=1)

    def print_frame_info(self):
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

    def _update_arm_firmware(self, image_filename, power_cycle=True):
        """
        Update the ARM SD card firmware. The image must be compressed with bzip2 and the boards must be power cycled.
        """
        self.ib._update_arm_firmware(image_filename, delay=120)
        # if self.ps and power_cycle:
        #     self.ps.unlock()
        #     self.ps.power_cycle(delay=4)

    async def set_adc_delays_async(self, **kwargs):
        """
        Set ADC delays for all Mezzanines on all Motherboards of the array. Calls
        set_adc_delays() on each Motherboard instance with the specified
        paramaters.
        """

        for mb in self.ib:
            if mb.is_open():
                self.logger.debug(f"{self.fpga_array!r}: Setting ADC delays")
                mb.set_adc_delays(**kwargs)
                await asyncio.sleep(0)
            else:
                self.logger.warning(f"{self.fpga_array!r}: Communication with FPGA is not initialized. Cannot set ADC delays")

    def print_adc_frequencies(self):
        """ Displays the frequency of the clocks coming out of each AC chip.
        """

        for mb in self.ib:
            freqs = ', '.join(f"{mb.FreqCtr.read_frequency(f'ADC_CLK{c}', gate_time=0.001) / 1000000.0:7.3f}" for c in (0,4,8,12))
            print(f'{mb!r}: {freqs}')

    IRIGB_SYNC_METHODS = ('irig-b', 'irigb', 'distributed_time')
    TRIG_SYNC_METHODS = ('trig',  'centralized_soft_trigger')
    LOCAL_SYNC_METHODS = ('local', 'local_soft_trigger')

    async def set_sync_method_async(self, method='irig-b', source=None, master=None, master_source=None, master_output=0):
        """ Sets the global syncing method, and setup the boards accordingly.

        Parameters:

            method (string): Specifies the synchronization method:

                - ``'irig-b'`` (or ``'irigb'``, ``'distributed_time'``): All boards sychronize when
                  the target IRIG-B time is reached. `source` specifies the source of the IRIG-B
                  signal that feeds the IRIG-B decoder module.

                - ``'trig'`` (or ``'centralized_soft_trigger'``): All boards synchronize on the
                  rising edge of the signal specified in ``source``.

                - ``'local'`` (or ``'local_soft_trigger'``): Each board generates its own
                  software-generated SYNC trigger when the `sync()` method is called. This can be
                  used to reset the processing pipeline of each board, but does not allow
                  synchronized capture packed capture between board or use of multi-board
                  corner-turning operational modes.

            source: (string): source of synchronization signal.
                - If ``method='irig-b'``, `source` must match one of the available IRIG-B sources (see motherboard method `set_irigb_source_async()`).

                - If ``method='trig'``, `source` must match one of the available REFCLK sources (see REFCLK method `set_sync_source`).

            master (Motherboard, string, int): Motherboard that is to be configured to generate the time
                or trigger signals.

                - None: No master board is set up.
                - `Motherboard` object: use the provided Motherboard object
                - str: Use board with specified serial number (must include all leading zeros)
                - int: Use board in specified slot number (starts with index 1)

            master_source: (string): source of the synchornization signal that the master board will
                send to the master output. Used as ``source`` paramater in method
                `set_user_output_source` called on the master board.

            master_output: (int): User output to which the synchronization signal will be sent. Used
                as ``output`` paramater in method `set_user_output_source` called on the master
                board.

        The boards start their sync process on the next rising edge of the 10
        MHz reference after the trigger event.  This events shoudld
        to occur between two reference clock edges in order to guarantee
        detection on the same edge across the entire array.

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
        # elif method == 'centralized_time_trigger':
        #     source = source or 'bp_time'
        #     # if not master or not master_source:
        #     #     raise ValueError('In the centralized time trigger mode, the master board '
        #     #                      'and its time source must be specified')
        #     # self.ib.set_sync_source(source)
        #     master.set_irigb_source_sync(master_source)
        #     master.set_user_output_source('irigb_trig')
        elif method in self.TRIG_SYNC_METHODS: # REFCLK trigs directly on the rising edge of the specified source signal
            source = source or 'bp_time'
            self.ib.set_sync_source(source)
        elif method in self.LOCAL_SYNC_METHODS:
            self.ib.set_sync_source('local')
            self.ib.sync()
        # elif method == 'external':
        #     source = source or 'sma_a'  # if no source is specified
        #     if master:
        #         raise ValueError('In the local soft trigger mode, a master board should NOT specified')
        #     if master_source:
        #         raise ValueError('In the local soft trigger mode, a master_source should NOT be specified')
        #     self.ib.set_sync_source(source)
        else:
            raise ValueError(f"Unknown syncing method '{method}'")



    async def set_channelizers_async(self, channels=None, local_sync=None, sync=True, **kwargs):
        """
        Configures all channelizers in the array.

        See firmware set_channelizer(...) for details.
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
        """ Configure noise injection gating signal.

        Parameters:

            board:  is either the serial number (as a string) of the target
                board, or is the target motherboard object. If board evaluates to
                False (empty string), the noise injection setup is skipped.

        Notes:

            A sync event is necessary to restart the counters so the gating signal will be generated properly.

        """
        if not board:
            return

        mb = self.get_iceboard(board)

        if enable:
            mb.set_user_output_source('pwm', output=output)

        mb.set_pwm(enable=enable, offset=offset, high_time=high_time,
                   period=period, local_sync=local_sync)

    async def get_user_output_state_async(self, board, output='bp_sma'):
        """ Get the current state of the specified user output and the
        information about its source.

        Parameters:

            board (tuple/list, str, Motherboard):  is either a 2-element tuple or
                list describing the board, or a string containing the board's
                serial number or hostname.

            output (str): name of the user output to which the noise injection
                hardware is connected. This will be used to verify if the PWM
                signal is actually sent to that output.

        Returns:

            A dict containing the following fields:

                board (str or tuple/list): the target board, as specified in the `board` parameter

                output (str): output to which the noise injection electronics
                    is connected, as specified by the `output` parameter.

                output_source (str): name of the source driving the specified
                    'output'.

                pwm_offset (int): the PWM generator waveform offset from frame 0, in frames

                pwm_high_time (int): the high time of the PWM generator waveform, in frames

                pwm_period (int): the period of the PWM generator waveform, in frames

                pwm_enabled (bool): True when the PWM generator is not in reset. Useful when output_source is 'pwm'.

                user_bit0 (int): State of the user bit 0. Useful when output_source is 'user_bit0'.

                user_bit1 (int): State of the user bit 1. Useful when output_source is 'user_bit1'.


            If `board` evaluates to False, only the first 3 fields are returned, with
            output_source set to None.

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
        """ Return the stream_ids if every channel of the array, indexed by channel_id.

        Returns:
            dict if the format {channel_id:stream_id}, where channel_id is a (crate, slot, channel) tuple.
        """

        stream_id_map = {}
        for mb in self.ib:
            stream_id_map.update(mb.get_stream_id_map())

        if len(stream_id_map) != len(set(stream_id_map.values())):
            self.logger.warning(f'{self!r}: Stream IDs are not unique!')

        return stream_id_map

    def update_config(self):
        """Refresh each board's firmware config tree and return board configs.

        Returns:
            list of each board's ``config`` State after :meth:`chFPGA.update_config`.
        """
        return [mb.update_config() for mb in self.ib]

    def get_raw_data_config(self):
        """Get an updated array-wide raw capture config aggregated from the configs of every board in the array."""
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
        self._raw_data_config.capture_format[0]=State.get_common_attr(funcgen_cfgs, "capture_format")
        self._raw_data_config.capture_format[1]=State.get_common_attr(scaler_cfgs, "capture_format")

        # Acknowledge the configs of each board so we don't trigger an update if none of the boards have changed their configs since the last time
        for mb in boards:
            mb.fpga.config.ack(context=ctx)
        return self._raw_data_config

    def get_corr_data_config(self):
        """Assemble array-wide correlator config from firmware CORR States."""
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

        # List of fields to include in the raw data config
        corr_fields = (
            "NCHAN",
            "NCLK",
            "NDECIM",
            "NCORRS",
            "NPROD_PER_CMAC",
            "NCMAC",
            "NBINS",
            "NPROD_PER_ARRAY",
            "firmware_integ_period",
            "bin_map",
            "prod_map",
        )
        # Update config with the common attributes
        self._corr_data_config.update(**{
            attr: State.get_common_attr(corrs_cfgs, attr)
            for attr in corr_fields})
        # Acknowledge the configs of each board so we don't trigger an update if none of the boards have changed their configs since the last time
        for mb in boards:
            mb.fpga.CORR.config.ack(context=ctx)
        return self._corr_data_config
