#!/usr/bin/python
# Disable pylint Line too long (=C0301)
# pylint: disable=C0301

"""
ANT.py module
    Implements interface to the channelizer modules

History:
    2011-07-12 : JFC : Created from test code in chFPGA.py
    2012-08-31 JFC: Swapped addresses of PROBER and SCALER to match the same change in firmware
    2012-09-25 JFC: Renamed to FRAMER and FR_DIST to SRCSEL
"""
import logging

import ADCDAQ
# import SRCSEL
import FFT
import SCALER
import PROBER
import FUNCGEN
from numpy import NaN as npNaN
# import INJECT

class ANT_channel(object):
    """ Implements the interface to one of the channelizer"""

    # Channelizer module addresses
    ADCDAQ_OFFSET_ADDR  = 0
    # SRCSEL_OFFSET_ADDR  = 1
    FFT_OFFSET_ADDR     = 2
    SCALER_OFFSET_ADDR  = 3
    PROBER_OFFSET_ADDR  = 4
    FUNCGEN_OFFSET_ADDR = 5
    # INJECT_OFFSET_ADDR  = 6

    def __init__(self, fpga_instance, base_address, submodule_address_increment, instance_number):
        self.ant_number = instance_number  # store current channelizer number for this instance
        self.fpga = fpga_instance
        self.logger = logging.getLogger(__name__)

        self.ADCDAQ  = ADCDAQ.ADCDAQ_base(fpga_instance,   base_address + self.ADCDAQ_OFFSET_ADDR*submodule_address_increment,  instance_number)
        self.FFT     = FFT.FFT_base(fpga_instance,         base_address + self.FFT_OFFSET_ADDR*submodule_address_increment,     instance_number)
        self.SCALER  = SCALER.SCALER_base(fpga_instance,   base_address + self.SCALER_OFFSET_ADDR*submodule_address_increment,  instance_number)
        self.PROBER  = PROBER.PROBER_base(fpga_instance,   base_address + self.PROBER_OFFSET_ADDR*submodule_address_increment,  instance_number)
        self.FUNCGEN = FUNCGEN.FUNCGEN_base(fpga_instance, base_address + self.FUNCGEN_OFFSET_ADDR*submodule_address_increment, instance_number)
        self.frame_length = self.fpga.FRAME_LENGTH


    def init(self, fmc_present):

        self.fmc_present = fmc_present

        """ Initializes the channelizer modules"""
        # self.logger.debug('Initializing modules for channel #%i' % self.ant_number)
        # self.logger.debug('  - ADCDAQ')
        self.ADCDAQ.init(fmc_present)
        # self.logger.debug('  - SRCSEL')
        # self.SRCSEL.init()
        # self.logger.debug('  - FFT')
        self.FFT.init()
        # self.logger.debug('  - SCALER')
        self.SCALER.init()
        # self.logger.debug('  - PROBER')
        self.PROBER.init()
        # self.logger.debug('  - FUNCGEN')
        self.FUNCGEN.init()
        # self.logger.debug('  - INJECT')
        # self.INJECT.init()


    def status(self):
        """ Displays the status of the channelizer modules"""
        pass

    def get_sim_output(self, analog_input):
        adcdaq_out = self.ADCDAQ.get_sim_output(analog_input)
        funcgen_out = self.FUNCGEN.get_sim_output(adcdaq_out)
        fft_out = self.FFT.get_sim_output(funcgen_out)
        scaler_out = self.SCALER.get_sim_output(fft_out)
        return scaler_out

