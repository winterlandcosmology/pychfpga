class GPIO(object):
    """Provides a single-point, abstracted access to all GPIO bits found on the IceBoard or IceCrate"""

    def __init__(self, gpio_table):
        self._gpio_table = gpio_table

    def read(self, name, select=True):
        if name not in self._gpio_table:
            raise AttributeError("'%s'  is not a valid GPIO signal name" % name)
        (io_expander, byte, bit, width) = self._gpio_table[name]
        value = io_expander.read(byte, select=select)
        value = (value >> bit) & ((1 << width)-1)
        return value

    def write(self, name, value, select=True):
        if name not in self._gpio_table:
            raise AttributeError("'%s'  is not a valid GPIO name" % name)
        (io_expander, byte, bit, width) = self._gpio_table[name]
        if width == 1:
            value = bool(value)
        elif value < 0 or value >= (1 << width):
            raise ValueError("%i is an invalid value for GPIO field '%s'" % (value, name))
        io_expander.write(byte, value << bit, mask= ((1 << width)-1) << bit, select=select)

    def __getattr__(self, name):
        if name in self._gpio_table:
            return self.read(name)
        else:
            raise AttributeError
