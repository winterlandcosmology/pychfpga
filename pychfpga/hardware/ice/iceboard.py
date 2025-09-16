""" Handler for the IceBoard's FPGA core UDP communication and hardware management firmware.
"""
# Standard Python packages
import time
import base64
import asyncio
import bz2
import subprocess
import shlex
import traceback
import os
# Pypi packages
import nest_asyncio

# External private packages

from wtl.metrics import Metrics

# Local packages
from pychfpga.common import Ccoll
from pychfpga.fpga_firmware import FPGAFirmware
from pychfpga.common import run_async, async_to_sync
from pychfpga.hardware import HardwareMap, Motherboard, Crate, Mezzanine
from pychfpga.hardware.interfaces import I2CInterface, BSB_MMI  # I2C and Byte-serial interface protocol definition

from .icecore.hardware_assets import TuberIceBoardBase
from .icecore.tuber import TuberError, TuberNetworkError, TuberRemoteError
from .icemezz import FMCMezzanine

# from lib import tmp100  # I2C Temperature sensor
from ..i2c_devices import pca9575  # I2C 16-bit IO Expander
from ..i2c_devices import tca9548a  # I2C switch
from ..i2c_devices import ina230  # I2C Voltage and current monitor
from ..i2c_devices import eeprom
from ..i2c_devices import qsfp
from ..i2c_devices import gpio


