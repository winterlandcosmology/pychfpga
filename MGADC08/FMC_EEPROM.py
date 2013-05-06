#!/usr/bin/python
# Disable pylint Line too long (=C0301)
# pylint: disable=C0301 

"""
FMC_EEPROM.py module 
 Implements the FMC EEPROM interface 
 History:
    2012-03-29 JFC : Created 
    2012-08-27 JFC : Fixed reference to common.util as pychime.common.util         
"""

from pychime.common import util


class FMC_EEPROM_base(object):
    """ Implements the MGADC08 FMC EEPROM interface """
    # I2C addresses
    FMC_EPPROM_ADDR = 0x50    # 0x50 and 0x51 are the two pages.
    FMC_EPPROM_PORT = 0    # 

    def __init__(self, fpga, verbose=1):
        self.fpga_instance = fpga
        self.verbose = verbose

    def read(self, addr, length=1, **kwargs):
        """ Reads from the EEPROM"""
        i2c = self.fpga_instance.I2C
        i2c.set_i2c_switch('FMC')
        data = i2c.i2c_write_read(self.FMC_EPPROM_PORT, self.FMC_EPPROM_ADDR + ((addr >> 16) & 1), [(addr >> 8) & 0xff, addr & 0xff], read_length=length, **kwargs) # reads a byte

        return data

    def write(self, addr, data, **kwargs):
        """ Writes to the EEPROM"""
        i2c = self.fpga_instance.I2C
        i2c.i2c_write(self.FMC_EPPROM_PORT, self.FMC_EPPROM_ADDR + ((addr >> 16) & 1), [(addr >> 8) & 0xff, addr & 0xff, data], **kwargs) # sets the address

    def init(self):
        """ Initializes the EEPROM handling module (the EEPROM is not accected)"""
        pass

    def read_DDR3_reg(self, addr):
        """ Reads from the EEPROM"""
        i2c = self.fpga_instance.I2C
        i2c_addr = 0x1b
        i2c.i2c_write(0, i2c_addr, [addr]) # sets the address
        data = i2c.i2c_read(0, i2c_addr, length=2) # reads a byte
        return data


    
    def status(self):
        """ Shows EEPROM data"""
        print '--------------- FMC EEPROM ---------------'
        if self.fpga_instance.ADC_BOARD.is_present():
            print 'FMC EEPROM data at address 0x00-0x03 is: ', util.hex(self.read(0, length=4))
        print '------------------------------------------'
    
