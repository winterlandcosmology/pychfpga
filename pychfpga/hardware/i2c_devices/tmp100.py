#!/usr/bin/python

""" Implements the interface for the TEMP100 I2C temperature sensor.

.. History:
 2014-03-04 JM: created
 2014-03-18 JM: Fixed read function to allow reading by register name, not only by register address
                Fixed masking in write function
                Added get_temperature
"""


class tmp100(object):
    """
    Implements the interface to the TEMP100 I2C temperature sensor.
    """
    REGISTER_TABLE = {
        'TEMP': 0x00,  # input port register
        'CONF': 0x01,
        'TLOW': 0x02,
        'THIGH': 0x03
        }

    def __init__(self, i2c_interface, address, port='GPIO', verbose=0):
        """
        Creates an object that interfaces the TEMP100 I2C temperature sensor.

        Access is done through the I2C object 'i2c_interface' at I2C address
        'address' and on port 'port'.

        The i2c interface must provide the following methods:
            set_port()
            write_read()
        """
        self.i2c = i2c_interface
        self.address = address
        self.port = port

    def init(self, bit_resolution=12):
        """
        Initialization of TEMP100 I2C temperature sensor object
        bit_resolution is the number of bits of resolution of the temperature register. It can take values 9, 10, 11, 12
        """
        self.write('CONF', (bit_resolution-9) << 5, mask=0x60)

    def select(self):
        """
        Selects the proper I2C port to talk to this device.
        """
        self.i2c.select_bus(self.port)

    def write(self, register, value, mask=0xff, select=True):
        """
        Writes a byte to the specified register of the TEMP100 I2C temperature sensor.

        'register' can be the register address or the register name taken from
        REGISTER_TABLE.

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
            old_value = self.i2c.write_read(self.address, data=[register], read_length=1)
            new_value = (old_value & (~mask & 0xff)) | (value & mask)
        else:
            new_value = value

        self.i2c.write_read(self.address, data=[register, new_value])

    def read(self, register, select=True, read_length=1):
        """
        Read a value to the specified register
        """
        # Convert port name a port address if the name is in the table.
        # Otherwise use the argument as a port address directly.
        if register in self.REGISTER_TABLE:
            register = self.REGISTER_TABLE[register]

        if select:
            self.select()

        return self.i2c.write_read(self.address, data=[register], read_length=read_length)

    def get_temperature(self):
        """
        Reads temperature (in degrees Celcius) from TEMP register
        """
        data = self.read('TEMP', read_length=2)  # reading temperature (2 bytes)
        data = (data[0] << 8) + data[1]

        if data >= (1 << 15):  # temperature is negative
            temp = -((1 << 16)-data) / 2. ** 8
        else:
            temp = data / 2. ** 8

        return temp
