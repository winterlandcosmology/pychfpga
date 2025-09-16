#!/usr/bin/python
# Disable pylint Line too long (=C0301)
# pylint: disable=C0301

"""
chan.py module
    Implements interface to the channelizer modules

History:
    2011-07-12 : JFC : Created from test code in chFPGA.py
    2012-08-31 JFC: Swapped addresses of PROBER and SCALER to match the same change in firmware
    2012-09-25 JFC: Renamed to FRAMER and FR_DIST to SRCSEL
"""

# Standard packages

import logging

# Pypi packages

from numpy import NaN as npNaN

# Local packages

from ..mmi import MMIRouter
from . import adcdaq
from . import fft
from . import scaler
from . import prober
from . import funcgen


class Chan(MMIRouter):
    """ Implements the interface to an individual channelizer"""
    ROUTER_PORT_NUMBER_WIDTH = 3
    ROUTER_PORT_MAP = {
        'ADCDAQ': 0,
        'FFT': 2,
        'SCALER': 3,
        'PROBER': 4,
        'FUNCGEN': 5
    }

    def __init__(self, router, router_port, instance_number):
        self.chan_number = instance_number  # store current channelizer number for this instance
        self.logger = logging.getLogger(__name__)
        super().__init__(router=router, router_port=router_port)

        if self.fpga.HAS_ADCDAQ:
            self.ADCDAQ = adcdaq.ADCDAQ(router=self, router_port='ADCDAQ', instance_number=instance_number)
        else:
            self.ADCDAQ = None
        self.FFT = fft.FFT(router=self, router_port='FFT', instance_number=instance_number)
        self.SCALER = scaler.SCALER(router=self, router_port='SCALER', instance_number=instance_number)
        if self.fpga.CAPTURE_TYPE == 'PROBER':
            self.PROBER = prober.PROBER(router=self, router_port='PROBER', instance_number=instance_number)
        else:
            self.PROBER = None
        self.FUNCGEN = funcgen.FUNCGEN(router=self, router_port='FUNCGEN', instance_number=instance_number)
        self.frame_length = self.fpga.FRAME_LENGTH

    def __repr__(self):
        """ Return a string that represents this object and its parent object.
        """
        return "%r.%s(%i)" % (self.fpga, self.__class__.__name__, self.chan_number)

    def init(self, fmc_present):

        self.fmc_present = fmc_present

        """ Initializes the channelizer modules"""
        # self.logger.debug('Initializing modules for channel #%i' % self.chan_number)
        # self.logger.debug('  - ADCDAQ')
        if self.ADCDAQ:
            self.ADCDAQ.init(fmc_present)
        # self.logger.debug('  - SRCSEL')
        # self.SRCSEL.init()
        # self.logger.debug('  - FFT')
        self.FFT.init()
        # self.logger.debug('  - SCALER')
        self.SCALER.init()
        # self.logger.debug('  - PROBER')
        if self.PROBER:
            self.PROBER.init()
        # self.logger.debug('  - FUNCGEN')
        self.FUNCGEN.init()
        # self.logger.debug('  - INJECT')
        # self.INJECT.init()

    def get_id(self):
        """ Return the channel ID as a (board, slot, channel) tuple.

        Returns:

            channel ID as a (board, slot, channel) tuple
        """

        return self.fpga.get_id(self.chan_number)

    def status(self):
        """ Displays the status of the channelizer modules"""
        pass

    def get_sim_output(self, analog_input):
        adcdaq_out = self.ADCDAQ.get_sim_output(analog_input)
        funcgen_out = self.FUNCGEN.get_sim_output(adcdaq_out)
        fft_out = self.FFT.get_sim_output(funcgen_out)
        scaler_out = self.SCALER.get_sim_output(fft_out)
        return scaler_out

