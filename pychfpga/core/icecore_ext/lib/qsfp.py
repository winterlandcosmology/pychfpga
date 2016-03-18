"""qsfp.py module: Provides a class to read/write to a QSFP cable
"""

# import iceboard as ib
import logging
import numpy as np
from .eeprom import eeprom as EEPROM

class QSFP(object):
    """ Class defining the interface to a QSFP+ cable.
    """

    QSFP_EEPROM_MAP = {
         # Register Name : (datatype, memory location, bytes, page)
         'Identifier': ('bin', 0, 1, 0),
         'Status': ('bin', 1, 2, 0),
         'ChanStatusIntFlags': ('bin', 3, 2, 0),
         'ModMonIntFlags': ('bin', 6, 2, 0),
         'ChanMonIntFlags' : ('bin', 9, 4, 0),
         'MeasuredTemp' : ('bin', 22, 2, 0),
         'MeasuredSupV' : ('bin', 26, 2, 0),
         'RxPow1': ('bin', 34, 8, 0),
         'RxPow2': ('bin', 36, 8, 0),
         'RxPow3': ('bin', 38, 8, 0),
         'RxPow4': ('bin', 40, 8, 0),
         'ChanTxBias':('bin', 42, 8, 0),
         'LaserDisable':('bin', 86, 1, 0),
         'RateSelect':('bin', 87, 2, 0),
         'RxAppSelect':('bin', 89, 4, 0),
         'PowerSet':('bin', 93, 1, 0),
         'TxAppSelect':('bin', 94, 4, 0),
         'IntLMask_LOS':('bin', 100, 1, 0),
         'IntLMask_TXFault':('bin', 101, 1, 0),
         'IntLMask_Temp':('bin', 103, 1, 0),
         'IntLMask_Vcc':('bin', 104, 1, 0),
         'PageSelect':('bin', 127, 1, 0),

         'Identifier2':('bin', 128, 1, 0),
         'ExtIdentifier':('bin', 129, 1, 0),
         'Connector':('bin', 130, 1, 0),
         'CompCodes':('bin', 131, 8, 0),
         'Encoding':('bin', 139, 1, 0),
         'BitRate':('bin', 140, 1, 0),
         'ExtRateSelectComp':('bin', 141, 1, 0),
         'SupportedLengths':('bin', 142, 5, 0),
         'CopperLength':('bin', 146, 1, 0),
         'DeviceTech':('bin', 147, 1, 0),
         'VendName':('str', 148, 16, 0),
         'ExtTranCode':('bin', 164, 1, 0),
         'VenOUI':('bin', 165, 3, 0),
         'VenPN':('str', 168, 16, 0),
         'VenRev':('bin', 184, 2, 0),
         'WaveLength':('bin', 186, 2, 0),
         'MaxCaseTemp':('bin', 190, 1, 0),
         'CCBase':('bin', 191, 1, 0),
         'ExtOptions':('bin', 192, 4, 0),
         'VenSN': ('str', 196, 16, 0),
         'DateCode':('str', 212, 8, 0),
         'DiagMon':('bin', 220, 1, 0),
         'EnhOpt':('bin', 221, 1, 0),
         'CCExt':('bin', 223, 1, 0),
         'VenSpecEEPROM':('str', 224, 32, 0)      #no idea if a string or binary info
         ##The other pages don't seem useful to us at all.
    }


    def __init__(self, i2c, bus_name, gpio_prefix, gpio, address=0x50):
        """ Create a QSFP object.

        `control_bits` is a dictionary defining:  {control_bit_name: (io_expander_object, register_number, bit_number, default), ... }
        Valid control bit names are: 'ModPrsL', 'ResetL', 'IntL', 'ModSelL', 'LPMode', 'Led'
        """
        self._logger = logging.getLogger(__name__)
        self._logger.debug('%.32r: Instantiating QSFP+ object' % self)

        self._i2c = i2c
        self._bus_name = bus_name
        self._gpio_prefix = gpio_prefix
        self._address = address
        self._gpio = gpio

        self._qsfp_eeprom = EEPROM(self._i2c, bus_name=self._bus_name, address=self._address, address_width=8, write_page_size=256)

    def open(self):
        pass

    def close(self):
        pass

    def init(self, enable_i2c=False):
        """Initializes the QSFP module to a known state (enable it)"""
        self.reset()
        self.enable_i2c(enable_i2c)  # Note: Enable by default only of this is the only device at that address on the bus
        self.set_power_mode(0)  # Low power

    def set_control_bit(self, name, value, select=True):
        self._gpio.write(self._gpio_prefix + name, value, select=select)

    def get_control_bit(self, name, select=True):
        return self._gpio.read(self._gpio_prefix + name, select=select)

    def set_led(self, state):
        self.set_control_bit('Led', state)

    def get_led(self):
        return self.get_control_bit('Led')

    def reset(self, state=None):
        """
        Set the QSFP reset line state.

        The reset line is active if state=True. If state is omitted or is None
        the module is reset line is pulsed.
        """
        if state is None:
            self.set_control_bit('ResetL', 0)
            self.set_control_bit('ResetL', 1)
        else:
            self.set_control_bit('ResetL', not state)

    def status(self):
        """
        """
        return {bit_name: self.get_control_bit(bit_name) for bit_name in self._control_bits}

    def enable_i2c(self, enable):
        """
        Enable QSFP I2C  when enable = True
        """
        self.set_control_bit('ModSelL', not enable)

    def is_present(self):
        """ Indicate if the QSFP module is present by probing the ModPrsL
        line, which is grounded when the module is inserted.
        """
        return not self.get_control_bit('ModPrsL')

    def set_power_mode(self, state):
        """ 0 = low power, 1 = High power """
        self.set_control_bit('LPMode', not state)

    def __getattr__(self, name):
        if name in self.QSFP_EEPROM_MAP:
            (type, __, __, __) = self.QSFP_EEPROM_MAP[name]
            if isinstance(type, str):
                return self.read_str(name)
            else:
                return self.read(name)
        else:
            return self.get_control_bit(name)

    def get_power_mode(self):
        return getattr(self, 'LPMode', None)

    def write(self, addr, data, page=0, enable=True):

        if enable:
            self.enable_i2c(True)

        if addr in self.QSFP_EEPROM_MAP:
            (__, addr, __, page) = self.QSFP_EEPROM_MAP[addr]

        if page:
            self._qsfp_eeprom.write(addr=127, data=page, length=1)  # Writing to page select register

        self._qsfp_eeprom.write(addr, data) # Writing at specified address

        if page:
            self._qsfp_eeprom.write(addr=127, data=0, length=1)  # Putting page back to 0

        if enable:
            self.enable_i2c(False)


    def read(self, addr, length=1, page=0, enable=True):
        """
        Reads QSFP eeprom. Enables I2C, reads, Disables I2C. Returns the data as a string.
        """

        if enable:
            self.enable_i2c(True)

        if addr in self.QSFP_EEPROM_MAP:
            (__, addr, length, page) = self.QSFP_EEPROM_MAP[addr]

        if page:
            self._qsfp_eeprom.write(addr=127, data=page, length=1) #Writing to page select register

        data = self._qsfp_eeprom.read(addr=addr, length=length) #Reading at specified address

        if page:
            self._qsfp_eeprom.write(addr=127, data=0, length=1)  #Putting page back to 0

        if enable:
            self.enable_i2c(False)

        return data

    def read_str(self, addr=148, length=16, page=0):
        self.enable_i2c(True)
        # data = ''.join([chr(x) for x in self.read(addr, length, page, enable=False)])
        data = self.read(addr, length, page, enable=False)
        self.enable_i2c(False)
        return data.rstrip()

    def read_byte(self, addr, page=0, enable=True):
        """ Read 8-bit byte. """
        data = self.read(addr, length=1, page=page, enable=enable)
        return ord(data[0])

    def read_word(self, addr, page=0, type=np.uint16, enable=True):
        """ Read 16-bit word as an unsigned big endian. """
        data = self.read(addr, length=2, page=page, enable=enable)
        return type((ord(data[0]) << 8) + ord(data[1]))

    def get_temperature(self):
        word = self.read_word('MeasuredTemp', type=np.int16)
        return word / 256.

    def get_supply_voltage(self):
        return self.read_word('MeasuredSupV') * 100e-6

    def get_rx_power(self):
        """ Return the optical power (in Watts) received by each of the 4 channels"""
        return [self.read_word('RxPow%i' % chan) * 0.1e-6 for chan in [1 , 2, 3, 4]]

    def get_uid(self):
        if not self.is_present():
            return None
        return '%s_%s_SN%s' % (self.read_str('VendName'), self.read_str('VenPN'), self.read_str('VenSN'))

    def get_serial_number(self):
        if not self.is_present():
            return None
        return self.read_str('VenSN')

    def get_info(self):
        """
        Gets all qsfp info marked up in the qsfp eeprom map for each slot
        Data is returned as a dictionary
        History:
        141015 AJG & JF: created
        """
        if not self.is_present():
            return None

        self.enable_i2c(True)  # Needed for self._qsfp_eeprom.is_present() below

        tech_table = {
            0b0000: '850 nm VCSEL',
            0b0001: '1310 nm VCSEL',
            0b0010: '1550 nm VCSEL',
            0b0011: '1310 nm FP',
            0b0100: '1310 nm DFB',
            0b0101: '1550 nm DFB',
            0b0110: '1310 nm EML',
            0b0111: '1550 nm EML',
            0b1000: 'Others',
            0b1001: '1490 nm DFB',
            0b1010: 'Copper cable unequalized',
            0b1011: 'Copper cable passive equalized',
            0b1100: 'Copper cable, near and far end limiting active equalizers',
            0b1101: 'Copper cable, far end limiting active equalizers',
            0b1110: 'Copper cable, near end limiting active equalizers',
            0b1111: 'Copper cable, linear active equalizers',
            }

        connector_types = {
            0x00: 'Unknown or unspecified',
            0x01: 'SC',
            0x02: 'FC Style 1 copper connector',
            0x03: 'FC Style 2 copper connector',
            0x04: 'BNC/TNC',
            0x05: 'FC coax headers',
            0x06: 'Fiberjack',
            0x07: 'LC',
            0x08: 'MT-RJ',
            0x09: 'MU',
            0x0A: 'SG',
            0x0B: 'Optical Pigtail',
            0x0C: 'MPO',
            0x20: 'HSSDC II',
            0x21: 'Copper pigtail',
            0x22: 'RJ45',
            0x23: 'No separable connector'
            }

        identifier_table = {
            0x00: 'Unknown or unspecified',
            0x01: 'GBIC',
            0x02: 'Module/connector soldered to motherboard',
            0x03: 'SFP',
            0x04: '300 pin XBI',
            0x05: 'XENPAK',
            0x06: 'XFP',
            0x07: 'XFF',
            0x08: 'XFP-E',
            0x09: 'XPAK',
            0x0A: 'X2',
            0x0B: 'DWDM-SFP',
            0x0C: 'QSFP',
            0x0D: 'QSFP+',
            }
        print 'Hardware lines'
        print '--------------'
        print 'Module is Present: %s' % bool(self.is_present())
        print 'Module I2C is Responding: %s' % bool(self._qsfp_eeprom.is_present())
        print 'Module type: %s' % ['Low power', 'High power'][self.get_power_mode() or 0]
        print 'I2C info'
        print '--------------'
        print '   Module temperature: %0.1f C' % self.get_temperature()
        print '   Module supply voltage: %0.2f V' % self.get_supply_voltage()
        print '   Received optical power: %s' % ', '.join(['Ch%i=%0.3f mW' % (i+1, rx_pow/1e-3) for (i,rx_pow) in enumerate(self.get_rx_power())])
        print '   Manufacturer: %s' % self.read_str('VendName')
        print '   Model: %s Revision %s' % (self.read_str('VenPN'), self.read_str('VenRev'))
        print '   Serial Number: %s' % (self.read_str('VenSN'))
        print '   Cable length (if copper): %im' % self.read_byte('CopperLength')
        print '   Device Technology: %s' % tech_table[self.read_byte('DeviceTech') >> 4]
        print '   Connector type: %s, %s' % (identifier_table.get(self.read_byte('Identifier2', 'Unknown')),
                                             connector_types.get(self.read_byte('Connector', 'Unknown')))

        data = {}
        for (key, (datatype, addr, length, page)) in self.QSFP_EEPROM_MAP.items():
            if datatype == 'str': #String detected, converting to readable characters
                data[key] = self.read(addr=addr, length=length, page=page)
            else: #assuming binary
                data[key] = self.read(addr=addr, length=length, page=page)
        return data






































