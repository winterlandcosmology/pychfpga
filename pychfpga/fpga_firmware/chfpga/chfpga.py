#!/usr/bin/python

"""
This module defines the `chFPGA_controller` class, which provides a Python interface to operate an
IceBoard and its chFPGA firmware.

.. Notes:
..     Created 2011-01-10. See GIT for commit history.
"""

# Python Standard Library packages
import logging
import time
import os
import pickle
import base64
from datetime import datetime, timedelta
import struct
from calendar import timegm
from functools import wraps
import socket
import __main__
import asyncio
import traceback
from typing import List, Literal

# PyPi external packages
import numpy as np
import yaml

# Private external packages

from wtl.metrics import Metrics

# Local packages

from pychfpga.common import async_to_sync, run_async
from pychfpga.hardware.interfaces import TCPipe_BSB_MMI, FPGAMmi

from pychfpga.fpga_firmware import FPGAFirmware

from .chFPGA_receiver import chFPGA_receiver
from .f_engine.scaler import SCALER

# FPGA subsystems handlers
from .system import spi
from .system import i2c
from .system import gpio
from .system import sysmon
from .system import freqctr
from .system import refclk

# FPGA Channelizer (F-Engine)
from .f_engine import chan
from .f_engine import prober  # needed to access RawFrameReceiver

# FPGA Corner-turn Engine
from .ct_engine import chan_crossbar
from .ct_engine import ucorn
from .ct_engine import shuffle_crossbar
from .ct_engine import shuffle
from .ct_engine import gpu
from .ct_engine import cge
from .ct_engine import ucap

# FPGA Correlator (X-Engine)
from .x_engine import CORR, UCORR  # 16-channel correlator (if implemented in firmware)


# Default ADC delays
ADC_DELAYS_MGK7MB_REV2_MGAC08_REV2 = {
    'valid': True,
    'sync_delays': (4, 5),
    0:  {'tap_delays': [16] * 8,                           'sample_delay': 3, 'clock_delay': 0},  # CH0
    1:  {'tap_delays': [7] * 8,                            'sample_delay': 3, 'clock_delay': 0},  # CH1
    2:  {'tap_delays': [22] * 8,                           'sample_delay': 3, 'clock_delay': 0},  # CH2
    3:  {'tap_delays': [19] * 8,                           'sample_delay': 3, 'clock_delay': 0},  # CH3
    4:  {'tap_delays': [15] * 8,                           'sample_delay': 3, 'clock_delay': 0},  # CH4
    5:  {'tap_delays': [14, 13, 14, 14, 13, 14, 15, 14], 'sample_delay': 3, 'clock_delay': 0},  # CH5
    6:  {'tap_delays': [18] * 8,                           'sample_delay': 3, 'clock_delay': 0},  # CH6
    7:  {'tap_delays': [17] * 8,                           'sample_delay': 4, 'clock_delay': 0},  # CH7
    8:  {'tap_delays': [15, 17, 15, 18, 17, 14, 17, 15], 'sample_delay': 3, 'clock_delay': 0},  # CH8
    9:  {'tap_delays': [16] * 8,                           'sample_delay': 4, 'clock_delay': 0},  # CH9
    10: {'tap_delays': [20] * 8,                           'sample_delay': 3, 'clock_delay': 0},  # CH10
    11: {'tap_delays': [18] * 8,                           'sample_delay': 3, 'clock_delay': 0},  # CH11
    12: {'tap_delays': [15] * 8,                           'sample_delay': 3, 'clock_delay': 0},  # CH12
    13: {'tap_delays': [18] * 8,                           'sample_delay': 3, 'clock_delay': 0},  # CH13
    14: {'tap_delays': [18] * 8,                           'sample_delay': 3, 'clock_delay': 0},  # CH14
    15: {'tap_delays': [16] * 8,                           'sample_delay': 3, 'clock_delay': 0}   # CH15
}

# Select the default delay table
ADC_DELAY_TABLE = ADC_DELAYS_MGK7MB_REV2_MGAC08_REV2
ADC_DELAY_TABLE_FOLDER = os.path.join(os.path.dirname(__file__), '../../adc_delay_tables')  # relative to this module location

class chFPGA_config(object):
    """
    Simple namespace that holds chFPGA configuration information stored within its attributes. Is returned
    by `chFPGA_controller.get_config()`.
    """
    def __str__(self):
        return '\n'.join(['%s = %s' % (key, repr(value)) for (key, value) in sorted(vars(self).items())])


