#!/usr/bin/python

"""
lmk04208spi: Interface to the LMK04208 PLL via the SC18IS202 II2-to-SPI bridge.

.. History:
    2013-08-08 : JFC : Created
    2014-02-23 JFC: Added register table, select(), masked write.
    2014-03-04 JM: Added default I/O pin configuration at init()
    2014-03-18 JM: Fixed read function to allow reading by register name, not only by register address
                   Fixed masking in write function
"""

import time
import os

from .TICSPRO_loader import load_pll_regs


class lmx2594spi(object):
    """
    Implements the interface to the LMK04208 PLL via the SC18IS202 II2-to-SPI bridge.
    """

    def __init__(self, i2c_interface, address, spi_port=0, port='GPIO', verbose=0):
        """
        """
        self.i2c = i2c_interface
        self.address = address
        self.bus_name = port
        self.spi_port = spi_port

    def init(self, filename="lmx2594_registers.txt"):
        """
        """

        regs = load_pll_regs(filename)

        for (reg, value) in regs:
            if reg<=78:
                print(f'Writing Reg {reg} with 0x{value:08X}')
                self.write_word(value)
                time.sleep(0.01)


    def select(self):
        """
        Selects the proper I2C port to talk to this device.
        """
        self.i2c.select_bus(self.bus_name)

    def write(self, data):
        spi_data = bytes([1<<self.spi_port]) + data
        self.select()
        self.i2c.write_read(self.address, spi_data)

    def write_word(self, value):
            self.write(value.to_bytes(3, 'big'))  # Commands are 24-bits wide

    # def write_reg(self, register, value, mask=0xff, select=True):
    #     """
    #     Writes a byte to the specified register of the IO Expander.
    #     'register' can be the register address or the register name taken from REGISTER_TABLE.
    #     The I2C port for this device is set prior to the operation if 'select' is True.
    #     If 'mask' is specified, only the bits position that are set in 'mask' are changed.
    #     """
    #     # Convert port name a port address if the name is in the table.
    #     # Otherwise use the argument as a port address directly.
    #     if register in self.REGISTER_TABLE:
    #         register = self.REGISTER_TABLE[register]

    #     if select:
    #         self.select()

    #     if (mask & 0xFF) != 0xff:
    #         old_value = self.i2c.write_read(self.address, data=[register], read_length=1)[0]
    #         new_value = (old_value & (~ mask)) | (value & mask)
    #         # print(f'Pca9575 register={register} old_value={old_value}')
    #     else:
    #         new_value = value

    #     self.i2c.write_read(self.address, data=[register, new_value])

    # def read_reg(self, register, select=True):
    #     """
    #     Read a value to the specified register
    #     """
    #     # Convert port name a port address if the name is in the table.
    #     # Otherwise use the argument as a port address directly.
    #     if register in self.REGISTER_TABLE:
    #         register = self.REGISTER_TABLE[register]

    #     if select:
    #         self.select()

    #     return self.i2c.write_read(self.address, data=[register], read_length=1)[0]

    # def write(self, byte, value, mask=0xff, select=True):
    #     self.write_reg('OUT%i' % byte, value, mask=mask, select=select)

    # def read(self, byte, select=True):
    #     return self.read_reg('IN%i' % byte, select=select)
