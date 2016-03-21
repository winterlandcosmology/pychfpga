#!/usr/bin/python
# Disable pylint Line too long (=C0301)
# pylint: disable=C0301

"""
REFCLK.py module
 Implements FMC Reference clock interface
#
# History:
    2011-09-22 JFC: Created
    2011-09-25 JFC: Modified to support new method on incrementing phase (pulse PS_EN unstead of PS_CLK)
    2011-11-15 JFC: Lots of modifications done to debug SYNC clock alignment.
    2012-05-xx JFC: Added disabling SYNC detect when the board is not there, because a floating input create spurious clocks and cause intermittent resets
    2012-09-05 JFC: Updated registers to match firmware. Includes a few status registers to debug SYNC generation mechanism. Added ENABLE_SYNC_GENERATION flag handling to fix spurious generation of SERDES_RST when FMC boar dis not present (the software LOCAL_SYNC and REFCLK noise got the SYNC state machine started and left it in SERDES_RST=1 state)
    2012-09-23 JFC: Removed MMCM status registers. Converted bitfield list to independent variables. Commented out set_refclk200_phase.
"""

from Module import Module_base, BitField

import logging
import time
import numpy as np

class REFCLK_base(Module_base):

    sync_delay=9 # default value.

    CONTROL = BitField.CONTROL
    STATUS = BitField.STATUS
    DRP = BitField.DRP

    # CONTROL bytes
    ADC_SYNC               = BitField(CONTROL, 0, 7, doc='Force a SYNC to the ADC, synchronized on the FMC Reference clock, but bypasses the SYNC state machine that resets the IOSERDES and BUFR')
    LOCAL_SYNC             = BitField(CONTROL, 0, 5, doc='Force the generation of a local SYNC sequence on the local board only. Has the same effect as a SYNC signed received on the 10 MHz clock.  The SYNC is synchronized to the 10 MHz output (transitions on its falling edge)')
    REMOTE_SYNC            = BitField(CONTROL, 0, 4, doc='Generate a SYNC signal encoded on the 10 MHz clock output. Will SYNC the local FMC board only if the 10 MHz output is connected to the 10 MHz input of the local FMC board')
    SYNC_SOURCE            = BitField(CONTROL, 0, 0, width=2, doc="Selects the source of the SYNC signal: 0: local sync only, 1: local sync or sync recovered from the clock, 2: local sync or backplane sync, 3: local sync or IRIG-B-based sync")

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

    def __init__(self, fpga, base_address):
        self.fpga = fpga
        self.logger = logging.getLogger(__name__)
        super(self.__class__, self).__init__(fpga, base_address)

    def init(self):
        # Sets the REFCLK delay to zero by default.
        self.set_refclk_delay(0)
        self.set_sync_delay(1)
        self.logger = logging.getLogger(__name__)
        # If the board is not present, disable SYNC detection on REFCLK to prevent noise on the floating REFCLK lien to generate spurioys resets.
        # self.ENABLE_SYNC_DETECTION = 1
        # self.ENABLE_SYNC_GENERATION = 1
        if self.fpga.is_fmc_present(0):
            self.logger.info('   REFCLK is using the 10 MHz reference clock from the ADC board')
            self.REFCLK_SEL = 0  # Use REFCLK coming from the FMC
        else:
            self.logger.info('   REFCLK is using the 10 MHz reference clock from FPGA since the ADC board is not prresent in FMC slot 0')
            self.REFCLK_SEL = 1  # Use internally generated REFCLK

    SYNC_SOURCE_TABLE = {
        'local': 0,
        'refclk': 1,
        'bp_trig': 2,
        'irigb': 3,
        'bp_time': 4}

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

    def sync(self, delay=None):
        if delay is not None:
            self.set_sync_delay(delay)
        self.set_refclk_delay(self.sync_delay)
        self.pulse_bit('REMOTE_SYNC')
        self.wait_for_bit('SYNC_DONE')

    def local_sync(self, delay=None):
        """
        Locally generates a SYNC pulse on the current board's ADCs and reset the data acquisition logic.
        This is the same as receiving a SYNC signal encoded on the 10 MHz reference clock.
        The timing of the sync pulse (delay relative to FMC 10 MHz reference clock can optionally be specified).
            If delay=None or is omited, the previous SYNC timing will be used.
            If delay is an integer between 0 and 31, the timing delay is set to that value.
        """
        if delay is None:
            self.set_refclk_delay(self.sync_delay)
        else:
            self.set_sync_delay(delay)
            self.set_refclk_delay(delay)
        # self.pulse_bit('LOCAL_SYNC') # Force the REFCLK state machine to initiate a SYNC event
        self.LOCAL_SYNC = 1
        self.LOCAL_SYNC = 0
        self.wait_for_bit('SYNC_DONE') # Wait until the SYNC process is completed

    def set_sync_delay(self, delay):
        """
        Sets the delay of the SYNC pulse relative to the Reference Clock. Valid range is 0-31.
        """
        self.sync_delay = delay  # Save the current delay value
        self.set_refclk_delay(delay)

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
        try:
            d0, d1 = delay
        except TypeError:
            d0 = d1 = delay
        self.REFCLK0_DELAY = d0
        self.REFCLK1_DELAY = d1
        self.pulse_bit('REFCLK_DELAY_RST')

    def acquire_adc_clock_waveform(self, channels=range(16),  sleep=0.005):
        """
        Measures the waveform of the 400 MHz ADC input clock forthe specifid ADC channels.
        This is done by sweeping the delay on the 10 MHz reference clock and sampling the ADC clock signal in that delayed clock for every delay value.
        32 samples are taken over total delay of 2.5 ns (78.125 ps/sample). The acquisition therefore spans the full period of a 400 MHz signal, and it is graranteed that a transition will be observed.

        'sleep' indicates how much time to wait between samples are taken.

        The method returns a numpy array of Nx32 integers. First dimension is the ADC number, second dimension is the delay.
        """
        # tap_delay = 1/200e6/32/2
        old_refclk_delay = self.get_refclk_delay()
        number_of_channels = max(channels) + 1
        samples = np.zeros((number_of_channels, 32), np.int8) # prepare an empty array that wil lcontain the clock sample values for all ADCs and all delay values.
        for delay in range(32):
            self.set_refclk_delay(delay)
            time.sleep(sleep)
            for ch in channels:
                samples[ch][delay] = self.fpga.ANT[ch].ADCDAQ.ADC_CLK_SAMPLE

        self.set_refclk_delay(old_refclk_delay)  # Return the reference clock delay to a known state
        return samples

    def print_bit_vector(self, samples, mark):
        print self.bit_vector_to_string(samples, mark)

    def bit_vector_to_string(self, samples, mark):

        bitstring = ''
        for i in range(len(samples)):
            if i == mark:
                bitstring += '!'
            else:
                bitstring += ('.','#')[bool(samples[i])]
        return bitstring

    # def compute_phase(self, s, freq=400e6, dt=1/200e6/32/2):
    #     r""" Compute the phase of a periodic signal by providing a vector of
    #     samples of that clock.

    #     The vector 's' is a Boolean array.

    #     The returned value is the sample number corresponding if the estimated
    #     rising edge of the signal (assuming a 50% duty cycle)
    #     """
    #     rad_per_sample = dt/(1/freq)*2*np.pi;
    #     phi = np.arange(len(s))*rad_per_sample  # phase corresponding to each sample
    #     #s=2*s-1 # convert array into -1 or +1
    #     s *= 1.0
    #     v = np.average(s*np.cos(s*phi)+1j*np.sin(s*phi))
    #     if abs(v) < 2/np.pi/4:
    #         self.logger.warning('Warning: bad signal to noise in determining phase of ADC_CLK')
    #     delay = np.mod((np.angle(v)/(2*np.pi))*32, 32) # substract a quarter of cycle to get the rising edge position assuming a 50% duty cycle

    #     return delay



    def find_edges(self, s, min_step=.5, stable_time=4, edge_type='both'):
        """
        Finds the position of all positive or negative edges of amplitude
        'min_step' followed by 'stable_time' samples where such a transition does
        not occur.
        """

        s = s * 1.0  # Convert array to float array
        if edge_type == 'both':
            ds = (np.abs(np.diff(s)) > min_step)
        elif edge_type == 'pos':
            ds = (np.diff(s) > min_step)
        elif edge_type == 'neg':
            ds = (np.diff(s) < - min_step)
        else:
            raise ValueError('Wrong type of edge wa specified')
        ds = ds * 2 - 1  # Convert Boolean to +1 or -1 values for correlation
        w = np.hstack(([1], [-1] * stable_time))  # Compute a window
        p = np.correlate(ds, w, mode='valid')
        return np.where(p == len(w))[0] + 1

    def find_rising_edge(self, s, period=32,  stable_time=2):
        """ Find the position of the first stable rising edge of the signal.
        If only falling edges are detected,  compute the position of next
        rising edge based on the first fallign edge and the signal period.

        """
        pos_edges = self.find_edges(s, min_step=0.5, stable_time=stable_time, edge_type='pos')
        if len(pos_edges):
            return pos_edges[0] % period
        neg_edges = self.find_edges(s, min_step=0.5, stable_time=stable_time, edge_type='neg')
        if len(neg_edges):
            return (neg_edges[0]+int(period/2.0)) % period
        raise RuntimeError('Could not find any edge')

    def unwrap(self, v, step, threshold=None):
        """
        Removes jumps of +/-step. If not specified, 'threshold' is 'step/2'.
        """
        if threshold is None:
            threshold = step/2.0
        ds = np.hstack(([0], np.diff(v*1.0)))
        vv = v - np.cumsum((np.abs(ds) >= threshold)*np.sign(ds)*step)
        return vv


    def compute_sync_delay(self, channels=[0, 4, 8, 12], sleep=0.001, repeat=1, delays=range(32), plot_adc=None, adc_clock_freq=400e6, set_sync_delays=False):
        """
        Compute and optionnally set the recommended ADC SYNC pulse timing to
        ensure that it will meet the ADC timing requirments and produce
        reproducible data acquisition timing relative to the 10MHz systrem
        reference clock.

        This is done by sweeping the timing of the SYNC pulse over a range of
        2.5 ns in 32 steps (78.125 ps steps) and by measuring the 400 MHz ADC
        data clock waveform for each sync delay value.

        Phase discontinuities will be seen where the timing requirments is not
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
        tap_delay = 1/200e6/32/2
        adc_clock_period_in_taps = (1/adc_clock_freq)/tap_delay
        number_of_channels = max(channels) + 1
        samples = []

        if isinstance(delays, int):
            delays = [delays]
        number_of_sync_delays = len(delays)


        if plot_adc is not None:
            import matplotlib.pyplot as plt
            plt.figure(1)
            plt.clf()
            plt.hold(1)
            plt.axis([0, 3*32, min(delays)-1, max(delays)+1])
            plt.xlabel('Time (tap delays, repeated 3 times)');
            plt.ylabel('Sync delay (tap delays)');
            plt.title('ADC%i clock waveform as a function of ADC_SYNC timing delay' % plot_adc)

        rising_edge = np.ones((number_of_channels, number_of_sync_delays))*np.nan  # One rising_edge for each ADC channel number

        for sync_delay in delays:
            print 'Sync delay %2i:' % (sync_delay),
            self.local_sync(sync_delay)
            samples = self.acquire_adc_clock_waveform(channels=channels, sleep=sleep) # Measure the ADC clock waveform for all ADCs
            for ADC_number in channels:
                rising_edge[ADC_number][sync_delay] = self.find_rising_edge(samples[ADC_number], period=adc_clock_period_in_taps)
                mark = rising_edge[ADC_number][sync_delay]
                bitstring = self.bit_vector_to_string(samples[ADC_number], mark)
                print 'ADC%2i: %s' % (ADC_number, bitstring),
            print
            if plot_adc is not None:
                extended_samples = np.hstack((samples[plot_adc], samples[plot_adc], samples[plot_adc]))
                plt.plot((extended_samples-.5)*.5 + sync_delay, 'k-')
                plt.plot([rising_edge[plot_adc][sync_delay]+adc_clock_period_in_taps], [sync_delay], 'go')
                plt.draw()

        print
        print 'Channel-wise Computed SYNC delays'
        sync_delays = np.ones(number_of_channels)*np.nan
        for ch in channels:
            print 'ADC%2i:' % (ch),
            rising_edge[ch] = self.unwrap(rising_edge[ch], step=adc_clock_period_in_taps, threshold=adc_clock_period_in_taps*3/4) + adc_clock_period_in_taps  # removes jumps greater than 16
            # rising_edge[ch] -= min(rising_edge[ch])
            phase_jumps = self.find_edges(rising_edge[ch], min_step=3, stable_time=4)
            print 'Phase jumps found at delays (%s)' % phase_jumps,
            if len(phase_jumps) < 2:
                print 'Insufficient number of ADC_CLK phase jumps edges to determine optimal SYNC timing'
            else:
                sync_delays[ch] = phase_jumps[0] + np.average(np.diff(phase_jumps*1.0))*0.40 # place sync at a fraction of the average distance between phase jumps
                print ' Recommended sync_delay: %i' % int(round(sync_delays[ch]))

        print
        print 'ADC-chip--wise Computed SYNC delays'
        adc_sync_delays = np.ones(4)*np.nan
        for adc_chip in range(4):
            adc_sync_delays[adc_chip] = int(round(np.average([sync_delays[ch] for ch in channels if 4*adc_chip <= ch <= 4*adc_chip+3])))
            print 'ADC Chip #%i: sync delay: %i' % (adc_chip, adc_sync_delays[adc_chip])

        print
        print 'Mezzanine-wise SYNC delays'
        mezz_sync_delays = np.ones(2)*np.nan
        for mezz in range(2):
            mezz_sync_delays[mezz] = int(round(np.average([sync_delays[ch] for ch in channels if 8*mezz <= ch <= 8*mezz+7])))
            print 'Mezzanine #%i sync delay: %i' % (mezz, mezz_sync_delays[mezz])

        print
        print "ADC Clock phase shifts ('|' marks computed optimal sync delay)"
        for ch in channels:
            s = list(''.join([('#-' if edge else '##') for edge in np.abs(np.diff(rising_edge[ch])) > 3]))
            s[2 * int(mezz_sync_delays[ch//8])] = '|'
            print 'CH%02i: %s' % (ch, ''.join(s))

        if plot_adc is not None:
            plt.plot(rising_edge[plot_adc], range(number_of_sync_delays), 'bo-')
            for mezz in range(2):
                plt.plot([0, 3*32], [mezz_sync_delays[mezz]]*2, 'ro-')
            plt.figure(2)
            plt.clf()
            plt.hold(1)
            plt.axis([0, 32, min(channels)-1, max(channels)+1])
            plt.gca().invert_yaxis()
            ymin, ymax = plt.gca().get_ylim()
            plt.gca().yaxis.set_ticks(np.arange(ymin, ymax, -1))
            plt.xlabel('Sync delay');
            plt.ylabel('Channel number');
            plt.title('Location of ADC data clock phase jumps as a function of ADC_SYNC timing delay')
            for ch in channels:
                w = np.cumprod(np.append([1], (np.abs(np.diff(rising_edge[ch])) <= 3)*2-1))
                x = np.arange(number_of_sync_delays)
                plt.fill_between(x, w*0.4 + ch, -w*0.4 + ch, interpolate=True)
            for mezz in range(2):
                ch = [c for c in channels if mezz*8 <= c <= mezz*8+7]
                plt.plot([mezz_sync_delays[mezz]]*2, [min(ch), max(ch)], 'ro-')
            plt.draw()

        if set_sync_delays:
            self.set_sync_delay(mezz_sync_delays)

        return mezz_sync_delays

    def status(self):
        """
        Displays the status of the REFCLK module.
        """
        # self.logger.info('-----------------------REFCLK------------------------------------')
        # self.logger.info('SYNC Detection Enabled: %s' % (bool(self.ENABLE_SYNC_DETECTION)))


