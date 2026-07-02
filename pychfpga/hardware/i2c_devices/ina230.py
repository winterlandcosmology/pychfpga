#!/usr/bin/python

"""
ina230: Implememnts access to a INA230 I2C current/power monitor.

History:
    2014-03-20 JM: created
"""
import numpy as np


class ina230(object):
    """
    Implements the interface to the INA230 I2C current/power monitor.
    """
    REGISTER_TABLE = {
        'CONF': 0x00,
        'V_SHUNT': 0x01,
        'V_BUS': 0x02,
        'POWER': 0x03,
        'CURRENT': 0x04,
        'CAL': 0x05,
        'MASK_EN': 0x06,
        'ALERT_LIM': 0x07,
        'DIE_ID': 0xFF
        }

    v_bus_lsb = 1.25e-3  # Bus voltage conversion factor (volts/LSB)
    v_shunt_lsb = 2.5e-6  # Shunt voltage conversion factor (volts/LSB)

    def __init__(self, i2c_interface, address, port='SMPS', verbose=0):
        """ Creates an object that interfaces the INA230 I2C current/power
        monitor.

        Access is done through the I2C object 'i2c_interface' at I2C address
        'address' and on port 'port'.

        The i2c interface must provide the following methods:
            set_port()
            write_read()
        """
        self.i2c = i2c_interface
        self.address = address
        self.port = port

    def init(self, v_out=0, r_shunt=0.01, i_typ=1, tol_i=0.2, avg=3, vbus_ct=0, vsh_ct=0):
        """ Initialization of INA230 I2C current/power monitor object

        Parameters:
            v_out is the output voltage in volts (v_bus)
            r_shunt is the shunt resistance in mOhms (inductor DC resistance)
            i_typ is the typical current to be measured in Amps
            tol_i is the tolerance in the measured resistance (the Maximum Expected Current is ityp*(1+tol_i))
            avg (int): codes the number of averages. Actual averaging is
               0: 1
               1: 4
               2: 16
               3: 64
               4: 128
               5: 256
               6: 512
               7: 1024 
        """
        # Calibration to be written in CAL register.
        # self.cal = int(np.floor(((2.**15) * 5.12) / ((i_typ * (1 + tol_i)) * r_shunt)))
        # current conversion factor (amps/LSB). First calculate self.cal since
        # that implies a rounding (maybe doesn't matter)
        # self.current_lsb = 5.12 / (self.cal * r_shunt)

        self.current_lsb = (i_typ * (1 + tol_i)) / 2**15
        self.cal = int(0.00512 / (self.current_lsb * r_shunt))

        self.power_lsb = 25. * self.current_lsb  # power conversion factor (watts/LSB)

        self.write('CONF', 0x8000, mask=0x8000)  # Generating system reset
        self.write('CAL', self.cal)  # Writing calibration value to CAL register to read current and power
        self.write('CONF', 0x4007 | (avg&7 <<9) | (vbus_ct&7 << 6) | (vsh_ct&7 << 3))

    def select(self):
        """ Selects the proper I2C port to talk to this device.
        """
        self.i2c.select_bus(self.port)

    def write(self, register, value, mask=0xffff, select=True):
        """ Writes a word (two bytes) to the specified 16-bit register of the
        INA230 I2C current/power monitor.

        'register' can be the register address or the register name taken from
        REGISTER_TABLE. The I2C port for this device is set prior to the
        operation if 'select' is True. If 'mask' is specified, only the bits
        position that are set in 'mask' are changed.
        """
        # Convert port name a port address if the name is in the table.
        # Otherwise use the argument as a port address directly.
        if register in self.REGISTER_TABLE:
            register = self.REGISTER_TABLE[register]

        if select:
            self.select()

        if (mask & 0xffff) != 0xffff:
            old_value = self.read(register)  # Read word from register
            new_value = (old_value & (~mask & 0xffff)) | (value & mask)
        else:
            new_value = value

        msbyte = (new_value >> 8) & 0xff
        lsbyte = new_value & 0xff

        self.i2c.write_read(self.address, data=[register, msbyte, lsbyte])

    def read(self, register, select=True):
        """ Read a word value to the specified register
        """
        # Convert port name a port address if the name is in the table.
        # Otherwise use the argument as a port address directly.
        if register in self.REGISTER_TABLE:
            register = self.REGISTER_TABLE[register]

        if select:
            self.select()

        value = self.i2c.write_read(self.address, data=[register], read_length=2)
        return (value[0] << 8) + value[1]

    def get_bus_voltage(self):
        """ Reads value from V_BUS register and returns the corresponding bus
        voltage in Volts (always positive)
        """
        return self.read(self.REGISTER_TABLE['V_BUS']) * self.v_bus_lsb

    def get_shunt_voltage(self):
        """
        Reads value from V_SHUNT register and returns the corresponding shunt voltage in Volts
        """
        vshunt = np.int16(self.read(self.REGISTER_TABLE['V_SHUNT']))

        return vshunt * self.v_shunt_lsb

    def get_current(self):
        """
        Reads value from CURRENT register and returns the corresponding current in Amps
        """
        current = np.int16(self.read(self.REGISTER_TABLE['CURRENT']))

        return current * self.current_lsb

    def get_power(self):
        """
        Reads value from POWER register and returns the corresponding power in Watts
        """
        return self.read(self.REGISTER_TABLE['POWER']) * self.power_lsb