class chFPGA(FPGAFirmware):
    """Provides an API to access the chFPGA firmware running on the motherboard's FPGA.

    The API provides the methods to establish communication with the FPGA to
    access its memory map, and provides objects to configure it and control
    its operations.

    This class supports two methods to access the FPGA firmware:

    - Ethernet/UDP-based Memory-mapped Interface (MMI) implemented directly by the FPGA,
      typically used by the IceBoard motherboard. The class provides SPI-based Memory-mapped
      Interface to the FPGA, which is used to configure the networking parameters in the FPGA
      via the on-board ARM processor.

    - TCPipe, TCP-based memory-map interface typically supported by the ZCU111
    """

    # Define the motherboard models and operational modes supported by this class, and associate corresponding FPGA
    # configuration bitstreams and initialization parameters The 'clock_divider' is used to know when clock frequency
    # to expect when we monitor the ADC clock with the frequency counter.This could be independent from the
    # procrssing clock.For the CRS, the procssing clock is Fs/ 8 and the monitoring clock is Fs/ 16. For the
    # IceBoard, it's both fs/4. (fs=sampling frequency)
    PLATFORM_SUPPORT = { # (platform_model, firmware_config, modes): {firmware_filename: <fw_fn>, <other platform parameters>}
        ("MGK7MB", "chFPGA", ("shuffle16", "shuffle128", "shuffle256", "shuffle512", "chan8", "chan4")): dict(firmware_url='chFPGA_MGK7MB_Rev2.bit', sampling_frequency=800e6, processing_frequency=200e6, adc_clock_divider=4),
        ("MGK7MB", "siFPGA", ("corr16",)): dict(firmware_url='SIFPGA_MGK7MB.bit', sampling_frequency=800e6, processing_frequency = 200e6, adc_clock_divider=4),
        ("MGK7MB", "chordFPGA", ("chord16",)): dict(firmware_url='chordFPGA_MGK7MB_Rev2.bit', sampling_frequency=1200e6, processing_frequency = 300e6),
        ("ZCU111", "siFPGA", ("corr4", "corr8")): dict(firmware_url='sifpga_zcu111_wrapper.bit', sampling_frequency=3000e6, processing_frequency = 375e6, adc_clock_divider=16),
        ("ZCU111", "chFPGA", ("chan8",)): dict(firmware_url='chfpga_zcu111.bit', sampling_frequency=3000e6, processing_frequency = 375e6, adc_clock_divider=16),
        ("CRS",    "siFPGA", ("corr4","corr8")): dict(firmware_url='chfpga_crs_corr.bit', sampling_frequency=3200e6, processing_frequency = 3200e6/8, adc_clock_divider=32),
        ("CRS",    "chFPGA", ("chan8", "shuffle8")): dict(firmware_url='chfpga_crs_ct.bit', sampling_frequency=3200e6, processing_frequency = 3200e6/8, adc_clock_divider=32),
    }


    ################################################################################################
    # Byte-Serial-Bus (BSB) Memory map
    ################################################################################################
    # Note: the address space for the core FPGA register is separate. See table below.

    # Memory page address offsets
    _CONTROL_BASE_ADDR = TCPipe_BSB_MMI._CONTROL_BASE_ADDR
    _STATUS_BASE_ADDR  = TCPipe_BSB_MMI._STATUS_BASE_ADDR
    _RAM_BASE_ADDR     = TCPipe_BSB_MMI._RAM_BASE_ADDR

    #Note: SPI-accessed registers are separate and use a different address space defined in `IceBoardExt`
    _TOP_BASE_ADDR      = 0x00000  #: Base address of the whome memory map, which is always zero.
    _TOP_SUBSYSTEM_INCREMENT = 0x10000  #: Address increments between top-level systems (address bits 18:16)

    # Top systems
    _SYSTEM_BASE_ADDR = 0x00000
    _SYSTEM_BASE_ADDR      = _TOP_BASE_ADDR + _TOP_SUBSYSTEM_INCREMENT * 0  #: 0x00000: System peripherals base address.
    _CHAN_BASE_ADDR        = _TOP_BASE_ADDR + _TOP_SUBSYSTEM_INCREMENT * 1  #: 0x10000: F-Engine (channelizer) base address. The ADCDAQ subsystem is located in the CHAN address space.
    _CROSSBAR1_BASE_ADDR   = _TOP_BASE_ADDR + _TOP_SUBSYSTEM_INCREMENT * 2  #: 0x20000: CT-Engine 1st CROSSBAR (channelizer crossbar) base address
    _GPU_LINK_BASE_ADDR    = _TOP_BASE_ADDR + _TOP_SUBSYSTEM_INCREMENT * 3  #: 0x30000: GPU Link base address
    _CROSSBAR3_BASE_ADDR   = _TOP_BASE_ADDR + _TOP_SUBSYSTEM_INCREMENT * 4  #: 0x40000: CT-Engine 3rd crossbar (shard with correlator)
    _CORR_BASE_ADDR        = _TOP_BASE_ADDR + _TOP_SUBSYSTEM_INCREMENT * 4  #: 0x40000: X-Engine (correlator) (shared with 3rd crossbar)
    _BP_SHUFFLE_BASE_ADDR  = _TOP_BASE_ADDR + _TOP_SUBSYSTEM_INCREMENT * 5  #: 0x50000: CT-Engine Backplane PCB and Backplane QSFP 10Gbps packet transmitter/receivers
    _CROSSBAR2_BASE_ADDR   = _TOP_BASE_ADDR + _TOP_SUBSYSTEM_INCREMENT * 6  #: 0x60000: CT-Engine 2nd CROSSBAR base address
    _UCAP_BASE_ADDR        = _TOP_BASE_ADDR + _TOP_SUBSYSTEM_INCREMENT * 7  #: 0x60000: UCAP base address

    _SYSTEM_ADDR_INCREMENT         = 0x01000  #: Address increment between each system peripheral (addressed by bits 15:12 -> 16 possible submodules)
    _CHAN_ADDR_INCREMENT           = 0x01000  #: Address increment between each channelizer (addressed by bits 15:12 -> 16 possible submodules)
    _CROSSBAR_ADDR_INCREMENT       = 0x00800  #: Address increment between subsystems in 1st, 2nd and 3rd crossbars (addressed by bits 15:11 -> 32 possible submodules)
    _GPU_LINK_ADDR_INCREMENT       = 0x00800  #: Address increment between each subsystem of the GPU links (addressed by bits 15:11 -> 32 possible submodules)
    _CORR_ADDR_INCREMENT           = 0x01000  #: Address increment between each correlator
    _BP_SHUFFLE_ADDR_INCREMENT     = 0x00800  #: Address increment between each shuffle submodule (addressed by bits 15:11 -> 32 possible submodules)

    _CHAN_SUBMODULE_ADDR_INCREMENT = 0x00200  #: Address increment between each submodule within a channelizer (ADCDAQ, FUNCGEN, FFT, SCALER etc.)


    # SYSTEM Peripherals Submodules addresses
    # _SYSTEM_GPIO_BASE_ADDR = _SYSTEM_BASE_ADDR + 0x00000
    _SYSTEM_GPIO_BASE_ADDR     = _TOP_BASE_ADDR + _SYSTEM_ADDR_INCREMENT * 0  #: 0x00000: Address of the SYSTEM.GPIO submodule
    _SYSTEM_SYSMON_BASE_ADDR   = _TOP_BASE_ADDR + _SYSTEM_ADDR_INCREMENT * 1  #: 0x01000: Address of the SYSTEM.SYSMON submodule
    _SYSTEM_FREQ_CTR_BASE_ADDR = _TOP_BASE_ADDR + _SYSTEM_ADDR_INCREMENT * 2  #: 0x02000: Address of the SYSTEM.FREQ_CTR submodule
    _SYSTEM_SPI_BASE_ADDR      = _TOP_BASE_ADDR + _SYSTEM_ADDR_INCREMENT * 3  #: 0x03000: Address of the SYSTEM.SPI submodule
    _SYSTEM_REFCLK_BASE_ADDR   = _TOP_BASE_ADDR + _SYSTEM_ADDR_INCREMENT * 4  #: 0x04000: Address of the SYSTEM.REFCLK submodule
    # _SYSTEM_I2C_BASE_ADDR = _SYSTEM_BASE_ADDR + 0x05000
    _SYSTEM_I2C_BASE_ADDR      = _TOP_BASE_ADDR + _SYSTEM_ADDR_INCREMENT * 5 #: 0x05000: Address of the SYSTEM.I2C submodule

    # Base address is always at zero so we can gather info from the FPGA
    # before we know the number of channelizers etc.

    # _GPIO_COOKIE_REG = 0x00 # Register address of the firmware cookie

    ################################################################################################
    # Core FPGA firmware registers
    ################################################################################################

    FPGA_CORE_FIRMWARE_COOKIE_ADDR        = 4 * 0 # (read only) 0xbeefface
    FPGA_APPLICATION_FIRMWARE_COOKIE_ADDR = 4 * 1 # (read-only) 0x42
    FPGA_APPLICATION_FLAGS_ADDR           = 4 * 2
    FPGA_FIRMWARE_CRC32_ADDR              = 4 * 3
    FPGA_FIRMWARE_TIMESTAMP_ADDR          = 4 * 4
    FPGA_SERIAL_NUMBER_LSW_ADDR           = 4 * 5
    FPGA_SERIAL_NUMBER_MSW_ADDR           = 4 * 6
    _FPGA_MAC_ADDR_LSW_ADDR         = 4 * 7 # FPGA listening MAC address
    _FPGA_MAC_ADDR_MSW_CMD_LISTENING_PORT_ADDR = 4 * 8 # FPGA listening MAC address and command listening port
    _FPGA_IP_ADDR_ADDR              = 4 * 9 # FPGA listening IP address
    _IRIGB_SAMPLE0_ADDR             = 4 * 10
    _IRIGB_SAMPLE1_ADDR             = 4 * 11
    _IRIGB_SAMPLE2_ADDR             = 4 * 12

    _IRIGB_TARGET0_ADDR             = 4 * 13
    _IRIGB_TARGET1_ADDR             = 4 * 14
    _IRIGB_TARGET2_ADDR             = 4 * 15
    _IRIGB_EVENT_CTR_ADDR           = 4 * 16
    _IRIGB_EVENT_CTR_ADDR2          = 4 * 17
    _SFP_CONFIG_ADDR                = 4 * 18
    _SFP_STATUS_ADDR                = 4 * 19
    _CMD_REPLY_DEST_PORT_ADDR       = 4 * 20 # command reply destination port
    _IRIGB_REFCLK_SAMPLE            = 4 * 21 # lower word of REFCLK counter
    _IRIGB_REFCLK_SAMPLE2           = 4 * 22 # upper word of REFCLK counter
    _FPGA_DATA_DEST_MAC_ADDR_LSW_ADDR         = 4 * 23 # Data packet destination MAC address
    _FPGA_DATA_DEST_MAC_ADDR_MSW_IP_PORT_ADDR = 4 * 24 # Data packet destination MAC address and port
    _FPGA_DATA_DEST_IP_ADDR_ADDR              = 4 * 25 # Data packet destination IP address


    ################################################################################################
    # Platform information
    ################################################################################################

    _PLATFORM_ID_ML605 = 0  #: ID number for the Virtex-6-based Xilinx ML606 Evaluation board
    _PLATFORM_ID_KC705 = 1  #: ID number for the Kintex-7-based Xilinx KC705 Evaluation board
    _PLATFORM_ID_MGK7MB_REV0 = 2  #: ID number for the McGill MGK7MB Rev 0 motherboard (a.k.a Iceboard Rev 0). Works for All subsequent revs.
    _PLATFORM_ID_MGK7MB_REV2 = 3  #: ID number for the McGill MGK7MB Rev 2 motherboard (a.k.a Iceboard Rev 2). Works for All subsequent revs.
    _PLATFORM_ID_ZCU111 = 4  #: ID number for Xilinx ZCU111 evaluation board.
    _PLATFORM_ID_CRS = 5  #: ID number for Xilinx ZCU111 evaluation board.

    #: Map of all supported platform indexed by the `PLATFORM_ID` returned by the FPGA
    _PLATFORM_ID_LIST = {
        # ID: ( Board name, class to instantiate)
        _PLATFORM_ID_ML605: ('Virtex 6 (XC6V240T-1 FFG1156) on Xilinx ML605 Evaluation board', None),
        _PLATFORM_ID_KC705: ('Kintex 7 (XC7K325T-2 FFG900C) on Xilinx KC705 Evaluation board', None),
        _PLATFORM_ID_MGK7MB_REV0: ('Kintex 7 (XC7K420T-2 FFG901) on McGill MGK7MB / ICEBoard Rev0', None),
        _PLATFORM_ID_MGK7MB_REV2: ('Kintex 7 (XC7K420T-2 FFG901) on McGill MGK7MB / ICEBoard Rev2', None),
        _PLATFORM_ID_ZCU111:  ('Zynq Ultrascale+ RfSoC (ZU28) Xilinx ZCU111 Evaluation Board', None),
        _PLATFORM_ID_CRS:  ('Zynq Ultrascale+ RfSoC (ZU48) t0 technology CRS Board ', None),
    }

    # UDP communication constants (used if implemented in firmware)

    _FPGA_CONTROL_BASE_PORT = 41000
    _BROADCAST_BASE_PORT = 41000
    _ZCU111_LOCAL_DATA_PORT_NUMBER = 41000 # port to which the ZCU111 is sending its adc/correlator UDP packets

    _XILINX_OUI = 0x000A35


    _CHFPGA_COOKIE = 0x42  # Expected cookie value for chFPGA, both on the SPI and UDP MMI

    # GPIO Register addresses
    _GPIO_COOKIE_REG = _STATUS_BASE_ADDR + _SYSTEM_GPIO_BASE_ADDR  # Register address of the firmware cookie
    _FPGA_TIMESTAMP_ADDR = _STATUS_BASE_ADDR + _SYSTEM_GPIO_BASE_ADDR + 7
    _FPGA_SERIAL_NUMBER_ADDR = _STATUS_BASE_ADDR + _SYSTEM_GPIO_BASE_ADDR + 12
    _FPGA_IP_SETUP_BASE_ADDR = _CONTROL_BASE_ADDR + _SYSTEM_GPIO_BASE_ADDR + 13
    # Unused addresses:
    #   (13-18): target MAC,
    #   (19-22): target IP,
    #   (23-24): target_base_port,
    #   (25-32) = Target FPGA serial,
    #   (33): bit 7 = trigger, bits 3:2: mac source select, 1:0: broadcast group

    # # Register address of the first byte of the IP config word
    # _GPIO_IPCONFIG_REG       = _CONTROL_BASE_ADDR + _SYSTEM_GPIO_BASE_ADDR + 0x08D



    def __init__(self, motherboard, **fw_params):
        """
        Creates an empty chFPGA firmware handler object, but do not interact with the board yet.

        Parameters:

            motherboard (Motherboard subclass): Motherboard platform on which the FPGA is located.

            **fw_params (dict): Extra parameters to be associated with this
                  version of the firmware. Those are obtained from the platform
                  support table. They will be used during the init phase.


        Note: `__init__` *only* create an empty `chFPGA` object and hold basic
        configuration information but does not attempt to interact with the
        FPGA yet. Interaction with the FPGA starts with `open_async`. Full
        firmwar einitialization is performed by `init_async`. This means that
        `chFPGA` objects can be created for board that do not exist are are
        not powered up yet. This is useful when arrays of boards are loaded
        from an unfiltered hardware map.
        """

        # Initialize basic instance attributes, but don't do anything yet that involves communicating with the firmware.

        self.mb = motherboard  # instance of the motherboard object. Needs to be define before we use repr()
        self.mezzanine = self.mb.mezzanine # shortcut to the Motherbord mezzanine object

        self.logger = logging.getLogger(__name__)
        self.logger.debug(f"{self!r}: Creating chFPGA FPGAFirmware object")


        # Firmware attributes provided by PLATFORM_SUPPORT. Those will be used later by `init_async`.
        self.fw_params = fw_params

        # Firmware attributes that will be intialized later by `init_async`
        self.processing_frequency = None
        self._sampling_frequency = None  # Set in init()
        self._reference_frequency = None  # set in init()
        self.FRAME_PERIOD = None
        self._FMC_present = []  # indicates if the FMC board is present. If not, the modules will act accordingly.
        self._last_init_time = None
        self.recv = None # raw data capture receiver object
        self.corr_recv = None # correlator data receiver object
        self.mmi = None

        self.PLATFORM_ID = None # Platform will be identified once communication is established with the FPGA. Might be called by get_metrics() before that.

        # IRIG-B year and day processing. If True, target IRIGB year and day
        # will always be written as zero binary values to be compatible with
        # the IRIG-B generator
        self.zero_target_irigb_year_and_day = False

        # Firmware attributes used for QC testing
        self.adc_clk_err_ctr = 0
        self.adc_clk_err_msgs = []
        self.udp_err_ctr = 0
        self.udp_err_msgs = []

    def get_id(self, lane=None):
        return self.mb.get_id(lane=lane)

    @property
    def slot(self):
        return self.mb.slot

    @property
    def crate(self):
        return self.mb.crate


    async def open_mmi_async(self):
        """
        Create a Memory-mapped interface (MMI) to the FPGA registers through the Byte-serial bus (BSB).

        Depending on the platform, BSB access is provided through:

            - Direct UDP communication with the FPGA
            - TCPipe connection with the on-board processor (requires the self.mb.tcpipe object)


        Parameters:

            udp_retries (int): How many times UDP commands will be retried
                before an IOError exception is raised. If `None`, the default
                value set at object creation will be used.

            fpga_ip_addr_fn (function): Function used to compute the FPGA IP
                address out of the ARM IP address. If `None`, the default
                value set at object creation will be used.

            interface_ip_addr (str): IP address of the interface to be used to
                communicate with both the ARM and FPGA. If `None`, the interface
                will be detected automatically by establishing a connection with
                the ARM.

        """

        # print '%r: opening MMI core' % self

        # Check if core communications with the FPGA was already opened
        if self.mmi:
            # if any(x is not None for x in [udp_retries, fpga_ip_addr_fn, interface_ip_addr]):
            raise RuntimeError('Attempting to re-open an already-open '
                               'MMI communication channel with the FPGA')

        # Check if the FPGA is programmed
        if not (await self.mb.is_fpga_programmed_async()):
            raise RuntimeError(
                f"{self!r}: The FPGA is not programmed with a bitstream. "
                f'Cannot open link with the FPGA and initialize it')

        # Discover ethernet interface through which we communicate withthe
        # motherboard. We will use the same interface to listen to data.
        self.set_interface_ip_address()

        # Creates the MMI interface and open communications to the FPGA through it.
        if self.mb.part_number in ("ZCU111", "CRS"):
            self.mmi = TCPipe_BSB_MMI(self.mb.tcpipe)
        elif self.mb.part_number == "MGK7MB":
            self.mmi = await self.open_udp_mmi_async()
        else:
            raise RuntimeError(f'Motherboard on type {self.mb.part_number} does not support any MMI/BSB interface to the FPGA')

    async def close_mmi_async(self):
        if self.mmi:
            self.mmi.close()
            self.mmi = None

    def set_interface_ip_address(self):
        """Sets the IP address of the interface through which the host running pychfpga
        communicates with the motherboard, if this interface is not yet defined.

        This sets the ``self.interface_ip_addr`` attribute. The interface is not changes if it already is non-zero.

        The interface if obtained by opening a TCP socket to the motherboard processor and inspecting the
        socket that was used.

        This is used to open targeted UDP sockets, and therefore assumes that
        the UDP data or command packets are arriving through the same
        interface (even if, in some cases, the data comes from a separate
        FPGA-bound ethernet port instead of the processor's port).
        """

        if not self.interface_ip_addr:  # set the interface only of we haven't manually defined one
            if self.mb.hostname:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.connect((self.mb.hostname, self.mb.port))
                self.logger.debug(f'{self!r}: Established TCP connection with {s.getsockname()} to determine interface.')
                (self.interface_ip_addr, _) = s.getsockname()
                s.close()
            else:
                self.interface_ip_addr = None




    ######################################################
    # Firmware UDP stack methods
    ######################################################
    # These methods are used to set-up and operate the firmware UDP stack and
    # use it to establish a UDP-based FPGA MMI interface.

    async def open_udp_mmi_async(self):
        """ Setup the FPGA UDP communication and return a mmi object.

        This method is IceBoard-specific.
        """

        self.logger.debug(f'{self!r}: Opening UDP connection to the FPGA')
        # # Overrides communication parameter defaults if specified
        # if udp_retries is not None:
        #     self.udp_retries = udp_retries

        # if fpga_ip_addr_fn is not None:
        #     self.fpga_ip_addr_fn = fpga_ip_addr_fn

        # if interface_ip_addr is not None:
        #     self.interface_ip_addr = interface_ip_addr


        # # Check the FPGA firmware cookie obtained through the ARM SPI interface to the FPGA
        # cookie = await self.get_fpga_application_cookie()
        # if cookie != self._CHFPGA_COOKIE:
        #     raise RuntimeError(
        #         '%r: The firmware currently configured on the FPGA is not '
        #         'chFPGA (got cookie 0x%04X instead of 0x%04X). '
        #         'Direct UDP link to FPGA and other chFPGA-specific methods and '
        #         'resources are not available.' % (self, cookie, self._CHFPGA_COOKIE))

        # self.fpga_serial_number = self.get_fpga_serial_number()  # Get SN from the SPI link (slow)

        # Compute the IP address to use for the FPGA UDP interface For now, we
        # replace a.b.c.d by a.b.3.d. We need to find a more generic mechanism
        # for this (like obtaining another IP from the DHCP server)
        if not self.fpga_ip_addr:
            ip_packed = socket.inet_aton(await self.mb.get_motherboard_ip_address())
            ip_tuple = tuple(c for c in ip_packed)
            ip_packed = bytes(self.fpga_ip_addr_fn(*ip_tuple))
            # ip_packed = ip_packed[:2] + chr(3) + ip_packed[3]
            self.fpga_ip_addr = socket.inet_ntoa(ip_packed)

        # Compute the local port number if requested (self.local_control_port_number
        # is None) and if possible (there is a slot and crate number)
        if self.local_control_port_number is None:
            if not self.slot or not self.crate or self.crate.crate_number is None:
                self.local_control_port_number = 0
                self.logger.debug(
                    f'{self!r}: Cannot use slot/crate_number-based UDP port number '
                    f'for UDP control channel. There is no slot or crate_number'
                    f' info. Using OS-assigned random port')
            else:
                self.local_control_port_number = self._FPGA_CONTROL_BASE_PORT + 16*self.crate.crate_number + (self.slot-1)
                # self.logger.info('%r: Replies will be sent to %s:%i'
                #                  % (self, self.interface_ip_addr, self.local_control_port_number))

        # -------------------------------------------------------------------------
        # Open the UDP MMI interface
        # -------------------------------------------------------------------------
        self.logger.info(
            '%r: Opening FPGA MMI with FPGA=(%s:%s),  local=(%s:%s)' % (
                self,
                self.fpga_ip_addr,
                self.fpga_control_port_number,
                self.interface_ip_addr,
                self.local_control_port_number))


        self.mmi = FPGAMmi(
            fpga_ip_addr=self.fpga_ip_addr,
            fpga_port_number=self.fpga_control_port_number,  # none or 0: use local port number
            interface_ip_addr=self.interface_ip_addr,
            local_port_number=self.local_control_port_number,  # 0 = randomly assigned by os
            udp_retries=self.udp_retries,
            parent=self)

        self.mmi.open()
        self.local_control_port_number = self.mmi.local_port_number
        self.fpga_control_port_number = self.mmi.fpga_port_number

        # Select the fpga port number
        # if not self.fpga_control_port_number:
        #    self.fpga_control_port_number = self.local_control_port_number

        # Set-up the FPGA networking parameters using the ARM-SPI link to the
        # FPGA We assume it has been checked that the proper version of the SD
        # card is present so the SPI communication  with the FPGA will work.
        self.fpga_mac_addr = await self.set_fpga_control_networking_parameters_async(
            fpga_ip_addr=self.fpga_ip_addr,
            fpga_port_number=self.fpga_control_port_number)

        for trial in range(10):
            # Make sure the PMA core uses the SGMII protocol with autonegociation
            self.logger.debug(f"{self!r}: Initializing SFP in SGMII mode with autonegociation (trial {trial+1})")
            await self.set_sgmii_config_vector(an=True, reset=1)
            await self.set_sgmii_config_vector(an=True, reset=0)
            await asyncio.sleep(0.150)  # wait for autonegotiation to complete
            status = await self.get_sgmii_status_vector()
            if status & 1:
                self.logger.info(f"{self!r}: SFP successfully established link with autonegociation (1000BASE-X mode)")
                break
            else:
                self.logger.debug(f"{self!r}: SFP did not respond to autonegociation on trial {trial+1}. It might not support it. Retrying without autonegociation.")
                await self.set_sgmii_config_vector(an=False, reset=1)
                await self.set_sgmii_config_vector(an=False, reset=0)
                await asyncio.sleep(0.150)  # wait for autonegotiation to complete
                status = await self.get_sgmii_status_vector()
                if status & 1:
                    self.logger.info(f"{self!r}: SFP successfully established link without autonegociation (SGMII mode)")
                    break
                raise IOError(f"{self!r}: Cannot get the SFP module to establish a link")

        self.logger.debug(f"{self!r}: Clearing FPGAs UDP communication stack")
        await self.reset_fpga_udp_stack()
        # self.mmi.flush()

        # -------------------------------------------------------------------------
        # Check if we can communicate with the FPGA over the direct Ethernet
        # link by reading the UDP MMI cookie (not the SPI one) and check if
        # the cookie correspond to the chFPGA firmware.
        # -------------------------------------------------------------------------

        self.logger.info(
            f"{self!r}: UDP communication link with the FPGA established "
            f"at {self.fpga_ip_addr}:{self.fpga_control_port_number}")


        return self.mmi



    async def open_async(
           self,
           init=1,
           verbose=0,
           fpga_ip_addr=None,
           fpga_ip_addr_fn=None, # lambda a, b, c, d: (a, b, 3, d),
           fpga_control_port_number=None,
           local_control_port_number=None,
           interface_ip_addr=None,
           udp_retries=10):
        """
        Opens communication with the FPGA, retrieves the firmware configuration information and
        create the Python objects needed to operate the firmware. If `init` =1, the :meth:`init`
        method will be called to initialize the FPGA. Otherwise, this is a read-only operation, i.e.
        the state of the FPGA is unchanged.


        The parameters that control FPGA communications are the default set at
        object creation. To pass specific parameters, explicitly call
        open_core() with the desired parameters before calling open().


        Parameters:

            init (int): initialization level: If <0, do not establish
                communication with the FPGA. Otherwise, perform the
                open_async() process normally. See `init_async()` to see how this this
                parameter is also used in the following initialization step.

            verbose (int): verbosity level, which is passed to the `init()`
                method.

            fpga_ip_addr (str): FPGA's listening IP address in the form
                'xx.xx.xx.xx'. If None (default), the address will be obtained by
                converting the ARM address using the function provided in
                fpga_ip_addr_fn.

            fpga_ip_addr_fn (function): function (A,B,C,D) = fn(a,b,c,d) which
                generates the FPGA address (A,B,C,D) based on the ARM IP address
                (a,b,c,d).

            fpga_control_port_number (int): FPGA's listening port number for commands. Defaults to 41000. If None,
                the local port number is used.

            local_control_port_number (int): UDP port number to use to receive command
                replies. If 0, the number is allocated randomly by the OS. If
                None, a fixed number based on the crate_number and a slot number
                will be used if available.

            interface_ip_addr (str): IP address of the interface to be used to
                communicate with both the ARM and FPGA. If `None`, the interface
                will be detected automatically by establishing a connection with
                the ARM.
        """

        # await super().open()  # Open UDP communication link

        self.fpga_ip_addr = fpga_ip_addr
        self.fpga_ip_addr_fn = fpga_ip_addr_fn
        # if fpga_ip_addr_fn is not None:
        #     self.logger.warning('fpga_ip_addr_fn will be deprecated. Instead specify fpga_ip_addr in the hardware map')
        self.udp_retries = udp_retries
        self.fpga_control_port_number = fpga_control_port_number
        self.local_control_port_number = local_control_port_number
        self.interface_ip_addr = interface_ip_addr

        self.logger.debug(f'{self!r}: Opening communication with the FPGA with parameters '
                          f'fpga_ip_addr={fpga_ip_addr}, fpga_control_port_number={fpga_control_port_number}, '
                          f'local_control_port_number={local_control_port_number}, interface_ip_addr={interface_ip_addr}, '
                          f'init={init}'
                         )

        # If init<0, we do not perform any communication with the FPGA, so we don't read the firmware configuration
        if init < 0:
            self.logger.warning(f'{self!r}: Upon user request (init < 0), communication with the FPGA are inhibited. '
                              'Initialization sequence stops here. Use this for debug only.')
            return

        # Open a communicaition link with the FPGA to create the BSB MMI interface
        await self.open_mmi_async()
        self.mmi.flush()


        try:  # catch initialization errors so we can free the socket for future instantiation
            # Try to communicate with the FPGA by reading he firmware BSB Registers cookie
            try:
                # Instantiate and initialize the GPIO subsystem
                self.logger.debug('%r: === Instantiating and initializing GPIO' % self)
                await asyncio.sleep(0)
                self.GPIO = gpio.GPIO(self, self._SYSTEM_GPIO_BASE_ADDR)
                # self.GPIO.init() # don't call init() yet as this sends some commands. The module can still read the cookie without it.

                # Read the firmware version cookie from the GPIO subsystem (this
                # is provided by the FPGA core firmware which is always present on
                # all versions of the FPGA)
                cookie = await self.get_fpga_firmware_cookie(resync=True)
            except IOError as e:
                error_message = (
                    f"{self!r}: Unable to communicate with the FPGA "
                    f"due to the following exception: {e!r}")
                self.logger.error(error_message)
                raise

            # Check the Core Register cookie
            if cookie != self._CHFPGA_COOKIE:
                error_message = (
                    f'{self!r}: The firmware is not chFPGA. The magic cookie '
                    f'returned by the FPGA is 0x{cookie:02X}, '
                    f'whereas we expected 0x{self._CHFPGA_COOKIE:02X}')
                self.logger.error(error_message)
                raise RuntimeError(error_message)

            self.logger.debug(
                f"{self!r}: Established a connection with the FPGA")

            self.logger.debug(f'{self!r}:    ---> Hello! This is chFPGA! <---')


            # for _ in range(100):
            #     print (await self.get_fpga_firmware_cookie(resync=True))
            # return

            self.GPIO.init()

            # Create handware handling objects
            #
            #  NOTE: Does not initialize them yet because some modules are
            #  interdependent - we need to wait until all of them are
            #  instantiated.
            #
            #  NOTE: The instantiation does not initiate communicattion with
            #  the hardware yet. this is done in the INIT phase.

            # ---------------------------------------------------------------------
            # -- Create basic FPGA resource handlers objects
            # ---------------------------------------------------------------------

            self.GPIO.set_channelizer_reset(True)  # stop the channelizer from sending data while we initialize

            # get system constants from the FPGA
            self.logger.debug('%r: === Getting board info information' % self)
            await asyncio.sleep(0)
            self.PLATFORM_ID = self.GPIO.PLATFORM_ID
            if self.PLATFORM_ID not in self._PLATFORM_ID_LIST:
                raise RuntimeError('%r: Platform ID 0x%02X is not recognized' % (self, self.PLATFORM_ID))


            self._NUMBER_OF_FMC_SLOTS = self.mb.NUMBER_OF_FMC_SLOTS

            self.NUMBER_OF_ADCS = self.GPIO.NUMBER_OF_ADCS

            # Set platform/implementation-specific features & constants based on a local table
            if self.PLATFORM_ID in (self._PLATFORM_ID_ZCU111, self._PLATFORM_ID_CRS):
                assert self.mb.part_number == "ZCU111" or self.mb.part_number == "CRS", 'This version of the firmware is meant to operate on the ZCU111 only'
                self.HAS_REFCLK = False
                self.HAS_SPI = False
                self.HAS_I2C = False
                self.HAS_ADCDAQ = False
                self.MAX_BSB_COMMAND_LENGTH = 512
                self.CROSSBAR1_TYPE = "URAM"
                self.GPU_LINK_TYPE = "100GE"
                self.CAPTURE_TYPE = "UCAP"
                self.CORR_TYPE = "UCORR44"
                self.ADC_FREQS_TO_CHECK = range(self.NUMBER_OF_ADCS)

            elif self.PLATFORM_ID in (self._PLATFORM_ID_MGK7MB_REV0, self._PLATFORM_ID_MGK7MB_REV2):
                assert self.mb.part_number == "MGK7MB", 'This version of the firmware is meant to operate on the MGK7MB (IceBoard) only'
                self.HAS_REFCLK = True
                self.HAS_SPI = True
                self.HAS_I2C = True
                self.HAS_ADCDAQ = True
                self.MAX_BSB_COMMAND_LENGTH = 2048  # maybe more, depends on the UDP bufer
                self.CROSSBAR1_TYPE = "BRAM"
                self.GPU_LINK_TYPE = "10GE"
                self.CAPTURE_TYPE = "PROBER"
                self.CORR_TYPE = "CORR44"
                self.ADC_FREQS_TO_CHECK = (0,4,8,12)
            else:
                raise RuntimeError(f'Unknown feature list for PLATFORM_ID = {self.PLATFORM_ID}')


            # Set platform/implementation-specific features & constants based on values reported by the FPGA GPIO module
            #
            # Get frame size info, i.e number of samples per frame
            self._LOG2_FRAME_LENGTH = self.GPIO.LOG2_FRAME_LENGTH
            self.FRAME_LENGTH = 2**self._LOG2_FRAME_LENGTH  # 2**11 = 2048 time samples per frame
            self.ADC_SAMPLES_PER_FRAME = self.FRAME_LENGTH  # more explicit name

            self.NUMBER_OF_FREQUENCY_BINS = self.FRAME_LENGTH // 2  # 1024 frequency bins per frame

            # Number of samples per word (i.e number of samples processed per clock)
            self._LOG2_SAMPLES_PER_WORD = self.GPIO.LOG2_SAMPLES_PER_WORD
            self.SAMPLES_PER_WORD = 2**self._LOG2_SAMPLES_PER_WORD

            # Number of bits and bytes per ADC sample
            self.ADC_BITS_PER_SAMPLE = self.GPIO.ADC_BITS_PER_SAMPLE
            self.ADC_BYTES_PER_SAMPLE = (self.GPIO.ADC_BITS_PER_SAMPLE + 7) // 8  # round up to the nearest byte

            # Number of bytes in a frame
            self.ADC_BYTES_PER_FRAME = self.FRAME_LENGTH * self.ADC_BYTES_PER_SAMPLE  # SAMPLES_PER_FRAME * BYTES_PER_SAMPLE

            # Identify the number of channelizers and their properties
            self.CHANNELIZERS_CLOCK_SOURCE = self.GPIO.CHANNELIZERS_CLOCK_SOURCE
            self.NUMBER_OF_CHANNELIZERS = self.GPIO.NUMBER_OF_CHANNELIZERS
            self.NUMBER_OF_ANTENNAS_WITH_FFT = self.GPIO.NUMBER_OF_CHANNELIZERS_WITH_FFT
            self.LIST_OF_ANTENNAS_WITH_FFT = list(range(self.NUMBER_OF_ANTENNAS_WITH_FFT))

            # Get corner-turn engine configuration info
            self.NUMBER_OF_CROSSBAR_INPUTS = self.GPIO.NUMBER_OF_CROSSBAR_INPUTS
            self.NUMBER_OF_CROSSBAR1_OUTPUTS = self.GPIO.NUMBER_OF_CROSSBAR1_OUTPUTS
            self.NUMBER_OF_BP_SHUFFLE_LANES = self.GPIO.NUMBER_OF_BP_SHUFFLE_LANES

            # Get GPU link configuration info
            self.NUMBER_OF_GPU_LINKS = self.GPIO.NUMBER_OF_GPU_LINKS

            # Get (optional) embedded firmware correlator configuration info and their properties
            self.NUMBER_OF_CORRELATORS_MAX = self.GPIO.NUMBER_OF_CORRELATORS
            self.NUMBER_OF_CORRELATORS = self.GPIO.NUMBER_OF_CORRELATORS
            self.LIST_OF_IMPLEMENTED_CORRELATORS = list(range(self.NUMBER_OF_CORRELATORS))
            self.NUMBER_OF_INPUTS_TO_CORRELATE = self.GPIO.NUMBER_OF_CHANNELIZERS_TO_CORRELATE
            if not self.NUMBER_OF_CORRELATORS:
                self.CORR_TYPE = None

            self.default_channels = list(range(self.NUMBER_OF_CHANNELIZERS))

            self.logger.debug('%r: Hardware platform: %s' % (self, self._PLATFORM_ID_LIST[self.PLATFORM_ID][0]))
            self.logger.debug('%r: Firmware timestamp: %s' % (self, self.get_version()))
            self.logger.debug('%r: Number of channelizers: %i' % (self, self.NUMBER_OF_CHANNELIZERS))
            self.logger.debug('%r: Number of channelizers with FFT: %i (antennas %s)' % (
                self,
                len(self.LIST_OF_ANTENNAS_WITH_FFT),
                str(self.LIST_OF_ANTENNAS_WITH_FFT)))
            self.logger.debug('%r: Crossbar configuration: %i inputs x %i outputs' % (
                self,
                self.NUMBER_OF_CROSSBAR_INPUTS,
                self.NUMBER_OF_CROSSBAR1_OUTPUTS))
            self.logger.debug('%r: Number of correlators: %i (correlators %s)' % (
                self,
                len(self.LIST_OF_IMPLEMENTED_CORRELATORS),
                str(self.LIST_OF_IMPLEMENTED_CORRELATORS)))
            self.logger.debug('%r: Number of channelizers supported by the correlators: %i ' % (
                self,
                self.NUMBER_OF_INPUTS_TO_CORRELATE))

            await asyncio.sleep(0)

            self.logger.debug('%r: === Instantiating FPGA ressources' % self)

            self.logger.debug('%r: === Instantiating SYSMON' % self)
            self.SYSMON = sysmon.SYSMON(self, self._SYSTEM_SYSMON_BASE_ADDR)

            if self.HAS_SPI:
                self.logger.debug('%r: === Instantiating SPI' % self)
                self.SPI = spi.SPI(self, self._SYSTEM_SPI_BASE_ADDR)
            else:
                self.SPI = None

            if self.HAS_I2C:
                self.logger.debug('%r: === Instantiating I2C' % self)
                self.I2C = i2c.I2C(self, self._SYSTEM_I2C_BASE_ADDR)
            else:
                self.I2C = None


            self.logger.debug('%r: === Instantiating FreqCtr' % self)
            self.FreqCtr = freqctr.FreqCtr(self, self._SYSTEM_FREQ_CTR_BASE_ADDR)

            if self.HAS_REFCLK:
                self.logger.debug('%r: === Instantiating REFCLK' % self)
                self.REFCLK = refclk.REFCLK(self, self._SYSTEM_REFCLK_BASE_ADDR)
            else:
                self.REFCLK = None

            self.logger.debug('%r: === Instantiating CHAN' % self)
            # Instantiate a channelizer for for each input
            self.chan = chan.ChanArray(
                self,
                self._CHAN_BASE_ADDR,
                self._CHAN_ADDR_INCREMENT,
                self._CHAN_SUBMODULE_ADDR_INCREMENT)
            self.CHAN_FMC_NUMBER = [i // 8 for i in range(self.NUMBER_OF_CHANNELIZERS)]

            await asyncio.sleep(0)

            self.logger.debug(f'{self!r}: === Instantiating 1st CROSSBAR, Type {self.CROSSBAR1_TYPE}')
            if self.CROSSBAR1_TYPE == "BRAM":
                self.CROSSBAR = chan_crossbar.ChanCrossbar(
                    self,
                    self._CROSSBAR1_BASE_ADDR,
                    self._CROSSBAR_ADDR_INCREMENT)  # CROSSBAR block
            elif self.CROSSBAR1_TYPE == "URAM":
                self.CROSSBAR = ucorn.UCorn(
                    self,
                    self._CROSSBAR1_BASE_ADDR,
                    self._CROSSBAR_ADDR_INCREMENT)
            else:
                raise RuntimeError('Unknown CROSSBAR1 type')

            if self.NUMBER_OF_BP_SHUFFLE_LANES:
                self.logger.debug('%r: === Instantiating Backplane shuffle subsystem' % self)
                self.BP_SHUFFLE = shuffle.Shuffle(
                    self,
                    self._BP_SHUFFLE_BASE_ADDR,
                    self._BP_SHUFFLE_ADDR_INCREMENT)
            else:
                self.BP_SHUFFLE = None

            if self.NUMBER_OF_BP_SHUFFLE_LANES and self.NUMBER_OF_GPU_LINKS:
                self.logger.debug('%r: === Instantiating 2nd CROSSBAR' % self)
                self.CROSSBAR2 = shuffle_crossbar.ShuffleCrossbar(
                    self,
                    self._CROSSBAR2_BASE_ADDR,
                    self._CROSSBAR_ADDR_INCREMENT,
                    crossbar_level=2,
                    number_of_bin_sel=2)  # CROSSBAR block

                self.logger.debug('%r: === Instantiating 3rd CROSSBAR' % self)
                self.CROSSBAR3 = shuffle_crossbar.ShuffleCrossbar(
                    self,
                    self._CROSSBAR3_BASE_ADDR,
                    self._CROSSBAR_ADDR_INCREMENT,
                    crossbar_level=3,
                    number_of_bin_sel=8)  # CROSSBAR block
            else:
                self.CROSSBAR2 = None
                self.CROSSBAR3 = None

            if self.NUMBER_OF_CORRELATORS:
                if self.CORR_TYPE == "CORR44":
                    self.logger.debug('%r: === Instantiating CORR' % self)
                    self.CORR = CORR.CORR(self, self._CORR_BASE_ADDR, self._CORR_ADDR_INCREMENT) # Correlator (XMUL, ACC) for each correlator
                elif self.CORR_TYPE == "UCORR44":
                    self.logger.debug('%r: === Instantiating UCORR44' % self)
                    self.CORR = UCORR.UCORR(self, self._CORR_BASE_ADDR, self._CORR_ADDR_INCREMENT) # Correlator (XMUL, ACC) for each correlator
            else:
                self.CORR = None

            if self.NUMBER_OF_GPU_LINKS:
                self.logger.debug(f'{self!r}: === Instantiating GPU LINK(S), Type={self.GPU_LINK_TYPE}')
                if self.GPU_LINK_TYPE=='10GE':
                    self.GPU = gpu.GPU(self, self._GPU_LINK_BASE_ADDR, self._GPU_LINK_ADDR_INCREMENT)
                elif self.GPU_LINK_TYPE=='100GE':
                    self.GPU = cge.CGE(self, self._GPU_LINK_BASE_ADDR, self._GPU_LINK_ADDR_INCREMENT)
                else:
                    raise RuntimeError(f'Unknown GPU link type {self.GPU_LINK_TYPE}')
            else:
                self.GPU = None

            if self.CAPTURE_TYPE == 'UCAP':
                self.logger.debug(f'{self!r}: === Instantiating UCAP')
                self.UCAP = ucap.UCAP(self, self._UCAP_BASE_ADDR, 0)
            elif self.CAPTURE_TYPE !='PROBER':
                raise RuntimeError(f'Unknown Data capture type {self.CAPTURE_TYPE}')

            self.logger.debug('%r: This motherboard has %i FMC slots' % (self, self._NUMBER_OF_FMC_SLOTS))

            # ---------------------------------------------------------------------
            # -- Create ADC board hardware ressource handlers objects
            # ---------------------------------------------------------------------

            await asyncio.sleep(0)
            self.logger.debug('%r: === Analyzing available FMC Mezzanines' % self)
            # self._adc_board = [
            #     self.mezzanine.get(1, None),
            #     self.mezzanine.get(2, None)]

            self._FMC_present = [False] * self._NUMBER_OF_FMC_SLOTS
            for fmc_number in range(self._NUMBER_OF_FMC_SLOTS):
                if fmc_number+1 in self.mezzanine.keys():
                    self._FMC_present[fmc_number] = True
                    self.logger.debug(f'{self!r}:   An MGADC08 ADC Board is present on FMC slot {fmc_number}')
                else:
                    self.logger.warning(f'{self!r}:   An MGADC08 ADC Board is *not* present on FMC slot {fmc_number}')

            # Determine if the FMC board corresponding to each channelizer is present
            # self.ANT_FMC_IS_PRESENT = [self._adc_board[self.CHAN_FMC_NUMBER[i]].is_present() for i in range(self.NUMBER_OF_CHANNELIZERS)]
            self.ANT_FMC_IS_PRESENT = [False] * self.NUMBER_OF_CHANNELIZERS
            for (chan_number, fmc_number) in enumerate(self.CHAN_FMC_NUMBER):
                if fmc_number + 1 in self.mezzanine.keys():
                    self.ANT_FMC_IS_PRESENT[chan_number] = True

            self._data_socket = None

        except Exception as e:
            self.logger.error('****Exception during open!****** =  %r' % e)
            await self.close_async()
            raise

    async def close_async(self):
        """
        Close chFPGA object
        """

        # # Close FMC boards
        # for mezz in self.mezzanine.values():
        #     if hasattr(mezz, 'close'):
        #         mezz.close()

        await self.close_mmi_async()
        # await super().close_async()  # Make sure we close superclasses (there are none)

    def is_open(self):
        return bool(self.mmi)

    async def init_async(
            self,
            sampling_frequency=None,
            processing_frequency=None,
            reference_frequency=None,
            adc_mode=0,
            adc_bandwidth=2,
            adc_delay_table=ADC_DELAY_TABLE,
            adc_clock_divider=None,
            data_width=4,
            group_frames=4,
            enable_gpu_link=1,
            create_receiver=False,
            verbose=0,
            **kwargs):
        """
        Initialize the FPGA firmware AND the Python objects to a known state.

        Parameters:

             sampling_frequency (float): Sampling frequency in Hz to set on the ADC Mezzanine boards. If `None`, the platform/mode default is used.
                 (default 800 MHz)

             processing_frequency (float): Frequency of the internally
                 generated channelizer signal processing clock in Hz. Is
                 compared with sampling_frequency/4 to determine if we can use
                 the internal clock instead of the ADC clock to avoid causing
                 large current spikes when we start and stop the ADCs. If `None`, the platform/mode defaut is used.

             reference_frequency (float): Frequency in Hz of the Iceboard's reference clock (default
                 is 10 MHz).

             adc_delay_table (dict): initial setting of the ADC delays. see `set_adc_delays`.
                 A default delay table is used if none is provided.

             adc_clock_divider (float): ratio between the ADC sampling frequency and the measured ADC clock speed. If `None`, the platform/mode default is used.

             data_width (int): 4 or 8. Indicate of the channelizer output is in (4+4)bit or (8+8
                 bit) mode

             group_frames (int): Number of frames per packets used by the corner-turn engine

             enable_gpu_link (bool): 1

             create_receiver (bool): False, obsolete

             verbose (int): verbose level

             kwargs (dict): Any other arguments. These are not used.

        Returns:
            None

        Note:

            Not all of the FPGA registers are rewritten durint `init()`, so it might be required to
            reprogramthe fpga to come back to a known state if manual changes were made.
        """

        self._sampling_frequency = sampling_frequency or self.fw_params.get('sampling_frequency') or 800e6;
        self._reference_frequency = reference_frequency  or self.fw_params.get('reference_frequency') or 10e6;
        self.adc_clock_divider = adc_clock_divider  or self.fw_params.get('adc_clock_divider');
        self._processing_frequency = processing_frequency  or self.fw_params.get('processing_frequency') or self._sampling_frequency / 4;

        self.FRAME_PERIOD = float(self.FRAME_LENGTH) / self._sampling_frequency
        self.FRAME_RATE = 1 / self.FRAME_PERIOD

        self.logger.debug('%r: --- Initializing FPGA subsystems' % self)

        self.logger.debug(f'{self!r}: sampling_frequency={self._sampling_frequency}')

        self.logger.debug('%r: --- Initializing GPIO' % self)

        # Initialize GPIO.
        #
        # This stops the channelizers from sending data. Needed if the FPGA
        # is flooding the buffers which prevent subsequent reads to come
        # through
        # GPIO module is already initialized
        self.GPIO.BUCK_PHASE = 0xfedcba9876543210  # debug
        if verbose >= 2:
            self.GPIO.status()


        # Module depend on the FMC_present flag after this point
        if self.REFCLK:
            self.logger.debug('%r: --- Initializing REFCLK' % self)
            await asyncio.sleep(0)
            self.REFCLK.init()
            # self.REFCLK.status()

        # Only do for ML605, not KC705 board
        self.logger.debug('%r: --- Initializing SYSMON' % self)
        await asyncio.sleep(0)
        self.SYSMON.init()
        # self.SYSMON.status()

        if self.SPI:
            self.logger.debug('%r: --- Initializing SPI' % self)
            await asyncio.sleep(0)
            self.SPI.init()
            # self.SPI.status()

        self.logger.debug('%r: --- Initializing FMC slots' % self)

        # Reduce the power load before we turn on the mezzanines
        await asyncio.sleep(0)
        if self.HAS_ADCDAQ:
            self.set_adc_mask(0)  # null the ADC data before it gets to the channelizers to reduce power consumption
        self.set_ant_reset(1)
        self.set_corr_reset(1)
        for mezz in self.mezzanine.values():
            await mezz.set_mezzanine_power_async(False)
        await asyncio.sleep(0.2)  # *** make async

        for mezz_number in self.mb.FMC_MEZZ_NUMBERS: # process every mezzanine, present or not
            if mezz_number in self.mezzanine: # if mezzanine pressent
                mezz = self.mezzanine[mezz_number]
                self.logger.debug('%r:   Powering down FMC%i' % (self, mezz_number - 1))
                await mezz.set_mezzanine_power_async(False)   # this method uses power-sequencing
                await mezz.set_mezzanine_reset_async(True)   # Puts the ADCs in reset

                await asyncio.sleep(0.2)  # *** make async
                self.logger.debug('%r:   Powering up FMC%i' % (self, mezz_number - 1))
                await mezz.set_mezzanine_power_async(True)
                # mezz.set_power(True)
                await asyncio.sleep(0.2)  # Give it some time for the power to stabilize
                # We need to initialize the ADC board before we initialize the channelizer
                # (and its data acquisition) because the delay blocks need a
                # clock
                self.logger.debug('%r:   Initializing FMC%i' % (self, mezz_number - 1))
                # Initializes the mezzanine
                # will disable the reset line
                await mezz.init(
                    sampling_frequency=self._sampling_frequency,
                    reference_frequency=self._reference_frequency,
                    processing_frequency=self._processing_frequency,
                    adc_mode=adc_mode,
                    adc_bandwidth=adc_bandwidth)
                # mezz.status()
            else:
                self.logger.debug('%r:    Skipping FMC%i initialization since no board is present in that slot' % (
                    self, mezz_number - 1))
        # self.set_sync_delays((0,0))  # debug
        self.sync()  # pulse the sync lines of the mezzanines to activate the ADC configurations
        #self.set_sync_delays((4,4))  # debug
        #self.sync()  # debug
        self.check_adc_frequencies('after 1st post-mezz-init SYNC (chan reset active) ')


        # self.logger.debug('%r:   Taking channelizers out of reset after FMC enabling' % (self))

        self.set_ant_reset(1)

        self.logger.debug('%r:   Sending sync()' % (self))
        await asyncio.sleep(0)
        self.sync()  # might be needed  to make sure that the clock is running to set delays
        self.check_adc_frequencies('after 1st channelizer reset SYNC')

        # self.logger.info('%r: --- Initializing FPGA subsystems' % self)
        self.logger.debug('%r: === Initializing Channelizers' % self)
        self.chan.init(delay_table=adc_delay_table, fmc_present=self.ANT_FMC_IS_PRESENT)
        # self.chan.status()

        self.check_adc_frequencies('after channelizer init')


        self.logger.debug('%r: === Initializing Corner-Turn engine' % self)
        self.logger.debug('%r: === Initializing 1st Crossbar' % self)
        await asyncio.sleep(0)
        if self.NUMBER_OF_CROSSBAR1_OUTPUTS > 0:
            self.logger.debug(f'{self!r}:  - 1st CROSSBAR, type {self.CROSSBAR1_TYPE}')
            self.CROSSBAR.init()
            # self.CROSSBAR.status()
        else:
            self.logger.warning("%r: There is no 1st CROSSBAR module in this firmware build "
                                 "(so there can't be data streamed to the correlators or GPU links!)" % self)


        if self.BP_SHUFFLE:
            await asyncio.sleep(0)
            self.logger.debug('%r: === Initializing Backplane Shuffle' % self)
            self.BP_SHUFFLE.init()

        self.logger.debug('%r: === Initializing 2nd Crossbar' % self)
        if self.CROSSBAR2:
            await asyncio.sleep(0)
            self.CROSSBAR2.init()
        else:
            self.logger.warning("%r: There is no 2nd CROSSBAR module in this firmware build" % self)

        self.logger.debug('%r: === Initializing 3rd Crossbar' % self)
        if self.CROSSBAR3:
            await asyncio.sleep(0)
            self.CROSSBAR3.init()
        else:
            self.logger.warning("%r: There is no 3rd CROSSBAR module in this firmware build" % self)

        self.check_adc_frequencies('after CROSSBAR init')

        if self.CORR:
            self.logger.debug('%r: === Initializing FPGA-based correlator (X-Engine)' % self)
            await asyncio.sleep(0)
            self.logger.debug('%r:  - CORR' % self)
            self.CORR.init()
        else:
            self.logger.debug('%r: There are no FPGA correlators in this firmware build' % self)

        self.set_data_width(data_width)  # Sets the data width of both the SCALER and CROSSBAR
        self.logger.debug('%r: Data width set to (Re+Im) = (%i+%i) bits' % (
            self,
            self.get_data_width(),
            self.get_data_width()))

        self.CROSSBAR.set_frames_per_packet(group_frames)
        self.logger.debug('%r: The 1st crossbar will pack %i frames per packet' % (self, group_frames))

        await asyncio.sleep(0)

        if self.GPU:
            self.logger.debug('%r: === Initializing GPU links' % self)
            self.GPU.init()
            self.GPU.set_enable(enable_gpu_link)
            self.logger.debug('%r: GPU link is currently %s' % (self, ['Disabled', 'Enabled'][bool(enable_gpu_link)]))

        self.logger.debug("%r: Done with initializations." % self)

        await asyncio.sleep(0)
        self.set_ant_reset(0)  # Disable channelizer reset

        self.check_adc_frequencies('after final channelizer reset release')


        self._last_init_time = time.time()

        # Create a data receiver
        if create_receiver:
            self.get_data_receiver()

    def check_adc_frequencies(self, stage):
        # TODO: disable if mezzanine is not attached
        # if self.mezzanine.atta
        target_frequency = self._sampling_frequency/self.adc_clock_divider

        for trial in range(10):
            freqs = [self.FreqCtr.read_frequency(f'ADC_CLK{i}', gate_time=0.001) for i in self.ADC_FREQS_TO_CHECK] # ***JFC debug
            err = any(abs(f - target_frequency) > 2.1e3 for f in freqs)
            msg = f'{self!r}: ADC output frequencies at stage {stage} are {[f/1e6 for f in freqs]} (check #{trial+1}) {"ERROR!" if err else ""}'
            if err:
                self.logger.warn(msg)
                self.adc_clk_err_ctr += 1
                self.adc_clk_err_msgs.append(msg)
            else:
                self.logger.debug(msg)
                break
        if err:
            msg = f'{self!r}: some ADCs are not generating a proper clock at stage "{stage}". Frequencies are {[f/1e6 for f in freqs]} MHz. ' \
                  f'Expected frequency is {target_frequency/1e6:.6f} MHz. Deltas = {[(f-target_frequency)/1e6 for f in freqs]}'
            self.logger.error(msg)
            pass
        return err

    ###################################
    # Core registers access methods
    ###################################
    """
    Core registers is a set of 32-bit registers that can be
    accessed both through the MMI interface AND through an external SPI link.

    When using the MMI interface, the core registers are mapped as a RAM page for the GPIO module.

    On some platforms (like the IceBoard), the SPI link is necessary at
    startup to configure the FPGA's networking stack, which is used to establish the MMI interface.

    """

    async def fpga_core_reg_read_async(self, addr):
        """ Reads the contents of a 32-bit core regster at the specified address

        The core registers are read using the MMI interface.

        Parameters:

           addr (int): address of the 32-bit core register. Must be a multiple of 4.

        Returns:

           int: 32-bit unsigned value of the register
        """
        # print(f'Reading core reg via MMI at {addr:03X}')
        if self.mmi:
            return int(self.mmi.read(self.mmi._RAM_BASE_ADDR + addr, type='<u4', length=1)) & 0xFFFFFFFF
        else:
            raise IOError('Attempted to read FPGA core registers before MMI is initialized')


    async def fpga_core_reg_write_async(self, addr, value):
        """ Reads the contents of a 32-bit core regster at the specified address

        The core registers are read using the MMI interface.

        Parameters:

            addr (int): address of the 32-bit core register. Must be a multiple of 4.

            value (int): 32-bit value to write in the register
        """
        # print(f'Writing core reg via MMI at {addr:03X}')
        if self.mmi:
            self.mmi.write(self.mmi._RAM_BASE_ADDR + addr, value.to_bytes(4, 'little'))
        else:
            raise IOError('Attempted to write FPGA core registers before MMI is initialized')


#    async def fpga_core_reg_read_async(self, addr):
#        return await self.mb.fpga_core_reg_spi_read_async(addr)

#    async def fpga_core_reg_write_async(self, addr, value):
#        await self.mb.fpga_core_reg_spi_write_async(addr, value)


    # Bitstream management

    async def get_fpga_core_cookie(self):
        """ Return the core FPGA firmware cookie. Should always be 0xBEEFFACE.

        The cookie is obtained from a core register via the MMI interface.
        """
        if not (await self.mb.is_fpga_programmed_async()):
            return None
        cookie = await self.fpga_core_reg_read_async(self.FPGA_CORE_FIRMWARE_COOKIE_ADDR)
        return cookie

    async def get_fpga_application_cookie(self):
        """ Return the application-specific FPGA firmware cookie.

        The cookie is obtained from a core register via the MMI interface.
        """
        if not (await self.mb.is_fpga_programmed_async()):
            return None
        cookie = await self.fpga_core_reg_read_async(self.FPGA_APPLICATION_FIRMWARE_COOKIE_ADDR)
        return cookie

    async def get_fpga_serial_number(self):
        """ Return the FPGA serial number, as read from the FPGA's core
        register through the MMI interface.
        """
        fpga_serial_number = (
            (await self.fpga_core_reg_read_async(self.FPGA_SERIAL_NUMBER_LSW_ADDR)) |
            (await (self.fpga_core_reg_read_async(self.FPGA_SERIAL_NUMBER_MSW_ADDR) << 32))
            )

        return fpga_serial_number

    async def get_fpga_firmware_timestamp(self):
        """ Returns a string containing the date-time of the currrent firmware
        bitstream.

        The value is retreived from the core register that exposes the
        USER_ACCESS field embedded in the firmware, which is set in Vivado to
        encode the bitstream generation time.

        The USER_ACCESS field is accessed through a core register read via the MMI interface.
        """
        timestamp = await self.fpga_core_reg_read_async(self.FPGA_FIRMWARE_TIMESTAMP_ADDR)
        seconds = (timestamp >> 0) & 0x3F
        minutes = (timestamp >> 6) & 0x3F
        hour = (timestamp >> 12) & 0x1F
        year = (timestamp >> 17) & 0x3F
        month = (timestamp >> 23) & 0x0F
        day = (timestamp >> 27) & 0x1F
        timestamp_string = '%04i-%02i-%02i %02i:%02i:%02i' % (
            year + 2000, month, day, hour, minutes, seconds)
        return timestamp_string



    async def ping_fpga_async(self, timeout=0.3):
        # Open the core right now if needed so we don't mask IOError exceptions this could generate
        if not self.mb._is_core_open():
            await self.open_core()
        try:
            self.mmi.read(self._GPIO_COOKIE_REG, type=int, length=1, timeout=timeout)
            return True
        except IOError:
            return False



    ############################
    # ICEBoard-specific methods
    ############################
    UDP_STATUS_VECT_BITS = [
            # Name, lsb pos, width
            ('tx_fifo_overflow', 17, 1),
            ('rx_fifo_overflow', 16, 1),
            ('sfp_remote_fault', 13, 1),
            ('sfp_duplex_mode', 12, 1),
            ('sfp_speed', 10, 2),
            ('rxnotintable',6, 1),
            ('rxdisperr', 5, 1),
            ('link_sync', 1, 1),
            ('link_status', 0, 1)]

    async def get_fpga_udp_metrics_async(self):
        """

        UDP metrics obtained from a core register via the MMI interface.

        """
        if not self.is_open():
            return 

        if self.PLATFORM_ID == self._PLATFORM_ID_ZCU111:
            self.logger.warning(f'{self!r} ZCU111 platform has no UDP metrics')
            return

        metrics = Metrics(
            type='GAUGE',
            slot=(self.slot or 0) - 1,
            id=self.get_string_id(),
            crate_id=self.crate.get_string_id() if self.crate else None,
            crate_number=self.crate.crate_number if self.crate else None)

        try:
            # await self.check_command_count_async(reset=True)
            metrics.add('fpga_udp_error_current_count', value=self.mmi.error_counter)
            vect = await self.fpga_core_reg_read_async(self._SFP_STATUS_ADDR)
            for name, pos, width in self.UDP_STATUS_VECT_BITS:
                metrics.add('fpga_udp_' + name, value= (vect >> pos) & (2**width-1))
        except IOError as e:
            self.logger.error('%r: Error getting FPGA udp metrics. Error is %r' % (self, e))
            self.udp_err_ctr += 1
            self.udp_err_msgs.append('%r: Error getting FPGA udp metrics. Error is %r' % (self, e))
        return metrics

    async def get_udp_status_async(self):
        """ Return a dictionary describing the status bits of the SGMII/1000BASE-X interface.
        """
        vect = await self.fpga_core_reg_read_async(self._SFP_STATUS_ADDR)
        return {name: ((vect >> pos) & (2**width-1)) for name, pos, width in self.UDP_STATUS_VECT_BITS}

    async def clear_fpga_udp_errors(self, force=False, no_reset=False, max_trials=3):
        """ Attempts to clear the FPGA UDP communication errors.

        IceBoard only.

        Parameters:

            force (bool): If True, the UDP stack is reset whether of not the
                current number of communication errors exceed the threshold or
                not.

        """
        if self.PLATFORM_ID == self._PLATFORM_ID_ZCU111:
            return

        trial = 1
        while True:
            if force or self.mmi.error_counter > 40:
                self.logger.warning(f'{self!r}: Resetting FPGA UDP stack (trial #{trial}/{max_trials})')

                if not no_reset:
                    await self.mb.reset_sfp()
                    await asyncio.sleep(0.1)
                    await self.reset_fpga_udp_stack()
                    await asyncio.sleep(0.1)
                self.logger.debug(f'{self!r}: Trying to read from FPGA UDP stack')
                try:
                    self.mmi.flush()
                    self.mmi.read(0, type=int, length=1, retry=-1, resync=1)
                    self.mmi.read(0, type=int, length=1, resync=1)
                    self.mmi.flush()
                    (cmd, rply) = self.GPIO.get_command_count()
                    self.mmi.send_counter = cmd
                    self.mmi.recv_counter = rply
                    break
                except IOError:
                    if trial >= max_trials:
                        # raise IOError(
                        #      '%r: cannot communicate with FPGA port after '
                        #      '%i FPGA UDP stack resets' % (self, trial))
                        self.logger.error(
                            f'{self!r}: cannot communicate with FPGA port '
                            f'after {trial} FPGA UDP stack resets')
                        break
                    else:
                        self.logger.warning(
                            f'{self!r}: Still obtaining FPGA UDP errors '
                            f'after {trial} FPGA UDP stack reset. Retrying...')
                    trial += 1
                finally:
                    self.mmi.error_counter = 0
                    self.logger.debug(f'{self!r}: Finished to attempt clearing FPGA UDP errors.')
            else:
                break

    async def check_command_count_async(self, reset=False):
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
                await asyncio.sleep(0)
                self.logger.debug(f'{self!r}: Checking command counters')
                (cmd, rply) = self.GPIO.get_command_count()
                await asyncio.sleep(0)
                valid = (cmd == self.mmi.send_counter & 0xFF) and (rply == self.mmi.recv_counter & 0xFF)
                if not valid:
                    self.logger.warning(
                        '%r: Command counters differ cmd/rply in FPGA is (%i, %i), '
                        'Python MMI is (%i, %i)'
                        % (self, cmd, rply, self.mmi.send_counter & 0xFF,
                           self.mmi.recv_counter & 0xFF))
                if reset:
                    self.mmi.send_counter = cmd
                    self.mmi.recv_counter = rply
                    valid = True
                break
            except IOError as e:
                self.logger.error(
                    "%r: UDP communinication error. Attempting to reset FPGA's"
                    " UDP stack via the ARM processor (trial %i).The error is:\n %s"
                    % (self, trial+1, e))
                await self.reset_fpga_udp_stack()
                valid = False
                reset = True
            except Exception as e:
                self.logger.error(
                    "%r: Unhandled error during UDP communinication check. "
                    "The error is:\n %r" % (self, e))
                raise
        else:  # executes if we exhausted the for loop iterations, i.e  no break
            errmsg = "%r: Could not re-establish UDP communinication with " \
                     "the FPGA. Raising an exception" % (self)
            self.logger.error(errmsg)
            raise IOError(errmsg)
        return valid

    async def reset_fpga_udp_stack(self):
        """

        IceBoard only.

        This method uses the IceBoard ARM-FPGA SPI link to access the core registers since access through UDB packsts might not work.
        """

        # self.logger.warning("%r: Resetting %s FPGA's UDP communication stack" % (self, self.hostname))
        await self.mb.fpga_core_reg_spi_write_async(self._SFP_STATUS_ADDR, 3 << 30)
        await self.mb.fpga_core_reg_spi_write_async(self._SFP_STATUS_ADDR, 0 << 30)
        self.mmi.send_counter = 0


    async def set_sgmii_config_vector(
            self, sgmii=1, an=1, pause=0, duplex=1, reset=0,
            los=0, fault=0, an_trig=False, loopback=0, speed=2):
        """

        This is an IceBoard-specific method.

        This is called before the UDP MMI interface exists and therefore uses the ARM-FPGA SPI link.

            bit 0: 1000BASE-X:0, SGMII=1
            bit 5: 1000BASE-X: full duplex
            bits: 8-7: 1000BASE-X: pause
            bits 13:12: 1000BASE-X: remote fault

            bit 16: unidir
            bit 17: loopback
            bit 18: power down
            bit 19: isolate
            bit 20: auto-negotiation enable

            bit 28: loss_of_signal
            bit 29: reset
            bit 30: an_restart
            bit 31: basex_or_sgmii


        """
        val = (loopback << 17) + (an << 20) + (los << 28)
        if sgmii:
            val |= (1 << 31) + (1 << 0) + (speed << 10) + (duplex << 12)
        else:
            val |= (duplex << 5) + (pause << 7) + (fault << 12)

        if reset:
            # await self.fpga_core_reg_write_async(18 * 4, val | (1<<29))
            val |= 1 << 29
        if an_trig:
            # await self.fpga_core_reg_write_async(18 * 4, val | (1 << 30))
            val |= 1 << 30
        await self.mb.fpga_core_reg_spi_write_async(self._SFP_CONFIG_ADDR, val)
        return val

    async def get_sgmii_status_vector(self):
        """

        This is an IceBoard-specific method.

        This is called before the UDP MMI interface exists and therefore uses the ARM-FPGA SPI link.

        """
        val = await self.mb.fpga_core_reg_spi_read_async(self._SFP_STATUS_ADDR)
        self.logger.debug(f'{self!r}: SGMII/1000BASE-X status: '
                          f'link={bool(val&1)}, sync={bool(val&(1<<1))}, '
                          f'RUDI={(val>>2)&(0b11111):05b}, PHY={(val>>7)&1}, '
                          f'ERR={(val>>13)&1} ERRCODE={(val>>8)&3:02b}, '
                          f'speed={(val>>10)&3:02b}, duplex={(val>>12)&1}, '
                          f'pause={(val>>14)&3:02b}')
        return val


    async def set_fpga_control_networking_parameters_async(
            self,
            fpga_mac_addr=None,
            fpga_ip_addr=None,
            fpga_port_number=_FPGA_CONTROL_BASE_PORT,
            fpga_command_reply_dest_port=0):
        """
        Set the FPGA MAC and IP addresses, and sets the UDP listening and reply destination ports of the command channel.

        This method **uses the SPI interface** to the FPGA provided by motherboard
        to access the relevant networking core registers. This method can therefore be
        called before the UDP MMI interface is initialized.

        Parameters:

            fpga_mac_addr (str): MAC address of the FPGA in the format
                'xx:xx:xx:xx:xx:xx, where 'xx' is a hex number'. If `fpga_mac_addr` is `None`, an
                arbitrary MAC address is created using the IP address to ensure its uniqueness.

            fpga_ip_addr (str):  Address at which the FPGA listens for commands. The address is in
                the format of 'a.b.c.d', where a,b,c and d are decimal numbers.

            fpga_port_number (int): Port on which the FPGA listens for commands. This port is 41000
                by default.

            fpga_command_reply_dest_port (int): port to which the command replies will be sent back towards the host. If 0,
                the destination port for the reply will be the source port of the previous command packet. This is typically used.

        Returns:

            str: MAC address that was used/computed, in the form 'xx:xx:xx:xx:xx'


        The command reply packets are sent to the following destination:

            * command reply MAC address: Is hardwired to use the source MAC address of the last valid
              packet received
            * command reply IP address: Is hardwired to use the source IP of the last valid IP packet
              received
            * command reply  port number: Is set by the `fpga_command_reply_dest_port` parameter.

        With the default configuration, the user can bind a UDP socket to any port (or let the
        system choose by specifying port 0 to the bind() method).  The outgoing command packets will
        have the source IP and source port set to that value, and reply packets will automatically
        come back to this port without having to set up the return port manually. Letting the socket
        choose ports allows the system to easily connect to multiple boards without having to ensure
        the availability of specific ports. Note that is might be necessary to bind the socket to a
        specific interface address in case there are multiple interfaces in the system.


        [Note1] The FPGA firmware allows for the UDP networking parameters to
        be set without the help of a SPI link to the FPGA core registers. This
        is done by using UDP broadcasts that select specific target FPGAs by
        their serial number.


        """

        fpga_ip_addr_bytes = socket.inet_aton(fpga_ip_addr)  #

        # Compute a MAC address for the FPGA
        # mac = socket.inet_aton(self._get_arm_mac())  #
        if fpga_mac_addr is None:
            fpga_mac_addr_bytes = bytes((0x12, 0x34)) + fpga_ip_addr_bytes
            fpga_mac_addr = ':'.join([f'{c:02X}' for c in fpga_mac_addr_bytes])
        else:
            fpga_mac_addr_bytes = bytes.fromhex(fpga_mac_addr.replace(':',''))

        fpga_mac_addr_int = int.from_bytes(fpga_mac_addr_bytes, 'big')
        fpga_ip_addr_int = int.from_bytes(fpga_ip_addr_bytes, 'big')
        # self.fpga_mac_addr = fpga_mac_addr
        # self.fpga_control_port_number = fpga_port_number
        # self.fpga_ip_addr = fpga_ip_addr


        # Set the FPGA Networking parameters over the ARM-FPGA SPI interface
        await self.mb.fpga_core_reg_spi_write_async(self._FPGA_MAC_ADDR_LSW_ADDR, fpga_mac_addr_int & 0xFFFFFFFF)
        await self.mb.fpga_core_reg_spi_write_async(self._FPGA_MAC_ADDR_MSW_CMD_LISTENING_PORT_ADDR,
            ((fpga_mac_addr_int >> 16) & 0xFFFF0000) | (fpga_port_number & 0xFFFF))
        await self.mb.fpga_core_reg_spi_write_async(self._FPGA_IP_ADDR_ADDR, fpga_ip_addr_int)

        word = await self.mb.fpga_core_reg_spi_read_async(self._CMD_REPLY_DEST_PORT_ADDR)
        await self.mb.fpga_core_reg_spi_write_async(self._CMD_REPLY_DEST_PORT_ADDR, (word & 0xFFFF0000) | (fpga_command_reply_dest_port & 0xFFFF))

        return fpga_mac_addr

    async def set_local_data_port_number_async(self, port):
        """
        Sets the port number to which the FPGA is sending data channel packets to the host. Use
        `set_data_target_address` instead.

        This method uses MMI interface to access the FPGA core registers.

        Parameters:

            port (int); target port number. If port==0, the data is sent to
                the source port number of the last received valid packet  + 1.

        Does not change the target MAC or IP address.
        """
        if self.PLATFORM_ID in (self._PLATFORM_ID_ZCU111, self._PLATFORM_ID_CRS):
            self.mb.tcpipe.core_reg_write(self.mb.tcpipe.CORE_REG_UDP_DATA_PORT, port)
        else:
            word = await self.fpga_core_reg_read_async(self._FPGA_DATA_DEST_MAC_ADDR_MSW_IP_PORT_ADDR)
            await self.fpga_core_reg_write_async(self._FPGA_DATA_DEST_MAC_ADDR_MSW_IP_PORT_ADDR, (word & 0xFFFF0000) | (port & 0xFFFF))

    async def get_local_data_port_number_async(self):
        """ Return the port number to which the FPGA is sending its captured data stream on the control network.

        This method uses MMI interface to access the FPGA core registers.

        """
        if self.PLATFORM_ID in (self._PLATFORM_ID_ZCU111, self._PLATFORM_ID_CRS):
            return self.mb.tcpipe.core_reg_read(self.mb.tcpipe.CORE_REG_UDP_DATA_PORT)
        else:
            return (await self.fpga_core_reg_read_async(self._FPGA_DATA_DEST_MAC_ADDR_MSW_IP_PORT_ADDR)) & 0xFFFF

    async def set_data_target_address_async(self, ip_addr=None, port=None, mac_addr=None):
        """
        Sets the IP address, port number and MAC address to which data is sent back to the host comptuter.

        This method sets the target address for data sent back to the host computer through the UDP
        data channel (UDP Channel 1). This is a unidirectiol UDP-to-HOST channel.

        The UDP data channel is usually used to send low-bandwidth data and is
        not to be confussed with dedicated data channels such as the 10G
        Ethernet links to GPU nodes. The UDP data channel is used by:

         - Raw data capture module (PROBER) to capture periodic rad data form the ADC or SCALER output
         - Correlator (CORR44) to stream integrated correlator products

        This method uses MMI interface to access the FPGA core registers. It must be used once the MMI interface is initialized.

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

        After initializarion, all three parameters are set to zero, meaning
        that if the FPGA receives control packets from port x, data will be
        sent back to the same host on port x+1. Only setting the port number
        ensures that the data will come back to the controlling host at the
        specified port. It is usually easier to let the operating system
        assign a random port and set that port number on the FPGA.

        """
        if self.PLATFORM_ID == self._PLATFORM_ID_ZCU111:
            self.logger.warning(
                f'{self!r}: Setting the target address for the data channel is not currently '
                f'supported by this platform. The request will be ignored. Make sure the receiver '
                f'uses the correct address and port for this platform.')
            return

        if not ip_addr:
            ip_addr_int = 0
        else:
            ip_addr_int = int.from_bytes(socket.inet_aton(ip_addr), 'big')

        if not mac_addr:
            mac_addr_int = 0
        else:
            mac_addr_int = int.from_bytes(bytes.fromhex(mac_addr.replace(':','')), 'big')

        self.logger.debug(
            f'{self!r}: setting data target address to '
            f'ip={ip_addr}({ip_addr_int}), port={port}({port}), '
            f'mac={mac_addr}({mac_addr_int})')

        # Set the UDP data channel networking parameters (MAC, IP & PORT) over the MMI interface
        await self.mb.fpga_core_reg_write_async(self._FPGA_DATA_DEST_MAC_ADDR_LSW_ADDR, mac_addr_int & 0xFFFFFFFF)
        await self.mb.fpga_core_reg_write_async(self._FPGA_DATA_DEST_MAC_ADDR_MSW_IP_PORT_ADDR, (mac_addr_int>>16) & 0xFFFF0000 | (port & 0xFFFF))
        await self.mb.fpga_core_reg_write_async(self._FPGA_DATA_DEST_IP_ADDR_ADDR, ip_addr_int)


    async def get_fpga_firmware_cookie(self, resync=False):
        """
        Get the FPGA firmware cookie.

        Reads the FPGA over the UDP link and returns the cookie that
        identifies the firmware. This method can be called before any FPGA
        modules are instatiated.

        If ``resync`` is True, the read command will reset the command
        sequence number to the value known by the FPGA. This should be is used
        by the first command sent to the FPGA to reset the communication link.
        """
        await asyncio.sleep(0)
        return self.mmi.read(self._GPIO_COOKIE_REG, type=int, length=1, resync=resync) & 0x7F

    def get_fpga_firmware_version(self):
        """
        Returns the firmware revion currenting running on the FPGA (which si
        the date and time of bitstream generation)
        """
        return self.GPIO.get_bitstream_date()

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
        return self.I2C.write_read(*args, **kwargs)

    def fpga_i2c_set_port(self, *args, **kwargs):
        """
        Sets the FPGA hardware port over which the i2c communications will be
        made after this call.

        This selects the FPGA pins over which the communications is done,
        *not* the bus selection done by an I2C switch.
        """
        return self.I2C.set_port(*args, **kwargs)

    async def _mezzanine_eeprom_read_async(self, mezzanine):
        """
        Reads the EEPROM on the specified mezzanine using the FPGA if the ARM
        firmware does not provide the functionality.

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

        # First try to read the EEPROM using the raw ARM method (if it exists)
        try:
            data = await self._tuber_mezzanine_eeprom_read_base64_async(mezzanine)
            return base64.decodebytes(data.encode())
        except (TuberRemoteError, AttributeError):
            self.logger.debug(
                f"{self!r}: Cannot read the Mezzanine {mezzanine} EEPROM through the ARM's "
                f"_mezzanine_eeprom_read_base64() method. Attempting to read "
                f"the Mezzanine EEPROM through the FPGA.")

        # We failed, so we will attempt to read it via the FPGA
        # First check if the FPGA is programmed; we need it!
        fpga_programmed = await self.mb.is_fpga_programmed_async()
        if not fpga_programmed:
            self.logger.debug(
                f"{self!r}: FPGA is not programmed, so cannot read the Mezzanine {mezzanine} "
                f"EEPROM through the FPGA.")
            return None

        # Get the first byte to determine if this is a McGill format.
        eeprom_data = self.mb.read_mezzanine_eeprom(mezzanine, 0, 1)
        if eeprom_data[0] == 0x0d:  # if this is McGill format
            self.logger.debug(
                f"{self!r}: EEPROM in Mezzanine {mezzanine} is McGill format. The FPGA will "
                f"be reading only bytes until the terminator character. ")
            # Read the eeprom block by block until we detect the end of the
            # dictionary
            block_size = 32
            string = bytearray()
            for i in range(512 / block_size):  # read 32 blocks of 16 bytes
                data_block = self.mb.read_mezzanine_eeprom(
                    mezzanine, addr=i*block_size, length=block_size, retry=3)
                string += data_block
                if (b'}' in data_block) or (255 in data_block):
                    break
            return string
        else:  # If not McGill format,
            self.logger.debug(
                f"{self!r}: EEPROM in Mezzanine {mezzanine} is not McGill format. The FPGA "
                f"will *NOT* read the EEPROM contents")
            return None

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
            'nano': lambda ts: ts.nano,
            'datetime+': lambda ts: (ts.datetime, ts.nano % 1000000000)
            }

        nano = None  # time in nanoseconds since epoch.

        def __str__(self):
            return self.isoformat()

        def __init__(self, arg=None):
            """
            """
            if arg is None:
                pass
            elif isinstance(arg, str):
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

    _IRIGB_SOURCE_TABLE = {
        'bp_trig': 0,
        'bp_time': 1,
        'irigb_gen': 2,
        'bp_gpio_int': 3,
        'sma_a': 4,
        'sma_b': 5,
        'bp_sma': 6
        }

    async def set_irigb_source_async(self, source):
        """
        Set the source of the IRIG-B signal. Also configures the user SMA as
        an 'input' if that SMA is used as a source.

        Parameters:

            source (str): Name of the source to use.

        """
        if source not in self._IRIGB_SOURCE_TABLE:
            raise ValueError(
                'Invalid IRIG-B source name. Valid names are %s'
                % ', '.join(self._IRIGB_SOURCE_TABLE.keys()))
        src = self._IRIGB_SOURCE_TABLE[source]
        w2 = await self.fpga_core_reg_read_async(self._IRIGB_SAMPLE2_ADDR)
        await self.fpga_core_reg_write_async(self._IRIGB_SAMPLE2_ADDR, (w2 & 0x3FFFFFFF) | ((src & 0b011) << 30))
        w2 = await self.fpga_core_reg_read_async(self._IRIGB_TARGET0_ADDR)
        await self.fpga_core_reg_write_async(self._IRIGB_TARGET0_ADDR, (w2 & 0x7FFFFFFF) | ((src >> 2) << 31))

        # If an user SMA is used, configure it as an input
        if source in self.GPIO.USER_OUTPUTS:
            self.set_user_output_source(output=source, source='input')


    async def get_irigb_source_async(self):
        """ Get the name of the current source of the IRIG-B signal."""
        w1 = await self.fpga_core_reg_read_async(self._IRIGB_SAMPLE2_ADDR)
        w2 = await self.fpga_core_reg_read_async(self._IRIGB_TARGET0_ADDR)

        source = ((w1 >> 30) & 0b011) | (((w2 >> 31) & 0b001) << 2)

        for (source_name, source_number) in self._IRIGB_SOURCE_TABLE.items():
            if source == source_number:
                return source_name
        raise ValueError('The IRIG-B module has an unknown source number %i' % source)

    async def detect_irigb_source_async(self, set_source=False):
        """ Returns the name of the first input on which valid IRIG-B time is detected. """
        old_source = await self.get_irigb_source_async()
        valid_source = None
        for source in self._IRIGB_SOURCE_TABLE.keys():
            await self.set_irigb_source_async(source)
            if (await self.get_irigb_time_async(noerror=True)):
                valid_source = source
                break
        await self.set_irigb_source_async(valid_source if set_source else old_source)
        return valid_source

    async def _get_irigb_time_async(self, trig=True, noerror=False):
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

        # Optionally trigger time capture, and check that the IRIG-B time is
        # captured AND to be valid. IF we trig, try to get a valid time until
        # a timeout has elapsed, otherwise fail immediately if the time was
        # not recently updated.
        t0 = time.time()
        while True:

            # Trigger the capture of the reference counter and IRIG-B time
            if trig:
                w2 = await self.fpga_core_reg_read_async(self._IRIGB_SAMPLE2_ADDR)
                # trigger time capture by creating a rising edge on `refclk_sample_trig`
                await self.fpga_core_reg_write_async(self._IRIGB_SAMPLE2_ADDR, w2 & ~(1 << 29))
                await self.fpga_core_reg_write_async(self._IRIGB_SAMPLE2_ADDR, w2 | (1 << 29))
                await asyncio.sleep(0)

            # Wait for the time capture to complete by monitoring
            # `refclk_sample_done`. Time is captured on the next 10 MHz
            # reference clock edge, so that should be quick, but we need
            # to make sure we have stable values. Timeout if it takes too long.

            # JFC: Commented out  until I fix the firmware
            # t1 = time.time()
            # while not (await self.fpga_core_reg_read_async(self._IRIGB_TARGET1_ADDR)) & (1 << 29):
            #                     # check refclk_sample_done
            #     self.logger.warning('%r: Time capture was not immediately ready - this is unexpected' % self)
            #            # Debug. should not happen since capture should be much faster
            #            # than the time it takes to read the done flag
            #     # TImeout if it takes too long. The time should be ready within a few 10 MHz cycles.
            #     if time.time() - t1 > 0.1: # 0.1s = 1,000,000 clock cycles of the 10 MHz clock. That is way enough
            #         raise RuntimeError(
            #            'Timeout while waiting for the reference clock counter '
            #            'and IRIG-B time capture to complete. Was the capture triggered?')

            # # At this point we have a stable IRIG-B timestamp ready to be read,
            # but we still don't know if the time within it is valid.

            # check the time valid (recent) flag
            w1 = await self.fpga_core_reg_read_async(self._IRIGB_SAMPLE1_ADDR)  # this will also be used later
            recent = (w1 >> 29) & 1
            # If we get a updated time, we're good: exit the loop
            if recent:
                break
            # If we don't have a valid timestamp, and have been waiting for
            # too long or are not allowed to trig to re-check the time, then
            # raise an error unless instructed not to.
            #
            # Wait a little bit more than one second in case the IRIG-B signal
            # just became valid (e.g. we just set the source)
            if not trig or time.time() - t0 > 2.5:
                if noerror:
                    return None
                else:
                    raise RuntimeError(
                        '%.32r: Could not get a recently updated IRIG-B time. '
                        'Check your cabling and the IRIG-B source selection.' % self)

        ts = self._IrigTimestamp()
        ts.system_time_before = time.time()

        ts.refclk_counter = await self.fpga_core_reg_read_async(self._IRIGB_REFCLK_SAMPLE)

        # Read the (other) IRIG-B capture registers. We assume nobody is callung
        # concurrent instances at the same same time, so we do this
        # asynchronously because each read is slow.
        w0 = await self.fpga_core_reg_read_async(self._IRIGB_SAMPLE0_ADDR)
        # w1 was read when checkiing for a valid IRIG-B capture
        w2 = await self.fpga_core_reg_read_async(self._IRIGB_SAMPLE2_ADDR)

        # check if the time is valid
        # t0 = self.fpga_core_reg_read_async(self._IRIGB_TARGET0_ADDR)
        # t1 = self.fpga_core_reg_read_async(self._IRIGB_TARGET1_ADDR)
        # t2 = self.fpga_core_reg_read_async(self._IRIGB_TARGET2_ADDR)
        # e0 = self.fpga_core_reg_read_async(self._IRIGB_EVENT_CTR_ADDR)

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
        # "straight binary seconds" since 00:00 on the current day (0-86399,
        # not BCD). Not necessarily supported by the GPS.
        ts.sbs = (w0 >> 8) & ((1 << 18) - 1)
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
        ts.datetime = dt = (
            datetime(ts.y + 2000, 1, 1) +
            timedelta(days=ts.d - 1, hours=ts.h, minutes=ts.m,
                      seconds=ts.s + 1, microseconds=ts.ss // 100))
        # compute a timestamp, one second in the future
        # unix timestamp = seconds since 1 Jan 1970 UTC
        timestamp = timegm((ts.y + 2000, 1, ts.d, ts.h, ts.m, ts.s + 1))
        ts.nano = int(timestamp * 1e9) + ts.ss * 10  # nanoseconds since 1 Jan 1970 UTC
        # Unix timestamp, as a float with as much resolution as the float can
        # provide (not necessarily to the nanosecond)
        ts.time = ts.nano / 1e9
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
        return ts

    async def set_irigb_trigger_time_async(self, datetime_=None, delay=None):
        """ Sets the time at which the IRIG-B module will generate a trigger
        that can be used to synchronize boards.

        Parameters:

            datetime_ (datetime): the base target time in the Python as a :class:`datetime` object.

            delay (float): is a time offset in seconds that is added to `datetime_` so set the
                target time. It defaults to zero.

        Returns:

            _IrigTimestamp object: contains the programmed trigger time expressed as a
                `datetime` object (.datetime) and in nanoseconds since epoch (.nano).

        If the trigger is used for synchronizing boards, the delay should be a
        multiple of 100 ns in order to ensure alignment with the 10 MHz
        reference clock and ensure deterministic start of the syncronization
        state machine.

        If `datetime_` and 'delay' are None, the trigger time is set 3 seconds
        after the current time (as returned by the board).
        """
        if datetime_ is None:
            ts = await self._get_irigb_time_async(trig=True)  # do this synchronously to we get an accurate time
            dt = ts.astype('datetime')
            self.logger.debug(f'{self!r}: Current IRIGB time is {ts.isoformat()}')
            if delay is None:
                delay = 3
        else:
            dt = datetime_
            if delay is None:
                delay = 0

        nano_delay = int(delay * 1e9) % 1000  # Get submicrosecond delay in nanosecond units
        delay = int(delay * 1e6) / 1e6  # Round delay to the microsecond
        # add delay in integer microseconds (datetime does not support more
        # than the microsecond accuracy)
        dt += timedelta(0, delay)
        self.logger.debug(
            f'{self!r}: Setting IRIGB target time to '
            f'{dt.isoformat()} + {nano_delay:3d} ns')
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

        self.logger.debug(
            f'{self!r}: Setting IRIGB target time with y={y}, d={d}, h={h}, '
            f'm={m}, s={s}, ss={ss}')


        # disable time comparison to prevent false triggers during setup
        t2 = await self.fpga_core_reg_read_async(self._IRIGB_TARGET2_ADDR)
        t2 |= (1 << 31) | (1 << 30)  # Enable compare enable bit, but force output to be 1 (i.e. we ar ebefore trig time) to prevent false trigger. We change noting else to prevent a false trigegr.
        await self.fpga_core_reg_write_async(self._IRIGB_TARGET2_ADDR, t2)

        t0 = await self.fpga_core_reg_read_async(self._IRIGB_TARGET0_ADDR) & 0xFFFFFF00
        t0 |= (y << 0)
        t1 = (d << 20) | (h << 14) | (m << 7) | (s << 0)
        t2 = (1 << 31) | (1 << 30) | (ss << 0) #  comparator is enabled, but still forcing output to 1

        await self.fpga_core_reg_write_async(self._IRIGB_TARGET0_ADDR, t0)
        await self.fpga_core_reg_write_async(self._IRIGB_TARGET1_ADDR, t1)
        await self.fpga_core_reg_write_async(self._IRIGB_TARGET2_ADDR, t2)

        # now re-enable time comparator
        t2 ^= (1 << 30) # Toggle force bit back to zero.
        await self.fpga_core_reg_write_async(self._IRIGB_TARGET2_ADDR, t2)


        # return the time at which the sync event is scheduled for
        ts = self._IrigTimestamp()
        ts.datetime = dt
        ts.nano = int(timegm((2000 + y, 1, d, h, m, s + 1)) * 1e9) + ss*10
        return ts

    async def is_irigb_before_trigger_time_async(self):
        """ Is true if the current IRIGB is before the target trigger time that was previously set-up.
        """
        t1 = await self.fpga_core_reg_read_async(self._IRIGB_TARGET1_ADDR)
        return bool(t1 & (1 << 31))

    async def capture_frame_time_async(self, trig=True, format='nano', timeout=5):
        """ Captures the IRIG-B of the first sample of the next frame coming
        out of the ADC data acquisition module.

        The returned time is (frame_number, time), where frame_number is the
        number of the frame that was measured, and time is the IRIG-B time in
        a format specified by 'format' (see get_irigb_time_async() for available
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
            w2 = await self.fpga_core_reg_read_async(self._IRIGB_SAMPLE2_ADDR)
            await self.fpga_core_reg_write_async(self._IRIGB_SAMPLE2_ADDR, w2 & ~(1 << 28))
            await self.fpga_core_reg_write_async(self._IRIGB_SAMPLE2_ADDR, w2 | (1 << 28))
            t0 = time.time()
            while not (await self.fpga_core_reg_read_async(self._IRIGB_TARGET1_ADDR)) & (1 << 30):
                if time.time() - t0 > timeout:
                    raise RuntimeError(
                        'Timeout while waiting for a Frame. Is data flowing '
                        'out of the ADC data acquisition module?')
        event_number = await self.fpga_core_reg_read_async(self._IRIGB_EVENT_CTR_ADDR)
        event_number += (await self.fpga_core_reg_read_async(self._IRIGB_EVENT_CTR_ADDR2)) << 32

        ts = await self._get_irigb_time_async(trig=0)  # The event trigger will automatically trig IRIGB
        captured_time = ts.astype(format)
        return (event_number, captured_time)

    async def get_frame_number_async(self):
        """
        Return the number of the next frame passing through the system.
        """
        w2 = await self.fpga_core_reg_read_async(self._IRIGB_SAMPLE2_ADDR)
        await self.fpga_core_reg_write_async(self._IRIGB_SAMPLE2_ADDR, w2 & ~(1 << 28))
        await self.fpga_core_reg_write_async(self._IRIGB_SAMPLE2_ADDR, w2 | (1 << 28))
        t0 = time.time()
        while not (await self.fpga_core_reg_read_async(self._IRIGB_TARGET1_ADDR)) & (1 << 30):
            if time.time() - t0 > 1:
                raise RuntimeError(
                    'Timeout while waiting for a Frame. Is data flowing out '
                    'of the ADC data acquisition module?')
        event_number = await self.fpga_core_reg_read_async(self._IRIGB_EVENT_CTR_ADDR)
        return event_number

    async def capture_refclk_time_async(self, trig=True, format='nano'):
        """ Measures the time at which the next 10MHz reference clock rising
        edge occurs.

        The method returns the reference clock edge number (from a 32-bit
        counter that wraps around) and the time in the specified format (see
        get_irigb_time_async() for available formats).

        This method can be used to measure the drift of the 10 MHz reference clock relative to
        the IRIG-B time.
        """
        ts = await self._get_irigb_time_async(trig=trig)
        return (ts.refclk_counter, ts.astype(format))  # Return

    async def get_irigb_time_async(self, trig=True, format='datetime', noerror=False):
        """ Return the current time as decoded on the IRIG-B input. The time
        is returned in a format specified by 'format':

        - 'raw': A object containing all the data fields read directly from
          the IRIG-B decoder and preprocessed datetime and nano values
        - 'datetime': Python 'datetime' object (with a microsecond resolution)
        - 'datetime+': A (dt,nano) tuple where dt is a datetime object, and
          nano is the number of nanoseconds within the second.
        - 'nano': An integer representing the number of nanoseconds since Jan
          1st 2000.
        """
        ts = await self._get_irigb_time_async(trig=trig, noerror=noerror)
        return ts.astype(format)



    # provide sync-wrapped functions for convenience

    get_irigb_time_sync = async_to_sync(get_irigb_time_async)
    capture_refclk_time_sync = async_to_sync(capture_refclk_time_async)
    get_frame_number_async = async_to_sync(get_frame_number_async)
    is_irigb_before_trigger_time_sync = async_to_sync(is_irigb_before_trigger_time_async)
    set_irigb_trigger_time_sync= async_to_sync(set_irigb_trigger_time_async)
    set_irigb_source_sync = async_to_sync(set_irigb_source_async)
    get_irigb_source_sync = async_to_sync(get_irigb_source_async)




    def is_fmc_present_for_channel(self, channel_number):
        return self.ANT_FMC_IS_PRESENT[channel_number]

    def is_fmc_present(self, slot_number):
        return self._FMC_present[slot_number]

    def get_channels(self):
        """ Return a list of available channel numbers """
        return list(self.chan.keys())

    def get_channelizers(self, channels=None):
        """ Return a list of channelizer objects for the specified or all channel numbers

        Parameters:

            channels (list of int): channels for which we want channelizers objects. `None` returns all channelizers.

        Returns:

            list of channelizer objects.

        """
        if channels is None:
            return list(self.chan.values())
        else:
            return [self.chan[ch] for ch in channels]



    def get_string_id(self):
        """
        Return a string that uniquely represents the board. It is composed of the model and serial
        number or the board, or, if those are unknown, the hostname of the board.

        Parameters:
            None

        Returns:
            string

        """
        if self.mb.serial and self.mb.part_number:
            return '%s_SN%s' % (self.mb.part_number, self.mb.serial)
        else:
            return self.hostname

    def __repr__(self):
        return f"chFPGA{self.get_id()}"


    def get_channel_ids(self, channels=None):
        """ return a list of channel IDs ((crate,slot,chan) tuples) for the specified or all channnels.

        Parameters:

            channels (list of int): channels for which the channel id is  desired. If None, the
                channelID for all channels is returned.

        Returns:
            list of (crate, slot, channel) tuples
        """

        if channels is None:
            channels = self.get_channels()
        return [self.get_id(ch) for ch in channels]

    def get_stream_ids(self, channels=None):
        """ Return the stream_ids (integers) of specified or all channels.

        Parameters:

            channels (list of int): channels for which the stream id is  desired. If None, stream ID
                for all channels is returned.

        Returns:
            list of int containing the stream IDs
        """

        if channels is None:
            channels = self.get_channels()
        return [self.chan[ch].PROBER.get_stream_id() for ch in channels]

    def get_stream_id(self, channel):
        """ Return the stream_id of a specified channel.

        Returns:
            int: the stream IDs
        """

        return self.chan[channel].PROBER.get_stream_id()

    def get_stream_id_map(self):
        """ Return the stream_ids of every channel of the board, indexed by channel_id.

        Returns:

            dict if the format {channel_id:stream_id}, where channel_id is a (crate, slot, channel)
            tuple.
        """

        return {self.get_id(ch): chan.PROBER.get_stream_id() for ch, chan in self.chan.items()}



    async def get_config_async(self, basic=False):
        """
        Return configuration for this FPGA.


        Parameters:

            basic (bool): If False, only the quickly accessible information is gathered

        Returns:

            chFPGA_config object (essentially just a namespace) containing the config parameters.

        TODO:
            -
        """
        config = chFPGA_config()  # Create empty config container
        # Add configuration parameters

        config.config_protocol_version = (1, 0)
        config.config_capture_time = time.time()
        config.system_firmware_version = self.get_version()
        config.system_platform_id = self.PLATFORM_ID
        config.system_interface_ip_address = self.interface_ip_addr

        config.system_fpga_ip_address = self.fpga_ip_addr
        config.system_fpga_port_number = self.fpga_control_port_number
        config.system_local_command_port_number = self.local_control_port_number
        config.system_local_data_port_number = await self.get_local_data_port_number_async()
        config.system_local_corr_port_number = self.local_control_port_number

        config.number_of_channelizers = self.NUMBER_OF_CHANNELIZERS
        config.system_list_of_antennas_with_channelizers = self.LIST_OF_ANTENNAS_WITH_FFT

        config.number_of_correlators_max = self.NUMBER_OF_CORRELATORS_MAX
        config.number_of_correlators = self.NUMBER_OF_CORRELATORS
        config.number_of_antennas_to_correlate = self.NUMBER_OF_INPUTS_TO_CORRELATE
        config.system_list_of_implemented_correlators = self.LIST_OF_IMPLEMENTED_CORRELATORS

        config.system_frame_length = self.FRAME_LENGTH
        config.system_sampling_frequency = self._sampling_frequency
        config.system_reference_frequency = self._reference_frequency
        config.system_frame_period = self.FRAME_PERIOD
        config.motherboard_serial = self.GPIO.FPGA_SERIAL_NUMBER

        if not basic:
            mezz1 = self.mezzanine.get(1, None)
            config.adc_board_is_present = bool(mezz1)

            if mezz1:
                config.adc_board_temperature = mezz1.AmbTemp.temperature
                config.adc_board_adc_chip_temperature = [adc.get_temperature() for adc in mezz1.ADC]
            config.adc_serial = [self.mezzanine[mezz_number].serial if mezz_number in self.mezzanine else None
                                 for mezz_number in (1, 2)]  # mezz1._board_info['Serial #']

            config.channelizer_data_source = self.get_data_source()
            config.channelizer_fft_bypass = self.get_FFT_bypass()
            config.channelizer_fft_shift_schedule = self.get_FFT_shift()
            config.channelizer_scaler_gain = self.get_gains()
            config.channelizer_adc_data_acquisition_delay_tables = self.chan.get_adc_delays()
            config.FPGA_board_frequency = self.FreqCtr.read_frequency('CLK200', gate_time=0.05)
            config.CTRL_clock_frequency = self.FreqCtr.read_frequency('CTRL_CLK', gate_time=0.05)
            config.ant_clock = self.FreqCtr.read_frequency('ANT_CLK', gate_time=0.05)

            if self.CORR:
                config.correlator_clock = self.FreqCtr.read_frequency('CORR_CLK', gate_time=0.05)
                # config.correlator_capture_period_in_frames = [corr.ACC.CAPTURE_PERIOD for corr in self.CORR]
                # config.correlator_integration_period_in_frames = [corr.ACC.INTEGRATION_PERIOD for corr in self.CORR]

            config.fmc_ref_clock = self.FreqCtr.read_frequency('FMCA_REFCLK', gate_time=0.05)
            config.mgt_ref_clock = self.FreqCtr.read_frequency('GPU_REFCLK', gate_time=0.05)
            config.mgt_word_clock = self.FreqCtr.read_frequency('GPU_TXCLK', gate_time=0.05)
            # todo: fix ADC range below (?)
            config.adc_clocks = [self.FreqCtr.read_frequency(('ADC_CLK' + str(i)), gate_time=0.05) for i in range(8)]
            # config.motherboard_serial = self.GPIO.FPGA_SERIAL_NUMBER
            # Add FFT shift, scaler gain, corr integration/capture period etc.
            # config.freq_flags = self.freq_flags  # JFC: what is that?
        return config

    def update_config(self):
        pass

    # def read_bit(self, addr, bit):
    #     return (self.read(addr) & (1 << bit)) != 0

    # def write_bit(self, addr, bit, data):
    #     old_data = self.read(addr)
    #     mask = 1 << bit
    #     self.write(addr, (old_data & (~mask)) | (mask if data else 0))

    # def write_mask(self, addr, mask, data):
    #     old_data = self.read(addr)
    #     self.write(addr, (old_data & (~mask)) | (mask & data))

    # def pulse_bit(self, addr, bit):
    #     old_data = self.read(addr)
    #     mask = 1 << bit
    #     self.write(addr, (old_data | mask))
    #     self.write(addr, (old_data & (~mask)))

    def sync(self, local=1, verbose=0):
        if verbose:
            self.logger.debug("%r: Syncing board" % self)

        if self.PLATFORM_ID in (self._PLATFORM_ID_ZCU111, self._PLATFORM_ID_CRS):
            self.logger.warning(f"{self!r}: Sync is not yet implemented on the {self.mb.part_number}")
        else:
            self.set_adc_mask(0)  # null the ADC data before it gets to the channelizers to reduce power consumption
            if local:
                self.REFCLK.local_sync()
            else:
                self.REFCLK.remote_sync()
            self.set_adc_mask(0xff)  # restore full ADC data

    def pulse_ant_reset(self):
        """ Resets the stats of all channelizers and clear the processing pipeline.
        Memory-mapped registers are not affected.
        """
        self.GPIO.pulse_ant_reset()  # resets all

    def reset(self):
        """ Resets the channelizers, corner-turn and correlator engines.
        Memory-mapped registers are not affected.
        """
        self.set_ant_reset(1)
        self.set_corr_reset(1)
        self.set_corr_reset(0)
        self.set_ant_reset(0)

    def set_default_channels(self, channels):
        """
        Sets the default channels to use in other functions when not specifically specified.
        """
        if isinstance(channels, int):
            channels = [channels]
        self.default_channels = channels

    def get_default_channels(self):
        """
        Returns the default channels that are used in other functions when not specifically specified.
        """
        return self.default_channels

    def set_channelizer(
            self,
            adc_mode=None,
            adc_sampling_mode=None,
            adc_bandwidth=None,
            adcdaq_mode=None,
            data_source=None,
            function=None,
            freq_test_bins=None,
            fft_bypass=None,
            fft_shift=None,
            scaler_bypass=None,
            gain=None,
            postscaler=None,
            scaler_eight_bit=None,
            scaler_rounding_mode=None,
            prober_user_flags=None,
            offset_binary_encoding=None,
            local_sync=True,
            channels=None,
            **function_kwargs):
        """
        Single command used to set all channelizer settings.

        The data processing chain is:

                  ADC --> ADCDAQ --> FUNCGEN --> --> FFT --> SCALER
        """
        # Set the ADC chip operational mode (data, ramp, pulse)
        if adc_mode is not None or adc_sampling_mode is not None or adc_bandwidth is not None:
            self.set_adc_mode(
                mode=adc_mode,
                sampling_mode=adc_sampling_mode,
                bandwidth=adc_bandwidth,
                sync=False)

        # Set the FPGA's ADC data acquisition module operational mode
        if adcdaq_mode is not None:
            self.set_adcdaq_mode(mode=adcdaq_mode, channels=channels)

        # Set the date source and the function generator that feed the FFT
        if data_source is not None:
            self.set_data_source(data_source, channels=channels, **function_kwargs)  # does a channelizer reset

        if function is not None:
            logger = logging.getLogger(self.__class__.__name__)
            logger.warning(
                "Using 'function' parameter for setting FUNCGEN function is obsolete. Please use 'data_source' instead."
            )
            # Handle special case where we set the output of the channelizer
            # with complex numbers that will give unique correlation products
            # (visibilities). There are 108 such numbers in a (4+4) bits
            # complex number. `freq_test_bins` is a list of up to 108  bin
            # numbers that will be assigned these special complex numbers. Other bins are zero. If
            # `freq_test_bins` is empty, all bins are set to (1+1j),
            #
            # (This should be moved in FUNCGEN).
            #
            # Configure funcgen so the visibility data has a unique real
            # number for 108 freq bins. The other freq bins are zeros
            if function == 'freq_test':
                # Get the number of bins to set with the special complex numbers
                # We have a limited pool of 108 special complex numbers, so we saturate the number.
                N = min(len(freq_test_bins), 108)
                if N == 0:
                    # Send same number (1+0j) for all frequencies
                    v = (9 * np.ones(2048, dtype=np.uint8)) << 4  # First set 1+1j (9 means 1 with offset encoding)
                    v[1::2] = (8 * np.ones(1024, dtype=np.uint8)) << 4  # Clear imag part (8 means 0 is offset encoding)
                else:
                    # Send pattern that will generate unique products
                    # First we define the unique complex number, offset-encoded, and shifted to the high nibble.
                    freq_pattern_real = np.array([ 1,  1,  1,  1,  1,  1,  1,  1,  1,  1,  1,  1,  1,  1,  1,  2,  2,
                                                   2,  2,  2,  2,  2,  2,  2,  2,  2,  2,  2,  2,  3,  3,  3,  3,  3,
                                                   3,  3,  3,  3,  3,  3,  3,  3,  4,  4,  4,  4,  4,  4,  4,  4,  4,
                                                   4,  4,  5,  5,  5,  5,  5,  5,  5,  5,  5,  6,  6,  6,  6,  6,  6,
                                                   6,  6,  7,  7,  7,  7,  7,  7,  7,  8,  8,  8,  8,  8,  8,  9,  9,
                                                   9,  9,  9,  9, 10, 10, 10, 10, 11, 11, 11, 11, 11, 12, 12, 12, 12,
                                                  13, 13, 13, 14, 14, 15], dtype=np.uint8) << 4
                    freq_pattern_imag = np.array([ 1,  2,  3,  4,  5,  6,  7,  8,  9, 10, 11, 12, 13, 14, 15,  2,  3,
                                                   4,  5,  6,  7,  8,  9, 10, 11, 12, 13, 14, 15,  3,  4,  5,  6,  7,
                                                   8,  9, 10, 11, 12, 13, 14, 15,  4,  5,  6,  8,  9, 10, 11, 12, 13,
                                                  14, 15,  6,  7,  8,  9, 11, 12, 13, 14, 15,  6,  8,  9, 10, 11, 12,
                                                  14, 15,  7,  8, 10, 12, 13, 14, 15,  8, 10, 12, 13, 14, 15,  9, 10,
                                                  11, 12, 14, 15, 12, 13, 14, 15, 11, 12, 13, 14, 15, 12, 13, 14, 15,
                                                  13, 14, 15, 14, 15, 15], dtype=np.uint8) << 4
                    v = np.zeros(2048, dtype=np.uint8)
                    v_real = np.zeros(1024, dtype=np.uint8)
                    v_imag = np.zeros(1024, dtype=np.uint8)
                    v_real[freq_test_bins] = freq_pattern_real[:N]
                    v_imag[freq_test_bins] = freq_pattern_imag[:N]
                    v[::2] = v_real
                    v[1::2] = v_imag
                # Set the vectors. If FFT and scaler are bypassed, then the
                # visibility data has a unique products for the 108 freq
                # bins in freq_test_bins. The rest are zeros.
                self.set_funcgen_function('arb', channels=channels, data=v)
            else:
                self.set_funcgen_function(function=function, channels=channels, **function_kwargs)

        # Set FFT bypass and shift schedule
        if fft_bypass is not None:
            self.set_fft_bypass(bypass_mode=fft_bypass, channels=channels)

        if fft_shift is not None:
            self.set_fft_shift(fft_shift, channels=channels)

        # Set Scaler parameters
        if scaler_bypass is not None:
            self.set_scaler_bypass(bypass_mode=scaler_bypass, channels=channels)

        if scaler_rounding_mode is not None:
            self.set_scaler_rounding_mode(
                scaler_rounding_mode=scaler_rounding_mode,
                channels=channels,
            )

        if scaler_eight_bit is not None:
            self.set_scaler_eight_bit(
                scaler_eight_bit=scaler_eight_bit,
                prober_user_flags=prober_user_flags,
                channels=channels,
            )

        if gain is not None:
            self.set_gains(gain=gain, postscaler=postscaler, channels=channels)

        if offset_binary_encoding is not None:
            self.set_offset_binary_encoding(offset=offset_binary_encoding, channels=channels, sync=False)

        if local_sync:
            self.sync()

    def set_channelizer_outputs(self, data):
        """
        Set the data outputted by the channelizers. FFT and SCALER and bypassed.
        data(chan, bin) = complex value (4+4) bits
        """

        d = np.zeros((16, 2048), np.int8)
        d[:, 0::2] = data.real
        d[:, 1::2] = data.imag
        d <<= 4

        self.set_channelizer(
            data_source='funcgen',
            function='AB',
            a=0,
            b=0,
            fft_bypass=1,
            scaler_bypass=1,
            offset_binary_encoding=0)

        for ch in range(16):
            self.set_funcgen_function('arb', channels=[ch], data=d[ch])
        return d

    # set_data_path = set_channelizer # for legacy compatibility

    def get_funcgen_buffer(self):
        """ Return the current function generator buffer.

        """
        return {chan.get_id():chan.FUNCGEN.get_buffer() for chan in self.mb.chan.values()}

    def set_data_source(self, source=None,  channels=None, **kwargs):
        """
        Selects the data that is being fed into the channelizer. If a wafeform
        name (and corresponding arguments) is provided, the function generator
        is automatically selected and the waveform is set-up.

        This function resets the channelizers, even if only the function is
        changed. Use `set_funcgen_function()` if the function generator is
        already active and you want to change only the waveform
        """
        data_sources = self.chan[0].FUNCGEN.DATA_SOURCE_NAMES.keys()
        function_names = self.chan[0].FUNCGEN.FUNCTION_NAMES.keys()

        source = source.lower()

        if channels is None:
            channels = self.default_channels

        if source in data_sources:
            self.set_ant_reset(1)  # Reset is needed to resynchronize the system with the new data
            for chan in self.get_channelizers(channels):
                chan.FUNCGEN.set_data_source(source)
            self.set_ant_reset(0)  # Reset is needed to resynchronize the system with the new data
        elif source in function_names:
            self.set_ant_reset(1)  # Reset is needed to resynchronize the system with the new data
            for chan in self.get_channelizers(channels):
                chan.FUNCGEN.set_data_source('funcgen')
                chan.FUNCGEN.set_function(source, **kwargs)
            self.set_ant_reset(0)  # Reset is needed to resyncronize the system with the new data
        else:
            raise ValueError("Invalid data source or function name '%s'. Valid data sources are %s:" % (
                    source,
                    ', '.join(data_sources + function_names)))

    def get_data_source(self):
        """
            Returns a list of data source for all channels.
        """
        return [chan.FUNCGEN.get_data_source() for chan in self.chan.values()]

    def set_funcgen_function(self, function=None, channels=None, **kwargs):
        """
        Sets the waveform generated by the function generator on specified
        channels (or default channels if the channels are not specified).

        This may cause one frame to partially contain the new waveform.
        """
        if (function is None) or (function.lower() not in self.chan[0].FUNCGEN.FUNCTION_NAMES):
            raise ValueError("Invalid function generator function '%s'. Valid functions are %s:" % (
                function,
                ', '.join(self.chan[0].FUNCGEN.FUNCTION_NAMES.keys())))

        if channels is None:
            channels = self.default_channels

        for ch in channels:
            chan = self.chan[ch]
            chan.FUNCGEN.set_function(function.lower(), **kwargs)

    def get_adc_board(self, channel):
        """
        Returns the ADC board object that is associated with the specified channel.
        If channel is a list, returns a list of unique board objects associated with the specified channels.
        """

        if isinstance(channel, int):
            # channel = [channel]
            mezz_number = (channel // 8) + 1
            return self.mezzanine.get(mezz_number, None)
        else:
            board_list = set()
            for ch in channel:
                mezz_number = (ch // 8) + 1
                if mezz_number in self.mezzanine.keys():
                    board_list.add(self.mezzanine[mezz_number])
                else:
                    self.logger.warning('%r: ADC Mezzanine board for channel %i is not present. '
                                         'Ignoring this board' % (self, ch))
            return list(board_list)

    ADC_MODE_NAMES = {
        # name, mode number, period (in 4-bytes words)
        'data': (0, 64),  # ADC sends analog data
        'ramp': (1, 64),  # ADC sends ramp from 0 to 255
        'pulse': (2, 11),  # ADC sends ten 0x00 followed by one 0xff
        }

    # ADC_MODE_NAMES_REVERSED = util.reverse_dict(ADC_MODE_NAMES)

    def set_adc_mode(self, mode='data', sampling_mode=0, bandwidth=2, channels=None, sync=True):
        """
        Sets the operating mode of the all the ADCs, sets the proper CAPTURE
        period, and sends a SYNC to actuate the change.

        By default, all ADCs on any board handling the specified channels are set to the desired mode.
        If no channels are specified, the default channel list is used.
        Again: both ADCs on every target board are set, even if we specify channels handled by only one adc chip.

        Parameters:
            mode (str): ADC mode to use:

                - 'data': Normal mode (ADC output contains analog samples)

                - 'ramp': Ramp mode (ADC output contains repeating 0-255
                  pattern. Note that ADCDAQ inverts bit 7 during acquisition
                  to convert offset binary to 2's complement binary)

                - 'pulse': Strobe mode (ADC output contains one 0xFF followed
                  by ten 0x00. It repeats with a pariod of 11. Same comment as
                  above)
        """
        if sampling_mode is None:
            sampling_mode = 0
        if bandwidth is None:
            bandwidth = 2

        if isinstance(mode, list):
            if channels:
                raise RuntimeError('channels cannot be specified when multiple modes are provided')
            for fmc_number, m in enumerate(mode):
                self.mezzanine[fmc_number + 1].ADC.set_test_mode(test_mode=self.ADC_MODE_NAMES[m.lower()][0])
            return

        if mode.lower() not in self.ADC_MODE_NAMES:
            raise ValueError("Invalid ADC mode '%s'. Valid modes are %s" % (
                mode,
                ', '.join(self.ADC_MODE_NAMES.keys())))
        (mode_value, capture_period) = self.ADC_MODE_NAMES[mode.lower()]

        if channels is None:
            channels = self.default_channels

        # Set the mode on all affected ADC boards
        adc_boards = self.get_adc_board(channels)
        for adc_board in adc_boards:
            adc_board.ADC.set_test_mode(test_mode=mode_value, adc_mode=sampling_mode, bandwidth=bandwidth)

        # Set the capture period for all specified channels
        for ch in channels:
            chan = self.chan[ch]
            # Set the period so we are ready to capture data correctly after the SYNC resets the CAPTURE logic
            chan.ADCDAQ.CAPTURE2_PERIOD = capture_period

        # self.current_ADC_mode = mode_value
        if sync:
            self.sync()  # make sure the ADC mode is set and that capture  restarts properly with the right period

    def get_adc_mode(self, channels=None):
        """
        Gets the current operating mode of all the ADCs as a string.

        If all ADCs operate in the same mode, a single mode string is
        returned. Otherwise a list of mode strings is returned.
        """

        if channels is None:
            channels = self.default_channels

        mode_names = []

        # get the ADC mode number for every ADC board
        adc_boards = self.get_adc_board(channels)
        for mezz in adc_boards:
            mode_value = mezz.ADC.get_test_mode()
            mode_name = [name for (name, value) in self.ADC_MODE_NAMES.items() if value[0] == mode_value][0]
            mode_names.append(mode_name)

        if len(set(mode_names)) == 1:  # Eliminate all duplicates. We should be left with only one mode number.
            return mode_names[0]
        else:
            return mode_names
        # if len(mode_value) != 1:
        #     raise RuntimeError('The ADC chips on the ADC boards are not ALL in the same mode')

    def set_adcdaq_mode(self, mode='data', channels=None):
        """
        Sets the source of the data acquisition module.

        test_mode:
            'data': the ADCDAAQ module sends data from the ADC
            'ramp': The ADCDAQ sens an internally generated ramp
        """

        assert self.HAS_ADCDAQ, 'The platform does not support the MGADC08 mezzanine and associated ADCDAQ firmware'
        if channels is None:
            channels = self.default_channels

        for ch in channels:
            chan = self.chan[ch]
            chan.ADCDAQ.set_ADCDAQ_mode(mode)

    set_ADCDAQ_mode = set_adcdaq_mode

    def set_ant_reset(self, state):
        """ Sets the state of the reset line of ALL channelizer.

        Parameters:

            state (bool): A true value will put the channelizers in reset, and data will stop flowing from them.

        Note:
            A channelizer reset is automatically done during a SYNC.

        """

        self.GPIO.ANT_RESET = state

    def get_ant_reset(self):
        """ Get the status of the channelizer reset line.

        Return:
            (bool): state of the reset line.
        """
        return self.GPIO.ANT_RESET

    def set_corr_reset(self, state):
        self.GPIO.CORR_RESET = state

    def set_trig(self, state):
        self.GPIO.GLOBAL_TRIG = state

    def stop_data_capture(self):
        """
        Stops the transmission of data.
        """
        self.GPIO.GLOBAL_TRIG = 0  # disable data transmission if continuous mode is currentlly selected
        for chan in self.chan.values():
            chan.PROBER.RESET = 1

    def get_data_receiver(self, verbose=1, threaded=False):
        if self.recv:
            return self.recv
        if threaded:
            # Old threaded data receiver
            if self.CAPTURE_TYPE != "PROBER":
                raise RuntimeError('The old threaded receiver is supported only by PROBER')
            chFPGA_config = run_async(self.get_config_async(basic=True))  # get only the info needed to start the receiver
            self.recv = chFPGA_receiver(chFPGA_config, verbose=verbose)
            self.logger.debug('Started data receiver threads on %s:%i' % (self.recv.host_ip, self.recv.port_number))
            run_async(self.set_local_data_port_number_async(self.recv.port_number))
        elif self.CAPTURE_TYPE == "PROBER":
            sock = self.get_data_socket()
            self.recv = prober.RawFrameReceiver(sock)
        elif self.CAPTURE_TYPE == "UCAP":
            sock = self.get_data_socket()
            self.recv = self.UCAP.get_data_receiver(sock)
        else:
            raise RuntimeError("Unknown capture engine type")

        return self.recv

    def get_data_socket(self, port_number=0):
        """
        Return a UDP socket that receives the raw/correlator data.

        If the socket does not already exists, the FPGA will be configured to send the data to the returned socket.

        Parameters:

            port_number (int): If non-zero, get a socket bound to the specified port. If not specified, the existing data port number will be used. If there is no existing port number,  a random port will be chosen.


        Returns:

            socket.socket(): a UDP socket.
        """
        # Make sure there is a list of opened sockets


        if not self._data_socket:
            port_number = port_number or run_async(self.get_local_data_port_number_async())
            opened_sockets = __main__.__dict__.setdefault('__opened_sockets__', {})

            # If we want to use a specific local port that was previously reserved, use its socket.
            if port_number and port_number in opened_sockets:
                self._data_socket = opened_sockets[port_number]
            else:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.bind((self.interface_ip_addr, port_number))
                # store the socket in the main module so it will live persistently until the Python session is closed.
                (actual_ip_addr, actual_port_number) = sock.getsockname()
                opened_sockets[actual_port_number] = sock
                self._data_socket = sock
                run_async(self.set_local_data_port_number_async(actual_port_number))

        return self._data_socket

    def set_data_capture_stream_ids(self, stream_ids):
        """ Set the STREAM ID of the raw data capture packets for each of the channels

        Parameters:

            stream_ids (list, dict or int): stream_ids to apply.

                if `stream_ids` is a list, the stream ids in the list will be
                applied directly to the channels.

                If `stream_ids` is a dict in the format {channel:stream_id},
                the specified channels will be set with the corresponding
                stream_ids.

                If `stream_ids` is an integer, it will be treated as a virtual
                slot number 'slot', and all channels on the board will be set
                to ``slot * 16 + channel_number``. There can be up to 4096
                virtual slots.



        Note: This does not set the STREAM ID of the packets sent through the
        corner turn or the correlator engines. This is only for the raw data
        capture done over the control interface.

        Examples:
            # Set stream ids from channels 0 to 100, channel 1 to 101 etc.
            ib.set_data_capture_stream_ids([100, 101, 102, ... 115])

            # Set stream ids for channels 0 and 5 to 400 and 401 respectively
            ib.set_data_capture_stream_ids({0:400, 5:401})

            # Set stream_ids for channel 0...15 to the values 1600, 1601, 1602 etc.
            ib.set_data_capture_stream_ids(100)
        """

        if isinstance(stream_ids, list):
            stream_ids = dict(enumerate(stream_ids))
        elif isinstance(stream_ids, int):
            stream_ids = {ch: (stream_ids * 16 + ch) for ch in self.chan.keys()}
        elif not isinstance(stream_ids, dict):
            raise TypeError('parameter must be a list, a dict or an integer')

        for ch, stream_id in stream_ids.items():
            self.chan[ch].PROBER.STREAM_ID = stream_id

    def start_data_capture(
            self,
            period=None,
            frames_per_burst=1,
            number_of_bursts=0,
            channels=None,
            source='scaler',
            sync=1,
            verbose=1,
            burst_period_in_seconds=None,
            burst_period_in_frames=None,
            offset=0,
            send_delay=0):
        """
        Starts the transmission of ADC (post-function generator / pre-FFT) or SCALER (post
        scaler) data frames to the Ethernet port at the specified rate.

        Parameters:

            period (float): Time (in seconds) between captured bursts

            burst_period_in_seconds (float): Same as `period` or as a number of frames

            burst_period_in_frames (int): Number of frames between captured bursts.

            number_of_bursts (int): Number of bursts to send, after which the FPGA stops sending
                data. If `number_of_bursts` =0, the transmission continues indefinitely, until
                stopped by `stop_data_capture()`.

            frames_per_burst (int): Number of frames to send in a single burst. Default is 1.
                Limited by buffer space in the FPGA.

            source (str): selects the data source. 'adc':  the data is taken after the function
                generator (sorry, non intuitive). `scaler`: the data is taken after the scaler.
                Default is 'scaler'.

            channels (list): list of channels for which the data capture will be enabled. others are
            left untouched.

            sync (bool):

            verbose (int):

            offset (int): Number that translates to how many frames are skipped before the capture
                counters starts after a sync(). This is used to stagger capture frame transmission
                between boards in a crate to prevent UDP packets from being dropped by a switch.

            send_delay (int): Number that sets the amount of time to wait
                before sending a group of packets that are captured in the local
                buffer. This is a 16-bit number, where each unit corresponds to
                524.288 us.

        Data is sent as N bursts ('number_of_bursts') of M frames ('frames_per_burst') . If
        'number_of_bursts' is zero or not specified, burst transmission is continuous.

        Burst repetition rate is set either as a period specified in seconds ('period' or
        'burst_period_in_seconds') or as a number of frames ('burst_period_in-frames').
        """
        if channels is None:
            channels = self.default_channels

        if burst_period_in_seconds is not None:
            period = burst_period_in_seconds

        if ((burst_period_in_frames is None) and (period is None)) \
           or ((burst_period_in_frames is not None) and (period is not None)):
            raise ValueError("You must specify either 'period' or 'burst_period_in_frames' ")

        if period is not None:
            burst_period_in_frames = max(float(period) / self.FRAME_PERIOD, 1)

        burst_period_in_frames = int(burst_period_in_frames)
        # print "%s" % channels.__repr__()
        # print "%i" frames_per_burst
        # print burst_period_in_frames
        # print burst_period_in_frames*self.FRAME_PERIOD*1000
        # print ('continuously when TRIG=1' if not number_of_bursts else \
        #       ('for a total of %i bursts' % number_of_bursts) )
        if verbose:
            self.logger.debug(
                f'{self!r}: Configuring channelizer {channels} to capture '
                f'{frames_per_burst} frame every {burst_period_in_frames} frames (i.e .every {burst_period_in_frames * self.FRAME_PERIOD * 1000:.3f} ms) '
                f'with first frame offset of {offset} frames ({offset * self.FRAME_PERIOD * 1000:.3f} ms)'
                f'and a send delay factor of {send_delay} ({send_delay * 65536 / 125e6 * 1000:.3f} ms).')

            frames_per_second = frames_per_burst * 1.0 / self.FRAME_PERIOD / burst_period_in_frames
            packet_size_in_bits = (self.FRAME_LENGTH + 10 + 42) * 8  # 10 header bytes, 42 Ethernet/IP/UDP overhead
            self.logger.debug('\n'.join((
                f'{self!r}: Data rates are:',
                f'    1 board, 1 channel: {frames_per_second * packet_size_in_bits / 1e6:.3f} Mbits/s',
                f'    1 board, {len(channels)} channels: {len(channels) * frames_per_second * packet_size_in_bits / 1e6:.3f} Mbit/s',
                f'    1 crate: {16 * 16 * frames_per_second * packet_size_in_bits / 1e6:.3f} Mbits/s'
                )))

        # stop data from going into the PROBER and MASTER to minimize the risk
        # of malformed packets and unstable communications
        reset_state = self.get_ant_reset()

        self.set_trig(0)  # disable data transmission if continuous mode is currently selected
        self.set_ant_reset(1)  # no longer supported by firmware

        # Do not limit the transfer rate
        self.GPIO.HOST_FRAME_READ_RATE = 5

        if self.CAPTURE_TYPE == 'UCAP':
            self.UCAP.set_data_source(source)
            # self.UCAP.config_capture() # doesn't exist yet. fixme
            # self.logger.debug # add some logging?
            self.UCAP.CAPTURE_PERIOD = burst_period_in_frames-1
            self.UCAP.CAPTURE_PERIOD2 = burst_period_in_frames-1

        if self.CAPTURE_TYPE =='PROBER':
            # Stop data capture on *ALL* channels
            for chan in self.get_channelizers():
                chan.PROBER.RESET = 1
            for chan in self.get_channelizers(channels):
                chan.PROBER.SUB_PERIOD = 23  # disable sub period
                chan.PROBER.set_data_source(source)
                chan.PROBER.config_capture(
                    frames_per_burst=frames_per_burst,
                    burst_period=burst_period_in_frames,
                    number_of_bursts=number_of_bursts,
                    offset=offset,
                    send_delay=send_delay)
                ch = chan.chan_number
                self.logger.debug('%r: %s raw data capture on channel %i' % (
                    self,
                    ('Disabling', 'Enabling')[ch in channels], ch))
                chan.PROBER.RESET = 0

        # ** line below no longer supported by firmware *** enables data transmission if continuous mode is selected
        self.set_trig(1)
        self.set_ant_reset(reset_state)

    def set_data_capture(self, channels=None, sub_period=23, source='adc'):
        """ Set the dynamic data capture parameters that can be changed on the
        fly without re-syncing the board.

        Parameters:

            channels (list of int): channels to configure

            sub_period (int): Sets how fast the data is to be temporarily
                transmitted and captured for the selected channel.

                A capture is always done at the beginning of each primary
                period, with subsequent captures spaced by 2**(sub_period+1)
                frames. This can be used to speed up captures, but the rate
                rate cannot be slower than the primary capture rate.

                The spacing between the last capture of a primary period and
                the first one of the following one might differ from other
                intervals if the primary period is not an exact multiple of
                the sub period.

            source (str): Data source to use.
        """

        if channels is None:
            channels = self.get_channels()

        self.logger.debug(
            f'{self!r}: Setting dynamic capture parameters to '
            f'sub_period={sub_period} and source={source} for channels={channels}')

        for chan in self.get_channelizers(channels):
            chan.PROBER.set_data_source(source)
            chan.PROBER.SUB_PERIOD = sub_period

    def set_fft_bypass(self, bypass_mode, channels=None):
        """
        Sets the BYPASS flag on both the FFT modules.
        If the list of channels is specified, only these channels will be set.
        All channelizers are reset to force the FFT to resynchronize to the frame boundaries.

        History:
            2012-08-31 JFC: Added this function
            2012-10-02 JFC: Added channelizer reset after bypass change to ensure the FFT is synced.
            2013-12-05 JFC: Changed behavior so only the specified channels are changed.
            2014-02-09 JFC: Removed scaler bypass setting
        """

        if channels is None:
            channels = self.default_channels

        configured_channels = set()
        for ch in channels:
            if ch not in self.chan:
                self.logger.warning('%r: FFT bypass mode on channel %i are not set '
                                     'because that channel is not available' % (self, ch))
            elif ch not in self.LIST_OF_ANTENNAS_WITH_FFT and not bypass_mode:
                self.logger.warning('%r: FFT bypass mode was disabled on channel %i'
                                     ' which has no FFT module. The command will have no effect.' % (self, ch))
            else:
                self.chan[ch].FFT.BYPASS = bypass_mode
                configured_channels.add(ch)
        self.logger.debug('%r: Setting FFT bypass mode to %s for channel %s' % (
            self,
            str(bool(bypass_mode)),
            ', '.join([str(i) for i in configured_channels])))
        # self.reset()
        # self.sync()

    set_FFT_bypass = set_fft_bypass  # for legacy code compatibility

    def get_fft_bypass(self):
        """
        Returns a list indicating if the FFT is bypassed or not for each channel.
        """
        return [bool(chan.FFT.BYPASS) for chan in self.chan.values()]

    get_FFT_bypass = get_fft_bypass  # for legacy code compatibility

    def set_scaler_bypass(self, bypass_mode, channels=None):
        """
        Sets the BYPASS flag on the SCALER modules.
        If the list of channels is specified, only these channels will be set.

        History:
            2013-12-05 JFC: Added this function
        """

        if channels is None:
            channels = self.default_channels

        self.logger.debug('%r: Setting SCALER bypass mode for channel %s' % (
            self,
            ', '.join([str(i) for i in channels])))
        for chan in self.chan.values():
            if chan.chan_number in channels:
                chan.SCALER.BYPASS = bypass_mode
            # else:
            #     self.logger.warning('Attempting to set SCALER bypass mode for channel channel %i '
            #                          'which is not present on this card' % ch)

    def get_scaler_bypass(self):
        """
        Returns a list indicating if the SCALER is bypassed or not for each channel.
        """
        return [bool(chan.SCALER.BYPASS) for chan in self.chan.values()]

    def reset_scaler_overflow_flags(self, channels=None):
        """ Resets the SCALER overflow flags.

        Parameters:

            channels (list of int): channels to reset. if None, the default channel list (all channels) is used.
        """
        for ch in channels or self.default_channels:
            self.chan[ch].SCALER.OVERFLOW_RESET = 1
            self.chan[ch].SCALER.OVERFLOW_RESET = 0


    def set_global_trigger(self, trigger_state):
        """
        Sets the global trigger to the specified value.

        In injection mode, the injection buffers are read only when
        trigger=True. This allows the buffers from all the channels to be read
        simultaneously. In this case, the CAPTURE flag if the injected frames
        is always set.

        In other modes, the trigger status is passed to the CAPTURE flag of
        the data frames on a frame-by-frame basis (the CAPTURE flag is set at
        the begining of the frame ans syats constant until the end of the
        frame so no partial frames will be captured downstream.)

        History:
            120918 JFC: Added this function
        """
        self.GPIO.set_global_trig(trigger_state)

    def get_version(self):
        """
        Returns the firmware revision currently running on the FPGA (which is the date and time of bitstream generation)
        """
        return self.GPIO.get_bitstream_date()

    def get_adc_delays(self):
        """ Return the current SYNC and ADC delays.

        Returns:

            A dict containing the items:

               0:delay_info, 1:delay_info, ... "sync_delays":sync_delays

            where

            - ``delay_info`` is a dict containing the ``tap_delays``, ``sample_delay`` and ``clock_delay`` for the channels specified in the key.
            - ``sync_delays`` is a list of the tap delays applied on the ADC sync line for each mezzanine
        """
        delay_table = self.chan.get_adc_delays()
        delay_table['sync_delays'] = self.REFCLK.get_sync_delays()
        return delay_table

    def set_adc_delays(
            self,
            source='default',
            compute_delays=1,
            save_delays=True,
            check_sync_delays=False,
            check_adc_delays=20,
            delay_table_folder=ADC_DELAY_TABLE_FOLDER,
            verbose=1,
            retry=5):
        """
        Set all the hardware delays (sync delays, ADC tap delays, sample_delay, clock_delay) required
        to achieve proper data acquisition from the ADCs.


        If `source` is None, does not contain or does not point to an existing delay table entry
        (including a missing delay file or missing tag), new delays will be computed if
        `compute_delays > 0`. If recomputing is not allowed, an exception will be raised.

        If valid delays exist but `compute_delays==2`, new delays will be computed anyways.

        The delays obtained at this point will be checked according to the `check_sync_delays` or
        `check_adc_delays` parameters. If the check fails, new delays delays will be computed if
        allowed, otherwise an exception will be raised. Computation and test of delays will tried up
        to `retry` times.

        If new delays were computed successfully and `save_delays` is True, the new delays will be
        saved in the delay table under the tag specified in `source`, or under the 'default' tag if
        `source` is None or empty.


        Scenarios:

            - No delay table provided: compute delay, test succeed, save delays, return
            - No delay table provided: compute delays, test failed, retry compute delays, test
              succeed, save delays, return
            - No delay table provided: compute delays, test failed, retry compute delays, test fail,
              exception
            - load delays, test succeed, return
            - load delays, test fail, compute, test succeed, save, return
            - load delays, test fail, compute, test fail, retry compute, test succeed, save, return
            - load delays, test fail, compute, test fail, retry compute, test fail, exception

        Parameters:

            source: Depending on the type of `source`:

               - *None*: no source is specified. `compute_delays` must be > 0 so new delays will be
                 computed.
               - *str*: fetch the latest delays from the delay file with the tag specified by
                 `source`. Defaults to the tag named 'default'
               - *dict*: use the delay tables provided by `dict`

            compute_delays (int): Determines when the delays are checked and when new ones should be computed

                - 0: Never compute delays. In this case, `source` must contain or point to valid
                  delay tables.
                - 1: Recompute delays if `source` is not specified or is invalid, or if errors are
                  detected during checks
                - 2: Always recompute delays, ignoring `source`.

            save_delays (bool): If True, newly computed delay will be saved in the delay table file

            check_sync_delays (int): Number of times the sync delays will be checked by pulsing the ADC
                ``sync`` line and verifying that the phase of the ADC clock stays constant relative
                to the system clock. If the test fails and if `compute_delays` allows it, a new
                delay for the sync pulse will be computed.

            check_adc_delays (int): Number of times the ADC is sync'ed and ramp data is read to
                check the integrity of the data acquisition. If the test fails and if
                `compute_delays` allows it, new data line delays will be computed.


            delay_table_folder (str): folder in which the delay table should be loaded from or save to.

            verbose (bool): If True, print the progress and results of the delay calculation and tests

        Returns:
            None

        The delays are saved in the folder 'adc_delay_files/MGK7MB_SNxxx.yaml', where xxx is the
        serial number of the motherboard. New delays are appended to the file. Each delay table is
        associated with a timestamp and a tag. The latest timestamp for a given tag is used.

        The delay file is a list in the format:

            ``[ {__tag__: , __date__:, __mezzanines__:, delay_table:}, ...]`` where:

                - __tag__ (str): arbitrary string identifying the set of delays. Multiple delays can
                  be saved on the same tag.
                - __date__ (str): date in ISO format where the delays were saved. used to find the
                  most recent set of delays.
                - __mezzanines__ ((str, str) tuple): tuple representing the model and serials of
                  both mezzanines. A delay table entry will be ignored unless both mezzanine IDs
                  match the current ones.
                - delay_table: dict containing the delay information to be applied for the mezzanines.

        A delay table is a dict in the following format::

            valid: bool
            0:
               tap_delays: [bit0_tap_delay, but1_tap_delay,...]
               sample_delay: int
               clock_delay: int
            1: ...
            ...
            15: ...
        """
        delay_table = None
        delay_table_updated = False

        # Don't bother getting delays from the specified source if we are going to recompute the delay table anyways
        if compute_delays < 2:
            if isinstance(source, str):
                delay_table = self._load_adc_delays(source, delay_table_folder=delay_table_folder)
            elif isinstance(source, dict):
                delay_table = source
            else:
                raise TypeError('Source must be either a tag from the delay file or a dict')

            if delay_table:
                self._set_adc_delays(delay_table)

                # Check sync delays
                if check_sync_delays:
                    sync_errors = self.REFCLK.check_sync_delays(trials=check_sync_delays, verbose=verbose)
                else:
                    sync_errors = None

                if check_adc_delays:
                    ramp_errors = self.check_ramp_errors(trials=check_adc_delays, verbose=verbose)
                else:
                    ramp_errors = None

                if sync_errors:
                    self.logger.warning(f'{self!r}: Provided delay table failed sync checks. There were {sync_errors} sync errors.')

                if ramp_errors:
                    self.logger.warning(f'{self!r}: Provided delay table failed adc ramp checks. There were {ramp_errors} ramp errors.')

                if sync_errors or ramp_errors:
                    if compute_delays:
                            self.logger.info(f'{self!r}: The provided delay table failed the sync or ramp checks. Proceeding to compute new tables.')
                    else:
                        raise RuntimeError(f'The provided delay table failed the sync and ramp checks and '
                                           f'we are not allowed to compute new delays.')
                else:
                    # If the provided delay table has no errors and we don't want to compute new delays, then we are done since the delays were already set.
                    return

            else:
                if compute_delays:
                    self.logger.warning(f'{self!r}: Delay tables were neither provided nor found. Proceeding to compute new delays.')
                else:
                    raise RuntimeError(f'Delay tables were neither provided nor found, and we are not allowed to compute new delays')

        # We get here if compute_delays >0
        self.logger.debug(f'{self!r}: Computing new sync and/or ADC delays')

        for trial in range(retry):


            sync_delays = self.compute_sync_delays(
                set_sync_delays=True,
                verbose=verbose)

            if check_sync_delays:
                sync_errors = self.check_sync_delays(
                    trials=check_sync_delays,
                    verbose=verbose)
            else:
                sync_errors = None

            if sync_errors:
                self.logger.warning(f'{self!r}: Provided delay table failed sync checks. There were {sync_errors} sync errors.')


            new_delays = self.compute_adc_delays(
                channels=list(range(16)),
                verbose=verbose,
                set_delays=True)

            if check_adc_delays:
                ramp_errors = self.check_ramp_errors(trials=check_adc_delays, verbose=verbose)
            else:
                ramp_errors = None

            if ramp_errors:
                self.logger.warning(f'{self!r}: Provided delay table failed adc ramp checks. There were {ramp_errors} ramp errors.')

            new_delays['sync_delays'] = sync_delays
            new_delays['valid'] = not bool(sync_errors or ramp_errors)

            if sync_errors or ramp_errors:
                if trial == retry - 1:
                    raise RuntimeError(f'{self!r}: Could not compute valid delay tables after {retry} trials. Giving up.')
                else:
                    self.logger.warning(f'{self!r}: Computed delay table failed checks on trial {trial+1}. Retrying...')
            else:
                if save_delays:
                    self._save_adc_delays(new_delays, tag=source or 'default', delay_table_folder=delay_table_folder)
                return

    def _load_adc_delays(self, tag='default', delay_table_folder=ADC_DELAY_TABLE_FOLDER):
        filename = '%s.yaml' % self.get_string_id()
        fullpath = os.path.join(delay_table_folder, filename)

        # print 'Loading YAML file %s' % filename
        try:
            with open(fullpath, 'r') as yamlfile:
                file_data = yaml.load(yamlfile, Loader=yaml.SafeLoader)
        except IOError:
            print('%s not found' % fullpath)
            return None
        if file_data is None:
            return None
        if not isinstance(file_data, list):
            raise RuntimeError('Delay table file should be a list')

        latest_delay_table = None
        latest_date = None
        for entry in file_data:
            if any(key not in entry for key in ('__tag__', '__mezzanines__', '__date__', 'delay_table')):
                continue
            mezzanines = {i: m.get_id() for i, m in self.mezzanine.items()}
            if entry['__tag__'] == tag and entry['__mezzanines__'] == mezzanines:
                date = datetime.strptime(entry['__date__'], "%Y-%m-%dT%H:%M:%S.%f")
                if latest_date is None or date >= latest_date:
                    latest_date = date
                    latest_delay_table = entry['delay_table']
        return latest_delay_table

    def _save_adc_delays(self, delay_table, tag='default', delay_table_folder=ADC_DELAY_TABLE_FOLDER):
        if not delay_table:
            raise ValueError('Please specify a valid delay table')
        filename = '%s.yaml' % self.get_string_id()
        fullpath = os.path.join(delay_table_folder, filename)
        print('Loading YAML file %s' % filename)
        try:
            with open(fullpath, 'r') as yamlfile:
                file_data = yaml.load(yamlfile, Loader=yaml.SafeLoader)
        except IOError:
            print('%s not found' % fullpath)
            file_data = []

        if file_data is None:
            file_data = []

        if not isinstance(file_data, list):
            raise RuntimeError('Delay table file should be a list')

        mezzanines = {i: m.get_id() for i, m in self.mezzanine.items()}
        date = datetime.utcnow().isoformat()

        new_entry = dict(__date__=date, __tag__=tag, __mezzanines__=mezzanines, delay_table=delay_table)
        print('new entry: ', new_entry)
        file_data.append(new_entry)
        s = yaml.safe_dump(file_data, default_flow_style=None)
        # Save file. Make sure we raise en exception here before we start writing the file,
        # otherwise we will lose the whole file.
        with open(fullpath, 'w') as yamlfile:
            yamlfile.write(s)


    def set_sync_delays(self, sync_delays):
        """ Sets the sync delay for both mezzanines.

        Paramters:

            delays (list or tuple): 2-element list or tuple describin the delays to be applied to the SYNC line of each mezzanine. Each element ranges from 0 to 31.

        """
        self.REFCLK.set_sync_delays(sync_delays)


    def _set_adc_delays(self, delay_table):
        """
        """
        sync_delays = delay_table.get('sync_delays', None)
        self.set_sync_delays(sync_delays)
        self.chan.set_adc_delays(delay_table)

    def check_ramp_errors(self, delay=0.1, trials=10, verbose=1):
        """
        Puts all ADCs in ramp mode and use the firmware ramp checker to check if the data acquired from them is valid.

        When the test is done, the ADC is then put in its original mode.

        Parameters:

            delay (float): Period of time during which the ADC data is checked.

            trials (int): Number of times the  ADC is sync'ed and the data is checked.

            verbose (bool): If True, prints the check progress and results.

        Returns:
            A dictionary listing the total number of mismatched words words were detected for all channels and all
            trials combined.

        """

        assert self.HAS_ADCDAQ, 'The platform does not support the MGADC08 mezzanine and associated ADCDAQ firmware'

        old_adc_mode = self.get_adc_mode()
        self.set_adc_mode('ramp')
        word_errors = []
        if verbose:
            self.logger.info(f'{self!r} Performing ADC Delay checks for board {self.get_id()}')
        for trial in range(trials):
            if verbose:
                self.logger.debug(f'{self!r}: Trial #{trial+1}')
            self.sync()  # This automatically clears the error counter
            time.sleep(delay)
            s = ''
            for i, chan in self.chan.items():
                e = chan.ADCDAQ.RAMP_ERR_CTR
                # We still sometimes get one (and only one) spurious error
                # count just after sync. There is probably still a firmware
                # problem. We'll ignore it by software.
                if e == 1:
                    e = 0
                be = chan.ADCDAQ.BIT_ERR_CTR  # bit error counters
                word_errors.append(e)
                # chan.ADCDAQ.RAMP_ERR_CLEAR = 0
                # chan.ADCDAQ.RAMP_ERR_CLEAR = 1
                if verbose:
                    s += f'CH{i:02d}:{e:2d} ({be:08X}) '
            self.logger.debug(f'{self!r}: {s}')
        self.set_adc_mode(old_adc_mode)
        return sum(word_errors)

    def capture_adc_eye_diagram(self, channels=list(range(16))):
        """
        Measures the eye diagram of the ADC digital data lines using the ADCDAQ capture feature.

        Parameters:
            channels (list of int): List of channels to which the command is applied

        Returns:

            ``N_channels`` x 32 x 11 byte array, where ``N_channels`` is the numbe of channels
            specified in `channels`.

        Note:

        """
        assert self.HAS_ADCDAQ, 'The platform does not support the MGADC08 mezzanine and associated ADCDAQ firmware'

        old_delays = self.get_adc_delays()
        # Get current ADC mode. Make sure we don't access boards not on the channel list: they may be powered off
        old_adc_mode = self.get_adc_mode(channels=channels)
        for ch in channels:
            self.chan[ch].ADCDAQ.set_delays((None, 0, 0))  # set all sample delays to zero before sync
        self.set_adc_mode('pulse', channels=channels, sync=True)  # generate pulse pattern and sync
        period = 11  # The pulse waveform repeats every 11 samples
        for i in range(1):
            self.sync()
            time.sleep(0.01)
        data = np.zeros((len(channels), 32, period), np.uint8)

        for i, ch in enumerate(channels):
            # d = np.zeros((32, 11), dtype=np.uint8) # 32 delays x 11 offsets
            # self.logger.info('%.32s: Reading channel %i.' % (self, ch))
            adcdaq = self.chan[ch].ADCDAQ
            for dly in range(32):
                # Set delay, don't change sample delay. No need to sync because sample delay not changed.
                adcdaq.set_delays(([dly] * 8, None, None))
                data[i, dly, :] = adcdaq.capture_pattern(period=11)
        self._set_adc_delays(old_delays)  # restore original SYNC and ADC delays before the function was called
        self.set_adc_mode(old_adc_mode, channels=channels)
        return data

    def compute_sync_delays(self,
                            channels=list(range(16)),
                            set_sync_delays=True,
                            verbose=1):
        """ Compute and set the SYNC delays for the ADC mezzanines.

        The ADC SYNC delays are adjusted by sweeping the SYNC delay lines and
        measuring the location of the phase jumps in the ADC pulse pattern
        after a sync pulse. We select the delay that is as far as possible from such jumps.

        The ADC chips are put in pulse mode for the computations and are then returned to their original mode.

        Returns:

            list of tap delays to be applied to the ADC sync line of each mezzanine.


        """

        old_adc_mode = self.get_adc_mode(channels=channels)
        self.set_adc_mode('pulse')

        adc_sampling_freq = self._sampling_frequency
        sync_delays = self.REFCLK.compute_sync_delays(
            adc_clock_freq=adc_sampling_freq / 2,
            set_sync_delays=set_sync_delays,
            verbose=verbose)

        self.set_adc_mode(old_adc_mode, channels=channels)

        return sync_delays

    def check_sync_delays(self,
                          channels=list(range(16)),
                          trials=10,
                          verbose=1):
        """Checks if the ADC yields stable results with the current SYNC delays.


        The ADC chips are put in pulse mode for the computations and are then returned to their original mode.

        Returns:

            int: number of errors (phase jumps)

        """

        old_adc_mode = self.get_adc_mode(channels=channels)
        self.set_adc_mode('pulse')

        adc_sampling_freq = self._sampling_frequency
        sync_errors = self.REFCLK.check_sync_delays(
            trials=trials,
            adc_clock_freq=adc_sampling_freq / 2,
            verbose=verbose)

        self.set_adc_mode(old_adc_mode, channels=channels)

        return sync_errors

    def compute_adc_delays(
            self,
            channels=list(range(16)),
            verbose=True,
            set_delays=True):
        """ Computes the ADC data line delays to ensure reliable data acquisition.



        The ADC data line delays are adjusted by sweeping the delay of each
        data line (bit) of the ADC in pulse mode and looking for the center of
        the pulse. In other words, it measures the eye diagram of the ADC
        digital data lines and computes the optimum delays


        Parameters:

            channels (list): List of channels for which ADC delays are to be computed

            verbose (bool):

            compute_sync_delays (bool):

            check_sync_delays (bool):

            check_adc_delays (bool):

            set_delays (bool):


        Returns:

        """

        new_delays = {}

        tap_delay = 1 / 200e6 / 32 / 2
        adc_sampling_freq = self._sampling_frequency
        pulse_period = int((1 / adc_sampling_freq) / tap_delay)  # 800 MHz period in tap delays (16 taps)




        data = self.capture_adc_eye_diagram(channels)  # N_chan x 32 x 11 array

        for i, ch in enumerate(channels):

            # First, find the offset for which the smallest number of '1' bits for every bit is as high as possible.
            #
            # Step 1: compute the smallest number of '1' for each possible bit, for each offset
            q = np.array([((data[i] & (1 << bit)) != 0).sum(axis=0) for bit in range(8)]).min(axis=0)
            # Step 2: find the offset that has the largest number of '1's
            offset = q.argmax()
            print('CH%02i: offset=%2i : %s' % (ch, offset, q))

            # Extract the samples for the current channel and selected offset, byt keep all 32 delays
            n = data[i, :, offset]

            # Now find the optimal delay for each bit
            computed_delay = np.zeros(8, dtype=np.uint8)
            for bit in range(8):
                mask = 1 << bit
                d = (n & mask) >> bit
                # Convert to a string of "1" and "0"s so we can use the 'find' method. before doing
                # that, make sure this is an int8 array otherwise we'll get more than one char per
                # value...
                s = (d.astype(np.int8) + ord('0')).tobytes()
                re = s.find(b'0111')
                fe = s.find(b'1110')
                # print s,re,fe
                if re >= 0 and fe >= 0 and fe > re:  # if we have both a rising edge
                    delay = (fe + 2 + re + 1) / 2
                elif re >= 0:  # if we have a rising edge only
                    # Compute delay. Is 4 samples after the rising edge, but stop at max delay. -1
                    # to be closer to the known good edge.
                    delay = min(re + 1 + pulse_period / 2 - 1, 31)
                elif fe >= 0:
                    delay = max(fe + 3 - pulse_period / 2 - 1, 0)
                else:
                    delay = -1  # invalid delay
                # computed_delay[bit] = np.sum(d*range(32)) / np.sum(d)
                computed_delay[bit] = delay

                bit_string = ''
                for delay in range(len(d)):
                    if delay == computed_delay[bit]:
                        bit_string += '!O'[d[delay]]
                    else:
                        bit_string += '.#'[d[delay]]
                s = 'Bit %i: %s Delay = %2i   (rise @ %2i, fall @ %2i)' % (
                    bit,
                    bit_string,
                    computed_delay[bit],
                    re + 1,
                    fe + 2)
                if verbose:
                    print(s)
                # self.logger.info(s)

            new_delays[ch] = {
                'tap_delays': computed_delay.tolist(),
                'sample_delay': int((offset + 3) % 11),
                'clock_delay': 0}

        if set_delays:
            self._set_adc_delays(new_delays)

        return new_delays

    def compute_adc_delay_offsets(self, channels=list(range(16))):
        """
        FOR QC ONLY. Measures the eye diagram of the ADC digital data lines and computes
        the permissible offset to ensure reliable data acquisition.

        Returns a delay/offset table (delaytable), flags any stuck bits
        (stuckbits), provides the logic level at the chosen eye sampling point
        (bitposgood)  and in that order. Note that stuck bits should all be
        false, bitposgood should be all 1s

        This method is used in testing the ADC mezzanines. Use `compute_adc_delays()` during normal operations.

        """
        delaytable = {}
        stuckbits = {}
        bitposgood = {}
        problem = 0

        for chan in channels:
            # Creating an offset / delay table 11 columns 32 rows
            t = self.read_eye_diagram(channels=[chan], offset=[0]*16, noffsets=11)

            # Finding both 0 and 255 in the table Means we have no stuck bits
            stuckbits[chan] = not (np.any(t[chan] == 0) and np.any(t[chan] == 255))
            if stuckbits[chan]:
                problem = 1  # Stuck bit detected

            # Choose the offset by looking at the offset/delay table and picking the column with
            # the highest sum (i.e most 255s)
            offset = t[chan].sum(axis=0).argmax()

            bitdelay = []
            changood = []
            for bit in range(8):
                mask = 1 << bit  # looking at one adc bit at a time
                sample = (t[chan][:, offset]) & mask
                if any(sample):  # If sample has nonzero values
                    # Perform a center of mass calculation to pick eye location
                    chosendelay = int((sample * np.arange(32)).sum() / sample.sum())
                    # Check what the bit level at the eye center is
                    changood.append( (((t[chan][:, offset])[chosendelay]) & mask) >> bit)
                else:  # Sample is all zeros
                    chosendelay = np.NaN
                    changood.append(np.NaN)
                    problem = 1  # Can't find a good spot so indicate a problem is present
                bitdelay.append(chosendelay)
                # self.logger.info( 'Warning: Center of eye diagram on bit %i of channel %i has glitch ' % (bit, chan))

            # The difference in offset between a pulse waveform 'high' sample and and the first sample of a  ramp
            offset = (offset + 3) % 11
            # offset = offset - 3  #The difference in offset between a pulse waveform and a ramp
            # if offset < 0:  # An untested wrap around conddition (Adam 12/12/2014)
            #     offset = offset + 11

            delaytable[chan] = (bitdelay, [offset] * 8)  # Building the delay table
            bitposgood[chan] = changood  # Building the eye diagram good table

            if 0 in changood:
                problem = 1  # Inverted bit detected

        return delaytable, stuckbits, bitposgood, problem


    def status(self):
        """
        """
        self.logger.info('%r: ----------- chFPGA status ---------------' % self)
        self.logger.info('%r:  Controller IP address: %s, port: %i ' % (self, self.ip_addr, self.mb.fpga.port_number))
        self.logger.info('%r:  Firmware version: %s' % (self, self.get_fpga_firmware_version()))
        self.logger.info('%r:  Number of channel inputs: %i' % (self, self.NUMBER_OF_CHANNELIZERS))
        self.logger.info('%r:  Number of channelizers with FFT: %i (channels %s)' % (
            self,
            len(self.LIST_OF_ANTENNAS_WITH_FFT),
            str(self.LIST_OF_ANTENNAS_WITH_FFT)))
        self.logger.info('%r:  Number of correlators: %i (correlators %s)' % (
            self,
            len(self.LIST_OF_IMPLEMENTED_CORRELATORS),
            str(self.LIST_OF_IMPLEMENTED_CORRELATORS)))

        self.FreqCtr.status()

    def set_data_width(self, width):
        """
        Set the number of bits used to represent the values computed by the channelizers and used by
        the GPU link and FPGA correlators.

        All channelizers, crossbars and correlators are set to the new setting.

        Parameters:

            width (int): Data width to use

                - width=4: data is 4 bits Real + 4 bits Imaginary

                - width=8: data is 8 bits Real + 8 bits Imaginary
        """

        if width not in (4, 8):
            raise ValueError('Number of bits %i is invalid. Only 4 or 8 is allowed' % width)

        # Set the channelizer data width
        self.chan.set_data_width(width)

        # Set the crossbar data width
        self.CROSSBAR.set_data_width(width)

    def get_data_width(self):
        """
        Returns number of bits used to represent the values computed by the channelizers and used by
        the GPU link and FPGA correlators.

        If all the hardware modules are not set in the same mode, an error is raised.
        """
        # get the channelizer and crossbar data width
        chan_data_width = self.chan.get_data_width()
        xbar_data_width = self.CROSSBAR.get_data_width()

        if xbar_data_width and xbar_data_width != chan_data_width:
            raise RuntimeError("The channelizers and crossbar are not set "
                               "to the same data width (chan=%i bits, xbar=%i bits). "
                               "The data stream won't make much sense" % (chan_data_width, xbar_data_width))
        return chan_data_width

    def configure_crossbar(self, *args, **kwargs):
        self.CROSSBAR.configure(*args, **kwargs)

    def set_offset_binary_encoding(self, offset=True, channels=None, sync=True):
        """
        Set the output to be encoded in offset binary instead of 2's complement
        if sync is true, perform a sync afterward.  Necessary for data to continue flowing

        Parameters:
            channels (list of int): List of channels to which the command is applied

        """
        if not offset:
            self.logger.warning(f'Offset binary Encoding is disabled: this mode is incompatible with the firmware correlator and may confuse gain calibrations. This should not be done.')

        if channels is None:
            channels = self.default_channels

        if not isinstance(channels, list):
            raise ValueError("Channels must be a list")
        else:
            # Set the scaler to use offset binary
            for channel in channels:
                self.chan[channel].SCALER.USE_OFFSET_BINARY = offset
            if sync:
                self.sync()

    def set_send_flags(self, send_flags=True, crossbar_outputs=None, sync=True):
        """
        Sets the Corner-Turn engine to add and send Scaler and Frame and flags in its output
        packets.

        Parameters:

            send_flags (bool): If True, the flags will be sent.

            crossbar_outputs (list of int): list of indices of the 1st CROSSBAR outputs that should
                be configured.

            sync (bool): If True (default), a local sync() will be performed.
        """
        if crossbar_outputs is None:
            crossbar_outputs = list(range(self.NUMBER_OF_CROSSBAR1_OUTPUTS))

        if not isinstance(crossbar_outputs, list):
            raise ValueError("'crossbar_outputs' must be a list")
        else:
            # Set the scaler to use offset binary
            for output in crossbar_outputs:
                self.CROSSBAR[output].CH_DIST.SEND_FLAGS = send_flags
            if sync:
                self.sync()

    def get_formatted_id(
            self,
            crate_slot_format='FCC{crate:02d}{slot:02d}',
            no_crate_format='{slot!s}',
            no_slot_format='{crate!s})'):
        """ Return a string that represent the board using the provided format list.

        Parameters:

            x_format (str): format to be applied in the specified condition.
            Uses the .format() syntax, with the following fields: slot=0-based
            slot number (int) or board model/serial (str); crate=crate number
            (int), crate model/serial (int) or None

        Returns:
            string
        """

        crate, slot_0based = board_id = self.get_id()

        if crate is None:
            return no_crate_format.format(slot=slot_0based, crate=crate, id=board_id)
        elif slot_0based is None:
            return no_slot_format.format(slot=slot_0based, crate=crate, id=board_id)
        elif isinstance(slot_0based, int) and isinstance(crate, int):
            return crate_slot_format.format(slot=slot_0based, crate=crate, id=board_id)
        else:
            return self.get_string_id()

    def get_gains_filename(self, folder=''):
        """ Return the name of the fulle path and filename of the file containing the gains for this board.


        """
        gain_filename = os.path.join(folder, 'gains_%s.pkl' % self.get_formatted_id())
        return gain_filename

    def load_gains(self, folder='.'):
        """ Loads the gain file associated with this board and return the gains.

        The gain file is a pickled dictionary in the format {channel_number:gains,..}.

        Parameters:

            folder (str): Folder in which the gain files are to be found. Default is the current directory.

        Returns:

            gains that have been loaded. `None` if the gains are not found.

        """

        gain_filename = self.get_gains_filename(folder=folder)
        try:
            with open(gain_filename, 'r') as f:
                gains = pickle.load(f)
            self.logger.debug('r: Loaded gains for board %s from file %s' % (self, gain_filename))
            # ib.set_gain(g_array, bank=bank)  # *** should this be bank=all_bank
        except IOError:
            self.logger.warning("Gain file '%s' not found for (crate,slot)= %r" % (gain_filename, self.get_id()))
            gains = None

        # # Fill any missing channel info with None
        # for ch in range(self.NUMBER_OF_CHANNELIZERS):
        #     if ch not in gains:
        #         gains[ch] = None
        return gains

    def save_gains(self, gains=None, folder='.'):
        """ Save the gains file associated with this board.

        The gain file is a pickled dictionary in the format {channel_number:gains,..}.

        Parameters:

            gains (dict): gains for all channels, in the format {channel_number:gains,..}

            folder (str): Folder in which the gain files are to be found. Default is the current directory.

        Returns:

            gains that have been loaded. `None` if the gains are not found.

        """
        gain_filename = self.get_gains_filename(folder=folder)

        try:
            with open(gain_filename, 'w') as f:
                gains = pickle.dump(gains, f)
            # self.logger.info('Setting gains on IceBoard SN%s, crate %s, slot %i' % (ib.serial, crate, slot))
            # ib.set_gain(g_array, bank=bank)  # *** should this be bank=all_bank
        except IOError:
            self.logger.warning("Gain file '%s' could not be saved for (crate,slot)=%r " % (gain_filename, self.get_id()))

    def set_scaler_eight_bit(
            self,
            scaler_eight_bit: bool,
            prober_user_flags: bool,
            channels: List[int],
    ):
        """
            Sets the scaler to 8-bit/4bit mode. The mode will be set individually for each channelizer.

            Parameters:

                scaler_eight_bit (bool): If True - sets the channel to 8-bit mode, otherwise 4-bit mode

                prober_user_flags (bool): If True, returns user flags in last 4 bits captured from scaler

                channels (list of int): channels to which the specified mode is applied. If 'channels' is None, it is
                applied to the default (active) channels (see set_default_channels()).

            Notes:


            Examples:

                set_eight_bit_scaler(True, False, [0,1,2,3])    # Sets first four channels to the 8-bit mode with data
                                                                taking the whole byte
                set_eight_bit_scaler(True, True)                # Sets active channels to the 4-bit mode, but last
                                                                4-bits in each byte will be returned user flags

            History:
                2023-10-10 Vadym B.: Added this function
            """

        if channels is None:
            channels = self.default_channels

        eb_support = [chan.SCALER.EIGHT_BIT_SUPPORT for chan in self.chan]
        if not all(eb_support):
            raise RuntimeError(
                f"The scaler of channelizers {[ch for ch in range(len(self.chan)) if not eb_support[ch]]} "
                f"does not support 8-bit mode."
            )

        for ch in range(len(self.chan)):
            if ch in channels:
                self.logger.debug(f"Setting scaler of channel {ch} to 8-bit mode.")
                self.chan[ch].SCALER.FOUR_BITS = not scaler_eight_bit

                self.logger.debug(
                    f"{'Adding' if prober_user_flags else 'Removing'} user flags from scaler data of channel {ch}."
                )
                if prober_user_flags is not None:
                    self.chan[ch].PROBER.PROBER_USER_FLAGS = prober_user_flags

    def set_scaler_rounding_mode(
            self,
            scaler_rounding_mode: Literal[1, 2, 3],
            channels: List[int],
    ):
        """
            Sets the rounding mode of the scaler. The mode will be set individually for each channelizer.

            Parameters:

                scaler_rounding_mode (int): 0 - truncation, 1 - rounding, 2 - convergent rounding

                channels (list of int): channels to which the specified mode is applied. If 'channels' is None, it is
                applied to the default (active) channels (see set_default_channels()).

            History:
                2023-10-23 Vadym B.: Added this function
            """

        rounding_options = {
            0: ("truncation", SCALER.ROUNDING_MODE_TRUNCATE),
            1: ("rounding", SCALER.ROUNDING_MODE_ROUND),
            2: ("convergent_rounding", SCALER.ROUNDING_MODE_CONVERGENT_ROUND),
        }

        if scaler_rounding_mode not in rounding_options.keys():
            raise ValueError(f"Invalid rounding mode requested. Available options: {list(rounding_options.keys())}")

        rm_name, rm_code = rounding_options[scaler_rounding_mode]

        if channels is None:
            channels = self.default_channels

        for ch in range(len(self.chan)):
            if ch in channels:
                self.logger.debug(f"Setting rounding mode of scaler in channel {ch} to {rm_name}.")
                self.chan[ch].SCALER.ROUNDING_MODE = not rm_code

    def set_gains(
            self,
            gain=None,
            postscaler=None,
            channels=None,
            use_fixed_gain=False,
            bank=0,
            when=None,
            gain_timestamp=None):
        """
        Sets the digital gain used by the SCALER module to scale the (18+18)
        bits output of the FFT to the (4+4) final channelizer output format.

        The gain can be set individually for every frequency bins and every
        ADC channel.

        Parameters:

            gain (scalar, tuple, list or dict): Linear gain and optional postscaler gain to apply to the
                specified channels. Gain elements can be defined as:

                - G = Glin_scalar: Single gain for all bins, default postscaler is used
                - G = (Glin_scalar, None): same as above
                - G = (Glin_scalar, Glog): Single gain for all bins with specified postscaler
                - G = Glin_vector: Gain value for each bin, using the default post-scaler value
                - G = (Glin_vector, None) : same as above
                - G = (Glin_vector, Glog) : Gain value for each bin with specified post-scaler value

                where:

<<<<<<< HEAD
                - ``Glin_scalar`` is a real or complex number. The real and imaginary part of the
                  linear gain are integer values ranging from -32768 to 32767.

                - ``Glog`` is the postscaler factor. This is a binary scaling factor, which is an
                  integer between 0 and 31 representing a power of two that multiplies the linear
                  gain. It is common to every bin.
=======
                    - ``Glin_scalar`` is a real or complex number. The real and imaginary part of the linear
                    gain are integer values ranging from -32768 to 32767.
                    - ``Glog`` is the postscaler factor. This is a binary scaling factor, which is an integer
                          between 0 and 31 representing a power of two that multiplies the linear
                          gain. It is common to every bin.
>>>>>>> origin/vb/firmtest

                - ``Glin_vector`` is a 1024-element vector of ``Glin_scalar``, where each element is
                  the individual gain of every bin.

                `gain` can take the following form:

                - `gain` = G. If `gain` is a scalar or tuple, the specified gains are applied to all
                  channels specified in `channels`.

                - `gain` = {ch_number1: G1, ch_number2: G2 ...}: If `gain` is  a dict, the gain is
                  applied to specified channel numbers, but only if they are included in `channels`

                - `gain` = [ (ch_number1, G1),  (ch_number2, G2), ...] or `gain` = [ (ch_list1 ,
                  G1), (ch_list2, G2), ...]: If `gain` is a list of tuples, a specified gain ``G``
                  profile is applied to unique channels number or to all channels in a of a list of
                  channel numbers. Channels not specified in `channels` are not set.

            postscaler (int): Postscaler factor to apply if ``Glog`` is not specified (Glog=None) in
                ``gain``.

            channels (list of int): channels to which the gain is applied. If 'channels' is None, it
                is applied to the default (active) channels (see set_default_channels()).

            use_fixed_gain (bool): put the scaler in fixed gain mode where the gain bank RAM is
                completely bypassed and a single complex gain is applied to every bin. Is functionally
                equivalent to set the gain of every bin to the same value. Mostly useful during the
                debugging phase.

            bank (int): The memory bank to which the gains should be applied (0 or 1) Once written,
                the bank is made active. If ``bank`` is None, the currently inactive bank is used.

            when (int): Specifies when the specified gains shall become active. If `when` is 'now', the
                gains are written immediately on the target bank and the bank is made active on the
                next frame. If `when` is an  *int*, the gains are written immediately to the bank  bank,
                but than bank will become active only on frame number (timestamp) specified by when.

            gain_timestamp: Unix timestamp when the gains were calculated.  If not provided,
                defaults to current time.

            use_fixed_gain (bool): if True, enables the use of fixed gain mode of the scaler module. In this
                case, 'gain' can only be a scalar. Is False by default.



        The actual gain between the scaler input and output for bin 'b' is:

           - 4-bit mode: out/in = :math:`Glin(b) * 2**(Glog-31)`

           - 8-bit output: out/in = :math:`Glin(b) * 2**(Glog-27)`


        Notes:
            #) The PFB/FFT has an intrinsic gain of 512 (a constant FFT input of '1' will yield the value 512 in bin 0 at the input of the scaler.
            #) If the FFT is bypassed, the 8-bit values from the ADC or the function generator are applied directly to the scaler input.
            #) In 4-bit mode, the output value is taken from bits 31 to 34 of the postscaled-value. In 8-bit mode, bits 27 to 31 are used.
            #) A smaller postscaler value allows a larger gain to be used to achieve the same overall gain while providing more gain resolution.
            #) A gain of (1, 31) allows the function generator values to appear on the scaler output with an overall gain of 1 in 4-bit mode. This is equivalent to (2, 30), (4,29) ... (16384, 8), except that the latter offers more gain resolution.
            #) A gain of (1, 27) dies the same in 8-bit mode.
            #) (16384, 8), except that the latter offers more gain resolution.

        Examples:
            set_gain(1) # Sets all gains to 1, leaves the poscslaler unchanged for all channelizers.
            set_gain((1, None)) # Same thing
            set_gain(postscaler = 26) # Sets postscaler on all channelizers
            set_gain((1,31)) # For all channelizers, sets all gains to 1 and postscaler to 31
            set_gain(16384,8) # In 4-bit, FFT enabled mode, outputs a value of '1' on bin 0 when the input of the FFT is a constant '1'.
            set_gain(np.arange(1024), channels=[1,2,3])
            set_gain({1: 16384, 4: 1300+15000*j, 5: np.arange(1024)}) # sets ADC channels 1-3 to a real gain of 16384, channel 4 to complex gain of (1300+15000j), and channels 5-7 with a gain ramp from 0 to 1023.

        History:
            2012-11-28 JM: Added this function
            2014-02-08 JFC: Rewrote and documented this function for the new scaler supporting complex gain tables.
        """

        # if postscaler is not None:
        #     if postscaler<0 or postscaler>31:
        #         raise chFPGAException('Invalid postscaler value');

        #         if postscaler is not None

        if channels is None:
            channels = self.default_channels

        # Convert the gains to a uniform format: a list of (channel, gain_element) tuples. channel
        # can be an integer or list of integers. gain_element can be a scalar or tuple.
        if isinstance(gain, list): # already a list (presumably of tuples)
            pass
        elif isinstance(gain, dict): # convert dict to list of tuples
            gain = list(gain.items())
        else:  # if anything else including None, a scalar, a gain tuple etc.
            gain = [(channels, gain)] # apply gain to all channels

        # Convert the gain timestamp to list
        if isinstance(gain_timestamp, list):
            pass
        elif isinstance(gain_timestamp, dict):
            gain_timestamp = [gain_timestamp[ch] for ch, g in gain]
        else:
            gain_timestamp = [gain_timestamp] * len(gain)

        configured_channels = set()

        for (channel_list, gain_value), timestamp_value in zip(gain, gain_timestamp):
            # Make sure channel_list is a list (in case we provide a single channel number)
            if isinstance(channel_list, int):
                channel_list = [channel_list]

            # Extract Glin and Glog from the specified gain value
            if gain_value is None: # If we have no gain nor postscaler
                Glin = None
                Glog = None
            elif isinstance(gain_value, (tuple, list)):  # The gain element is a tuple
                Glin = gain_value[0]  # scalar or list of scalars
                Glog = gain_value[1]  # integer or None
            else:  # The gain element is presumed to be scalar or a vector
                Glin = gain_value # scalar or list of scalars
                Glog = None

            # Replace default postscaler value if one is provided
            if (Glog is None) and (postscaler is not None):
                Glog = postscaler

            # Process each channel
            for ch in channel_list:
                # Ignore channels not in the `channels` parameters
                if ch not in channels:
                    continue

                # Warn and skip if a channel does not exist
                if ch not in self.chan.keys():
                    self.logger.warning('%r: Gains on channel %i are not set because that channel is not available' % (
                        self, ch))
                    continue

                # Set the postscaler value
                if Glog is not None:
                    self.chan[ch].SCALER.SHIFT_LEFT = int(Glog)

                if use_fixed_gain:
                    if not np.isscalar(Glin):
                        raise TypeError('%r: Only scalar gains are allowed when using set_fixed_gain=True.' % self)
                    self.chan[ch].SCALER.USE_GAIN_TABLE = 0
                    self.chan[ch].SCALER.set_fixed_gain(Glin)
                else:  # use vector-based gain table
                    self.chan[ch].SCALER.USE_GAIN_TABLE = 1
                    self.chan[ch].SCALER.set_gain_table(Glin, bank=bank, gain_timestamp=timestamp_value)
                configured_channels.add(ch)
        self.logger.debug('%r: Setting scaler gains for channel %s' % (
            self,
            ', '.join([str(i) for i in configured_channels])))

        if when is not None:
            self.switch_gains(bank=bank, when=when)

    def get_next_gain_bank(self):
        """
        Return the gain bank that will be used on the next automatic bank switch.
        """
        return [chan.SCALER.READ_COEFF_BANK ^ 1 for chan in self.chan.values()]

    def get_gains(self, bank=0, use_cache=True):
        """
        Returns the log2 SCALER gain and the linear gain table used for each channelizer.

        Parameters:

            bank (int): gain bank from which to get the gains.

        Returns:

            list of gains for each channelizer, in the format::
                 [[channel_number, [linear_gain_table, log gain]], ...]

                 ``linear_gain_table`` is an array of 1024 complex values.
                 ``log gain`` is an integer.
        """
        gain_list = []
        for chan in self.get_channelizers():
            glog = chan.SCALER.SHIFT_LEFT
            glin = chan.SCALER.get_gain_table(bank=bank, use_cache=True)
            gain_list.append([chan.chan_number, [glin, glog]])
        return gain_list

    def get_gain_timestamps(self, bank=0):
        """
        Returns the timestamp at which the gains for each channel was set.

        Parameters:

            bank (int): gain bank from which to get the gains timestamp.

        Returns:

            list of (channel_number, timestamp), one for each channel.
            Elements are None if the gains was not set for that channel.
        """
        return [(chan.chan_number, chan.SCALER.get_gains_timestamp(bank=bank)) for chan in self.get_channelizers()]

    def switch_gains(self, bank=None, when='now'):
        """
        Switch the gains to the specified `bank` at the moment specified by `when`.

        Parameters:

            bank (int): target gain bank in which the gains will be written. If `bank` is -1 or
                None, the unused bank is switched in.

            when ('now', None or int):
                if `when` is 'now' (default), the switch is done immediately.
                If `when` is None, no switch is done.
                if 'when' is an integer, the switch will be done at the frame numbers specified by `when`.
        """

        if when is None:
            return

        for chan in self.chan.values():
            if bank is None or bank < 0:
                next_bank = chan.SCALER.READ_COEFF_BANK ^ 1
            else:
                next_bank = bank

            if when == 'now':
                chan.SCALER.SYNCHRONIZE_GAIN_BANK = False
                chan.SCALER.READ_COEFF_BANK = next_bank
            else:
                chan.SCALER.SYNCHRONIZE_GAIN_BANK = True
                chan.SCALER.READ_COEFF_BANK = next_bank
                chan.SCALER.GAIN_BANK_SWITCH_FRAME_NUMBER = when

    def reset_fft_overflow_count(self):
        """ Reset the FFT overflow counter in all channelizers.
        """
        for chan in self.chan.values():
            chan.FFT.reset_fft_overflow_count()

    def set_fft_shift(self, fft_shift=0b11111111111, channels=None):
        """
        Sets the FFT shift schedule for the FFT.  .

        Parameters:

            fft_shift (int): binary value that indicates whether each FFT stage will shift the
                result (i.e divide by 2) of that stage. Each bit represents a divide by 2 for that
                stage of the FFT.  Default is to shift every stage.  11 stage FFT, so default is
                2**11-1. expects a number  in the range 0b11111111111 (2047) and 0b00000000000 (0)

        History:
            2013-02-19 KMB: Added this function
        """

        if channels is None:
            channels = self.default_channels

        for chan in self.chan.values():
            if chan.chan_number in channels:
                self.logger.debug('%r: Setting FFT shift of channel %i' % (self, chan.chan_number))
                chan.FFT.FFT_SHIFT = fft_shift

    set_FFT_shift = set_fft_shift  # For legacy code compatibility

    def get_fft_shift(self):
        """
        Returns the FFT shift schedule for each channelizer.

        Returns:

            list of int: one integer indicating the shift pattern for each channelizer.
        """
        return [chan.FFT.FFT_SHIFT for chan in self.chan.values()]

    get_FFT_shift = get_fft_shift  # for legacy compatibility

    def set_user_output_source(self, source, output=0):
        """
        Selects the signal to be sent to the user outputs (SMAs & LEDs).

        Parameters:

            source (str): is the source name

                * 'sync' : User-generated SYNC signal (sunc_out)
                * 'pps' : 1 PPS signal from the IRIG-B decoder (pps_out)
                * 'pwm' : Output from the frame-based pwm generator (pwm_out)
                * 'irigb_trig' :# not(irigb_before_target)
                * 'bp_trig' : (bp_trig_reg)
                * 'bp_time' : (bp_time_reg)
                * 'refclk' : 10 MHz reference clock (clk10)
                * 'irigb_gen' : (irigb_gen_out)
                * 'heartbeat1' : (gpio_led_int(4))
                * 'heartbeat2' : (gpio_led_int(7))
                * 'debug1' : (debug1, currently crossbar2.align_pulse)
                * 'debug2' : (debug2, currently crossbar0.lane_monitor)
                * 'user_bit0' : (user_bit(0))
                * 'user_bit1' : (user_bit(1))
                * 'fmc_refclk': Refclk from Mezz selected by user_bits(0:1)
                * 'input' : SMA is a high-impedance input and is not driving any signal


            output (str or int) is the number or name of the output to configure.

               * 'sma_a' or 0: SMA-A on the motherboard
               * 'sma_b' or 1: SMA-B and FPGA LED2 on the motherboard LED on the backplane
               * 'bp_sma' or 2: BP_SMA on the backplane and FPGA LED1

        """
        self.GPIO.set_user_output_source(source, output=output)

    def get_user_output_source(self, output):
        """ Return the name of the source that drives the specified SMA


        Parameters:

            output (sma): Name of the SMA to query

        Returns:

            str: name of the source currently routed to the specified SMA
        """
        return self.GPIO.get_user_output_source(output=output)

    def set_sync_source(self, source):
        """ Sets the source of the signal that will trigger SYNC events.

        Parameters:

            source (str): name of the source. See `REFCLK.set_sync_source()` for valid names.

        """
        if not self.REFCLK:
            self.logger.warning('The platform does not support SYNC sources')
            return

        if source not in self.REFCLK.SYNC_SOURCE_TABLE:
            raise ValueError('Invalid SYNC source name. Valid names are %s' %
                             ', '.join(self.REFCLK.SYNC_SOURCE_TABLE))
        self.REFCLK.set_sync_source(source)
        # If an user SMA is used, configure it as an input
        if source in self.GPIO.USER_OUTPUTS:
            self.set_user_output_source(output=source, source='input')

    def get_sync_source(self):
        """ Return the current source used to trigger SYNC events

        Returns:
            str describing the SYNC trigger source.
        """
        if not self.REFCLK:
            self.logger.warning('The platform does not support SYNC sources')
            return None
        else:
            return self.REFCLK.get_sync_source()

    def set_pwm(self, enable, offset, high_time, period, local_sync=False):
        """ Sets the frame-based PWM generator. All times are stated as the number of frames.

        A SYNC is needed after changes for the changes PWM signal to work properly (it might be in
        some invalid state). A global sync is needed if the PWM signal is to be synchronous across
        multiple boards.

        See GPIO.set_pwm() for more details.

        The `set_user_output_source()` method shall be used to route the PWB signal to the user SMA.

        Parameters:

            enable (bool): if `True`, PWM mode is enabled.

            offset (int): number of frames to wait before the first pulse

            high_time (int) number of frames during which the PWD signal stays high

            period (int): period of the PWM signal in number of frames

            local_sync (bool): If `True`, a local sync signal will be trigerred. Do not use if
                multiple boards need to be opearating synchronously.

        """
        self.GPIO.set_pwm(enable=enable, offset=offset, high_time=high_time, period=period, pwm_reset=False)
        if local_sync:
            self.sync()

    def get_pwm(self):
        """ Return the state of the PWM generator

        Returns:

            A tuple containing the offset, high time, period and reset state.
        """

        return self.GPIO.get_pwm()

    def get_user_bits(self):
        """ Return the state of the user-programmable bits that can be routed to any of the user outputs.

        Returns:

            int: the value of (user_bit0, user_bit1).
        """

        return self.GPIO.get_user_bits()


    def set_user_bits(self, value):
        """ Sets the state of the user-programmable bits that can be routed to any of the user outputs.

        Parameters:

            value (int): The value sof the user bits
        """
        self.GPIO.set_user_bits(value)

    def set_adc_mask(self, mask=0xFF, channels=None):
        """ Set the mask that is applied on the ADC data on the specified channel.

        Parameters:

            mask (int): an 8 bit value which is ANDed with the incoming ADC bytes. mask=0xFF
                therefore disables the masking effect. Can be useful to reduce power consumption of
                the channelizer without affecting synchronization of the data processing pipeline.

            channels (list of int): list of channelizer numbers for which the ADC mask is set.

        """
        assert self.HAS_ADCDAQ, 'The platform does not support the MGADC08 mezzanine and associated ADCDAQ firmware'

        if channels is None:
            channels = self.default_channels

        for ch in channels:
            self.chan[ch].ADCDAQ.BYTE_MASK = mask

#     def check_adc_data_acquisition(self, test_duration=1):
#         """
#         Sets the ADC in ramp mode and compare the incoming ramp in real time with an internally
#         generated ramp to compute the total number of words in error (and an error count for each
#         bit)
#         """
#         old_adc_mode = self.get_adc_mode()
#         self.set_adc_mode('ramp')
#         self.sync() # sync the board to make sure that data acquisition starts on the right ramp sample

#         # Clear the word and bit error counters
#         for chan in self.chan.values():
#             print 'Clearing channelizer', chan.chan_number
#             chan.ADCDAQ.RAMP_ERR_CLEAR=0
#             chan.ADCDAQ.RAMP_ERR_CLEAR=1
#         self.logger.info('%r: Measuring the data acquisition error rate over %0.1f seconds...' % (
#               self, test_duration))
#         t0 = time.time();
#         word_error = np.zeros(len(self.chan))
#         bit_error = np.zeros((len(self.chan), 8))
#         try:
#             while time.time() - t0 <= test_duration:
#                 for (i, chan) in self.chan.items():
#                     print  self.chan[i].ADCDAQ.RAMP_ERR_CTR,
#                     word_error[i] += chan.ADCDAQ.RAMP_ERR_CTR
#                     for bit_number in range(8):
#                         bit_error[i, bit_number] += ((chan.ADCDAQ.BIT_ERR_CTR >> (bit_number*4)) & 0x0F)
#                     chan.ADCDAQ.RAMP_ERR_CLEAR = 0
#                     chan.ADCDAQ.RAMP_ERR_CLEAR = 1
#                     # self.logger.info('CH%i: %3i (%08X)' % (
#                       chan.chan_number, chan.ADCDAQ.RAMP_ERR_CTR, chan.ADCDAQ.BIT_ERR_CTR))
#         except KeyboardInterrupt:
#             pass
#
#         for (i, chan) in enumerate(self.chan):
#             self.logger.info('%r: CH%i: %5i word errors, bit errors (7:0) = (%s)' % (
#                   self, chan.chan_number, word_error[i], ','.join('%3i' % e for e in bit_error[i,::-1])))
#         total_word_errors = np.sum(word_error)
#         self.logger.info('%r: There were %i word errors in total' % (self, total_word_errors))
# #        self.set_adc_mode(old_adc_mode)
#         return total_word_errors

    def test_speed(self, n=1000, timeout=0.1):
        """ Test the speed of UDP communications and measure the number of errors.

        The results are printed on stdout.

        Parameters:

            n (int): number of UDP interactions

            timeout: time to wait for a reply before giving up and count the trial as a failed
        """
        old_timeout = self.mb.fpga.get_timeout()
        self.mb.fpga.set_timeout(timeout)
        t0 = time.time()
        errors = 0
        trials = 0
        for i in range(n):
            try:
                trials += 1
                self.get_fpga_cookie()
            except IOError:
                errors += 1
                print('error on transaction #%i' % i)
            except KeyboardInterrupt:
                break
        t1 = time.time()
        self.mb.fpga.set_timeout(old_timeout)
        print('%i read operations performed in %.2f s (%.0f read/s) with %i errors (%0.3f%% errors)' % (
            trials,
            t1 - t0,
            float(n) / (t1 - t0),
            errors,
            float(errors) / float(trials) * 100))


    def get_temperatures(self):
        """ Return the core temperature of the fpga and the mezzanine ADC chips through the FPGA's SYSMON and mezzanine SPI links.

        Returns:

            dict in the format {('target': temperature),...}, with an entry for the FPGA core and each of the ADC chips.

        """
        res = {}
        res['FPGA_core'] = self.SYSMON.temperature()
        for mezz_number, mezz in self.mezzanine.items():
            for (adc_number, adc) in enumerate(mezz.ADC):
                res['FMC%i ADC%i' % (mezz_number - 1, adc_number)] = adc.get_temperature()
        return res


    def init_crossbars(
            self,
            mode=None,
            frames_per_packet=2,
            bin_map=None,

            cb1_lanes=16,
            cb1_bins=64,
            dsmap=list(range(16)),
            cb1_bypass=False,
            cb1_combine_data_flags=0,

            bp_shuffle_bypass=1,

            cb2_lanes=None,
            cb2_bins=1,
            cb2_bypass=False,

            crate_shuffle_bypass=1,

            remap=True,
            chan8_channel_map=list(range(8)),
            send_flags=True):
        """ Initializes the Corner Turn engine in the specified operation mode.

        This method configured the 1st, 2nd and 3rd crossbars (which each can include a remap, frame
        aligner and an array of bin selectors), as well as the PCB and QSFP data shuffle links.


        Parameters:

            mode (str): String describing a predefined corner-turn engine operation mode. If None,
                the crossbars and shuffle enginecan be set manually with the other provided
                parameters. The valid modes are:

                    - 'chan8': Corner-turn engine is mostly bypassed and raw
                      8-bit data from 8 channelizers is sent directly to the 8
                      CT-Engine outputs.

                    - 'chan4': Corner-turn engine is mostly bypassed and raw
                      8-bit data from 16 channelizers is sent directly to the
                      8 CT-Engine outputs.

                    - 'shuffle16 and 'chord16': A corner-turn operation is applied only
                      within the 16 channelizer outputs of this board.

                    - 'shuffle256': The corner-turn operation is applies
                      between 16 channelizers within a board and between the
                      16 boards within a crate using the backplane PCB links.

                    - 'shuffle512': The corner-turn operation is applies
                      between 16 channelizers within a board, between the 16
                      boards within a crate using the backplane PCB links, and
                      between 2 crates using the backplane QSFP links.

                    - 'corr4', 'corr8', 'corr16': The corner-turn engine is configured to feed
                      the internal firmware correlator (only if the firmware
                      was compiled with it).


            frames_per_packet (int): Number of frames to be packaged in a packet. Defaults to 2. Is
                limited by the amount of memory used by the FPGA to store data and flags.

            bin_map (dict): Describes the bin indices to be assigned to the
                outputs of each crossbar.

            cb1_lanes (int): Number of input lanes considered in the
                CROSSBAR1. Defaults to 16. Used only if `mode` = `None`.

            cb1_bins (int): Number of frequency bins to be included in the first
                crossbar. Defaults to 64. Used only if `mode` = `None`.


            dsmap (list) : Destination slot for each of the bin selectors of
               the first crossbar. Defaults to range(16). Used only in modes
               that use the backplane PCB links (``shuffle256``,
               ``shuffle512``).


            cb1_bypass (bool): If True, the first crossbar will be bypassed.
                Used only if `mode` = `None`.

            cb1_combine_data_flags (bool): if True, the data flags at the output of CROSSBAR1 will
                be packed in 32-bit words. Defaults to False, where each data word is associated
                with a data flag word filled with only 16 flag bits, which is needed to allow the
                next crossbar to further split the flags and data as part of the 2nd stage corner
                turn operation.

            cb2_lanes (list of tuple): list of (lower_lane, upper_lane), one for each of the 2
                CROSSBAR2 bin selector, that describe the range of input lanes that shall be read
                out by the bin selectors. Only partial number of lanes is used if the data is to be
                further recombined by the 3rd stage corner turn operation. If None, both bin
                selectros will combine data from all 16 input lanes.


            cb2_bins (int): Number of bins to be included in the bin selectors
                of the second crossbar.



            cb2_bypass (bool): If True the 2nd stage cirner-turn operation (CROSSBAR2) will be
                bypassed. Defaults to `False`


            bp_shuffle_bypass (bool): If `False`, the 16 data lanes form the CROSSBAR1 will be sent
                to other boards through the backplane PCB links, and the CROSSBAR2 will receiver 16
                lanes of data from the other boards (note than lane 0 is in fact always internal).
                If `True`, the 16 data lanes from the CROSSBAR1 will be forwrded directly to the the
                input of CROSSBAR2.


            crate_shuffle_bypass (bool): If False, half of the 8 outputs lanes of CROSSBAR2 will be
                sent to other boards in the other crate through the backplane QSFP links, and half
                will be sent to the CROSSBAR3. CROSSBAR3 will receive half its data from the other
                crate the corresponding board.If True, the 8 output lanes of CROSSBAR2 go directly
                to the 8 input lanes of CROSSBAR3.



            remap (bool). Defaults to True

            chan8_channel_map (list) : channel remapping to be used in `chan8` mode. Defaults to the
                identity map (range(8))

            send_flags (bool): If False, the Scaler and Frame flags will not be sent.

        Returns:

            list of the stream ids at each output lane of the corner turn engine

        Note:
            bin_map is a dict in the format

                {'cb1':cb1_bin_indices, 'cb2':cb2_bin_indices, 'cb3':cb3_bin_indices}

            where

            -   cb1_bin_indices (list of list): List of bin indices that will be
                selected for each bin selector (output lane) of the first
                crossbar.

                There are 16 bin selectors in the first crossbar.
                `cb1_bin_indices` should therefore consist of 16 lists, even
                in modes where only the first 8 bin selectors are used. The
                list is in the order of the destination slot. The list will be
                reordered to account for the backplane connectivity in the
                modes where those links are used..

                If `None`, the default bin selection is used: bins are
                interleaved between each bin selector. `shuffle16` and `corr16` select 128
                bins per bin selector (only the first 8 bin selector will be
                used);and `shuffle256`/`shuffle512` select 64 bins.

                The Bin selectors require 4 clocks to process a selected bin.
                A bin pair (even,odd) is processed on each clock. This means
                that if a bin from a bin pair is selected, no bin can be
                selected from the following 3 bin pairs.

            -  cb2_bin_indices (list of list): List of bin indices that will be
                selected for each bin selector of the second
                crossbar.

                There are 2 bin selectors in the second crossbar.

                If `None`, the default assignments are used. In `shuffle256`,
                all 64 incoming  bins are selected (from half the input lanes,
                to be reassembled by the following crossbar). In `shuffle512`,
                the bins are interleaved between the 2 bin_selectors, with the
                even bins sent to the even crate number. `cb2_bin_indices` is
                not used in other modes.

                The first list is always for the even crate and the second
                list is for the odd crate. In `shuffle512`, The list is reordered automatically
                to account for crate connectivity (i.e the lists for an odd
                crate are swapped).

            -   cb3_bin_indices (list of list): List of bin indices that will be
                selected for each bin selector of the third (and final)
                crossbar.

                There are 8 bin selectors in the third crossbar.

                If `None`, the default assignments are used.  In `shuffle512`,
                the bins are interleaved between the 8 bin_selectors.
                `cb3_bin_indices` is not used in other modes.

        Returns:

            list of stream IDs that will be present at each output (i.e GPU link) of the corner turn
            engine

        """

        cb1 = self.CROSSBAR
        cb2 = self.CROSSBAR2
        cb3 = self.CROSSBAR3

        number_of_cb1_bin_sel = 16
        number_of_cb2_bin_sel = 2
        number_of_cb3_bin_sel = 8

        if bin_map is None:
            bin_map = {}
        cb1_bin_indices = bin_map.get('cb1', None)
        cb2_bin_indices = bin_map.get('cb2', None)
        cb3_bin_indices = bin_map.get('cb3', None)

        cb2_ignore_lane = 0
        cb3_ignore_lane = 0

        if not (cb1_bin_indices is None or len(cb1_bin_indices) == number_of_cb1_bin_sel):
            raise ValueError('1st crossbar bin map should have %i lists of bins. Ir currently has %i' % (
                number_of_cb1_bin_sel, len(cb1_bin_indices)))
        if not (cb2_bin_indices is None or len(cb2_bin_indices) == number_of_cb2_bin_sel):
            raise ValueError('2nd crossbar bin map should have %i lists of bins. Ir currently has %i' % (
                number_of_cb2_bin_sel, len(cb2_bin_indices)))
        if not (cb3_bin_indices is None or len(cb3_bin_indices) == number_of_cb3_bin_sel):
            raise ValueError('3rd crossbar bin map should have %i lists of bins. Ir currently has %i' % (
                number_of_cb3_bin_sel, len(cb3_bin_indices)))

        def get_dest_slot_for_src_lane(src_lane):
            tx = (self.slot, src_lane)  # unique transmitter id (slot, lane)
            dest_slot = self.crate.get_matching_rx(tx)[0]
            return dest_slot

        # def get_src_slot_for_dest_lane(dest_lane):
        #     rx = (self.slot, dest_lane)
        #     src_slot = self.crate.get_matching_tx(rx)[0]
        #     return src_slot

        if frames_per_packet < 1 or frames_per_packet > 4:
            raise ValueError('Number of frames per packet must be between 1 and 4')
        if cb1_lanes in (4, 8, 12, 16):
            cb1_lanes = [(0, cb1_lanes // 4 - 1)] * number_of_cb1_bin_sel
        else:
            raise ValueError('Crossbar 1 number of input lanes must be 4,8,12 or 16')

        if cb2_lanes is None:
            cb2_lanes = ((0, 15), (0, 15))  # Both bin selectors

        # if cb2_lanes % 2:
        #     raise ValueError('Crossbar 2 number of input lanes must be a multiple of 2')

        cb2_timeout_period = None
        cb2_sof_window_stop = None

        if mode == 'chan8':

            if self.CROSSBAR1_TYPE=="URAM":
                self.set_corr_reset(0)
                self.set_ant_reset(0)
                return (0,)

            # Get raw data from the channelizer (all 32-bit sent as is). Only 8 lanes are available to the GPU.
            #############################
            # 1st Crossbar
            #############################
            # In bypass mode. Bin sel 0-15 get every word out of channelizers 0-15
            cb1_bypass = True
            cb1_four_bit = False
            cb1_combine_data_flags = 0
            cb1_bin_select_map = [[]] * number_of_cb1_bin_sel

            #################################
            # Backplane PCB (intra-crate) shuffle
            #################################
            bp_shuffle_bypass = True

            #############################
            # 2nd Crossbar
            #############################
            # Bypassed. Lanes are reordered to select which of the 8 channelizers we want to forward.
            cb2_lane_map = np.hstack((chan8_channel_map, chan8_channel_map))  # Here we could select which 8 inputs we want to stream to the GPU
            cb2_bypass = True
            cb2_bin_select_map = [[]] * number_of_cb2_bin_sel

            #################################
            # Backplane QSFP (crate) shuffle
            #################################
            crate_shuffle_bypass = True

            #############################
            # 3rd Crossbar
            #############################
            # Bypassed. No channel reordering.
            cb3_lane_map = list(range(8))
            cb3_bypass = True
            crate_number = self.crate.crate_number or 0 if self.crate else 0
            cb3_bin_select_map = [[]] * number_of_cb3_bin_sel

            cb3_output_bins = 0  # debug
            cb3_output_words_per_bin = 0  # debug
            cb3_output_data_flags_words_per_bin = 0  # debug
            cb3_output_frame_flags_words_per_frame = 0  # debug

            stream_type = 0
            cb2_ignore_lane = 0
            cb3_ignore_lane = 0


        elif mode == 'chan4':
            """
            In 'chan4' mode, we combine the high nibble of each pair of channelizers, allowing us to
            stream data from ALL channelizers. depending on the configuration of the channelizer, the data can be:

              - 4-bit ADC bit data from all channelizers
              - (4+4) bit FFT value from all channelizers

            """
            #############################
            # 1st Crossbar
            #############################
            # In bypass mode. Bin sel 0 selects 4+4 bit data for all bins of
            # channelizer 0 and 1, etc.  Bin selectors cover all 16
            # channelizers. Bin selectors 8-15 has the same info as bin sel
            # 0-7.
            cb1_bypass = True
            cb1_four_bit = True  # combines high nibbles of pair of input lanes
            cb1_combine_data_flags = 0
            cb1_bin_select_map = []

            #################################
            # Backplane PCB (intra-crate) shuffle
            #################################
            bp_shuffle_bypass = True

            #############################
            # 2nd Crossbar
            #############################
            # Bypassed. No channel reordering. Data from input lanes 8-15 is redundant and is not forwarded.
            cb2_lane_map = list(range(16))  # All information
            cb2_bypass = True
            cb2_bin_select_map = []

            #################################
            # Backplane QSFP (crate) shuffle
            #################################
            crate_shuffle_bypass = True

            #############################
            # 3rd Crossbar
            #############################
            # Bypassed. No channel reordering.
            cb3_lane_map = list(range(8))
            cb3_bin_select_map = []
            cb3_bypass = True
            crate_number = self.crate.crate_number if self.crate else 0
            stream_type = 0

        elif mode in ('shuffle16', 'chord16'):
            """
            In shuffle16 mode, each of the GPU links output data for 128 bins,
            each bins containing the data from 16 channels. The data for each
            channel is a byte, representing the FFT output as a (4+4) bit
            compler number.

            In this mode, each of the 16 bin selector select data from lane
            groups 0-3 (i.e. lanes 0-15). They each select 128 bins, i.e. 1/64
            of the 1024 bins incoming from the channelizer. The first 8 bin
            selectors contain all the data; the last 8 will not be passed by
            the following crossbar.

            There are 4 words per bins (16 channels). The data flags of two
            consecutive bins are combined to build a single data flag word.

            The packet format is as follows (assume one frame per packet):
                - Header: 4 words (16 bytes) for the header
                - Data block: 128 bins x 16 channels = 2056 bytes
                - Data flags block: We combine the data flags, so scaler flags
                  from two consecutive bins are combined into a single 32 bit
                  word ( one bit per channel for each of the 2 bins). This
                  represents 128*16/32=64 words = 256 bytes
                - Frame flags: 16 bit FFT overflow + 16 bit ADC overflow flags
                  per frame. This is 1 word = 4 bytes.
                - Packet flags: 32 bit word (4 bytes)
                - Total: 2328 bytes

            """

            #############################
            # 1st Crossbar
            #############################
            # Selects data from all channelizers, and spread the bins betweeen
            # the first 8 bin selectors so the data can go through the
            # following bypassed crossbars.
            cb1_bypass = False
            cb1_four_bit = True
            # BS0 grabs data from FIFO 0-1 (lanes 0-7), BS1 from FIFO 2-3
            # (lanes 8-15), repeat... We capture 2 words per bin in 2 clocks,
            # bins are separated by 2 clocks, so we have time to empty the
            # FIFO
            # set the first and last inlut lane group
            cb1_lanes = [(0, 3)] * number_of_cb1_bin_sel
            # Set the bins selected by each bin selector
            if cb1_bin_indices:
                cb1_bin_select_map = cb1_bin_indices
                cb1_bins = len(cb1_bin_indices[0])
            else:  # Use default
                cb1_bins = 128
                cb1_bin_spacing = 1024 // cb1_bins  # = 8 bins, or 4 clocks
                cb1_bin_select_map = [
                    np.arange(cb1_bins) * cb1_bin_spacing + (i % cb1_bin_spacing)
                    for i in range(number_of_cb1_bin_sel)]
            cb1_combine_data_flags = 1
            cb1_output_words_per_bin = 4
            cb1_output_bins = cb1_bins
            cb1_input_lanes_per_output_lane = 16
            cb1_output_data_flags_words_per_bin = (cb1_output_words_per_bin * 4.0) / (32 if cb1_combine_data_flags else 16)  # This is 0.5 if we combine_flags
            cb1_output_frame_flags_words_per_frame = 1


            #################################
            # Backplane PCB (intra-crate) shuffle
            #################################
            bp_shuffle_bypass = True

            #############################
            # 2nd Crossbar
            #############################
            # Bypassed. No channel reordering.

            cb2_lane_map = list(range(16))
            cb2_bypass = True
            cb2_input_words_per_bin = cb1_output_words_per_bin
            cb2_input_data_flags_words_per_bin = cb1_output_data_flags_words_per_bin
            cb2_input_frame_flags_words_per_frame = cb1_output_frame_flags_words_per_frame
            cb2_input_bins = cb1_bins
            # cb2_lanes : Not applicable because of bypass
            # cb2_bins : Not applicable because of bypass
            # cb2_bin_spacing : Not applicable because of bypass
            # cb2_bin_select_map : Not applicable because of bypass
            cb2_output_words_per_bin = cb2_input_words_per_bin
            cb2_output_bins = cb2_bins
            cb2_input_lanes_per_output_lane = cb1_input_lanes_per_output_lane
            cb2_output_data_flags_words_per_bin = cb2_input_data_flags_words_per_bin
            cb2_output_frame_flags_words_per_frame = cb2_input_frame_flags_words_per_frame

            crate_number = self.crate.crate_number or 0 if self.crate else 0
            stream_type = 1

            #################################
            # Backplane QSFP (crate) shuffle
            #################################
            crate_shuffle_bypass = True


            #############################
            # 3rd Crossbar
            #############################
            # Bypassed. No channel reordering.

            cb3_lane_map = list(range(8))
            cb3_bypass = True
            cb3_output_words_per_bin = cb2_input_words_per_bin
            cb3_output_bins = cb2_input_bins
            cb3_output_data_flags_words_per_bin = cb2_output_data_flags_words_per_bin
            cb3_output_frame_flags_words_per_frame = cb2_output_frame_flags_words_per_frame

        elif mode == 'shuffle128':
            """ Configures the corner-turn engine for a 8-board shuffle.

            Boards are assumed to be in slots 1-8 of the crates (slots 0-7).

            In this mode, crossbar1 merges 16 channelizer outputs into 8
            output lanes, with 128 bins per lane, like 'shuffle16'. However,
            we do not bypass the backplane PCB shuffle. The lanes that end up
            in one of the 8 populated slots are used. The backplane data rate is close to the limit (7.5 Gbps). We cannot send the flags due to bandwidth limitations.

            Crossbar2 remaps the 8 received lanes only. Crossbar 2 bin selectors doubles the data rate, so we can't use them and they are bypassed.

            Crossbar3 operates as in shuffle512: data from 8 input lanes is sent to the 8 output GPU lanes. We have 16 bins per lane.
            """
            if not self.slot:
                raise RuntimeError('The slot number is unknown. Cannot route the appropriate bins to the target boards in the same crate')

            #############################
            # 1st Crossbar
            #############################
            # Selects data from all channelizers, and spread the bins betweeen
            # the first 8 bin selectors so the data can go through the
            # following bypassed crossbars.
            cb1_bypass = False
            cb1_four_bit = True
            # Bin selectors grab data from all lanes
            cb1_lanes = [(0, 3)] * number_of_cb1_bin_sel

            # Set the bins selected by each bin selector
            if cb1_bin_indices:
                cb1_bin_select_map = cb1_bin_indices
                cb1_bins = len(cb1_bin_indices[0])
            else:  # Use default
                cb1_bins = 128
                cb1_bin_spacing = 1024 // cb1_bins  # = 8 bins, or 4 clocks

                # *** JFC debug
                cb1_bin_spacing = 8  # Normally  8 bins, which is the minimum
                cb1_bins = 1024 // cb1_bin_spacing


                cb1_bin_select_map = [
                    np.arange(cb1_bins) * cb1_bin_spacing + (i % cb1_bin_spacing)
                    for i in range(number_of_cb1_bin_sel)]

            # Reorder cb1_bin_select_map with the knowledge of the backplane
            # PCB links connectivity so slot 0 gets cb1_bin_select_map[0],
            # slot 1 gets cb1_bin_select_map[1] etc.
            cb1_bin_select_map = [
                cb1_bin_select_map[dsmap[get_dest_slot_for_src_lane(i) - 1]]
                for i in range(16)]

            cb1_combine_data_flags = 0 # we cannot combine the flags of two bins because we further bin-select them in crossbar 3
            send_flags = False  # There is not enough bandwidth on the backplane to send uncombined flags
            cb1_output_words_per_bin = 4
            cb1_output_bins = cb1_bins
            cb1_input_lanes_per_output_lane = 16
            cb1_output_data_flags_words_per_bin = send_flags * (cb1_output_words_per_bin * 4.0) / (32 if cb1_combine_data_flags else 16)  # This is 0.5 if we combine_flags
            cb1_output_frame_flags_words_per_frame = 1 * send_flags


            #################################
            # Backplane PCB (intra-crate) shuffle
            #################################
            bp_shuffle_bypass = False

            #############################
            # 2nd Crossbar
            #############################
            # Bypassed. No channel reordering.

            # CB2 packet aligner
            cb2_timeout_period = 0
            cb2_sof_window_stop = 55
            # CB2 REMAP
            # get a list that indicates which input lanes to get data from so we get it in increasing order of origin slot number
            # eg, if we are in slot 2 (zero-based)
            # (tx_slot => rx_lane) = (0=>1), (1->3), 2->0, 3->2, we have a map of [1, 3, 0, 2]
            # If slot 3 is not sending data,  we should discard  data from lanes lane_map[3] = 2
            cb2_lane_map = self.CROSSBAR2.compute_bp_shuffle_lane_map()
            print(f'slot {self.slot-1}/15: CB2 lane map: {cb2_lane_map}') #[ 0  8  6 14  5  4 15  7  3  2 11  1 13 12  9 10]
            cb2_ignore_lane = sum((1<<lane) for lane in cb2_lane_map[8:16])
            cb2_lane_map[8:16] = [0,0,0,0,0,0,0,0]  # hack to make sure all the bin sel input lanes have valid data
            print(f'slot {self.slot-1}/15: CB2 lane map: {cb2_lane_map}') #[ 0  8  6 14  5  4 15  7  3  2 11  1 13 12  9 10]
            cb2_bypass = True

            cb2_input_words_per_bin = cb1_output_words_per_bin
            cb2_input_data_flags_words_per_bin = cb1_output_data_flags_words_per_bin
            cb2_input_frame_flags_words_per_frame = cb1_output_frame_flags_words_per_frame
            cb2_input_bins = cb1_output_bins
            # cb2_lanes : Not applicable because of bypass
            # cb2_bins : Not applicable because of bypass
            # cb2_bin_spacing : Not applicable because of bypass
            # cb2_bin_select_map : Not applicable because of bypass
            cb2_output_words_per_bin = cb2_input_words_per_bin
            cb2_output_bins = cb2_input_bins
            cb2_input_lanes_per_output_lane = cb1_input_lanes_per_output_lane
            cb2_output_data_flags_words_per_bin = cb2_input_data_flags_words_per_bin
            cb2_output_frame_flags_words_per_frame = cb2_input_frame_flags_words_per_frame

            crate_number = self.crate.crate_number or 0 if self.crate else 0
            stream_type = 1

            #################################
            # Backplane QSFP (crate) shuffle
            #################################
            crate_shuffle_bypass = True


            #############################
            # 3rd Crossbar
            #############################
            # Crossbar 2 just passed the 8 lanes from the 8 boards, reordered by slot number origin
            # Crossbar 3 can finish the job, i.e. channels are spread over its
            # 8 input links.

            # Remap input lanes so data is selected in proper channel order
            cb3_lane_map = [0, 1, 2, 3, 4, 5, 6, 7]
            cb3_ignore_lane = 0
            cb3_bypass = False
            cb3_input_words_per_bin = cb2_output_words_per_bin
            cb3_input_data_flags_words_per_bin = cb2_output_data_flags_words_per_bin
            cb3_input_frame_flags_words_per_frame = cb2_output_frame_flags_words_per_frame
            cb3_input_bins = cb2_output_bins
            cb3_lanes = [(0, 7)] * number_of_cb3_bin_sel
            cb3_input_lanes_per_output_lane = cb3_lanes[0][1] - cb3_lanes[0][0] + 1  # 8 input lanes per bin sel output
            cb3_combine_data_flags = False  # hardwired to False in crossbar 3

            # Select the bins to be assigned to each bin selector output.
            if cb3_bin_indices is not None:
                cb3_bins = len(cb3_bin_indices[0])
                cb3_bin_select_map = cb3_bin_indices
            else:
                # Default: we select 1/8th of the incoming bins from all the input lanes
                # We merge data from 8 full bandwidth input lanes, so we select 1/8th of the bins on each output lane
                cb3_bins = cb3_input_bins // 8
                cb3_bin_spacing = 8  # use maximum possible number so we minimize FIFO usage
                cb3_bin_select_map = [
                    np.arange(cb3_bins) * cb3_bin_spacing + i
                    for i in range(number_of_cb3_bin_sel)]

            cb3_output_words_per_bin = cb3_input_words_per_bin * 8
            cb3_output_bins = cb3_bins
            cb3_output_data_flags_words_per_bin = (
                cb3_input_data_flags_words_per_bin * cb3_input_lanes_per_output_lane
                // (2 if cb3_combine_data_flags else 1))
            cb3_output_frame_flags_words_per_frame = (
                cb3_input_frame_flags_words_per_frame * cb3_input_lanes_per_output_lane)

        elif mode == 'shuffle256':
            if not self.slot:
                raise RuntimeError('The slot number is unknown. Cannot route the appropriate bins to the target boards in the same crate')

            #############################
            # 1st Crossbar
            #############################
            # Selects data from all channelizers, and spread the bins betweeen
            # all 16 bin selectors to spread the data across 16 boards in a crate.
            cb1_bypass = False
            cb1_four_bit = True
            cb1_combine_data_flags = 0
            cb1_lanes = [(0, 3)] * number_of_cb1_bin_sel
            # Select the bins to be assigned to each bin selector output.
            # Here, we assume that the  list is in the order of the
            # destination slot.
            if cb1_bin_indices is not None:
                cb1_bin_select_map = cb1_bin_indices
                cb1_bins = len(cb1_bin_indices[0])
            else:
                cb1_bins = 64
                cb1_bin_spacing = 1024 // cb1_bins
                cb1_bin_select_map = [
                    np.arange(cb1_bins) * cb1_bin_spacing + i
                    for i in range(number_of_cb1_bin_sel)]
            # Reorder cb1_bin_select_map with the knowledge of the backplane
            # PCB links connectivity so slot 0 gets cb1_bin_select_map[0],
            # slot 1 gets cb1_bin_select_map[1] etc.
            cb1_bin_select_map = [
                cb1_bin_select_map[dsmap[get_dest_slot_for_src_lane(i) - 1]]
                for i in range(16)]
            cb1_output_words_per_bin = 16 // 4
            cb1_output_bins = cb1_bins

            #################################
            # Backplane PCB (intra-crate) shuffle
            #################################
            bp_shuffle_bypass = False

            #############################
            # 2nd Crossbar
            #############################
            #
            # CB2 has 2 BIN_SEL
            # Each BS captures data from 16 input lanes and has 4 outputs.
            # Each output covers gathers data from 4 input lanes (sublanes 0-3).
            #   Output 0: Sublanes 0-3 = Input Lanes 0-3
            #   Output 1: Sublanes 0-3 = Input Lanes 4-7
            #   Output 2: Sublanes 0-3 = Input Lanes 8-11
            #   Output 3: Sublanes 0-3 = Input Lanes 12-15
            #
            # In this config, we will bypass the crate_shuffle. We therefore want all outputs to
            # output the same bins. So BS0 gets data from half of its sublanes, and BS1 gets data
            # from the other half.
            #
            # CB2 Output Lane 0: BS0.0: all 64 bins from sublanes 0-1 (Input lanes 0-1   = CH0-31)
            # CB2 Output Lane 1: BS0.1: all 64 bins from sublanes 0-1 (Input lanes 4-5   = CH64-95)
            # CB2 Output Lane 2: BS0.2: all 64 bins from sublanes 0-1 (Input lanes 8-9   = CH128-159)
            # CB2 Output Lane 3: BS0.3: all 64 bins from sublanes 0-1 (Input lanes 12-13 = CH192-223)
            # CB2 Output Lane 4: BS1.0: all 64 bins from sublanes 2-3 (Input lanes 2-3   = CH32-63)
            # CB2 Output Lane 5: BS1.1: all 64 bins from sublanes 2-3 (Input lanes 6-7   = CH96-127)
            # CB2 Output Lane 6: BS1.2: all 64 bins from sublanes 2-3 (Input lanes 10-11 = CH160-191)
            # CB2 Output Lane 7: BS1.3: all 64 bins from sublanes 2-3 (Input lanes 14-15 = CH224-255)
            # Crate shuffle is bypassed.
            # CB3 inputs are therefore identical to CB2 output
            # CB3 lane map selects data in the order: [BS0.0, BS1.0, BS0.1, BS1.1 ...]
            # CB3 remapped BIN_SEL inputs are
            #    CB3 Input Lane 0: 64 bins CH0-31
            #    CB3 Input Lane 1: 64 bins CH32-63
            #    CB3 Input Lane 2: 64 bins CH64-95
            #    CB3 Input Lane 3: 64 bins CH96-127
            #    CB3 Input Lane 4: 64 bins CH128-159
            #    CB3 Input Lane 5: 64 bins CH160-191
            #    CB3 Input Lane 6: 64 bins CH192-223
            #    CB3 Input Lane 7: 64 bins CH224-255
            # CB3 outputs are:
            #    CB3 Output Lane 0: BS0.0: 8 bins (0,8...)  from sublanes 0-7 (Input lanes 0-7 =CH0-511)
            #    CB3 Output Lane 1: BS0.1: 8 bins (1,9...)  from sublanes 0-7 (Input lanes 0-7 =CH0-511)
            #    CB3 Output Lane 2: BS0.2: 8 bins (2,10...) from sublanes 0-7 (Input lanes 0-7 =CH0-511)
            #    CB3 Output Lane 3: BS0.3: 8 bins (3,11...) from sublanes 0-7 (Input lanes 0-7 =CH0-511)
            #    CB3 Output Lane 4: BS1.0: 8 bins (4,12...) from sublanes 0-7 (Input lanes 0-7 =CH0-511)
            #    CB3 Output Lane 5: BS1.1: 8 bins (5,13...) from sublanes 0-7 (Input lanes 0-7 =CH0-511)
            #    CB3 Output Lane 6: BS1.2: 8 bins (6,14...) from sublanes 0-7 (Input lanes 0-7 =CH0-511)
            #    CB3 Output Lane 7: BS1.3: 8 bins (7,15...) from sublanes 0-7 (Input lanes 0-7 =CH0-511)

            # CB2 ALIGN (JM changed cb2_sof_window_stop from 70 to 50 which the default value of CROSSBAR.SOF_WINDOW_STOP in shuffle_crossbar module. When 70, get errors when establishing connection with gpu nodes, reporting wrong packet size). JFC: CHanged to 55 during backplane shuffle debugging
            cb2_timeout_period = 0
            cb2_sof_window_stop = 55
            # CB2 REMAP
            cb2_lane_map = self.CROSSBAR2.compute_bp_shuffle_lane_map()
            cb2_bypass = False
            # CB2 BIN_SEL
            cb2_input_words_per_bin = cb1_output_words_per_bin
            cb2_input_bins = cb1_output_bins
            cb2_input_data_flags_words_per_bin = 1
            cb2_input_frame_flags_words_per_frame = 1

            # BS0 selects sublanes 0-1, i.e. its 4 outputs gather data from lanes 0-1, 4-5, 8-9, and 12-13
            # BS1 selects sublanes 2-3 (lanes 2-3, 6-7, 10-12 and 14-15 )
            cb2_lanes = ((0, 1), (2, 3))
            cb2_combine_data_flags = True  # hardwired to True in crossbar 2
            cb2_input_lanes_per_output_lane = cb2_lanes[0][1] - cb2_lanes[0][0] + 1  # 2 input lanes per output

            # Select the bins to be assigned to each bin selector output.
            if cb2_bin_indices is not None:
                cb2_bins = len(cb2_bin_indices[0])
                cb2_bin_select_map = cb2_bin_indices
            else:
                # Default: we select all 64 incoming bins, but from half the input lanes
                cb2_bins = 64
                cb2_bin_spacing = 1
                cb2_bin_select_map = [np.arange(cb2_bins) * cb2_bin_spacing
                                      for i in range(number_of_cb2_bin_sel)]

            cb2_output_words_per_bin = cb2_input_words_per_bin * cb2_input_lanes_per_output_lane

            cb2_output_data_flags_words_per_bin = (
                cb2_input_data_flags_words_per_bin * cb2_input_lanes_per_output_lane
                // (2 if cb2_combine_data_flags else 1))

            cb2_output_frame_flags_words_per_frame = (
                cb2_input_frame_flags_words_per_frame * cb2_input_lanes_per_output_lane)

            cb2_output_bins = cb2_bins
            crate_number = (self.crate.crate_number or 0) if self.crate else 0
            stream_type = 2

            #################################
            # Backplane QSFP (crate) shuffle
            #################################
            crate_shuffle_bypass = True

            #############################
            # 3rd Crossbar
            #############################
            # Crossbar 2 partially combined the channels in a way that
            # Crossbar 3 can finish the job, i.e. channels are spread over its
            # 8 input links.

            # Remap input lanes so data is selected in proper channel order In
            # shuffle256, input lanes 0-3 are from BS2 input anes 0-1, 4-5,
            # 8-9, and 12-13, and input lanes 4=7 are from lanes 2-3, 6-7,
            # 10-12 and 14-15.
            cb3_lane_map = [0, 4, 1, 5, 2, 6, 3, 7]
            cb3_bypass = False
            cb3_input_words_per_bin = cb2_output_words_per_bin
            cb3_input_data_flags_words_per_bin = cb2_output_data_flags_words_per_bin
            cb3_input_frame_flags_words_per_frame = cb2_output_frame_flags_words_per_frame
            cb3_input_bins = cb2_output_bins
            cb3_lanes = [(0, 7)] * number_of_cb3_bin_sel
            cb3_input_lanes_per_output_lane = cb3_lanes[0][1] - cb3_lanes[0][0] + 1  # 8 input lanes per bin sel output
            cb3_combine_data_flags = False  # hardwired to False in crossbar 3

            # Select the bins to be assigned to each bin selector output.
            if cb3_bin_indices is not None:
                cb3_bins = len(cb3_bin_indices[0])
                cb3_bin_select_map = cb3_bin_indices
            else:
                # Default: we select 1/8th of the incoming bins from all the input lanes
                # We merge data from 8 full bandwidth input lanes, so we select 1/8th of the bins on each output lane
                cb3_bins = 8
                cb3_bin_spacing = 8  # use maximum possible number so we minimize FIFO usage
                cb3_bin_select_map = [
                    np.arange(cb3_bins) * cb3_bin_spacing + i
                    for i in range(number_of_cb3_bin_sel)]

            cb3_output_words_per_bin = cb3_input_words_per_bin * 8
            cb3_output_bins = cb3_bins
            cb3_output_data_flags_words_per_bin = (
                cb3_input_data_flags_words_per_bin * cb3_input_lanes_per_output_lane
                // (2 if cb3_combine_data_flags else 1))
            cb3_output_frame_flags_words_per_frame = (
                cb3_input_frame_flags_words_per_frame * cb3_input_lanes_per_output_lane)

        elif mode == 'shuffle512':
            """
            Like shuffle256, but adds a corner-turn operation between pair of crates using the
            backplane QSFP connections between them.
            """
            if not self.slot:
                raise RuntimeError('The slot number is unknown. Cannot route the appropriate bins '
                                   'to the target boards in the same crate')

            #############################
            # 1st Crossbar
            #############################
            # Selects data from all channelizers, and spread the bins between
            # all 16 bin selectors to spread the data across 16 boards in a crate.
            cb1_bypass = False
            cb1_four_bit = True
            cb1_combine_data_flags = 0
            # get data from channel group 0 - 3. Each channel group combines data from 4 channelizers (in 4 bit mode)
            cb1_lanes = [(0, 3)] * number_of_cb1_bin_sel
            # Select the bins to be assigned to each bin selector output.
            # Here, we assume that the  list is in the order of the
            # destination slot.
            if cb1_bin_indices is not None:
                cb1_bin_select_map = cb1_bin_indices
                cb1_bins = len(cb1_bin_indices[0])
            else:
                cb1_bins = 64
                cb1_bin_spacing = 1024 // cb1_bins
                cb1_bin_select_map = [
                    np.arange(cb1_bins) * cb1_bin_spacing + i
                    for i in range(number_of_cb1_bin_sel)]
            # Reorder cb1_bin_select_map with the knowledge of the backplane
            # PCB links connectivity so slot 0 gets cb1_bin_select_map[0],
            # slot 1 gets cb1_bin_select_map[1] etc.
            cb1_bin_select_map = [
                cb1_bin_select_map[dsmap[get_dest_slot_for_src_lane(i) - 1]]
                for i in range(16)]

            # Output packet geometry
            cb1_output_words_per_bin = 16 // 4
            cb1_output_bins = cb1_bins

            #################################
            # Backplane PCB (intra-crate) shuffle
            #################################
            bp_shuffle_bypass = False

            # In this config, we do not bypass the crate_shuffle. Half the bins are sent out, and we
            # receive bins that are the same as those of the direct lanes.
            #
            # We therefore want BS0 to get half the bins from all input lanes, and BS1 gets the
            # other half of the bins also from all input lanes.
            #
            # CB2 Output lanes are:
            #    CB2 Output Lane 0: BS0.0: 32 even bins from sublanes 0-3 (Input lanes 0-3   = CH0-63)
            #    CB2 Output Lane 1: BS0.1: 32 even bins from sublanes 0-3 (Input lanes 4-7   = CH64-127)
            #    CB2 Output Lane 2: BS0.2: 32 even bins from sublanes 0-3 (Input lanes 8-11  = CH128-191)
            #    CB2 Output Lane 3: BS0.3: 32 even bins from sublanes 0-3 (Input lanes 12-15 = CH192-255)
            #    CB2 Output Lane 4: BS1.0: 32 odd  bins from sublanes 0-3 (Input lanes 0-3   = CH0-63)
            #    CB2 Output Lane 5: BS1.1: 32 odd  bins from sublanes 0-3 (Input lanes 4-7   = CH64-127)
            #    CB2 Output Lane 6: BS1.2: 32 odd  bins from sublanes 0-3 (Input lanes 8-11  = CH128-191)
            #    CB2 Output Lane 7: BS1.3: 32 odd  bins from sublanes 0-3 (Input lanes 12-15 = CH192-255)
            # Crate shuffle is *not* bypassed
            # CB3 inputs are therefore:
            #    CB3 Input Lane 0: 32 even bins CH0-63
            #    CB3 Input Lane 1: 32 even bins CH64-127
            #    CB3 Input Lane 2: 32 even bins CH128-191
            #    CB3 Input Lane 3: 32 even bins CH192-255
            #    CB3 Input Lane 4: 32 even bins CH256-319
            #    CB3 Input Lane 5: 32 even bins CH320-383
            #    CB3 Input Lane 6: 32 even bins CH384-447
            #    CB3 Input Lane 7: 32 even bins CH448-511
            # CB3 lane map selects data in the input lane order: [0, 1, 2, 3 ... 7]
            # CB3 BIN sel inputs are therefore identical to CB3 inputs
            # CB3 outputs are:
            #    CB3 Output Lane 0: BS0.0: 4 bins (0,8...)  from sublanes 0-7 (Input lanes 0-7 =CH0-511)
            #    CB3 Output Lane 1: BS0.1: 4 bins (1,9...)  from sublanes 0-7 (Input lanes 0-7 =CH0-511)
            #    CB3 Output Lane 2: BS0.2: 4 bins (2,10...) from sublanes 0-7 (Input lanes 0-7 =CH0-511)
            #    CB3 Output Lane 3: BS0.3: 4 bins (3,11...) from sublanes 0-7 (Input lanes 0-7 =CH0-511)
            #    CB3 Output Lane 4: BS1.0: 4 bins (4,12...) from sublanes 0-7 (Input lanes 0-7 =CH0-511)
            #    CB3 Output Lane 5: BS1.1: 4 bins (5,13...) from sublanes 0-7 (Input lanes 0-7 =CH0-511)
            #    CB3 Output Lane 6: BS1.2: 4 bins (6,14...) from sublanes 0-7 (Input lanes 0-7 =CH0-511)
            #    CB3 Output Lane 7: BS1.3: 4 bins (7,15...) from sublanes 0-7 (Input lanes 0-7 =CH0-511)

            #############################
            # 2nd Crossbar
            #############################

            # CB2 ALIGN
            # cb2_timeout_period = 0

            crate_number = (self.crate.crate_number or 0) if self.crate else 0
            stream_type = 3  # crossbar 3-level data

            # Input packet geometry

            cb2_input_data_flags_words_per_bin = 1
            cb2_input_words_per_bin = cb1_output_words_per_bin
            cb2_input_bins = cb1_output_bins
            cb2_input_frame_flags_words_per_frame = 1

            # Configuration
            cb2_sof_window_stop = 55
            cb2_bypass = False
            # CB2 REMAP
            cb2_lane_map = self.CROSSBAR2.compute_bp_shuffle_lane_map()
            # CB@ BIN_SEL
            cb2_lanes = [(0, 3), (0, 3)]  # Every output of both bin sels get data from all the 4 sublanes they get.
            cb2_input_lanes_per_output_lane = cb2_lanes[0][1] - cb2_lanes[0][0] + 1  # 4 input lanes per output
            # Select the bins to be assigned to each bin selector output.
            if cb2_bin_indices is not None:
                cb2_bin_select_map = cb2_bin_indices
                cb2_bins = len(cb2_bin_indices[0])
            else:
                # Default: we select half the bins (from all input lanes)
                cb2_bins = cb1_output_bins // number_of_cb2_bin_sel  # 64/2 = 32
                cb2_bin_spacing = number_of_cb2_bin_sel  # 2
                cb2_bin_select_map = [
                    np.arange(cb2_bins) * cb2_bin_spacing + i
                    for i in range(number_of_cb2_bin_sel)]
            # swap bin selection list on odd crates so the bins on the first
            # list are sent to the other (even) crate
            if crate_number & 1:
                cb2_bin_select_map = cb2_bin_select_map[::-1]

            cb2_combine_data_flags = True  # hardwired to True in crossbar 2

            # Output packet geometry

            cb2_output_words_per_bin = cb2_input_words_per_bin * cb2_input_lanes_per_output_lane
            cb2_output_bins = cb2_bins
            cb2_output_data_flags_words_per_bin = (
                cb2_input_data_flags_words_per_bin * cb2_input_lanes_per_output_lane //
                (2 if cb2_combine_data_flags else 1))
            cb2_output_frame_flags_words_per_frame = (
                cb2_input_frame_flags_words_per_frame * cb2_input_lanes_per_output_lane)
            # print("cb2_output frame flags words=%i, input frame flags words=%i, input_lanes=%i" % (
            #   cb2_output_frame_flags_words_per_frame,
            #   cb2_input_frame_flags_words_per_frame,
            #   cb2_input_lanes_per_output_lane))

            #################################
            # Backplane QSFP (crate) shuffle
            #################################
            crate_shuffle_bypass = False
            # crate_shuffle_bypass = True #***debug

            #############################
            # 3rd Crossbar
            #############################
            # Crossbar gathers the data from the 2 crates.

            # Input packet geometry
            cb3_input_words_per_bin = cb2_output_words_per_bin
            cb3_input_data_flags_words_per_bin = cb2_output_data_flags_words_per_bin
            cb3_input_frame_flags_words_per_frame = cb2_output_frame_flags_words_per_frame
            cb3_input_bins = cb2_output_bins

            # Configuration

            # Input lane remapping to keep the channel number consistent
            # whether we are on an odd or even crate number.
            #
            # For the even crate, input lane 0-3 contains the local even bins
            # for the low channels, and lane 4-7 get the even bins from the
            # other crate (high channels). For the odd crate, input lane  0-3
            # are the odd bins from the local high channels, and lanes 3-7 are
            # the odd bins from the other crate low channels.
            #
            # JM: modified this line to have consistent input ordering between
            # pairs of crates. Before this change this line was just range(8)
            cb3_lane_map = [4, 5, 6, 7, 0, 1, 2, 3] if (crate_number & 1) \
                else [0, 1, 2, 3, 4, 5, 6, 7]
            cb3_bypass = False
            cb3_lanes = [(0, 7)] * number_of_cb3_bin_sel
            cb3_input_lanes_per_output_lane = cb3_lanes[0][1] - cb3_lanes[0][0] + 1  # 8 input lanes per bin sel output

            # Select the bins to be assigned to each bin selector output.
            if cb3_bin_indices is not None:
                cb3_bins = len(cb3_bin_indices[0])
                cb3_bin_select_map = cb3_bin_indices
            else:
                # Default: we select 1/8th of the incoming bins from all the input lanes
                cb3_bins = cb2_output_bins // number_of_cb3_bin_sel  # 32/8 = 4
                cb3_bin_spacing = number_of_cb3_bin_sel  # = 8
                cb3_bin_select_map = [
                    np.arange(cb3_bins) * cb3_bin_spacing + i
                    for i in range(number_of_cb3_bin_sel)]
            cb3_combine_data_flags = False  # hardwired to False in crossbar 3

            # Output packet geometry
            cb3_output_words_per_bin = cb3_input_words_per_bin * cb3_input_lanes_per_output_lane
            cb3_output_bins = cb3_bins
            cb3_output_data_flags_words_per_bin = (
                cb3_input_data_flags_words_per_bin * cb3_input_lanes_per_output_lane
                // (2 if cb3_combine_data_flags else 1))
            cb3_output_frame_flags_words_per_frame = (
                cb3_input_frame_flags_words_per_frame * cb3_input_lanes_per_output_lane)

        elif mode in ('corr16', ):
            """
            Implement the corner-turn operation for the 16-channel firmware correlator embedded in
            the same FPGA. in this mode, we simply enable the 1st crossbar. The 2nd and 3rd
            crossbars are not present in the firmware.
            """
            #############################
            # 1st Crossbar
            #############################
            # Reorders the data from the 16 local channelizers

            number_of_cb1_bin_sel = 8
            cb1_bypass = False
            cb1_four_bit = True
            # BS0 grabs data from FIFO 0-1 (lanes 0-7), BS1 from FIFO 2-3
            # (lanes 8-15), repeat... We capture 2 words per bin in 2 clocks,
            # bins are separated by 2 clocks, so we have time to empty the
            # FIFO
            cb1_lanes = [(0, 3)] * number_of_cb1_bin_sel
            cb1_combine_data_flags = 1
            # Select the bins to be assigned to each bin selector output.
            if cb1_bin_indices:
                cb1_bin_select_map = cb1_bin_indices
                cb1_bins = len(cb1_bin_indices[0])
            else:
                cb1_bins = 128
                cb1_bin_spacing = 1024 // cb1_bins  # = 8 = 4 clocks
                cb1_bin_select_map = [
                    np.arange(cb1_bins) * cb1_bin_spacing + (i % cb1_bin_spacing)
                    for i in range(number_of_cb1_bin_sel)]
            cb1_output_words_per_bin = 4
            cb1_output_bins = cb1_bins

            #################################
            # Backplane PCB (intra-crate) shuffle
            #################################
            # Not implemented in the firmware correlator

            #################################
            # Backplane QSFP (crate) shuffle
            #################################
            # Not implemented in the firmware correlator

            #############################
            # 2nd and 3rd Crossbar
            #############################
            # Not implemented in the firmware correlator
            # We define default values to prevent the packet size computation to fail.
            cb2_bypass = True
            cb3_bypass = True
            cb3_output_bins = cb1_bins
            cb3_output_words_per_bin = 0  # To be updated
            cb3_output_data_flags_words_per_bin = 0  # To be updated
            cb3_output_frame_flags_words_per_frame = 0  # To be updated
            stream_type = 0  # not used, as the shuffled packets are correlated never get out of the FPGA
            crate_number = 0  # idem

        elif mode == 'corr4':
            """
            Implement the corner-turn operation for the 16-channel firmware correlator embedded in
            the same FPGA. in this mode, we simply enable the 1st crossbar. The 2nd and 3rd
            crossbars are not present in the firmware.
            """
            #############################
            # 1st Crossbar
            #############################
            # Reorders the data from the 16 local channelizers

            number_of_cb1_bin_sel = 8
            cb1_bypass = False
            cb1_four_bit = True
            send_flags = False
            # BS0 grabs data from FIFO 0-1 (lanes 0-7), BS1 from FIFO 2-3
            # (lanes 8-15), repeat... We capture 2 words per bin in 2 clocks,
            # bins are separated by 2 clocks, so we have time to empty the
            # FIFO
            cb1_lanes = [(0, 0)] * number_of_cb1_bin_sel
            cb1_combine_data_flags = 1
            # Select the bins to be assigned to each bin selector output.
            if cb1_bin_indices:
                cb1_bin_select_map = cb1_bin_indices
                cb1_bins = len(cb1_bin_indices[0])
            else:
                cb1_bins = 256
                cb1_bin_spacing = 1024 // cb1_bins  # = 8 = 4 clocks
                cb1_bin_select_map = [
                    np.arange(cb1_bins) * cb1_bin_spacing + (i % cb1_bin_spacing)
                    for i in range(number_of_cb1_bin_sel)]
            cb1_output_words_per_bin = 1
            cb1_output_bins = cb1_bins

            #################################
            # Backplane PCB (intra-crate) shuffle
            #################################
            # Not implemented in the firmware correlator

            #################################
            # Backplane QSFP (crate) shuffle
            #################################
            # Not implemented in the firmware correlator

            #############################
            # 2nd and 3rd Crossbar
            #############################
            # Not implemented in the firmware correlator
            # We define default values to prevent the packet size computation to fail.
            cb2_bypass = True
            cb3_bypass = True
            cb3_output_bins = cb1_bins
            cb3_output_words_per_bin = 0  # To be updated
            cb3_output_data_flags_words_per_bin = 0  # To be updated
            cb3_output_frame_flags_words_per_frame = 0  # To be updated
            stream_type = 0  # not used, as the shuffled packets are correlated never get out of the FPGA
            crate_number = 0  # idem

        elif mode == 'corr8':
            """
            Implement the corner-turn operation for the 16-channel firmware correlator embedded in
            the same FPGA. in this mode, we simply enable the 1st crossbar. The 2nd and 3rd
            crossbars are not present in the firmware.
            """
            if self.CROSSBAR1_TYPE=="URAM": # hack
                self.set_corr_reset(0)
                self.set_ant_reset(0)
                return 0

            raise RuntimeError('Unsupported mode corr8 with current firmware configuration')

        elif mode is None:  # Manual config
            cb1_four_bit = True

            # cb1_bypass = False

            # Select the bins to be assigned to each bin selector output.
            if cb1_bin_indices:
                cb1_bin_select_map = cb1_bin_indices
            else:
                cb1_bin_spacing = 1024 // cb1_bins
                cb1_bin_select_map = [
                    (np.arange(cb1_bins) * cb1_bin_spacing + i) % 1024
                    for i in range(number_of_cb1_bin_sel)]

            #################################
            # Backplane QSFP (crate) shuffle
            #################################
            crate_shuffle_bypass = 1

            crate_number = self.crate.crate_number if self.crate else 0
            stream_type = 4

            cb2_input_bins = cb1_bins
            if bp_shuffle_bypass and self.slot is not None:
                cb2_lane_map = self.CROSSBAR2.compute_bp_shuffle_lane_map()
            else:
                cb2_lane_map = list(range(16))

            cb2_input_frame_flags_words_per_frame = 1
            cb2_input_data_flags_words_per_bin = 1
            # Select the bins to be assigned to each bin selector output.
            if cb2_bin_indices:
                cb2_bins = len(cb2_bin_indices[0])
                cb2_bin_select_map = cb2_bin_indices
            else:
                # Default: we select all 64 incoming bins, but from half the input lanes
                cb2_bins = cb2_input_bins
                cb2_bin_spacing = 1
                cb2_bin_select_map = [np.arange(cb2_bins) * cb2_bin_spacing
                                      for i in range(number_of_cb2_bin_sel)]

            #################################
            # Backplane QSFP (crate) shuffle
            #################################
            # Not supported in this mode
            crate_shuffle_bypass = True

            #############################
            # 3rd Crossbar
            #############################
            # Not supported in this mode
            cb3_bypass = True
            cb3_lane_map = list(range(8))

            cb1_output_words_per_bin = cb1_lanes[0][1] - cb1_lanes[0][0] + 1
            cb1_output_bins = cb1_bins

        else:
            raise ValueError('Unknown mode')

        # cb2_payload_size = header_size + packet_flags_size + frames_per_packet \
        #   * (cb2_input_words_per_bin * cb2_bins* cb2_lanes + 1*cb2_bins*cb2_lanes/2 + cb2_lanes) * 4

        self.logger.debug(
            '%r: Configuring crossbars 1 & 2 with frames_per_packet=%i, cb1_lanes=%s, cb1_bins=%i, '
            'cb2_lanes=%s, cb2_bins=%i, cb2_bypass=%s, bp_shuffle_bypass=%s' % (
                self,
                frames_per_packet,
                cb1_lanes,
                cb1_bins,
                cb2_lanes,
                cb2_bins,
                bool(cb2_bypass),
                bool(bp_shuffle_bypass)))

        # Put everything in reset
        self.set_ant_reset(1)
        self.set_corr_reset(1)

        # slot_number = self.slot - 1 if self.slot is not None else 0
        slot_number = self.slot - 1 if self.slot else 0  # SC 03/17/2019 changed for individual iceboard

        ###########################
        #  Configure CROSSBAR 1
        ###########################

        stream_id = [((stream_type << 12) | (crate_number << 8) | (slot_number << 4) | lane)
                     for lane in range(cb1.NUMBER_OF_CROSSBAR_OUTPUTS)]
        for (cb1_output_lane, bs) in enumerate(cb1):
            bs.BYPASS = cb1_bypass
            bs.STREAM_ID = stream_id[cb1_output_lane] >> 4  # lane is already hardwared in the last 4 bits
            bs.COMBINE_DATA_FLAGS = cb1_combine_data_flags
            bs.SEND_FLAGS = send_flags
            bs.GROUP_FRAMES = frames_per_packet
            # print(stream_type, crate_number, slot_number)
            bs.FOUR_BITS = cb1_four_bit
            bs.FIRST_FIFO_NUMBER = cb1_lanes[cb1_output_lane][0]
            bs.LAST_FIFO_NUMBER = cb1_lanes[cb1_output_lane][1]
            # bs.NUMBER_OF_LANES = cb1_lanes
            bs.select_bins(cb1_bin_select_map[cb1_output_lane])

        ###########################
        # Configure BP_SHUFFLE
        ###########################
        if self.BP_SHUFFLE:
            # Configure PCB (intra-crate) shuffle
            self.BP_SHUFFLE.BYPASS_PCB_SHUFFLE = bp_shuffle_bypass
            # Configure CRATE (inter-crate) shuffle
            self.BP_SHUFFLE.BYPASS_QSFP_SHUFFLE = crate_shuffle_bypass
        elif not bp_shuffle_bypass:
            raise RuntimeError("The FPGA firmware implement BP_SHUFFLE in the '%s' operational mode "
                               "(shuffle bypass flag is not set in that mode)", mode)
        ###########################
        # Configure CROSSBAR 2
        ###########################
        if cb2:
            if cb2_bypass:  # if we bypass, just remap the stream ids from the previous crossbar
                stream_id = [stream_id[i] for i in cb2_lane_map]
            else:  # otherwise, the bin selector overrides
                stream_id = [((stream_type << 12) | (crate_number << 8) | (slot_number << 4) | lane)
                             for lane in range(cb2.NUMBER_OF_CROSSBAR_OUTPUTS)]
            cb2.IGNORE_LANE = cb2_ignore_lane
            cb2.set_lane_map(cb2_lane_map)
            if cb2_timeout_period is not None:
                cb2.TIMEOUT_PERIOD = cb2_timeout_period
            if cb2_sof_window_stop is not None:
                cb2.SOF_WINDOW_STOP = cb2_sof_window_stop
            for (cb2_bin_sel, bs) in enumerate(cb2):
                bs.BYPASS = bool(cb2_bypass)
                if not cb2_bypass:
                    bs.STREAM_ID = stream_id[cb2_bin_sel * cb2.NUMBER_OF_OUTPUTS_PER_BIN_SEL] >> 4
                    bs.SEND_FLAGS = send_flags
                    bs.NUMBER_OF_FRAMES_PER_PACKET = frames_per_packet
                    bs.NUMBER_OF_BINS_PER_FRAME = cb2_input_bins
                    bs.NUMBER_OF_WORDS_PER_BIN = cb2_input_words_per_bin
                    bs.NUMBER_OF_DATA_FLAGS_WORDS_PER_BIN = cb2_input_data_flags_words_per_bin
                    bs.NUMBER_OF_FRAME_FLAGS_WORDS_PER_FRAME = cb2_input_frame_flags_words_per_frame
                    bs.FIRST_LANE = cb2_lanes[cb2_bin_sel][0]
                    bs.LAST_LANE = cb2_lanes[cb2_bin_sel][1]
                    bs.select_bins(cb2_bin_select_map[cb2_bin_sel])
        elif not cb2_bypass:
            raise RuntimeError("The FPGA firmware must have a CROSSBAR2 in the '%s' operational mode", mode)

        ###########################
        # Configure CROSSBAR 3
        ###########################
        if cb3:
            cb3.IGNORE_LANE = cb3_ignore_lane
            cb3.set_lane_map(cb3_lane_map)
            if cb3_bypass:  # if we bypass, just remap the stream ids from the previous crossbar
                stream_id = [stream_id[i] for i in cb3_lane_map]
            else:  # otherwise, the bin selector overrides
                stream_id = [((stream_type << 12) | (crate_number << 8) | (slot_number << 4) | lane)
                             for lane in range(cb3.NUMBER_OF_CROSSBAR_OUTPUTS)]
            for (cb3_bin_sel, bs) in enumerate(cb3):
                bs.BYPASS = bool(cb3_bypass)
                if not cb3_bypass:
                    bs.STREAM_ID = stream_id[cb3_bin_sel * cb3.NUMBER_OF_OUTPUTS_PER_BIN_SEL] >> 4
                    bs.SEND_FLAGS = send_flags
                    bs.NUMBER_OF_FRAMES_PER_PACKET = frames_per_packet
                    assert cb3_input_data_flags_words_per_bin == int(cb3_input_data_flags_words_per_bin), f'CB3 number of flag words is not an integer ({cb3_input_data_flags_words_per_bin})'
                    bs.NUMBER_OF_DATA_FLAGS_WORDS_PER_BIN = int(cb3_input_data_flags_words_per_bin)
                    bs.NUMBER_OF_FRAME_FLAGS_WORDS_PER_FRAME = cb3_input_frame_flags_words_per_frame
                    # print('CB3: FFWPF=%i' % bs.NUMBER_OF_FRAME_FLAGS_WORDS_PER_FRAME)
                    bs.FIRST_LANE = cb3_lanes[cb3_bin_sel][0]
                    bs.LAST_LANE = cb3_lanes[cb3_bin_sel][1]
                    # print('CB3: FFWPF=%i' % bs.NUMBER_OF_FRAME_FLAGS_WORDS_PER_FRAME)
                    bs.NUMBER_OF_BINS_PER_FRAME = cb3_input_bins
                    bs.NUMBER_OF_WORDS_PER_BIN = cb3_input_words_per_bin
                    bs.select_bins(cb3_bin_select_map[cb3_bin_sel])

                    # bs.SEND_FLAGS = 0  # JFC debug. Does not affect data.
            # print("cb3 input frame flags words=%i" %cb3_input_frame_flags_words_per_frame)
        elif not cb3_bypass:
            raise RuntimeError("The FPGA firmware must implement CROSSBAR3 in the '%s' operational mode", mode)

        def print_packet_size(
                crossbar_name,
                frames_per_packet,
                bins,
                data_words_per_bin,
                data_flags_words_per_bin,
                frame_flags_words_per_frame):
            header_words_per_packet = 4
            packet_flags_words_per_packet = 1
            if not send_flags:
                data_flags_words_per_bin = 0
                frame_flags_words_per_frame = 0
            payload_size = 4 * (
                header_words_per_packet +
                frames_per_packet * (
                    (data_words_per_bin + data_flags_words_per_bin) * bins +
                    frame_flags_words_per_frame) +
                packet_flags_words_per_packet)
            ethernet_packet_overhead_bytes = 42
            ethernet_packet_size = (ethernet_packet_overhead_bytes + payload_size + 7) // 8 * 8
            # eth_data_rate = 156.25e6 * 66 * 32/33
            # bp_data_rate = 156.25e6* 50 * 32/33
            packet_rate = 800e6 / 2048 / frames_per_packet
            ethernet_data_rate = (packet_rate * ethernet_packet_size) * 8
            self.logger.info(
                f'{self!r}: {crossbar_name}, Eth packet size {ethernet_packet_size} bytes, Eth data rate {ethernet_data_rate/1e9:0.1f} Gbit/s')
            self.logger.debug(
                f'{self!r}: {crossbar_name}:'
                f'   UDP payload size: {payload_size} bytes\n'
                f'   Ethernet packet size: {ethernet_packet_size} bytes\n'
                f'   Ethernet data rate: {ethernet_data_rate/1e9:0.1f} Gbit/s\n'
                f'   Packet geometry: {frames_per_packet} frames_per_packet\n'
                f'                    {bins} bins\n'
                f'                    {data_words_per_bin} data words/bin\n'
                f',                   {data_flags_words_per_bin} data flags_words/bin\n'
                f'                    {frame_flags_words_per_frame} frame_flags_words/frame)'
                )
            # self.logger.info('%r: %s config: frames_per_packet=%i, cb1_lanes=%s, cb1_bypass=%s, '
            #                   'cb1_combine=%s, cb1_bins=%i, cb1_words_per_bin=%i' % (
            #                   self, frames_per_packet, cb1_lanes, bool(cb1_bypass), bool(cb1_combine_data_flags),
            #                   cb1_bins, cb1_output_words_per_bin ))
            # self.logger.debug(
            #   '%r: CROSSBAR1 output packets payload = %i bytes (%i words)' % (
            #       self, cb1_payload_size, (cb1_payload_size+3)//4))

        print_packet_size('CROSSBAR3',
                          frames_per_packet=frames_per_packet,
                          bins=cb3_output_bins,
                          data_words_per_bin=cb3_output_words_per_bin,
                          data_flags_words_per_bin=cb3_output_data_flags_words_per_bin,
                          frame_flags_words_per_frame=cb3_output_frame_flags_words_per_frame)

        self.set_corr_reset(0)
        self.set_ant_reset(0)
        return stream_id

    def reset_gpu_links(self):
        """ Resets the GPU links.

        All links are reset. There is no way to reset an individual QSFP or individual link.
        """
        self.GPU.reset()




    async def get_bp_shuffle_metrics_async(self, reset=True):
        if not self.is_open() or not self.BP_SHUFFLE:
            return Metrics()
        try:
            await self.clear_fpga_udp_errors()
            # await self.check_command_count(reset=True)
            metrics = await self.BP_SHUFFLE.get_metrics(reset=reset)
        except IOError as e:
            self.logger.error('%r: Error getting FPGA backplane link metrics. Error is %r' % (self, e))
            metrics = Metrics()
        return metrics

    async def get_channelizer_metrics_async(self, reset=True):
        metrics = Metrics(
            type='GAUGE',
            slot=(self.slot or 0) - 1,
            id=self.get_string_id(),
            crate_id=self.crate.get_string_id() if self.crate else None,
            crate_number=self.crate.crate_number if self.crate else None)

        if not self.is_open():
            return metrics
        try:
            # await self.check_command_count(reset=True)
            await self.clear_fpga_udp_errors()
            for i, chan in self.chan.items():
                metrics.add('fpga_fft_overflow_count', value=chan.FFT.OVERFLOW_COUNT, chan=i)
                metrics.add('fpga_scaler_overflow_count', value=chan.SCALER.STATS_SCALER_OVERFLOWS, chan=i)
                metrics.add('fpga_adc_overflow_count', value=chan.SCALER.STATS_ADC_OVERFLOWS, chan=i)
                if reset:
                    chan.FFT.reset_fft_overflow_count()
        except IOError as e:
            self.logger.error('%r: Error getting FPGA channelizer metrics. Error is %r' % (self, e))
        return metrics

    async def get_crossbar_metrics_async(self, reset=True):
        metrics = Metrics()
        if not self.is_open():
            return metrics
        try:
            # await self.check_command_count(reset=True)
            await self.clear_fpga_udp_errors()
            for cb in [self.CROSSBAR, self.CROSSBAR2, self.CROSSBAR3]:
                if cb:  # make sure the crossbar is in this firmware
                    metrics += await cb.get_metrics(reset=reset)
        except IOError as e:
            self.logger.error('%r: Error getting FPGA crossbar metrics. Error is %r\n\n%s' % (self, e, traceback.format_exc()))
        except Exception as e:
            self.logger.error('%r: Unhandled error while  getting FPGA crossbar metrics. Error is %r\n\n%s' % (self, e, traceback.format_exc()))
        return metrics



    def get_correlator_params(self):
        """ Returns the correlator geometry and configuration """
        return dict(
            number_of_correlators=self.NUMBER_OF_CORRELATORS,  # hard coded in firmware
            number_of_correlated_inputs=self.NUMBER_OF_INPUTS_TO_CORRELATE,  # hard coded in firmware
            number_of_bins_per_frame=self.CROSSBAR.BIN_SEL[0].NUMBER_OF_SELECTED_WORDS  # depends on crossbar configuration
            )

    def start_correlator(
            self,
            integration_period=16384,
            autocorr_only=False,
            correlators=None,
            bandwidth_limit=0.5e9,
            verbose=1):
        """
        (Re)starts the correlator with the specified integration time.

        Parameters:

            integration_period (32-bit int): Number of frames to integrate
               before sending the correlated products. Lower integration
               period increase the frequency at which correlated frames are
               sent and increase the require bandwidth. Longer integration
               periods will procuce larger accumulated products that will
               saturate if they exceed the accumulator limits (from -131072 to
               131071 for each if the real and imaginary component).

            autocorr_only (bool): When 'True', the correlator will only send
               the autocorrelation products, which will reduce bandwidth
               requirement (12% of the full bandwidth) and will allow shorter
               integration periods. Note that the imaginary parts are always
               zero but are sent anyways to keep the frame format identical
               despite the waste of bandwidth.

            correlators (list of int): List of correlator cores to enable. All
               other cores will be disabled. Default is None, which means all
               correlators will be enabled. Each correlator core process the
               frequency bins selected with its corresponding bin selector.
               Using a smaller number of cores will process less frequency
               bins but will proportionnally usee less data bandwidth.

            bandwidth_limit (float): Maximum acceptable data bandwidth that
               the correlator can produce, in bits/s. Default is 0.5 Gbps. If the correlator
               parameters are to make the data exceed this bandwidth, an
               exception will be raised, with a message that describe
               alternate settings. In this case, no changes are made to the
               correlator operation.

        Returns:
            None

        Note:

            - Sets the offest binary encoding to False. This is not restored
              when the correlator is stopped.

        """
        if not self.CORR:
            raise RuntimeError('The FPGA firmware does not contain a correlator core')

        corr_params = self.get_correlator_params()
        self.CORR.start_correlator(integration_period=integration_period,
                                   autocorr_only=autocorr_only,
                                   correlators=correlators,
                                   bins_per_frame=corr_params['number_of_bins_per_frame'],
                                   bandwidth_limit=bandwidth_limit,
                                   verbose=verbose)

    def stop_correlator(self):
        if not self.CORR:
            raise RuntimeError('The FPGA firmware does not contain a correlator core')

        self.CORR.stop_correlator()

    def get_corr_receiver(self, verbose=1):
        if self.corr_recv:
            return self.corr_recv
        sock = self.get_data_socket()
        self.corr_recv = self.CORR.get_data_receiver(sock)
        return self.corr_recv


    def compute_corr_output(self, data, integration_period=16384):
        """
        Compute the expected correlator output given the channelizer output `data`.

        Parameters:

            data (ndarray): data[channel, bin] = complex

        Returns:

            array(bins, i, j) = complex
        """
        corr = data.T[:, None, :] * data.T[:, :, None].conj() * integration_period
        corr = np.clip(corr.real, -131072, 131071) + 1j*np.clip(corr.imag, -131072, 131071)
        i, j = np.tril_indices_from(corr[0], -1)  # indices of the lower triangle excluding the diagonal
        # Reapply upper triangle from lower, because saturation is not the same for negative and
        # positive imaginary values
        corr[:, j, i] = corr[:, i, j].conj()
        return corr

    def test_correlator_output(self, data, integration_period=32768, verbose=0):
        """ Set the channelizer outputs to `data` and check the correlator output.


        Parameters:
            data (ndarray): Data that should appear at the channelizer
                output, indexed as data[channel, bin] = complex_value. channel
                ranges from 0 to 15, bin from 0 to 1023. The complex value has the
                ranged of a signed (4+4) bit, meaning that the real and imaginary
                part can range from -8 to 7.

        """
        r = self.get_data_receiver(verbose=0)
        self.set_channelizer_outputs(data)
        self.start_correlator(integration_period=integration_period, verbose=verbose)
        self.sync()
        p = self.compute_corr_output(data, integration_period=integration_period)
        f = r.read_corr_frames(flush=False, complete_set=True, max_trials=100, verbose=verbose)
        return np.all(p == f), p, f

    def test_correlator(self, test_name='rand_complex', integration_period=8192, trials=100, verbose=0):
        if test_name == 'rand_complex':
            for data_set_number in range(trials):
                print('Trial #%i' % data_set_number)
                data = np.floor(np.random.rand(16, 1024) * 4 - 2) + 1j * np.floor(np.random.rand(16, 1024) * 4 - 2)
                trial = 0
                while True:
                    match, p, f = self.test_correlator_output(data=data, integration_period=integration_period)
                    if match:
                        break
                    trial += 1
                    if trial < 10:
                        print('Frames did not match! Retrying after rewriting the test data again...')
                    else:
                        print('Cannot make frames match!')
                        return match, data, p, f

        elif test_name == 'rand_complex_C':
            for data_set_number in range(trials):

                data = (np.floor(np.random.rand(16, 1024) * 4 - 2) + 1j * np.floor(np.random.rand(16, 1024) * 4 - 2))

                trial = 0
                while True:
                    self.set_channelizer_outputs(data)

                    self.start_correlator(integration_period=integration_period, verbose=(0 if trial == 0 else 0))

                    self.sync()
                    p = self.compute_corr_output(data, integration_period=integration_period)
                    timestamp,f = ir.read_correlator_frame(verbose=verbose)
                    f = np.swapaxes(f, 0, 2)

                    match = np.all(p==f)
                    if match:
                        if data_set_number % 10 == 0 and data_set_number > 0:
                            print('[{3:s}]: Test #{0:d}/{1:d}; trial {2:d}'.format(data_set_number, trials, trial, datetime.now().strftime("%H:%M:%S.%f")))
                            print("\t{0:d}".format(timestamp))
                        break
                    trial += 1
                    if trial < 10:
                        print('Trial {0:d} frames did not match! Retrying after rewriting the test data again...'.format(trial))
                        if(verbose):
                            print("Difference: ")
                            print(p - f)
                    else:
                        print('Cannot make frames match!')
                        return match, data, p, f

        else:
            raise ValueError('Unknown test name %s' % test_name)

        print('*** TEST PASSED! ***')
        return True, None, None, None
