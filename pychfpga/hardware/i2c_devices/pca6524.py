#!/usr/bin/python

"""
 Implememnts interface to the pca6524 I2C  IO expander with 3 8-bit ports.

"""


class pca6524(object):
    """
    Implements the interface to the TCA6416A I2C IO expander.
    """
    REGISTER_TABLE = {
        'IN0': 0x00,  # input port register
        'IN1': 0x01,
        'IN2': 0x02,

        'OUT0': 0x04,
        'OUT1': 0x05,
        'OUT2': 0x06,

        'POL0': 0x08,
        'POL1': 0x09,
        'POL1': 0x0A,

        'CFG0': 0x0C,  # 1=in, 0=out
        'CFG1': 0x0D,  # 1=in, 0=out
        'CFG2': 0x0E,  # 1=in, 0=out
        }

    def __init__(self, i2c_interface, address, port='GPIO', verbose=0):
        """
        Creates an object that interfaces the PCA6524 I2C IO Extender.
        Access is done through the I2C object 'i2c_interface' at I2C address 'address' and on port 'port'.

        The i2c interface must provide the following methods:

        - set_port()
        - write_read()
        """
        self.i2c = i2c_interface
        self.address = address
        self.bus_name = port

    def init(self):
        """
        Initialization of the I2C IO Extender object
        cfg0_def, cfg1_def sets the default configuration of the I/O pins. By default all pins are inputs.
        """
        # if out0_default is not None:
        #     self.write_reg('OUT0', out0_default)

        # if out1_default is not None:
        #     self.write_reg('OUT1', out1_default)

        # self.write_reg('CFG0', cfg0_def)
        # self.write_reg('CFG1', cfg1_def)
        # self.write_reg('BKEN0', bken0)
        # self.write_reg('BKEN1', bken1)
        # self.write_reg('PUPD0', pupd0)
        # self.write_reg('PUPD1', pupd1)
        pass

    def select(self):
        """
        Selects the proper I2C port to talk to this device.
        """
        self.i2c.select_bus(self.bus_name)

    def write_reg(self, register, value, mask=0xff, select=True):
        """
        Writes a byte to the specified register of the IO Expander.
        'register' can be the register address or the register name taken from REGISTER_TABLE.
        The I2C port for this device is set prior to the operation if 'select' is True.
        If 'mask' is specified, only the bits position that are set in 'mask' are changed.
        """
        # Convert port name a port address if the name is in the table.
        # Otherwise use the argument as a port address directly.
        if register in self.REGISTER_TABLE:
            register = self.REGISTER_TABLE[register]

        if select:
            self.select()

        if (mask & 0xFF) != 0xff:
            old_value = self.i2c.write_read(self.address, data=[register], read_length=1)[0]
            new_value = (old_value & (~ mask)) | (value & mask)
            # print(f'Pca9575 register={register} old_value={old_value}')
        else:
            new_value = value

        self.i2c.write_read(self.address, data=[register, new_value])

    def read_reg(self, register, select=True):
        """
        Read a value to the specified register
        """
        # Convert port name a port address if the name is in the table.
        # Otherwise use the argument as a port address directly.
        if register in self.REGISTER_TABLE:
            register = self.REGISTER_TABLE[register]

        if select:
            self.select()

        return self.i2c.write_read(self.address, data=[register], read_length=1)[0]

    def write(self, byte, value, mask=0xff, select=True):
        self.write_reg('OUT%i' % byte, value, mask=mask, select=select)

    def read(self, byte, select=True):
        return self.read_reg('IN%i' % byte, select=select)
