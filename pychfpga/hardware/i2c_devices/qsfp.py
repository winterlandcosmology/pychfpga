"""qsfp.py module: Provides a class to read/write to a QSFP cable
"""

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

    def __init__(self, i2c, bus_name, gpio_prefix, gpio, address=0x50, parent=None):
        """ Create a QSFP object that allows read/write of the QSFP's hardware control lines and internal registers.

        Parameters:

            i2c (object): I2C interface object through which I2C communications will be performed. Must support the methods ``select_bus()`` and ``write_read()``.

            bus_name (int, tuple, dict): Parameter that is passed to i2c.select_bus() to enable access to this device

            gpio_prefix (str): String that is prefixed to the GPIO names to access GPIOs provided by the `gpio` object

            gpio (:class:`~pychfpga.hardware.i2c_devices.gpio.GPIO`): GPIO-class object
                that provides read() and write() methods to access the QSFP hardware
                control lines.
                The object must define the following GPIOs:

                    - <prefix>ModPrsL
                    - <prefix>ResetL
                    - <prefix>IntL
                    - <prefix>ModSelL
                    - <prefix>LPMode
                    - <prefix>Led: Optional. Used in set/get_led only.

            address (int): 7-bit I2C address of the QSFP

            parent: Used in repr() of this QSFP object to clarify to which object this one is dependent from

        Valid control bit names are: 'ModPrsL', 'ResetL', 'IntL', 'ModSelL', 'LPMode', 'Led'
        """
        self._logger = logging.getLogger(__name__)

        self._i2c = i2c
        self._bus_name = bus_name
        self._gpio_prefix = gpio_prefix
        self._address = address
        self._gpio = gpio
        self.parent = parent

        self._logger.debug(f'{self!r}: Instantiating QSFP+ object')

        self._qsfp_eeprom = EEPROM(
            self._i2c, bus_name=self._bus_name,
            address=self._address,
            address_width=8,
            write_page_size=256)

    def __repr__(self):
        """Return a compact string representation.

        Returns:
            Any: Returned value.
        """
        return f"{self.parent!r}.{self.__class__.__name__}"

    def open(self):
        """Open or acquire access to the device.

        Returns:
            None: No value is returned.
        """
        pass

    def close(self):
        """Close or release access to the device.

        Returns:
            None: No value is returned.
        """
        pass

    def init(self, enable_i2c=False):
        """Initializes the QSFP module to a known state (enable it)

        Args:
            enable_i2c (Any): Parameter value.

        Returns:
            None: No value is returned.
        """
        self.reset()
        # Note: Enable by default only of this is the only device at that address on the bus
        self.enable_i2c(enable_i2c)
        self.set_power_mode(0)  # Low power

    def set_control_bit(self, name, value, select=True):
        """Set control bit configuration.

        Args:
            name (Any): Parameter value.
            value (Any): Parameter value.
            select (Any): Parameter value.

        Returns:
            None: No value is returned.
        """
        self._gpio.write(self._gpio_prefix + name, value, select=select)

    def get_control_bit(self, name, select=True):
        """Return control bit value.

        Args:
            name (Any): Parameter value.
            select (Any): Parameter value.

        Returns:
            Any: Returned value.
        """
        return self._gpio.read(self._gpio_prefix + name, select=select)

    def set_led(self, state):
        """Set led configuration.

        Args:
            state (Any): Parameter value.

        Returns:
            None: No value is returned.
        """
        self.set_control_bit('Led', state)

    def get_led(self):
        """Return led value.

        Returns:
            Any: Returned value.
        """
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
        return {bit_name: self.get_control_bit(bit_name) for bit_name in ('ModPrsL','LPMode','ResetL','IntL')}

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
        """ 0 = low power, 1 = High power.

        Drives the LPMode hardware pin and, when enabling high power, also
        programs byte 93 (PowerSet) so that modules declaring Power Class 5-8
        (>3.5 W) are actually allowed to draw their full rated power. Per
        SFF-8636 the LPMode pin alone only gates Power Classes 1-4; classes
        5-7 additionally require the "High Power Class Enable" bit and
        class 8 requires the "Class 8 Enable" bit.
        """
        if state:
            try:
                self.enable_high_power_class()
            except Exception:
                self._logger.exception(
                    '%r: Failed to program PowerSet (byte 93) for high power '
                    'class; module may stay clamped to Power Class 1.', self)
        self.set_control_bit('LPMode', not state)

    def get_power_class(self):
        """ Return the QSFP module's declared Power Class (1..8) based on
        byte 129 (ExtIdentifier) per SFF-8636.

        Returns None if the module is not present or cannot be read.
        """
        if not self.is_present():
            return None
        try:
            ext = self.read_byte('ExtIdentifier')
        except Exception:
            self._logger.exception('%r: Failed to read ExtIdentifier', self)
            return None
        # bit 5: Power Class 8 implemented (max power encoded in byte 107)
        if ext & (1 << 5):
            return 8
        # bits [1:0]: Power Class 5-7 extension (00 => use bits [7:6])
        ext_low = ext & 0b11
        if ext_low:
            return 4 + ext_low  # 01->5, 10->6, 11->7
        # bits [7:6]: Power Class 1-4 (00->1 .. 11->4)
        return ((ext >> 6) & 0b11) + 1

    def enable_high_power_class(self):
        """ Program byte 93 (PowerSet) according to the module's declared
        Power Class so that classes >3.5 W are allowed to power up.

        - Always sets Power_override (bit 0) so software controls the class.
        - Clears Power_set (bit 1) so the module is not forced to low power.
        - Sets High Power Class Enable (bit 2) for Power Class 5-8.
        - Sets Class 8 Enable (bit 3) for Power Class 8.

        Returns the value written to byte 93, or None if no module is present.
        """
        power_class = self.get_power_class()
        if power_class is None:
            return None
        value = 0x01  # bit 0: Power_override
        if power_class >= 5:
            value |= 0x04  # bit 2: High Power Class Enable (classes 5-7, also required for 8)
        if power_class >= 8:
            value |= 0x08  # bit 3: Class 8 Enable
        self._logger.info(
            '%r: Module declares Power Class %d; writing PowerSet=0x%02X',
            self, power_class, value)
        self.write('PowerSet', bytes([value]))
        return value

    def __getattr__(self, name):
        """Resolve dynamic attributes through device accessors.

        Args:
            name (Any): Parameter value.

        Returns:
            Any: Returned value.
        """
        if name in self.QSFP_EEPROM_MAP:
            (type, __, __, __) = self.QSFP_EEPROM_MAP[name]
            if isinstance(type, str):
                return self.read_str(name)
            else:
                return self.read(name)
        else:
            return self.get_control_bit(name)

    def get_power_mode(self):
        """Return power mode value.

        Returns:
            Any: Returned value.
        """
        return getattr(self, 'LPMode', None)

    def write(self, addr, data, page=0, enable=True):

        """Write data to the device.

        Args:
            addr (Any): Parameter value.
            data (Any): Parameter value.
            page (Any): Parameter value.
            enable (Any): Parameter value.

        Returns:
            None: No value is returned.
        """
        if enable:
            self.enable_i2c(True)

        if addr in self.QSFP_EEPROM_MAP:
            (__, addr, __, page) = self.QSFP_EEPROM_MAP[addr]

        try:
            if page:
                self._qsfp_eeprom.write(addr=127, data=page)  # Writing to page select register
            self._qsfp_eeprom.write(addr, data)  # Writing at specified address
        finally:
            if page:
                self._qsfp_eeprom.write(addr=127, data=0)  # Putting page back to 0
            if enable:
                self.enable_i2c(False)

    def read(self, addr, length=1, page=0, enable=True):
        """
        Reads QSFP eeprom. Enables I2C, reads, Disables I2C. Returns the data as a string.

        Parameters:

            addr (int): memory address

            length (int): number of bytes to read

            page (int): data page to read (affects only address 128 and above)

            enable (bool): Enable the I2C port and I2C switch chain

        Returns:

            bytes: bytestring containing the requested bytes.

        """

        if enable:
            self.enable_i2c(True)

        if addr in self.QSFP_EEPROM_MAP:
            (__, addr, length, page) = self.QSFP_EEPROM_MAP[addr]

        try:
            if page:
                self._qsfp_eeprom.write(addr=127, data=page)  # Writing to page select register
            data = self._qsfp_eeprom.read(addr=addr, length=length)  # Reading at specified address
        finally:
            if page:
                self._qsfp_eeprom.write(addr=127, data=0)  # Putting page back to 0
            if enable:
                self.enable_i2c(False)

        return data

    # SFF-8636 page 03h signal-integrity controls, one 4-bit value per channel 1-4. Volatile: lost at module power-down.
    def _get_nibbles(self, addr):
        b = self.read(addr, 2, page=3)
        return [b[0] >> 4, b[0] & 15, b[1] >> 4, b[1] & 15]

    def _set_nibbles(self, addr, values, allowed):
        ch = [values] * 4 if isinstance(values, int) else list(values)
        if len(ch) != 4 or not all(c in allowed for c in ch):
            raise ValueError(f'{self!r}: values {ch} not in {list(allowed)} (one value, or one per channel 1-4)')
        self.write(addr, [ch[0] << 4 | ch[1], ch[2] << 4 | ch[3]], page=3)

    def get_tx_input_eq(self):
        """TX input equalization per channel 1-4, in dB (page 03h bytes 234-235)."""
        return self._get_nibbles(234)

    def set_tx_input_eq(self, db):
        """Set TX input equalization: one value for all channels, or a list for channels 1-4. 0..max dB (page 03h byte 224[7:4])."""
        self._set_nibbles(234, db, range((self.read(224, page=3)[0] >> 4) + 1))

    def get_rx_output_emphasis(self):
        """RX output emphasis per channel 1-4, in dB (page 03h bytes 236-237)."""
        return self._get_nibbles(236)

    def set_rx_output_emphasis(self, db):
        """Set RX output emphasis: one value for all channels, or a list for channels 1-4. 0..max dB (page 03h byte 224[3:0])."""
        self._set_nibbles(236, db, range((self.read(224, page=3)[0] & 15) + 1))

    def get_rx_output_amplitude(self):
        """RX output amplitude code per channel 1-4 (page 03h bytes 238-239; 0 = 100-400 mV, 1 = 300-600 mV, ...)."""
        return self._get_nibbles(238)

    def set_rx_output_amplitude(self, code):
        """Set RX output amplitude code: one value for all channels, or a list for channels 1-4. Supported codes: page 03h byte 225[3:0]."""
        sup = self.read(225, page=3)[0]
        self._set_nibbles(238, code, [c for c in range(4) if sup >> c & 1])

    def read_str(self, addr=148, length=16, page=0):
        """Read and decode a fixed-length text field.

        Args:
            addr (Any): Parameter value.
            length (Any): Parameter value.
            page (Any): Parameter value.

        Returns:
            Any: Returned value.
        """
        self.enable_i2c(True)
        # data = ''.join([chr(x) for x in self.read(addr, length, page, enable=False)])
        data = self.read(addr, length, page, enable=False)
        self.enable_i2c(False)
        return data.rstrip()

    def read_byte(self, addr, page=0, enable=True):
        """ Read 8-bit byte. 

        Args:
            addr (Any): Parameter value.
            page (Any): Parameter value.
            enable (Any): Parameter value.

        Returns:
            Any: Returned value.
        """
        data = self.read(addr, length=1, page=page, enable=enable)
        return data[0]

    def read_word(self, addr, page=0, type=np.uint16, enable=True):
        """ Read 16-bit word as an unsigned big endian. 

        Args:
            addr (Any): Parameter value.
            page (Any): Parameter value.
            type (Any): Parameter value.
            enable (Any): Parameter value.

        Returns:
            Any: Returned value.
        """
        data = self.read(addr, length=2, page=page, enable=enable)
        return (data[0] << 8) + data[1]

    def get_temperature(self):
        """Return temperature value.

        Returns:
            Any: Returned value.
        """
        word = self.read_word('MeasuredTemp', type=np.int16)
        return word / 256.

    def get_supply_voltage(self):
        """Return supply voltage value.

        Returns:
            Any: Returned value.
        """
        return self.read_word('MeasuredSupV') * 100e-6

    def get_rx_power(self):
        """ Return the optical power (in Watts) received by each of the 4 channels

        Returns:
            Any: Returned value.
        """
        return [self.read_word(f'RxPow{chan}') * 0.1e-6 for chan in [1, 2, 3, 4]]

    def get_uid(self):
        """Return uid value.

        Returns:
            Any: Returned value.
        """
        if not self.is_present():
            return None
        return '{}_{}_SN{}'.format(self.read_str('VendName'), self.read_str('VenPN'), self.read_str('VenSN'))

    def get_serial_number(self):
        """Return serial number value.

        Returns:
            Any: Returned value.
        """
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
        print('Hardware lines')
        print('--------------')
        print(f'Module is Present: {bool(self.is_present())}')
        print(f'Module I2C is Responding: {bool(self._qsfp_eeprom.is_present())}')
        print('Module type: {}'.format(['Low power', 'High power'][self.get_power_mode() or 0]))
        print('I2C info')
        print('--------------')
        print(f'   Module temperature: {self.get_temperature():0.1f} C')
        print(f'   Module supply voltage: {self.get_supply_voltage():0.2f} V')
        rx_powers = self.get_rx_power()
        print(
            '   Received optical power: '
            + ', '.join(
                f'Ch{i + 1}={rx_pow / 1e-3:0.3f} mW' for i, rx_pow in enumerate(rx_powers)
            )
        )
        print('   Manufacturer: {}'.format(self.read_str('VendName')))
        print('   Model: {} Revision {}'.format(self.read_str('VenPN'), self.read_str('VenRev')))
        print('   Serial Number: {}'.format(self.read_str('VenSN')))
        print(f'   Cable length (if copper): {self.read_byte("CopperLength")}m')
        print('   Device Technology: {}'.format(tech_table[self.read_byte('DeviceTech') >> 4]))
        print('   Connector type: {}, {}'.format(identifier_table.get(self.read_byte('Identifier2', 'Unknown')),
                                             connector_types.get(self.read_byte('Connector', 'Unknown'))))

        data = {}
        for (key, (datatype, addr, length, page)) in list(self.QSFP_EEPROM_MAP.items()):
            if datatype == 'str':  # String detected, converting to readable characters
                data[key] = self.read(addr=addr, length=length, page=page)
            else:  # assuming binary
                data[key] = self.read(addr=addr, length=length, page=page)
        return data


