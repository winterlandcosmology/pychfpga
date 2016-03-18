""" Basic handler for chFPGA firmware.
"""

import logging
from datetime import datetime, timedelta
from calendar import timegm
import time
import struct
import base64

import socket

from ..icecore import IceBoardPlusHandler
from ..icecore import tuber  # Used to get TuberRemoteError
from ..icecore import Ccoll


from .. import I2C as i2c
from .. import GPIO as fpga_gpio

from . import icecrate_ext  # this module is not referenced here, but loading it registers the handler with IceCrate.

# Import IceBoard hardware handlers
# from lib import tmp100  # I2C Temperature sensor
from lib import pca9575  # I2C 16-bit IO Expander
from lib import tca9548a  # I2C switch
# from lib import ina230  # I2C Voltage and current monitor
from lib import eeprom
from lib import qsfp
from lib import gpio

# import icebox # don't use from .. import ... because of circular import problems

class IceBoardExtHandler(IceBoardPlusHandler):
    """ Provides basic access to CHIME-specific basic IceBoard firmware and
    hardware resources.

    Differences with the standard Iceboard handler:

    - A direct FPGA Ethernet-based 8-bit Memory Map Interface is provided
    - Access to the IceBoard hardware is provided through the FPGA I2C
      interface. Most of the equivalent methods are provided by the ARM, but
      missing methods are offered through this FPGA interface.
    - Acces to the backplane hardware. This is used by the IceCrate handler to
      provide backplane services.
    - The standard ARM mezzanine identification methods are intecepted to
      allow support for non-IPMI McGill ADC mezzanine boards.

     Python-based application-specific FPGA firmware and hardware handler are
     meant to be derived from this class.
    """

    _FPGA_CONTROL_BASE_PORT = 41000
    _BROADCAST_BASE_PORT = 41000

    _SYSTEM_BASE_ADDR      = 0x00000 # This is always at zero so we can gather info from the FPGA before we know the number of antennas etc.
    _SYSTEM_GPIO_BASE_ADDR = _SYSTEM_BASE_ADDR + 0x00000
    _SYSTEM_I2C_BASE_ADDR  = _SYSTEM_BASE_ADDR + 0x05000

    # Match those with what is used by Module
    _CONTROL_BASE_ADDR = 0x000000
    _STATUS_BASE_ADDR  = 0x080000
    _RAM_BASE_ADDR     = 0x100000

    _CHFPGA_COOKIE = 0x42 # Expected cookie value for chFPGA, both on the SPI and UDP MMI

    # GPIO Register addresses
    _GPIO_COOKIE_REG         = _STATUS_BASE_ADDR + _SYSTEM_GPIO_BASE_ADDR  # Register address of the firmware cookie
    _FPGA_TIMESTAMP_ADDR     = _STATUS_BASE_ADDR + _SYSTEM_GPIO_BASE_ADDR + 7
    _FPGA_SERIAL_NUMBER_ADDR = _STATUS_BASE_ADDR + _SYSTEM_GPIO_BASE_ADDR + 12
    _FPGA_IP_SETUP_BASE_ADDR = _CONTROL_BASE_ADDR + _SYSTEM_GPIO_BASE_ADDR + 13 # (13-18): target MAC, (19-22): target IP, (23-24): target_base_port, (25-32) = Target FPGA serial, (33): bit 7 = trigger, bits 3:2: mac source select, 1:0: broadcast group
    # _GPIO_IPCONFIG_REG       = _CONTROL_BASE_ADDR + _SYSTEM_GPIO_BASE_ADDR + 0x08D # Register address of the first byte of the IP config word

    # SPI Application registers
    _FPGA_MAC_ADDR_LSW_ADDR         = 4 * 7
    _FPGA_MAC_ADDR_MSW_IP_PORT_ADDR = 4 * 8
    _FPGA_IP_ADDR_ADDR              = 4 * 9
    _IRIGB_SAMPLE0_ADDR             = 4 * 10
    _IRIGB_SAMPLE1_ADDR             = 4 * 11
    _IRIGB_SAMPLE2_ADDR             = 4 * 12

    _IRIGB_TARGET0_ADDR             = 4 * 13
    _IRIGB_TARGET1_ADDR             = 4 * 14
    _IRIGB_TARGET2_ADDR             = 4 * 15
    _IRIGB_EVENT_CTR_ADDR           = 4 * 16
    _BP_BUCK_SYNC_ADDR              = 4 * 17
    _BP_BUCK_SYNC_ADDR2             = 4 * 18
    _SFP_STATUS_ADDR                = 4 * 19
    _REMOTE_IP_PORT_ADDR            = 4 * 20
    _IRIGB_REFCLK_SAMPLE            = 4 * 21

    _XILINX_OUI = 0x000A35

    # ---------------------------------------
    # Instance attributes
    # ---------------------------------------

    fpga_ip_addr = None
    interface_ip_addr = None  # Is automatically detected by opening a TCP connection to the ARM
    zero_target_irigb_year_and_day = False # If True, target IRIGB yead and day will always be written as zero binary values to me compatible with the IRIG-B generator

    class AutoOpen(object):
        """ Automatcally call the specified 'open' method that creates an
        attribute if the attribute is accessed and is not yet defined.
        """
        def __init__(self, open_method, attribute_name):
            self._open_method = open_method
            self._attribute_name = attribute_name

        def __get__(self, object, class_):
            # print 'Auto-open %s' % self._attribute_name
            getattr(object, self._open_method)()  # Execute the open method
            return getattr(object, self._attribute_name)  # Get target object

    mmi       = AutoOpen('open_core', 'mmi')
    i2c       = AutoOpen('open_core', 'i2c')
    core_gpio = AutoOpen('open_core', 'core_gpio')
    core_i2c  = AutoOpen('open_core', 'core_i2c')
    hw        = AutoOpen('open_hw', 'hw')
    # bp        = AutoOpen('open_bp', 'bp')

    def __init__(self, **kwargs):
        """
        Creates an Iceboard that is accessed through the networking parameters
        specified in the database.

        The created object does not have any fpga or hardware handlers yet.
        Those will be created when the Iceboard is opened.
        """
        super(IceBoardExtHandler, self).__init__(**kwargs)
        self.logger = logging.getLogger(__name__)
        self._mezzanine_ipmi_cache = {1: None, 2: None}
        self._is_core_open = None
        self._is_hw_open = None
        # self._is_bp_open = None
        self._is_open = None

    # ------------------------------------------------------------------
    # CHIME-specific MMI interface
    # ------------------------------------------------------------------
    # Uses direct Ethernet link to the FPGA SFP port to read and write
    # firmware registers using UDP packets.
    #
    # As the CHIME Firmware pre-dates the ARM-to-FPGA communication link, we
    # generally do not use that (slower) link except for basic core functions.
    # ------------------------------------------------------------------

    def mmi_read(self, *args, **kwargs):
        """ Read bytes at specified byte address through direct access to the
        FPGA."""
        return self.mmi.read(*args, **kwargs)

    def mmi_write(self, *args, **kwargs):
        """ Write bytes at specified byte address through direct access to the
        FPGA."""
        self.mmi.write(*args, **kwargs)

    # def spi_mmi_read(self, addr): """ Read a single 32-bit word at specified
    #     byte address through the ARM<->FPGA SPI link.""" return
    #     self._fpga_spi_peek(addr)

    # def spi_mmi_write(self, addr, value): """ Write a single 32-bit word at
    #     specified byte address through the ARM<->FPGA SPI link."""
    #     self._fpga_spi_poke(addr, value)

    def open_core(self):
        """
        Establishes the connection with the hardware and firmware on the
        IceBoard and create all appropriate handling classes.
        """
        # print '%r: opening core' % self

        if not self.is_fpga_programmed():
            raise RuntimeError(
                "%r: The FPGA is not programmed with a bitstream . "
                'Direct UDP link to FPGA cannot be established ' % (self))

        # Check the FPGA firmware cookie obtained through the ARM SPI interface to the FPGA
        cookie = self.get_fpga_application_cookie()
        if cookie != self._CHFPGA_COOKIE:
            raise RuntimeError(
                '%r: The firmware currently configured on the FPGA is not chFPGA (got cookie 0x%04X instead of 0x%04X). '
                'Direct UDP link to FPGA and other chFPGA-specific methods and '
                'resources are not available.' % (self, cookie, self._CHFPGA_COOKIE))

        # self.fpga_serial_number = self.get_fpga_serial_number()  # Get SN from the SPI link (slow)

        # Get the address of the interface through which we can access the
        # board over UDP by opening a TCP socket to the ARM and inspeting the
        # interface that was used. This assumes that both the ARM and FPGAs
        # are accessed through the same interface.
        if self.hostname:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.connect((self.hostname, 80))
            (self.interface_ip_addr, _) = s.getsockname()
            s.close()
        else:
            self.interface_ip_addr = None

        # Compute the IP address to use for the FPGA UDP interface For now, we
        # replace a.b.c.d by a.b.3.d. We need to find a more generic mechanism
        # for this (like obtaining another IP from the DHCP server)
        ip_packed = socket.inet_aton(self._get_arm_ip())  #
        ip_packed = ip_packed[:2] + chr(3) + ip_packed[3]
        fpga_ip_addr = socket.inet_ntoa(ip_packed)

        # Set-up the FPGA networking parameters using the ARM-SPI link to the FPGA
        self.set_fpga_control_networking_parameters(fpga_ip_addr=fpga_ip_addr)


        # if the FPGA handler instance was not created, check if one exists
        # create it
        if self.is_core_open():
            self.logger.warning(
                '%.32r: Attempting to open core while it is already opened. '
                'Ignoring.' % (self))
            return

        # -------------------------------------------------------------------------
        # Open the UDP MMI interface
        # -------------------------------------------------------------------------
        from .lib import fpga_mmi
        self.mmi = fpga_mmi.FpgaMmi(
            ip_addr=self.fpga_ip_addr,
            port_number=self.fpga_port_number,
            interface_ip_addr=self.interface_ip_addr)
        self.mmi.open()
        self.local_port_number = self.mmi.local_port_number

        # -------------------------------------------------------------------------
        # Open FPGA's GPIO module interface
        # -------------------------------------------------------------------------
        self.core_gpio = fpga_gpio.GPIO_base(self, self._SYSTEM_GPIO_BASE_ADDR)

        # -------------------------------------------------------------------------
        # Check if we can communicate with the FPGA over the direct Ethernet
        # link by reading the UDP MMI cookie (not the SPI one) and check if
        # the cookie correspond to the chFPGA firmware.
        # -------------------------------------------------------------------------
        self.logger.info("%.32r: Attempting to communicate with the FPGA over direct Ethernet link" % self)
        try:
            cookie = self.get_fpga_firmware_cookie(resync=True)  # Read the firmware version cookie from the GPIO subsystem (this is provided by the FPGA core firmware which is always present on all versions of the FPGA)
        except IOError as e:
            error_message = "%.32r: Unable to communicate with the FPGA at address %s:%i due to the following exception: %s" % (self, self.fpga_ip_addr, self.fpga_port_number, repr(e))
            self.close()
            self.logger.error(error_message)
            raise

        if cookie != self._CHFPGA_COOKIE:
            error_message = '%.32r: The firmware at %s:%i is not chFPGA. The magic cookie returned by the FPGA is 0x%02X, whereas we expected 0x%02X' % (self, self.fpga_ip_addr, self.fpga_port_number, cookie, self._CHFPGA_COOKIE)
            self.logger.error(error_message)
            self.close()
            raise RuntimeError(error_message)

        self.logger.info("%.32r: Established a UDP/Ethernet connection with the FPGA" % self)

        # -------------------------------------------------------------------------
        # Open FPGA's I2C interfaces
        # -------------------------------------------------------------------------
        self.core_i2c = i2c.I2C_base(self, self._SYSTEM_I2C_BASE_ADDR)
        # Create standardized I2C interface
        self.i2c = I2CInterface(
            self.fpga_i2c_write_read,
            self.fpga_i2c_set_port,
            IceBoardHardware.FPGA_I2C_BUS_LIST,
            IceBoardHardware._FPGA_I2C_SWITCH_ADDR)

        self._is_core_open = True

    def close_core(self):
        # self.close_bp()
        self.close_hw()

        if self._is_core_open:
            self.mmi.close()
            # Make the AutoOpen data descriptior visible again
            del self.mmi, self.core_i2c, self.core_gpio, self.i2c
            self._is_core_open = False

    def is_core_open(self):
        # return self.iceboard_pk in type(self)._active_instances
        return bool(self._is_core_open)

    def open_hw(self):
        # Open IceBoard hardware manager object
        self.hw = IceBoardHardware(iceboard=self)
        self._is_hw_open = True
        self.hw.open()

    def close_hw(self):
        if self._is_hw_open:
            self.hw.close()
            del self.hw
            self._is_hw_open = False

    def open(self):

        self.logger.info('%.32r: open() is called' % (self))
        self.open_core()

        self.hw.init()
        self.hw.set_led('GP_LED2', 1)  # Hardware link is on
        self.hw.set_led('GP_LED1', 0)  # Full FPGA firmware is not yet on

        self._is_open = True

    def close(self):
        self.close_core()
        self._is_open = False

    def is_open(self):
        return self._is_open

    def ping_fpga(self, timeout=0.3):
        # Open the core right now if needed so we don't mask IOError exceptions this could generate
        if not self.is_core_open():
            self.open_core()
        try:
            self.mmi_read(self._GPIO_COOKIE_REG, timeout=timeout)
            return True
        except IOError:
            return False

    def check_command_count(self, reset=False):
        """ Returns a boolen that checks if the number of command and replies sent to/from the FPGA
        by the Python memory mapped interface matches the counts tallied by the FPGA
        firmware.

        Since both ends drop packets that have invalid CRCs, this should
        detect any transmission error in addition to UDP packets dropped by
        the switches, routers or the networking stack on the host computer.

        The packet counts are modulo 256, so a loss of a multiple of 256 packets will not be detected.

        If 'reset' is True, the MMI counters are reset to the FPGA values in order to clear the error on future checks.
        """

        (cmd, rply) = self.core_gpio.get_command_count()
        valid = (cmd == self.mmi.send_counter & 0xFF) and (rply == self.mmi.recv_counter & 0xFF)
        if reset:
            self.mmi.send_counter = cmd
            self.mmi.recv_counter = rply
        return valid

    def set_fpga_control_networking_parameters(self, fpga_mac_addr=None, fpga_ip_addr=None, fpga_port_number=_FPGA_CONTROL_BASE_PORT, local_port=0):
        """ Set the FPGA UDP networking parameters using the ARM SPI MMI link.
        'fpga_ip_addr' is a mandatory string in the format of 'a.b.c.d', where a,b,c and d are decimal numbers.

        'fpga_port_number' is the port to which command packets are sent to on the FPGA. This port is 41000 by default.

        'fpga_mac_addr' is a string representing the MAC address of the FPGA in the format 'xx:xx:xx:xx:xx:xx, where
        'xx' is a hex number'. If fpga_mac_addr is None, an arbitrary MAC address is
        created using the IP address to ensure its uniqueness.

        'local port' is the port number to which command replies are sent
        back. If 'local_port' is 0 (default), the command reply packets will
        be sent back to source port number of the last command packet received
        by the FPGA (which presumably is the command that sollicited the
        reply) .
        """

        ip_packed = socket.inet_aton(fpga_ip_addr)  #

        # Compute a MAC address for the FPGA
        # mac = socket.inet_aton(self._get_arm_mac())  #
        if fpga_mac_addr is None:
            mac_packed = struct.pack('>H4s', 0x1234, ip_packed)
            fpga_mac_addr = ':'.join(['%02X' % ord(c) for c in mac_packed])
        else:
            mac_packed = [chr(int(s, 16)) for s in fpga_mac_addr.split(':')]

        self.fpga_mac_addr = fpga_mac_addr
        self.fpga_port_number = fpga_port_number
        self.fpga_ip_addr = fpga_ip_addr

        # Set the FPGA Networking parameters over the ARM-FPGA SPI interface
        self.fpga_mmi_write(self._FPGA_MAC_ADDR_LSW_ADDR, struct.unpack('>I', mac_packed[2:6])[0])
        self.fpga_mmi_write(self._FPGA_MAC_ADDR_MSW_IP_PORT_ADDR, (struct.unpack('>H', mac_packed[0:2])[0] << 16) | fpga_port_number)
        self.fpga_mmi_write(self._FPGA_IP_ADDR_ADDR, struct.unpack('>I', ip_packed)[0])

    def set_local_data_port_number(self, port):
        """ Sets the port number to which the FPGA is sending its captured data stream on the control network.

        If port==0, the data is sent to the control port number + 1.
        """
        word = self.fpga_mmi_read(self._REMOTE_IP_PORT_ADDR)
        self.fpga_mmi_write(self._REMOTE_IP_PORT_ADDR, (word & 0xFFFF) | (port << 16))

    def get_local_data_port_number(self):
        """ Return the port number to which the FPGA is sending its captured data stream on the control network.
        """
        return self.fpga_mmi_read(self._REMOTE_IP_PORT_ADDR) >> 16


    def get_fpga_firmware_cookie(self, resync=False):
        """
        Reads the FPGA over the UDP link and returns the cookie that
        identifies the firmware. This method can be called before any FPGA
        modules are instatiated.

        If ``resync`` is True, the read command will reset the command
        sequence number to the value known by the FPGA. This should be is used
        by the first command sent to the FPGA to reset the communication link.
        """
        return self.mmi_read(self._GPIO_COOKIE_REG, resync=resync) & 0x7F

    def get_fpga_firmware_version(self):
        """
        Returns the firmware revion currenting running on the FPGA (which si
        the date and time of bitstream generation)
        """
        return self.core_gpio.get_bitstream_date()

    def fpga_i2c_write_read(self, *args, **kwargs):
        """
        Performs I2C read, write or SMB-compatible combined write/read
        operations (SMB or its subset PMB require the register address to be
        written and then data to be read immediately after an I2C restart. It
        cannot be done in separate write and read  operations).

        Writes up to 3 bytes to the addressed I2C device and/or reads up to 4
        bytes from that device after a restart.

        See the FPGA I2C module for detailed method description.
        """
        return self.core_i2c.write_read(*args, **kwargs)

    def fpga_i2c_set_port(self, *args, **kwargs):
        """
        Sets the FPGA hardware port over which the i2c communications will be
        made after this call.

        This selects the FPGA pins over which the communications is done,
        *not* the bus selection done by an I2C switch.
        """
        return self.core_i2c.set_port(*args, **kwargs)

    def _mezzanine_eeprom_read(self, mezzanine):
        """ Reads the EEPROM on the specified mezzanine using the FPGA if the ARM firmware does not provide the functionnality.

        This method is a 'temporary' patch that overrides the same method in
        IceBoardPlus to allow proper operations of systems with old ARM
        firmware connected to MGADC08 mezzanines that use a non-IPMI-compliant
        standard.

        If the ARM provides its own raw EEPROM read method, the full EEPROM
        contents is returned using that method.

        Otherwise, the data is read (slowly) through the FPGA I2C interface.
        This will work only if the FPGA is programmed and initialized.

        To mitigate the slow access speed of the FPGA, this method will only
        read EEPROMs formatted in the McGill format and will stop reading when
        the the '}' or 0xFF are found. Otherwise, this method will return
        None, which will signal to the upper software that it can attempt to
        reading the IPMI standard data decoded by the ARM.
                """
        try:
            return base64.decodestring(self._mezzanine_eeprom_read_base64(mezzanine))
        except tuber.TuberRemoteError:
            self.logger.debug("%.32r: Cannot read the Mezzanine %i EEPROM through the ARM's _mezzanine_eeprom_read_base64() method. Attempting to read the Mezzanine EEPROM through the FPGA." % (self, mezzanine))

        eeprom_data = self.hw.read_mezzanine_eeprom(mezzanine, 0, 1)
        if ord(eeprom_data[0]) == 0x0d:  # if this is McGill format
            self.logger.debug("%.32r: EEPROM in Mezzanine %i is McGill format. The FPGA will be reading only bytes until the terminator character. " % (self, mezzanine))
            # Read the eeprom block by block until we detect the end of the
            # dictionary
            block_size = 32
            string = ''
            for i in range(512 / block_size):  # read 32 blocks of 16 bytes
                data_block = self.hw.read_mezzanine_eeprom(
                    mezzanine, addr=i*block_size, length=block_size, retry=3)
                string += data_block
                if ('}' in data_block) or (chr(255) in data_block):
                    break
            return string
        else:  # If not McGill format,
            self.logger.debug("%.32r: EEPROM in Mezzanine %i is not McGill format. The FPGA will *NOT* read the EEPROM contetnt " % (self, mezzanine))
            return None

    # def _get_mezzanine_mcgill_ipmi(self, mezzanine, retry=3, use_cache=False):
    #     """ Returns the IMPI data for the mezzanine located on slot
    #     'mezzanine' (1 or 2). Returns None if no mezzanine is present.

    #     This method overrides the ARM method of the same name so we can
    #     correctly read MGADC08 mezzanines which have a non-standard EEPROM
    #     data structure.

    #     if ``use_cache = False``, the method also does not use the IPMI data cached by the ARM but
    #     rather re-reads the EEPROM. This is useful in case the mezzanine has
    #     been changed without power cycling the IceBoard (which is typically
    #     done during Quality Control runs).

    #     Todo:
    #         - Should be made asynchronous
    #     """
    #     if not self.is_mezzanine_present(mezzanine):  # Check mezzanine presence using the FMC PRSNT line.
    #         return None

    #     if use_cache:
    #         try:
    #             # *** JFC: broken now. fixme
    #             return tuber.TuberObject.__getattr__(self,'_get_mezzanine_ipmi')(mezzanine)  # Try to get the ipmi data from tuber
    #         except tuber.TuberRemoteError:
    #             pass
    #         # Tuber has nothing, so see if we have a Python-cached version of the IPMI data
    #         if self._mezzanine_ipmi_cache[mezzanine]:
    #             return self._mezzanine_ipmi_cache[mezzanine]

    #     self._mezzanine_ipmi_cache[mezzanine] = None

    #     # We can't use the cache, or the ARM cannot understand the EEPROM format, so read and decode the IPMI data ourselves
    #     eeprom_data = base64.decodestring(self._mezzanine_eeprom_read_base64(mezzanine))



    #     self._mezzanine_ipmi_cache[mezzanine] = fru
    #     return fru

    def _get_mezzanine_type(self, mezzanine):
        """ Returns the type of mezzanine located on slot 'mezzanine' (1 or 2).
        Returns None if no mezzanine is present.

        This method overrides the ARM method of the same name so we can call
        our own _get_mezzanine_ipmi() which can correctly read non-standard
        MGADC08 EEPROM data structure.
        """
        ipmi = self._get_mezzanine_mcgill_ipmi(mezzanine)
        if ipmi and hasattr(ipmi,'product') and hasattr(ipmi.product, 'part_number'):
            return ipmi.product.part_number
        else:
            return None

    # ---------------------------------------------------------
    # IRIG-B time support methods
    # ---------------------------------------------------------

    class _IrigTimestamp(object):
        pass

    _IRIGB_SOURCE_TABLE = {
        'bp_trig': 0,
        'bp_time': 1,
        'irigb_gen': 2,
        'bp_gpio_int': 3
        }

    def set_irigb_source(self, source):
        """ Set the source of the IRIG-B signal."""
        if source not in self._IRIGB_SOURCE_TABLE:
            raise ValueError('Invalid IRIG-B source name. Valid names are %s' % ', '.join(self._IRIGB_SOURCE_TABLE.keys()))
        w2 = self.fpga_mmi_read(self._IRIGB_SAMPLE2_ADDR)
        self.fpga_mmi_write(self._IRIGB_SAMPLE2_ADDR, (w2 & 0x3FFFFFFF) | (self._IRIGB_SOURCE_TABLE[source] << 30))

    def get_irigb_source(self):
        """ Get the name of the current source of the IRIG-B signal."""
        source = self.fpga_mmi_read(self._IRIGB_SAMPLE2_ADDR) >> 30

        for (source_name, source_number) in self._IRIGB_SOURCE_TABLE.items():
            if source == source_number:
                return source_name
        raise ValueError('The IRIG-B module has an unknown source')

    _IRIGB_TIME_FORMAT = {
        'raw': lambda ts: ts,
        'datetime': lambda ts: ts.datetime,
        'nano' : lambda ts: ts.nano,
        'datetime+': lambda ts: (ts.datetime, ts.nano % 1000000000)
        }


    def _get_irigb_time(self, trig=True, format='datetime', noerror=False):
        """ Reads the IRIG-B time from the time decoder and returns an object
        that contains all the time information gathered from it.

        If trig=True, the time of the next 10 MHz reference clock rising edge
        is measured and returned. Otherwise, the last captured time is returned.
        """

        if format not in self._IRIGB_TIME_FORMAT:
            raise ValueError('Invalid time format. Valid formats are: %s' % (', '.join(self._IRIGB_TIME_FORMAT.keys())))

        # Capture current time
        if trig:
            w2 = self.fpga_mmi_read(self._IRIGB_SAMPLE2_ADDR)
            self.fpga_mmi_write(self._IRIGB_SAMPLE2_ADDR, w2 & ~(1 << 29))
            self.fpga_mmi_write(self._IRIGB_SAMPLE2_ADDR, w2 | (1 << 29))  # Create a rising edge
            t0 = time.time()
            while not self.fpga_mmi_read(self._IRIGB_TARGET1_ADDR) & (1 << 29):
                if time.time() - t0 > 1:
                    raise RuntimeError('Timeout while waiting for a Reference clock edge')

        w0 = self.fpga_mmi_read(self._IRIGB_SAMPLE0_ADDR)
        w1 = self.fpga_mmi_read(self._IRIGB_SAMPLE1_ADDR)
        w2 = self.fpga_mmi_read(self._IRIGB_SAMPLE2_ADDR)

        # t0 = self.fpga_mmi_read(self._IRIGB_TARGET0_ADDR)
        # t1 = self.fpga_mmi_read(self._IRIGB_TARGET1_ADDR)
        # t2 = self.fpga_mmi_read(self._IRIGB_TARGET2_ADDR)
        # e0 = self.fpga_mmi_read(self._IRIGB_EVENT_CTR_ADDR)

        ts = self._IrigTimestamp()
        ts.pps = (w0 >> 26) & ((1 << 6) - 1)
        ts.sbs = (w0 >> 8) & ((1 << 18) - 1)
        ts.y = (w0 >> 0) & ((1 << 8) - 1)
        ts.d = (w1 >> 20) & ((1 << 9) - 1)
        ts.h = (w1 >> 14) & ((1 << 6) - 1)
        ts.m = (w1 >> 7) & ((1 << 7) - 1)
        ts.s = (w1 >> 0) & ((1 << 7) - 1)
        ts.ss = (w2 >> 0) & ((1 << 28) - 1)
        ts.source = (w1 >> 30) & ((1 << 2) - 1)
        ts.recent = (w1 >> 29) & 1

        if not noerror and not ts.recent:
            raise RuntimeError('Invalid or no IRIG-B signal. Check your cable and source.')

        if not noerror and not self.zero_target_irigb_year_and_day and (ts.d < 1 or ts.d > 366):
            raise RuntimeError('Invalid IRIG-B day value %i. Day-of-year must be between 1 and 366' % ts.d)

        if self.zero_target_irigb_year_and_day:
            y = 0
            d = 1
        else:
            y = ts.y
            d = ts.d

        if not noerror and (ts.h > 23 or ts.m > 59 or ts.s > 59):
            raise RuntimeError('Invalid IRIG-B time value %ih %im %is.' % (ts.h, ts.m, ts.s))

        ts.datetime = datetime(y + 2000, 1, 1) + timedelta(d-1, ts.s, ts.ss//100, 0, ts.m, ts.h)
        # ts.before_target = (t1 >> 31) & 1
        # ts.done = (t1 >> 30) & 1
        ts.nano = int(timegm((y + 2000, 1, 1, 0, 0, 0)) * 1e9) + ((d-1) *24*3600 + ts.h * 3600 + ts.m * 60 + ts.s)*1000000000 + ts.ss*10
        # ts.event_ctr = e0

        return self._IRIGB_TIME_FORMAT[format](ts)

    def set_irigb_trigger_time(self, datetime_=None, delay=None):
        """ Sets the time at which the IRIG-B module will generate a trigger
        that can be used to synchronize boards.

        'datetime_' is the base target time in the Python as a 'datetime' object.

        'delay' is a time offset in seconds that is added to 'datetime_' so set
        the target time. It defaults to zero.

        If the trigger is used for synchronizing boards, the delay should be a
        multiple of 100 ns in order to ensure alignment with the 10 MHz
        reference clock and ensure deterministic start of the syncronization
        state machine.

        If 'datetime_' and 'delay' are None, the trigger time is set 3 seconds after the current time.
        """
        if datetime_ is None:
            dt = self._get_irigb_time(trig=True, format='datetime')
            self.logger.info('%.32r: Current IRIGB time is %s' % (self, dt.isoformat()))
            if delay is None:
                delay = 3
        else:
            dt = datetime_
            if delay is None:
                delay = 0

        nano_delay = int(delay * 1e9) % 1000  # Get submicrosecond delay in nanosecond units
        delay = int(delay * 1e6)/1e6  # Round delay to the microsecond
        dt += timedelta(0, delay)
        self.logger.info('%.32r: Setting IRIGB target time to %s + %3i ns' % (self, dt.isoformat(), nano_delay))
        if self.zero_target_irigb_year_and_day:
            y = 0
            d = 0
        else:
            y = dt.year % 100
            d = (dt - datetime(dt.year, 1, 1)).days + 1
        h = dt.hour
        m = dt.minute
        s = dt.second
        ss = dt.microsecond * 100 + int(nano_delay/10)

        self.logger.info('%.32r: Setting IRIGB target time with y=%i, d=%i, h=%i, m=%i, s=%i, ss=%i' % (self, y, d, h, m, s, ss))

        t0 = (y << 0)
        t1 = (d << 20) | (h << 14) | (m << 7) | (s << 0)
        t2 = (1 << 31) | (ss << 0)

        self.fpga_mmi_write(self._IRIGB_TARGET0_ADDR, t0)
        self.fpga_mmi_write(self._IRIGB_TARGET1_ADDR, t1)
        self.fpga_mmi_write(self._IRIGB_TARGET2_ADDR, t2)

    def is_irigb_before_trigger_time(self):
        """ Is true if the current IRIGB is before the target trigger time that was previously set-up.
        """
        t1 = self.fpga_mmi_read(self._IRIGB_TARGET1_ADDR)
        return bool((t1 >> 31) & 1)

    def capture_frame_time(self, trig=True, format='nano'):
        """ Captures the IRIG-B of the first sample of the next frame coming
        out of the ADC data acquisition module.

        The returned time is (frame_number, time), where frame_number is the
        number of the frame that was measured, and time is the IRIG-B time in
        a format specified by 'format' (see get_irigb_time() for available
        formats).

        NOTE1: floats do not have enough resolution to represent the current
        time down to nanoseconds (as opposed to Pythin int's which have
        infinite resolution), so beware of conversions.

        NOTE2: The method will generate a timeout error if there is no data
        coming out of the data acquisition module.

        NOTE3: There is a delay between the time the first sample of a packet
        is taken and the time the packet comes out of the data acquistion
        module (due to initial dropping of a few frames and the FIFO filling-
        delay).
        """
        if trig:
            w2 = self.fpga_mmi_read(self._IRIGB_SAMPLE2_ADDR)
            self.fpga_mmi_write(self._IRIGB_SAMPLE2_ADDR, w2 & ~(1 << 28))
            self.fpga_mmi_write(self._IRIGB_SAMPLE2_ADDR, w2 | (1 << 28))
            t0 = time.time()
            while not self.fpga_mmi_read(self._IRIGB_TARGET1_ADDR) & (1 << 30):
                    if time.time() - t0 > 1:
                        raise RuntimeError('Timeout while waiting for a Frame. Is data flowing out of the ADC data acquisition module?')
        event_number = self.fpga_mmi_read(self._IRIGB_EVENT_CTR_ADDR)
        captured_time = self._get_irigb_time(trig=0, format=format)  # The event trigger will automatically trig IRIGB
        return (event_number, captured_time)

    def capture_refclk_time(self, trig=True, format='nano'):
        """ Measures the time at which the next 10MHz reference clock rising
        edge occurs.

        The method returns the reference clock edge number (from a 32-bit
        counter that wraps around) and the time in the specified format (see
        get_irigb_time() for available formats).

        This method can be used to measure the drift of the 10 MHz reference clock relative to
        the IRIG-B time.
        """
        t = self._get_irigb_time(trig=trig, format=format)
        c = self.fpga_mmi_read(self._IRIGB_REFCLK_SAMPLE)
        return (c, t)  # Return

    def get_irigb_time(self, trig=True, format='datetime'):
        """ Return the current time as decoded on the IRIG-B input. The time
        is returned in a format specified by 'format':

        'raw': A object containing all the data fields read directly from the IRIG-B decoder and preprocessed datetime and nano values
        'datetime': Python 'datetime' object (with a microsecond resolution)
        'datetime+': A (dt,nano) tuple where dt is a datetime object, and nano is the number of nanoseconds within the second.
        'nano': An integer representing the number of nanoseconds since Jan 1st 2000.
        """
        return self._get_irigb_time(trig=trig, format=format)


class I2CInterface(object):
    """
    This class wraps all that is needed to access an I2C device in
    a standardized way, whether the access is done through the
    FPGA or through the ARM.
    """

    I2CException = IOError  # Exception object to expect from I2C communication errors

    def __init__(self, write_read_fn, port_select_fn, bus_table, _switch_addr, verbose=None):
        self.write_read_fn = write_read_fn
        self.set_port_fn = port_select_fn
        self._I2C_BUS_LIST = bus_table

        self._i2c_switch = tca9548a.tca9548a(self, _switch_addr)
        self._logger = logging.getLogger(__name__)

    def select_bus(self, bus_names, *args, **kwargs):
        """
        Configure the I2C port and I2C switch so the following
        communications will access the desired I2C bus. 'bus_id'
        can be a bus name or bus number, or a list of those if
        multiple buses are to be accessed at the same time. An
        error will be provided if all the buses are not accessible
        through the same FPGA I2C port. This function assumes that
        each FPGA I2C port has an identical I2C switch.
        """
        if isinstance(bus_names, (str, int)):
            bus_names = [bus_names]
        selected_fpga_port_number = None
        selected_switch_port_numbers = []
        for bus_name in bus_names:
            if bus_name not in self._I2C_BUS_LIST:
                self._logger.error("I2C bus '%s' is not part of the available buses. Valid values are %s" % (bus_name, ','.join(str(self._I2C_BUS_LIST.keys()))) )
            (fpga_port_number, switch_port_number) = self._I2C_BUS_LIST[bus_name]
            if selected_fpga_port_number is None:
                selected_fpga_port_number = fpga_port_number
            elif selected_fpga_port_number != fpga_port_number:
                self._logger.error("I2C bus '%s' is not on the same FPGA port as the other buses" % (bus_name) )
            selected_switch_port_numbers.append(switch_port_number)
            # self._logger.debug("Enabling I2C bus %s" % bus_name)

        if selected_fpga_port_number is not None:
            self.set_port_fn(selected_fpga_port_number)

        self._i2c_switch.set_port(selected_switch_port_numbers, *args, **kwargs)

    def write_read(self, *args, **kwargs):
        """
        Writes up to 3 bytes to the addressed I2C device and/or
        reads up to 4 bytes from that device after a restart. See
        the FPGA I2C module for detailed method description.
        """
        # self._logger.debug("Accessing I2C bus...")
        return self.write_read_fn(*args, **kwargs)

    def is_present(self, addr, bus_name=None):
        """ Test the presence of an I2C device at the specified address.
        """
        if bus_name:
            self.select_bus(bus_name, retry=3)
        try:
            self.write_read(addr, data=[], read_length=0, retry=0) #dummy I2C acces
        except IOError:
            return False
        return True



class IceBoardHardware(object):
    """
    Provides access to the hardware of an IceBoard Rev2/Rev3, including:
        - LED control
        - GPIO input/output
        - Temperature sensors
        - Power monitoring for every rail (voltage,current)
        - Motherboard EEPROM

    This class implements these methods by issuring I2C commands
    through the I2C interface provided either by the FPGA or by the
    ARM.

    Part or all of of functionnalities described in this class might
    eventually be implemented in the ARM processor itself. In thise
    case, these those will be available through the ARM's Tuber
    interface.
    """

    #------------------------------------
    # Define hardware-specific constants
    #------------------------------------
    NUMBER_OF_FMC_SLOTS = 2  # Indicates the number of FMC slots supported by this platform

    # I2C switch addresses (visible on all buses on a specific port)
    _FPGA_I2C_SWITCH_ADDR = 0b1110100  # 0x74
    _ARM0_I2C_SWITCH_ADDR = 0b1110000  # 0x70
    _ARM1_I2C_SWITCH_ADDR = 0b1110001  # 0x71
    _ARM2_I2C_SWITCH_ADDR = 0b1110010  # 0x72
    _ARM3_I2C_SWITCH_ADDR = 0b1110011  # 0x73

    FPGA_I2C_BUS_LIST = {
        # Bus name, (FPGA I2C port, I2C switch enable bit)
        "FMC":   (0, 0),  # equivalent to FMCA. Included for backwards compatibility with single-FMC code
        "FMCA":  (0, 0),
        "FMCB":  (0, 1),
        "QSFPA": (0, 2),
        "QSFPB": (0, 3),
        "SFP":   (0, 4),
        "SMPS":  (0, 5),
        "BP":    (0, 6),
        "GPIO":  (0, 7)
        }

    # FMC EEPROM, on FMCA or FMCB
    _FMC_EEPROM_ADDR = 0x50  # 0x50 (0x51 is also used for the 2nd page of large eeprom with address width > 16 bits).
    _FMC_EEPROM_ADDR_WIDTH = 7  # FMC EEPROM internal addresses are 7 bits wide.
    _FMC_EEPROM_PAGE_SIZE = 8  #

    # Oversize , non-FMC-standard EEPROM found on some McGill Mezzanines
    _MCGILL_FMC_EEPROM_ADDR_WIDTH = 17  # FMC EEPROM internal addresses are is 17 bits wide. (2 bytes as data, 1 bit in lsb of I2C address)
    _MCGILL_FMC_EEPROM_PAGE_SIZE = 256  #

    # Motherboard EEPROM
    _MOTHERBOARD_EEPROM_DATA_ADDR = 0x57  #
    _MOTHERBOARD_EEPROM_SERIAL_ADDR = 0x5F  #
    _MOTHERBOARD_EEPROM_ADDR_WIDTH = 7  # EEPROM internal addresses are is 17 bits wide. (2 bytes as data, 1 bit in lsb of I2C address)
    _MOTHERBOARD_EEPROM_PAGE_SIZE = 8  #


    # IO Expanders, on GPIO bus
    _GPIO_POWER_I2C_ADDR    = 0b0100000  # 0x20
    _GPIO_SFP_QSFP_I2C_ADDR = 0b0100001  # 0x21
    _GPIO_SW_LEDS_ADDR      = 0b0100010  # 0x22
    _GPIO_ARM_PHY_LEDS_ADDR = 0b0100011  # 0x23

    # # Temperature sensors, on GPIO bus
    # _TMP_ARM_I2C_ADDR   = 0b1001010 #0x4A
    # _TMP_PHY_I2C_ADDR   = 0b1001100 #0x4C
    # _TMP_FPGA_I2C_ADDR  = 0b1001011 #0x4B
    # _TMP_POWER_I2C_ADDR = 0b1001000 #0x48

    # # Power monitors, on SMPS bus
    # _POWER_ICEVADJ_I2C_ADDR   = 0b1000011 #0x43
    # _POWER_ICE12V0_I2C_ADDR   = 0b1000111 #0x47
    # _POWER_ICE5V0_I2C_ADDR    = 0b1001000 #0x48
    # _POWER_ICE3V3_I2C_ADDR    = 0b1001001 #0x49
    # _POWER_ICE1V5_I2C_ADDR    = 0b1001100 #0x4C
    # _POWER_ICE1V2_I2C_ADDR    = 0b1001101 #0x4D
    # _POWER_ICE1V0_I2C_ADDR    = 0b1001110 #0x4E
    # _POWER_ICE1V8_I2C_ADDR    = 0b1001011 #0x4B
    # _POWER_ICE1V0GTX_I2C_ADDR = 0b1001111 #0x4F

    # _POWER_FMCA12V0_I2C_ADDR  = 0b1000000 #0x40
    # _POWER_FMCA3V3_I2C_ADDR   = 0b1000001 #0x41
    # _POWER_FMCAVADJ_I2C_ADDR  = 0b1000010 #0x42

    # _POWER_FMCB12V0_I2C_ADDR  = 0b1000100 #0x44
    # _POWER_FMCB3V3_I2C_ADDR   = 0b1000101 #0x45
    # _POWER_FMCBVADJ_I2C_ADDR  = 0b1000110 #0x46

    def __init__(self, iceboard):
        """
        Creates all the I2C objects needed to interface the hardware. In order
        to do this, the following methods will be required from the iceboard
        object:

            - i2c_set_port(...) # Port number 0 (connected to the FPGA I2C switch) is used for all accesses
            - i2c_write_read(...) # FPGA I2C engine

        Those methods can be provided either by the ARM or the core FPGA firmware.

        For FPGA-based I2C:
            - fpga_core is not Null
            - fpga_core provides the following methods
        """

        self._logger = logging.getLogger(__name__)
        self._logger.debug('%.32r: Initializing Iceboard hardware' % iceboard)
        self._iceboard = iceboard
        self._i2c = self._iceboard.i2c
        self._logger.info('%.32r: Instantiating Motherboard EEPROM managers' % self._iceboard)
        self._motherboard_eeprom_data = eeprom.eeprom(self._i2c, self._MOTHERBOARD_EEPROM_DATA_ADDR, 'GPIO', self._MOTHERBOARD_EEPROM_ADDR_WIDTH, self._MOTHERBOARD_EEPROM_PAGE_SIZE)
        self._motherboard_eeprom_serial = eeprom.eeprom(self._i2c, self._MOTHERBOARD_EEPROM_SERIAL_ADDR, 'GPIO', self._MOTHERBOARD_EEPROM_ADDR_WIDTH, self._MOTHERBOARD_EEPROM_PAGE_SIZE)

        self._logger.info('%.32r:  Instantiating FMC EEPROM managers' % self._iceboard)

        # We check if the EEPROM has multiple pages, and if so, we *assume* that
        # the EEPROM is a large (non-FMC compliant) EEPROM with 2-byte addresses.
        # Otherwise we assume the EEPROM has a single byte of addressing.
        #
        # If the EEPROM is multipage but has a single address byte (4Kbit,
        # 8kbit or 16kbit EEPROMs), then the EEPROM contents will be
        # corrupted, even by read operations, because the second address byte
        # will be interpreted as data to be written. It would be equally bad
        # if there has another I2C device at the address following the EEPROM
        # address.
        if self._i2c.is_present(self._FMC_EEPROM_ADDR+1, bus_name='FMCA'):
            self._logger.info('%.32r: Detected multipage EEPROM on FMCA. Assuming >16-bit addressing.' % self._iceboard)
            self._fmca_eeprom = eeprom.eeprom(self._i2c, self._FMC_EEPROM_ADDR, 'FMCA', self._MCGILL_FMC_EEPROM_ADDR_WIDTH, self._MCGILL_FMC_EEPROM_PAGE_SIZE)
        else:
            self._fmca_eeprom = eeprom.eeprom(self._i2c, self._FMC_EEPROM_ADDR, 'FMCA', self._FMC_EEPROM_ADDR_WIDTH, self._FMC_EEPROM_PAGE_SIZE)

        if self._i2c.is_present(self._FMC_EEPROM_ADDR+1, bus_name='FMCB'):
            self._logger.info('%.32r: Detected multipage EEPROM on FMCB. Assuming >16-bit addressing.' % self._iceboard)
            self._fmcb_eeprom = eeprom.eeprom(self._i2c, self._FMC_EEPROM_ADDR, 'FMCB', self._MCGILL_FMC_EEPROM_ADDR_WIDTH, self._MCGILL_FMC_EEPROM_PAGE_SIZE)
        else:
            self._fmcb_eeprom = eeprom.eeprom(self._i2c, self._FMC_EEPROM_ADDR, 'FMCB', self._FMC_EEPROM_ADDR_WIDTH, self._FMC_EEPROM_PAGE_SIZE)

        self._FMC_EEPROM_TABLE = {
            1: self._fmca_eeprom,
            2: self._fmcb_eeprom
            }


        self._logger.info('%.32r: Instantiating I2C GPIO manager' % self._iceboard)
        self._gpio_power = pca9575.pca9575(self._i2c, self._GPIO_POWER_I2C_ADDR, 'GPIO')
        self._gpio_sw_leds = pca9575.pca9575(self._i2c, self._GPIO_SW_LEDS_ADDR, 'GPIO')
        self._gpio_arm_phy_leds = pca9575.pca9575(self._i2c, self._GPIO_ARM_PHY_LEDS_ADDR, 'GPIO')
        self._gpio_sfp_qsfp = pca9575.pca9575(self._i2c, self._GPIO_SFP_QSFP_I2C_ADDR, 'GPIO')




        self._gpio = gpio.GPIO(gpio_table={
            # name : (expander object, byte, lsb bit number,  width)
            'GP_SW1': (self._gpio_sw_leds, 0, 0, 1),
            'GP_SW2': (self._gpio_sw_leds, 0, 1, 1),
            'GP_SW3': (self._gpio_sw_leds, 0, 2, 1),
            'GP_SW4': (self._gpio_sw_leds, 0, 3, 1),
            'GP_SW5': (self._gpio_sw_leds, 0, 4, 1),
            'GP_SW6': (self._gpio_sw_leds, 0, 5, 1),
            'GP_SW7': (self._gpio_sw_leds, 0, 6, 1),
            'GP_SW8': (self._gpio_sw_leds, 0, 7, 1),
            'GP_LED1': (self._gpio_sw_leds, 1, 7, 1),
            'GP_LED2': (self._gpio_sw_leds, 1, 6, 1),
            'GP_LED3': (self._gpio_sw_leds, 1, 5, 1),
            'GP_LED4': (self._gpio_sw_leds, 1, 4, 1),
            'GP_LED5': (self._gpio_sw_leds, 1, 3, 1),
            'GP_LED6': (self._gpio_sw_leds, 1, 2, 1),
            'GP_LED7': (self._gpio_sw_leds, 1, 1, 1),
            'GP_LED8': (self._gpio_sw_leds, 1, 0, 1),
            'GP_LED9': (self._gpio_arm_phy_leds, 0, 0, 1),
            'GP_LED10': (self._gpio_arm_phy_leds, 0, 1, 1),
            'GP_LED11': (self._gpio_arm_phy_leds, 0, 2, 1),
            'GP_LED12': (self._gpio_arm_phy_leds, 0, 3, 1),
            'GTX1V8PowerFault': (self._gpio_arm_phy_leds, 0, 4, 1),
            'PHYAPowerFault': (self._gpio_arm_phy_leds, 0, 5, 1),
            'PHYBPowerFault': (self._gpio_arm_phy_leds, 0, 6, 1),
            'ArmPowerFault': (self._gpio_arm_phy_leds, 0, 7, 1),
            'BP_GPIO0': (self._gpio_arm_phy_leds, 1, 0, 1),
            'BP_GPIO1': (self._gpio_arm_phy_leds, 1, 1, 1),
            'BP_GPIO2': (self._gpio_arm_phy_leds, 1, 2, 1),
            'BP_GPIO3': (self._gpio_arm_phy_leds, 1, 3, 1),
            'BP_GPIO4': (self._gpio_arm_phy_leds, 1, 4, 1),
            'BP_GPIO5': (self._gpio_arm_phy_leds, 1, 5, 1),
            'BP_SLOT_NUMBER': (self._gpio_arm_phy_leds, 1, 0, 4),  # Also corresponds to BP_GPIO0-3
            'QSFPA_ModPrsL': (self._gpio_sfp_qsfp, 0, 0, 1),
            'QSFPA_ResetL': (self._gpio_sfp_qsfp, 0, 2, 1),
            'QSFPA_IntL': (self._gpio_sfp_qsfp, 0, 1, 1),
            'QSFPA_LPMode': (self._gpio_sfp_qsfp, 0, 4, 1),
            'QSFPA_ModSelL': (self._gpio_sfp_qsfp, 0, 3, 1),
            'QSFPB_ModPrsL': (self._gpio_sfp_qsfp, 0, 5, 1),
            'QSFPB_ResetL': (self._gpio_sfp_qsfp, 0, 7, 1),
            'QSFPB_IntL': (self._gpio_sfp_qsfp, 0, 6, 1),
            'QSFPB_LPMode': (self._gpio_sfp_qsfp, 1, 5, 1),
            'QSFPB_ModSelL': (self._gpio_sfp_qsfp, 1, 4, 1),
            'SFP_LOS': (self._gpio_power, 0, 7, 1),
            'SFP_TxFault': (self._gpio_sfp_qsfp, 1, 0, 1),
            'SFP_TxDisable': (self._gpio_sfp_qsfp, 1, 1, 1),
            'SFP_RS0': (self._gpio_sfp_qsfp, 1, 2, 1),
            'SFP_RS1': (self._gpio_sfp_qsfp, 1, 3, 1),
            'SFP_ModSelL': (self._gpio_power, 1, 7, 1),
            'FMCA_PG_M2C': (self._gpio_power, 0, 3, 1),
            'FMCB_PG_M2C': (self._gpio_power, 1, 3, 1)
        })

        self._qsfpa = qsfp.QSFP(self._i2c, 'QSFPA', gpio_prefix='QSFPA_', gpio=self._gpio)
        self._qsfpb = qsfp.QSFP(self._i2c, 'QSFPB', gpio_prefix='QSFPA_', gpio=self._gpio)

        self.qsfp = Ccoll((self._qsfpa, self._qsfpb))
        # self._logger.info(' Instantiating I2C temperature sensors')
        # self._tmp_power = tmp100.tmp100(self._i2c, self._TMP_POWER_I2C_ADDR, 'GPIO')
        # self._tmp_phy = tmp100.tmp100(self._i2c, self._TMP_PHY_I2C_ADDR, 'GPIO')
        # self._tmp_fpga = tmp100.tmp100(self._i2c, self._TMP_FPGA_I2C_ADDR, 'GPIO')
        # self._tmp_arm = tmp100.tmp100(self._i2c, self._TMP_ARM_I2C_ADDR, 'GPIO')

        # self._logger.info(' Instantiating I2C current/power monitors')
        # self._power_ice_3v3 = ina230.ina230(self._i2c, self._POWER_ICE3V3_I2C_ADDR, 'SMPS')
        # self._power_ice_12v0 = ina230.ina230(self._i2c, self._POWER_ICE12V0_I2C_ADDR, 'SMPS')
        # self._power_ice_5v0 = ina230.ina230(self._i2c, self._POWER_ICE5V0_I2C_ADDR, 'SMPS')
        # self._power_ice_1v0_gtx = ina230.ina230(self._i2c, self._POWER_ICE1V0GTX_I2C_ADDR, 'SMPS')
        # self._power_ice_vadj = ina230.ina230(self._i2c, self._POWER_ICEVADJ_I2C_ADDR, 'SMPS')
        # self._power_ice_1v2 = ina230.ina230(self._i2c, self._POWER_ICE1V2_I2C_ADDR, 'SMPS')
        # self._power_ice_1v5 = ina230.ina230(self._i2c, self._POWER_ICE1V5_I2C_ADDR, 'SMPS')
        # self._power_ice_1v0 = ina230.ina230(self._i2c, self._POWER_ICE1V0_I2C_ADDR, 'SMPS')
        # self._power_ice_1v8 = ina230.ina230(self._i2c, self._POWER_ICE1V8_I2C_ADDR, 'SMPS')

        # self._power_fmca_12v0 = ina230.ina230(self._i2c, self._POWER_FMCA12V0_I2C_ADDR, 'SMPS')
        # self._power_fmca_3v3 = ina230.ina230(self._i2c, self._POWER_FMCA3V3_I2C_ADDR, 'SMPS')
        # self._power_fmca_vadj = ina230.ina230(self._i2c, self._POWER_FMCAVADJ_I2C_ADDR, 'SMPS')

        # self._power_fmcb_12v0 = ina230.ina230(self._i2c, self._POWER_FMCB12V0_I2C_ADDR, 'SMPS')
        # self._power_fmcb_3v3 = ina230.ina230(self._i2c, self._POWER_FMCB3V3_I2C_ADDR, 'SMPS')
        # self._power_fmcb_vadj = ina230.ina230(self._i2c, self._POWER_FMCBVADJ_I2C_ADDR, 'SMPS')


        # self.TEMPERATURE_SENSOR_TABLE = {
        #     # sensor name: tmp object
        #     'TEMP_POWER': self._tmp_power,
        #     'TEMP_PHY': self._tmp_phy,
        #     'TEMP_FPGA': self._tmp_fpga,
        #     'TEMP_ARM': self._tmp_arm
        # }

        # self.POWER_SENSOR_TABLE = {
        #     # sensor name : (ina230 object, output voltage(volts), rshunt(inductor) (mohm), typical current(amps), current tolerance (0<tol<1))
        #     'ICE_3V3': (self._power_ice_3v3, 3., 2.36, 8., 0.5),         #SER1360-182L
        #     'ICE_12V0': (self._power_ice_12v0, 12., 5.5, 3., 0.5),       #SER1360-602L
        #     'ICE_5V0': (self._power_ice_5v0, 5., 2.36, 11., 0.5),        #SER1360-182L
        #     'ICE_1V0_GTX': (self._power_ice_1v0_gtx, 1., 0.77, 16., 0.5),#SER1360-331L
        #     'ICE_1V2': (self._power_ice_1v2, 1.2, 0.77, 8., 0.5),        #SER1360-651L
        #     'ICE_1V5': (self._power_ice_1v5, 1.5, 2.36, 3., 0.5),        #SER1360-182L
        #     'ICE_1V0': (self._power_ice_1v0, 1., 0.77, 16., 0.5),        #SER1360-331L
        #     'ICE_1V8': (self._power_ice_1v8, 1.8, 5.5, 1., 0.5),         #SER1360-602L
        #     'ICE_VADJ': (self._power_ice_vadj, 2.5, 2.36, 8., 0.5),
        #     'FMCA_12V0': (self._power_fmca_12v0, 12., 5, 2., 0.5),
        #     'FMCB_12V0': (self._power_fmcb_12v0, 12., 5, 2., 0.5),
        #     'FMCA_3V3': (self._power_fmca_3v3, 3., 5, 5., 0.5),
        #     'FMCB_3V3': (self._power_fmcb_3v3, 3., 5, 5., 0.5),
        #     'FMCA_VADJ': (self._power_fmca_vadj, 2.5, 5, 1., 0.5),
        #     'FMCB_VADJ': (self._power_fmcb_vadj, 2.5, 5, 1., 0.5)
        # }

    def open(self):
        """
        """
        pass


    def close(self):
        self._logger.info('Closing Iceboard hardware')
        if self._i2c:
            self._i2c = None

    def init(self):
        """Initializes the motherboard hardware to a known state.
        This will turn off FMC power.
        """
        self._init_gpio_expanders()
        # self._init_temperature_sensors()
        # self.set_fmc_power()
        # self._init_power_sensors()

        # Initialize the QSFPs. This sets the reset and LowPower mode. Some
        # QSFP+ modules (like the 3M AOCs) will not work without this.
        self._qsfpa.init()
        self._qsfpb.init()

    def _init_gpio_expanders(self):
        """
        Initializes GPIO expanders

        History

        140304 JM: created. todo: make more flexible for I/O pin configuration
        of each expander. Need to confirm I/O pin config with JF
        """
        self._gpio_power.init(cfg0_def=0b10101000,
                              cfg1_def=0b10101000,
                              out0_default=0,
                              out1_default=0,
                              bken0=0b00,  # We need to disable 100K internal pull-ups/down so the PG_M2C can work properly (there is another external 100K pull up to VCC3V3 which pulls to GND when there is no power. Pulling up doesn't work when board is off , pull down doesn't work when board is ON)
                              bken1=0b00,
                              pupd0=0b00001000,  # don't care, pullups not enabled
                              pupd1=0b00001000
                              )
        self._gpio_sw_leds.init(cfg1_def=0b00000000)
        self._gpio_arm_phy_leds.init(cfg0_def=0b11110000)
        self._gpio_sfp_qsfp.init(cfg0_def=0b01100011, cfg1_def=0b11001111)

    def get_i2c_interface(self):
        """
        Returns an I2C interface object that provides a standardized bus selection.
        """
        return self._i2c

    def get_number_of_fmc_slots(self):
        return self.NUMBER_OF_FMC_SLOTS

    # def get_slot_number(self):
    #     return self._gpio.read('BP_SLOT_NUMBER') + 1

    def read_motherboard_eeprom(self, addr, length, **kwargs):
        return self._motherboard_eeprom_data.read(addr, length, **kwargs)

    def write_motherboard_eeprom(self, addr, data, **kwargs):
        return self._motherboard_eeprom_data.write(addr, data, **kwargs)

    def read_mezzanine_eeprom(self, mezzanine, addr, length, **kwargs):
        eeprom_object = self._FMC_EEPROM_TABLE[mezzanine]
        return eeprom_object.read(addr, length, **kwargs)

    def write_mezzanine_eeprom(self, mezzanine, addr, data, **kwargs):
        eeprom_object = self._FMC_EEPROM_TABLE[mezzanine]
        return eeprom_object.write(addr, data, **kwargs)

    def set_mezzanine_power(self, fmc_number=range(NUMBER_OF_FMC_SLOTS), state=[True]*NUMBER_OF_FMC_SLOTS):
        """
        Enables or disables power of the specified FMC slot.
        Proper power sequencing is done to prevent the FMC board switchers to create too much a current spike when enabled.

        History:
            140223 JFC: Modified to use register names.
            140304 JM: Modified it so a state for every fmc can be specified. For now, state is either a boolean or a list of booleans with the same length as 'fmc_number'
        Todo:
            140223 JFC: used masked writes to avoid side effects.
        """
        if isinstance(fmc_number, int):
            fmc_number = [fmc_number]

        if isinstance(state, (bool, int)):
            state = [state] * len(fmc_number)

        for (fmc, fmc_state) in zip(fmc_number, state):
            if fmc not in range(self.NUMBER_OF_FMC_SLOTS):
                raise ValueError('FMC number %i is not a valid value' % fmc)
            else:
                # out_reg = 'OUT%i' % fmc # sets the register name to access based on the FMC number
                # cfg_reg = 'CFG%i' % fmc
                # self._gpio_power.write(out_reg, 0b00000000) # Turn off all power signals before we enable the GPIO outputs
                # self._gpio_power.write(cfg_reg, 0b10101000)
                self._gpio_power.write(fmc, 0b00000111*bool(fmc_state))  # Turn on power to board
                self._gpio_power.write(fmc, 0b01010111*bool(fmc_state))  # Set Power Good and CLKDIR to 1

    def set_led(self, led_name, state):
        """
        Set the LED(s) specified in 'led_name' to the the 'state'.
        'led_name' can be a list of LED names found in
        gpio object.  'state' can be a single boolean value, or
        an array with the same length as 'led_name'
        """
        if isinstance(led_name, str):
            led_name = [led_name]

        if isinstance(state, (bool, int)):
            state = [state] * len(led_name)

        for (led, led_state) in zip(led_name, state):
            self._gpio.write(led, led_state)

    def get_led(self, led_name):
        """
        Returns the status of specified LED(s) in a dictionary
        led_status where each key is a led_name and the respective value
        is the led status.
        """
        led_status = {}
        if isinstance(led_name, str):
            led_name = [led_name]

        for led in led_name:
            led_status[led] = self._gpio.read(led)

        return led_status

    # def _init_temperature_sensors(self, temperature_sensor_name=None, bit_resolution=12):
    #     """ Initialize temperature sensors.

    #     'temperature_sensor_name' can be a list of temperature sensor names
    #     found in TEMPERATURE_SENSOR_TABLE. If temperature_sensor_name=None,
    #     all sensors in the list are initialized.

    #     'bit_resolution' is the number of bits of resolution of the
    #     temperature register. It can take values 9, 10, 11, 12

    #     History:
    #     140318 JM: created
    #     """
    #     if bit_resolution<9 or bit_resolution>12:
    #         raise self.ValueError('Bit_resolution is out of range. Must be 9,10,11 or 12 bits')
    #     else:
    #         if temperature_sensor_name == None:
    #             temperature_sensor_name = self.TEMPERATURE_SENSOR_TABLE.keys()
    #         elif isinstance(temperature_sensor_name, str):
    #             temperature_sensor_name = [temperature_sensor_name]

    #         for temp_sensor in temperature_sensor_name:
    #             if temp_sensor not in self.TEMPERATURE_SENSOR_TABLE:
    #                 raise ValueError('Invalid temperature sensor name. Valid names are %s' % ','.join(self.TEMPERATURE_SENSOR_TABLE.keys()))
    #             else:
    #                 tmp_object = self.TEMPERATURE_SENSOR_TABLE[temp_sensor]
    #                 try:
    #                     tmp_object.init(bit_resolution)
    #                 except:
    #                     self._logger.info('%.32r: Temperature sensor %s failed to initialize.' % (self._iceboard, temp_sensor))

    # def _init_power_sensors(self, power_sensor_name=None):
    #     """
    #     initializes current/power monitors
    #     'power_sensor_name' can be a list of current/power monitor names found in POWER_SENSOR_TABLE. If power_sensor_name=None, all sensors in
    #     POWER_SENSOR_TABLE are initialized.

    #     History:
    #     140320 JM: created
    #     """
    #     if power_sensor_name == None:
    #         power_sensor_name = self.POWER_SENSOR_TABLE.keys()
    #     elif isinstance(power_sensor_name, str):
    #         power_sensor_name = [power_sensor_name]

    #     for power_sensor in power_sensor_name:
    #         if power_sensor not in self.POWER_SENSOR_TABLE:
    #            raise ValueError('Invalid power sensor name. Valid names are %s' % ','.join(self.POWER_SENSOR_TABLE.keys()))
    #         else:
    #             power_sensor_object, v_out, r_shunt, i_typ, tol_i = self.POWER_SENSOR_TABLE[power_sensor]
    #             try:
    #                 power_sensor_object.init(v_out=v_out, r_shunt=r_shunt, i_typ=i_typ, tol_i=tol_i)
    #             except:
    #                 self._logger.info('%.32r: Power sensor %s failed to initialize.' % (self._iceboard, power_sensor))


    # def get_temperature(self, temperature_sensor_name=None):
    #     """
    #     Returns the current temperature measured on the specified
    #     sensor(s).  NOTE: some temperatures are taken from the FPGA
    #     inetrnal SYSTEM monitor.

    #     initializes temperature expanders
    #     'temperature_sensor_name' can be a list of temperature sensor
    #     names found in TEMPERATURE_SENSOR_TABLE

    #     Returns a dictionary with keys corresponding to the temperature_sensor_name names.

    #     History:
    #     140318 JM: created
    #     """
    #     temperature_dict = {}
    #     if temperature_sensor_name == None:
    #         temperature_sensor_name = self.TEMPERATURE_SENSOR_TABLE.keys()
    #     elif isinstance(temperature_sensor_name, str):
    #         temperature_sensor_name = [temperature_sensor_name]

    #     for temp_sensor in temperature_sensor_name:
    #         if temp_sensor not in self.TEMPERATURE_SENSOR_TABLE:
    #             raise ValueError('Invalid temperature sensor name')
    #         else:
    #             tmp_object = self.TEMPERATURE_SENSOR_TABLE[temp_sensor]
    #             temperature_dict[temp_sensor] = tmp_object.get_temperature()

    #     return temperature_dict

    # def get_power(self, power_sensor_name=None):
    #     """
    #     Returns the voltage, current and power of the power monitoring system.
    #     Includes power measured internally from  the FPGA's system monitor.
    #     Multiple targets can be specified.

    #     Arguments:

    #        'power_sensor_name' can be a list of temperature sensor
    #         names found in POWER_SENSOR_TABLE. If
    #         power_sensor_name=None, measurements of all sensors in
    #         TEMPERATURE_SENSOR_TABLE are returned.

    #     Returns dictionary with keys corresponding to the
    #     power_sensor_name names. The respective value is a (bus
    #     voltage (V), shunt voltage (V), current (A), power (W)) tuple.

    #     History:
    #     140320 JM: created
    #     """
    #     power_dict = {}
    #     if power_sensor_name == None:
    #         power_sensor_name = self.POWER_SENSOR_TABLE.keys()
    #     elif isinstance(power_sensor_name, str):
    #         power_sensor_name = [power_sensor_name]

    #     for power_sensor in power_sensor_name:
    #         if power_sensor not in self.POWER_SENSOR_TABLE:
    #             raise ValueError('Invalid power sensor name. Valid names are %s.' % ','.join(self.POWER_SENSOR_TABLE.keys()))
    #         else:
    #             power_sensor_object = self.POWER_SENSOR_TABLE[power_sensor][0]

    #             try:
    #                 bus_voltage = power_sensor_object.get_bus_voltage()
    #                 shunt_voltage = power_sensor_object.get_shunt_voltage()
    #                 current = power_sensor_object.get_current()
    #                 power =  power_sensor_object.get_power()
    #             except:
    #                 bus_voltage = None
    #                 shunt_voltage = None
    #                 current = None
    #                 power = None
    #             power_dict[power_sensor]=(bus_voltage, shunt_voltage, current, power)

    #     return power_dict


    # def get_serial_number(self):
    #     """
    #     Returns the board's serial number. which is actually the FPGA's
    #     serial number.
    #     """
    #     return self._iceboard.get_serial_number(); # tentative code

    # def get_info(self):
    #     """Loads the info data on the motherboard"""
    #     pass

    # def status(self):
    #     """Displays the status of the motherboard"""



# vim: sts=4 ts=4 sw=4 tw=80 smarttab expandtab
