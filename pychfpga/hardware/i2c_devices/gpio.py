"""Generic GPIO accessor on top of I2C expander backends."""

class GPIO(object):
    """Provides a single-point, abstracted access to all GPIO bits found on the IceBoard or IceCrate"""

    def __init__(self, gpio_table):
        """Build a GPIO namespace from a signal-to-expander mapping.

        Args:
            gpio_table (Any): Parameter value.

        Returns:
            None: No value is returned.
        """
        self._gpio_table = gpio_table

    def read(self, name, select=True):
        """Read a GPIO field value by symbolic name.

        Args:
            name (Any): Parameter value.
            select (Any): Parameter value.

        Returns:
            Any: Returned value.
        """
        if name not in self._gpio_table:
            raise AttributeError(f"'{name}'  is not a valid GPIO signal name")
        (io_expander, byte, bit, width) = self._gpio_table[name]
        value = io_expander.read(byte, select=select)
        value = (value >> bit) & ((1 << width)-1)
        return value

    def write(self, name, value, select=True):
        """Write a GPIO field value by symbolic name.

        Args:
            name (Any): Parameter value.
            value (Any): Parameter value.
            select (Any): Parameter value.

        Returns:
            None: No value is returned.
        """
        if name not in self._gpio_table:
            raise AttributeError(f"'{name}'  is not a valid GPIO name")
        (io_expander, byte, bit, width) = self._gpio_table[name]
        if width == 1:
            value = bool(value)
        elif value < 0 or value >= (1 << width):
            raise ValueError(f"{int(value)} is an invalid value for GPIO field '{name}'")
        io_expander.write(byte, value << bit, mask=((1 << width)-1) << bit, select=select)

    def __getattr__(self, name):
        """Expose GPIO names as read-only attributes.

        Args:
            name (Any): Parameter value.

        Returns:
            Any: Returned value.
        """
        if name in self._gpio_table:
            return self.read(name)
        else:
            raise AttributeError
