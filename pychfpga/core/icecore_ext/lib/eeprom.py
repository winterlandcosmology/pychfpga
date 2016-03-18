#!/usr/bin/python
# Disable pylint Line too long (=C0301)
# pylint: disable=C0301

"""
FMC_EEPROM.py module
 Implements the EEPROM read/write interface through the FPGA
 History:
    2012-03-29 JFC : Created
    2012-08-27 JFC : Fixed reference to common.util as pychfpga.common.util
"""
import logging
import numpy as np

class eeprom(object):
    """ Implements an EEPROM interface optimized for I2C access through the FPGA"""

    class EEPROMException(Exception): pass

    def __init__(self, i2c_handler, address, bus_name, address_width, write_page_size=0, verbose=1):
        self.i2c = i2c_handler
        self.bus_name = bus_name
        self.verbose = verbose
        self.logger = logging.getLogger(__name__)
        self.address = address
        self.address_width = address_width
        self.address_max = (1<<address_width)-1
        self.write_page_size = write_page_size
        self.address_mask = (1<<address_width)-1
        self.address_page_mask = write_page_size-1

    def _get_addr_bytes(self, addr):
        """
        Return a list of bytes corresponding to the EEPROM address.
        Byte 0 are excess bits going in the I2C command byte
        Bytes 1:N are the address bytes sent as the first data bytes sent with each command.
        """
        addr_bytes = max((self.address_width+7) // 8, 2) # add an extra byte for the part that falls in the I2c address field
        bytes = [(((addr & self.address_max) >> (8*i)) & 0xff) for i in range(addr_bytes-1, -1, -1)]
        # bytes[0] &= 2**(self.address_width % 8)-1 # mask the bits not used for data address in the i2c command byte
        return bytes

    def read(self, addr, length=1, retry=0, **kwargs):
        """ Reads from the EEPROM. Data is returned as a string.

        if length == -1, the data is read from the specified address until the end of the EEPROM.
        """

        if length == -1:
            length = (1 << self.address_width) - addr

        if addr is not None:
            if (addr <0 or (addr+length-1) > (2**self.address_width-1)):
                raise ValueError('Invalid EEPROM address range. All reads must be from adress 0x%x and 0x%x' % (0, (2**self.address_width-1)))

        try:
            self.i2c.select_bus(self.bus_name, retry=retry)
        except:
            self.logger.error('Failed to set I2C switch to %s.' % (self.bus_name))
            raise

        data = ""
        while length:
            # print '.',
            block_length = min(length, 4)
            if addr is None:
                addr_bytes = [0]
            else:
                addr_bytes = self._get_addr_bytes(addr)
            # trial = 0
            # while True:
            try:

                block_data = self.i2c.write_read(self.address + addr_bytes[0], addr_bytes[1:], read_length=block_length, retry=retry, **kwargs).tostring() # reads a byte
                    # break
            except:
                    # self.logger.warning('I2C Error while reading EEPROM at memory address %i. Retrying...' % addr)
                    # trial +=1
                    # if trial>retry:
                self.logger.error('Failed to read EEPROM at memory address %i after %i retries.' % (addr, retry))
                raise
            data += block_data
            # print 'data=', data
            length -= block_length
            if addr is not None:
                addr += block_length
            # print length
        return data

    def write(self, addr, data, select=True,  **kwargs):
        """ Writes to the EEPROM.
        Data can be a string, list of numpy array.
        """

        if not self.write_page_size:
            raise RuntimeError('EEPROM is not writable (write page size is zero)')

        # make sure the data is always a list
        if isinstance(data, int):
            data = [data]
        elif isinstance(data, str):
            data = [ord(c) for c in data]
        else:
            data = list(data)

        if select:
            self.i2c.select_bus(self.bus_name)

        while data:
            addr_bytes = self._get_addr_bytes(addr)
            #Block  length must not exceed:
            #  1) The number of bytes to send
            #  2) The number of bytes that the I2C interface can send ( 3 - number of address bytes)
            #  3) The number of bytes until the end of the page
            block_length = min(len(data), 3-len(addr_bytes)+1, (addr | self.address_page_mask) - addr + 1)
            self.i2c.write_read(self.address | addr_bytes[0], addr_bytes[1:] + data[:block_length], read_length = 0, **kwargs) # sets the address
            data = data[block_length :]
            addr += block_length

    def set_addr(self, addr, **kwargs):
        """ Sets the current read/write address of the EEPROM"""
        self.i2c.select_bus(self.bus_name)
        addr_bytes = self._get_addr_bytes(addr)
        self.i2c.write_read(self.address + addr_bytes[0], addr_bytes[1:], read_length = 0, **kwargs) # sets the address

    def is_present(self, page = 0):
        """ Test the presence of the EEPROM for specified page (i.e. I2C address offset)
        """
        self.i2c.select_bus(self.bus_name, retry=3)
        try:
            self.i2c.write_read(self.address + page, data=[], read_length=0, retry=0 ) #dummy I2C acces
        except IOError:
            return False
        return True

    def init(self):
        """ Initializes the EEPROM handling module (the EEPROM is not accecssed)"""
        pass

    def status(self):
        """ Shows EEPROM data"""
        self.logger.info('-- FMC EEPROM ')
        try:
            self.logger.info('FMC EEPROM data at address 0x00-0x03 is: %s' % ' '.join([hex(x) for x in self.read(0, length=4)]))
        except:
            self.logger.info('FMC EEPROM did not respond')

