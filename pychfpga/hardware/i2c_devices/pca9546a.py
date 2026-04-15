#!/usr/bin/python

"""
pca9546a.py module
Defines a class that implements the interface to the PCA9546A 4-port I2C switch.
"""


class pca9546a(object):
    """
    Implements the interface to the PCA9546A 4-port I2C switch.

    Parameters:

        i2c_interface (I2CInterface object): object providing the write command to the I2C bus on which the device is connected.

        address (int): 7-bit I2C address of this device

        port (int or dict): integer specyfing which I2C port to use, or dict describing the I2C switch and switch parameters to use.

        verbose (int): sets the amount of messages printed

    The switch has a single register: the control register, whose 4 lsb bits
    enables the connection of each input bus to the output bus.
    """

    def __init__(self, i2c_interface, address, port=0, verbose=0):
        """Initialize this device driver instance.

        Args:
            i2c_interface (Any): Parameter value.
            address (Any): Parameter value.
            port (Any): Parameter value.
            verbose (Any): Parameter value.

        Returns:
            None: No value is returned.
        """
        self.i2c = i2c_interface
        self.switch_address = address
        self.port = port
    def set_port(self, ports=None, bitmask=None, *args, **kwargs):
        """ Activate selected I2C ports of the switch and disable the others.

        The ports can be either specified as a single port number (e.g.
        ports=1), a list of ports (e.g. ports=[1,2,3]) or a bitmask (bit_mask
        = 0x83)

        Parameters:

            ports (int or list of int): port number(s) to activate. Cannot be used with `bitmask`

            bitmask (int): 4-bit value indicating the state of each bus.

            arg (list): positional arguments passed to the i2c write command

            kwargs (list): keyword arguments passed to the i2c write command

        """
        if (ports is not None) and (bitmask is None):
            if isinstance(ports, int):
                ports = [ports]
            bitmask = sum((1 << port) for port in ports)
        elif (ports is None) and (bitmask is not None):
            pass
        else:
            raise Exception('Must provide either port number(s) or a bit mask')

        self.i2c.write_read(self.switch_address, data=[bitmask], *args, **kwargs)
