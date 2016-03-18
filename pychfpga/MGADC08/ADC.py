#!/usr/bin/python
# Disable pylint TAB warnings
# pylint: disable=W0312
# Disable pylint Line too long Convention
# pylint: disable=C0301

"""
ADC.py module
 Implements interface to ADC on the ADC FMC
#
# History:
# 2011-07-07 : JFC : Created from test code in chFPGA.py
    2011-09-29 JFC: Added set_test_mode()
    2012-06-07 JFC: Added ADC_chip.get_temperature()
"""

import numpy as np
import logging

class ADC_chip(object):
    """
    Provides the parameters and methods related to one ADC chip.
    """
    # ADC-related constants
    REG_CHIP_ID = 0x00
    REG_CONTROL = 0x01
    REG_STATUS = 0x02
    REG_SWRESET = 0x04
    REG_TEST = 0x05
    REG_SYNC = 0x06
    REG_CHANNEL_SELECT = 0x0F
    REG_CH_CAL_CTRL = 0x10
    REG_CH_CAL_CTRL_MLBX = 0x11
    REG_CH_STATUS = 0x12
    REG_TRIM = 0x13  ## added kmb to set to 100ohms

    def __init__(self, adc_instance, adc_number):
        #super(ADC_chip,self).__init__(fpga)
        self.adc = adc_instance # store reference to the parent instance (ADC_Base)
        self.adc_number = adc_number # store current ADC number for this instance
        self.logger = logging.getLogger(__name__)

    def read(self, addr):
        """ Reads a register of the ADC chip """
        return self.adc.read(self.adc_number, addr)

    def write(self, addr, value):
        """ Writes a register of the ADC chip """
        return self.adc.write(self.adc_number, addr, value)

    def init(self, adc_mode=0, standby_mode=0, dmux=False, gray_code=False, bandwidth=2, full_scale=0, test_mode=0, sync_delay=0x08):
        """
        Writes the control and test register of the ADC
            adc_mode (0-15, default=0=4 channel mode): selects between 1,2 and 4 channel mode and which analog input is used
            standby_mode (0-3, default=0 full active): selects the active mode. 0=Full active, 3=full standby
            dmux (bool, default=false): selects whether the 2:1 DMUX mode is selected
            gray_code (bool, default=False): selects if output is in binary (false) or gray code (True)
            bandwidth (0-3, default=2), selects the analog bandwiddth of the ADC: 0= 500 MHz, 1=600 MHz, 2=1.5 GHz, 3=2 GHz
            full_scale (0-1, default=0), selects the full scale peak-to-peak voltage: 0=500 mV full scale, 1=625 mV full scale
            test_mode (0-2, default=0): test mode of the ADC: 0= Normal operation, 1=ramp, 2= 00/FF pulse
            sync_delay (0-15, default=8): Number of clocks to hold off the data clock after a SYNC event
        """


        ADC_MODE = adc_mode # 0-15, 0=4-channel mode
        STDBY = standby_mode # 0-3, 0=Full active, 3=Full standby
        DMUX_RATIO = not dmux # 0=DMUX2:1, 1=DMUX1:1
        BG = gray_code # 0=Binary, 1=Gray code
        BDW = bandwidth # 0-3, 0= 500 MHz, 1=600 MHz, 2=1.5 GHz, 3=2 GHz
        FS = full_scale # 0=500 mV full scale, 1=625 mV full scale
        TEST = bool(test_mode) # 0=No test mode, 1=test mode activated

        REG_CONTROL_Value = np.uint32((TEST<<12)+(FS<<10)+(BDW<<8)+(BG<<7)+(DMUX_RATIO<<6)+(STDBY<<4)+ADC_MODE)
        REG_TEST_Value = (0, 0, 1)[test_mode] # Select test pattern: test=0: no test mode, test=1:ramp, test=2: flashing 0xff
        REG_SYNC_Value = sync_delay #0-15

        self.write(self.REG_CONTROL, REG_CONTROL_Value)
        self.write(self.REG_TEST, REG_TEST_Value)
        self.write(self.REG_SYNC, REG_SYNC_Value)

    channel = property(lambda s: s.read(s.REG_CHANNEL_SELECT), lambda s, value: s.write(s.REG_CHANNEL_SELECT, value))
    chip_id = property(lambda s: s.read(s.REG_CHIP_ID), lambda s, value: s.write(s.REG_CHIP_ID, value))
    temperature = property(lambda s: s.adc.temperature(s.adc_number))

    def get_test_mode(self):
        """
        Reads the current test mode from the ADC and returns:
           0 = no test mode (data)
           1 = ramp
           2 = pulse
        """
        control_reg = self.read(self.REG_CONTROL)
        test_reg = self.read(self.REG_TEST)

        return (0, 0, 1, 2)[bool(control_reg & 1<<12) * 2 + test_reg]


    def get_temperature(self, **kwargs):
        """ Returns the temparature of this ADC chip in degC, as measured on the internal diode through the SPI ADC temperature sensor. """
        return self.adc.get_temperature(self.adc_number, **kwargs)

    def set_trim(self, value):
        for ch in range(4):
            self.write(self.REG_CHANNEL_SELECT, ch + 1)
            self.write(self.REG_TRIM, value)

    def status(self):
        """ Display the status of this ADC chip """
        w = self.read(self.REG_CHIP_ID)
        self.logger.info('  ADC[%i]: Chip type: 0x%x, Version: %i.%i, Branch: %i' % (self.adc_number, w>>8 , (w >> 2) & 0x03, w & 0x03, (w >> 4) & 0x0F ))
        self.logger.info('  ADC[%i] test mode: %s' % (self.adc_number, ('Data', 'Ramp', 'Pulse')[self.get_test_mode()]) )




