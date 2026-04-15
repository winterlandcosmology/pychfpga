#!/usr/bin/python

"""
tca9544a.py module
Defines a class that implements the interface to the TCA9544A I2C switch.

 History:
 2021-09-21 : JFC : Created
"""


class tca9544a(object):
    """
    Implements the interface to the TCA9548A I2C switch.
    """

    def __init__(self, i2c_interface, address,  verbose=0):
        """Initialize this device driver instance.

        Args:
            i2c_interface (Any): Parameter value.
            address (Any): Parameter value.
            verbose (Any): Parameter value.

        Returns:
            None: No value is returned.
        """
        self.i2c = i2c_interface
        self.switch_address = address

    def set_port(self, port=None, *args, **kwargs):
        """
        Activates selected I2C port of the switch.

        The port should be specified as a single port number.
        """
        bit_pattern = (0x40 | port) if port is not None else 0
        self.i2c.write_read(self.switch_address, data=[bit_pattern], *args, **kwargs)
