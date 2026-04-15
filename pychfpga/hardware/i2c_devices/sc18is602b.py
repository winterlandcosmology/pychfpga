#!/usr/bin/python

"""
sc18is602b: Interface to the SC18IS602B II2-to-SPI bridge.

"""


class sc18is602b(object):
    """
    Implements the interface to the SC18IS602B II2-to-SPI bridge.
    """
    CONFIG = 0xF0
    CLEAR_INT = 0xF1
    IDLE = 0xF2
    GPIO_WRITE = 0xF4
    GPIO_READ = 0xF5
    GPIO_ENABLE = 0xF6
    GPIO_CONFIG = 0xF7


    def __init__(self, i2c_interface, address, port=None, verbose=0, clock_rate=3):
        """

        Parameters:

            i2c_interface (I2CInterface): I2C interface object

            address (int): I2C address

            port: object passed to i2c_interface.select_bus() to enable communication to this device

            verbose (int):

            clock_rate(int): SPI clock rate
                0: 1843 kHz
                1: 461 kHz
                2: 115 kHz
                3: 58 kHz

        """
        self.i2c = i2c_interface
        self.address = address
        self.bus_name = port
        self.clock_rate = clock_rate

    def init(self):
        """
        """
        self.set_config(order=0, mode=0, clock_rate=self.clock_rate)

    def select(self):
        """
        Selects the proper I2C port to talk to this device.
        """
        self.i2c.select_bus(self.bus_name)

    def write(self, data):
        """Write data to the device.

        Args:
            data (Any): Parameter value.

        Returns:
            None: No value is returned.
        """
        self.i2c.write_read(self.address, data)

    def read(self, read_length=1):
        """Read data from the device.

        Args:
            read_length (Any): Parameter value.

        Returns:
            Any: Returned value.
        """
        return self.i2c.write_read(self.address, [], read_length)

    def spi_write(self, port, data):
        """Write bytes on the downstream SPI target.

        Args:
            port (Any): Parameter value.
            data (Any): Parameter value.

        Returns:
            None: No value is returned.
        """
        self.select()
        self.write(bytes([1<<port]) + data)

    def spi_write_read(self, port, data):
        """ Writes N bytesto the SPI  bus and return the N bytes that were sent by the SPI device during the transaction.
        Note:
            Not sure if the chip supports repeated start. used separate readn and write commands.
        """
        self.select()
        self.write(bytes([1<<port]) + data)
        return self.read(len(data))

    def spi_read(self, read_length=1):
        """ returns the SPI data captured during the previous write 

        Args:
            read_length (Any): Parameter value.

        Returns:
            Any: Returned value.
        """
        return self.read(read_length)

    def set_config(self, order=0, mode=0, clock_rate=0):
        """
        order (int):
            0: MSB first
            1: LSB first
        mode (int):
            0: CLK low when idle. data clocked on the rising edge
            1: CLK low when idle. data clocked on the falling edge
            2: CLK high when idle. data clocked on the rising edge
            3: CLK high when idle. data clocked on the falling edge
        clock_rate:
            0: 1843 kHz
            1: 461 kHz
            2: 115 kHz
            3: 58 kHz
        """
        self.select()
        self.write(bytes((self.CONFIG, (order<<5) | (mode <<2) | clock_rate)))

    def clear_interrupts(self):
        """Clear pending interrupt status in the bridge.

        Returns:
            None: No value is returned.
        """
        self.write(bytes((self.CLEAR_INT,)))


    def set_idle(self):
        """Set idle configuration.

        Returns:
            None: No value is returned.
        """
        self.write(bytes((self.IDLE,)))

    def gpio_write(self, pattern):
        """
        pattern (int): 4 bit pattern to write to the GPIO lines. bit0 is for CS0.
        """
        self.write(bytes((self.GPIO_WRITE, pattern)))

    def gpio_read(self):
        """
        pattern (int): 4 bit pattern to write to the GPIO lines. bit0 is for CS0.
        """
        self.write(bytes((self.GPIO_READ, 0)))
        return self.read(1)[0]

    def gpio_enable(self, pattern):
        """
        pattern (int): 4 bit pattern indicating which of the CS lines can be controlled as GPIO lines.
            - bit0 is for CS0.
            - 0 = disable GPIO. 1 = enable GPIO.
            - Chip default at reset is 0b000.
        """
        self.write(bytes((self.GPIO_ENABLE, pattern)))

    def gpio_config(self, pattern):
        """
        pattern (int): 8 bit pattern indicating how each GPIO line is driven when defines as GPIO.
            - bit 0:1 is for SS0, etc...
                - 00 = quasi bidir
                - 01: push-pull
                - 10: input only (Hi-Z)
                - 11: open drain
            - Chip default at reset is 0b000.
        """
        self.write(bytes((self.GPIO_CONFIG, pattern)))


