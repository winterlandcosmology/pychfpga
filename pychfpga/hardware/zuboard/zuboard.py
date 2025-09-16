""" Defines the class to operate the ZCU111.
"""
# Standard Python packages
import logging
import datetime
from calendar import timegm
import time
import struct
import base64
from collections import OrderedDict
import socket
import asyncio

import nest_asyncio

# External private packages
from wtl.metrics import Metrics

# Local packages
from pychfpga.hardware import Motherboard
from pychfpga.common import run_async, async_to_sync, Ccoll
from pychfpga.hardware.interfaces import TCPipe, TCPipe_I2C, TCPipe_SPI, ipmi_fru
from pychfpga.fpga_firmware import FPGAFirmware

from ..i2c_devices.eeprom import eeprom


class ZUBoard(Motherboard):
    """ Provide the basic code needed to operate the Avnet ZUBOARD 1CG

    If is assumed that the board's processing system (ARM processor) is running TCPipe.

    This class provides:
        - Lightweight list-based Hardware map management and optional mDNS discovery
        - Model & serial self discovery
        - Access to on-board I2C peripherals (EEPROM)
        - Access to
        - Access to the memory-mapped registers in the FPGA's firmware using the TCP link.

    Parameters:

    """

    # Define model number for hardware map management and discovery
    part_number = 'ZUBOARD1GC'
    _ipmi_part_numbers = ['ZUBOARD1GC']

    NUMBER_OF_CHANNELIZERS = 0

    port = 7  # port number on which to access the platform `hostname`

    # ---------------

    def __init__(self, hostname=None, serial=None, slot=None, subarray=None, **kwargs):
        super().__init__(
            hostname=hostname,
            serial=serial,
            slot=slot,
            subarray=subarray, **kwargs)

        self.iic = None
        self.spi = None
        # self.mmi = None
        self.tcpipe = None
        self._is_open = None
        self.fpga = None  # firmware object

        self.firmware_crc = None  # temporary local storage of the CRC since we currently can't read it until the firmware is programmed.
        self.revision = None


    def open(self):
        run_async(self.open_async())


    async def open_platform_async(self, **kwargs):

        self.revision = 0
        self.logger.debug(f'{self!r}: open() is called')


        # Open tcp communication with the board
        self.logger.debug(f'{self!r}: Opening TCP connection to the board')
        self.tcpipe = TCPipe(self.hostname, self.port)

        # create I2C interface
        self.iic = TCPipe_I2C(self.tcpipe)

        # I2C1 peripherals
        self.i2c1_eeprom_data = eeprom(self.iic, address=0x50, bus_name=1, address_width=8, max_read_length=255, max_write_length=8, write_page_size=8)
        self.i2c1_eeprom_serial = eeprom(self.iic, address=0x58, bus_name=1, address_width=8, max_read_length=255)  # must read 16 bytes from memory address 0x80



    def close_platform(self):
        if self.tcpipe:
            self.tcpipe.close()
            self.tcpipe = None
        self._is_open = False


    async def open_fpga_async(self, **kwargs):
        """ Open communication link with the FPGA. This creates the MMI interface, gather configuration information from the firmware, and instantiate the objects that will handle the firmware."""

        await self.fpga.open_async()

    async def close_fpga_async(self):
        await self.fpga.close()

    def is_open(self):
        return self._is_open

    def is_fmc_present(self, slot):
        return False

    async def ping_async(self, timeout=0.1):
        """
        Returns a boolean indicating whether the board is responding to network queries.
        """
        # print(f'{self!r} Ping_async()')
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.logger.info(f'{self!r}: Pinging {self.hostname} at {s.getsockname()}')
        s.settimeout(timeout)
        loop = asyncio.get_event_loop()
        try:
            await loop.sock_connect(s, (self.hostname, self.port))
            if_addr = s.getsockname()
            s.close()
        except (socket.timeout, Exception) as e:
            self.logger.warn(f'Could not establish a TCP connection with {self.hostname}:{self.port}. Error is: {e!r}\n ')
            return False
        return True





    ###################################
    # Auto discovery methods
    ###################################
    # Called after open_platform_async(), but before the FPGA firmware is set.


    async def discover_serial_async(self, update=True):
        """
        Discover the serial number of this IceBoard from its IPMI data, and update the hardware map accordingly if `update=True`
        """
        self.logger.debug(f'{self!r}: discovering the serial number of board at {self.hostname}')
        return "003"

    async def discover_slot_async(self, update=True):
        """ Discover the slot number of this board. Always returns None since it cannot be mounted on a backplane"""
        return None

    async def discover_mezzanines_async(self, update=True):
        """Detect mezzanines attached to this board. Currently always returns None.
        """
        return None


    async def discover_crate_async(self, update=True):
        """ Detect the crate in which the board is installed. This board always returns None since it cannot be mounted in a backplane.
        """
        return None



   ###################################
    # Firmware management
    ###################################
    # Called after open_platform_async(), but before the FPGA firmware is set.

    async def is_fpga_programmed_async(self):
        return True
        # return self.tcpipe.is_fpga_programmed()

    async def set_fpga_bitstream_async(self, firmware_mode=None, force=False, bitfile_override=None):
        """
        Configures the FPGA with the specified bitstream.

        Parameters:

            firmware (str or FPGABitstream): The firmware to program into the FPGA

                - ``FPGABitstream``: Use the specified bitstream object directly.

                - str: if `firmware` has no special characters ('.', '/' etc) it is treated as a
                  generic name that will used to be look up the firmware filename in the
                  PLATFORM_SUPPORT table of all registered FPGAFirmware classes. Otherwise, the
                  string is treated as a pathname and is passed to FPGABitstream directly.

            force (bool or None):

                - force = True: FPGA will always be configured independent of the signature of the currently programmed firmware
                - force = False: FPGA will be configured if it is not configured or if its bitstream
                  CRC differ from the provided bitstream
                - force = None: FPGA will be configured only if it is not configured
        """

        t0 = time.time()
        self.logger.debug(f'{self!r}: called set_fpga_bitstream')

        if hasattr(self, 'close'):
            self.close()

        fw_cls, buf, fw_params = FPGAFirmware.get_firmware(self.part_number, firmware_mode, bitfile_override=bitfile_override)
        crc32 = buf.crc32
        bitstream = buf.raw_bitstream

        self.logger.debug(f'{self!r}: Getting is_programmed')
        is_fpga_programmed = await self.is_fpga_programmed_async()
        is_crc_valid = False
        if self.fpga and is_fpga_programmed:
            self.logger.debug(f'{self!r}: Getting FPGA crc')
            fpga_bitstream_crc = await self.get_fpga_bitstream_crc_async()
            self.logger.debug(
                f'{self!r}: fpga_programmed={is_fpga_programmed}, force={force}, '
                f'fpga_crc={fpga_bitstream_crc or 0:08X}, bitstream_crc={crc32 or 0:08X}')
            is_crc_valid = fpga_bitstream_crc == crc32

        if not is_fpga_programmed or force or (force is not None and not is_crc_valid):
            self.logger.debug(f'{self!r}: Configuring FPGA')
            self.tcpipe.set_fpga_bitstream(bitstream, crc=crc32)
        else:
            self.logger.debug(
                f'{self!r}: FPGA is already configured. Skipping configuration.')

        self.fpga = fw_cls(self, **fw_params)

    # Mezzanine management


    async def get_fpga_bitstream_crc_async(self):
        """ Return the signature of the firmware currently configured in the
        FPGA.

        Returns None if the FPGA is not configured.
        """
        fpga_is_programmed = await self.is_fpga_programmed_async()
        return self.tcpipe.get_fpga_bitstream_crc() if fpga_is_programmed else None

    async def set_fpga_bitstream_crc_async(self, crc32):
        """ Return the signature of the firmware currently configured in the
        FPGA.

        Returns None if the FPGA is not configured.
        """
        fpga_is_programmed = await self.is_fpga_programmed_async()
        self.tcpipe.get_fpga_bitstream_crc(crc32 if fpga_is_programmed else None)

    # def clear_fpga_bitstream(self):
    #     """ Stop the operation of the FPGA.

    #     Could be used if we detect that we don't have the right kind of
    #     mezzanines.
    #     """
    #     raise NotImplementedError()


    ###################################
    # Mezzanine EEPROM access methods
    ###################################

    async def _mezzanine_eeprom_read_async(self, mezzanine):
        """ Returns the contents of the specified mezzanine's EEPROM.
        """
        return None

    # Backplane/crate-related methods

    ###################################
    # Motherboard EEPROM access methods
    ###################################

    async def _write_motherboard_spi_eeprom_base64(self, *args, **kwargs):
        return None

    def _eeprom_write_ipmi(self, serial_number, product_version):
        """Write IPMI-formatted EEPROM.

        These fields are read back and parsed by software, so you have
        to get them right or things will misbehave. This method currently
        expects the following formatting::

            m._eeprom_write_ipmi(serial_number="004", product_version="2")

        """

        fru = ipmi_fru.FRU(
            board=ipmi_fru.Board(
                mfg_date=datetime.datetime.now(),
                manufacturer="Avnet",
                product_name=self.part_number,
                part_number=self.part_number,
                serial_number=serial_number,
                fru_file="",
            ),
            product=ipmi_fru.Product(
                manufacturer="Avnet",
                product_name=self.part_number,
                part_number=self.part_number,
                product_version=product_version,
                serial_number=serial_number,
                asset_tag="",
                fru_file="",
            )
        )
        # Convert IPMI structures into a byte stream to be written
        ipmi_bytes = fru.encode()
        self.i2c1_eeprom_data.write(0, ipmi_bytes)

    ###################################
    # Backplane info methods
    ###################################


    async def is_backplane_present_async(self):
        return False

    async def get_slot_number(self):
        return None

    async def get_iceboard_clock_source_async(self):
        return "CRYSTAL"

    def get_iceboard_clock_source_sync(self):
        return "CRYSTAL"

    def get_motherboard_temperature(self, sensor):
        """ Synchronous wrapper to return motherboard temperature sensor value.
        """
        return None


    #################################
    # Metrics
    #################################

    async def get_metrics_async(self):
        """ Get the motherboard hardware monitoring information.

        Returns:
            a :class:`Metrics` object.
        """

        metrics = await super().get_metrics_async()

        # Add ZCU111 monitoring data to metrics here

        return metrics

