#!/usr/bin/python

"""
AmbTemp.py module
 Implements the interface to the FMC Ambient Temperature monitor chip

History:
    2011-07-09 : JFC : Created from test code in chFPGA.py
"""
import numpy as np
import logging

class AmbTemp_base(object):

    def __init__(self, adc_board, verbose=0):
        self.adc_board = adc_board
        self.verbose = verbose
        self.logger = logging.getLogger(__name__)

    def init(self):
        pass

    def read(self):
        """
        Reads the raw word from the SPI Ambient temperature monitor on the ADC FMC
        """
        brd = self.adc_board
        return brd.spi_read_write(brd.SPI_AMB_TEMP_ADDR, [0, 0], type=np.dtype('>u2'))  # Read 16 bit word

    def get_temperature(self):
        """
        Returns the ambient temperature of the FMC ambient temperature monitor chip in Celcius
        """
        data = self.read()
        data = np.int16(data << 2) >> 2  # sign-extend bit 13 to bits 14 and 15 to obtain a 16-bit signed value
        temp = data / 32
        if self.verbose:
            print(f'FMC Board Temperature is {temp:.2f} C')
        return temp

    temperature = property(get_temperature)

    def status(self):
        self.logger.info(f'{self.adc_board!r}: --- Ambient Temperature Sensor')
        self.logger.info(f'{self.adc_board!r}: Ambient Temperature of the ADC board: {self.temperature:.1f} C')