class ChanArray(MMIRouter):
    """
    Instantiates a container for all channelizers available on the FPGA.
    It mimics the basin functionnalities of a 'dict'.

    """

    ROUTER_PORT_NUMBER_WIDTH = 4

    def __init__(self, *, router, router_port, verbose=0):
        self.verbose = verbose
        self.logger = logging.getLogger(__name__)
        super().__init__(router=router, router_port=router_port)
        # Create an instance of ADC_chip for each chip of the FMC board
        self.frame_length = self.fpga.FRAME_LENGTH
        self.chan = []
        for i in range(self.fpga.NUMBER_OF_CHANNELIZERS):
            self.chan.append(Chan(router=self, router_port=i, instance_number=i))

    def __repr__(self):
        """ Return a string that represents this object and its parent object.
        """
        return "%r.%s" % (self.fpga, self.__class__.__name__)

    def __getitem__(self, key):
        """If the user indexes this object (chan[n] instead of chan) then return the channelizer instance"""
        return self.chan[key]

    def __len__(self):
        """Returns the number of channelizers"""
        return len(self.chan)

    def __contains__(self, value):
        """
        """
        return value in self.keys()

    def __iter__(self):
        return iter(self.chan)

    def keys(self):
        """
        """
        return list(range(self.fpga.NUMBER_OF_CHANNELIZERS))

    def values(self):
        """
        """
        return self.chan

    def items(self):
        """
        """
        return list(zip(self.keys(), self.values()))

    # Low-level access functions

    # def read(self, chan_number, module_number, addr, *args, **kwargs):
    #     """ Reads from the register of a module of a specified channelizer"""
    #     fpga = self.fpga
    #     data = fpga.read(fpga.ANT_PORT[chan_number], module_number, addr, *args, **kwargs)
    #     return data

    # def write(self, chan_number, module_number, addr, data, *args, **kwargs):
    #     """ Writes to the register of a module of a specified channelizer"""
    #     fpga = self.fpga
    #     fpga.write(fpga.ANT_PORT[chan_number], module_number, addr, data, *args, **kwargs)

    def init(self, delay_table=None, fmc_present=None):
        """ Initializes all channelizer modules


        Clock selection
        ---------------
        We select the system clock or ADC clock as a channelizer clock source based on
        whether the ADC card that normally provides the clock is present or
        not.

        We also select the internal system clock in the presence of the ADC
        mezzanine if the sampling clock is exactly 1/4th of the processing
        clock for that firmware build. This is to prevent large current spikes
        that trip the FPGA's VCCINT DC-DC switcher whever the ADC is started
        or stopped.


        """

        if fmc_present[self.fpga.CHANNELIZERS_CLOCK_SOURCE]:
            self.logger.debug(f'{self!r}: Using the ADC to generate the channelizer clock')
            if self.fpga._sampling_frequency/4 == self.fpga._processing_frequency:
                self.fpga.GPIO.CHAN_CLK_SRC = 1 # *** JFC: uses the internal clock always. Works only for sampling at 800.000 MSPS
                self.logger.debug(f"{self!r}: Since the sampling frequency is {self.fpga._sampling_frequency} MHz, we'll use the internal {self.fpga._processing_frequency} MHz system clock to clock the channelizers instead of the ADC clock so that syncing the board won't cause large current changes that may upset the core switcher")
            else:
                self.fpga.GPIO.CHAN_CLK_SRC = 0 # uses the ADC clock to clock the channelizers
                self.logger.error(f"{self!r}: The channelizers is clocked by the ADC because we do not sample at exactly 800 MHz. The channelizer clock will be interrupted during syncing, which will cause cause large current changes that may upset the core switcher")
        else:
            self.logger.debug(f'{self!r}: Using the internal clock to generate the channelizer clock since the ADC is not available')
            self.fpga.GPIO.CHAN_CLK_SRC = 1 # uses the internal clock to clock the channelizer

        #self.logger.debug("%r: Initializing each channelizer", self.fpga)
        for i, chan in enumerate(self.chan):
            # self.logger.debug('%r: Initializing channelizer #%i %s' % (self.fpga, chan.chan_number, '' if fmc_present[i] else '(No ADC board)'))
            chan.init(fmc_present[i])
        #self.logger.debug("%r: Initializing delay tables", self.fpga)

        if self.fpga.HAS_ADCDAQ:
            if delay_table is not None:
                self.set_adc_delays(delay_table)

    def status(self):
        """ Displays the status of all channelizer modules"""
        for chan in self.chan:
            chan.status()

        #self.chan[1].ADCDAQ.set_divclk_phase(1) # Adjust phase of the DIVCLK signal to allow proper sampling of the deserialized words

    def set_adc_delays(self, adc_delay_table):
        """
        Sets the delays for all ADC data lines using the provided delay table.

        ``adc_delay_table`` is a dictionary where the key is a channel number and the corresponding values are the (tap_delays,
        sample_delay), where tap_delays is a list of 8 tap delay values for each of
        the ADC bits, and sample_offset is the number of samples acquisition is delayed after sync.

        If ``adc_delay_table`` is a list of (tap_delays, sample_delay), the delays are applied in order to channels 0,1,2 etc...
        """
        if not isinstance(adc_delay_table, dict):
            adc_delay_table = dict(enumerate(adc_delay_table))

        for ch, chan in enumerate(self.chan):
            if ch in adc_delay_table:
                tap_delays = adc_delay_table[ch]['tap_delays']
                sample_delay = adc_delay_table[ch]['sample_delay']
                clock_delay = adc_delay_table[ch]['clock_delay']
                if (tap_delays is not None and  any(bd is None or bd < 0 for bd in tap_delays)) or (sample_delay is not None and sample_delay < 0):
                    self.logger.warning("%r: Skipping channel set_delay on channel %i since Invalid bit or sample delay detected in delay table entry" % (self, ch))
                else:
                    chan.ADCDAQ.set_delays((tap_delays, sample_delay, clock_delay))

    def get_adc_delays(self):
        """
        Return the delays currently in use for all ADC data lines of all channels.

        Returns:

            dict containing the fields:

                'tap_delays': list of the 8 integer tap delays, one for each bit of the ADC sample
                'sample_delay': number of integer samples to delay
                'clock_delay': tap delay applied to the clock line
        """
        delay_table = {}
        for ch, chan in enumerate(self.chan):
            (tap_delays, sample_delay, clock_delay) = chan.ADCDAQ.get_delays()
            delay_table[ch] = {'tap_delays': tap_delays, 'sample_delay': sample_delay, 'clock_delay':clock_delay}
        return delay_table

    def set_data_width(self, width):
        """
        Set the number of bits used to represent the values computed by the channelizers.

        All channelizers are set to the new setting.

        Parameters:

            width (int): Target data width:

                - width=4: data is 4 bits Real + 4 bits Imaginary
                - width=8: data is 8 bits Real + 8 bits Imaginary
        """

        if width == 4:
            is_four_bits = 1
        elif width == 8:
            is_four_bits = 0
        else:
            raise ValueError('Number of bits %i is invalid for the channelizers. Only 4 or 8 is allowed' % width)

        # Set the channelizer data width
        for ch in self.chan:
            if not is_four_bits and not ch.SCALER.EIGHT_BIT_SUPPORT:
                raise ValueError('8-bit mode not supported in this build of the SCALER firmware')
            ch.SCALER.FOUR_BITS = is_four_bits

    def get_data_width(self):
        """
        Returns number of bits used to represent the values computed by the channelizers.
        If all the channelizer are not set in the same mode, an error is raised.
        """

        four_bits = set() # use a set to uniquely record all the possible encountered states

        for ch in self.chan:
            four_bits.add(ch.SCALER.FOUR_BITS)

        if four_bits == set([0]):
            return 8
        elif four_bits == set([1]):
            return 4
        else:
            raise RuntimeError("The channelizers are not set to the same data width.")


    def print_ramp_errors(self):
        try:
            while 1:
                for chan in self.chan:
                    print('CH%i: %3i' % (chan.chan_number, chan.ADCDAQ.RAMP_ERR_CTR), end=' ')
                print()
        except KeyboardInterrupt:
            pass