class ANT_base(object):
    """
    Instantiates a container for all channelizers available on the FPGA.
    It mimics the basin functionnalities of a 'dict'.

    """

    def __init__(self, fpga, base_address, address_increment, submodule_address_increment, verbose=0):
        self.fpga = fpga
        self.verbose = verbose
        self.logger = logging.getLogger(__name__)
        # Create an instance of ADC_chip for each chip of the FMC board
        self.frame_length = fpga.FRAME_LENGTH
        self.ANT = []
        for i in range(fpga.NUMBER_OF_ANTENNAS):
            self.ANT.append(ANT_channel(self.fpga, base_address + i * address_increment, submodule_address_increment, i))

    def __getitem__(self, key):
        """If the user indexes this object (ANT[n] instead of ANT) then return the channelizer instance"""
        return self.ANT[key]

    def __len__(self):
        """Returns the number of channelizers"""
        return len(self.ANT)

    def __contains__(self, value):
        """
        """
        return value in self.keys()

    def __iter__(self):
        return iter(self.ANT)

    def keys(self):
        """
        """
        return range(self.fpga.NUMBER_OF_ANTENNAS)

    def values(self):
        """
        """
        return self.ANT

    def items(self):
        """
        """
        return zip(self.keys(), self.values())

    # Low-level access functions

    # def read(self, ant_number, module_number, addr, *args, **kwargs):
    #     """ Reads from the register of a module of a specified channelizer"""
    #     fpga = self.fpga
    #     data = fpga.read(fpga.ANT_PORT[ant_number], module_number, addr, *args, **kwargs)
    #     return data

    # def write(self, ant_number, module_number, addr, data, *args, **kwargs):
    #     """ Writes to the register of a module of a specified channelizer"""
    #     fpga = self.fpga
    #     fpga.write(fpga.ANT_PORT[ant_number], module_number, addr, data, *args, **kwargs)

    def init(self, delay_table=None, fmc_present=None):
        """ Initializes all channelizer modules"""

        # Selects which clock is used to clock the channelizes based on whether the ADC card that normally provides the clock is present or not.
        if fmc_present[self.fpga.CHANNELIZERS_CLOCK_SOURCE]:
            self.logger.debug('%.32r: Using the ADC to generate the channelizer clock' % self.fpga)
            if self.fpga._sampling_frequency == 800.0e6:
                self.fpga.GPIO.CHAN_CLK_SRC = 1 # *** JFC: uses the internal clock always. Works only for sampling at 800.000 MSPS
                self.logger.debug("%.32r: Since the sampling frequency is exactly 800.000000 MHz, we'll use the internal 200 MHz clock to clock the channelizers instead of the ADC clock so that syncing the board won't cause large current changes that may upset the core switcher", self.fpga)
            else:
                self.fpga.GPIO.CHAN_CLK_SRC = 0 # uses the ADC clock to clock the channelizers
                self.logger.error("%.32r: The channelizers is clocked by the ADC because we do not sample at exactly 800 MHz. The channelizer clock will be interrupted during syncing, which will cause cause large current changes that may upset the core switcher", self.fpga)
        else:
            self.logger.debug('%.32r: Using the internal clock to generate the channelizer clock since the ADC is not available' % self.fpga)
            self.fpga.GPIO.CHAN_CLK_SRC = 1 # uses the internal 200 MHz clock to clock the channelizer

        #self.logger.debug("%.32r: Initializing each channelizer", self.fpga)
        for (i, ant) in enumerate(self.ANT):
            # self.logger.debug('%.32r: Initializing channelizer #%i %s' % (self.fpga, ant.ant_number, '' if fmc_present[i] else '(No ADC board)'))
            ant.init(fmc_present[i])
        #self.logger.debug("%.32r: Initializing delay tables", self.fpga)
        
        if delay_table is not None:
            self.set_adc_delays(delay_table)

    def status(self):
        """ Displays the status of all channelizer modules"""
        for ant in self.ANT:
            ant.status()

        #self.ANT[1].ADCDAQ.set_divclk_phase(1) # Adjust phase of the DIVCLK signal to allow proper sampling of the deserialized words

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

        for ch, ant in enumerate(self.ANT):
            if ch in adc_delay_table:
                tap_delays = adc_delay_table[ch]['tap_delays']
                sample_delay = adc_delay_table[ch]['sample_delay']
                clock_delay = adc_delay_table[ch]['clock_delay']
                if (tap_delays is not None and  any(bd is None or bd < 0 for bd in tap_delays)) or (sample_delay is not None and sample_delay < 0):
                    self.logger.warning("Skipping channel set_delay on channel %i since Invalid bit or sample delay detected in delay table entry" % ch)
                else:
                    ant.ADCDAQ.set_delays((tap_delays, sample_delay, clock_delay))

    def get_adc_delays(self):
        """
        Return the delays currently in use for all ADC data lines.
        """
        delay_table = {}
        for ch, ant in enumerate(self.ANT):
            (tap_delays, sample_delay, clock_delay) = ant.ADCDAQ.get_delays()
            delay_table[ch] = {'tap_delays': tap_delays, 'sample_delay': sample_delay, 'clock_delay':clock_delay}
        return delay_table

    def set_data_width(self, width):
        """
        Set the number of bits used to represent the values computed by the channelizers.
        All channelizers are set to the new setting.
            width=4: data is 4 bits Real + 4 bits Imaginary
            width=8: data is 8 bits Real + 8 bits Imaginary
        """

        if width==4:
            is_four_bits = 1
        elif width == 8:
            is_four_bits = 0
        else:
            raise ValueError('Number of bits %i is invalid for the channelizers. Only 4 or 8 is allowed' % width)

        # Set the channelizer data width
        for ch in self.ANT:
            if not is_four_bits and not ch.SCALER.EIGHT_BIT_SUPPORT:
                raise ValueError('8-bit mode not supported in thie build of the SCALER firmware')
            ch.SCALER.FOUR_BITS = is_four_bits

    def get_data_width(self):
        """
        Returns number of bits used to represent the values computed by the channelizers.
        If all the channelizer are not set in the same mode, an error is raised.
        """

        four_bits = set() # use a set to uniquely record all the possible encountered states

        for ch in self.ANT:
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
                for ant in self.ANT:
                    print 'CH%i: %3i' % (ant.ant_number, ant.ADCDAQ.RAMP_ERR_CTR),
                print
        except KeyboardInterrupt:
            pass

