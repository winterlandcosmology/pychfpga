"""amc6821.py module: Provides a class to read/write to the AMC6821 fan controller chip
"""

# import iceboard as ib
import logging
import numpy as np

class AMC6821(object):
    """ Class defining the interface to the AMC6821 fan controller chip
    """

    REGISTER_MAP = {
         # Register Name : (datatype, memory location, bytes, page)
         'DeviceID': (0x3D, 0, 8),
         'START' : (0,0,1),
         'FDRC': (0x00, 5 ,2),
         'LocalTempLSB': (0x06, 5, 3),
         'LocalTempMSB': (0x0A, 0, 8),
         'DutyCycle': (0x22, 0, 8),
    }


    def __init__(self, i2c, address=0x18, bus_name='BP'):
        """ Create a fan controller object.
        """
        self._logger = logging.getLogger(__name__)
        # self._logger.debug('%.32r: Instantiating AMC6821 Fan controller object' % self)

        self._i2c = i2c
        self._bus_name = bus_name
        self._address = address

    def open(self):
        pass

    def close(self):
        pass

    def init(self):
        """Initializes the backplane hardware to a known state"""
        # self.write('START', 1)

        self.write(0x00, 0x9C)  # Set software duty cycle mode, invert PWM polarity (high=ON), start temperature & PWM monitoring
        # self.set_duty_cycle(100)

    def select(self):
        """
        Selects the proper I2C port to talk to this device.
        """
        self._i2c.select_bus(self.bus_name)

    def is_present(self):
        return self._i2c.is_present(self._address, self._bus_name)

    def read(self, name, select=True):
        if isinstance(name, str):
            if name not in self.REGISTER_MAP:
                raise RuntimeError("'%s'  is not a valid fan controller register name" % name)
            (register, bit, width) = self.REGISTER_MAP[name]
        else:
            (register, bit, width) = (name, 0, 8)

        if select:
            self._i2c.select_bus(self._bus_name)

        value = self._i2c.write_read(self._address, data=[register], read_length=1)[0]
        value = (value >> bit) & ((1 << width)-1)
        return value

    def write(self, name, value, select=True):
        if isinstance(name, str):
            if name not in self.REGISTER_MAP:
                raise ValueError("'%s'  is not a valid fan controller register name" % name)
            (register, bit, width) = self.REGISTER_MAP[name]
        else:
            (register, bit, width) = (name, 0, 8)

        if width == 1:
            value = bool(value)
        elif value < 0 or value >= (1 << width):
            raise ValueError("%i is an invalid value for fan controller field value '%s'" % (value, name))

        if select:
            self._i2c.select_bus(self._bus_name)

        if width == 8:
            self._i2c.write_read(self._address, data=[register, value])
        else:
            mask = ((1 << width)-1) << bit
            old_value = self._i2c.write_read(self._address, data=[register], read_length=1)  #self.i2c.write_read(self.address, read_length=1)
            new_value = (old_value & (~ mask)) | (value & mask)
            self._i2c.write_read(self._address, data=[register, new_value])

    def set_control_mode(self, mode):
        self.write('FDRC', mode)

    def set_duty_cycle(self, duty):
        self.write('DutyCycle', int(duty/100.*255.))

    def get_local_temperature(self):
        return np.int16((self.read('LocalTempLSB') << 5) + (self.read('LocalTempMSB') << 8))/256.  # LSB must be read first




























