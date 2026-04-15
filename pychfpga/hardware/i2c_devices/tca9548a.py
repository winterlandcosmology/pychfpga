#!/usr/bin/python

"""
tca9548a.py module
Defines a class that implements the interface to the TCA9548A I2C switch.

 History:
 2013-08-08 : JFC : Created
"""


class tca9548a(object):
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

    def set_port(self, ports=None, bitmask=None, *args, **kwargs):
        """
        Activates selected I2C ports of the switch and disable the others.

        The ports can be either specified as a single port number (e.g.
        ports=1), a list of ports (e.g. ports=[1,2,3]) or a bitmask (bit_mask
        = 0x83)
        """
        bit_pattern = 0

        if (ports is not None) and (bitmask is None):
            if isinstance(ports, int):
                ports = [ports]
            for port in ports:
                bit_pattern |= (1 << port)
        elif (ports is None) and (bitmask is not None):
            bit_pattern = bitmask
        else:
            raise Exception('Must provide either port number(s) or a bit mask')

        self.i2c.write_read(self.switch_address, data=[bit_pattern], *args, **kwargs)
