#!/usr/bin/python
# Disable pylint Line too long (=C0301)
# pylint: disable=C0301

"""
ADC_PLL.py module
 Implements the ADC PLL interface

History:
    2011-07-08 JFC : Created from test code in chFPGA.py
    2011-08-30 KB : Changed default reference to 10 MHz
    2011-09-25 JFC: Made fdiv computation work for any frequency
    2011-10-10 JFC: Change FB_select to '0' to make sure the output divider was in the feedback loop and eliminate a phase ambiguity.
        Modify computation of int_div to handle both FB_select==0 and FB_select==1
        Added check on int_div range
        Changed default phase to 2000 to allow reliable SYNC
    2011-05-28 JFC: Fixed VCO frequency display to take into account input divider
    2011-05-29 JFC: Changed default frequency to 1700 MHz to match current CHIME value.
"""

import numpy as np
import logging
import time

class ADC_PLL_base(object):

    def __init__(self, adc_board, verbose=0):
        self.adc_board = adc_board
        self.verbose = verbose
        self.logger = logging.getLogger(__name__)

    def write(self, data):
        """ Writes a 32-bit word to PLL (MSB first). The register address is contained in the word."""
        brd = self.adc_board
        brd.spi_read_write(brd.SPI_PLL1_ADDR, data)

    def init(self, fout=1600, fref=10, verbose=None, **args):
        """
        Initializes the ADC PLL (Analog Devices ADF4350) to provide an adequate clock to the ADC.
            fout: ADC reference frequency in MHz. Sampling rate is fout/2.
            fref: PLL reference frequency in MHZ (typically 10 or 25 MHz)

        NOTES:
            - The reference clock x2 doubler or /2 divider are never enabled
        """

        if verbose is None:
            verbose = self.verbose
        else:
            verbose |= self.verbose

        # fref=25 # MHz - PLL reference frequency (fixed)
        # fout=1600 # MHz - ADC Reference Frequency. Sampling rate is fout/2
        # fdiv=2 if fout<2200 else 1 # Output division factor
        fmin = 2200  # MHz. Minimum VCO frequency
        fmax = 4400  # MHz. Minimum VCO frequency

        fdiv = int(2**np.ceil(np.log2(float(fmin)/fout))) #110925 JFC - Compute any factor for the output divider fdiv.
        if fdiv > 16:
            raise Exception('Output frequency is too low')

        if verbose:
            self.logger.debug('%.32r: --------------------- ADC PLL ------------------------------------' % self.adc_board)
            self.logger.debug('%.32r:  PLL Reference frequency         %7.3f MHz' % (self.adc_board, fref))
            self.logger.debug('%.32r:  Target ADC reference frequency: %7.3f MHz' % (self.adc_board, fout))

        # REGISTER 5
        LD_pin_mode = 1  # 0=LOW, 1=Lock Detect, 2=Low, 3= High

        # REGISTER 4
        FB_select = 0  # 0=feedback from output divided (needed to ensure absolute phase reproducibility), 1=feedback from VCO directly
        RF_div = int(np.log2(fdiv))  # Output divider: 0=/1, 1=/2, 2=/4, 3=/8, 4=/16
        band_sel_div = int(fref*8) #1-255. R counter output / band_sel_div < 125 kHz.
        vco_power_down = 0  # 0-1
        mute_until_lock_detect = 0  # 0-1
        AUX_sel = 0  #  0=use output divider output, 1=use VCO output directly,
        AUX_enable = 1  #0-1
        AUX_power = 2  # 0=-4 dBm, 1=-1 dBm, 2=2 dBm, 3=5 dBm
        RF_enable = 1        #0-1
        RF_power = 2  # 0=-4 dBm, 1=-1 dBm, 2=2 dBm, 3=5 dBm

        # REGISTER 3
        cycle_slip_reduction = 0  # 0-1. Needs 50% duty cycle and lowest CP current
        clock_div_mode = 2  # 0=clock divider off, 1=fast lock, 2=resync enable, 3=reserved #111018 JFC: set to 2 to allow phase control
        clock_div = 2  # 0-4095


        # REGISTER 2
        noise_mode = 0  # 0=low noise, 1-2: reserved, 3=low spur
        muxout = 0  # !using 4 interferes with the locking process! 0=Hi-Z, 1=Vdd, 2=GND, 3=R Divider out, 4= N divider out, 5=Analog lock detect, 6= Digital lock detect, 7=reserved
        ref_doubler = 0  # Reference clock doubler: 0=disabled, 1=enabled
        rdiv2 = 1  # Reference clock divide-by-2: 0=disabled, 1=enabled
        R_counter = 1  # Reference clock divider: 1-1023
        double_buf = 0  # 0=disabled, 1=enabled
        CP_current = 0  # 0-15
        LDF = 1  # 0 = Lock Detect Fractional: frac-N, 1=INT-N #111018 JFC: set to 1 to enable phase shift
        LDP = 1  # 0=10 ns, 1 = 6ns
        PD_polarity = 1  # 0=negative, 1=positive
        power_down = 0  # 0=disabled, 1=enabled
        CP_three_state = 0  # 0=disabled, 1=enabled
        counter_reset = 0  # 0=disabled, 1=enabled

        # REGISTER 1
        prescaler = 0  # 0=4/5. 1=8/9
        phase = 0  # 0-modulus # 111011 JFC: Changed to make the SYNC stable
        modulus = 200  # 0-4095

        # REGISTER 0
        if FB_select:  # if feedback is from VCO directly
            int_div = int(fdiv*fout/fref/2*(rdiv2+1))  #23-65535
        else: # if feedback is from the output of the output divider
            int_div = int(fout/fref*(rdiv2+1))  #23-65535
        fractional_mode = True

        int_div -= 1 * fractional_mode
        frac_div = modulus*fractional_mode  #0-4095 Non-zero for frationnal mode
        fvco = (int_div+float(frac_div)/modulus)*fref/(rdiv2+1) if FB_select else (int_div+float(frac_div)/modulus)*fref/(rdiv2+1)*fdiv

        if int_div < 23 or int_div > 65535:
            raise Exception('Integer division factor is out of range (it_div=%i, range is 23-65535)' % int_div)

        if verbose:
            self.logger.debug('%.32r:  Reference divide-by-2 enabled: %s' % (self.adc_board, bool(rdiv2)))
            self.logger.debug('%.32r:  PFB frequency: %.0f MHz' % (self.adc_board, fref/(1+rdiv2)))
            self.logger.debug('%.32r:  Integer multiplication factor: %i' % (self.adc_board, int_div))
            self.logger.debug('%.32r:  Fractional multiplication factor/modulus: %i/%i' % (self.adc_board, frac_div, modulus))
            self.logger.debug('%.32r:  Total multiplication factor: %i' % (self.adc_board, int_div + float(frac_div) / modulus))
            self.logger.debug('%.32r:  Feedback includes output dividor: %s' %  (self.adc_board, not FB_select))
            self.logger.debug('%.32r:  VCO Frequency: %.3f MHz (%.0f MHz min, %.0f MHz max)' % (self.adc_board, fvco, fmin, fmax))
            self.logger.debug('%.32r:  Output division factor: %i' % (self.adc_board, fdiv))
            self.logger.debug('%.32r:  Programmed output frequency: %.3f' % (self.adc_board, float(fvco) / fdiv))

        # Override variable names if any is specified in the function call
        for (varname, value) in args.items():
            if varname in locals():
                if verbose:
                    self.logger.debug('%.32r: Setting %s = %i' % (self.adc_board, varname, value))
                exec('%s=%i' % (varname, value))
            else:
                self.logger.debug('%.32r: "%s" is not a PLL variable' % (self.adc_board, varname))

        PLL_reg5 = np.uint32((LD_pin_mode << 22) + (0x3 << 19) + 5)
        PLL_reg4 = np.uint32((FB_select << 23) + (RF_div << 20) + (band_sel_div << 12) + (vco_power_down << 11) + (mute_until_lock_detect << 10) + (AUX_sel << 9) + (AUX_enable << 8) + (AUX_power << 6) + (RF_enable << 5) + (RF_power << 3)+4)
        PLL_reg3 = np.uint32((cycle_slip_reduction << 18) + (clock_div_mode << 15) + (clock_div << 3)+3)
        PLL_reg2 = np.uint32((noise_mode << 29) + (muxout << 26) + (ref_doubler << 25) + (rdiv2 << 24) + (R_counter << 14) + (double_buf << 13) + (CP_current << 9) + (LDF << 8) + (LDP << 7) + (PD_polarity << 6) + (power_down << 5) + (CP_three_state << 4) + (counter_reset << 3)+2)
