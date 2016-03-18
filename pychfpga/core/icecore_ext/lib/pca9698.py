#!/usr/bin/python
# Disable pylint Line too long (=C0301)
# pylint: disable=C0301

"""
pca9698: Implememnts access to a PCA9698 I2C switch.

 History:
 2013-08-08 : JFC : Created
 2014-02-23 JFC: Added register table, select(), masked write.
 2014-03-04 JM: Added default I/O pin configuration at init()
 2014-03-18 JM: Fixed read function to allow reading by register name, not only by register address
                Fixed masking in write function
 2014-10-14 AJG: Modified code to make applicable to PCA9698 (original code was for PCA9575)

"""
# MGADC08 FMC ADC board device handlers
import logging

class pca9698(object):
    """
    Implements the interface to the PC9698 I2C IO Extender.
    http://www.nxp.com/documents/data_sheet/PCA9698.pdf
    """
    REGISTER_TABLE = {
        'IN0': 0x00,   #Input port registers
        'IN1': 0x01,
        'IN2': 0x02,
        'IN3': 0x03,
        'IN4': 0x04,

        'OUT0' : 0x08, #Output port registers
        'OUT1' : 0x09,
        'OUT2' : 0x0A,
        'OUT3' : 0x0B,
        'OUT4' : 0x0C,

        'INVRT0': 0x10, #Polarity inversion registers
        'INVRT1': 0x11,
        'INVRT2': 0x12,
        'INVRT3': 0x13,
        'INVRT4': 0x14,

        'CFG0': 0x18, #IO configuration registers
        'CFG1': 0x19,
        'CFG2': 0x1A,
        'CFG3': 0x1B,
        'CFG4': 0x1C,

        'MSK0': 0x20, #Mask interupt registers
        'MSK1': 0x21,
        'MSK2': 0x22,
        'MSK3': 0x23,
        'MSK4': 0x24,

        'OUTCONF': 0x28, #Miscellaneous registers
        'ALLBNK': 0x29,
        'MODE': 0x30,

        'R1': 0x05,   #Reserved registers
        'R2': 0x06,
        'R3': 0x07,
        'R4': 0x0D,
        'R5': 0x0E,
        'R6': 0x0F,
        'R7': 0x15,
        'R8': 0x16,
        'R9': 0x17,
        'R10': 0x1D,
        'R11': 0x1E,
        'R12': 0x1F,
        'R13': 0x25,
        'R14': 0x26,
        'R15': 0x27
        }

    def __init__(self, i2c_interface, address, port='BP', verbose=0):
        """
        Creates an object that interfaces the PCA9698 I2C IO Extender.
        Access is done through the I2C object 'i2c_interface' at I2C address 'address' and on port 'port'.
        The i2c interface must provide the following methods:
            set_port()
            write_read()

        Valid backplane addresses are: 0b0100000 (QSFPsetA 0x20), 0b0100010 (QSFPsetB, 0x22), 0b0100100 (Resets, 0x24)
        """

        self.i2c = i2c_interface
        self.address = address
        self.port = port

    def init(self,
             cfg0_def=0xff, cfg1_def=0xff, cfg2_def=0xff, cfg3_def=0xff, cfg4_def=0xff,
             out0_def=0, out1_def=0, out2_def=0, out3_def=0, out4_def=0, verbose=0):
        """
        Initialization of PCA9698 I2C IO Extender object
        cfg0_def..cfg4_def sets the default configuration of the I/O pins. By default all pins are inputs.
        out0_def..out4_def sets the default output state to logic 0 when direction is set to output
        """

        self.write_reg('CFG0', cfg0_def)
        self.write_reg('CFG1', cfg1_def)
        self.write_reg('CFG2', cfg2_def)
        self.write_reg('CFG3', cfg3_def)
        self.write_reg('CFG4', cfg4_def)

        self.write_reg('OUT0', out0_def)
        self.write_reg('OUT1', out1_def)
        self.write_reg('OUT2', out2_def)
        self.write_reg('OUT3', out3_def)
        self.write_reg('OUT4', out4_def)

    def select(self):
        """
        Selects the proper I2C port to talk to this device.
        """
        self.i2c.select_bus(self.port)

    def write_reg(self, register, value, mask=0xff, select=True):
        """
        Writes a byte to the specified register of the IO Expander.

        'register' can be the register address or the register name taken from
        REGISTER_TABLE.

        The I2C port for this device is set prior to the operation if 'select'
        is True.

        If 'mask' is specified, only the bits position that are set in 'mask'
        are changed.
        """
        # Convert port name a port address if the name is in the table. Otherwise use the argument as a port address directly.
        if register in self.REGISTER_TABLE:
            register = self.REGISTER_TABLE[register]

        if select:
            self.select()

        if (mask & 0xFF)  != 0xff:
            old_value = self.i2c.write_read(self.address, data=[register], read_length=1)#self.i2c.write_read(self.address, read_length=1)
            new_value = (old_value & (~ mask)) | (value & mask)
        else:
            new_value = value

        self.i2c.write_read(self.address, data=[register, new_value])

    def read_reg(self, register, select=True):
        """
        Read a value from the specified register (an address or name)
        """
        # Convert port name a port address if the name is in the table. Otherwise use the argument as a port address directly.
        if register in self.REGISTER_TABLE:
            register = self.REGISTER_TABLE[register]

        if select:
            self.select()

        return self.i2c.write_read(self.address, data=[register], read_length=1)[0]

    def write(self, byte, value, mask=0xff, select=True):
        self.write_reg('OUT%i' % byte, value, mask=mask, select=select)

    def read(self, byte, select=True):
        return self.read_reg('IN%i' % byte, select=select)

    def is_present(self):
        return self.i2c.is_present(self.address, self.port)