class IceBoard(Motherboard, TuberIceBoardBase):
    """ Provide the methods needed to operate the Iceboard hardware and its FPGA firmware.

    IceBoard is derived for the following superclasses:

    - Motherboard: Generic motherboard definition with hardware map management

    - TuberIceBoardBase: provides access to functions running
      on the IceBoard's ARM processor as if they were local methods. This is done using the `Tuber` protocol.

    Note: `Motherboard` has to be first in the parent class list otherwise access to undefined attributes will be passed to Tuber

    The IceBoard class adds:
        - Tuber-based Model, serial, slot, crate and mezzanine self discovery
        - Tuber-based method to configure the FPGA
        - Access to the core memory-mapped registers in the FPGA's firmware using the ARM-FPGA SPI link. The core registers are used to check the firmware CRC,  setup FPGA networking, and provide access to the IRIG-B module.
            (see `fpga_core_reg_read/write_async()` methods.
        - Alternate access to the board I2C peripherals through the FPGA, including:
            - LED control
            - GPIO input/output
            - Temperature sensors
            - Power monitoring for every rail (voltage,current)
            - Motherboard EEPROM read/write

    The IceBoard class also exposes any method provided by the FPGA firmware
    once it is programmed.  These methods are provided by the FPGAFirmware
    subclass that is set in self.fpga when the FPGA is configured through
    `set_fpga_bitstream_async()`.

    """

    # ------------------------------------
    # Hardware map parameters
    # ------------------------------------

    part_number = 'MGK7MB'  # Required to identify the part number associated with this class
    _ipmi_part_numbers = ['MGK7MB']  # Possible equivalent names found in IPMI records that correspond to this platform

    # ------------------------------------
    # IceBoard-specific class attributes
    # ------------------------------------

    NUMBER_OF_FMC_SLOTS = 2  # Number of FMC Mezzanines supported by this platform
    FMC_SLOT_NUMBERS = range(NUMBER_OF_FMC_SLOTS)  # Physical hardware slot numbers (0, 1)
    FMC_MEZZ_NUMBERS = (1, 2) # Logical mezzanine numbers (1, 2)


    port = 80  # port number on which to access the platform `hostname`. Tuber implicitly uses 80 due to the use of the http:// URL to access the board. But fpga_master needs that to prepare for TCP pings.


    # ------------------------------------
    # IceBoard Hardware-specific constants
    # ------------------------------------

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


    # Motherboard EEPROM AT24CS01 128-byte EEPROM with 128-bit serial number (data @ 0x57, serial @ 0x)
    _MOTHERBOARD_EEPROM_DATA_ADDR = 0x57  #
    _MOTHERBOARD_EEPROM_SERIAL_ADDR = 0x5F  #
    _MOTHERBOARD_EEPROM_ADDR_WIDTH = 7 # For the serial number, address must be 0b10xxxxxx
    _MOTHERBOARD_EEPROM_PAGE_SIZE = 8  #


    # FMC EEPROM, on FMCA or FMCB
    _FMC_EEPROM_ADDR = 0x50  # 0x50 (0x51 is also used for the 2nd page of large eeprom with address width > 16 bits).

    # Standard FMC EEPROM addressing scheme
    _FMC_EEPROM_ADDR_WIDTH = 7  # FMC EEPROM internal addresses are 7 bits wide.
    _FMC_EEPROM_PAGE_SIZE = 8  #

    # McGill's FMC EEPROM FMC EEPROM addressing scheme
    # McGill's FMC EEPROM internal addresses are is 17 bits wide. (2 bytes as data, 1 bit in lsb of I2C address)
    _MCGILL_FMC_EEPROM_ADDR_WIDTH = 17
    _MCGILL_FMC_EEPROM_PAGE_SIZE = 256  #


    # IO Expanders, on GPIO bus
    _GPIO_POWER_I2C_ADDR = 0b0100000  # 0x20
    _GPIO_SFP_QSFP_I2C_ADDR = 0b0100001  # 0x21
    _GPIO_SW_LEDS_ADDR = 0b0100010  # 0x22
    _GPIO_ARM_PHY_LEDS_ADDR = 0b0100011  # 0x23

    # # Temperature sensors, on GPIO bus
    # _TMP_ARM_I2C_ADDR   = 0b1001010 #0x4A
    # _TMP_PHY_I2C_ADDR   = 0b1001100 #0x4C
    # _TMP_FPGA_I2C_ADDR  = 0b1001011 #0x4B
    # _TMP_POWER_I2C_ADDR = 0b1001000 #0x48

    # # Power monitors, on SMPS bus
    # _POWER_ICEVADJ_I2C_ADDR   = 0b1000011 #0x43
    _POWER_ICE12V0_I2C_ADDR = 0b1000111  # 0x47
    # _POWER_ICE5V0_I2C_ADDR    = 0b1001000 #0x48
    # _POWER_ICE3V3_I2C_ADDR    = 0b1001001 #0x49
    # _POWER_ICE1V5_I2C_ADDR    = 0b1001100 #0x4C
    # _POWER_ICE1V2_I2C_ADDR    = 0b1001101 #0x4D
    _POWER_ICE1V0_I2C_ADDR = 0b1001110  # 0x4E
    # _POWER_ICE1V8_I2C_ADDR    = 0b1001011 #0x4B
    # _POWER_ICE1V0GTX_I2C_ADDR = 0b1001111 #0x4F

    # _POWER_FMCA12V0_I2C_ADDR  = 0b1000000 #0x40
    # _POWER_FMCA3V3_I2C_ADDR   = 0b1000001 #0x41
    # _POWER_FMCAVADJ_I2C_ADDR  = 0b1000010 #0x42

    # _POWER_FMCB12V0_I2C_ADDR  = 0b1000100 #0x44
    # _POWER_FMCB3V3_I2C_ADDR   = 0b1000101 #0x45
    # _POWER_FMCBVADJ_I2C_ADDR  = 0b1000110 #0x46

    # SSH options used to acess the IceBoard via ssh or scp
    # The ARM runs an old version of linux, so we need to downgrade the security protocols
    # we also disable host key check so we don't have an interactive prompt to accept a new host
    SSH_OPTIONS = '-o KexAlgorithms=diffie-hellman-group14-sha1 -o HostKeyAlgorithms=+ssh-dss -o StrictHostKeyChecking=no'

    def __init__(self,
                 hostname=None,
                 serial=None,
                 slot=None,
                 subarray=None,
                 **kwargs):
        """ Create a new IceBoard instance.

        One should generally use IceBoard.get_unique_instance(...) to obtain a
        unique instance of an IceBoard. Is there is an existing instance
        matching the specified hostname or serial number, that instance will
        be returned, otherwise a new instance will be created (through this `init()`) and will be returned.

        Parameters:

            hostname (str): hostname or IP address of the IceBoard ARM
                processor.  If `hostname` is None, methods provided by the
                IceBoard ARM processor (over Tuber) cannot be used on this
                instance. If `hostname` is 'None' but the instance has a
                serial number, then the board hostname can potentially be
                resolved using mDNS discovery.

            serial (str or int): Serial number of the board, in the exact
                format it is found on the IPMI info of the board. For
                convenience, if `serial` is an integer, it is converted into a
                properly formatted string serial.

            slot (int): slot number on a crate, or virtual slot number for
                crate-less standalone boards. Ranges from 1 to NUMBER_OF_SLOTS
                in the particular icecrate. A value of  0 or None (i.e.
                bool(slot) == False)  indicates that there is no slot
                information.

            subarray: Arbitrary value that is used to group boards in logical
                categories.

            kwargs (dict): Extra arguments that are stored in the instance for
                use in future initialization steps.

        Notes:

        `__init__` only creates the IceBoard instances and stores its key
        parameters for future use.  IceBoard objects are freely created and
        destroyed during the process of hardware map creation. `__init__`
        therefore shall not initiate communication with the hardware or
        perform any complex operations; this is done later by init_async()
        once the hardware map is completed and stable.
        """

        # print(f"IceBoard.__init__(): Creating {self.__class__.__name__}(serial={serial}, hostname={hostname}, slot={slot}, subarray={subarray}, kwargs={kwargs})")

        # First initialize the superclasses. The MRO provides the following  __init__ call sequence:
        #   1) Motherboard.__init__() is called first and gobbles all the arguments.
        #   2) TuberIceboardBase.__init__() is then called without arguments. This is ok: it does not expect any.
        # The superclasses define:
        #   logger
        #   hostname, serial, slot, subarray: initialized from the specified parameters
        #   extra_args : assigned any extra arguments captured in `kwargs`
        #   crate: set to None
        #   mezzanine: set to an empty dict
        #   fpga: set to None. Will be set to a FPGAFirmware instance when the FPGA is programmed.
        super().__init__(hostname=hostname, serial=serial, slot=slot, subarray=subarray, **kwargs)

        # Initialize key objects
        self.i2c = None # I2C interface object

        self._mezzanine_ipmi_cache = {1: None, 2: None}
        self._is_core_open = None
        self._is_hw_open = None

    async def ping_async(self, timeout=0.1):
        """ Checks if the platform responds to the target hostname address.

        For the Iceboard, we do this by checking if the Tuber object is available at
        the specified ARM hostname.

        This can be called before `open_platform_async()`, as we catch the errors thrown if the board does not respond.



        Parameters:

            timeout (float): [NOT IMPLEMENTED] How much time (in seconds) we shall wait for a response

        Returns:

            bool, which is True if the board has responded within the specified delay.

        Notes:

            - This could be implemented without Tuber, like is done in raw_acq
              (just a TCP connection), as this is a first low-level check for
              the board presence. It would then be easier to implement the
              timeout.
        """
        # print(f'{self!r} Ping_async()')
        self.logger.info('%r: Pinging %s' % (self, self.tuber_uri))
        try:
            await self._tuber_get_meta_async()  # make sure the tuber info is loaded
            await self._tuber_sleep_async(0)  # make a dummy call
            return True
        except TuberError as e:
            self.logger.debug(
                f'{self!r}: Tuber Ping returned an error. '
                f'Board is considered to be absent. Error is \n{e!r}')
            return False

    async def open_platform_async(self):
        """ Initialize and establish communication with the platform.

        For the Iceboard, this means that we initialize the Tuber communication with the Iceboard.

        """
        self.logger.debug(f'{self!r}: Opening Tuber connection to the IceBoard')
        # Ask Tuber to fetch the methods & properties profided by the on-board
        # ARM software.
        await self._tuber_get_meta_async()
        # Check if the board is running a compatible ARM firmware.
        self.logger.debug(f'{self!r}: Checking ARM firmware version...')
        await self.check_tuber_version_async()

    async def close_platform_async(self):
        """ Close communication with the IceBoard

        For now we don't do anything. The TCP connection to Tuber is left intact.
        """

        pass

    ###################################
    # Auto discovery methods
    # (discover the serial, slot, crate and mezzanines through the platform)
    ###################################

    async def discover_serial_async(self, update=True):
        """ Discover the serial number of this IceBoard and
        update the hardware map accordingly if `update=True`

        We do this using the Tuber method get_motherboard_serial() which
        gets it from the board's IPMI EEPROM when the board booted up.


        Parameters:

            update (bool): if True, update the serial number of this IceBoard with the value that was obtained.

        Returns:

            str: the serial number of this board. The original serial number is returned if `update` =False.
        """
        self.logger.debug(f'{self!r}: discovering the serial number of board at {self.tuber_uri}')
        try:
            actual_serial = str((await self.tuber_get_motherboard_serial_async()))
        except Exception as e:  # Deal with uninitialized boards
            self.logger.warning(f'{self!r}: Error while attempring to read the board serial number. '
                                f'The exception is {e}')
            actual_serial = None
        self.logger.debug(f'{self!r}: got the serial number of board at {self.tuber_uri} to be {actual_serial}')
        await asyncio.sleep(0)
        if update:
            if not actual_serial:
                self.logger.warning(f'{self!r}: Could not read the board serial number from IPMI storage, '
                                    f'or serial number is null. Serial number is not updated.')
            elif self.serial and actual_serial != self.serial:
                self.logger.warning(f'{self!r}: The discovered serial number differs from the current '
                                    f'(hardware map) one. Updating to the discovered value.')
            self.update_instance(serial=actual_serial)
        self.logger.debug(f'{self!r}: finished discovering the serial number of board at {self.tuber_uri}')
        return self.serial

    async def discover_slot_async(self, update=True):
        """ Discover the slot number of this IceBoard, and update the hardware map accordingly if `update=True`

        Slot number is obtained using Tuber methods.

        Parameters:

            update (bool): if True, update the slot number of this IceBoard with the value that was obtained.

        Returns:

            int: the board's slot number. The original slot number is returned
                if `update` =False. A slot number of 0 means the board is not
                connected to a backplane.
        """
        actual_slot = await self.tuber_get_backplane_slot_async()
        if update:
            if not actual_slot:
                self.logger.info(f'{self!r}: The board is not connected to a backplane. Slot number is not updated.')
            else:
                if self.slot and actual_slot != self.slot:
                    self.logger.warning(f'{self!r}: The discovered slot ({actual_slot}) number differs from the current '
                                        f'(hardware map) one ({self.slot}). Updating to the discovered slot.')
                self.update_instance(slot=actual_slot)
        return self.slot

    async def discover_mezzanines_async(self, update=True):
        """Detect mezzanines attached to the Iceboard and update the hardware map accordingly if
        update=True.

        This method uses IPMI data on the mezzanine's EEPROMs to determine its model ans serial number.
        New Mezzanine objects that match the IPMI model (product) number are added in
        the hardware map if found.

        Parameters:

            update (bool): if True, the IceBoard is updated to point to the discovered mezzanine objects.

        Returns:

            dict: dict in the format {mezz_number:mezz_class, ...} that lists the mezzanine class
            discovered for each mezzanine, whether or not `update` =True or not.
         """
        mezz_class = {}  # {mezz_number:mezz_class, ...}
        for m in self.FMC_MEZZ_NUMBERS:

            # MezzClass = MissingMezzanine # Used by Graeme
            part_number = None
            serial = None
            mezz_class[m] = None
            # if there is no mezzanine in this slot, proceed to the next one
            if not (await self.tuber_is_mezzanine_present_async(m)):
                continue

            # If a mezzanine is present, get its EEPROM data and search for
            # the first mezzanine class that can decode it.
            try:
                # Try to read the EEPROM. This does not read all the EEPROM for some mezzanines...
                eeprom_data = await self._mezzanine_eeprom_read_async(m)
                # We used to read the EEPROM via the FPGA when the platform did not provide a raw read method.
                # That is no longer necessary, but here's the line:
                # eeprom_data = self.read_mezzanine_eeprom(m,0,512)
            except (TuberRemoteError, AttributeError):  # If the method does not exist
                eeprom_data = None

            # Find a registered Mezzanine class that can decode the EEPROM data
            # This is done to recognize the legacy non-IPMI EEPROM data format that the ARM cannot decode
            ipmi = None
            if eeprom_data is not None:
                for cls in Mezzanine.get_all_classes():  # for each registered Mezzanine class
                    if hasattr(cls, 'decode_eeprom'):  # If there si a decode method
                        ipmi = cls.decode_eeprom(eeprom_data)   # try to decode
                        if ipmi:  # if not None, we're done
                            break
            # If we still don't have IPMI data, try to get it directly from the ARM software
            if not ipmi:
                try:
                    ipmi = await self._tuber_get_mezzanine_ipmi_async(m)  # Read IPMI from the ARM's cache
                    self.logger.debug(
                        f'{self!r}: detect_mezzanines(): '
                        f'read Mezzanine {m} EEPROM using the ARM')
                except TuberRemoteError:
                    pass

            # If we still did not get an IPMI block, give up and proceed to the next mezzanine
            if not ipmi:
                self.logger.debug(
                    f'{self!r}: detect_mezzanines(): Could not decode '
                    f'the EEPROM in Mezzanine {m}')
                continue

            # Extract the useful information from IPMI
            part_number = ipmi.product.part_number
            serial = ipmi.product.serial_number

            self.logger.debug(
                f'{self!r}: detect_mezzanines(): Detected Mezzanine '
                f'Model: {part_number} Serial {serial} in Mezzanine {m}')

            # Look through the Mezzanine classes to see if one matches the model number found in the EEPROM
            # for mapper in class_mapper(FMCMezzanine).self_and_descendants:
            #         if mapper.class_._ipmi_part_numbers == part_number:
            #             mezz_class[m] = mapper.class_
            #             break
            mezz_class[m] = Mezzanine.get_class_by_ipmi_part_number(part_number)

            if update:
                if m in self.mezzanine:
                    del(self.mezzanine[m])

            if not mezz_class[m]:
                self.logger.warning(
                    "%r: detect_mezzanines(): There is no known "
                    "class for Mezzanine object of type '%r' "
                    "in mezzanine slot %r" % (self, part_number, m))
            elif update:
                self.logger.debug(
                    '%r: detect_mezzanines(): Creating Mezzanine '
                    'Serial %s in Mezzanine %i on iceboard %r' % (self, serial, m, self))
                # new_mezz = get_unique_class_instance(
                #     mezz_class[m],
                new_mezz = mezz_class[m](
                    serial=serial,
                    mezzanine=m,  # mezzanine number (FMC slot)
                    iceboard=self)  # back reference to the carrier iceboard
                new_mezz.iceboard = self
                self.mezzanine[m] = new_mezz
                self.logger.debug(f'{self!r} mezzanines on FMC {m} are {self.mezzanine[m]}')
        return mezz_class

    async def discover_crate_async(self, update=True):
        """ Detect the Icecrate and slot number on which this Iceboard is
        attached by reading the backplane IPMI data, and update the hardware
        map accordingly if `update=True`.

        The crate object is then set with the (part_number, serial_number)
        tuple, which is replaced later by the actual crate object.

        This method does *not* use mDNS. It relies of the IPMI data stored in
        the backplane's EEPROM, which is obtaines through the Iceboard's ARM
        processor.

        Parameters:

            update (bool): if True, the IceBoard is updated to point to the discovered IceCrate objects.

        Returns:

            class of the discovered crate object, or None if none is present. The new class is returned whether or not `update` =True or not.

        """

        icecrate_class = None
        part_number = None
        serial = None
        slot_number = None
        if (await self.is_backplane_present_async()):
            try:
                ipmi = await self._tuber_get_backplane_ipmi_async()  # Tuber call

                part_number = ipmi.product.part_number
                serial = ipmi.product.serial_number
                icecrate_class = self.get_class_by_ipmi_part_number(base_class_name='Crate', part_number=part_number)
                slot_number = await self.tuber_get_backplane_slot_async()
                self.logger.debug(
                    f'{self!r}: discover_crate(): Detected Backplane '
                    f'Model {part_number} Serial {serial}')
            except TuberRemoteError:
                self.logger.warning(
                    f'{self!r}: discover_crate(): Detected Backplane '
                    f'but it has no IPMI information')

        if not icecrate_class:
            self.logger.warning(
                f"{self!r}: discover_crate(): There is no known backplane object "
                f"with part number '{part_number}'")

        if icecrate_class and update:
            crate_number = self.crate.crate_number if self.crate else None
            crate = Crate.get_unique_instance(new_class=icecrate_class, serial=serial, crate_number=crate_number)
            self.update_instance(crate=crate)  # update crate info and repr
            self.logger.debug(f"{self} now has crate {self.crate}")
            # if slot_number:
            #     self.crate.slot[slot_number] = self

        return icecrate_class

    # --------------------------
    # -- Motherboard info
    # --------------------------


    async def get_motherboard_ip_address(self):
        """ Return the IP address of the ARM processor on the IceBoard """
        return await self._tuber_get_arm_ip_async()

    # --------------------------
    # -- Platform metrics
    # --------------------------

    async def _get_motherboard_metrics_async(self):
        """ Return status information on the board, and mezzanines, including voltages
        current, power consumption, temperatures etc.

        The measurements are obtained only through the ARM processor; the FPGA does not need to be configured.

        Parameters:

            None

        Returns:

            A (info, metrics) tuple, where:

                ``info`` is a dict containing the status information in the format ``{metric_name:value,
                ...}`` where both ``metric_name`` and ``value`` are strings.

                ``metrics`` is a Metrics object containing the same status information.

        """

        info = dict()
        metrics = await super().get_metrics_async()

        ####################################
        # Motherboard temperatures
        ####################################

        mb_temp_sensors = [
            ('MB FPGA Die Temp', 'FPGA DIE', self.TEMPERATURE_SENSOR.MB_FPGA_DIE),
            ('MB FPGA Temp'    , 'FPGA',     self.TEMPERATURE_SENSOR.MB_FPGA    ),
            ('MB ARM Temp'     , 'ARM',      self.TEMPERATURE_SENSOR.MB_ARM     ),
            ('MB PHY Temp'     , 'PHY',      self.TEMPERATURE_SENSOR.MB_PHY     ),
            ('MB POW Temp'     , 'Switcher', self.TEMPERATURE_SENSOR.MB_POWER   )]

        for display_name, sensor, sensor_name in mb_temp_sensors:
            value = await self.tuber_get_motherboard_temperature_async(sensor_name)
            info[display_name] = '%0.1fC' % value
            metrics.add('fpga_motherboard_temp', value,  sensor=sensor)

        ####################################
        # Motherboard voltages and currents
        ####################################

        mb_power_sensors = [
            ('MB VCC12V'    , 'VCC12V'    , self.RAIL.MB_VCC12V0   , True),
            ('MB VCC3V3'    , 'VCC3V3'    , self.RAIL.MB_VCC3V3    , True),
            ('MB VADJ'      , 'VADJ'      , self.RAIL.MB_VADJ      , False),  # VADJ is normally powered from VCC5V0
            ('MB VCC5V5'    , 'VCC5V5'    , self.RAIL.MB_VCC5V5    , True),
            ('MB VCC1V0'    , 'VCC1V0'    , self.RAIL.MB_VCC1V0    , False),
            ('MB VCC1V0 GTX', 'VCC1V0 GTX', self.RAIL.MB_VCC1V0_GTX, False),
            ('MB VCC1V2'    , 'VCC1V2'    , self.RAIL.MB_VCC1V2    , False),
            ('MB VCC1V5'    , 'VCC1V5'    , self.RAIL.MB_VCC1V5    , False),
            ('MB VCC1V8'    , 'VCC1V8'    , self.RAIL.MB_VCC1V8    , False)]

        total_power = 0
        for display_name, sensor, tuber_sensor_name, add_to_total_power in mb_power_sensors:
            voltage = await self.tuber_get_motherboard_voltage_async(tuber_sensor_name)
            current = await self.tuber_get_motherboard_current_async(tuber_sensor_name)
            info[display_name] = '%0.1fV@%0.3fA' % (voltage, current)
            metrics.add('fpga_motherboard_voltage', value=voltage, sensor=sensor)
            metrics.add('fpga_motherboard_current', value=current, sensor=sensor)
            if add_to_total_power:
                total_power += voltage * current

        ####################################
        # Mezzanines voltages and currents
        ####################################

        mezz_power_sensors = [
            ('Mezz %i VCC12V'    , 'VCC12V'    , self.RAIL.MEZZ_VCC12V0),
            ('Mezz %i VCC3V3'    , 'VCC3V3'    , self.RAIL.MEZZ_VCC3V3),
            ('Mezz %i VADJ'      , 'VADJ'      , self.RAIL.MEZZ_VADJ)]

        for mezz_number in self.FMC_MEZZ_NUMBERS:
            for display_name, sensor, sensor_name in mezz_power_sensors:
                voltage = await self.tuber_get_mezzanine_voltage_async(sensor_name, mezz_number)
                current = await self.tuber_get_mezzanine_current_async(sensor_name, mezz_number)
                info[display_name % mezz_number] = '%0.1fV@%0.3fA' % (voltage, current)
                metrics.add('fpga_mezzanine_voltage', value=voltage, sensor=sensor, mezzanine=mezz_number)
                metrics.add('fpga_mezzanine_current', value=current, sensor=sensor, mezzanine=mezz_number)

        info['MB Total power'] = '%0.1fW' % total_power
        metrics.add('fpga_motherboard_power', value=total_power)

        ####################################
        # Motherboard QSFPs present
        ####################################

        for qsfp in [1, 2]:
            is_present = await self.tuber_is_qsfp_present_async(qsfp)
            metrics.add('fpga_motherboard_qsfp_present', value=is_present, qsfp=qsfp)

        # is_voltage_nominal
        # sysmon?
        # QSFP voltage, temp, signal
        return (info, metrics)

    async def get_metrics_async(self):
        """ Get the Iceboard hardware monitoring information.

        The metrics are obtained only through the ARM processor; the FPGA does not need to be configured.


        Returns:
            a :class:`Metrics` object containing the IceBoard monitoring information.
        """
        try:
            _, metrics = await self._get_motherboard_metrics_async()
        except Exception as e:
            self.logger.error('%r: Error getting FPGA hardware metrics. Error is %r' % (self, e))
            metrics = Metrics()
        return metrics


    async def get_total_power(self):
        """ Return the total power used by this board.

        The power is measured by measuring the voltage and current on the VCC3V3, VCC5V5 and VCC12V0
        rails through the ARM processor.

        Returns:
            Total power, in Watts, as a float.
        """
        # Get and sum power asynchronously. We have to use a list comprehension, not generator (a
        # yield inside a generator is not consistent in Python 2.7)
        power = sum([(await self.tuber_get_motherboard_voltage_async(rail)) * (await self.tuber_get_motherboard_current_async(rail))
                     for rail in (self.RAIL.MB_VCC3V3, self.RAIL.MB_VCC5V5, self.RAIL.MB_VCC12V0)])
        return power

    async def _get_backplane_metrics_async(self):
        """ Get the backplane hardware monitoring information, as accessed from this Iceboard.

        The measurements are obtained only through the ARM processor; the FPGA does not need to be configured.

        Returns:
            A :class:`Metrics` object.

        Note: an 'info' dict is also created but is not returned as the metrics is sufficient for now.

        """

        info = dict()
        crate_number = self.crate.crate_number if self.crate else None
        crate_id = self.crate.get_string_id() if self.crate else None
        metrics = Metrics(crate_number=crate_number, crate_id=crate_id, type='GAUGE')

        if (await self.is_backplane_present_async()):
            try:
                ####################################
                # Backplane temperatures
                ####################################

                bp_temp_sensors = [
                    ('BP Slot1 Temp', 'Slot1', self.TEMPERATURE_SENSOR.BP_SLOT1),
                    ('BP Slot16 Temp', 'Slot16', self.TEMPERATURE_SENSOR.BP_SLOT16)]

                for display_name, sensor, sensor_name in bp_temp_sensors:
                    value = await self.tuber_get_backplane_temperature_async(sensor_name)
                    info[display_name] = '%0.1fC' % value
                    metrics.add('fpga_backplane_temp', value, sensor=sensor)

                ####################################
                # Backplane voltages and currents
                ####################################

                voltage = await self.tuber_get_backplane_voltage_async()
                current = await self.tuber_get_backplane_current_async()
                power = await self.tuber_get_backplane_power_async()
                info['BP VCC3V3'] = '%0.1fV@%0.3fA' % (voltage, current)
                info['BP power'] = '%0.1fW' % power
                metrics.add('fpga_backplane_voltage', value=voltage)
                metrics.add('fpga_backplane_current', value=current)
                metrics.add('fpga_backplane_power', value=power)

                ####################################
                # Fan tray
                ####################################

                metrics.add('fpga_backplane_fantray_tachometer', value=(await self.tuber_get_fantray_tachometer_async()))
                metrics.add('fpga_backplane_fantray_duty_cycle',
                            value=(await self.tuber_get_fantray_duty_cycle_async()) / 255.)

                ####################################
                # Backplane QSFPs present
                ####################################
                for slot in range(1, 17):
                    is_present = await self.tuber_is_bp_qsfp_present_async(slot)
                    metrics.add('fpga_backplane_qsfp_present', value=is_present, slot=(slot-1))

            except Exception as e:
                self.logger.error('%r: error getting backplane metrics: error is %r\n\n%s' % (self, e, traceback.format_exc()))
        return metrics

        # backplane QSFP voltage, temp, signal-level


    async def get_backplane_metrics_async(self):
        """ Get the IceCrate hardware monitoring information.

        The measurements are obtained only through the ARM processor; the FPGA does not need to be configured.

        Returns:
            a :class:`Metrics` object.
        """
        try:
            metrics = await self._get_backplane_metrics_async()
        except Exception as e:
            self.logger.error('%r: Error getting Backplane metrics. Error is %r' % (self, e))
            metrics = Metrics()
        return metrics


    # ----------------------------
    # Bitstream management
    # ----------------------------

    async def is_fpga_programmed_async(self):
        """ Indicate whether the FPGA is configured with a bitstream.

        Returns:

            bool: True if the FPGA is configured.
        """
        return await self.tuber_is_fpga_programmed_async()

    async def set_fpga_bitstream_async(self, firmware_mode=None, force=False, bitfile_override=None):
        """ Configure the FPGA with the bitstream that is appropriate for specified firmware mode.


        Parameters:

            firmware_mode (str): The desired operational mode. This will be
                used to automatically select the proper bitstream file for
                this platform and create the proper FPGAFirmware class.

            force (bool or None):

                - force = True: FPGA will always be configured independent of the signature of the currently programmed firmware
                - force = False: FPGA will be configured if it is not configured or if its bitstream
                  CRC differ from the provided bitstream
                - force = None: FPGA will be configured only if it is not configured

            bitfile_override (str): Specifies the path to a folder in which to
                search for the default bitstream file,  or the path to the
                bitstream file to use instead of the default one.

        """

        t0 = time.time()
        self.logger.debug(f'{self!r}: called set_fpga_bitstream')

        if hasattr(self, 'close'):
            self.close()

        # If the bitstream is not explicitely provided, ask the handler to
        # provide it. The str() of the returned object must yield the valid
        # bitstream buffer in a string.
        fw_cls, buf, fw_params = FPGAFirmware.get_firmware(self.part_number, firmware_mode, bitfile_override=bitfile_override)
        crc32 = buf.crc32
        base64_bytes = buf.base64
        self.logger.debug(f'{self!r}: Getting is_programmed')
        is_fpga_programmed = await self.tuber_is_fpga_programmed_async()
        t1 = time.time()
        self.logger.debug(f'{self!r}: Getting FPGA crc')
        fpga_bitstream_crc = await self.get_fpga_bitstream_crc_async()
        self.logger.debug(
            f'{self!r}: fpga_programmed={is_fpga_programmed}, force={force}, '
            f'fpga_crc={fpga_bitstream_crc or 0:08X}, bitstream_crc={crc32 or 0:08X}')
        t2 = time.time()
        if not is_fpga_programmed or force or (force is not None and (fpga_bitstream_crc != crc32)):
            self.logger.debug(f'{self!r}: Configuring FPGA')
            with self.tuber_use_json_cache():
                await self._tuber_set_fpga_bitstream_base64_async(base64_bytes)
            await self.set_fpga_bitstream_crc_async(crc32)
            self.logger.debug(f'{self!r}: Done configuring FPGA. ')
        else:
            self.logger.debug(
                f'{self!r}: FPGA is already configured. Skipping configuration.')
        self.fpga = fw_cls(self, mode=firmware_mode, **fw_params)


    FPGA_FIRMWARE_CRC32_ADDR = 4 * 3

    async def get_fpga_bitstream_crc_async(self):
        """ Return the signature (CRC) of the firmware currently configured in the
        FPGA.

        This CRC is read from the FPGA's firmware core registers, accessed through the ARM-FPGA SPI interface.

        Returns:
            int: CRC read from the FPGA
            None if the FPGA is not configured.
        """
        is_fpga_programmed = await self.is_fpga_programmed_async()
        if not is_fpga_programmed:
            return None
        crc = await self.fpga_core_reg_spi_read_async(self.FPGA_FIRMWARE_CRC32_ADDR)
        return crc

    async def set_fpga_bitstream_crc_async(self, crc32):
        """ Store in the specified firmware  signature (CRC) in the FPGA.


        This CRC will be used later to determine which version of the firmware is programmed in the FPGA, if any.

        Parameters:

            crc32 (int): 32-bit Firmware CRC value to write in the FPGA's firmware core register.

        """
        if (await self.is_fpga_programmed_async()):
            # self._bitstream_crc = crc32
            await self.fpga_core_reg_spi_write_async(self.FPGA_FIRMWARE_CRC32_ADDR, crc32)
        else:
            # self._bitstream_crc = None
            await self.fpga_core_reg_spi_write_async(self.FPGA_FIRMWARE_CRC32_ADDR, 0)


    # ------------------------------------
    # FPGA core register access methods (through the SPI ARM-FPGA link)
    # ------------------------------------
    #  Methods to read and write FPGA's core registers through the SPI link
    #  between the ARM the the FPGA. These methods are specific to the
    #  IceBoard, and are used when access to the core registers is required
    #  when the MMI interface is not yet operational.
    #
    #  For the IceBoard, these methods are typically used to:
    #
    #    - Read and write the FPGA firmware CRC that is programmed into the FPGA
    #    - Setup the FPGA's UDP networking over which the MMI interface will be established.
    #
    #  The core registers are also mapped into an address space accessible through the MMI interface.
    #  Apart from the exceptions listed above, core registers are typically accessed through this much faster MMI interface.

    async def fpga_core_reg_spi_read_async(self, addr):
        """ Read a single 32-bit word from the FPGA core register at the specified byte
        address. This uses the ARM-FPGA SPI link.


        Parameters:

            addr (int): byte address to read from in the core register memory space. This address must be a multiple of 4.

        Returns:

            32-bit value read at specified core register address. Value is returned as an unsigned integer.
        """
        # print(f'Reading core reg via SPI at {addr:03X}')

        assert not (addr & 3), "Core register address is not a multiple of 4 bytes"
        word = await self._tuber_fpga_spi_peek_async(addr)
        return word & 0xFFFFFFFF

    async def fpga_core_reg_spi_write_async(self, addr, value):
        """ Write a single 32-bit word to the FPGA core register at specified byte address.
        This uses the ARM-FPGA SPI link)

        Parameters:

            addr (int): byte address to read from in the core register memory space. This address must be a multiple of 4.

            value (int): 32-bit value to write into the specified register
        """
        # print(f'Writing core reg via SPI at {addr:03X}')

        assert not (addr & 3), "Core register address is not a multiple of 4 bytes"
        await self._tuber_fpga_spi_poke_async(addr, value)


    # ----------------------------
    # open & close methods
    # ----------------------------

    async def open_fpga_async(self, **kwargs):
        """ Open communication with the FPGA firmware and initialize objects that provide access to the Iceboard hardware via the FPGA.

        The FPGA must be configured prior to calling this method.

        For an IceBoard running a variant of the chfpga firmware, this implies
        configuring the FPGA networking stack, establish UDP communication
        with it, and creating (but not initializing) all the objects that
        represent the firmware subsystems.


        The hardware objects provide access to IceBoard hardware devices that
        is not provided by the ARM firmware. Those devices are created after
        the communication with the FPGA is established since we need the FPGA
        firmware to access the I2C through the FPGA.


        Parameters:

            kwargs: parameters to be passed to the firmware's `open_async()`
                method. These are typically networking parameters.

        """

        assert self.fpga, f'{self}: FPGA is not programmed. Cannot execute open_fpga()'
        self.logger.debug(f'{self!r}: open() is called')

        # Open communication witht he FPGA, and create the objects required to handle the FPGA's firmware modules.
        # This does not initialize all of the firmware, but basic functions such as GPIO, and I2C are usable
        await self.fpga.open_async(**kwargs)

        # Create a I2C interface to access the IceBoard hardware. We use the
        # I2C interface from the FPGA, since Tuber does not provide raw I2C
        # access methods.
        self.i2c = I2CInterface(
            write_read_fn=self.fpga_i2c_write_read,  # write-read function
            port_select_fn=self.fpga_i2c_set_port,
            bus_table=self.FPGA_I2C_BUS_LIST,
            switch_addr=self._FPGA_I2C_SWITCH_ADDR,
            parent=self)  # parent object, whose repr() is used to tag messages

        # Initialize hardware handlers
        await self.open_hw_async()

        # Set LEDs to indicate initialization state
        await self.set_led('GP_LED2', 1)  # Hardware link is on
        await self.set_led('GP_LED1', 0)  # Full FPGA firmware not initialized yet



    async def close_fpga_async(self):
        """ Close communication with the FPGA.
        """
        await self.close_hw_async()
        if self.fpga:
            await self.fpga.close_async()

    async def init_fpga_async(self, **kwargs):
        """ Initializes the FPGA firmware

        This method powers up the mezzanines and configures every subsystem of the FPGA to prepare it for operations.

        The communication with the FPFA must have been previously established with `open_fpga_async`.

        Parameters:

            kwargs: parameters to be passed to the firmware's `init_async()`
                method. These parameters are typically related to the
                application for which the firmware is to be used.

        """
        # Fully initialize the FPGA firmware
        await super().init_fpga_async(**kwargs)

        # Set LEDs to indicate initialization state
        await self.set_led('GP_LED1', 1)  # Full FPGA firmware is initialized




    ###################################
    # SFP module
    ###################################

    async def reset_sfp(self):
        """ Resets the SFP by temporarily disconnecting the FPGA from it

        This is done through the ARM procesor (via Tuber), so no FPGA firmware is needed.
        """

        self.logger.warning(
            "%r: Temporarily disconnecting the SFP to reset the %s FPGA's "
            "UDP communication stack" % (self, self.hostname))
        await self.tuber_set_pci_switch_direction_async('SEL_ARM')
        await self.tuber_set_pci_switch_direction_async('SEL_SFP')


    ###################################
    # IceBoard Hardware management
    ###################################

    async def open_hw_async(self):
        """ Initializing objects to access the Iceboard hardware

        Creates all the objects needed to interface the Iceboard I2C devices.

        I2C access is done through the I2CInterface object, which must be defined and shall provide the following methods:

            - i2c_set_port(...) # Port number 0 (connected to the FPGA I2C switch) is used for all accesses
            - i2c_write_read(...) # FPGA I2C engine

        Exceptions:

            - An AssertionError will be raised if the self.i2c object is not defined
        """
        self.logger.debug('%r: Initializing Iceboard hardware' % self)
        assert self.i2c, "I2C interface is not initialized"

        # -------------------
        # Motherboard EEPROM
        # -------------------

        self.logger.debug('%r: Instantiating Motherboard EEPROM managers' % self)
        self._i2c_mb_eeprom_data = eeprom.eeprom(
            self.i2c, self._MOTHERBOARD_EEPROM_DATA_ADDR, 'GPIO',
            self._MOTHERBOARD_EEPROM_ADDR_WIDTH,
            self._MOTHERBOARD_EEPROM_PAGE_SIZE)
        self._i2c_mb_eeprom_serial = eeprom.eeprom(
            self.i2c, self._MOTHERBOARD_EEPROM_SERIAL_ADDR, 'GPIO',
            self._MOTHERBOARD_EEPROM_ADDR_WIDTH,
            self._MOTHERBOARD_EEPROM_PAGE_SIZE)


        # -------------------
        # FMC EEPROMs
        # -------------------

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
        self.logger.debug('%r:  Instantiating FMC EEPROM managers' % self)
        if self.i2c.is_present(self._FMC_EEPROM_ADDR + 1, bus_name='FMCA'):
            self.logger.debug('%r: Detected multipage EEPROM on FMCA. Assuming >16-bit addressing.' % self)
            self._fmca_eeprom = eeprom.eeprom(
                self.i2c, self._FMC_EEPROM_ADDR, 'FMCA',
                self._MCGILL_FMC_EEPROM_ADDR_WIDTH,
                self._MCGILL_FMC_EEPROM_PAGE_SIZE)
        else:
            self._fmca_eeprom = eeprom.eeprom(
                self.i2c, self._FMC_EEPROM_ADDR, 'FMCA',
                self._FMC_EEPROM_ADDR_WIDTH,
                self._FMC_EEPROM_PAGE_SIZE)

        if self.i2c.is_present(self._FMC_EEPROM_ADDR + 1, bus_name='FMCB'):
            self.logger.debug('%r: Detected multipage EEPROM on FMCB. Assuming >16-bit addressing.' % self)
            self._fmcb_eeprom = eeprom.eeprom(
                self.i2c, self._FMC_EEPROM_ADDR, 'FMCB',
                self._MCGILL_FMC_EEPROM_ADDR_WIDTH, self._MCGILL_FMC_EEPROM_PAGE_SIZE)
        else:
            self._fmcb_eeprom = eeprom.eeprom(
                self.i2c, self._FMC_EEPROM_ADDR, 'FMCB',
                self._FMC_EEPROM_ADDR_WIDTH, self._FMC_EEPROM_PAGE_SIZE)

        self._FMC_EEPROM_TABLE = {
            1: self._fmca_eeprom,
            2: self._fmcb_eeprom
            }

        # -------------------
        # I2C IO Expanders
        # -------------------

        self.logger.debug('%r: Instantiating I2C GPIO manager' % self)
        self._gpio_power = pca9575.pca9575(self.i2c, self._GPIO_POWER_I2C_ADDR, 'GPIO')
        self._gpio_sw_leds = pca9575.pca9575(self.i2c, self._GPIO_SW_LEDS_ADDR, 'GPIO')
        self._gpio_arm_phy_leds = pca9575.pca9575(self.i2c, self._GPIO_ARM_PHY_LEDS_ADDR, 'GPIO')
        self._gpio_sfp_qsfp = pca9575.pca9575(self.i2c, self._GPIO_SFP_QSFP_I2C_ADDR, 'GPIO')

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

        # -------------------
        # QSFP EEPROMs
        # -------------------

        self._qsfpa = qsfp.QSFP(self.i2c, bus_name='QSFPA', gpio_prefix='QSFPA_', gpio=self._gpio, parent=self)
        self._qsfpb = qsfp.QSFP(self.i2c, bus_name='QSFPB', gpio_prefix='QSFPB_', gpio=self._gpio, parent=self)

        self.qsfp = Ccoll((self._qsfpa, self._qsfpb))
        # self.logger.info(' Instantiating I2C temperature sensors')
        # self._tmp_power = tmp100.tmp100(self.i2c, self._TMP_POWER_I2C_ADDR, 'GPIO')
        # self._tmp_phy = tmp100.tmp100(self.i2c, self._TMP_PHY_I2C_ADDR, 'GPIO')
        # self._tmp_fpga = tmp100.tmp100(self.i2c, self._TMP_FPGA_I2C_ADDR, 'GPIO')
        # self._tmp_arm = tmp100.tmp100(self.i2c, self._TMP_ARM_I2C_ADDR, 'GPIO')

        # self.logger.info(' Instantiating I2C current/power monitors')
        # self._power_ice_3v3 = ina230.ina230(self.i2c, self._POWER_ICE3V3_I2C_ADDR, 'SMPS')
        self._power_ice_12v0 = ina230.ina230(self.i2c, self._POWER_ICE12V0_I2C_ADDR, 'SMPS')
        # self._power_ice_5v0 = ina230.ina230(self.i2c, self._POWER_ICE5V0_I2C_ADDR, 'SMPS')
        # self._power_ice_1v0_gtx = ina230.ina230(self.i2c, self._POWER_ICE1V0GTX_I2C_ADDR, 'SMPS')
        # self._power_ice_vadj = ina230.ina230(self.i2c, self._POWER_ICEVADJ_I2C_ADDR, 'SMPS')
        # self._power_ice_1v2 = ina230.ina230(self.i2c, self._POWER_ICE1V2_I2C_ADDR, 'SMPS')
        # self._power_ice_1v5 = ina230.ina230(self.i2c, self._POWER_ICE1V5_I2C_ADDR, 'SMPS')
        self._power_ice_1v0 = ina230.ina230(self.i2c, self._POWER_ICE1V0_I2C_ADDR, 'SMPS')
        # self._power_ice_1v8 = ina230.ina230(self.i2c, self._POWER_ICE1V8_I2C_ADDR, 'SMPS')

        # self._power_fmca_12v0 = ina230.ina230(self.i2c, self._POWER_FMCA12V0_I2C_ADDR, 'SMPS')
        # self._power_fmca_3v3 = ina230.ina230(self.i2c, self._POWER_FMCA3V3_I2C_ADDR, 'SMPS')
        # self._power_fmca_vadj = ina230.ina230(self.i2c, self._POWER_FMCAVADJ_I2C_ADDR, 'SMPS')

        # self._power_fmcb_12v0 = ina230.ina230(self.i2c, self._POWER_FMCB12V0_I2C_ADDR, 'SMPS')
        # self._power_fmcb_3v3 = ina230.ina230(self.i2c, self._POWER_FMCB3V3_I2C_ADDR, 'SMPS')
        # self._power_fmcb_vadj = ina230.ina230(self.i2c, self._POWER_FMCBVADJ_I2C_ADDR, 'SMPS')

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


    async def close_hw_async(self):
        self.logger.debug('Closing Iceboard hardware')
        if self.i2c:
            self.i2c = None

    # **********************************
    # Local methods
    # **********************************

    # Backplane/crate-related methods
    # -------------------------------

    async def is_backplane_present_async(self):
        return await self.tuber_is_backplane_present_async()

    async def get_slot_number(self):
        """ Reads the GPIO to determine in which slot number this IceBoard is
        connected.

        """
        if await self.is_backplane_present_async():  # Is this test necessary?
            return await self.tuber_get_backplane_slot_async()
        else:
            return None

    # ---------------------------
    # Motherboard EEPROM
    # ---------------------------
    async def _write_motherboard_spi_eeprom_base64(self, *args, **kwargs):
        result = await self._tuber_motherboard_eeprom_write_base64_async(*args, **kwargs)
        return(result)

    def read_motherboard_eeprom(self, addr, length, **kwargs):
        """ Reads the Motherboard's EEPROM through the I2C interface object (via the FPGA)

        Parameters:

            addr (int): start memory address

            length (int): number of bytes to read

            kwargs: additional parameters passed to the self.i2c object

        Returns:

            Read data as bytes

        """
        return self._i2c_mb_eeprom_data.read(addr, length, **kwargs)

    def write_motherboard_eeprom(self, addr, data, **kwargs):
        """ Writes the Motherboard's EEPROM through the I2C interface object (via the FPGA)

        Parameters:

            addr (int): start memory address

            data (bytes): daat to write

            kwargs: additional parameters passed to the self.i2c object
        """
        return self._i2c_mb_eeprom_data.write(addr, data, **kwargs)

    # def get_motherboard_serial(self):
    #     """ Read the motherboard serial number from the IPMI data. """
    #     ipmi = self._get_motherboard_ipmi()
    #     return str(ipmi.board.serial_number)

    # *** JFC: We now have the equivalent ARM method. Will delete this when we
    #     confirm it behaves the same.

    # def get_serial_number(self):
    #     """
    #     Returns the board's serial number. which is actually the FPGA's
    #     serial number.
    #     """
    #     return self.get_serial_number(); # tentative code

    # -------------------------
    # Motherboard IO Expanders
    # -------------------------

    def _init_gpio_expanders(self):
        """
        Initializes GPIO expanders using the I2C Interface object.

        This is normally done at boot time by the ARM processor and does not
        need to be redone. This method can be usedif the
        Iceboard is operated without the ARM processor support.

        History

        140304 JM: created. todo: make more flexible for I/O pin configuration
        of each expander. Need to confirm I/O pin config with JF
        """
        self._gpio_power.init(cfg0_def=0b10101000,  # 1 = input, 0=output
                              cfg1_def=0b10101000,
                              out0_default=None,
                              out1_default=None,
                              bken0=0b00,  # We need to disable 100K internal
                              #       pull-ups/down so the PG_M2C can work
                              #       properly (there is another external 100K
                              #       pull up to VCC3V3 which pulls to GND
                              #       when there is no power. Pulling up
                              #       doesn't work when board is off , pull
                              #       down doesn't work when board is ON)
                              bken1=0b00,
                              pupd0=0b00001000,  # don't care, pullups not enabled
                              pupd1=0b00001000
                              )
        self._gpio_sw_leds.init(cfg1_def=0b00000000)
        self._gpio_arm_phy_leds.init(cfg0_def=0b11110000)
        self._gpio_sfp_qsfp.init(cfg0_def=0b01100011, cfg1_def=0b11001111)



    # -------------------------
    # Motherboard LEDs
    # -------------------------
    async def set_led(self, led_name, state):
        """
        Set the LED(s) specified in 'led_name' to the the 'state' using the I2C Interface.

        Parameters:

            led_name (str or list of str): Name of LED to set. Can be a list of LED names found in
                gpio object.

            state (bool or list of bool): State to set the led in . Can be a single boolean value, or
                an array with the same length as 'led_name'
        """
        if isinstance(led_name, str):
            led_name = [led_name]

        if isinstance(state, (bool, int)):
            state = [state] * len(led_name)

        await asyncio.sleep(0)
        for (led, led_state) in zip(led_name, state):
            self._gpio.write(led, led_state)

    async def get_led(self, led_name):
        """
        Returns the status of specified LED(s) in a dictionary using the I2C Interface.

        Parameters:

            led_name (str or list of str): Name of LED to set. Can be a list of LED names found in
                gpio object.

        Returns:
            dict in the format {led_name:led_status} describing the LED state for each requested LED name.
        """
        led_status = {}
        if isinstance(led_name, str):
            led_name = [led_name]

        for led in led_name:
            led_status[led] = self._gpio.read(led)

        return(led_status)

    # -------------------------
    # Motherboard Clock source
    # -------------------------

    async def get_iceboard_clock_source_async(self):
        """ Returns the clock source curently used by the IceBoard using Tuber"""
        return await self.tuber_get_clock_source_async()

    get_iceboard_clock_source_sync = async_to_sync(get_iceboard_clock_source_async)


    # -------------------------
    # Motherboard Temperatures
    # -------------------------


    def get_motherboard_temperature(self, sensor):
        """ Get the motherboard temperature sensor value using Tuber (sychrounous wrapper).
        """
        return run_async(self.tuber_get_motherboard_temperature_async(sensor))


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
    #                 raise ValueError(
    #                      'Invalid temperature sensor name. Valid names are %s'
    #                       % ','.join(self.TEMPERATURE_SENSOR_TABLE.keys()))
    #             else:
    #                 tmp_object = self.TEMPERATURE_SENSOR_TABLE[temp_sensor]
    #                 try:
    #                     tmp_object.init(bit_resolution)
    #                 except:
    #                     self.logger.info(
    #                         '%r: Temperature sensor %s failed to initialize.'
    #                          % (self, temp_sensor))

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

    # -----------------------------------------------
    # Motherboard Voltage, Current and Power sensors
    # -----------------------------------------------


    # def _init_power_sensors(self, power_sensor_name=None):
    #     """
    #     initializes current/power monitors
    #
    #     'power_sensor_name' can be a list of current/power monitor names
    #     found in POWER_SENSOR_TABLE. If power_sensor_name=None, all sensors
    #     in POWER_SENSOR_TABLE are initialized.

    #     History:
    #     140320 JM: created
    #     """
    #     if power_sensor_name == None:
    #         power_sensor_name = self.POWER_SENSOR_TABLE.keys()
    #     elif isinstance(power_sensor_name, str):
    #         power_sensor_name = [power_sensor_name]

    #     for power_sensor in power_sensor_name:
    #         if power_sensor not in self.POWER_SENSOR_TABLE:
    #            raise ValueError(
    #                'Invalid power sensor name. Valid names are %s'
    #                % ','.join(self.POWER_SENSOR_TABLE.keys()))
    #         else:
    #             power_sensor_object, v_out, r_shunt, i_typ, tol_i = self.POWER_SENSOR_TABLE[power_sensor]
    #             try:
    #                 power_sensor_object.init(v_out=v_out, r_shunt=r_shunt, i_typ=i_typ, tol_i=tol_i)
    #             except:
    #                 self.logger.info('%r: Power sensor %s failed to initialize.' % (self, power_sensor))


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
    #             raise ValueError(
    #                    'Invalid power sensor name. Valid names are %s.'
    #                    % ','.join(self.POWER_SENSOR_TABLE.keys()))
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




    # -------------------------
    # Mezzanine-related methods
    # -------------------------

    async def _mezzanine_eeprom_read_async(self, mezzanine):
        """ Returns the contents of the specified mezzanine's EEPROM.


        This method reads the EEPROM through the Tuber method.

        Parameters:

            mezzanine (int): mezzanine number (1 or 2)

        Returns:

            bytes: Contents of the EEPROM

        """
        data = await self._tuber_mezzanine_eeprom_read_base64_async(mezzanine)  # returns a str
        return base64.decodebytes(data.encode())  # encode the str into bytes before calling base64.decodebytes()


    def read_mezzanine_eeprom(self, mezzanine, addr, length, **kwargs):
        """
        Reads the mezzanine EEPROM through the FPGA.


        Parameters:

            mezzanine (int): mezzanine number, as defined in self.FMC_MEZZ_NUMBERS

            addr (int): byte address to start to read from

            length (int): number of bytes to read

            kwargs:  extra arguments passed to the EEPROM read method

        Returns: bytes
        """

        # get the I2CInterface object for this mezzanine
        eeprom_object = self._FMC_EEPROM_TABLE[mezzanine]
        return eeprom_object.read(addr, length, **kwargs)

    def write_mezzanine_eeprom(self, mezzanine, addr, data, **kwargs):
        """
        Writes the mezzanine EEPROM through the FPGA.


        Parameters:

            mezzanine (int): mezzanine number, as defined in self.FMC_MEZZ_NUMBERS

            addr (int): byte address to start to write from

            data (bytes): data to write

            kwargs:  extra arguments passed to the EEPROM write method

        Returns: bytes
        """
        # get the I2CInterface object for this mezzanine
        eeprom_object = self._FMC_EEPROM_TABLE[mezzanine]
        return eeprom_object.write(addr, data, **kwargs)

    async def set_mezzanine_power_async(self, mezzanine, state=True):
        """
        Enables or disables power of the specified FMC slot through the FPGA using power sequencing.

        This method differs from the Tuber equivalent as it does power sequencing to prevent the FMC board switchers to
        create too much of a current spike when enabled.

        Power is set using the _gpio_power object, which is the IO Extender
        that controls the FMC power lines. Is is accessed through a I2C
        Interface object.

        Parameters:

            mezzanine (int): integer representing the mezzanine number of the mezzanine to power ON or OFF.

            state (bool): Desired ON/OFF state of thecified FMC(s). True=ON, False=OFF.

        History:
            140223 JFC: Modified to use register names.

            140304 JM: Modified it so a state for every fmc can be specified.
                For now, state is either a boolean or a list of booleans with
                the same length as 'fmc_number' Todo: 140223 JFC: used masked
                writes to avoid side effects.
        """
        if isinstance(mezzanine, int):
            mezzanine = [mezzanine]

        if isinstance(state, (bool, int)):
            state = [state] * len(mezzanine)

        for (mezz, mezz_state) in zip(mezzanine, state):
            if mezz not in self.FMC_MEZZ_NUMBERS:
                raise ValueError(f'{mezz} is nor a valid mezzanine number.')

            # Find the hardware FMC slot number corresponding to the logical mezzanine number
            fmc = self.FMC_SLOT_NUMBERS[self.FMC_MEZZ_NUMBERS.index(mezz)]

            # out_reg = 'OUT%i' % fmc # sets the register name to access based on the FMC number
            # cfg_reg = 'CFG%i' % fmc
            # Turn off all power signals before we enable the GPIO outputs
            # self._gpio_power.write(out_reg, 0b00000000)
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
            if mezz_state:
                # Turn on 12V, 3.3V and VADJ power to board
                self._gpio_power.write(fmc, 0b00000010, mask=0b00000010)
                # self._gpio_power.write(fmc, 0b00000110, mask=0b00000110)
                await asyncio.sleep(0.010)
                # Turn on 12V, 3.3V and VADJ power to board
                self._gpio_power.write(fmc, 0b00000100, mask=0b00000100)
                await asyncio.sleep(0.100)
                # Turn on 12V, 3.3V and VADJ power to board
                self._gpio_power.write(fmc, 0b00000001, mask=0b00000001)
                await asyncio.sleep(0.050)
                # Set Power Good (start switcher) and CLKDIR to 1
                self._gpio_power.write(fmc, 0b01010000, mask=0b01010000)
                await asyncio.sleep(0.050)
            else:
                # Stop mezzanine switcher (PG=0)
                self._gpio_power.write(fmc, 0b00000000, mask=0b01010000)
                await asyncio.sleep(0.030)
                # Turn off 12V
                self._gpio_power.write(fmc, 0b00000000, mask=0b00000001)
                await asyncio.sleep(0.030)
                # Turn off rail
                self._gpio_power.write(fmc, 0b00000000, mask=0b00000010)
                await asyncio.sleep(0.030)
                # Turn off rail
                self._gpio_power.write(fmc, 0b00000000, mask=0b00000100)
                await asyncio.sleep(0.100)

    async def set_mezzanine_reset_async(self, mezzanine, state=True):
        """
        Enables or disables the reset line of the specified mezzanine through the FPGA.

        Parameters:

            mezzanine (int): integer representing the mezzanine number of the mezzanine for which the reset state is to be set.

            state (bool): Reset state to be set

        """
        if mezzanine not in self.FMC_MEZZ_NUMBERS:
            raise ValueError(f'{mezzanine} is nor a valid mezzanine number.')
        # Find the hardware FMC slot number corresponding to the logical mezzanine number
        fmc = self.FMC_SLOT_NUMBERS[self.FMC_MEZZ_NUMBERS.index(mezzanine)]
        if fmc:
            self.fpga.GPIO.ADC1_RESET = state
        else:
            self.fpga.GPIO.ADC0_RESET = state

    # ---------------------
    # Tuber-related methods
    # ---------------------

    async def check_tuber_version_async(self):
        """ Check if the ARM processor provides the methods required to run this code. """

        required_tuber_methods = [  # Use the unmangled name as published by the ARM
            'is_fpga_programmed',
            'get_build_info',
            # '_mezzanine_eeprom_read_base64',
        ]

        (meta, props, tuber_methods) = await self._tuber_get_meta_async()  # get the meta info
        if not tuber_methods:
            raise RuntimeError(f"{self!r}: The ARM does not publish any methods "
                               f"under the object name '{self.tuber_objname}'. "
                               f"Was the right Tuber object name used for this ARM firmware?")

        for method in required_tuber_methods:
            if method not in tuber_methods:
                raise RuntimeError(f"{self!r}: The current version of the ARM firmware "
                                   f"does not provide the method '{method}' that is needed for this application")

        # Make sure we have the right version of IceCore (Tuber) and SD card bootloader
        build_info = await self.tuber_get_build_info_async()

        self.logger.debug(f'{self!r}: SD card build info is {build_info}')

        if 'R11.4' not in build_info.icecore_git_hash:
            raise RuntimeError(f'ARM firmware is not compatible with this version of the FPGA firmare (SPI link between ARM and FPGA will not work - cannot setup networking). Build info is {build_info}')

        if 'R11.4-6' not in build_info.icecore_git_hash or build_info.icecore_git_time != '2021-11-19':
            self.logger.warning(f'{self!r}: Current version of the icecore software on the SD card is not the latest one. It is recommended to update the SD card with the latest image. Build info is {build_info}')

        if '_get_file_contents' not in tuber_methods:
            self.logger.warning(f'{self!r}: The current version of the icecore software does not allow checking the bootloader version. Please update the SD card if there are boot issues (boards not booting or not showing up on the network). Build info is {build_info}')
        else:
            version = (await self._tuber_get_file_contents_async('/usr/lib/iceboard/version.txt')).strip()
            self.logger.info(f'{self!r}: SD card version is {version}')
            if version != 'Chime_R11.3i':
                self.logger.warning(f'{self!r}: The current version of the SD card is not up to date. Please update the SD card if there are boot issues (boards not booting or not showing up on the network). Build info is {build_info}, version is {version}')

        return True

    def print_tuber_methods(self):
        """ Print all the methods and properties provided by the Iceboard's
        ARM processor through the Tuber protocol.
        """
        (meta, props, methods) = self._tuber_get_meta()  # get the meta info
        print("Methods for tuber object '%s':" % self.tuber_objname)
        print('-----------------------------------------')
        for method_name, method_properties in sorted(methods.items()):
            print('%-30s: %s' % (method_name, method_properties.summary))
        print()
        print("Properties for tuber object '%s':" % self.tuber_objname)
        print('-----------------------------------------')
        for prop_name, prop_properties in sorted(props.items()):
            try:
                values = ', '.join('.%s' % p for p in prop_properties)
            except TypeError:
                values = '= %s' % prop_properties
            print('%-30s: %s' % (prop_name, values))

    # ---------------------------
    # ARM Linux shell commands
    # ---------------------------

    async def _call_subprocess(self, cmd):
        """
        Executes a subprocess in a non-blocking way.

        Parameters:

            cmd (str): Shell command to execute locally
        """
        pipe = subprocess.PIPE

        split_cmd = shlex.split(cmd)
        p = subprocess.Popen(split_cmd, stdout=pipe, stderr=pipe)
        while p.poll() is None:
            await asyncio.sleep(0)
        if p.returncode:
            msg = b''.join(p.stderr.readlines())
            raise RuntimeError(
                f"The command '{cmd}' returned with the error code {p.returncode}. "
                f"stderr is displayed below:\n {msg}")
        return p.stdout.readlines()

    async def arm_exec(self, cmd):
        """
        Executes a command on the ARM over SSH.

        Parameters:

            cmd (str): shell command to execute on the ARM processor

        """
        self.logger.info(f"{self!r}: Executing command '{cmd}' on the ARM")
        ssh_cmd = f'ssh {self.SSH_OPTIONS} root@{self.hostname} "{cmd}"'
        result = await self._call_subprocess(ssh_cmd)
        return result

    async def arm_scp(self, source_filename, destination_filename='/tmp'):
        """
        Sends a file to the arm using scp.
        """
        self.logger.info(f'{self!r}: Sending image file {source_filename} to the ARM in {destination_filename}')
        scp_cmd = f'scp {self.SSH_OPTIONS} {source_filename} root@{self.hostname}:{destination_filename}'
        result = await self._call_subprocess(scp_cmd)
        return result

    async def _update_arm_firmware(self, image_filename, delay=120):
        """
        Overwrites the ARM firmware on the SD card with the specified image compressed with bzip2.

        !!! WARNING: This is a very ugly hack that can make the SD card
        inoperable. You must do this only if you are in a position to manually
        replace a SD card if this fails!!!

        !!! The image must be in BZIP2 format. If not, the ARM won't boot
        again unless you replace the SD card !!!

        You must power-cycle the board after this command. The normal reboot()
        method won't work because this corrupts the ARMs filesystem (did we
        say this was a bad hack?).
        """
        image_header = b'\xfa\xb8\x00\x10\x8e\xd0\xbc\x00'
        with bz2.BZ2File(image_filename) as fh:
            data = fh.read(100)  # read a few bytes to make sure this is really a bz2 file
            if not data.startswith(image_header):
                raise RuntimeError('The image does not seem to contain a compressed SDcard image')
        print('%r: Sending file...' % self)
        await self.arm_scp(image_filename, '/tmp/image.bz2')
        print('%r: Writing SD card' % self)
        await self.arm_exec('bzcat /tmp/image.bz2 >/dev/mmcblk0')
        self.logger.info('%r: Command completed. Waiting %i seconds to ensure cache is flushed' % (self, delay))
        print('%r: Waiting %i seconds' % (self, delay))
        await asyncio.sleep(delay)
        return True

    async def _update_arm_MLO(self, mlo_filename):
        await self.arm_exec('mount -o remount,rw /dev/mmcblk0p1')
        await self.arm_scp(mlo_filename, '/boot/MLO')
        await self.arm_exec('sync')

        return True

    async def _upload_fpga_bitstream(self, filename, card_filename=None, delay=120):
        """
        Remounts the filesystem as read write
        Uploads the bit file to the SD card in folder /usr/lib/iceboard/
        Remounts the file system back to read only
        """

        if card_filename is not None:
            filename_sd_card = '/usr/lib/iceboard/' + card_filename
        else:
            filename_sd_card = '/usr/lib/iceboard/' + os.path.basename(filename)

        print('%r: Remounting the SD card file system as readwrite' % self)
        await self.arm_exec('mount / -o remount,rw')

        print('%r: Making /usr/lib/iceboard folder if needed' % self)
        await self.arm_exec('mkdir -p /usr/lib/iceboard')

        print('%r: Sending file to /usr/lib/iceboard/' % self)
        await self.arm_scp(filename, filename_sd_card)

        self.logger.info('%r: Command completed. Waiting %i seconds to ensure cache is flushed' % (self, delay))
        print('%r: Waiting %i seconds' % (self, delay))
        await asyncio.sleep(delay)

        print('%r: Remounting the SD card file system as readonly' % self)
        await self.arm_exec('mount / -o remount,ro')
        return True

    async def _delete_fpga_bitstream(self, filename, delay=120):
        """
        Remounts the filesystem as read write
        Removes  the bit file on the SD card in folder /usr/lib/iceboard/
        Remounts the file system back to read only
        """

        print('%r: Remounting the SD card file system as readwrite' % self)
        await self.arm_exec('mount / -o remount,rw')

        remove_file = 'rm /usr/lib/iceboard/' + os.path.basename(filename)
        print('%r: Removing the requested file' % self)
        await self.arm_exec(remove_file)

        self.logger.info('%r: Command completed. Waiting %i seconds to ensure cache is flushed' % (self, delay))
        print('%r: Waiting %i seconds' % (self, delay))
        await asyncio.sleep(delay)

        print('%r: Remounting the SD card file system as readonly' % self)
        await self.arm_exec('mount / -o remount,ro')
        return True

# vim: sts=4 ts=4 sw=4 tw=80 smarttab expandtab