#        PLL_reg2_tristate=(noise_mode << 29) + (muxout << 26) + (ref_doubler << 25) + (rdiv2 << 24) + (R_counter << 14) + (double_buf << 13) + (CP_current << 9) + (LDF << 8) + (LDP << 7) + (PD_polarity << 6) + (power_down << 5) + (1 << 4) + (counter_reset << 3)+2
        PLL_reg1 = np.uint32((prescaler << 27) + (phase << 15) + (modulus << 3)+1)
        PLL_reg0 = np.uint32((int_div << 15) + (frac_div << 3)+0)

        trial = 0
        while True:
            self.write(np.uint32(PLL_reg5))  # write Reg 5:
            self.write(np.uint32(PLL_reg4))  # write Reg 4:
            self.write(np.uint32(PLL_reg3))  # write Reg 3:
            self.write(np.uint32(PLL_reg2))  # write Reg 2:
            self.write(np.uint32(PLL_reg1))  # write Reg 1:
            self.write(np.uint32(PLL_reg0))  # write Reg 0:
            self.write(np.uint32(PLL_reg0))  # write Reg 0: # To make sure DBR values are clocked in.

            time.sleep(0.050)
            if self.is_locked():
                self.logger.debug('%.32r: ADC PLL is locked in trial #%i' % (self.adc_board, trial + 1))
                break
            elif trial > 5:
                raise RuntimeError('ADC PLL cannot be locked')
            self.logger.warning('%.32s: ADC PLL did not lock, retrying...' % self.adc_board)
            trial += 1
            # self.adc_board.IOExpander.wait_for_bit('PLL1_LOCK', timeout=1)
        # if verbose:

        return (PLL_reg0, PLL_reg1, PLL_reg2, PLL_reg3, PLL_reg4, PLL_reg5)

    def is_locked(self):
        return self.adc_board.IOExpander.PLL1_LOCK

    def status(self):
        self.logger.info('%.32r: --- ADC PLL' % self.adc_board)
        if not self.adc_board.is_mezzanine_present():
            self.logger.info('%.32r: FMC board not present' % self.adc_board)
        self.logger.info('%.32r:  No status info' % self.adc_board)

