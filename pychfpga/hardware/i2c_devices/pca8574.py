#!/usr/bin/python

"""
 Implememnts interface to the PCA8574 I2C 8-bit IO expander.

"""


class PCA8574(object):
    """
    Implements the interface to the TCA6416A I2C IO expander.
    """

    def __init__(self, i2c_interface, address, port='GPIO', verbose=0):
        """
        """
        self.i2c = i2c_interface
        self.address = address
        self.bus_name = port

    def init(self):
        """
        Initialization of the I2C IO Extender object
        cfg0_def, cfg1_def sets the default configuration of the I/O pins. By default all pins are inputs.
        """
        pass

    def select(self):
        """
        Selects the proper I2C port to talk to this device.
        """
        self.i2c.select_bus(self.bus_name)

    def write(self, value, register, mask=0xff, select=True):
        """
        Writes a byte to the specified register of the IO Expander.
        The I2C port for this device is set prior to the operation if 'select' is True.
        If 'mask' is specified, only the bits position that are set in 'mask' are changed.
        """
        # Convert port name a port address if the name is in the table.
        # Otherwise use the argument as a port address directly.

        if select:
            self.select()

        if (mask & 0xFF) != 0xff:
            old_value = self.i2c.write_read(self.address, data=[register], read_length=1)[0]
            new_value = (old_value & (~ mask)) | (value & mask)
            # print(f'Pca9575 register={register} old_value={old_value}')
        else:
            new_value = value

        self.i2c.write_read(self.address, data=[new_value])

    def read(self, select=True):
        """
        Read a value to the specified register
        """

        if select:
            self.select()

        return self.i2c.write_read(self.address, data=[], read_length=1)[0]

