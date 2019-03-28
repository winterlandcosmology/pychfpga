""" Basic handler for chFPGA firmware.
"""

import logging
from datetime import datetime, timedelta
from calendar import timegm
import time
import struct
import base64
from collections import OrderedDict
import socket

from ..icecore import IceBoardPlusHandler
from ..icecore import tuber  # Used to get TuberRemoteError
from ..icecore import Ccoll
from ..icecore import async, async_sleep, async_return, async_moment
from wtl.metrics import Metrics

from .. import I2C as i2c
from .. import GPIO as fpga_gpio

from . import icecrate_ext  # this module is not referenced here, but loading it registers the handler with IceCrate.

# Import IceBoard hardware handlers
# from lib import tmp100  # I2C Temperature sensor
from lib import pca9575  # I2C 16-bit IO Expander
from lib import tca9548a  # I2C switch
from lib import ina230  # I2C Voltage and current monitor
from lib import eeprom
from lib import qsfp
from lib import gpio


class IceBoardExtHandler(IceBoardPlusHandler):
    """ Provides basic access to the basic CHIME-specific FPGA firmware features and to the IceBoard
    hardware resources through the FPGA.

    This class provides:

    - UDP/IP/Ethernet-based direct Memory Map Interface (MMI) to the FPGA using its Ethernet port. Note
      that this accesses an address space that is separate from the one accessed throughthe ARM SPI interface.
      Methods to initialize the Ethernt networking parameters through the ARM SPI interface are provided.
    - Alternate access to the IceBoard and Backplane hardware through the FPGA I2C interface through
      the `hw` object. Access to the hardware is normally done through the high-level ARM-provided
      methods, but these methods are useful for development and debugging. The exception is the the
      IceCrate handler which uses the FPGA to access backplane resources.
    - Overriden mezzanine identification methods that support non-IPMI McGill ADC mezzanine boards.
    - IRIG-B subsystem operation (through the ARM SPI interface)

    `IceBoardExtHandler` can be created as a standard Python object initialized with a number of
    parameters which set corresponding attributes (see below). If a `parent_getter` function is
    provided, the value of these attributes will instead be fetched dynamically from the parent
    object. Note that any explicitely specified parameter overrides a parent parameter.

    Parameters:

        parent_getter (func): Function that dynamically return the parent object from which the
            following parameters will be fetched. Is `None` if there is no parent.

        hostname (str): hostname or IP address of the ICEBoard ARM processor (mandatory)

        serial (str): Serial number of the board. Can be provided by the ARM.

        part_number (str): Part number of the IceBoard. Can be obtained from the ARM.

        crate (IceCrateHandler): = object that handle the backplane on which the board is connected.
            `None` if the board is not connected to a backplane.

        slot (int): Slot number in which the board is installed ona backplane. None if there is no
            backplane.

        mezzanine (dict): Map {mezzanine_number: Mezzanine Handler, ...} describing the installed
            mezzanines. Can be obtained from the ARM.

        tuber_objname (str): name of the set of software functions that will be provided by the ARM
            processor through the Tuber interface.

        fpga_ip_address (str): FPGA's listening IP address in the form 'xx.xx.xx.xx'. If None
            (default), the address will be obtained by converting the ARM address 'a.b.c.d' into 'a.b.3.d'.

        fpga_port_number (int): FPGA listening port number for commands. Defaults to 41000. If None,
            the local port number is used.

        local_port_number (int): UDP port number to use to receive command replies. If 0, the number
            is allocated randomly by the OS. If None, and there is a crate_number and a slot number,
            then the port numbers will be derived from hese parameters.

    Python-based application-specific FPGA firmware and hardware handler are meant to be derived
    from this class.
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
    _IRIGB_EVENT_CTR_ADDR2          = 4 * 17
    _BP_BUCK_SYNC_ADDR              = 4 * 18
    _SFP_STATUS_ADDR                = 4 * 19
    _REMOTE_IP_PORT_ADDR            = 4 * 20
    _IRIGB_REFCLK_SAMPLE            = 4 * 21

    _XILINX_OUI = 0x000A35

    # ---------------------------------------
    # Instance attributes
    # ---------------------------------------

    interface_ip_addr = None  # Is automatically detected by opening a TCP connection to the ARM
    zero_target_irigb_year_and_day = False # If True, target IRIGB yead and day will always be written as zero binary values to me compatible with the IRIG-B generator

    class AutoOpen(object):
        """ Automatcally call the specified 'open' method that creates an
        attribute if the attribute is accessed and is not yet defined.
        """
        def __init__(self, attribute_name, open_method):
            self._open_method = open_method
            self._attribute_name = attribute_name

        def __get__(self, obj, class_):
            getattr(obj, self._open_method)()  # Execute the open method. This normally overrites the attribute, so this will not be called again.
            return getattr(obj, self._attribute_name)  # Get target object

    mmi       = AutoOpen('mmi',       'open_core')
    i2c       = AutoOpen('i2c',       'open_core')
    core_gpio = AutoOpen('core_gpio', 'open_core')
    core_i2c  = AutoOpen('core_i2c',  'open_core')
    hw        = AutoOpen('hw',        'open_hw'  )
    # bp        = AutoOpen('open_bp', 'bp')

    def __init__(self, parent_getter=None,
                 hostname=None, serial=None, part_number=None,
                 crate=None, slot=None, mezzanine={},
                 tuber_objname='IceBoard',
                 fpga_ip_addr=None, fpga_port_number=None,
                 local_port_number=None):
        """
        Creates an Iceboard that is accessed through the networking parameters
        specified in the database.

        The created object does not have any fpga or hardware handlers yet.
        Those will be created when the Iceboard is opened.
        """
        super(IceBoardExtHandler, self).__init__(parent_getter=parent_getter,
              hostname=hostname, serial=serial, part_number=part_number,
              crate=crate, slot=slot, mezzanine=mezzanine, tuber_objname=tuber_objname)
        self.logger = logging.getLogger(__name__)

        self.fpga_ip_addr = fpga_ip_addr
        self.fpga_port_number = fpga_port_number
        self.local_port_number = local_port_number

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

    # def spi_mmi_read(self, addr):
        # """ Read a single 32-bit word at specified
        #     byte address through the ARM<->FPGA SPI link."""
        # return self._fpga_spi_peek(addr)

    # def spi_mmi_write(self, addr, value): """ Write a single 32-bit word at
    #     specified byte address through the ARM<->FPGA SPI link."""
    #     self._fpga_spi_poke(addr, value)

    @async
    def open_core(self, udp_retries=10):
        """
        Establishes the connection with the hardware and firmware on the
        IceBoard and create all appropriate handling classes.
        """
        # print '%r: opening core' % self

        if not (yield self.is_fpga_programmed.async()):
            raise RuntimeError(
                "%r: The FPGA is not programmed with a bitstream . "
                'Direct UDP link to FPGA cannot be established ' % (self))

        # Check the FPGA firmware cookie obtained through the ARM SPI interface to the FPGA
        cookie = yield self.get_fpga_application_cookie.async()
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
        if not self.interface_ip_addr:  # set the interface only of we haven't manually defined one
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
        if not self.fpga_ip_addr:
            ip_packed = socket.inet_aton(self._get_arm_ip())  #
            ip_packed = ip_packed[:2] + chr(3) + ip_packed[3]
            self.fpga_ip_addr = socket.inet_ntoa(ip_packed)

        # Compute the local port number if requested (self.local_port_number is None) and if possible (there is a slot and crate number)
        if self.local_port_number is None:
            if not self.slot or not self.crate or self.crate.crate_number is None:
                self.local_port_number = 0
                self.logger.warning('%r: cannot use slot/crate_number-based UDP port number for UDP control channel. There is no slot or crate_number info. Using OS-assigned random port' % self)
            else:
                self.local_port_number = self._FPGA_CONTROL_BASE_PORT + 16*self.crate.crate_number + (self.slot-1)
                self.logger.info('%r: Replies will be sent to %s:%i' % (self, self.interface_ip_addr, self.local_port_number))



        # if the FPGA handler instance was not created, check if one exists
        # create it
        if self.is_core_open():
            self.logger.warning(
                '%r: Attempting to open core while it is already opened. '
                'Ignoring.' % (self))
            return

        # -------------------------------------------------------------------------
        # Open the UDP MMI interface
        # -------------------------------------------------------------------------
        from .lib import fpga_mmi
        self.logger.info('%r: Opening FPGA MMI with FPGA=(%s:%s),  local=(%s:%s)' % (self, self.fpga_ip_addr, self.fpga_port_number, self.interface_ip_addr, self.local_port_number))
        self.mmi = fpga_mmi.FpgaMmi(
            fpga_ip_addr=self.fpga_ip_addr,
            fpga_port_number=self.fpga_port_number, # none or 0: use local port number
            interface_ip_addr=self.interface_ip_addr,
            local_port_number=self.local_port_number, # 0 = randomly assigned by os
            udp_retries=udp_retries,
            parent=self)
        self.mmi.open()
        self.local_port_number = self.mmi.local_port_number
        self.fpga_port_number = self.mmi.fpga_port_number
        # print('opened fpga (%s:%s) local (%s:%s)' % (self.fpga_ip_addr, self.fpga_port_number, self.interface_ip_addr, self.local_port_number))
        # print('opened fpga (%s:%i) local (%s:%i)' % (self.fpga_ip_addr, self.fpga_port_number, self.interface_ip_addr, self.local_port_number))

        # Select the fpga port number
        #if not self.fpga_port_number:
        #    self.fpga_port_number = self.local_port_number

        # Set-up the FPGA networking parameters using the ARM-SPI link to the FPGA
        self.fpga_mac_addr = yield self.set_fpga_control_networking_parameters.async(
            fpga_ip_addr=self.fpga_ip_addr,
            fpga_port_number=self.fpga_port_number)

        # -------------------------------------------------------------------------
        # Open FPGA's GPIO module interface
        # -------------------------------------------------------------------------
        self.core_gpio = fpga_gpio.GPIO_base(self, self._SYSTEM_GPIO_BASE_ADDR)

        # -------------------------------------------------------------------------
        # Check if we can communicate with the FPGA over the direct Ethernet
        # link by reading the UDP MMI cookie (not the SPI one) and check if
        # the cookie correspond to the chFPGA firmware.
        # -------------------------------------------------------------------------
        yield self.clear_fpga_udp_errors.async(force=True, no_reset=True) # Try to prevent initial error on first command
        self.logger.debug("%r: Attempting to communicate with the FPGA over direct Ethernet link" % self)
        try:
            cookie = yield self.get_fpga_firmware_cookie.async(resync=True)  # Read the firmware version cookie from the GPIO subsystem (this is provided by the FPGA core firmware which is always present on all versions of the FPGA)
        except IOError as e:
            error_message = "%r: Unable to communicate with the FPGA at address %s:%i due to the following exception: %s" % (self, self.fpga_ip_addr, self.fpga_port_number, repr(e))
            self.close()
            self.logger.error(error_message)
            raise

        if cookie != self._CHFPGA_COOKIE:
            error_message = '%r: The firmware at %s:%i is not chFPGA. The magic cookie returned by the FPGA is 0x%02X, whereas we expected 0x%02X' % (self, self.fpga_ip_addr, self.fpga_port_number, cookie, self._CHFPGA_COOKIE)
            self.logger.error(error_message)
            self.close()
            raise RuntimeError(error_message)

        self.logger.debug("%r: Established a UDP/Ethernet connection with the FPGA at %s:%i" % (self, self.fpga_ip_addr, self.fpga_port_number))

        # -------------------------------------------------------------------------
        # Open FPGA's I2C interfaces
        # -------------------------------------------------------------------------
        self.core_i2c = i2c.I2C_base(self, self._SYSTEM_I2C_BASE_ADDR)
        yield async_moment
        # Create standardized I2C interface
        self.i2c = I2CInterface(
            write_read_fn=self.fpga_i2c_write_read, # write-read function
            port_select_fn=self.fpga_i2c_set_port,
            bus_table=IceBoardHardware.FPGA_I2C_BUS_LIST,
            switch_addr=IceBoardHardware._FPGA_I2C_SWITCH_ADDR,
            parent=self) # parent object, whose repr() is used to tag messages

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

    @async
    def open_hw(self):
        # Open IceBoard hardware manager object
        self.hw = IceBoardHardware(iceboard=self)
        self._is_hw_open = True
        yield self.hw.open.async()

    def close_hw(self):
        if self._is_hw_open:
            self.hw.close()
            del self.hw
            self._is_hw_open = False

    @async
    def open(self, udp_retries=10):

        self.logger.debug('%r: open() is called' % (self))
        yield self.open_core.async(udp_retries=udp_retries)

        yield self.open_hw.async()
        yield self.hw.init.async()
        yield self.hw.set_led.async('GP_LED2', 1)  # Hardware link is on
        yield self.hw.set_led.async('GP_LED1', 0)  # Full FPGA firmware is not yet on

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

    @async
    def get_fpga_udp_metrics(self):
        metrics = Metrics(
            type='GAUGE',
            slot=(self.slot or 0) - 1,
            id=self.get_string_id(),
            crate_id=self.crate.get_string_id() if self.crate else None,
            crate_number=self.crate.crate_number if self.crate else None)

        if not self.is_open():
            async_return(metrics)
        try:
            # yield self.check_command_count.async(reset=True)
            metrics.add('fpga_udp_error_current_count', value=self.mmi.error_counter)
            vect = yield self.fpga_mmi_read.async(self._SFP_STATUS_ADDR)
            metrics.add('fpga_udp_tx_fifo_overflow', value= bool(vect & 1 << 17))
            metrics.add('fpga_udp_rx_fifo_overflow', value= bool(vect & 1 << 16))
            metrics.add('fpga_udp_sfp_remote_fault', value= bool(vect & 1 << 13))
            metrics.add('fpga_udp_sfp_duplex_mode', value= bool(vect & 1 << 12))
            metrics.add('fpga_udp_sfp_speed', value= (vect >> 10) & 3)
            metrics.add('fpga_udp_rxnotintable', value= (vect >> 6) & 1)
            metrics.add('fpga_udp_rxdisperr', value= (vect >> 5) & 1)
            metrics.add('fpga_udp_link_sync', value= (vect >> 1) & 1)
            metrics.add('fpga_udp_link_status', value= (vect >> 0) & 1)
        except IOError as e:
            self.logger.error('%r: Error getting FPGA udp metrics. Error is %r' % (self, e))
        async_return(metrics)




    @async
    def clear_fpga_udp_errors(self, force=False, no_reset=False):
        if force or self.mmi.error_counter > 40:
            self.logger.info('%r: clearing FPGA UDP errors' % self)
            if not no_reset:
                yield self.reset_sfp()
                yield self.reset_fpga_udp_stack.async()
            try:
                self.mmi.flush()
                self.mmi.read(0, length=1, retry=-1, resync=1)
                self.mmi.read(0, length=1, resync=1)
                self.mmi.flush()
                (cmd, rply) = self.core_gpio.get_command_count()
                self.mmi.send_counter = cmd
                self.mmi.recv_counter = rply
            except IOError:
                pass
            self.mmi.error_counter = 0
            self.logger.info('%r: Finished clearing FPGA UDP errors. Hoping it works now...' % self)

    @async
    def check_command_count(self, reset=False):
        """ Check UDP communication command/reply synchronization and optionally reset counts.

        Checks if the number of command and replies sent to/from the FPGA
        by the Python memory mapped interface matches the counts tallied by the FPGA
        firmware.

        Parameters:
            reset (bool): If true, reset the counts so the counts will be synchronized for the next command

        Returns:
            True if the communication (before the optional reset) is in sync.


        Since both ends drop packets that have invalid CRCs, this should
        detect any transmission error in addition to UDP packets dropped by
        the switches, routers or the networking stack on the host computer.

        The packet counts are modulo 256, so a loss of a multiple of 256 packets will not be detected.

        If 'reset' is True, the MMI counters are reset to the FPGA values in order to clear the error on future checks.
        """

        for trial in range(2):
            try:
                yield async_moment
                self.logger.info('%r: Checking command counters' % (self))
                (cmd, rply) = self.core_gpio.get_command_count()
                yield async_moment
                valid = (cmd == self.mmi.send_counter & 0xFF) and (rply == self.mmi.recv_counter & 0xFF)
                if not valid:
                    self.logger.warning('%r: Command counters differ cmd/rply in FPGA is (%i, %i), Python MMI is (%i, %i)' % (self, cmd, rply, self.mmi.send_counter & 0xFF, self.mmi.recv_counter & 0xFF))
                if reset:
                    self.mmi.send_counter = cmd
                    self.mmi.recv_counter = rply
                    valid = True
                break
            except IOError as e:
                self.logger.error("%r: UDP communinication error. Attempting to reset FPGA's UDP stack via the ARM processor (trial %i).The error is:\n %s" % (self, trial+1, e))
                yield self.reset_fpga_udp_stack()
                valid = False
                reset = True
            except Exception as e:
                self.logger.error("%r: Unhandled error during UDP communinication check. The error is:\n %r" % (self, e))
                raise
        else: # executes if we exhausted the for loop iterations, i.e  no break
            errmsg = "%r: Could not re-establish UDP communinication with the FPGA. Raising an exception" % (self)
            self.logger.error(errmsg)
            raise IOError(errmsg)
        async_return(valid)

    @async
    def reset_fpga_udp_stack(self):
        self.logger.warning("%r: Resetting %s FPGA's UDP communication stack" % (self, self.hostname))
        yield self.fpga_mmi_write.async(self._SFP_STATUS_ADDR, 3 << 30)
        yield self.fpga_mmi_write.async(self._SFP_STATUS_ADDR, 0 << 30)

    @async
    def reset_sfp(self):
        self.logger.warning("%r: Temporarily disconnecting the SFP to reset the %s FPGA's UDP communication stack" % (self, self.hostname))
        yield self.set_pci_switch_direction.async('SEL_ARM')
        yield self.set_pci_switch_direction.async('SEL_SFP')

    @async
    def set_fpga_control_networking_parameters(
            self,
            fpga_mac_addr=None,
            fpga_ip_addr=None,
            fpga_port_number=_FPGA_CONTROL_BASE_PORT):
        """
        Set the FPGA listening UDP networking parameters for the FPGA's *incoming* control packets
        using the ARM SPI MMI link. Reply packets will be sent back over UDP transmit channel 0, i.e.
        back to the address and port from which the command originated..

        This method sets the FPGA's listening addresses while assuming that the FPGA's is in
        addressing mode "00" (the default addressing mode),  which means that the channel 0
        IP/PORT/MAC will be set through the ARM-FPGA SPI registers [#f1]_.

        Parameters:

            fpga_mac_addr (str): MAC address of the FPGA in the format
                'xx:xx:xx:xx:xx:xx, where 'xx' is a hex number'. If fpga_mac_addr is None, an
                arbitrary MAC address is created using the IP address to ensure its uniqueness.

            fpga_ip_addr (str):  Address at which the FPGA listens for commands. The address is in
                the format of 'a.b.c.d', where a,b,c and d are decimal numbers.

            fpga_port_number (int): Port on which the FPGA listens for commands. This port is 41000
                by default. It is independent from the port from which the host computer sends and
                receives packets, which can be any port.

        Returns:

            str: MAC address that was used/computed, in the form 'xx:xx:xx:xx:xx'

        This method sets up the FPGA's *incoming* traffic network parameters (in other words, the
        FPGA listening address). The  *outgoing* command reply packets are sent through either UDP
        transmit channels 0 or 1. The channel on which the *commands* are returned is set by the
        FPGA GPIO field CTRL_RPLY_IP_PORT_OFFSET. This is set to channel '0' by default, and should
        not be changed.

        UDP channel 0 sends packets with the following destination:

            * target mac address: Is hardwired to use the source MAC address of the last valid
              packet received
            * target ip address: Is hardwired to use the source IP of the last valid IP packet
              received
            * target port number: Is set in the in the lower 16-bits of the SPI register
              _REMOTE_IP_PORT_ADDR. The default is a value of 0, meaning that the target port will
              be the source port number of the last valid received packet. This default should
              normally not be changed. There is no direct method provided in this class to override
              the default. This method does not affect this parameter.

        With the default configuration, the user can bind a UDP socket to any port (or let the
        system choose by specifying port 0 to the bind() method).  The outgoing command packets will
        have the source IP and source port set to that value, and reply packets will automatically
        come back to this port without having to set up the return port manually. Letting the socket
        choose ports allows the system to easily connect to multiple boards without having to ensure
        the availability of specific ports. Note that is might be necessary to bind the socket to a
        specific interface address in case there are multiple interfaces in the system.


        UDP Channel 1 is generally used to send back data to the host computer. See
        `set_data_target_address` for a decription of that channel.

        ----------


        _[#f1] '00' is the default FPGA addressing mode. Other modes are designed to allow setting
        the FPGA networking address without the help of the ARM processor and are not used. The
        addressing mode is changed by causing a rising edge on the GPIO registers TARGET_LOAD while
        TARGET_FPGA_SERIAL_NUMBER matches the serial number of the FPGA. This is a feature meant to
        allow ARM-less configuring of the FPGA through broadcasting, but we don't use it here since
        it's much easier and reliable to go through the ARM, which can get its networking parameters
        automatically through DHCP.


        """

        ip_packed = socket.inet_aton(fpga_ip_addr)  #

        # Compute a MAC address for the FPGA
        # mac = socket.inet_aton(self._get_arm_mac())  #
        if fpga_mac_addr is None:
            mac_packed = struct.pack('>H4s', 0x1234, ip_packed)
            fpga_mac_addr = ':'.join(['%02X' % ord(c) for c in mac_packed])
        else:
            mac_packed = [chr(int(s, 16)) for s in fpga_mac_addr.split(':')]

        # self.fpga_mac_addr = fpga_mac_addr
        # self.fpga_port_number = fpga_port_number
        # self.fpga_ip_addr = fpga_ip_addr

        # Set the FPGA Networking parameters over the ARM-FPGA SPI interface
        yield self.fpga_mmi_write.async(self._FPGA_MAC_ADDR_LSW_ADDR, struct.unpack('>I', mac_packed[2:6])[0])
        yield self.fpga_mmi_write.async(self._FPGA_MAC_ADDR_MSW_IP_PORT_ADDR, (struct.unpack('>H', mac_packed[0:2])[0] << 16) | fpga_port_number)
        yield self.fpga_mmi_write.async(self._FPGA_IP_ADDR_ADDR, struct.unpack('>I', ip_packed)[0])

        async_return(fpga_mac_addr)

    @async
    def set_local_data_port_number(self, port):
        """
        Sets the port number to which the FPGA is sending the data for UDP channel 1. Use
        `set_data_target_address' instead.

        Parameters:

            port (int); target port number. If port==0, the data is sent to the source port number of the last received valid packet  + 1.

        Does not change the target MAC or IP address.
        """
        word = yield self.fpga_mmi_read.async(self._REMOTE_IP_PORT_ADDR)
        yield self.fpga_mmi_write.async(self._REMOTE_IP_PORT_ADDR, (word & 0xFFFF) | (port << 16))

    @async
    def get_local_data_port_number(self):
        """ Return the port number to which the FPGA is sending its captured data stream on the control network.
        """
        async_return((yield self.fpga_mmi_read.async(self._REMOTE_IP_PORT_ADDR)) >> 16)

    @async
    def set_data_target_address(self, ip_addr=None, port=None, mac_addr=None):
        """
        Sets the IP address, port number and MAC address to which data is sent back to the host comptuter.

        This method sets the target address for data sent back to the host computer through the UDP
        channel 1.

        UDP Channel 1 (a.k.a the data channel) is generally used to send data back to the host
        computer through the control Ethernet interface but on a different port. This channel is
        usually used to send low-bandwidth data, and is not to be confussed with dedicated data
        channels such as the 10G Ethernet links to GPU nodes.

        The GPIO fields `DATA_IP_PORT_OFFSET` and `CORR_IP_PORT_OFFSET` set on which UDP channels is
        sent the data generated by the raw data capture (PROBER) or correlator (CORR44) subsystems,
        respectively. Both are set to UDP Channel 1 by default.

        UDP Channe1 1 target address is set and behaves differnetly than UDP channel 0, and is
        networking parameters are set by the method parameters `ip_addr`, `port` and `eth_addr`.

        Parameters:

            ip_addr (str):  Destination IP Address to which the FPGA send the UDP Channel 1 packets. The address is
                in the format of 'a.b.c.d', where a,b,c and d are decimal numbers. If `ip_addr` is
                '0.0.0.0' or None, then data will be sent back to the source address of the last
                valid received control packet.

            port (int): Destination port to which the FPGA sends UDP Channel 1 packets. If `port` is
                0, the packets will be sent to the the source port of the last valid received
                control packet **plus one**.

            mac_addr (str): Destination MAC address to which the FPGA will sends UDP Channel 1
                packets. It is in the format 'xx:xx:xx:xx:xx:xx, where 'xx' is a hex number'. If
                fpga_mac_addr is None or '00:00:00:00:00:00', the packets will be sent to the the
                source MAC address of the last valid received control packet.

        After initializarion, all three parameters are set to zero, meaning that if the FPGA
        receives control packets from port x, data will be sent back to the same host on port x+1.
        This implies that the host was able to allocate two consecutive UDP port addresses for
        control and data sockets. It is usually easier to let the operating system assign a random
        port and set that pot number explicitely with a non-zero value.


        The Channel 1 addressing described above is valid for addressing mode '00' (the only mode
        available to this module, see [#f1]) In this mode, the IP address and MAC address are set by
        the GPIO registers TARGET_MAC_ADDR and TARGET_IP_ADDR. The port number is set by the lower
        16 bits of the SPI register _REMOTE_IP_PORT_ADDR  (GPIO's TARGET_IP_PORT is *not* used). The
        Channel 1 destination addresses are set differently in other addressing modes.

        """
        if not ip_addr:
            ip_addr_int = 0
        else:
            ip_addr_int = struct.unpack('>L',socket.inet_aton(ip_addr))[0] # Ip address, as an integer

        if not mac_addr:
            mac_addr_int = 0
        else:
            mac_addr_int = sum(int(s, 16) << (8 * i) for i, s in enumerate(reversed(mac_addr.split(':'))))

        self.logger.debug('%r: setting data target address to ip=%r(%r), port=%r(%r), mac=%r(%r)' % (self, ip_addr, ip_addr_int, port, port, mac_addr, mac_addr_int))
        # Set the UDP transmit channel 1 IP and MAC addresses
        self.core_gpio.TARGET_MAC_ADDR = mac_addr_int
        self.core_gpio.TARGET_IP_ADDR = ip_addr_int

        # Set the UDP  Channel 1 outgoing packet destination port number, on the ARM-FPGA SPI registers
        word = yield self.fpga_mmi_read.async(self._REMOTE_IP_PORT_ADDR)
        yield self.fpga_mmi_write.async(self._REMOTE_IP_PORT_ADDR, (word & 0xFFFF) | (port << 16))




    @async
    def get_fpga_firmware_cookie(self, resync=False):
        """
        Get the FPGA firmware cookie.

        Reads the FPGA over the UDP link and returns the cookie that
        identifies the firmware. This method can be called before any FPGA
        modules are instatiated.

        If ``resync`` is True, the read command will reset the command
        sequence number to the value known by the FPGA. This should be is used
        by the first command sent to the FPGA to reset the communication link.
        """
        yield async_moment
        async_return((self.mmi_read(self._GPIO_COOKIE_REG, resync=resync) & 0x7F))

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

    @async
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
            data = yield self._mezzanine_eeprom_read_base64.async(mezzanine)
            async_return(base64.decodestring(data))
        except (tuber.TuberRemoteError, AttributeError):
            self.logger.debug("%r: Cannot read the Mezzanine %i EEPROM through the ARM's _mezzanine_eeprom_read_base64() method. Attempting to read the Mezzanine EEPROM through the FPGA." % (self, mezzanine))

        fpga_programmed = yield self.is_fpga_programmed.async()
        if not fpga_programmed:
            self.logger.debug("%r: FPGA is not programmed, so cannot read the Mezzanine %i EEPROM through the FPGA." % (self, mezzanine))
            async_return(None)

        eeprom_data = self.hw.read_mezzanine_eeprom(mezzanine, 0, 1)
        if ord(eeprom_data[0]) == 0x0d:  # if this is McGill format
            self.logger.debug("%r: EEPROM in Mezzanine %i is McGill format. The FPGA will be reading only bytes until the terminator character. " % (self, mezzanine))
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
            async_return(string)
        else:  # If not McGill format,
            self.logger.debug("%r: EEPROM in Mezzanine %i is not McGill format. The FPGA will *NOT* read the EEPROM contetnt " % (self, mezzanine))
            async_return(None)

    # ---------------------------------------------------------
    # IRIG-B time support methods
    # ---------------------------------------------------------



    class _IrigTimestamp(object):
        """ Represents the date/time that is obtained from and sent to th IRIG-B subsystem down to a 10 ns resolution.ns

        The class is based on ``Datetime``, and extends it with additional
        methods to support various additional time formats and sipport the
        increased time resolution. Standard datetime functions can be used but
        are limited to the microsecond resolution.

        time is represented as an integer number of nanoseconds (``nano``) since the UTC
        epoch (1 Jan 1970 00:00:00 UTC). Since Python intergers have an
        infinite amount of resolution, we can represent the time to with a
        nanosecond accurary without loss of precision.

        The object is also used to store low-level IRIG-B-related information.

        """
        _IRIGB_TIME_FORMAT = {
            'raw': lambda ts: ts,
            'datetime': lambda ts: ts.datetime,
            'nano' : lambda ts: ts.nano,
            'datetime+': lambda ts: (ts.datetime, ts.nano % 1000000000)
            }

        nano = None # time in nanoseconds since epoch.

        def __str__(self):
            return self.isoformat()

        def __init__(self, arg=None):
            """
            """
            if arg is None:
                pass
            elif isinstance(arg, basestring):
                if arg.lower() == 'now':
                    self.nano = self.datetime_to_nano(datetime.now())
                else:
                    raise AttributeError('Cannot convert string to nano time')
            elif isinstance(arg, datetime):
                    self.nano = self.datetime_to_nano(arg)
            elif isinstance(arg, int):
                    self.nano = arg

        def datetime_to_nano(self, d, nano_offset=0):
            """
            """
            return int(timegm(
                (d.year, d.month, d.day,
                 d.hour, d.minute, d.second + d.microsecond / 1e6)) * 1e9)

        def nano_to_datetime(self, nano):
            return datetime(1970, 1, 1) + timedelta(seconds=nano/1e9)

        def isoformat(self):
            n = self.datetime
            # COnvert into an ISO time string with more second resolution.self.
            #
            # Note that to obtain the fractional time, we cannot do
            # ``(nano/1e9) %1``, as ``nano/1e9`` is represented as a float and
            # does not have enough resolution to properly represent
            # nanoseconds. We have to do integer math to extract the subsecond
            # offset, then confert it to float with ``(nano % 1000000000) /
            # 1e9 ``.
            return '%04i-%02i-%02i%s%02i:%02i:%02.9f' % (
                n.year,
                n.month,
                n.day,
                'T',
                n.hour,
                n.minute,
                n.second + (self.nano % 1000000000) / 1e9)  # See note above


        def astype(self, format):
            return self._IRIGB_TIME_FORMAT[format](self)

    _IRIGB_SOURCE_TABLE = OrderedDict([
        ('bp_trig', 0),
        ('bp_time',  1),
        ('irigb_gen',  2),
        ('bp_gpio_int',  3)
        ])

    @async
    def set_irigb_source(self, source):
        """ Set the source of the IRIG-B signal."""
        if source not in self._IRIGB_SOURCE_TABLE:
            raise ValueError('Invalid IRIG-B source name. Valid names are %s' % ', '.join(self._IRIGB_SOURCE_TABLE.keys()))
        w2 = yield self.fpga_mmi_read.async(self._IRIGB_SAMPLE2_ADDR)
        self.fpga_mmi_write(self._IRIGB_SAMPLE2_ADDR, (w2 & 0x3FFFFFFF) | (self._IRIGB_SOURCE_TABLE[source] << 30))

    @async
    def get_irigb_source(self):
        """ Get the name of the current source of the IRIG-B signal."""
        source = (yield self.fpga_mmi_read.async(self._IRIGB_SAMPLE2_ADDR)) >> 30

        for (source_name, source_number) in self._IRIGB_SOURCE_TABLE.items():
            if source == source_number:
                async_return(source_name)
        raise ValueError('The IRIG-B module has an unknown source')

    @async
    def detect_irigb_source(self, set_source=False):
        """ Returns the name of the first input on which valid IRIG-B time is detected. """
        old_source = yield self.get_irigb_source.async()
        valid_source = None
        for source in self._IRIGB_SOURCE_TABLE.keys():
            yield self.set_irigb_source.async(source)
            if (yield self.get_irigb_time.async(noerror=True)):
                valid_source = source
                break
        yield self.set_irigb_source.async(valid_source if set_source else old_source)
        async_return(valid_source)

    @async
    def _get_irigb_time(self, trig=True, noerror=False):
        """ Reads the Reference clock and IRIG-B time and returns an object
        that contains all the time information gathered from it. Optionally
        trigger the capture of a new reference clock & IRIG-B time if `trig`
        is True.

        Parameters:

            trig (bool): if trig=True, a trigger is generated to capture the
                time on the rising edge of the next 10 MHz reference clock.
                Otherwise, the last captured time is returned. In this last
                case, it is assumed that some trigger was performed manually
                or automatically (by a SYNC event, for example)

            noerror (bool): Suppress the raising of error in the case the
                IRIG-B time is not valid (i.e not updated or has invalid
                values)


        Returns:

            an _IrigTimestamp object which contains the captured.


        Notes:

            The retuned time is advanced by one second to account for the fact
            that the IRIG-B time decode by the IRIG-B FPGA logic is latched on the
            beginning of the following second. However, the pipelining delay
            offsets not included.


        """

        # if format not in self._IRIGB_TIME_FORMAT:
        #     raise ValueError('Invalid time format. Valid formats are: %s' % (', '.join(self._IRIGB_TIME_FORMAT.keys())))
        # ts = self._IrigTimestamp()


        # Optionally trigger time capture, and check that the IRIG-B time is
        # captured AND to be valid. IF we trig, try to get a valid time until
        # a timeout has elapsed, otherwise fail immediately if the time was
        # not recently updated.
        t0 = time.time()
        while True:

            # Trigger the capture of the reference counter and IRIG-B time
            if trig:
                w2 = yield self.fpga_mmi_read.async(self._IRIGB_SAMPLE2_ADDR)
                # trigger time capture by creating a rising edge on `refclk_sample_trig`
                yield self.fpga_mmi_write.async(self._IRIGB_SAMPLE2_ADDR, w2 & ~(1 << 29))
                yield self.fpga_mmi_write.async(self._IRIGB_SAMPLE2_ADDR, w2 | (1 << 29))
                yield async_moment

            # Wait for the time capture to complete by monitoring
            # `refclk_sample_done`. Time is captured on the next 10 MHz
            # reference clock edge, so that should be quick, but we need
            # to make sure we have stable values. Timeout if it takes too long.
            t1 = time.time()
            # JFC: Commented out  until I fix the firmware
            # while not (yield self.fpga_mmi_read.async(self._IRIGB_TARGET1_ADDR)) & (1 << 29): # check refclk_sample_done
            #     self.logger.warn('%r: Time capture was not immediately ready - this is unexpected' % self)  # Debug. should not happen since capture should be much faster than the time it takes to read the done flag
            #     # TImeout if it takes too long. The time should be ready within a few 10 MHz cycles.
            #     if time.time() - t1 > 0.1: # 0.1s = 1,000,000 clock cycles of the 10 MHz clock. That is way enough
            #         raise RuntimeError('Timeout while waiting for the reference clock counter and IRIG-B time capture to complete. Was the capture triggered?')

            # # At this point we have a stable IRIG-B timestamp ready to be read,
            # but we still don't know if the time within it is valid.

            # check the time valid (recent) flag
            w1 = yield self.fpga_mmi_read.async(self._IRIGB_SAMPLE1_ADDR) # this will also be used later
            recent = (w1 >> 29) & 1
            # If we get a updated time, we're good: exit the loop
            if recent:
                break
            # If we don't have a valid timestamp, and have been waiting for
            # too long or are not allowed to trig to re-check the time, then
            # raise an error unless instructed not to.
            if not trig or time.time() - t0 > 2.5: # Wait a little bit more than one second in case the IRIG-B signal just became valie (e.g. we just set the source)
                if noerror:
                    async_return(None)
                else:
                    raise RuntimeError('%.32r: Could not get a recently updated IRIG-B time. Check your cabling and the IRIG-B source selection.' % self)

        ts = self._IrigTimestamp()
        ts.system_time_before = time.time()

        ts.refclk_counter = yield self.fpga_mmi_read.async(self._IRIGB_REFCLK_SAMPLE)

        # Read the (other) IRIG-B capture registers. We assume nobody is callung
        # concurrent instances at the same same time, so we do this
        # asynchronously because each read is slow.
        w0 = yield self.fpga_mmi_read.async(self._IRIGB_SAMPLE0_ADDR)
        # w1 was read when checkiing for a valid IRIG-B capture
        w2 = yield self.fpga_mmi_read.async(self._IRIGB_SAMPLE2_ADDR)

        # check if the time is valid
        # t0 = self.fpga_mmi_read(self._IRIGB_TARGET0_ADDR)
        # t1 = self.fpga_mmi_read(self._IRIGB_TARGET1_ADDR)
        # t2 = self.fpga_mmi_read(self._IRIGB_TARGET2_ADDR)
        # e0 = self.fpga_mmi_read(self._IRIGB_EVENT_CTR_ADDR)

        ts.system_time = time.time()

        # Extract time from IRIG-B capture registers Get year and day of year.
        # Override for debugging or misbehaviored IRIG-B source (like our
        # CoolRunner generator board)
        if self.zero_target_irigb_year_and_day:
            ts.y = 0
            ts.d = 1
        else:
            ts.y = (w0 >> 0) & ((1 << 8) - 1)  # year from 0 to 99. Assumes a base year of 2000.
            ts.d = (w1 >> 20) & ((1 << 9) - 1)
        ts.h = (w1 >> 14) & ((1 << 6) - 1)
        ts.m = (w1 >> 7) & ((1 << 7) - 1)
        ts.s = (w1 >> 0) & ((1 << 7) - 1)
        ts.ss = (w2 >> 0) & ((1 << 28) - 1)
        ts.pps = (w0 >> 26) & ((1 << 6) - 1)
        ts.sbs = (w0 >> 8) & ((1 << 18) - 1)  # "straight binary seconds" since 00:00 on the current day (0-86399, not BCD). Not necessarily supported by the GPS.
        ts.source = (w1 >> 30) & ((1 << 2) - 1)
        ts.recent = recent
        # ts.before_target = (t1 >> 31) & 1
        # ts.done = (t1 >> 30) & 1

        if not noerror:
            if ts.h > 23 or ts.m > 59 or ts.s > 60:
                raise RuntimeError('Invalid IRIG-B time value %ih %im %is.' % (ts.h, ts.m, ts.s))

            if ts.d < 1 or ts.d > 366:
                raise RuntimeError('Invalid IRIG-B day value %i. Day-of-year must be between 1 and 366' % ts.d)

        # Compute a datetime object, one second in the future. We use
        # timedelta because ts.d > 31, and ts.s may be > 59 because of the
        # added second and possibly leap seconds
        ts.datetime = dt = (datetime(ts.y + 2000, 1, 1) +
            timedelta(days=ts.d - 1, hours=ts.h, minutes=ts.m,
                      seconds=ts.s + 1, microseconds=ts.ss // 100))
        # compute a timestamp, one second in the future
        timestamp = timegm((ts.y + 2000, 1, ts.d, ts.h, ts.m, ts.s + 1))  # unix timestamp = seconds since 1 Jan 1970 UTC
        ts.nano = int(timestamp * 1e9) + ts.ss * 10  # nanoseconds since 1 Jan 1970 UTC
        ts.time = ts.nano / 1e9  # Unix timestamp, as a float with as much resolution as the float can provide (not necessarily to the nanosecond)
        # Compute an modified time structure (a tuple) that contains the time
        # elements including fractional nicroseconds
        tt = time.gmtime(timestamp)
        ts.time_struct = [tt.tm_year, tt.tm_mon, tt.tm_mday, tt.tm_hour,
            tt.tm_min, tt.tm_sec, (ts.nano % 1000000000) / 1000.0]

        # alternate of computing nano, to check if is is ok to pass days>31 and seconds>59 to timegm.
        ts.nano2 = (int(timegm((ts.y + 2000, 1, 1, 0, 0, 0)) * 1e9) +
                    ((ts.d - 1) * 24 * 3600 +
                     ts.h * 3600 +
                     ts.m * 60 + ts.s + 1) * 1000000000 +
                    ts.ss * 10)

        ts.time2 = ts.nano2 / 1e9
        ts.time_struct2 = [dt.year, dt.month, dt.day, dt.hour, dt.minute,
                dt.second, (ts.nano2 % 1000000000) / 1000.0]
        # ts.event_ctr = e0

        if not (ts.nano == ts.nano2 and
                ts.time == ts.time2 and
                ts.time_struct == ts.time_struct2):
            self.logger.error("%r: IRIGB time computation error" % self)
        async_return(ts)

    @async
    def set_irigb_trigger_time(self, datetime_=None, delay=None):
        """ Sets the time at which the IRIG-B module will generate a trigger
        that can be used to synchronize boards.

        Parameters:

            datetime_ (datetime): the base target time in the Python as a 'datetime' object.

            delay (float): is a time offset in seconds that is added to
            `datetime_` so set the target time. It defaults to zero.

        Returns:

            _IrigTimestamp object: contains the programmed trigger time expressed as a
                `datetime` object (.datetime) and in nanoseconds since epoch (.nano).

        If the trigger is used for synchronizing boards, the delay should be a
        multiple of 100 ns in order to ensure alignment with the 10 MHz
        reference clock and ensure deterministic start of the syncronization
        state machine.

        If 'datetime_' and 'delay' are None, the trigger time is set 3 seconds
        after the current time (as returned by the board).


        """
        if datetime_ is None:
            ts = self._get_irigb_time(trig=True)  # do this synchronously to we get an accurate time
            dt = ts.astype('datetime')
            self.logger.debug('%r: Current IRIGB time is %s' % (self, ts.isoformat()))
            if delay is None:
                delay = 3
        else:
            dt = datetime_
            if delay is None:
                delay = 0

        nano_delay = int(delay * 1e9) % 1000  # Get submicrosecond delay in nanosecond units
        delay = int(delay * 1e6) / 1e6  # Round delay to the microsecond
        dt += timedelta(0, delay) # add delay in integer microseconds (datetime does not support more than the microsecond accuracy)
        self.logger.debug('%r: Setting IRIGB target time to %s + %3i ns' % (self, dt.isoformat(), nano_delay))
        if self.zero_target_irigb_year_and_day:
            y = 0
            d = 0
        else:
            y = dt.year % 100
            d = (dt - datetime(dt.year, 1, 1)).days + 1
        h = dt.hour
        m = dt.minute
        s = dt.second
        ss = dt.microsecond * 100 + int(nano_delay / 10)

        self.logger.debug('%r: Setting IRIGB target time with y=%i, d=%i, h=%i, m=%i, s=%i, ss=%i' % (self, y, d, h, m, s, ss))

        t0 = (y << 0)
        t1 = (d << 20) | (h << 14) | (m << 7) | (s << 0)
        t2 = (1 << 31) | (ss << 0)

        yield self.fpga_mmi_write.async(self._IRIGB_TARGET0_ADDR, t0)
        yield self.fpga_mmi_write.async(self._IRIGB_TARGET1_ADDR, t1)
        yield self.fpga_mmi_write.async(self._IRIGB_TARGET2_ADDR, t2)

        # return the time at which the sync event is scheduled for
        ts = self._IrigTimestamp()
        ts.datetime = dt
        ts.nano = int(timegm((2000 + y, 1, d, h, m, s + 1)) * 1e9) + ss*10
        async_return(ts)

    @async
    def is_irigb_before_trigger_time(self):
        """ Is true if the current IRIGB is before the target trigger time that was previously set-up.
        """
        t1 = yield self.fpga_mmi_read.async(self._IRIGB_TARGET1_ADDR)
        async_return(bool((t1 >> 31) & 1))

    @async
    def capture_frame_time(self, trig=True, format='nano', timeout=5):
        """ Captures the IRIG-B of the first sample of the next frame coming
        out of the ADC data acquisition module.

        The returned time is (frame_number, time), where frame_number is the
        number of the frame that was measured, and time is the IRIG-B time in
        a format specified by 'format' (see get_irigb_time() for available
        formats).

        NOTE1: floats do not have enough resolution to represent the current
        time down to nanoseconds (as opposed to Python int's which have
        infinite resolution), so beware of conversions.

        NOTE2: The method will generate a timeout error if there is no data
        coming out of the data acquisition module.

        NOTE3: There is a delay between the time the first sample of a packet
        is taken and the time the packet comes out of the data acquistion
        module (due to initial dropping of a few frames and the FIFO filling-
        delay).
        """
        if trig:
            w2 = yield self.fpga_mmi_read.async(self._IRIGB_SAMPLE2_ADDR)
            yield self.fpga_mmi_write.async(self._IRIGB_SAMPLE2_ADDR, w2 & ~(1 << 28))
            yield self.fpga_mmi_write.async(self._IRIGB_SAMPLE2_ADDR, w2 | (1 << 28))
            t0 = time.time()
            while not (yield self.fpga_mmi_read.async(self._IRIGB_TARGET1_ADDR)) & (1 << 30):
                    if time.time() - t0 > timeout:
                        raise RuntimeError('Timeout while waiting for a Frame. Is data flowing out of the ADC data acquisition module?')
        event_number = yield self.fpga_mmi_read.async(self._IRIGB_EVENT_CTR_ADDR)
        event_number += (yield self.fpga_mmi_read.async(self._IRIGB_EVENT_CTR_ADDR2)) << 32

        ts = yield self._get_irigb_time.async(trig=0)  # The event trigger will automatically trig IRIGB
        captured_time = ts.astype(format)
        async_return((event_number, captured_time))

    @async
    def get_frame_number(self):
        """
        Return the number of the next frame passing through the system.
        """
        w2 = yield self.fpga_mmi_read.async(self._IRIGB_SAMPLE2_ADDR)
        yield self.fpga_mmi_write.async(self._IRIGB_SAMPLE2_ADDR, w2 & ~(1 << 28))
        yield self.fpga_mmi_write.async(self._IRIGB_SAMPLE2_ADDR, w2 | (1 << 28))
        t0 = time.time()
        while not (yield self.fpga_mmi_read.async(self._IRIGB_TARGET1_ADDR)) & (1 << 30):
            if time.time() - t0 > 1:
                raise RuntimeError('Timeout while waiting for a Frame. Is data flowing out of the ADC data acquisition module?')
        event_number = yield self.fpga_mmi_read.async(self._IRIGB_EVENT_CTR_ADDR)
        async_return(event_number)

    @async
    def capture_refclk_time(self, trig=True, format='nano'):
        """ Measures the time at which the next 10MHz reference clock rising
        edge occurs.

        The method returns the reference clock edge number (from a 32-bit
        counter that wraps around) and the time in the specified format (see
        get_irigb_time() for available formats).

        This method can be used to measure the drift of the 10 MHz reference clock relative to
        the IRIG-B time.
        """
        ts = yield self._get_irigb_time.async(trig=trig)
        async_return((ts.refclk_counter, ts.astype(format)))  # Return

    @async
    def get_irigb_time(self, trig=True, format='datetime', noerror=False):
        """ Return the current time as decoded on the IRIG-B input. The time
        is returned in a format specified by 'format':

        'raw': A object containing all the data fields read directly from the IRIG-B decoder and preprocessed datetime and nano values
        'datetime': Python 'datetime' object (with a microsecond resolution)
        'datetime+': A (dt,nano) tuple where dt is a datetime object, and nano is the number of nanoseconds within the second.
        'nano': An integer representing the number of nanoseconds since Jan 1st 2000.
        """
        ts = yield self._get_irigb_time.async(trig=trig, noerror=noerror)
        async_return(ts.astype(format))


class I2CInterface(object):
    """
    This class wraps all that is needed to access an I2C device in
    a standardized way, whether the access is done through the
    FPGA or through the ARM.
    """

    I2CException = IOError  # Exception object to expect from I2C communication errors

    def __init__(self, write_read_fn, port_select_fn, bus_table, switch_addr, parent=None, verbose=None):
        self.parent = parent
        self.write_read_fn = write_read_fn
        self.set_port_fn = port_select_fn
        self._I2C_BUS_LIST = bus_table

        self._i2c_switch = tca9548a.tca9548a(self, switch_addr)
        self._logger = logging.getLogger(__name__)

    def __repr__(self):
        """ Return a string representation of this I2C interface.

        Returns:

            A string which include the parent object id.

        """
        return "%s(%r)" % (self.__class__.__name__, self.parent)

    def select_bus(self, bus_names, *args, **kwargs):
        """
        Configure the I2C port and I2C switch so the following
        communications will access the desired I2C bus. 'bus_id'
        can be a bus name or bus number, or a list of those if
        multiple buses are to be accessed at the same time. An
        error will be provided if all the buses are not accessible
        through the same FPGA I2C port. This function assumes that
        each FPGA I2C port has an identical I2C switch.

        Parameters:

            bus_names (str, int, or list of str or int): Name or number of the
                I2C bus to enable on the I2C switch. Multiple buses can be
                enabled at one time.

            args, kwargs: passed to the bus select function

        Exceptions:

            IOError: Raised by an FPGA-based I2C controller in case of transaction errors

        """
        if isinstance(bus_names, (str, int)):
            bus_names = [bus_names]
        selected_fpga_port_number = None
        selected_switch_port_numbers = []
        for bus_name in bus_names:
            if bus_name not in self._I2C_BUS_LIST:
                self._logger.error("%r: I2C bus '%s' is not part of the available buses. Valid values are %s" % (self, bus_name, ','.join(str(self._I2C_BUS_LIST.keys()))) )
            (fpga_port_number, switch_port_number) = self._I2C_BUS_LIST[bus_name]
            if selected_fpga_port_number is None:
                selected_fpga_port_number = fpga_port_number
            elif selected_fpga_port_number != fpga_port_number:
                self._logger.error("%r: I2C bus '%s' is not on the same FPGA port as the other buses" % (self, bus_name) )
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

        Parameters:
            See `I2C.write_read`

        Returns:
            See `I2C.write_read`

        Exceptions:

            IOError: Raised by an FPGA-based I2C controller in case of transaction errors

        """
        # self._logger.debug("Accessing I2C bus...")
        return self.write_read_fn(*args, **kwargs)

    def is_present(self, addr, bus_name=None):
        """ Test the presence of an I2C device at the specified address.

        Parameters:

            addr (int): I2C address of the device to query

            bus_name (str, int, or list of str or int): I2C bus(es) to activate

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
    _POWER_ICE12V0_I2C_ADDR   = 0b1000111 #0x47
    # _POWER_ICE5V0_I2C_ADDR    = 0b1001000 #0x48
    # _POWER_ICE3V3_I2C_ADDR    = 0b1001001 #0x49
    # _POWER_ICE1V5_I2C_ADDR    = 0b1001100 #0x4C
    # _POWER_ICE1V2_I2C_ADDR    = 0b1001101 #0x4D
    _POWER_ICE1V0_I2C_ADDR    = 0b1001110 #0x4E
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
        self._logger.debug('%r: Initializing Iceboard hardware' % iceboard)
        self._iceboard = iceboard
        self._i2c = self._iceboard.i2c
        self._logger.debug('%r: Instantiating Motherboard EEPROM managers' % self._iceboard)
        self._motherboard_eeprom_data = eeprom.eeprom(self._i2c, self._MOTHERBOARD_EEPROM_DATA_ADDR, 'GPIO', self._MOTHERBOARD_EEPROM_ADDR_WIDTH, self._MOTHERBOARD_EEPROM_PAGE_SIZE)
        self._motherboard_eeprom_serial = eeprom.eeprom(self._i2c, self._MOTHERBOARD_EEPROM_SERIAL_ADDR, 'GPIO', self._MOTHERBOARD_EEPROM_ADDR_WIDTH, self._MOTHERBOARD_EEPROM_PAGE_SIZE)

        self._logger.debug('%r:  Instantiating FMC EEPROM managers' % self._iceboard)

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
            self._logger.debug('%r: Detected multipage EEPROM on FMCA. Assuming >16-bit addressing.' % self._iceboard)
            self._fmca_eeprom = eeprom.eeprom(self._i2c, self._FMC_EEPROM_ADDR, 'FMCA', self._MCGILL_FMC_EEPROM_ADDR_WIDTH, self._MCGILL_FMC_EEPROM_PAGE_SIZE)
        else:
            self._fmca_eeprom = eeprom.eeprom(self._i2c, self._FMC_EEPROM_ADDR, 'FMCA', self._FMC_EEPROM_ADDR_WIDTH, self._FMC_EEPROM_PAGE_SIZE)

        if self._i2c.is_present(self._FMC_EEPROM_ADDR+1, bus_name='FMCB'):
            self._logger.debug('%r: Detected multipage EEPROM on FMCB. Assuming >16-bit addressing.' % self._iceboard)
            self._fmcb_eeprom = eeprom.eeprom(self._i2c, self._FMC_EEPROM_ADDR, 'FMCB', self._MCGILL_FMC_EEPROM_ADDR_WIDTH, self._MCGILL_FMC_EEPROM_PAGE_SIZE)
        else:
            self._fmcb_eeprom = eeprom.eeprom(self._i2c, self._FMC_EEPROM_ADDR, 'FMCB', self._FMC_EEPROM_ADDR_WIDTH, self._FMC_EEPROM_PAGE_SIZE)

        self._FMC_EEPROM_TABLE = {
            1: self._fmca_eeprom,
            2: self._fmcb_eeprom
            }


        self._logger.debug('%r: Instantiating I2C GPIO manager' % self._iceboard)
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

        self._qsfpa = qsfp.QSFP(self._i2c, bus_name='QSFPA', gpio_prefix='QSFPA_', gpio=self._gpio, parent=self)
        self._qsfpb = qsfp.QSFP(self._i2c, bus_name='QSFPB', gpio_prefix='QSFPB_', gpio=self._gpio, parent=self)

        self.qsfp = Ccoll((self._qsfpa, self._qsfpb))
        # self._logger.info(' Instantiating I2C temperature sensors')
        # self._tmp_power = tmp100.tmp100(self._i2c, self._TMP_POWER_I2C_ADDR, 'GPIO')
        # self._tmp_phy = tmp100.tmp100(self._i2c, self._TMP_PHY_I2C_ADDR, 'GPIO')
        # self._tmp_fpga = tmp100.tmp100(self._i2c, self._TMP_FPGA_I2C_ADDR, 'GPIO')
        # self._tmp_arm = tmp100.tmp100(self._i2c, self._TMP_ARM_I2C_ADDR, 'GPIO')

        # self._logger.info(' Instantiating I2C current/power monitors')
        # self._power_ice_3v3 = ina230.ina230(self._i2c, self._POWER_ICE3V3_I2C_ADDR, 'SMPS')
        self._power_ice_12v0 = ina230.ina230(self._i2c, self._POWER_ICE12V0_I2C_ADDR, 'SMPS')
        # self._power_ice_5v0 = ina230.ina230(self._i2c, self._POWER_ICE5V0_I2C_ADDR, 'SMPS')
        # self._power_ice_1v0_gtx = ina230.ina230(self._i2c, self._POWER_ICE1V0GTX_I2C_ADDR, 'SMPS')
        # self._power_ice_vadj = ina230.ina230(self._i2c, self._POWER_ICEVADJ_I2C_ADDR, 'SMPS')
        # self._power_ice_1v2 = ina230.ina230(self._i2c, self._POWER_ICE1V2_I2C_ADDR, 'SMPS')
        # self._power_ice_1v5 = ina230.ina230(self._i2c, self._POWER_ICE1V5_I2C_ADDR, 'SMPS')
        self._power_ice_1v0 = ina230.ina230(self._i2c, self._POWER_ICE1V0_I2C_ADDR, 'SMPS')
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

    def __repr__(self):
        return "%r.%s" % (self._iceboard, self.__class__.__name__)

    @async
    def open(self):
        """
        """
        pass

    def close(self):
        self._logger.debug('Closing Iceboard hardware')
        if self._i2c:
            self._i2c = None

    @async
    def init(self):
        """Initializes the motherboard hardware to a known state.
        This will turn off FMC power.
        """
        yield async_moment
        self._init_gpio_expanders()
        # self._init_temperature_sensors()
        # self.set_fmc_power()
        # self._init_power_sensors()

        # Initialize the QSFPs. This sets the reset and LowPower mode. Some
        # QSFP+ modules (like the 3M AOCs) will not work without this.
        yield async_moment
        self._qsfpa.init()
        yield async_moment
        self._qsfpb.init()

    def _init_gpio_expanders(self):
        """
        Initializes GPIO expanders

        History

        140304 JM: created. todo: make more flexible for I/O pin configuration
        of each expander. Need to confirm I/O pin config with JF
        """
        self._gpio_power.init(cfg0_def=0b10101000,  # 1 = input, 0=output
                              cfg1_def=0b10101000,
                              out0_default=None,
                              out1_default=None,
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

    @async
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

                # Bits are:
                #  7: SFP_LOS/SFM_ModPrsn
                #  6: FMC_CLK_DIR
                #  5: FMC_PRSNT
                #  4: FMC_PG_C2M
                #  3: FMC_PG_M2C
                #  2: FMC_EN_VADJ
                #  1: FMC_EN_3V3
                #  0: FMC_EN_12V
                if fmc_state:
                    self._gpio_power.write(fmc, 0b00000010, mask=0b00000010)  # Turn on 12V, 3.3V and VADJ power to board
                    # self._gpio_power.write(fmc, 0b00000110, mask=0b00000110)  # Turn on 12V, 3.3V and VADJ power to board
                    yield async_sleep(0.010)
                    self._gpio_power.write(fmc, 0b00000100, mask=0b00000100)  # Turn on 12V, 3.3V and VADJ power to board
                    yield async_sleep(0.100)
                    self._gpio_power.write(fmc, 0b00000001, mask=0b00000001)  # Turn on 12V, 3.3V and VADJ power to board
                    yield async_sleep(0.050)
                    self._gpio_power.write(fmc, 0b01010000, mask=0b01010000)  # Set Power Good (start switcher) and CLKDIR to 1
                    yield async_sleep(0.050)
                else:
                    self._gpio_power.write(fmc, 0b00000000, mask=0b01010000)  # Stop mezzanine switcher (PG=0)
                    yield async_sleep(0.030)
                    self._gpio_power.write(fmc, 0b00000000, mask=0b00000001)  # Turn off 12V
                    yield async_sleep(0.030)
                    self._gpio_power.write(fmc, 0b00000000, mask=0b00000010)  # Turn off rail
                    yield async_sleep(0.030)
                    self._gpio_power.write(fmc, 0b00000000, mask=0b00000100)  # Turn off rail
                    yield async_sleep(0.100)


    @async
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

        yield async_moment
        for (led, led_state) in zip(led_name, state):
            self._gpio.write(led, led_state)

    @async
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

        async_return(led_status)

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
    #                     self._logger.info('%r: Temperature sensor %s failed to initialize.' % (self._iceboard, temp_sensor))

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
    #                 self._logger.info('%r: Power sensor %s failed to initialize.' % (self._iceboard, power_sensor))


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