class ADC_base(object):
    """ Container for an array of ADC chips that can be indexed to access all the ADC on the board """

    def __init__(self, adc_board, verbose=0):
        self.adc_board = adc_board
        self.verbose = verbose
        self.logger = logging.getLogger(__name__)
        # Create an instance of ADC_chip for each chip of the FMC board
        self.ADC = []
        for i in range(2):
            self.ADC.append(ADC_chip(self, i))

    def __getitem__(self, key):
        # if the user indexes this object (ADC[n] instead of ADC) then return the chip instance
        return self.ADC[key]

    # Low-level access functions

    def read(self, adc_number, addr):
        """ reads a word from the register of the specified ADC"""
        brd = self.adc_board # use a shorter variable name to access the FPGA instance attributes
        data = brd.spi_read_write(brd.SPI_ADC0_ADDR + adc_number, data=[0x00+addr, 0x00, 0x00], type=np.dtype('>u2'))
        return data

    def write(self, adc_number, addr=0, data=0):
        """ Writes a word to the register of the specified ADC"""
        brd = self.adc_board # use a shorter variable name to access the FPGA instance attributes
        brd.spi_read_write(brd.SPI_ADC0_ADDR + adc_number, data=[0x80 + addr, data >> 8, data & 0xFF])

    # High level functions
    def get_temperature(self, adc_number, verbose=False):
        """Reads the external temperature sensor connected to the sensing diode in the specified ADC chip"""

        brd = self.adc_board # use a shorter variable name to access the FPGA instance attributes
        data = brd.spi_read_write(brd.SPI_ADC0_TEMP_ADDR + adc_number, [0, 0], type=np.dtype('>u2'))
        temp = (data>>3)/16.0
        if self.verbose or verbose:
            print 'ADC%i Temperature is %.2f C (raw data=0x%04x)' % (adc_number, temp, data)
        return temp #110918 JFC

    # Class functions (applies to all ADCs)
    def reset(self):
        """ Resets both ADCs"""
        self.adc_board.adc_reset()

    def sync(self):
        """ Resyncs both ADCs"""
        #sysmod=self.fpga.SYSMOD; # use a shorter variable name to access the FPGA instance attributes
        #sysmod.pulse_bit('ADC_SYNC')
        self.adc_board.adc_sync()

    def init(self, **kwargs):
        """ Resets and initialize all ADCs ion the FMC board"""
        # Do nothing if the FMC is not present
        if not self.adc_board.is_present():
            return

        self.reset() # Send reset pulse on both ADCs
        for adc in self.ADC:
            adc.init(**kwargs)
            adc.channel = 0
        #self.sync() # Send sync pulse -- creates problems. to be debugged.

    def set_test_mode(self, **kwargs):
        """
        Set the test mode on all ADCs on the FMC board.
        A local sync is sent to all the local ADCs to activate the new mode.
        The sync is NOT encoded into the clock and doe snot affects other boards.
        """
        for adc in self.ADC:
            adc.init(**kwargs)
        self.sync()

    def get_test_mode(self, **kwargs):
        """
        Returns the test mode ofthe ADCs.
        An error will be raised if the mode is not the same for both ADCs
        """
        test_modes =  [adc.get_test_mode() for adc in self.ADC]
        if not all([t==test_modes[0] for t in test_modes]):
            raise SystemError('Error: ADC test modes are not indentical on all ADCs. Cannot return  the current mode as a single string.')
        return test_modes[0]

    def status(self):
        """ Prints the status of all ADCs on the board"""
        self.logger.info('--- ADCs')
        if not self.adc_board.is_present():
            self.logger.info('FMC board %i not present' % self.adc_board.fmc_number)
            return
        for adc in self.ADC:
            adc.status()
