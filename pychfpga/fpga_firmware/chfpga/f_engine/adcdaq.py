#!/usr/bin/python
# Disable pylint Line too long (=C0301)
# pylint: disable=C0301

"""
ADCDAQ.py module
 Implements the interface to the ADC data acquisition module

History:
    2011-07-12 JFC: Created from test code in chFPGA.py
    2012-05-29 JFC: Extracted from ANT.py
    2012-09-25 JFC: Cleaned up for pylint.
"""

import time
import numpy as np
from ..mmi import MMI, BitField


class ADCDAQ(MMI):
    """ Implements interface to the ADC data acquisition logic"""

    ADDRESS_WIDTH = 19-3-4-3

    # Create local variables for page numbers to make the table more readable
    CONTROL = BitField.CONTROL
    STATUS = BitField.STATUS
    DRP = BitField.DRP

    # CONTROL BYTES
    DELAY0 =               BitField(CONTROL, 0, 0, width=5, doc='IODELAY value for the data line. Loaded the IODELAY_RST is pulsed.')
    DELAY1 =               BitField(CONTROL, 1, 0, width=5, doc='IODELAY value for the data line. Loaded the IODELAY_RST is pulsed.')
    DELAY2 =               BitField(CONTROL, 2, 0, width=5, doc='IODELAY value for the data line. Loaded the IODELAY_RST is pulsed.')
    DELAY3 =               BitField(CONTROL, 3, 0, width=5, doc='IODELAY value for the data line. Loaded the IODELAY_RST is pulsed.')
    DELAY4 =               BitField(CONTROL, 4, 0, width=5, doc='IODELAY value for the data line. Loaded the IODELAY_RST is pulsed.')
    DELAY5 =               BitField(CONTROL, 5, 0, width=5, doc='IODELAY value for the data line. Loaded the IODELAY_RST is pulsed.')
    DELAY6 =               BitField(CONTROL, 6, 0, width=5, doc='IODELAY value for the data line. Loaded the IODELAY_RST is pulsed.')
    DELAY7 =               BitField(CONTROL, 7, 0, width=5, doc='IODELAY value for the data line. Loaded the IODELAY_RST is pulsed.')
    CLK_DELAY =            BitField(CONTROL, 8, 0, width=5, doc='IODELAY value for the clock line. Loaded the CLK_IODELAY_RST is pulsed.')
    MMCM_RST =             BitField(CONTROL, 9, 7, doc='MCMM reset. Must be high when using DRP')
    PLL_CLK_SRC =          BitField(CONTROL, 9, 6, doc='Selects the data clock output source: 0=PLL output generated from the ADC clock, 1= 200 MHz system clock')
    CLK_IODELAY_RESET =    BitField(CONTROL, 9, 5, doc='Resets the IODELAY element in the clock path. This loads the delay values into the delay lines')
    ENABLE_RAMP =          BitField(CONTROL, 9, 4, doc='Enables transmission of a ramp. 0=inactive, 1=active')
    IDELAYCTRL_RESET =     BitField(CONTROL, 9, 3, doc='Resets the IDELAYCTRL. Forces it to recalibrate. ')
    BUFR_RESET =           BitField(CONTROL, 9, 2, doc='Resets the BUFR.')
    ISERDES_RESET =        BitField(CONTROL, 9, 1, doc='Resets the ISERDES.')
    IODELAY_RESET =        BitField(CONTROL, 9, 0, doc='Resets the IODELAY element. This loads the delay values into the delay lines')
    RAMP_ERR_CLEAR =       BitField(CONTROL, 10, 5, doc='Resets ramp error counter')
    CLEAR_FIFO_FLAGS =     BitField(CONTROL, 10, 4, doc='Resets sticky FIFO flags (Overflow, underflow etc)')
    SAMPLE_DELAY =         BitField(CONTROL, 11, 0, width=12, doc='Number of samples to skip before starting data acquisition after a SYNC event')
    CAPTURE2_PERIOD =      BitField(CONTROL, 12, 0, width=8, doc='Word capture period, from 0-255. 0 means 256 words')
    CAPTURE2_WORD_NUMBER = BitField(CONTROL, 13, 0, width=8, doc='Word number to be capured in CAPTURE_PATTERN. Must be <=CAPTURE2_PERIOD-1 for data to be captured')
    BYTE_MASK =            BitField(CONTROL, 14, 0, width=8, doc='"AND"s the ADC values')

    # STATUS BYTES
    DELAY0_STATUS         = BitField(STATUS, 0, 0, width=5, doc='Current delay value for the data line as read from the IODELAY.')
    CLK_DELAY_STATUS      = BitField(STATUS, 8, 0, width=5, doc='Current delay value of the CLK line IODELAY')
    CAPTURE_DONE          = BitField(STATUS, 9, 7, doc="'1' when capture is complete")
    PLL_LOCKED            = BitField(STATUS, 9, 3, doc='Indicates if the PLL is locked  (only if one is implemented in this module)')
    PLL_CLK_SRC_INT       = BitField(STATUS, 9, 2, doc='Indicates the source of the data processing clock output: 0=PLL output generated from the ADC clock, 1= 200 MHz system clock. This bit allows to determine of the use of the system clock was forced due to the loss of the PLL clock or reset of the ADCDAQ logic.')
    IDELAYCTRL_PRESENT    = BitField(STATUS, 9, 1, doc='Indicate whether this ADCDAQ instantiated a IODELAYCTRL')
    IDELAYCTRL_RDY        = BitField(STATUS, 9, 0, doc='Indicate if the IODELAYCTRL has finished calibrating')
    FIFO_OVERFLOW         = BitField(STATUS, 10, 7, doc="'1' if the FIFO is currently in overflow. ")
    FIFO_UNDERFLOW        = BitField(STATUS, 10, 6, doc="'1' if the FIFO is currently in underflow.  ")
    FIFO_EMPTY            = BitField(STATUS, 10, 5, doc="'1' if the FIFO is currently empty. ")
    FIFO_OVERFLOW_STICKY  = BitField(STATUS, 10, 4, doc="'1' if the FIFO has overflowed since last clear or reset. ")
    FIFO_UNDERFLOW_STICKY = BitField(STATUS, 10, 3, doc="'1' if the FIFO has underflowed since last clear or reset.  ")
    FIFO_EMPTY_STICKY     = BitField(STATUS, 10, 2, doc="'1' if the FIFO has been empty since last clear or reset. ")
    RAMP_ERR_CTR          = BitField(STATUS, 12, 0, width=16,doc='Counts how many words coming out of the ADC did not match the internal ramp generator')

    FIFO_WR_EN            = BitField(STATUS, 13, 0, doc="")
    FIFO_RD_EN            = BitField(STATUS, 13, 1, doc="")
    FIFO_DATA_VALID       = BitField(STATUS, 13, 2, doc="")
    DATA_READY            = BitField(STATUS, 13, 3, doc="")


    RAMP_CTR              = BitField(STATUS, 15, 0, width=8,doc='Free running word counter for readout interface, used to generate ramp at the ADCDAQ level')
    FIFO_WR_COUNT         = BitField(STATUS, 16, 0, width=8, doc='Number of words in the FIFO, as seen from the WR clock')
    FIFO_RD_COUNT         = BitField(STATUS, 17, 0, width=8, doc='Number of words in the FIFO, as seen from the RD clock (readout system)')
    ADC_CLK_SAMPLE        = BitField(STATUS, 18, 0, doc='Non-delayed 400 MHz ADC clock sampled by REFCLK')
    CAPTURE2_PATTERN0     = BitField(STATUS, 19, 0, width=8, doc='Captured byte')
    CAPTURE2_PATTERN1     = BitField(STATUS, 20, 0, width=8, doc='Captured byte')
    CAPTURE2_PATTERN2     = BitField(STATUS, 21, 0, width=8, doc='Captured byte')
    CAPTURE2_PATTERN3     = BitField(STATUS, 22, 0, width=8, doc='Captured byte')
    CAPTURE2_PATTERN      = BitField(STATUS, 22, 0, width=32, doc='Captured word ( 4 bytes). First sample is in the MSB.')
    BIT_ERR_CTR           = BitField(STATUS, 26, 0, width=32, doc='Word containing 8 4-bit counters that track ramp bit errors.')

    # DRP Ports
    MMCM_FB_LOW =       BitField(DRP, 0x14, 0, width=6, doc='MCMM Feedback clock Low time (in VCO cycles)')
    MMCM_FB_HIGH =      BitField(DRP, 0x14, 6, width=6, doc='MCMM Feedback clock High time (in VCO cycles)')
    MMCM_FB_PHASE =     BitField(DRP, 0x14, 13, width=3, doc='MCMM Feedback clock phase in increments of 1/8 the VCO period')
    MMCM_CLKIN_LOW =    BitField(DRP, 0x16, 0, width=6, doc='MCMM Input clock divider Low time (in input clock cycles)')
    MMCM_CLKIN_HIGH =   BitField(DRP, 0x16, 6, width=6, doc='MCMM input clock divider High time (in input clock cycles)')
    MMCM_CLKIN_BYPASS = BitField(DRP, 0x16, 12, doc='MCMM input clock divider bypass')
    MMCM_DIVCLK_LOW =   BitField(DRP, 0x0A, 0, width=6, doc='MCMM DIVCLK clock Low time (in VCO cycles)')
    MMCM_DIVCLK_HIGH  = BitField(DRP, 0x0A, 6, width=6, doc='MCMM DIVCLK clock High time (in VCO cycles)')
    MMCM_DIVCLK_PHASE = BitField(DRP, 0x0A, 13, width=3, doc='MCMM DIVCLK clock phase in increments of 1/8 the VCO period')
    MMCM_DIVCLK_DELAY = BitField(DRP, 0x0B, 0, width=6, doc='MCMM DIVCLK clock delay in increments of the VCO period')
    MMCM_ADCCLK_LOW =   BitField(DRP, 0x0C, 0, width=6, doc='MCMM DIVCLK clock Low time (in VCO cycles)')
    MMCM_ADCCLK_HIGH =  BitField(DRP, 0x0C, 6, width=6, doc='MCMM DIVCLK clock High time (in VCO cycles)')
    MMCM_ADCCLK_PHASE = BitField(DRP, 0x0C, 13, width=3, doc='MCMM DIVCLK clock phase in increments of 1/8 the VCO period')
    MMCM_ADCCLK_DELAY = BitField(DRP, 0x0D, 0, width=6, doc='MCMM DIVCLK clock delay in increments of the VCO period')
    MMCM_POWER =        BitField(DRP, 0x28, 0, width=16, doc='MCMM Power bits. Must be set to 0xFFFF in order to successfully program the other MMCM registers')


    def __init__(self, *, router, router_port, instance_number):
        super().__init__(router=router, router_port=router_port, instance_number=instance_number)
        self._lock() # Prevent accidental addition of attributes (if, for example, a value is assigned to a wrongly-spelled property)

    def set_ADCDAQ_mode(self, mode):
        if mode =='data':
            self.ENABLE_RAMP = 0
        elif mode == 'ramp':
            self.ENABLE_RAMP = 1
        else:
            raise Exception('Invalid ADCDAQ mode')

    def set_delays(self, dly=([0] * 8, None, None)):
        """
        Sets the data acquisition delays
        WARNING: will work only if DIVCLK is clocking (i.e. ADC not in SYNC, and BUFR/PLL not in RESET)
        WARNING:  The sample delays will be valid only after the next SYNC event.
        """

        tap_delays, sample_delay, clock_delay = dly

        if tap_delays is not None:
            if isinstance(tap_delays, int):
                tap_delays = [tap_delays] * 8
            if len(tap_delays) > 9:
                raise Exception('Tap Delay vector too long')
            self.write(self.get_addr('DELAY0'), tap_delays) # Set delay in registers
            self.pulse_bit('IODELAY_RESET')

        if sample_delay is not None:
            if 31 < sample_delay < 0:
                raise ValueError('Invalid sample delay')
            self.SAMPLE_DELAY = sample_delay

        if clock_delay is not None:
            if 31 < sample_delay < 0:
                raise ValueError('Invalid clock delay')
            self.set_clk_delay(clock_delay)


    def get_delays(self):
        """ Return the curent 8 tap delays, sample delay and clock delay"""
        tap_delays =  list(self.read(self.get_addr('DELAY0'), length=8, type=np.uint8)) # Reads the delay in registers
        sample_delay =  self.SAMPLE_DELAY # Reads the sample delays
        clock_delay =  self.CLK_DELAY # Reads the sample delays

        return [tap_delays, sample_delay, clock_delay]


    def set_clk_delay(self, dly):
        """ Sets the tap delay on the clock line
        WARNING: will work only if DIVCLK is clocking (i.e. ADC not in SYNC, and BUFR/PLL not in RESET)
        """
        if not isinstance(dly, int):
            raise Exception('Delay on the clock line must be a scalar')

        self.write(self.get_addr('CLK_DELAY'), dly) # Set delay in registers
        self.pulse_bit('CLK_IODELAY_RESET')



    def get_actual_delay(self):
        """ Reads the 8 actual delay tap values (returned by the IODELAY themselves, not the last delay set point) and return them as an array"""
        return self.read(self.get_addr('DELAY0_STATUS'), length=8, type=np.uint8) # Reads the delay in registers

    def set_divclk_phase(self, phase):
        """
        Sets DIVCLK phase on MCMM in inrements of 1/8 VCO cycles. Valid range is 0-512.
        """
        self.MMCM_RST = 1
        self.MMCM_POWER = 0xFFFF
        self.MMCM_DIVCLK_PHASE = phase & 0x07
        self.MMCM_DIVCLK_DELAY = phase >> 3
        self.MMCM_RST = 0

    def set_adcclk_phase(self, phase):
        """
        Sets ADC_CLK phase on MCMM in inrements of 1/8 VCO cycles. Valid range is 0-512.
        """
        self.MMCM_RST = 1
        self.MMCM_POWER = 0xFFFF
        self.MMCM_ADCCLK_PHASE = phase & 0x07
        self.MMCM_ADCCLK_DELAY = phase >> 3
        self.MMCM_RST = 0

    delay = property(set_delays, get_delays)


    def capture_pattern(self, period=11, number_of_samples=None):
        """
        Capture ``number_of_samples`` bytes from the ADC with a periodicity of ``period``.
        If ``number_of_samples`` is None, ``period`` samples are captured.

        For this method to work, the following must be done prior to the call:
            - ADC data bit delays must  have been set
            - SAMPLE_DELAY and CAPTURE2_PERIOD must have been set and a ``sync`` must have been issued to make sure the waveform will follow these parameters.
        """
        if number_of_samples is None:
            number_of_samples = period
        if period != self.CAPTURE2_PERIOD:
            raise RuntimeError('capture_pattern: the waveform period set in ADCDAQ does not match the expected period')
        number_of_words = (number_of_samples + 3) // 4 # round up
        pattern = np.zeros(number_of_words, np.dtype('>u4')) # Important: The MSB bust be stores first in memory for when we convert to a byte array.
        for i in range(number_of_words):
            #print '  Acquiring pattern for delay %i' % (dly)
            #dly=0
            self.CAPTURE2_WORD_NUMBER = i
            time.sleep(1 / 200e6 * period * 2) # make sure the data has time to be captured
            pattern[i] = self.CAPTURE2_PATTERN
        return pattern.view(np.uint8)[:number_of_samples] # trim extra bytes in case we didn't specify a multiple of 4.

    def init(self, fmc_present):
        """
        Initializes the ADCDAQ module
        """

        # Enable ramp generation if the ADC board is absent.
        # That will ensure that data will come out of the ADCDAQ module even if there is no ADC clock signal.
        # If only one channelizer is not producing output, the crossbar aligner will wait forever to align all the frames.
        self.ENABLE_RAMP = not fmc_present


    def status(self):
        """
        Prints the ADCDAQ module status.
        """
        print('-------------- CHAN[%i].ADCDAQ STATUS --------------' % self.instance_number)

        print('Clock source: %s' % ('ADC','SYSTEM CLOCK')[self.PLL_CLK_SRC])
        print('Data Acquisition FIFO status')
        print('   Flag        Current   Sticky')
        print('   ----------- -------   ------')
        print('   OVERFLOW    %5s     %5s' % (bool(self.FIFO_OVERFLOW), bool(self.FIFO_OVERFLOW_STICKY)))
        print('   UNDERFLOW   %5s     %5s' % (bool(self.FIFO_UNDERFLOW), bool(self.FIFO_UNDERFLOW_STICKY)))
        print('   EMPTY       %5s     %5s' % (bool(self.FIFO_EMPTY), bool(self.FIFO_EMPTY_STICKY)))
        print('Number of ramp errors: %i', self.RAMP_ERR_CTR)

        # fin=200
        # input_div=1 if self.MMCM_CLKIN_BYPASS else (self.MMCM_CLKIN_HIGH + self.MMCM_CLKIN_LOW)
        # divclk_div=self.MMCM_DIVCLK_HIGH + self.MMCM_DIVCLK_LOW
        # fb_div=self.MMCM_FB_HIGH + self.MMCM_FB_LOW


        # print 'MMCM'
        # print '  Input clock divider: HIGH:%i, LOW: %i, TOTAL: %i, BYPASS:%i' % (self.MMCM_CLKIN_HIGH, self.MMCM_CLKIN_LOW, self.MMCM_CLKIN_HIGH + self.MMCM_CLKIN_LOW,self.MMCM_CLKIN_BYPASS)
        # print '  FB divider           HIGH:%i, LOW: %i, TOTAL: %i, PHASE: %i' % (self.MMCM_FB_HIGH, self.MMCM_FB_LOW, self.MMCM_FB_HIGH + self.MMCM_FB_LOW, self.MMCM_FB_PHASE)
        # print '  DIVCLK divider       HIGH:%i, LOW: %i, TOTAL: %i, PHASE: %i, Delay: %i' % (self.MMCM_DIVCLK_HIGH, self.MMCM_DIVCLK_LOW, self.MMCM_DIVCLK_HIGH + self.MMCM_DIVCLK_LOW, self.MMCM_DIVCLK_PHASE, self.MMCM_DIVCLK_DELAY)
        # print '  ADCCLK divider       HIGH:%i, LOW: %i, TOTAL: %i, PHASE: %i, Delay: %i' % (self.MMCM_ADCCLK_HIGH, self.MMCM_ADCCLK_LOW, self.MMCM_ADCCLK_HIGH + self.MMCM_ADCCLK_LOW, self.MMCM_ADCCLK_PHASE, self.MMCM_ADCCLK_DELAY)
        # print '  Assmued input frequency: %.0f MHz'% fin
        # print '  Computed PFD frequency: %.0f MHz'% (fin*1.0/input_div)
        # print '  Computed VCO frequency: %.0f MHz'% (fin*1.0/input_div*fb_div)
        # print '  Computed DIVCLK frequency: %.0f MHz' % (fin*1.0/input_div*fb_div/divclk_div)

        print()

    def get_sim_output(self, analog_input=None, number_of_frames=None):
        frame_length = self.fpga.FRAME_LENGTH

        if self.ENABLE_RAMP:
            if number_of_frames is None:
                number_of_frames = 4
            data_bytes = np.repeat([np.arange(frame_length, dtype=np.int8)], number_of_frames, axis=0)
        else:
            if number_of_frames is None:
                number_of_frames = -1  # Means whatever number of frames fits in the reshaped vector
            data_bytes = np.reshape(np.array(analog_input, dtype=np.int8), (number_of_frames, frame_length))

        flags = (data_bytes == -128) | (data_bytes == 127)
        word_flags = np.sum(np.reshape(flags, (-1, frame_length / 4, 4)) * [1, 2, 4, 8], axis=-1, dtype=np.uint8)
        data_words = data_bytes.view('>u4')
        return (word_flags, data_words)
