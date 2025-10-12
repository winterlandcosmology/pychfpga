"""Implements the interface to the REFCLK FPGA module.

.. History:
    2011-09-22 JFC: Created
    2011-09-25 JFC: Modified to support new method on incrementing phase (pulse PS_EN unstead of PS_CLK)
    2011-11-15 JFC: Lots of modifications done to debug SYNC clock alignment.
    2012-05-xx JFC: Added disabling SYNC detect when the board is not there, because a floating input create spurious clocks and cause intermittent resets
    2012-09-05 JFC: Updated registers to match firmware. Includes a few status registers to debug SYNC generation mechanism. Added ENABLE_SYNC_GENERATION flag handling to fix spurious generation of SERDES_RST when FMC boar dis not present (the software LOCAL_SYNC and REFCLK noise got the SYNC state machine started and left it in SERDES_RST=1 state)
    2012-09-23 JFC: Removed MMCM status registers. Converted bitfield list to independent variables. Commented out set_refclk200_phase.
"""

from ..mmi import MMI, BitField, CONTROL, STATUS, DRP

import logging
import time
import numpy as np

class REFCLK(MMI):

    ADDRESS_WIDTH = 12

    sync_delay = 9  # default value.

     # CONTROL bytes
    ADC_SYNC               = BitField(CONTROL, 0, 7, doc='Force a SYNC to the ADC, synchronized on the FMC Reference clock, but bypasses the SYNC state machine that resets the IOSERDES and BUFR')
    LOCAL_SYNC             = BitField(CONTROL, 0, 5, doc='Force the generation of a local SYNC sequence on the local board only. Has the same effect as a SYNC signed received on the 10 MHz clock.  The SYNC is synchronized to the 10 MHz output (transitions on its falling edge)')
    REMOTE_SYNC            = BitField(CONTROL, 0, 4, doc='Generate a SYNC signal encoded on the 10 MHz clock output. Will SYNC the local FMC board only if the 10 MHz output is connected to the 10 MHz input of the local FMC board')
    SYNC_SOURCE            = BitField(CONTROL, 0, 0, width=3, doc="Selects the source of the SYNC signal: 0: local sync only, 1: local sync or sync recovered from the clock, 2: local sync or backplane sync, 3: local sync or IRIG-B-based sync")

    REFCLK_SEL             = BitField(CONTROL, 0x01, 7, doc='Selects the source of the REFCLK needed for SYNC generation. 0=FMC, 1=internal REFCLK generator.')
    # ENABLE_SYNC_GENERATION = BitField(CONTROL, 0x01, 6, doc='Allows the internal state machine to generate the SYNC sequence (generate the ADC SYNC and resets the ADCDAQ SERDES and BUFG)')
    # ENABLE_SYNC_DETECTION  = BitField(CONTROL, 0x01, 5, doc='When 1, enable SYNC detection based on the Refecence clock pulse length. Disable if the FMC board is not present to prevent spurious resets of the data path.')
    REFCLK1_DELAY           = BitField(CONTROL, 0x01, 0, width=5, doc='Delay applied to the FMC1 Reference clock. Must pulse REFCLK_DELAY_RST to load.')

    REFCLK_DELAY_RST       = BitField(CONTROL, 0x02, 7, doc='Resets the REFCLK line IODELAY and loads the delay value specified in REFCLK0_DELAY.')
    REFCLK0_DELAY           = BitField(CONTROL, 0x02, 0, width=5, doc='Delay applied to the FMC0 Reference clock. Must pulse REFCLK_DELAY_RST to load.')

    # STATUS bytes
    SYNC_CTR               = BitField(STATUS, 0x00, 4, width=4, doc='Counts the SYNC events')
    DCI_LOCKED             = BitField(STATUS, 0x00, 3, doc='1 when DCI is locked')
    RECOVERED_SYNC         = BitField(STATUS, 0x00, 2, doc='1 when a SYNC signal encoded on the 10 MHz is detected ')
    SYNC                   = BitField(STATUS, 0x00, 1, doc='Status on the internal SYNC signal, which is a combination of various sources (recovered from RefClk, from pin, from bit etc)')
    SERDES_RST             = BitField(STATUS, 0x00, 0, doc='Status of the SERDER Reset output line')

    DIFF_COUNTER           = BitField(STATUS, 0x01, 0, width=8, doc='DIfference between clocks')

    SYNC_DONE              = BitField(STATUS, 0x02, 5, doc='1 when the local SYNC process is completed')
    SYNC_IN                = BitField(STATUS, 0x02, 0, doc='reflects the level on the sync_in port')
    # SYNC_DELAY_READBACK  = BitField(STATUS, 0x02, 0, width=5,doc='Reads back the delay set onthe SYNC IODELAY')

    def __init__(self, *, router, router_port):
        self.logger = logging.getLogger(__name__)
        super().__init__(router=router, router_port=router_port)

    def init(self):
        # Sets the REFCLK delay to zero by default.
        # self.set_refclk_delay(0)
        self.set_sync_delays(1)
        self.logger = logging.getLogger(__name__)

        # If the board is not present, disable SYNC detection on REFCLK to
        # prevent noise on the floating REFCLK lien to generate spurious
        # resets.

        # self.ENABLE_SYNC_DETECTION = 1
        # self.ENABLE_SYNC_GENERATION = 1
        if self.fpga.HAS_FMC and self.fpga.is_fmc_present(0):
            self.logger.debug('%r:   REFCLK is using the 10 MHz reference clock from the ADC board' % self.fpga)
            self.REFCLK_SEL = 0  # Use REFCLK coming from the FMC
        else:
            self.logger.debug('%r:   REFCLK is using the 10 MHz reference clock from FPGA since the ADC board '
                              'is not present in FMC slot 0' % self.fpga)
            self.REFCLK_SEL = 1  # Use internally generated REFCLK

    SYNC_SOURCE_TABLE = {
        'local': 0,  # No external trigger, software only
        'refclk': 1,  # Refclk pulse width
        'bp_trig': 2,  # Backplane trig line
        'irigb': 3,  # Output of the IRIG-B timestamp comparator
        'bp_time': 4,  # Backplane time signal
        'bp_gpio_int': 5,  # backplane GPIO interrupt line
        'sma_a': 6,  # IceBoard SMA_A
        'sma_b': 7}  # Iceboard SMA B

    def set_sync_source(self, source):
        """ Set the source of the SYNC signal."""
        if source not in self.SYNC_SOURCE_TABLE:
            raise ValueError('Invalid SYNC source name. Valid names are %s' % ', '.join(self.SYNC_SOURCE_TABLE))
        self.SYNC_SOURCE = self.SYNC_SOURCE_TABLE[source]

    def get_sync_source(self):
        """ Get the name of the current source of the SYNC signal."""
        source = self.SYNC_SOURCE
        for (source_name, source_number) in self.SYNC_SOURCE_TABLE.items():
            if source == source_number:
                return source_name
        raise ValueError('The REFCLK module has an unknown SYNC source')

    def remote_sync(self, delay=None):
        """
        """
        self.set_sync_delays(delay)
        self.pulse_bit('REMOTE_SYNC')
        self.wait_for_bit('SYNC_DONE')

    def local_sync(self, delay=None, max_trials=3):
        """
        Locally generates a SYNC pulse on the current board's ADCs and reset the data acquisition logic.
        This is the same as receiving a SYNC signal encoded on the 10 MHz reference clock.
        The timing of the sync pulse (delay relative to FMC 10 MHz reference clock can optionally be specified).

        Parameters:
            delay: Specify sync signal timing delays

                - If delay=None or is omited, the previous SYNC timing will be used.
                - If delay is an integer between 0 and 31, the timing delay is set to that value.

            max_trials (int):
        """
        self.set_sync_delays(delay)
        # self.pulse_bit('LOCAL_SYNC') # Force the REFCLK state machine to initiate a SYNC event

        trial = 0
        while True:
            self.LOCAL_SYNC = 1
            self.LOCAL_SYNC = 0
            if self.wait_for_bit('SYNC_DONE', no_error=True):  # Wait until the SYNC process is completed
                return
            trial += 1
            if trial >= max_trials:
                raise RuntimeError('Local SYNC failed after %i trials' % trial)
            else:
                self.logger.warning('%r: Local SYNC failed on trial %i/%i. Retrying...' % (self, trial, max_trials))

    def set_sync_delays(self, delay):
        """
        Sets the delay of the ADC SYNC pulse relative to the Reference Clock.


        The delay is stored in an internal variable (self.sync_delays) which is used to set the
        REFCLK delay prior to generate SYNC events.

        Parameters:

        delay (tuple): (mez0_delay, mezz1_delay) tuple specifying the SYNC
            delay for each mezzanine. Valid range for delay values is 0-31.
            If delay is None, the SYNC delays are set to the stored
            sync_delay.
        """
        if delay is None:
            self.set_refclk_delay(self.sync_delay)

        else:
            self.set_refclk_delay(delay)
            self.sync_delay = self.get_refclk_delay()  # Save the current delay value

        # if set_sync_delay was called with -1 we didn't actually set anything so the saved value
        # shouldnt be -1 it should be what ever is actually used

    def get_sync_delays(self):
        return self.sync_delay

    def get_refclk_delay(self):
        """
        Get the current delays on the Mezzanine Reference clock for both mezzanines.
        """
        return (self.REFCLK0_DELAY, self.REFCLK1_DELAY)

    def set_refclk_delay(self, delay):
        """
        Sets the delay applied on the Mezzanine Reference clock of the two
        mezzanines as they enter the FPGA. Valid range is 0-31. These delays
        are used to scan the ADC clock waveform and to set the ADC SYNC
        timing.
        """
        # print(f'Setting REFCLK delays to {delay}')
        try:
            d0, d1 = delay
        except TypeError:
            d0 = d1 = delay
        if d0 != -1:
            self.REFCLK0_DELAY = d0
        if d1 != -1:
            self.REFCLK1_DELAY = d1
        self.pulse_bit('REFCLK_DELAY_RST')

    def acquire_adc_clock_waveforms(self, channels=list(range(16))):
        """
        Measures the waveform of the 400 MHz ADC input clock for the specified ADC channels.

        This is done by sweeping the delay on the 10 MHz reference clock and
        sampling the ADC clock signal on the rising edge of that delayed
        clock. 32 samples are taken over total delay of 2.5 ns (78.125
        ps/sample). The acquisition therefore spans the full period of a 400
        MHz signal, and it is guaranteed that a transition will be observed.

        The method returns a N x 32 numpy array (first dimension (row) is the
        channel, second dimension is the sample for each of the 32 tap
        delays).
        """
        old_refclk_delay = self.get_refclk_delay()

        # prepare an empty array that will contain the ADC clock waveform for each channel
        waveforms = np.zeros((len(channels), 32))

        # self.fpga.chan[channels[0]].ADCDAQ.wait_for_bit('FIFO_EMPTY', target_value=0) # Make sure the ADCDAQ SYNC process is completed
        for delay in range(32):
            self.set_refclk_delay(delay)
            time.sleep(0.002)
            for i, ch in enumerate(channels):
                waveforms[i][delay] = self.fpga.chan[ch].ADCDAQ.ADC_CLK_SAMPLE
        self.set_refclk_delay(old_refclk_delay)  # Return the reference clock delay to a known state
        return waveforms

    def find_rising_edges(self, waveforms, period=32, stable_time=None):
        """
        Find the location of the rising edge of each waveform in ``waveforms``
        by searching for a stable rising edge, or if not is found, a stable
        falling edge to which an offset is added to point towards the expected
        location of the rising edge.

        ``waveform`` must be a (N x S) numpy array, where N is the
        number of channels, and S is the number of samples in the waveform.
        Each element of the waveform is an integer (1 or 0s).

        The position of the rising edge is an integer modulo 'period'. It is
        therefore necessarily between 0 and ``period-1``, but might be larger than
        the number of samples in the waveform if the period is longer than the
        waveform.

        Returns a numpy single-dimension vector of floats of the rising edge position for
        waveform. If no valid rising or falling edge is found for a waveform,
        NaN is returned for that channel (hence the use of a float array).
        """
        if stable_time is None:
            stable_time = period / 8
        stable_time = int(stable_time)

        rising_edges = np.zeros(waveforms.shape[0])  # make sure the returned value is always an Int
        for i, waveform in enumerate(waveforms):  # for each row
            # Convert to a string of "1" and "0"s so we can use the 'find'
            # method. before doing that, make sure this is an int8 array
            # otherwise we'll get more than one char per value...
            s = (waveform.astype(np.int8) + ord(b'0')).tobytes()
            re = s.find(b'0' + b'1' * stable_time)
            fe = s.find(b'1' * stable_time + b'0')
            if re >= 0:
                rising_edges[i] = (re + 1) % period
            elif fe >= 0:
                rising_edges[i] = (fe + stable_time + period / 2.0) % period
            else:
                rising_edges[i] = np.NaN
        return rising_edges

    def find_centers(self, edge_map, threshold=4):
        """
        For each column of ``edge_map``, find the centers of runs of 0s longer
        than ``threshold`` that are bounded by '1's.
        """
        centers = []
        for edges in edge_map.T:
            pos_ones = np.nonzero(edges)[0]
            if len(pos_ones) < 2:  # we don't have 2 edges, can't compute
                pos = []
            else:
                run_length = np.diff(pos_ones)
                # i = np.argmax(run_length)  # index_of_longest_run
                i = np.where(run_length >= threshold)[0]  # indices of where  run_length > threshold

                # compute the centers. We add 1 so we are a bit further from the end of the run than the beginning.
                pos = [(pos_ones[ii] + pos_ones[ii + 1] + 1) // 2 for ii in i]
            centers.append(pos)
        return centers

    def compute_sync_delays(
            self,
            channels=[0, 4, 8, 12],
            adc_clock_freq=400e6,
            set_sync_delays=False,
            verbose=1,
            sync_sleep=0.010):
        """
        Compute and optionally set the recommended ADC SYNC pulse timing to
        ensure that it will meet the ADC timing requirements and produce
        reproducible data acquisition timing relative to the 10MHz system
        reference clock.

        This is done by sweeping the timing of the SYNC pulse over a range of
        2.5 ns in 32 steps (78.125 ps per step) and by measuring the 400 MHz ADC
        data clock waveform for each sync delay value.

        Phase discontinuities will be seen where the timing requirements is not
        met (i.e. the SYNC falling edge is too close to the 1600 MHz ADC input
        clock and the setup or hold requirements are not met).

        The algorithm then look for those discontinuities, and computes the
        delay that will place the SYNC between the first two of them.
        The average SYNC timing for all specified ADCs is used as the optimal value.

        NOTE: This will work only of the ADC board is configured to SYNC the
        ADC directly from the SYNC signal coming from the FPGA.

        On the MGADC08 REV2 boards, this means:
            1) The FPGA SYNC is used as a source by setting the appropriate
               control bit on the SYNC mux.
            2) The SYNC Flip Flop is bypassed by hardware, and the ADC SYNC
               selection mux control bit must also be set to use the bypassed
               input.
        """
        old_sync_delays = self.get_sync_delays()  # Save the current delay value
        tap_delay = 1 / 200e6 / 32 / 2
        adc_data_clock_period = int((1 / adc_clock_freq) / tap_delay)  # 400 MHz period in tap delays
        adc_input_clock_period = int((1 / 1600e6) / tap_delay)  # 1600 MHz period in tap delays

        #  Measure the ADC clock waveform for each of the 32 possible sync delays
        waveforms = np.zeros((32, len(channels), 32))  # 32 sync delays x N channels x  32-sample waveform/channel
        rising_edges = np.zeros((32, len(channels)))  # 32 rows for sync delays, N columns for channels
        for sync_delay in range(32):
            self.local_sync(sync_delay)
            time.sleep(sync_sleep)
            # Measure the ADC clock waveform for all ADCs
            waveforms[sync_delay] = self.acquire_adc_clock_waveforms(channels=channels)
            # Find the position of the rising edge for the ADC clock waveform for each channel
            rising_edges[sync_delay] = self.find_rising_edges(waveforms[sync_delay], period=adc_data_clock_period)

        # Now find where phase jump occur on the waveform for each channel
        phases = 2 * np.pi * rising_edges / adc_data_clock_period
        delta_phases = np.diff(phases, axis=0)
        # Compute the smallest angle between the 2 angles while correctly dealing with wraparounds (e.g. 0 and 31 are separated by 1, not 31)
        delta_phases = np.arctan2(np.sin(delta_phases), np.cos(delta_phases))
        # We then create a vector that has '1' where large phase transition occur
        jumps = (np.abs(delta_phases) > 2 * np.pi / adc_data_clock_period * adc_input_clock_period / 2) * 1
        centers = self.find_centers(jumps, threshold=adc_input_clock_period / 2)

        # Gather the sync delays for each ADC board
        adc_board_sync_delays = [[], []]
        for i, ch in enumerate(channels):
            adc_board_sync_delays[ch // 8].extend(centers[i])

        # Average the delays
        adc_board_average_sync_delays = [[], []]
        for i, adc_board_sync_delay in enumerate(adc_board_sync_delays):
            if adc_board_sync_delay:
                a = np.array(adc_board_sync_delay) * 2 * np.pi / adc_input_clock_period
                adc_board_average_sync_delays[i] = (
                    (int(np.round(np.arctan2(np.sum(np.sin(a)), np.sum(np.cos(a)))
                     / 2 / np.pi * adc_input_clock_period)) + 1) % adc_input_clock_period)
            else:
                adc_board_average_sync_delays[i] = None

        if verbose:
            print('ADC Clock waveform for %r' % self.fpga)
            for sync_delay in range(32):
                print('Sync delay %2i:' % (sync_delay, ), end=' ')
                for i, ch in enumerate(channels):
                    # Compute string representing the bits. Pad in case there the clock period is longer than 32
                    bitstring = bytearray((waveforms[sync_delay][i].astype(np.int8) + ord(b'0')).tobytes() + (b' ' * (adc_data_clock_period - 32)))
                    if ~np.isnan(rising_edges[sync_delay, i]):
                        edge_pos = int(rising_edges[sync_delay, i])
                        if bitstring[edge_pos] == ord(b'1'):
                            # we converted the string to bytearray so we could do assignments like this
                            bitstring[edge_pos] = ord(b'!')
                        else:
                            bitstring[edge_pos] = ord(b'?')
                        jump_flag = ('>' if sync_delay in centers[i] else
                                     '-' if sync_delay < 31 and jumps[sync_delay, i] else
                                     ' ')
                    else:
                        jump_flag = '?'
                    print('CH%02i %3.0f %s: %s ' % (ch, rising_edges[sync_delay, i], jump_flag, bitstring.decode()), end=' ')
                print()
            print('   ADC boards average sync delays:', adc_board_average_sync_delays)

        if any(d is None for d in adc_board_average_sync_delays):
            raise RuntimeError('Unable to compute SYNC delays.')

        if set_sync_delays:
            self.set_sync_delays(adc_board_average_sync_delays)
        else:
            self.set_sync_delays(old_sync_delays)
        return adc_board_average_sync_delays

    def check_sync_delays(
            self,
            sync_delay=None,
            channels=[0, 4, 8, 12],
            trials=10,
            adc_clock_freq=400e6,
            sync_sleep=0.010,
            verbose=True):
        """Verify that the ADC clock waveforms are stable when measured after
        syncing the ADCs ``trials`` times. It is assumed that the ADCs are in
        pulse mode.

        Returns the number of detected phase jumps for all specified ADCs
        combined. A value of zero means that the SYNC alignment is adequate.


        Parameters:

            sync_delay (tuple): (delay_mezz0, delay_mezz1) tuple providing the
                sync delays for each of the mezzanine. Delays are integer
                between 0 and 31.

            channels (tuple): channels for which the sync delays are checked.
                It is sufficient to check one channel per ADC chip.

            trials (int): numbe of times we check the ADC clock waveform at
                the specified delays

            adc_clock_freq (float); frequency of the data clock coming out of
                the ADC

            sync_sleep (float): delay in seconds.

            verbose (bool): If True, messages on the progress of the tests
                will be printed

        Returns:

            int: number of observed phase jumps in the meazured data. Zero
                means the data is valid.

        """
        old_sync_delays = self.get_sync_delays()  # Save the current delay value
        tap_delay = 1 / 200e6 / 32 / 2
        adc_data_clock_period = int((1 / adc_clock_freq) / tap_delay)  # 400 MHz period in tap delays
        adc_input_clock_period = int((1 / 1600e6) / tap_delay)  # 1600 MHz period in tap delays

        errors = 0
        if sync_delay is not None:
            self.set_sync_delays(sync_delay)
        last_rising_edges = None
        if verbose:
            print('Sync delay checks for %r' % (self.fpga))
            print('ADC Clock waveform, Sync delay = %s' % (self.get_sync_delays(),))
        for trial in range(trials):
            self.local_sync()
            time.sleep(sync_sleep)
            # Measure the ADC clock waveform for all ADCs
            waveforms = self.acquire_adc_clock_waveforms(channels=channels)
            # Find the position of the rising edge for the ADC clock waveform for each channel
            rising_edges = self.find_rising_edges(waveforms, period=adc_data_clock_period)
            if last_rising_edges is not None:
                delta_phases = (last_rising_edges - rising_edges) * 2 * np.pi / adc_data_clock_period
                # compute the smallest angle between the 2 angles while
                # correctly dealing with wraparounds (e.g. 0 and 31 are
                # separated by 1, not 31)
                delta_phases = np.arctan2(np.sin(delta_phases), np.cos(delta_phases))
                jumps = (np.abs(delta_phases) > 2 * np.pi / adc_data_clock_period * adc_input_clock_period / 2) * 1
                if any(jumps):
                    errors += 1
            else:
                jumps = [0] * len(channels)
            last_rising_edges = rising_edges
            if verbose:
                print('Trial #%3i:' % (trial + 1), end=' ')
                for i, ch in enumerate(channels):
                    # Compute string representation of bits. Pad in case there the clock period is longer than 32
                    bitstring = bytearray((waveforms[i].astype(np.int8) + ord('0')).tobytes())
                    print('CH%02i %s: %s ' % (ch, '!' if jumps[i] else ' ', bitstring.decode()), end=' ')
                print()
        self.set_sync_delays(old_sync_delays)
        return errors

    def status(self):
        """
        Displays the status of the REFCLK module.
        """
        # self.logger.info('-----------------------REFCLK------------------------------------')
        # self.logger.info('SYNC Detection Enabled: %s' % (bool(self.ENABLE_SYNC_DETECTION)))
