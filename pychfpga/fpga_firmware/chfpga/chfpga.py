#!/usr/bin/python

"""
This module defines the `chFPGA` class, which provides a Python interface to operate an
chFPGA firmware and its associated hardware platform.

.. Notes:
..     Created 2011-01-10. See GIT for commit history.
"""

# Python Standard Library packages
import logging
import time
import os
import pickle
import base64
from datetime import datetime, timedelta, timezone
import struct
from calendar import timegm
from functools import wraps
import socket
import __main__
import asyncio
import traceback
from typing import List, Literal, Optional, Sequence, Tuple

# PyPi external packages
import numpy as np
import yaml

# Private external packages

from wtl.metrics import Metrics

# Local packages


from pychfpga.common import State, run_async

from pychfpga.fpga_firmware import FPGAFirmware
from .chfpga_udp import chFPGA_UDP
from .chFPGA_receiver import chFPGA_receiver
from .f_engine.scaler import SCALER
# Memory-mapped interface
from .mmi import MMI, MMIRouter

# FPGA subsystems handlers
from .system import spi
from .system import i2c
from .system import gpio
from .system import sysmon
from .system import freqctr
from .system import refclk

# FPGA Channelizer (F-Engine)
from .f_engine import chan, fft
from .f_engine import prober

# FPGA Corner-turn Engine

from .ct_engine import uct_engine
from .ct_engine import bct_engine
from .ct_engine import upack
from .ct_engine import gpu
from .ct_engine import cge
from .f_engine import ucap

# FPGA Correlator (X-Engine)
from .x_engine import CORR, UCORR  # Correlators (if implemented in firmware)
from .chfpga_array_methods import chFPGAArrayMethods



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
        return '\n'.join([f'{key} = {value!r}' for (key, value) in sorted(vars(self).items())])


class chFPGA(chFPGA_UDP, FPGAFirmware):
    """Provides an API to access the chFPGA firmware running on the motherboard's FPGA.

    The API provides the methods to establish communication with the FPGA to
    access its memory map, and provides objects to configure it and control
    its operations.

    This class supports two methods to access the FPGA firmware:

    - Ethernet/UDP-based Memory-mapped Interface (MMI) implemented directly by the FPGA
      (UltraScale+ USP UDP stack), used on MGK7MB today. SPI-based access from the on-board
      processor configures FPGA networking before that UDP link comes up.

    - TCPipe: TCP-based BSB bridge on the on-board processor (ZCU111 / CRS today).
    """

    _array_method_classes = {'chFPGA': chFPGAArrayMethods}

    #: Values for ``self.BSB_TRANSPORT`` and ``PLATFORM_SUPPORT[''bsb_transport'']`` (same strings).
    BSB_TRANSPORT_UDP = 'udp'
    BSB_TRANSPORT_TCPIPE = 'tcpipe'

    #: Requested kernel receive-buffer (SO_RCVBUF) for the UDP data socket. Sized to
    #: absorb datagram bursts while the single-threaded receiver is busy in
    #: process/format. Clamped by net.core.rmem_max (raise that if drops persist).
    UDP_DATA_RCVBUF = 16 * 1024 * 1024

    # Define the motherboard models and operational modes supported by this class, and associate corresponding FPGA
    # configuration bitstreams and initialization parameters The 'clock_divider' is used to know when clock frequency
    # to expect when we monitor the ADC clock with the frequency counter.This could be independent from the
    # procrssing clock. For the CRS, the processing clock is Fs/8 and the monitoring clock is Fs/16. For the
    # IceBoard, it's both fs/4. (fs=sampling frequency)
    PLATFORM_SUPPORT = ( # [ {"platform":<platform_model>, "modes":<supported_modes>, "firmware_url":<firmware filename_or_url>, <other platform key-value parameters>}, ...]
        dict(platform="MGK7MB", modes=("shuffle16", "shuffle128", "shuffle256", "shuffle512","chan8", "chan4"),    
            firmware_url='chfpga_ice_ct.bit',         
            bsb_transport=BSB_TRANSPORT_UDP,    
            sampling_frequency=800e6,  processing_frequency=200e6, adc_clock_divider=4,
            corr_readout_bins=None),
        dict(platform="MGK7MB", modes=("corr16",),           
            firmware_url='chfpga_ice_corr16_pbg_jun16.bit', 
            bsb_transport=BSB_TRANSPORT_UDP,   
            sampling_frequency=800e6,  processing_frequency=200e6, adc_clock_divider=4,
            corr_readout_bins=(0,0)),
        dict(platform="MGK7MB", modes=("chord16",),          
            firmware_url='chordFPGA_MGK7MB_Rev2.bit', 
            bsb_transport=BSB_TRANSPORT_UDP,   
            sampling_frequency=1200e6, processing_frequency=300e6, adc_clock_divider=4,
            corr_readout_bins=None),
        dict(platform="ZCU111", modes=("corr4", "corr8"),    
            firmware_url='sifpga_zcu111_wrapper.bit', 
            bsb_transport=BSB_TRANSPORT_TCPIPE, 
            sampling_frequency=3000e6, processing_frequency=375e6, adc_clock_divider=16,
            corr_readout_bins=(0,1,2,3)),
        dict(platform="ZCU111", modes=("chan8",),            
            firmware_url='chfpga_zcu111.bit',         
            bsb_transport=BSB_TRANSPORT_TCPIPE, 
            sampling_frequency=3000e6, processing_frequency=375e6, adc_clock_divider=16,
            corr_readout_bins=None),
        dict(platform="CRS",    modes=("corr8",),     
            firmware_url='chfpga_crs_corr8.bit',     
            bsb_transport=BSB_TRANSPORT_TCPIPE, 
            sampling_frequency=3200e6, processing_frequency=3200e6/8, adc_clock_divider=32,
            corr_readout_bins=(0,1,2,3)),
        dict(platform="CRS",    modes=("corr32",),           
            firmware_url='chfpga_crs_corr32.bit',     
            bsb_transport=BSB_TRANSPORT_TCPIPE, 
            sampling_frequency=3200e6, processing_frequency=3200e6/8, adc_clock_divider=32,
            corr_readout_bins=(0,)),
        dict(platform="CRS",    modes=("corr64",),           
            firmware_url='chfpga_crs_corr64.bit',     
            bsb_transport=BSB_TRANSPORT_TCPIPE, 
            sampling_frequency=3200e6, processing_frequency=3200e6/8, adc_clock_divider=32,
            corr_readout_bins=(0,)),
        dict(platform="CRS",    modes=("chan8", "shuffle8"), 
            firmware_url='chfpga_crs_ct.bit',        
            bsb_transport=BSB_TRANSPORT_TCPIPE, 
            sampling_frequency=3200e6, processing_frequency=3200e6/8, adc_clock_divider=32,
            corr_readout_bins=None),
    )

    FFT_INFO = fft.FFT_INFO

    # Define the top BSB command router for this firmware
    # Here we set the address width of the BSB, but other routers derive it automatically from previous routers.  

    class TopRouter(MMIRouter):
        ADDRESS_WIDTH = 19
        ROUTER_PORT_NUMBER_WIDTH = 3
        ROUTER_PORT_MAP = {
            'SYSTEM': 0,
            'CHAN': 1,
            'CT': 2,
            'GPU': 3,
            'CORR': 4,
            'UPACK': 5,
            'UCAP': 7
        }

    class SystemRouter(MMIRouter):
        ROUTER_PORT_NUMBER_WIDTH = 4
        ROUTER_PORT_MAP = {
            'GPIO': 0,  #:  SYSTEM.GPIO submodule
            'SYSMON': 1,  #:  SYSTEM.SYSMON submodule
            'FREQ_CTR': 2,  #:  SYSTEM.FREQ_CTR submodule
            'SPI': 3,  #:  SYSTEM.SPI submodule
            'REFCLK': 4,  #:  SYSTEM.REFCLK submodule
            'I2C': 5 #:  SYSTEM.I2C submodule
        }


    ################################################################################################
    # Core FPGA firmware registers
    ################################################################################################
    # Core register addresses are byte offsets 
 
    FPGA_CORE_FIRMWARE_COOKIE_ADDR        = 4 * 0 # (read only) 0xbeefface
    FPGA_APPLICATION_FIRMWARE_COOKIE_ADDR = 4 * 1 # (read-only) 0x42
    FPGA_APPLICATION_FLAGS_ADDR           = 4 * 2
    FPGA_FIRMWARE_CRC32_ADDR              = 4 * 3
    FPGA_FIRMWARE_TIMESTAMP_ADDR          = 4 * 4
    FPGA_SERIAL_NUMBER_LSW_ADDR           = 4 * 5
    FPGA_SERIAL_NUMBER_MSW_ADDR           = 4 * 6
    _FPGA_MAC_ADDR_LSW_ADDR         = 4 * 7 # FPGA listening MAC address (UDP BSB / ``BSB_TRANSPORT_UDP`` only)
    _FPGA_MAC_ADDR_MSW_CMD_LISTENING_PORT_ADDR = 4 * 8 # FPGA listening MAC address and command listening port (UDP BSB only)
    _FPGA_IP_ADDR_ADDR              = 4 * 9 # FPGA listening IP address (UDP BSB only)
    _IRIGB_SAMPLE0_ADDR             = 4 * 10
    _IRIGB_SAMPLE1_ADDR             = 4 * 11
    _IRIGB_SAMPLE2_ADDR             = 4 * 12

    _IRIGB_TARGET0_ADDR             = 4 * 13
    _IRIGB_TARGET1_ADDR             = 4 * 14
    _IRIGB_TARGET2_ADDR             = 4 * 15
    _IRIGB_EVENT_CTR_ADDR           = 4 * 16
    _IRIGB_EVENT_CTR_ADDR2          = 4 * 17
    _SFP_CONFIG_ADDR                = 4 * 18  # SFP configuration register (UDP BSB only)
    _SFP_STATUS_ADDR                = 4 * 19  # SFP status register (UDP BSB only)
    _CMD_REPLY_DEST_PORT_ADDR       = 4 * 20 # command reply destination port (UDP BSB only)
    _IRIGB_REFCLK_SAMPLE            = 4 * 21 # lower word of REFCLK counter (UDP BSB only)
    _IRIGB_REFCLK_SAMPLE2           = 4 * 22 # upper word of REFCLK counter (UDP BSB only)
    _FPGA_DATA_DEST_MAC_ADDR_LSW_ADDR         = 4 * 23 # Data packet destination MAC address (UDP BSB only)
    _FPGA_DATA_DEST_MAC_ADDR_MSW_IP_PORT_ADDR = 4 * 24 # Data packet destination MAC address and port (UDP BSB only)
    _FPGA_DATA_DEST_IP_ADDR_ADDR              = 4 * 25 # Data packet destination IP address (UDP BSB only)


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
        _PLATFORM_ID_CRS:  ('Zynq Ultrascale+ RfSoC (XCZU47) t0 technology CRS Board ', None),
    }


    _XILINX_OUI = 0x000A35

    _CHFPGA_COOKIE = 0x42  # Expected cookie value for chFPGA, both on the SPI and UDP MMI

    # GPIO Register addresses
    _GPIO_COOKIE_ADDR = 0  # MMI address of the firmware cookie

    def __init__(self, motherboard, mode, **fw_params):
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
        self.mode = mode # operational mode that was requested to load the firmware
        self.logger = logging.getLogger(__name__)
        self.logger.debug(f"{self!r}: Creating chFPGA FPGAFirmware object")
        self.config = State()

        # Firmware attributes provided by PLATFORM_SUPPORT. Those will be used later by `init_async`.
        self.fw_params = fw_params

        # Firmware attributes that will be intialized later by `init_async`
        self.processing_frequency = None
        self._sampling_frequency = None  # Set in init()
        self._reference_frequency = None  # set in init()
        self.FRAME_PERIOD = None
        self._FMC_present = []  # indicates if the FMC board is present. If not, the modules will act accordingly.
        self._last_init_time = None
        self._capture_pkt_frames_per_burst = None
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

        #: ``BSB_TRANSPORT_UDP``, ``BSB_TRANSPORT_TCPIPE``, or ``None`` until :meth:`open_mmi_async`.
        self.BSB_TRANSPORT = None

    def get_id(self, lane=None, default_crate=None, default_slot=None, numeric_only=False):
        """ Get the ID of the chFPGA board as a tuple containing the crate number, slot number, and optional lane number.

        Parameters:
            lane (int): The lane number of the chFPGA board. If None, the lane number is not included in the ID.
            default_crate (int): The default crate number to use if the crate number is not available.
            default_slot (int): The default slot number to use if the slot number is not available.
            numeric_only (bool): If True, the ID will only contain the numeric crate and slot numbers. Otherwise the crate and slot fields could be the model+serial string if the crate_number is not initialied or the board is not in a crate and no slot has been manually provided.

        Returns:
            A tuple containing the crate number, slot number, and lane number.
        """
        return self.mb.get_id(lane=lane, default_crate=default_crate, default_slot=default_slot, numeric_only=numeric_only)

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

        Transport is chosen from the bsb_transport parameter in the firmware parameters.

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

        transport = self.fw_params['bsb_transport']
        self.BSB_TRANSPORT = transport
        if not self.BSB_TRANSPORT:
            raise ValueError(f'Unsupported bsb_transport {transport!r} in firmware params')

        # Discover ethernet interface through which we communicate with the
        # motherboard. We will use the same interface to listen to data.
        self.set_host_interface_ip_address()

        # Creates the MMI interface and open communications to the FPGA through it.
        if self.BSB_TRANSPORT == self.BSB_TRANSPORT_TCPIPE:
            self.mmi = await self.mb.tcpipe.open_tcpipe_mmi_async()
        elif self.BSB_TRANSPORT == self.BSB_TRANSPORT_UDP:
            self.mmi = await self.open_udp_mmi_async()
        else:
            raise RuntimeError(
                f'{self!r}: No BSB transport was specified for this firmware/motherboard (BSB_TRANSPORT={self.BSB_TRANSPORT!r}'); 
                
    async def close_mmi_async(self):
        """ Closes the MMI interface to the FPGA 
        
        Returns:
            None
        """
        if self.mmi:
            self.mmi.close()
            self.mmi = None

    def set_host_interface_ip_address(self):
        """Sets the IP address of the host interface through which the host running pychfpga
        communicates with the motherboard, if this interface is not yet defined.

        This sets the ``self.interface_ip_addr`` attribute. The interface is not changed if it already is non-zero.

        The indterface is determined by looking at the interface address of a socket once it is connected to the motherboard. We assume that
        the data packets are arriving through the same
        interface as the command stream. 

        If the BSB transport is TCPipe, the interface is obtained by inspecting the existing TCPIPE
        socket.**

        If the BSB transport is not TCPipe, the interface is obtained by opening a TCP socket to the motherboard processor and inspecting the
        socket that was used, and then close that socket. **This assumes that the brief socket connection will not cause side-effects on the motherboard.**
        """

        if self.interface_ip_addr:  # set the interface only if we have not manually defined one
            return

        if self.mb.hostname:
            if self.BSB_TRANSPORT == self.BSB_TRANSPORT_TCPIPE:
                s = self.mb.tcpipe.sock
                (self.interface_ip_addr, _) = s.getsockname()
            else:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.connect((self.mb.hostname, self.mb.port))
                self.logger.debug(f'{self!r}: Established TCP connection with {s.getsockname()} to determine interface.')
                (self.interface_ip_addr, _) = s.getsockname()
                s.close() 
        else:
            self.interface_ip_addr = None


    async def open_async(
           self,
           init=1,
           verbose=0,
           fpga_ip_addr=None,
           fpga_ip_addr_fn=None, # lambda a, b, c, d: (a, b, 3, d),
           fpga_control_port_number=None,
           local_control_port_number=None,
           interface_ip_addr=None,
           udp_retries=10,
           irigb_source=None):
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

            irigb_source (str): IRIG-B source name to select after MMI is up.
                If ``None``, the decoder source is left unchanged. Specifying this parameter 
                allows the IRIG-B decoder to be setup as soon as possible during the init sequence 
                so it can decode its first valid second as soon as possible.


        Notes:

            Some networking  parameters can be overridden by matching
            values are stored on ``self`` and used for the rest of open.
            extra parameters provided during hwm construction.
        """

        # Store networking parameters, overriding them with the extra hardware map parameters if they were provided at construction time
        hwm = self.mb.extra_hwm_args
        self.fpga_ip_addr = hwm.get('fpga_ip_addr', fpga_ip_addr)
        self.fpga_ip_addr_fn = hwm.get('fpga_ip_addr_fn', fpga_ip_addr_fn)
        self.fpga_control_port_number = hwm.get('fpga_control_port_number', fpga_control_port_number)
        self.local_control_port_number = hwm.get('local_control_port_number', local_control_port_number)
        self.interface_ip_addr = hwm.get('interface_ip_addr', interface_ip_addr)
        self.udp_retries = hwm.get('udp_retries', udp_retries)

        self.logger.debug(
            f'{self!r}: Opening communication with the FPGA with parameters '
            f'fpga_ip_addr={self.fpga_ip_addr}, '
            f'fpga_control_port_number={self.fpga_control_port_number}, '
            f'local_control_port_number={self.local_control_port_number}, '
            f'interface_ip_addr={self.interface_ip_addr}, init={init}')

        # If init<0, we do not perform any communication with the FPGA, so we don't read the firmware configuration
        if init < 0:
            self.logger.warning(
                f'{self!r}: Upon user request (init < 0), communication with '
                f'the FPGA are inhibited. Initialization sequence stops here. '
                f'Use this for debug only.')
            return

        # Open a communicaition link with the FPGA to create the BSB MMI interface
        await self.open_mmi_async()
        self.mmi.flush()

        self.top_router = self.TopRouter(fpga_instance=self)
        self.system_router = self.SystemRouter(router=self.top_router, router_port='SYSTEM')

        try:  # catch initialization errors so we can free the socket for future instantiation
            # Try to communicate with the FPGA by reading he firmware BSB Registers cookie
            try:
                # Instantiate and initialize the GPIO subsystem
                self.logger.debug(f'{self!r}: === Instantiating and initializing GPIO')
                await asyncio.sleep(0)
                self.GPIO = gpio.GPIO(router=self.system_router, router_port='GPIO', config=self.config.add("gpio"))
                # self.GPIO.init() # don't call init() yet as this sends some commands. The module can still read the cookie without it.

                # Read the firmware version cookie from the GPIO subsystem (this
                # is provided by the FPGA core firmware which is always present on
                # all versions of the FPGA)
                cookie = await self.get_fpga_firmware_cookie_async(resync=True)
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

            self.GPIO.init()

            with self.mmi.aggregate_commands():
                # Start the IRIG-B decoder as soon as MMI works so it can lock a
                # full second during the rest of open (e.g. CRS 0.5 s PLL settle)
                # and firmware init. Later set_irigb_source_async calls skip if
                # the mux is already this source, so they do not clear `recent`.
                self._current_irigb_source_sel_pol = None
                if irigb_source:
                    await self.set_irigb_source_async(irigb_source)

                # ---------------------------------------------------------------------
                # -- Gather platform and FPGA firmware configuration information
                # ---------------------------------------------------------------------
                #  We use the information provided from GPIO registers to determine what
                # firmware modules are present and what their geometry is.
                #
                # We try to have the FPGA auto-report its configuration in details and avoid using look-up tables.
                # The exception to this are the platform-specific parameters which are pretty static.
                #
                # We don't want to instantiate
                # modules that are not present in firmware, as their initialization will fail.
                # ---------------------------------------------------------------------

                self.GPIO.set_channelizer_reset(True)  # stop the channelizer from sending data while we initialize

                # get system constants from the FPGA
                self.logger.debug(f'{self!r}: === Getting board info information')
                await asyncio.sleep(0)
                self.PLATFORM_ID = self.GPIO.PLATFORM_ID
                if self.PLATFORM_ID not in self._PLATFORM_ID_LIST:
                    raise RuntimeError(f'{self!r}: Platform ID 0x{self.PLATFORM_ID:02X} is not recognized')


                self._NUMBER_OF_FMC_SLOTS = self.mb.NUMBER_OF_FMC_SLOTS

                self.NUMBER_OF_ADCS = self.GPIO.NUMBER_OF_ADCS
                self.FFT_TYPE = self.GPIO.FFT_TYPE
                self.FFT_LATENCY = (self.FFT_INFO[self.FFT_TYPE])['latency']
                self.CT_TYPE = ('NONE', 'BCT','UCT')[self.GPIO.CT_TYPE]
                self.CT_LEVEL = self.GPIO.CT_LEVEL

                # Set platform/implementation-specific features & constants based on a local table
                if self.PLATFORM_ID in (self._PLATFORM_ID_ZCU111, self._PLATFORM_ID_CRS):
                    self.HAS_REFCLK = True
                    self.HAS_SPI = False
                    self.HAS_I2C = False
                    self.HAS_ADCDAQ = False
                    self.HAS_FMC = False
                    self.MAX_BSB_COMMAND_LENGTH = 512
                    self.HAS_UPACK = self.mode == 'shuffle8' # Hacky method until we can read it back.
                    self.CROSSBAR1_TYPE = "URAM"
                    self.GPU_LINK_TYPE = "100GE"
                    self.CAPTURE_TYPE = "UCAP"
                    self.CORR_TYPE = "UCORR44"
                    self.CAPTURE_BUS_WIDTH = 128  # width of the data capture bus from FUNCGEN/SCALER into the capture engine
                    self.ADC_FREQS_TO_CHECK = range(self.NUMBER_OF_ADCS)
                    assert self.BSB_TRANSPORT == self.BSB_TRANSPORT_TCPIPE, f'{self!r}: Expected TCPipe BSB for PLATFORM_ID ZCU111/CRS'

                elif self.PLATFORM_ID in (self._PLATFORM_ID_MGK7MB_REV0, self._PLATFORM_ID_MGK7MB_REV2):
                    assert self.mb.part_number == "MGK7MB", 'This version of the firmware is meant to operate on the MGK7MB (IceBoard) only'
                    self.HAS_REFCLK = True
                    self.HAS_SPI = True
                    self.HAS_I2C = True
                    self.HAS_ADCDAQ = True
                    self.HAS_FMC = True
                    self.MAX_BSB_COMMAND_LENGTH = 2048  # maybe more, depends on the UDP bufer
                    # self.CT_TYPE = "BCT" # Programmable BRAM- and GTX-based corner turn (16 inputs (2 bins/input/clk) x 8 outputs across 1,16 and 32 boards)
                    self.HAS_UPACK = False
                    self.CROSSBAR1_TYPE = "BRAM"
                    self.GPU_LINK_TYPE = "10GE"
                    self.CAPTURE_TYPE = "PROBER"
                    # self.CORR_TYPE = "CORR44"
                    self.CORR_TYPE = "UCORR44"
                    self.ADC_FREQS_TO_CHECK = (0,4,8,12)
                    self.CAPTURE_BUS_WIDTH = 32 # width of the data capture bus from FUNCGEN/SCALER into the capture engine
                    assert self.BSB_TRANSPORT == self.BSB_TRANSPORT_UDP, f'{self!r}: Expected FPGA UDP BSB for PLATFORM_ID MGK7MB'

                else:
                    raise RuntimeError(f'Unknown feature list for PLATFORM_ID = {self.PLATFORM_ID}')


                # Set platform/implementation-specific features & constants based on values reported by the FPGA GPIO module
                #
                # Get frame size info, i.e number of samples per frame
                self._LOG2_FRAME_LENGTH = self.GPIO.LOG2_FRAME_LENGTH
                self.FRAME_LENGTH = 2**self._LOG2_FRAME_LENGTH  # 2**11 = 2048 time samples per frame
                self.ADC_SAMPLES_PER_FRAME = self.FRAME_LENGTH  # more explicit name

                self.NUMBER_OF_FREQUENCY_BINS = self.FRAME_LENGTH // 2  # 1024 frequency bins per frame

                # Number of ADC samples per word (i.e number of samples processed per clock)
                self._LOG2_SAMPLES_PER_WORD = self.GPIO.LOG2_SAMPLES_PER_WORD
                self.SAMPLES_PER_WORD = 2**self._LOG2_SAMPLES_PER_WORD

                # Number of bits and bytes per ADC sample
                self.ADC_BITS_PER_SAMPLE = self.GPIO.ADC_BITS_PER_SAMPLE
                self.ADC_BYTES_PER_SAMPLE = (self.GPIO.ADC_BITS_PER_SAMPLE + 7) // 8  # round up to the nearest byte
                assert self.CAPTURE_BUS_WIDTH == 8 * self.ADC_BYTES_PER_SAMPLE * self.SAMPLES_PER_WORD, f'{self!r}: CAPTURE_BUS_WIDTH must be equal to 8 * ADC_BYTES_PER_SAMPLE * SAMPLES_PER_WORD (all samples must fit in the capture bus)'
                self.FFT_BINS_PER_WORD = int(self.FFT_INFO.get(self.FFT_TYPE, {}).get("bins_per_word") or 1)
                # Number of bytes in a frame
                self.ADC_BYTES_PER_FRAME = self.FRAME_LENGTH * self.ADC_BYTES_PER_SAMPLE  # SAMPLES_PER_FRAME * BYTES_PER_SAMPLE

                # Identify the number of channelizers and their properties
                self.CHANNELIZERS_CLOCK_SOURCE = self.GPIO.CHANNELIZERS_CLOCK_SOURCE
                self.NUMBER_OF_CHANNELIZERS = self.GPIO.NUMBER_OF_CHANNELIZERS
                self.NUMBER_OF_ANTENNAS_WITH_FFT = self.GPIO.NUMBER_OF_CHANNELIZERS_WITH_FFT
                self.LIST_OF_ANTENNAS_WITH_FFT = list(range(self.NUMBER_OF_ANTENNAS_WITH_FFT))

                # Get corner-turn engine configuration info
                # CT-Engine type (CT_TYPE) is defined by lookup table based on the platform
                self.NUMBER_OF_CROSSBAR_INPUTS = self.GPIO.NUMBER_OF_CROSSBAR_INPUTS
                self.NUMBER_OF_CROSSBAR1_OUTPUTS = self.GPIO.NUMBER_OF_CROSSBAR1_OUTPUTS

                # Get Real-time data offload link configuration info
                # GPU_LINK_TYPE is defined by lookup table based on the platform
                self.NUMBER_OF_GPU_LINKS = self.GPIO.NUMBER_OF_GPU_LINKS

                # Get (optional) embedded firmware correlator configuration info and their properties
                self.NUMBER_OF_CORRELATORS_MAX = self.GPIO.NUMBER_OF_CORRELATORS
                self.NUMBER_OF_CORRELATORS = self.GPIO.NUMBER_OF_CORRELATORS
                self.LIST_OF_IMPLEMENTED_CORRELATORS = list(range(self.NUMBER_OF_CORRELATORS))
                self.NUMBER_OF_INPUTS_TO_CORRELATE = self.GPIO.NUMBER_OF_CHANNELIZERS_TO_CORRELATE
                if not self.NUMBER_OF_CORRELATORS:
                    self.CORR_TYPE = None

                self.default_channels = list(range(self.NUMBER_OF_CHANNELIZERS))

                self.logger.debug(f'{self!r}:     Firmware timestamp: {self.get_version()}')
                self.logger.debug(f'{self!r}:     Hardware platform: {self._PLATFORM_ID_LIST[self.PLATFORM_ID][0]}')
                self.logger.debug(f'{self!r}:         Number of FMC slots: {self._NUMBER_OF_FMC_SLOTS}')
                self.logger.debug(f'{self!r}:     ADC configuration')
                self.logger.debug(f'{self!r}:         Number of ADCs: {self.NUMBER_OF_ADCS}')
                self.logger.debug(f'{self!r}:         Number of bits per sample: {self.ADC_BITS_PER_SAMPLE}')


                self.logger.debug(f'{self!r}:     F-Engine configuration')
                self.logger.debug(f'{self!r}:         Number of channelizers: {self.NUMBER_OF_CHANNELIZERS}')
                self.logger.debug(f'{self!r}:         Number of channelizers with FFT: {len(self.LIST_OF_ANTENNAS_WITH_FFT)} (input channel {str(self.LIST_OF_ANTENNAS_WITH_FFT)})')
                self.logger.debug(f'{self!r}:     CT-Engine configuration')
                self.logger.debug(f'{self!r}:         Crossbar configuration: {self.NUMBER_OF_CROSSBAR_INPUTS} inputs x {self.NUMBER_OF_CROSSBAR1_OUTPUTS} outputs')
                self.logger.debug(f'{self!r}:     X-Engine configuration')
                self.logger.debug(f'{self!r}:         Number of correlators: {len(self.LIST_OF_IMPLEMENTED_CORRELATORS)} (correlators {self.LIST_OF_IMPLEMENTED_CORRELATORS}')
                self.logger.debug(f'{self!r}:         Number of channelizers supported by the correlators: {self.NUMBER_OF_INPUTS_TO_CORRELATE}')

                await asyncio.sleep(0)


                # ---------------------------------------------------------------------
                # -- Create basic FPGA resource handlers objects
                # ---------------------------------------------------------------------
                #
                #  NOTE: Does not initialize module objects yet because some modules are
                #  interdependent - we need to wait until all of them are
                #  instantiated before we initialize them.
                #
                #  NOTE: The instantiation does not initiate communicattion with
                #  the hardware yet. This is done in the INIT phase.


                self.logger.debug(f'{self!r}: === Instantiating SYSTEM objects')

                self.logger.debug(f'{self!r}: ===     Instantiating SYSMON')
                self.SYSMON = sysmon.SYSMON(router=self.system_router, router_port='SYSMON', config=self.config.add("sysmon"))

                if self.HAS_SPI:
                    self.logger.debug(f'{self!r}: ===     Instantiating SPI')
                    self.SPI = spi.SPI(router=self.system_router, router_port='SPI', config=self.config.add("spi"))
                else:
                    self.SPI = None

                if self.HAS_I2C:
                    self.logger.debug(f'{self!r}: ===     Instantiating I2C')
                    self.I2C = i2c.I2C(router=self.system_router, router_port='I2C', config=self.config.add("i2c"))
                else:
                    self.I2C = None


                self.logger.debug(f'{self!r}: ===     Instantiating FreqCtr')
                self.FreqCtr = freqctr.FreqCtr(router=self.system_router, router_port='FREQ_CTR', config=self.config.add("freqctr"))

                if self.HAS_REFCLK:
                    self.logger.debug(f'{self!r}: ===     Instantiating REFCLK')
                    self.REFCLK = refclk.REFCLK(router=self.system_router, router_port='REFCLK', config=self.config.add("refclk"))
                else:
                    self.REFCLK = None

                # ---------------------
                # Instantiate F-Engine
                # ---------------------

                self.logger.debug(f'{self!r}: === Instantiating F-Engine')
                # Instantiate a channelizer for for each input
                self.chan = chan.ChanArray(router = self.top_router, router_port = 'CHAN', config=self.config.add("chan"))
                self.CHAN_FMC_NUMBER = [i // 8 for i in range(self.NUMBER_OF_CHANNELIZERS)]
                # self._init_capture_state_tracking()

                await asyncio.sleep(0)


                # ----------------------
                # Instantiate CT-Engine
                # ----------------------

                self.logger.debug(f'{self!r}: === Instantiating CT-Engine, type {self.CT_TYPE} level {self.CT_LEVEL}')


                #  BCT corner-turn (CHIME-like)
                if self.CT_TYPE == "BCT":

                    self.CT = bct_engine.BCTEngine(
                        router=self.top_router,
                        router_port = 'CT',
                        config=self.config.add("ct")
                        )
                # UCT corner-turn
                elif self.CT_TYPE == "UCT":
                    self.CT = uct_engine.UCTEngine(
                        router=self.top_router,
                        router_port = 'CT',
                        config=self.config.add("ct")
                        )
                else:
                    raise RuntimeError(f'Unknown CT-Engine type {self.CT_TYPE}')

                if self.HAS_UPACK:  # ***JFC temp hack to determine if we have UPACK. Not in capability reg yet.
                    self.UPACK = upack.UPACK(
                        router=self.top_router,
                        router_port = 'UPACK',
                        config=self.config.add("upack")
                        )
                else:
                    self.UPACK = None



                # ---------------------
                # Instantiate X-Engine
                # ---------------------

                if self.NUMBER_OF_CORRELATORS:
                    self.logger.debug(f'{self!r}: === Instantiating X-Engine, type={self.CORR_TYPE}')
                    if self.CORR_TYPE == "CORR44":
                        self.CORR = CORR.CORR(router=self.top_router, router_port = 'CORR', config=self.config.add("corr"))
                    elif self.CORR_TYPE == "UCORR44":
                        self.CORR = UCORR.UCORR(router=self.top_router, router_port = 'CORR', config=self.config.add("corr"))
                else:
                    self.CORR = None

                # ---------------------
                # Instantiate real-time data offload engine object (10G or 100G links)
                # ---------------------

                if self.NUMBER_OF_GPU_LINKS:
                    self.logger.debug(f'{self!r}: === Instantiating real-time data offload links, type={self.GPU_LINK_TYPE}')
                    if self.GPU_LINK_TYPE == '10GE':
                        self.GPU = gpu.GPU(router=self.top_router, router_port = 'GPU', config=self.config.add("gpu"))
                    elif self.GPU_LINK_TYPE == '100GE':
                        self.GPU = cge.CGE(router=self.top_router, router_port = 'GPU', config=self.config.add("gpu"))
                    else:
                        raise RuntimeError(f'Unknown data link type {self.GPU_LINK_TYPE}')
                else:
                    self.GPU = None

                # -------------------------------
                # Instantiate global data capture
                # -------------------------------

                if self.CAPTURE_TYPE == 'UCAP':
                    self.logger.debug(f'{self!r}: === Instantiating UCAP')
                    self.UCAP = ucap.UCAP(router=self.top_router, router_port = 'UCAP', config=self.config.add("ucap"))
                elif self.CAPTURE_TYPE !='PROBER':
                    raise RuntimeError(f'Unknown Data capture type {self.CAPTURE_TYPE}')


                # ---------------------------------------------------------------------
                # Instantiate ADC board hardware ressource handlers objects
                # ---------------------------------------------------------------------

                await asyncio.sleep(0)
                self.logger.debug(f'{self!r}: === Analyzing available FMC Mezzanines')
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
                self._tcp_data_socket = None

        except Exception as e:
            self.logger.error(f'****Exception during open!****** =  {e!r}')
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
        """Check if the MMI interface to the FPGA is open

        Returns:
            bool: True if the MMI interface to the FPGA is open, False otherwise
        """
        return bool(self.mmi)

    async def init_async(
            self,
            sampling_frequency=None,
            processing_frequency=None,
            reference_frequency=None,
            adc_clock_divider=None,

            adc_mode=0,
            adc_bandwidth=2,
            adc_delay_table=ADC_DELAY_TABLE,
            data_width=4,
            enable_gpu_link=1,
            create_receiver=False,
            verbose=0,
            **kwargs):
        """
        Initialize the FPGA firmware AND the Python objects to a known state.

        Parameters:

             sampling_frequency (float): Sampling frequency in Hz to set on the ADC Mezzanine
                 boards. If `None`, the platform/mode default is used. (default 800 MHz). Normally
                 `None`, in which case the value is taken from firmware parameter table.

             processing_frequency (float): Frequency of the internally generated channelizer signal
                 processing clock in Hz. Is compared with sampling_frequency/4 to determine if we
                 can use the internal clock instead of the ADC clock to avoid causing large current
                 spikes when we start and stop the ADCs. Is notmally `None`, in which case the
                 platform/mode defaut is used.

             reference_frequency (float): Frequency in Hz of the Iceboard's reference clock (default
                 is 10 MHz). Normally `None`, in which case the value is taken from firmware parameter table.

             adc_delay_table (dict): initial setting of the ADC delays. see `set_adc_delays`.
                 A default delay table is used if none is provided.

             adc_clock_divider (float): ratio between the ADC sampling frequency and the measured
                 ADC clock speed. Is normally `None` to use the platform/mode default.

             data_width (int): 4 or 8. Indicate of the channelizer output is in (4+4)bit or (8+8
                 bit) mode

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
        self.CORR_READOUT_BINS = self.fw_params.get('corr_readout_bins')
        self.FRAME_PERIOD = float(self.FRAME_LENGTH) / self._sampling_frequency
        self.FRAME_RATE = 1 / self.FRAME_PERIOD

        from pychfpga.common.raw_data_packet_processor import PACKET_COOKIE_ADC, PACKET_COOKIE_FFT
        bytes_per_frame = int(self.ADC_BYTES_PER_SAMPLE * self.ADC_SAMPLES_PER_FRAME)
        self.config.update(
            capture_engine=self.CAPTURE_TYPE,
            adc_bits_per_sample=int(self.ADC_BITS_PER_SAMPLE),
            adc_bytes_per_sample=int(self.ADC_BYTES_PER_SAMPLE),
            capture_bus_width=int(self.CAPTURE_BUS_WIDTH), # width of the data capture bus from FUNCGEN/SCALER into the capture engine
            samples_per_frame=int(self.ADC_SAMPLES_PER_FRAME),
            bytes_per_frame=bytes_per_frame,
            fft_bins_per_word=int(self.FFT_BINS_PER_WORD),
            capture_bits_per_word=128 if self.CAPTURE_TYPE == "UCAP" else 32,
            subframes_per_frame=4 if self.CAPTURE_TYPE == "UCAP" else 1,
            packet_header_format="raw-v1",
            packet_cookie=[PACKET_COOKIE_ADC, PACKET_COOKIE_FFT], # cookies for both data sources
            sample_freq_mhz=float(self._sampling_frequency) / 1e6,
            platform_id=int(self.PLATFORM_ID),
            channels=tuple(self.default_channels),
        )
        self.logger.debug(f'{self!r}: --- Initializing FPGA subsystems')

        self.logger.debug(f'{self!r}: sampling_frequency={self._sampling_frequency}')

        self.logger.debug(f'{self!r}: --- Initializing GPIO')

        with self.mmi.aggregate_commands():

            # Initialize GPIO.
            #
            # This stops the channelizers from sending data. Needed if the FPGA
            # is flooding the buffers which prevent subsequent reads to come
            # through
            # GPIO module is already initialized
            self.logger.debug(f'{self!r}: --- Setting buck phases')
            # await asyncio.sleep(1.3)  # JFC debug.
            self.GPIO.BUCK_PHASE = 0xfedcba9876543210  # debug
            self.logger.debug(f'{self!r}: --- Done setting buck phases. We survived this!')
            if verbose >= 2:
                self.logger.debug(f'{self!r}: --- Checking GPIO status')
                self.GPIO.status()


            # Module depend on the FMC_present flag after this point
            if self.REFCLK:
                self.logger.debug(f'{self!r}: --- Initializing REFCLK')
                await asyncio.sleep(0)
                self.REFCLK.init()
                # self.REFCLK.status()

            # Only do for ML605, not KC705 board
            self.logger.debug(f'{self!r}: --- Initializing SYSMON')
            await asyncio.sleep(0)
            self.SYSMON.init()
            # self.SYSMON.status()

            if self.SPI:
                self.logger.debug(f'{self!r}: --- Initializing SPI')
                await asyncio.sleep(0)
                self.SPI.init()
                # self.SPI.status()

            self.logger.debug(f'{self!r}: --- Initializing FMC slots')

            # Reduce the power load before we turn on the mezzanines
            await asyncio.sleep(0)
            if self.HAS_ADCDAQ:
                self.set_adc_mask(0)  # null the ADC data before it gets to the channelizers to reduce power consumption
            self.set_chan_reset(1)
            self.set_corr_reset(1)
            for mezz in self.mezzanine.values():
                await mezz.set_mezzanine_power_async(False)
            if self.mezzanine:
                await asyncio.sleep(0.2)  # let mezzanine supplies collapse before power-up

            for mezz_number in self.mb.FMC_MEZZ_NUMBERS: # process every mezzanine, present or not
                if mezz_number in self.mezzanine: # if mezzanine pressent
                    mezz = self.mezzanine[mezz_number]
                    self.logger.debug(f'{self!r}:   Powering down FMC{mezz_number - 1}')
                    await mezz.set_mezzanine_power_async(False)   # this method uses power-sequencing
                    await mezz.set_mezzanine_reset_async(True)   # Puts the ADCs in reset

                    await asyncio.sleep(0.2)  # *** make async
                    self.logger.debug(f'{self!r}:   Powering up FMC{mezz_number}')
                    await mezz.set_mezzanine_power_async(True)
                    # mezz.set_power(True)
                    await asyncio.sleep(0.2)  # Give it some time for the power to stabilize
                    # We need to initialize the ADC board before we initialize the channelizer
                    # (and its data acquisition) because the delay blocks need a
                    # clock
                    self.logger.debug(f'{self!r}:   Initializing FMC{mezz_number}')
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
                    self.logger.debug(f'{self!r}:    Skipping FMC{mezz_number - 1} initialization since no board is present in that slot')
            # self.set_sync_delays((0,0))  # debug
            await self.sync_async()  # pulse the sync lines of the mezzanines to activate the ADC configurations
            #self.set_sync_delays((4,4))  # debug
            #self.sync()  # debug
            await self.check_adc_frequencies_async('after 1st post-mezz-init SYNC (chan reset active) ')


            # self.logger.debug('%r:   Taking channelizers out of reset after FMC enabling' % (self))

            self.set_chan_reset(1)

            self.logger.debug(f'{self!r}:   Sending sync()')
            await asyncio.sleep(0)
            await self.sync_async()  # might be needed  to make sure that the clock is running to set delays
            await self.check_adc_frequencies_async('after 1st channelizer reset SYNC')

            # self.logger.info(f'{self!r}: --- Initializing FPGA subsystems' % self)
            self.logger.debug(f'{self!r}: === Initializing Channelizers')
            self.chan.init(delay_table=adc_delay_table, fmc_present=self.ANT_FMC_IS_PRESENT)
            # self.chan.status()

            await self.check_adc_frequencies_async('after channelizer init')


            self.logger.debug(f'{self!r}: === Initializing Corner-Turn engine, Type {self.CT_TYPE} Level {self.CT_LEVEL}')

            if not self.CT:
                raise RuntimeError('    The firmware does not have a corner-turn engine')
            else:
                self.CT.init()


            if self.UPACK:
                self.logger.debug(f'{self!r}: === Initializing UPACK frame packetizer')
                self.UPACK.init()



            await self.check_adc_frequencies_async('after corner-turn init')

            # -------------------
            # Initialize X-Engine
            # -------------------

            if self.CORR:
                self.logger.debug(f'{self!r}: === Initializing FPGA-based correlator (X-Engine)')
                await asyncio.sleep(0)
                self.logger.debug(f'{self!r}:  - CORR')
                self.CORR.init()
                self.set_offset_binary_encoding(self.CORR.REQUIRES_OFFSET_BINARY_ENCODING)
            else:
                self.logger.debug(f'{self!r}: There are no FPGA correlators in this firmware build')



            self.set_data_width(data_width)  # Sets the data width of both the Channelizer(SCALER) output and Corner-turn engine
            self.logger.debug(f'{self!r}: Data width set to (Re+Im) = ({self.get_data_width()}+{self.get_data_width()}) bits')

            if not self.CT:
                self.logger.debug(f'{self!r}: There is no Corner-turn engine in this firmware build')

            await asyncio.sleep(0)

            # Initialize real-time data offload engine (10G or 100G links)
            if self.GPU:
                self.logger.debug(f'{self!r}: === Initializing data offload links')
                self.GPU.init()
                self.GPU.set_enable(enable_gpu_link)
                self.logger.debug(f"{self!r}: Data offload links are currently {('Disabled', 'Enabled')[bool(enable_gpu_link)]}")

                # Enable high-power mode if transceiver declares Power Class 5-8
                if hasattr(self.mb, 'i2c1_qsfp'):

                    self.logger.debug(f'{self!r}: QSFP module is present: {self.mb.i2c1_qsfp.is_present()}')

                    # Enable high-power mode if transceiver declares Power Class 5-8,
                    # and disable forward error correction (FEC)
                    try:
                        if self.mb.i2c1_qsfp.get_power_class() >= 5:
                            self.mb.enable_qsfp_high_power_mode() # Enable high-power mode
                            self.mb.GPU.set_fec(False) # Disable FEC

                            # Double-check that high-power mode is enabled and FEC disabled
                            if self.mb.i2c1_qsfp.get_power_mode != 1:
                                self.logger.warning(f'{self!r}: Attempted to enable high-power mode on {self.mb}, but it is still enabled.')
                            if self.mb.GPU.CTL_TX_RSFEC_ENABLE != 0:
                                self.logger.warning(f'{self!r}: Attempted to disable FEC on {self.mb}, but it is still enabled.')
                    except Exception as e:
                        raise Exception(f'Attempted to set high-power mode and disable FEC on QSFP link for {self.mb}, but high-power mode was not enabled. The error is {e}.')

                    # If high-power mode is enabled, disable FEC
                    if self.mb.i2c1_qsfp.get_power_mode() == 1:
                        self.mb.GPU.set_fec(False)

            if self.CAPTURE_TYPE == 'UCAP':
                self.logger.debug(f'{self!r}: === Initializing UCAP')
                self.UCAP.init()


            self.logger.debug(f"{self!r}: Done with initializations.")

            await asyncio.sleep(0)
            self.set_chan_reset(0)  # Disable channelizer reset

            await self.check_adc_frequencies_async('after final channelizer reset release')


            self._last_init_time = time.time()

            # Create a data socket. The socket will be cached for future use. This also sets the destination address/port for data streams in the FPGA.
            # self.get_data_socket()

    def _report_adc_frequencies(self, freqs, stage, trial):
        """Log one ADC-clock check trial. Returns True if any clock is out of spec."""
        target_frequency = self._sampling_frequency / self.adc_clock_divider
        err = any(abs(f - target_frequency) > 2.1e3 for f in freqs)
        msg = f'{self!r}: ADC output frequencies at stage {stage} are {[f/1e6 for f in freqs]} (check #{trial+1}) {"ERROR!" if err else ""}'
        if err:
            self.logger.warning(msg)
            self.adc_clk_err_ctr += 1
            self.adc_clk_err_msgs.append(msg)
        else:
            self.logger.debug(msg)
        return err

    def _finish_adc_frequency_check(self, err, freqs, stage):
        """Log a final error if clocks never matched. Returns ``err``."""
        if err:
            target_frequency = self._sampling_frequency / self.adc_clock_divider
            msg = f'{self!r}: some ADCs are not generating a proper clock at stage "{stage}". Frequencies are {[f/1e6 for f in freqs]} MHz. ' \
                  f'Expected frequency is {target_frequency/1e6:.6f} MHz. Deltas = {[(f-target_frequency)/1e6 for f in freqs]}'
            self.logger.error(msg)
        return err

    def check_adc_frequencies(self, stage):
        """ Check the frequencies of the ADC clocks 

        This method is called at various stages of the initialization to check the frequencies of the ADC clocks. This catches issues of  IceBoard ADC mezzanines sometimes erratically losing their clocks after the initial power-up. On CRS it confirms the PLL / RFDC output clocks.

        Parameters:
            stage (str): The stage of the initialization to check the ADC frequencies for. This string is used ot identify where the check is being performed in the log messages.

        Returns:
            bool: True if any ADC frequency is out of spec, False otherwise.

        Note:
            Prefer :meth:`check_adc_frequencies_async` from async code so the
            frequency-counter gate wait yields to other boards.
        """
        # TODO: disable if mezzanine is not attached
        # if self.mezzanine.atta
        err = True
        freqs = []
        for trial in range(10):
            freqs = [self.FreqCtr.read_frequency(f'ADC_CLK{i}', gate_time=0.001) for i in self.ADC_FREQS_TO_CHECK]
            err = self._report_adc_frequencies(freqs, stage, trial)
            if not err:
                break
        return self._finish_adc_frequency_check(err, freqs, stage)

    async def check_adc_frequencies_async(self, stage):
        """Async version of :meth:`check_adc_frequencies`."""
        # TODO: disable if mezzanine is not attached
        err = True
        freqs = []
        for trial in range(10):
            freqs = [
                await self.FreqCtr.read_frequency_async(f'ADC_CLK{i}', gate_time=0.001)
                for i in self.ADC_FREQS_TO_CHECK]
            err = self._report_adc_frequencies(freqs, stage, trial)
            if not err:
                break
        return self._finish_adc_frequency_check(err, freqs, stage)

    ###################################
    # Core registers access methods
    ###################################
    """
    Core registers is a set of 32-bit registers that can be
    accessed both through the BSB  interface AND, for some platforms, an external SPI link.

    When using the BSB interface, the core registers are mapped as a RAM page for the GPIO module.

    On platforms that use direct FPGA UDP for BSB (UltraScale+ USP stack, MGK7MB today),
    SPI from the on-board processor may be required to configure networking before the UDP MMI link works.
    """

    def fpga_core_reg_read(self, addr):
        """Reads the contents of a 32-bit core register at the specified address.

        Blocking MMI access. Use this from other blocking helpers (e.g. IRIG
        sequences run under ``asyncio.to_thread``). Async callers should use
        :meth:`fpga_core_reg_read_async`.

        Parameters:

           addr (int): address of the 32-bit core register. Must be a multiple of 4.

        Returns:

           int: 32-bit unsigned value of the register
        """
        if not self.mmi:
            raise IOError('Attempted to read FPGA core registers before MMI is initialized')
        return int(self.mmi.read(self.mmi._RAM_BASE_ADDR + addr, type='<u4', length=1)[0]) & 0xFFFFFFFF

    def fpga_core_reg_write(self, addr, value):
        """Writes a 32-bit value to the core register at the specified address.

        Blocking MMI access. See :meth:`fpga_core_reg_read`.

        Parameters:

            addr (int): address of the 32-bit core register. Must be a multiple of 4.

            value (int): 32-bit value to write in the register
        """
        if not self.mmi:
            raise IOError('Attempted to write FPGA core registers before MMI is initialized')
        self.mmi.write(self.mmi._RAM_BASE_ADDR + addr, value.to_bytes(4, 'little'))

    async def fpga_core_reg_read_async(self, addr):
        """Reads the contents of a 32-bit core register at the specified address.

        Async wrapper around :meth:`fpga_core_reg_read`. The body is blocking
        MMI; ``async`` exists so other async firmware methods can ``await`` it.
        Tuber does not override this — IceBoard SPI access before UDP MMI is up
        is :meth:`~pychfpga.hardware.ice.iceboard.IceBoard.fpga_core_reg_spi_read_async`.

        Parameters:

           addr (int): address of the 32-bit core register. Must be a multiple of 4.

        Returns:

           int: 32-bit unsigned value of the register
        """
        return self.fpga_core_reg_read(addr)

    async def fpga_core_reg_write_async(self, addr, value):
        """Writes a 32-bit value to the core register at the specified address.

        Async wrapper around :meth:`fpga_core_reg_write`. See :meth:`fpga_core_reg_read_async`.

        Parameters:

            addr (int): address of the 32-bit core register. Must be a multiple of 4.

            value (int): 32-bit value to write in the register
        """
        self.fpga_core_reg_write(addr, value)

    ###################################
    # Bitstream management methods
    ###################################

    async def get_fpga_core_cookie_async(self):
        """ Return the core chFPGA firmware cookie. Should always be 0xBEEFFACE.

        The cookie is obtained from a core register via the BSB interface.

        Returns:
            int: The core chFPGA firmware cookie. Should always be 0xBEEFFACE.
        """
        if not (await self.mb.is_fpga_programmed_async()):
            return None
        cookie = await self.fpga_core_reg_read_async(self.FPGA_CORE_FIRMWARE_COOKIE_ADDR)
        return cookie

    async def get_fpga_application_cookie_async(self):
        """ Return the application-specific FPGA firmware cookie.

        The cookie is obtained from a core register via the MMI interface.

        Returns:
            int: The application-specific FPGA firmware cookie.
        """
        if not (await self.mb.is_fpga_programmed_async()):
            return None
        cookie = await self.fpga_core_reg_read_async(self.FPGA_APPLICATION_FIRMWARE_COOKIE_ADDR)
        return cookie

    async def get_fpga_serial_number_async(self):
        """ Return the FPGA serial number, as read from the FPGA's core
        register through the MMI interface.
        """
        fpga_serial_number = (
            (await self.fpga_core_reg_read_async(self.FPGA_SERIAL_NUMBER_LSW_ADDR)) |
            (await (self.fpga_core_reg_read_async(self.FPGA_SERIAL_NUMBER_MSW_ADDR) << 32))
            )

        return fpga_serial_number

    async def get_fpga_firmware_timestamp_async(self):
        """ Returns a string containing the date-time of the currrent firmware
        bitstream.

        The value is retreived from the FPGA firmware core register that exposes the
        USER_ACCESS field embedded in the firmware, which is set in Vivado to
        encode the bitstream generation time.

        Returns:
            A string containing the date-time of the current firmware bitstream.
        """
        timestamp = await self.fpga_core_reg_read_async(self.FPGA_FIRMWARE_TIMESTAMP_ADDR)
        seconds = (timestamp >> 0) & 0x3F
        minutes = (timestamp >> 6) & 0x3F
        hour = (timestamp >> 12) & 0x1F
        year = (timestamp >> 17) & 0x3F
        month = (timestamp >> 23) & 0x0F
        day = (timestamp >> 27) & 0x1F
        timestamp_string = f'{int(year + 2000):04}-{int(month):02}-{int(day):02} {int(hour):02}:{int(minutes):02}:{int(seconds):02}'
        return timestamp_string



    async def ping_fpga_async(self, timeout=0.3):
        """ Check if the FPGA firmware is responsive by attempting to perform a BSB read on a known register.

        Note: This does not check is the platform is visible on the network: that is a prerequisite. This rather checks if the FPGA firmware is configured and the FPGA's BSB bus is accessible through the UDP/IP network or TCPIPE interface, depending on the platform.

        Parameters:
            timeout (float): The timeout in seconds to wait for the FPGA to respond.

        Returns:
            True if the FPGA is responsive, False otherwise.
        """
        # Open the core right now if needed so we don't mask IOError exceptions this could generate
        if not self.mb._is_core_open():
            await self.open_core()
        try:
            self.mmi.read(self.mmi._STATUS_BASE_ADDR + self._GPIO_COOKIE_ADDR, type=int, length=1, timeout=timeout)
            return True
        except IOError:
            return False

    async def check_command_count_async(self, reset=False):
        """Check BSB/MMI command/reply counters against FPGA GPIO tallies.

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
                        f'{self!r}: Command counters differ cmd/rply in FPGA is ({int(cmd)}, {int(rply)}), '
                        f'Python MMI is ({int(self.mmi.send_counter & 255)}, {int(self.mmi.recv_counter & 255)})')
                if reset:
                    self.mmi.send_counter = cmd
                    self.mmi.recv_counter = rply
                    valid = True
                break
            except IOError as e:
                self.logger.error(
                    f"{self!r}: BSB/MMI communication error while checking command counters "
                    f"(trial {int(trial + 1)}). The error is:\n {e}")
                if self.BSB_TRANSPORT == self.BSB_TRANSPORT_UDP:
                    await self.reset_fpga_udp_stack()
                valid = False
                reset = True
            except Exception as e:
                self.logger.error(
                    f"{self!r}: Unhandled error during command counter check. The error is:\n {e!r}")
                raise
        else:  # executes if we exhausted the for loop iterations, i.e  no break
            errmsg = f"{self!r}: Could not re-establish UDP communinication with the FPGA. Raising an exception"
            self.logger.error(errmsg)
            raise IOError(errmsg)
        return valid


    async def set_local_data_port_number_async(self, port):
        """
        Sets the port number to which the FPGA sends data-channel packets to the host.

        This method uses MMI interface to access the FPGA core registers.

        Parameters:

            port (int); target port number. If port==0, the data is sent to
                the source port number of the last received valid packet  + 1.

        Does not change the target MAC or IP address.
        """
        if self.BSB_TRANSPORT == self.BSB_TRANSPORT_UDP:
            await self._set_local_udp_data_port_number_async(port)
        elif self.BSB_TRANSPORT == self.BSB_TRANSPORT_TCPIPE:
            self.mb.tcpipe.set_local_data_port_number(port)
        else:
            raise RuntimeError(f'{self!r}: No UDP or TCPipe path for local data port')

    async def get_local_data_port_number_async(self):
        """Return the UDP data-channel destination port (FPGA core map or TCPipe register)."""
        if self.BSB_TRANSPORT == self.BSB_TRANSPORT_UDP:
            return await self._get_local_udp_data_port_number_async()
        if self.BSB_TRANSPORT == self.BSB_TRANSPORT_TCPIPE:
            return self.mb.tcpipe.get_local_data_port_number()
        raise RuntimeError(f'{self!r}: No UDP or TCPipe path for local data port')

    async def set_udp_data_target_address_async(self, ip_addr=None, port=None, mac_addr=None):
        """Set the destination MAC/IP/port for the UDP data stream sent by the FPGA.

        On ``BSB_TRANSPORT_UDP``, programs FPGA core registers via
        :meth:`_set_udp_data_target_address_async`. On ``BSB_TRANSPORT_TCPIPE``,
        MAC and IP are managed by the platform; only ``port`` is programmed via
        :meth:`set_local_data_port_number_async`.

        Parameters:
            ip_addr (str): The IP address of the data target (UDP path only).
            port (int): The port number of the data target.
            mac_addr (str): The MAC address of the data target (UDP path only).
        """
        if self.BSB_TRANSPORT == self.BSB_TRANSPORT_TCPIPE:
            if port is not None:
                self.logger.info(
                    f'{self!r}: setting UDP data destination port to {port} via TCPipe '
                    f'(MAC/IP managed by platform; ip={ip_addr!r}, mac={mac_addr!r} ignored)')
                await self.set_local_data_port_number_async(port)
            else:
                self.logger.warning(
                    f'{self!r}: set_udp_data_target_address_async called without port on '
                    f'TCPipe platform; destination port unchanged.')
            return

        if self.BSB_TRANSPORT == self.BSB_TRANSPORT_UDP:
            await self._set_udp_data_target_address_async(
                ip_addr=ip_addr, port=port, mac_addr=mac_addr)
            return

        self.logger.warning(
            f'{self!r}: Setting the target MAC & IP address for the UDP data channel is not currently '
            f'supported by this platform (it is likely managed by the platform). The request will be ignored. Make sure the UDP receiver '
            f'uses the correct address and port for this platform.')

    # ---------------------------------------------------------
    # IceBoard / mezzanine helpers (GPIO path, I2C — not the USP UDP stack)
    # ---------------------------------------------------------

    async def get_fpga_firmware_cookie_async(self, resync=False):
        """
        Get the FPGA firmware cookie.

        Reads the FPGA over the BSB (UDP MMI or TCPipe, depending on platform) and returns the cookie that
        identifies the firmware. This method can be called before any FPGA
        modules are instatiated.

        If ``resync`` is True, the read command will reset the command
        sequence number to the value known by the FPGA. This should be is used
        by the first command sent to the FPGA to reset the communication link.
        """
        await asyncio.sleep(0)
        return self.mmi.read(self.mmi._STATUS_BASE_ADDR + self._GPIO_COOKIE_ADDR, type=int, length=1, resync=resync) & 0x7F

    def get_fpga_firmware_version(self):
        """
        Returns the firmware revion currenting running on the FPGA (which si
        the date and time of bitstream generation)

        Returns:
            str: The firmware revision of the current firmware bitstream.
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

        Parameters:
            *args: Variable length argument list.
            **kwargs: Arbitrary keyword arguments.

        Returns:
            list: The data read from the I2C device.
        """
        return self.I2C.write_read(*args, **kwargs)

    def toto(self, a,b,c, aa=10, bb=20, cc=30):
        pass

        toto(1,2,3, aa=10, bb=20, cc=30)

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
            return f'{int(n.year):04}-{int(n.month):02}-{int(n.day):02}T{int(n.hour):02}:{int(n.minute):02}:{n.second + self.nano % 1000000000 / 1000000000.0:02.9f}'  # See note above

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

    async def set_irigb_source_async(self, source, inv_pol=False):
        """
        Set the source of the IRIG-B signal. Also configures the user SMA as
        an input if that SMA is used as a source.

        Parameters:

            source (str): Name of the source to use. If `source` is prefixed by ``~`` or ``!``, `inv_pol` is forced to True.

            inv_pol (bool): If True, the polarity of the selected IRIG-B source is inverted

        """
        if source.startswith('~') or source.startswith('!'):
            inv_pol = True
            source = source[1:]

        if source not in self._IRIGB_SOURCE_TABLE:
            source_list = ', '.join(self._IRIGB_SOURCE_TABLE.keys())
            raise ValueError(f'Invalid IRIG-B source name. Valid names are {source_list}')

        sel = (source, bool(inv_pol))
        if getattr(self, '_current_irigb_source_sel_pol', None) == sel:
            return

        self.logger.debug(f'{self!r}: Setting IRIG-B source to "{source}", invert={inv_pol}')

        src = self._IRIGB_SOURCE_TABLE[source]

        def _mmi(src=src, inv_pol=inv_pol):
            w2 = self.fpga_core_reg_read(self._IRIGB_TARGET2_ADDR)
            self.fpga_core_reg_write(
                self._IRIGB_TARGET2_ADDR, (w2 & ~(1 << 29)) | (int(inv_pol) << 29))

            w2 = self.fpga_core_reg_read(self._IRIGB_SAMPLE2_ADDR)
            self.fpga_core_reg_write(
                self._IRIGB_SAMPLE2_ADDR, (w2 & 0x3FFFFFFF) | ((src & 0b011) << 30))
            w2 = self.fpga_core_reg_read(self._IRIGB_TARGET0_ADDR)
            self.fpga_core_reg_write(
                self._IRIGB_TARGET0_ADDR, (w2 & 0x7FFFFFFF) | ((src >> 2) << 31))

            w2 = self.fpga_core_reg_read(self._IRIGB_SAMPLE2_ADDR)
            self.fpga_core_reg_write(self._IRIGB_SAMPLE2_ADDR, w2 & ~(1 << 29))
            self.fpga_core_reg_write(self._IRIGB_SAMPLE2_ADDR, w2 | (1 << 29))

        await asyncio.to_thread(_mmi)
        self._current_irigb_source_sel_pol = sel

        # If an user SMA is used, configure it as an input
        if source in self.GPIO.USER_OUTPUTS:
            self.set_user_output_source(output=source, source='input')


    async def get_irigb_source_async(self):
        """ Get the name of the current source of the IRIG-B signal."""
        def _mmi():
            w1 = self.fpga_core_reg_read(self._IRIGB_SAMPLE2_ADDR)
            w2 = self.fpga_core_reg_read(self._IRIGB_TARGET0_ADDR)
            return w1, w2

        w1, w2 = await asyncio.to_thread(_mmi)

        source = ((w1 >> 30) & 0b011) | (((w2 >> 31) & 0b001) << 2)

        for (source_name, source_number) in self._IRIGB_SOURCE_TABLE.items():
            if source == source_number:
                return source_name
        raise ValueError(f'The IRIG-B module has an unknown source number {int(source)}')

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

    def _program_irigb_trigger_sync(self, y, d, h, m, s, ss):
        """Program the IRIG compare target in one blocking burst."""
        # disable time comparison to prevent false triggers during setup
        t2 = self.fpga_core_reg_read(self._IRIGB_TARGET2_ADDR)
        t2 |= (1 << 31) | (1 << 30)  # Enable compare enable bit, but force output to be 1 (i.e. we are before trig time) to prevent false trigger. We change noting else to prevent a false trigegr.
        self.fpga_core_reg_write(self._IRIGB_TARGET2_ADDR, t2)

        t0 = self.fpga_core_reg_read(self._IRIGB_TARGET0_ADDR) & 0xFFFFFF00
        t0 |= (y << 0)
        t1 = (d << 20) | (h << 14) | (m << 7) | (s << 0)
        t2 = (t2 & ~((1 << 28) - 1)) | (ss << 0)  # Set subsecond target. comparator is enabled, but still forcing output to 1

        self.fpga_core_reg_write(self._IRIGB_TARGET0_ADDR, t0)
        self.fpga_core_reg_write(self._IRIGB_TARGET1_ADDR, t1)
        self.fpga_core_reg_write(self._IRIGB_TARGET2_ADDR, t2)

        # now re-enable time comparator
        t2 ^= (1 << 30)  # Toggle force bit back to zero.
        self.fpga_core_reg_write(self._IRIGB_TARGET2_ADDR, t2)

    async def _get_irigb_time_async(self, trig=True, noerror=False):
        """Read IRIG time; retry sleeps on the event loop, not in a worker."""
        t0 = time.time()
        while True:
            ts = await asyncio.to_thread(
                self._decode_irigb_time, trig, True, False)
            if ts is not None:
                hms_valid = not (ts.h > 23 or ts.m > 59 or ts.s > 60)
                day_valid = not (ts.d < 1 or ts.d > 366)
                if ts.recent and hms_valid and day_valid:
                    return ts
                self.logger.debug(
                    f'{self!r}: Waiting for valid IRIG-B time. '
                    f'(recent={ts.recent}, HMS valid={hms_valid}, '
                    f'day valid={day_valid})')
            elif trig:
                self.logger.debug(
                    f'{self!r}: Waiting for valid IRIG-B time. (recent=0)')
            if not trig or time.time() - t0 > 2.5:
                if noerror:
                    return None
                raise RuntimeError(
                    f'{self!r}: Could not get a recently updated and valid '
                    f'IRIG-B time. Check your cabling and the IRIG-B source '
                    f'selection.')
            await asyncio.sleep(0.1)

    def _decode_irigb_time(self, trig=True, noerror=False, wait=True):
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

            wait (bool): If True (default) and ``trig`` is True, retry for up
                to 2.5 s until ``recent`` and the HMS/day fields are valid.
                If False, make a single capture. Async callers should pass
                False and sleep on the event loop between attempts.


        Returns:

            an _IrigTimestamp object which contains the captured.


        Notes:

            The retuned time is advanced by one second to account for the fact
            that the IRIG-B time decode by the IRIG-B FPGA logic is latched on the
            beginning of the following second. However, the pipelining delay
            offsets not included.


        """

        ts = self._IrigTimestamp()

        # Optionally trigger time capture, and check that the IRIG-B time is
        # captured AND to be valid. IF we trig, try to get a valid time until
        # a timeout has elapsed, otherwise fail immediately if the time was
        # not recently updated.
        t0 = time.time()
        while True:

            ts.system_time_before = time.time()
            if trig:
                w2 = self.fpga_core_reg_read(self._IRIGB_SAMPLE2_ADDR)
                # trigger time capture by creating a rising edge on `refclk_sample_trig`
                self.fpga_core_reg_write(self._IRIGB_SAMPLE2_ADDR, w2 & ~(1 << 29))
                self.fpga_core_reg_write(self._IRIGB_SAMPLE2_ADDR, w2 | (1 << 29))
                # Wait for the time capture to complete by monitoring
                # `refclk_sample_done`. Time is captured on the next 10 MHz
                # reference clock edge, so that should be quick, but we need
                # to make sure we have stable values. Timeout if it takes too long.
                #
                # JFC: Commented out  until I fix the firmware
                # t1 = time.time()
                # while not (self.fpga_core_reg_read(self._IRIGB_TARGET1_ADDR) & (1 << 29)):
                #     self.logger.warning('%r: Time capture was not immediately ready - this is unexpected' % self)
                #     if time.time() - t1 > 0.1:
                #         raise RuntimeError(
                #            'Timeout while waiting for the reference clock counter '
                #            'and IRIG-B time capture to complete. Was the capture triggered?')
            # Capture is on the next 10 MHz edge after CDC (plus ~2 clk200 for
            # the IRIG snapshot). That is microseconds. The first SAMPLE0 read
            # below is a full TCPipe round trip later, so the registers are
            # stable by then. ``refclk_sample_done`` (TARGET1 bit 29) is the
            # proper handshake, but it is not polled (JFC: commented out until
            # the firmware bit is fixed). ``recent`` is separate: it means the
            # decoder has a valid frame, not that the capture latch has settled.
            w0 = self.fpga_core_reg_read(self._IRIGB_SAMPLE0_ADDR)
            w1 = self.fpga_core_reg_read(self._IRIGB_SAMPLE1_ADDR)
            w2 = self.fpga_core_reg_read(self._IRIGB_SAMPLE2_ADDR)
            ts.refclk_counter = self.fpga_core_reg_read(self._IRIGB_REFCLK_SAMPLE)

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
            ts.recent = (w1 >> 29) & 1
            ts.system_time = time.time()
            hms_valid = not (ts.h > 23 or ts.m > 59 or ts.s > 60)
            day_valid = not (ts.d < 1 or ts.d > 366)
            # If we get a updated and valid time, we're good: exit the loop
            if ts.recent and hms_valid and day_valid:
                break

            # Consider we failed if we wither 1) don't expect the time to be updated (i.e no trig)
            # or 2) a timeout has elapsed. Teh timeout  is set a little bit more than one second
            # in case the IRIG-B signal just became valid (e.g. we just set the source or polarity)
            if not wait or not trig or time.time() - t0 > 2.5:
                if noerror:
                    return None
                else:
                    raise RuntimeError(
                        f'{self!r}: Could not get a recently updated and valid IRIG-B time. '
                        f'Check your cabling and the IRIG-B source selection.')
            else:
                self.logger.debug(f'{self!r}: Waiting for valid IRIG-B time. (recent={ts.recent}, HMS valid={hms_valid}, day valid={day_valid})')
                time.sleep(0.1)  # wait a bit before rechecking time

        # check if the time is valid
        if not noerror:
            if not hms_valid:
                raise RuntimeError(f'Invalid IRIG-B time value {int(ts.h)}h {int(ts.m)}m {int(ts.s)}s.')

            if not day_valid:
                raise RuntimeError(f'Invalid IRIG-B day value {int(ts.d)}. Day-of-year must be between 1 and 366')

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
        # elements including fractional microseconds
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

        # Validate that the different ways of computing 
        # the times agree with each other
        if not (ts.nano == ts.nano2 and
            ts.time == ts.time2 and
            ts.time_struct == ts.time_struct2):
            self.logger.error("%r: IRIGB time computation error" % self)

        # # Last thing: apply an offset to correct for GPS rollover time
        # # if a given GPS receiver has not had its firmware updated.
        # # The GPS rollover time is 1024 weeks. Recall that epoch 0 ns = Jan. 1 1970 UTC
        # # Start by defining the GPS epoch, Jan. 6 1980 UTC, with respect to UNIX epoch as a datetime object
        # gps_t0_datetime_utc = datetime(year = 1980, month = 1, day = 6, tzinfo = timezone.utc)

        # # Next, add a timedelta of n*1024, where n is an integer referring to the epoch we are in (1980, 1999, 2019, ...) 
        # epochs = np.arange(5) # brings us to July 7, 2058
        # gps_epochs_t0_datetime = [gps_t0_datetime_utc + timedelta(weeks = int(n*1024)) for n in epochs] # GPS time 0 for each epoch, datetime object
        # gps_epochs_t0_s = [int(gps_epochs_t0_datetime[n].timestamp()) for n in epochs] # GPS time 0 for each epoch, seconds
        
        # # Finally, check the system time, and apply the offset to bring the IRIG-B time into the correct epoch
        # # First, get the GPS epoch index for the system by determining the last epoch where the system time 
        # # is greater than the epoch t0
        # system_gps_epoch = int(np.where([ts.system_time > gps_epochs_t0_s[n] for n in epochs])[0][-1]) 

        # # Next, get the GPS epoch index for the GPS receiver
        # gps_receiver_gps_epoch = int(np.where([dt.timestamp() > gps_epochs_t0_s[n] for n in epochs])[0][-1])

        # if gps_receiver_gps_epoch != system_gps_epoch:
        #     # Iterate through and update each attribute of _IrigTimestamp accordingly
        #     n_epochs_to_add = system_gps_epoch - gps_receiver_gps_epoch 

        #     self.logger.debug('Difference in GPS epoch detected between GPS receiver and system. Correction needed.')
        #     self.logger.debug(f'GPS receiver time before correction: {dt}')
        #     self.logger.debug(f'System time: {datetime.fromtimestamp(ts.system_time)}')
        #     self.logger.debug(f'GPS receiver\'s GPS epoch is {gps_receiver_gps_epoch} (starting at {gps_epochs_t0_datetime[gps_receiver_gps_epoch]}), but system\'s GPS epoch is {system_gps_epoch} (starting at {gps_epochs_t0_datetime[system_gps_epoch]}). Adding {n_epochs_to_add*1024} weeks to IRIG-B time.')
            
        #     if n_epochs_to_add < 0:
        #         raise NotImplementedError('GPS receivers time-traveled from the future to the present not supported.')

        #     # Start with the datetime object, to which we can simply add a timedelta 
        #     ts.datetime = dt = ts.datetime + timedelta(weeks = n_epochs_to_add*1024)

        #     # Then, use the datetime object to recover year, month, day, etc.
        #     ts.y = dt.year
        #     ts.d = dt.day
        #     ts.h = dt.hour
        #     ts.m = dt.minute
        #     ts.s = dt.second
            
        #     # ts.ss and ts.pps don't need to be updated, 
        #     # since we're only adding a number of weeks
        #     # ts.pps
        #     # ts.sbs

        #     ts.time = dt.timestamp()
        #     ts.nano = int(ts.time*1e9)
        #     ts.time_struct = [dt.year, dt.month, dt.day, dt.hour, dt.minute,
        #                       dt.second, (ts.nano2 % 1000000000) / 1000.0]

        #     # We can assign the second time values to the first,
        #     # since we already checked that they were the same 
        #     # before adding the GPS epoch offset
        #     ts.time2 = ts.time
        #     ts.nano2 = ts.nano
        #     ts.time_struct2 = ts.time_struct
            
        #     # # "straight binary seconds" since 00:00 on the current day (0-86399,
        #     # # not BCD). Not necessarily supported by the GPS.
        #     # ts.sbs = (w0 >> 8) & ((1 << 18) - 1)
        #     # ts.source = (w1 >> 30) & ((1 << 2) - 1)
        #     # ts.recent = (w1 >> 29) & 1
        #     # ts.system_time = time.time()
        #     # ts.refclk_counter = await self.fpga_core_reg_read_async(self._IRIGB_REFCLK_SAMPLE)

        #     self.logger.debug(f'GPS receiver time after correction: {dt}')
            
        return ts

    def _set_irigb_trigger_time_sync(self, datetime_=None, delay=None):
        """Blocking IRIG trigger arm. Safe to run in a worker thread per board."""
        if datetime_ is None:
            ts = self._decode_irigb_time(trig=True)
            dt = ts.astype('datetime')
            self.logger.debug(f'{self!r}: Current IRIGB time is {ts.isoformat()}')
            if delay is None:
                delay = 3
        else:
            dt = datetime_
            if delay is None:
                delay = 0

        nano_delay = round(delay * 1e9) % 1000  # Get submicrosecond delay in nanosecond units
        delay = int(delay * 1e6) / 1e6  # Round delay to the microsecond
        # add delay in integer microseconds (datetime does not support more
        # than the microsecond accuracy)
        dt += timedelta(0, delay)
        self.logger.debug(
            f'{self!r}: Setting IRIGB target time to '
            f'{dt.isoformat()} + {nano_delay:3d} ns')
        # get_irigb_time is 1 s ahead of the decoder latch; the comparator
        # uses the raw fields, so program 1 s earlier.
        hw_dt = dt - timedelta(seconds=1)
        if self.zero_target_irigb_year_and_day:
            y = 0
            d = 0
        else:
            y = hw_dt.year % 100
            d = (hw_dt - datetime(hw_dt.year, 1, 1)).days + 1
        h = hw_dt.hour
        m = hw_dt.minute
        s = hw_dt.second
        ss = hw_dt.microsecond * 100 + int(nano_delay / 10)

        self.logger.debug(
            f'{self!r}: Setting IRIGB target time with y={y}, d={d}, h={h}, '
            f'm={m}, s={s}, ss={ss}')

        self._program_irigb_trigger_sync(y, d, h, m, s, ss)

        ts = self._IrigTimestamp()
        ts.datetime = dt
        ts.nano = (
            int(timegm((dt.year, dt.month, dt.day,
                        dt.hour, dt.minute, dt.second)) * 1e9)
            + dt.microsecond * 1000 + nano_delay)
        return ts

    async def set_irigb_trigger_time_async(self, datetime_=None, delay=None):
        """ Sets the time at which the IRIG-B module will generate a trigger
        that can be used to synchronize boards.

        Parameters:

            datetime_ (datetime): Target time in the same convention as
                :meth:`get_irigb_time_async` (decoder latch plus one second).
                Microsecond resolution; nanosecond digits are zero. If
                `None`, the current FPGA IRIG time is used.

            delay (float): is a time offset in seconds that is added to `datetime_` to set the
                target time. This delay can be specified with an accuracy of up to 10 ns (lower digits are
                ignored and assumed to be zero). If `datetime` is specified, it defaults to zero
                (i.e the specified `datetime` is the target time, so it is assumed to be in the
                future). If `datetime` is not specified, (i.e. it will be the currnt FPGA time),
                `delay` defaults to 3 seconds. The FPGA comparator is programmed one second
                earlier than this sum so it matches the decoder fields.


        Be aware of the limited resolution of floating points. With a float in python (a double), it is not possible to
        represent a delay of more than about 1E6 seconds (11 days) without losing the 10 nanosecond
        resolution. Hence the use of a low accuracy reference time and a high accuracy delay relative to that time.


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
        return await asyncio.to_thread(
            self._set_irigb_trigger_time_sync, datetime_, delay)

    def _is_irigb_before_trigger_time_sync(self):
        t1 = self.fpga_core_reg_read(self._IRIGB_TARGET1_ADDR)
        return bool(t1 & (1 << 31))

    async def is_irigb_before_trigger_time_async(self):
        """ Is true if the current IRIGB is before the target trigger time that was previously set-up.
        """
        return await asyncio.to_thread(self._is_irigb_before_trigger_time_sync)

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
        def _mmi(trig=trig, timeout=timeout):
            if trig:
                w2 = self.fpga_core_reg_read(self._IRIGB_SAMPLE2_ADDR)
                self.fpga_core_reg_write(self._IRIGB_SAMPLE2_ADDR, w2 & ~(1 << 28))
                self.fpga_core_reg_write(self._IRIGB_SAMPLE2_ADDR, w2 | (1 << 28))
                t0 = time.time()
                while not self.fpga_core_reg_read(self._IRIGB_TARGET1_ADDR) & (1 << 30):
                    if time.time() - t0 > timeout:
                        raise RuntimeError(
                            'Timeout while waiting for a Frame. Is data flowing '
                            'out of the ADC data acquisition module?')
            event_number = self.fpga_core_reg_read(self._IRIGB_EVENT_CTR_ADDR)
            event_number += self.fpga_core_reg_read(self._IRIGB_EVENT_CTR_ADDR2) << 32
            return event_number

        event_number = await asyncio.to_thread(_mmi)

        ts = await self._get_irigb_time_async(trig=0)  # The event trigger will automatically trig IRIGB
        captured_time = ts.astype(format)
        return (event_number, captured_time)

    async def get_frame_number_async(self):
        """
        Return the number of the next frame passing through the system.
        """
        def _mmi():
            w2 = self.fpga_core_reg_read(self._IRIGB_SAMPLE2_ADDR)
            self.fpga_core_reg_write(self._IRIGB_SAMPLE2_ADDR, w2 & ~(1 << 28))
            self.fpga_core_reg_write(self._IRIGB_SAMPLE2_ADDR, w2 | (1 << 28))
            t0 = time.time()
            while not self.fpga_core_reg_read(self._IRIGB_TARGET1_ADDR) & (1 << 30):
                if time.time() - t0 > 1:
                    raise RuntimeError(
                        'Timeout while waiting for a Frame. Is data flowing out '
                        'of the ADC data acquisition module?')
            return self.fpga_core_reg_read(self._IRIGB_EVENT_CTR_ADDR)

        return await asyncio.to_thread(_mmi)

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




    def is_fmc_present_for_channel(self, channel_number):
        return self.ANT_FMC_IS_PRESENT[channel_number]

    def is_fmc_present(self, slot_number):
        return self._FMC_present[slot_number]

    def get_channels(self, channels=None):
        """ Return a list of available channel numbers """
        if channels is None:
            return self.default_channels
        else:
            return list(channels)

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

    def _normalize_channels(self, channels=None):
        """Normalize channel selectors into a concrete channel list.

        This helper accepts ``None``, a single channel index, or any iterable
        of channel indices and returns a plain ``list[int]``. Capture-state
        helpers use one normalization path so callers can pass flexible
        selectors without repeating type handling. ``None`` maps to
        ``self.default_channels``, an ``int`` becomes a single-item list, and
        other values are materialized with ``list()``.
        """
        if channels is None:
            return list(self.default_channels)
        if isinstance(channels, int):
            return [channels]
        return list(channels)

    def update_config(self):
        """Refresh subsystem configs onto ``self.config``."""
        self.chan.update_config()
        if self.CT is not None:
            self.CT.update_config()
        if self.CORR is not None:
            self.CORR.update_config()
        return self.config

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
            return f'{self.mb.part_number}_SN{self.mb.serial}'
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
        if self.CAPTURE_TYPE == 'UCAP':
            ucap_stream_ids = self.UCAP.get_stream_ids()
            return [ucap_stream_ids[ch] for ch in channels] # only return stream IDs of specified channels
        elif self.CAPTURE_TYPE == 'PROBER':
            return [self.chan[ch].PROBER.get_stream_id() for ch in channels]
        else:
            raise ValueError(f"Invalid capture type: {self.CAPTURE_TYPE}")
    def get_stream_id(self, channel):
        """ Return the stream_id of a specified channel.

        Returns:
            int: the stream IDs
        """

        if self.CAPTURE_TYPE == 'UCAP':
            return self.UCAP.get_stream_ids()[channel]
        elif self.CAPTURE_TYPE == 'PROBER':
            return self.chan[channel].PROBER.get_stream_id()
        else:
            raise ValueError(f"Invalid capture type: {self.CAPTURE_TYPE}")

    def get_stream_id_map(self):
        """ Return the stream_ids of every channel of the board, indexed by channel_id.

        Returns:

            dict if the format {channel_id:stream_id}, where channel_id is a (crate, slot, channel)
            tuple.
        """

        if self.CAPTURE_TYPE == 'UCAP':
            ucap_stream_ids = self.UCAP.get_stream_ids()
            return {self.get_id(ch): ucap_stream_ids[ch] for ch in self.chan.keys()}
        elif self.CAPTURE_TYPE == 'PROBER':
            return {self.get_id(ch): chan.PROBER.get_stream_id() for ch, chan in self.chan.items()}
        else:
            raise ValueError(f"Invalid capture type: {self.CAPTURE_TYPE}")

    async def get_system_config_async(self, basic=False):
        """
        Return a system monitoring snapshot for this FPGA.

        Prefer :meth:`update_config` for the hierarchical tracked firmware config.

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

    def sync(self, local=True, verbose=0):
        if verbose:
            self.logger.debug(f"{self!r}: Syncing board")

        if not local:
            raise DeprecatedError(f'{self!r}: remote sync no longer supported using `sync()`. use `generate_sync()` instead.')

        if self.HAS_ADCDAQ:
            self.set_adc_mask(0)  # null the ADC data before it gets to the channelizers to reduce power consumption

        self.REFCLK.local_sync()

        if self.HAS_ADCDAQ:
            self.set_adc_mask(0xff)  # restore full ADC data

    async def sync_async(self, local=True, verbose=0):
        """Async version of :meth:`sync`. Yields while waiting for ``SYNC_DONE``."""
        if verbose:
            self.logger.debug(f"{self!r}: Syncing board")

        if not local:
            raise DeprecatedError(f'{self!r}: remote sync no longer supported using `sync()`. use `generate_sync()` instead.')

        if self.HAS_ADCDAQ:
            self.set_adc_mask(0)  # null the ADC data before it gets to the channelizers to reduce power consumption

        await self.REFCLK.local_sync_async()

        if self.HAS_ADCDAQ:
            self.set_adc_mask(0xff)  # restore full ADC data

    def generate_sync(self):
        """ Have the REFCLK generate a SYNC signal.

        For this to work, the signal `sync_out` should be routed beforehand to the desired sync output
        using ``set_user_output_source(source='sync_out', output=<desired_output>)``.

        """
        self.REFCLK.remote_sync()


    # def pulse_ant_reset(self):
    #     """ Resets the stats of all channelizers and clear the processing pipeline.

    #     Memory-mapped registers are not affected.
    #     """
    #     self.GPIO.pulse_ant_reset()  # resets all

    def reset(self):
        """ Resets the channelizers, corner-turn and correlator engines.
        Memory-mapped registers are not affected.
        """
        self.set_chan_reset(1)
        self.set_corr_reset(1)
        self.set_corr_reset(0)
        self.set_chan_reset(0)

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

    def init_crossbars(self, **kwargs):
        if self.CT:
            self.CT.init_crossbars(**kwargs)

    def set_channelizer(
            self,
            adc_mode=None,
            adc_sampling_mode=None,
            adc_bandwidth=None,
            adcdaq_mode=None,
            data_source=None,
            function=None,
            funcgen_left_shift=None,
            funcgen_right_shift=None,
            # freq_test_bins=None,  # will be passed into function_kwargs
            fft_bypass=None,
            fft_shift=None,
            scaler_bypass=None,
            scaler_cap_data_type=None,
            scaler_bypass_data_type=None,
            gains=None,
            postscaler=None,
            scaler_eight_bit=None,
            scaler_rounding_mode=None,
            symmetric_saturation=None,
            zero_on_sat=None,
            prober_user_flags=None,
            offset_binary_encoding=None,
            local_sync=True,
            channels=None,
            **function_kwargs):
        """
        Single command used to set all channelizer settings.

        The data processing chain is:

                  ADC --> ADCDAQ --> FUNCGEN --> FFT --> SCALER
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

        # if data_source is not None :
        #     self.set_data_source(data_source, channels=channels, **function_kwargs)  # does a channelizer reset

        if function is not None:
            self.logger.warning(
                "Using 'function' parameter for setting FUNCGEN function is obsolete. Please use 'data_source' instead."
            )

        if function is not None or data_source is not None:
        # Set the date source and the function generator that feed the FFT
            self.set_funcgen(function=data_source or function, channels=channels, left_shift=funcgen_left_shift, right_shift=funcgen_right_shift, **function_kwargs)

        # Set FFT bypass and shift schedule
        if fft_bypass is not None:
            self.set_fft_bypass(bypass_mode=fft_bypass, channels=channels)

        if fft_shift is not None:
            self.set_fft_shift(fft_shift, channels=channels)

        # Set Scaler parameters
        if scaler_bypass == False or scaler_bypass_data_type is not None or scaler_cap_data_type is not None:
            self.set_scaler_output_modes(bypass=scaler_bypass,
                                         bypass_data_type=scaler_bypass_data_type,
                                         cap_data_type=scaler_cap_data_type, channels=channels)

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

        if symmetric_saturation is not None:
            self.set_symmetric_saturation(symmetric_saturation=symmetric_saturation, channels=channels)

        if zero_on_sat is not None:
            self.set_zero_on_sat(zero_on_sat=zero_on_sat, channels=channels)

        if gains is not None:
            self.set_gains(gain=gains, postscaler=postscaler, channels=channels)

        if offset_binary_encoding is not None:
            self.set_offset_binary_encoding(offset=offset_binary_encoding, channels=channels)

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

    def set_data_source(self, source=None,  channels=None, reset_chan=True, **kwargs):
        """
        Selects the data that is being fed into the channelizer. If a wafeform
        name (and corresponding arguments) is provided, the function generator
        is automatically selected and the waveform is set-up.

        This method is a subset of `set_funcgen`.

        Parameters:

            source (str): name of the data source/function name/waveform name.

            channels (list): list of integers specifying which channels are to be configured. If
                `None`, the default channel list is used.

            reset_chan (bool): If True, the channelizer is reset. This should be done if we change
                between an external data source (i.e the ADC) to an internal one. It is not needed
                if we are simply changing a waveform.

            kwargs (dict): parameters that will be passed to the source-setting method.
        """

        self.set_funcgen(function=source, channels=channels, reset_chan=reset_chan, **kwargs)

    def get_data_source(self):
        """
            Returns a list of data source for all channels.

        Returns:

            list of str: list of strings describing the data source used for each function generator

        """
        return [chan.FUNCGEN.get_data_source() for chan in self.chan.values()]

    def set_funcgen(self, function=None, channels=None, left_shift=None, right_shift=None, reset_chan=False, **kwargs):
        """
        Same as `set_data_source`, except the channelizer is not reset by default. Use only for
        changing between waveforms.

        This may cause one frame to partially contain the new waveform. We do not check if the new
        function is just a waveform change that does not require a reset.

        Parameters:

            function (str): name of the data source/function name/waveform name.

            channels (list): list of integers specifying which channels are to be configured. If
                `None`, the default channel list is used.

            left_shift (int): Number of bits to shift left the output of the function generator.
                Result is saturated. Left shift is applied before the right shift. Unchanged if ``None``.

            right_shift (int): Number of bits to right left the output of the function generator.
                Left shift is applied before the right shift. Unchanged if ``None``.

            reset_chan (bool): If True, the channelizer is reset. This should be done if we change
                between an external data source (i.e the ADC) to an internal one. It is not needed
                if we are simply changing a waveform.

            kwargs (dict): parameters that will be passed to the source-setting method.
        """
        if channels is None:
            channels = self.default_channels

        if reset_chan:
            self.set_chan_reset(1)  # Reset is needed to resynchronize the system with the new data
        for chan in self.get_channelizers(channels):
            chan.FUNCGEN.set_output_shifting(left_shift, right_shift)
            chan.FUNCGEN.set_data_source(function, **kwargs)
        if reset_chan:
            self.set_chan_reset(0)  # Release reset

    # set_funcgen_function = set_funcgen # for backwards compatibility

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
                    self.logger.warning(f'{self!r}: ADC Mezzanine board for channel {int(ch)} is not present. Ignoring this board')
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
            valid_modes = ', '.join(self.ADC_MODE_NAMES.keys())
            raise ValueError(f"Invalid ADC mode '{mode}'. Valid modes are {valid_modes}")
        (mode_value, capture_period) = self.ADC_MODE_NAMES[mode.lower()]

        if channels is None:
            channels = self.default_channels

        # Set the mode on all affected ADC boards
        adc_boards = self.get_adc_board(channels)
        for adc_board in adc_boards:
            adc_board.ADC.set_test_mode(test_mode=mode_value, adc_mode=sampling_mode, bandwidth=bandwidth)

        # Set the capture period for all specified channels
        if self.HAS_ADCDAQ:
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

    def set_chan_reset(self, state):
        """ Sets the state of the reset line of ALL channelizer.

        Parameters:

            state (bool): A true value will put the channelizers in reset, and data will stop flowing from them.

        Note:
            A channelizer reset is automatically done during a SYNC.
        """

        self.GPIO.ANT_RESET = state


    def get_chan_reset(self):
        """ Get the status of the channelizer reset line.

        Return:
            (bool): state of the reset line.
        """
        return self.GPIO.ANT_RESET

    set_ant_reset = set_chan_reset # for backwards compatibility
    get_ant_reset = get_chan_reset # for backwards compatibility

    def set_corr_reset(self, state):
        """ Sets the state of the reset line of botht he Corner-Turn engine and the Correlator."""
        self.GPIO.CORR_RESET = state

    def set_trig(self, state):
        self.GPIO.GLOBAL_TRIG = state

    def stop_data_capture(self):
        """
        Stops the transmission of data.
        """
        self.GPIO.GLOBAL_TRIG = 0  # disable data transmission if continuous mode is currentlly selected
        if self.CAPTURE_TYPE == "PROBER":
            for chan in self.chan.values():
                chan.PROBER.RESET = 1
        elif self.CAPTURE_TYPE == "UCAP":
            self.UCAP.USER_RESET = 1
        else:
            raise RuntimeError("Stop data capture only implemented on prober and ucap currently")

    def get_data_socket(self, port_number=None):
        """
        Return a UDP socket that receives the raw/correlator data.

        If the socket does not already exists, the FPGA will be configured to send the data to the returned socket.

        Prefer :meth:`get_data_socket_async` when an event loop is already running
        (e.g. Jupyter).

        Parameters:

            port_number (int): Port number to use:

                - If ``None``, attempts to open a socket at the destination port currently programmed in the FPGA. If that port is zero, act as if ``port_number`` =0.
                - If zero, open a socket at a random  (OS-provided) port, and set the corresponding destination port in the FPGA.
                - If non-zero, get a socket bound to the specified port. An exception will be raised if that port is already used by another program.


        Returns:

            socket.socket(): a UDP socket.
        """
        return run_async(self.get_data_socket_async(port_number=port_number))

    async def get_data_socket_async(self, port_number=None):
        """Async variant of :meth:`get_data_socket`."""
        if port_number is None:
            port_number = await self.get_local_data_port_number_async()

        opened_sockets = __main__.__dict__.setdefault('__opened_sockets__', {})

        if port_number and (port_number in opened_sockets):
            sock = opened_sockets[port_number]
            self.logger.debug(f'{self!r}: Reusing already allocated socket {sock} for port {port_number}')
        else:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            # Enlarge the kernel UDP receive buffer so datagrams that arrive while
            # the (single-threaded) receiver is busy in process/format instead of
            # recvfrom() are absorbed rather than dropped. The default (~208 kB) is
            # only ~40 aggregated 5280 B datagrams of headroom. The kernel clamps to
            # net.core.rmem_max, so the effective value may be smaller — raise it with
            # `sudo sysctl -w net.core.rmem_max=67108864` if drops persist.
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, self.UDP_DATA_RCVBUF)
                got = sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
                self.logger.debug(
                    f'{self!r}: UDP data socket SO_RCVBUF requested {self.UDP_DATA_RCVBUF} B, '
                    f'kernel granted {got} B (halved value includes bookkeeping)'
                )
            except OSError as e:
                self.logger.warning(f'{self!r}: could not set UDP SO_RCVBUF: {e}')
            port_number = port_number or 0
            self.logger.debug(f'{self!r}: Binding data socket at port {port_number} to interface IP {self.interface_ip_addr}')
            try:
                sock.bind((self.interface_ip_addr, port_number))
            except OSError:
                raise OSError(f'Socket at port {port_number} is already in use on interface {self.interface_ip_addr}. On Linux, use "netstat -ulpe" to find which user/process has the port already open')

        await self.set_data_socket_async(sock)
        return sock

    def get_tcp_data_socket(self, port_number=None, rcvbuf=8 * 1024 * 1024, force=False):
        """Return a cached TCP socket connected to this board's fixed data listen port.

        The board always listens on a well-known port (host-initiated); no TCPipe
        register programming is required. Array ``get_data_receiver_async``
        still uses UDP unless ``tcp=True``.

        Sockets are assumed stable for a session (firmware drops on backpressure
        instead of closing). Use ``force=True`` (or array reinit via
        ``get_data_receiver_async(..., force=True)``) to connect fresh.

        Parameters
        ----------
        port_number : int or None
            Board listen port. ``None`` uses :attr:`TCPipe.DEFAULT_TCP_DATA_PORT`.
        rcvbuf : int
            ``SO_RCVBUF`` so the kernel can absorb bursts between process calls.
        force : bool
            If True, discard any cached socket and connect fresh.

        Returns
        -------
        socket.socket
            Connected TCP stream socket.
        """
        from pychfpga.hardware.interfaces.tcpipe import TCPipe

        if port_number is None:
            port_number = TCPipe.DEFAULT_TCP_DATA_PORT
        port_number = int(port_number)

        host = self.mb.hostname
        if not host:
            raise RuntimeError(f"{self!r}: no hostname for TCP data connect")

        opened = __main__.__dict__.setdefault("__opened_tcp_data_sockets__", {})
        key = (host, port_number)

        if force:
            old = opened.pop(key, None)
            if old is not None:
                try:
                    old.close()
                except OSError:
                    pass
            sock = None
        else:
            sock = opened.get(key)

        if sock is not None:
            self._tcp_data_socket = sock
            self.logger.debug(
                f"{self!r}: Reusing TCP data socket {sock} for {host}:{port_number}"
            )
            return sock

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, int(rcvbuf))
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.logger.debug(f"{self!r}: Connecting TCP data socket to {host}:{port_number}")
        sock.connect((host, port_number))
        sock.settimeout(0.1)
        opened[key] = sock
        self._tcp_data_socket = sock
        return sock

    def set_data_socket(self, sock, force=False):
        """ Specified the data socket to which data will be sent.

        Prefer :meth:`set_data_socket_async` when an event loop is already running.
        """
        return run_async(self.set_data_socket_async(sock, force=force))

    async def set_data_socket_async(self, sock, force=False):
        """Async variant of :meth:`set_data_socket`."""
        if sock is self._data_socket and not force:
            return

        opened_sockets = __main__.__dict__.setdefault('__opened_sockets__', {})

        (actual_ip_addr, actual_port_number) = sock.getsockname()
        opened_sockets[actual_port_number] = sock
        if self._data_socket:
            self.logger.warning(f'{self!r}: Abandonning previously allocated socket {self._data_socket} for new socket {sock}')
        self._data_socket = sock
        await self.set_local_data_port_number_async(actual_port_number)


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
            self.chan[ch].PROBER.set_stream_id(stream_id)

    def start_data_capture(
            self,
            period=None,
            frames_per_burst=1,
            channels=None,
            source='scaler',
            data_type=None,
            sync=1,
            verbose=1,
            burst_period_in_seconds=None,
            burst_period_in_frames=None,
            burst_period_frame_rounding=4,
            offset=0,
            send_delay=0,
            mode = 0):
        """
        Starts periodic capture and transmission to the host computer of the Function generator (or ADC) raw data or SCALER data frames.


        Some parameter are applicable to the PROBER or the UCAP capture engines, depending on which
        one is supported by the platform.

        Parameters:

            period (float): Time (in seconds) between captured bursts

            burst_period_in_seconds (float): Same as `period` or as a number of frames

            burst_period_in_frames (int): Number of frames between captured bursts - 1 second corresponds to approximately 39 000 frames.

            burst_period_frame_rounding (int): rounds burst period to an integer multiple of `burst_period_frame_rounding`.

            frames_per_burst (int): (PROBER only) Number of frames to send in a single burst. Default is 1.
                Limited by buffer space in the FPGA. This field is ignored if the UCAP capture
                engine is used by the platform (see ``mode`` parameter).

            source (str or int): Capture source name or logical index.

                - ``'funcgen'`` / ``0``: data after the function generator (ADC or waveform).
                - ``'scaler'`` / ``1``: data on the scaler capture port.

            data_type (str or int): If the data is from the scaler, indicates what type of data is to be captured.
                For scaler capture modes 4/5 (multi-packet logical decode), one host logical frame is reconstructed
                from packets produced by consecutive physical frames; it is not replayed from a single buffered frame.

            channels (list): list of channels for which the data capture will be enabled. Behavior differs depending on the capture engine:

                - PROBER: Capture is configured ont he selected channels only
                - UCAP: Only specified channels will be captured. The number of allowed channels differs depending
                  on `mode`:

                  - mode 0: All 8 channels are sent, and `channels` must be exactly [0,1,2,3,4,5,6,7]. This is the default when None.
                  - mode 1: Any 4 channels are sent. `channels` must be a list of exactly 4 channel numbers. Default when None is [0,1,2,3].
                  - mode 2: Any 2 channels are sent. `channels` must be a list of exactly 2 channel numbers. Default when None is [0,1].
                  - mode 3: A single `channel` is sent. Default when None is [0].

            sync (bool): When True, a local sync is performed.

            verbose (int): A non-zero value increases the amount of information that is printed or logged.

            offset (int): (PROBER only) Number that translates to how many frames are skipped before the capture
                counters starts after a sync(). This is used to stagger capture frame transmission
                between boards in a crate to prevent UDP packets from being dropped by a switch.

            send_delay (int): (PROBER) Time delay in PROBER ``SEND_DELAY`` units. (UCAP)
                ``READOUT_DELAY`` in ADC input frames (0–65535). Per-board staggering is applied by
                :meth:`~pychfpga.fpga_firmware.chfpga.chfpga_array_methods.start_data_capture_async`.

            mode (int): (UCAP only). Sets the UCAP cature mode:

                - 0: captures 2 contiguous frames from all 8 channels (``channels`` has no effect)
                - 1: captures 4 contiguous frames from the first 4 channels listed in ``channels``.
                - 2: captures 8 contiguous frames from the first 2 channels listed in ``channels``.
                - 3: captures 16 contiguous frames from the first channel listed in ``channels``.

        Data is sent as periodic bursts of M frames (`frames_per_burst`) and continues
        until stopped by ``stop_data_capture()``.

        Burst repetition rate is set either as a period specified in seconds (`period` or
        `burst_period_in_seconds`) or as a number of frames (`burst_period_in_frames`).
        """

        if burst_period_in_seconds is not None:
            period = burst_period_in_seconds

        if ((burst_period_in_frames is None) and (period is None)) \
           or ((burst_period_in_frames is not None) and (period is not None)):
            raise ValueError("You must specify either 'period' or 'burst_period_in_frames' ")

        if period is not None:
            burst_period_in_frames = max(float(period) / self.FRAME_PERIOD, 1)

        # round the burst period to an integer multiple of burst_period_frame_rounding
        burst_period_in_frames = (int(burst_period_in_frames) // burst_period_frame_rounding) * burst_period_frame_rounding
        if channels is None:
            if self.CAPTURE_TYPE == "PROBER":
                channels = self.default_channels
            elif self.CAPTURE_TYPE == "UCAP":
                channels = self.UCAP.get_default_channels(mode)
            self.logger.info(
                f"The channels are not specified for data capture. "
                f"Defaulting to {channels}."
            )

        if self.CAPTURE_TYPE == 'UCAP':
            source_index, _ = self.UCAP.normalize_source(source)
        else:
            source_index, _ = prober.PROBER.normalize_source(source)
 
        if verbose:
            self.logger.debug(
                f'{self!r}: Configuring channelizer {channels} to capture '
                f'{frames_per_burst} frame every {burst_period_in_frames} frames (i.e .every {burst_period_in_frames * self.FRAME_PERIOD * 1000:.3f} ms) '
                f'with first frame offset of {offset} frames ({offset * self.FRAME_PERIOD * 1000:.3f} ms)'
                f'and a send delay of {send_delay}')

        if data_type is not None:
            if source_index != 1:
                raise RuntimeError(f"'data_type' can be specified only for source=1 (scaler)")
            for ch in channels:
                self.chan[ch].SCALER.set_capture_data_type(data_type)
        else:
            if source_index == 1:
                self.logger.warning(f"data_type not specified for scaler capture. Defaulting to 0.")
                for ch in channels:
                    self.chan[ch].SCALER.set_capture_data_type(0)

        # stop data from going into the PROBER and MASTER to minimize the risk
        # of malformed packets and unstable communications
        reset_state = self.get_chan_reset()

        self.set_chan_reset(1)


        if self.CAPTURE_TYPE == 'UCAP':
            self.UCAP.start_data_capture(
                source=source,
                mode=mode,
                channels=channels,
                periods=[burst_period_in_frames - 1, burst_period_in_frames - 1],
                send_delay=send_delay,
            )
            self.config.channels = tuple(self._normalize_channels(channels))
            self.UCAP.USER_RESET = 0
            self._capture_pkt_frames_per_burst = None

        elif self.CAPTURE_TYPE =='PROBER':
            self._capture_pkt_frames_per_burst = frames_per_burst

            frames_per_second = frames_per_burst * 1.0 / self.FRAME_PERIOD / burst_period_in_frames
            packet_size_in_bits = (self.FRAME_LENGTH + 10 + 42) * 8  # 10 header bytes, 42 Ethernet/IP/UDP overhead
            self.logger.debug('\n'.join((
                f'{self!r}: Data rates are:',
                f'    1 board, 1 channel: {frames_per_second * packet_size_in_bits / 1e6:.3f} Mbits/s',
                f'    1 board, {len(channels)} channels: {len(channels) * frames_per_second * packet_size_in_bits / 1e6:.3f} Mbit/s',
                f'    1 crate: {16 * 16 * frames_per_second * packet_size_in_bits / 1e6:.3f} Mbits/s'
                )))

            if channels is None:
                channels = self.default_channels

            # Do not limit the transfer rate
            self.GPIO.HOST_FRAME_READ_RATE = 5

            # Stop data capture on *ALL* channels
            for chan in self.get_channelizers():
                chan.PROBER.RESET = 1
            for chan in self.get_channelizers(channels):
                chan.PROBER.set_sub_period(23)  # disable sub period
                chan.PROBER.set_data_source(source)
                chan.PROBER.config_capture(
                    frames_per_burst=frames_per_burst,
                    burst_period=burst_period_in_frames,
                    offset=offset,
                    send_delay=send_delay)
                ch = chan.chan_number
                action = ('Disabling', 'Enabling')[ch in channels]
                self.logger.debug(f'{self!r}: {action} raw data capture on channel {ch}')
                chan.PROBER.RESET = 0

            self.config.channels = tuple(self._normalize_channels(channels))

        # ** line below no longer supported by firmware *** enables data transmission if continuous mode is selected
        self.set_trig(1)
        self.set_chan_reset(reset_state)

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

        # *** TODO: rewrite the UCAP block below to use the new UCAP.set_data_capture() method.
        if self.CAPTURE_TYPE == 'UCAP':
            _, source_name = self.UCAP.normalize_source(source)
            self.UCAP.SOURCE_SEL = self.UCAP.DATA_SOURCE_TABLE[source_name]
            self.UCAP.SUB_PERIOD = sub_period
        elif self.CAPTURE_TYPE == 'PROBER':
            for chan in self.get_channelizers(channels):
                chan.PROBER.set_data_source(source)
                chan.PROBER.set_sub_period(sub_period)
        else:
            raise RuntimeError(
                f'{self!r}: set_data_capture() is not supported for '
                f'capture engine {self.CAPTURE_TYPE!r}')
        self.config.channels = tuple(self._normalize_channels(channels))

    def set_fft_bypass(self, bypass_mode, channels=None):
        """
        Sets the BYPASS flag on both the FFT modules.
        If the list of channels is specified, only these channels will be set.
        All channelizers are reset to force the FFT to resynchronize to the frame boundaries.
        """

        if channels is None:
            channels = self.default_channels

        configured_channels = set()
        for ch in channels:
            if ch not in self.chan:
                self.logger.warning(f'{self!r}: FFT bypass mode on channel {ch} are not set '
                                    f'because that channel is not available')
            elif ch not in self.LIST_OF_ANTENNAS_WITH_FFT and not bypass_mode:
                self.logger.warning(f'{self!r}: FFT bypass mode was disabled on channel {ch}'
                                    f' which has no FFT module. The command will have no effect.')
            else:
                self.chan[ch].FFT.set_bypass(bypass_mode)
                configured_channels.add(ch)
        channels_str = ', '.join([str(i) for i in configured_channels])
        self.logger.debug(f'{self!r}: Setting FFT bypass mode to {bool(bypass_mode)} for channel {channels_str}')
        # self.reset()
        # self.sync()

    set_FFT_bypass = set_fft_bypass  # for legacy code compatibility

    def get_fft_bypass(self):
        """
        Returns a list indicating if the FFT is bypassed or not for each channel.
        """
        return [chan.FFT.get_bypass() for chan in self.chan.values()]

    get_FFT_bypass = get_fft_bypass  # for legacy code compatibility

    def get_scaler_bypass(self):
        """
        Returns a list indicating if the SCALER is bypassed or not for each channel.
        """
        return [chan.SCALER.get_bypass() for chan in self.chan.values()]

    def set_scaler_output_modes(self, bypass=None, bypass_data_type=None, cap_data_type=None, channels=None):
        '''
        Configures both the science and capture outputs of the scaler.

        Parameters:
            bypass: When set to False, the science output will output normal (i.e. 4+4 bit scaled FFT data). Also sets bypass_data_type to 0.

            bypass_data_type: Sets the mode of the science output. These are:
                - 0: The input FFT data is forwarded directly to the output.
                - 1: If an overflow occurs in either the real or imaginary component of the bin, 1+0j is output for that bin. Otherwise, 0+0j is output.
                - 2: If a positive overflow occurs in the real component of the bin, 1+0j is output for that bin. If a negative overflow occurs in that component, 0+1j is output. Otherwise, 0+0j is output.
                - 3: If a positive overflow occurs in the imaginary component of the bin, 1+0j is output for that bin. If a negative overflow occurs in that component, 0+1j is output. Otherwise, 0+0j is output.
            Note that setting bypass_data_type also automatically sets the bypass register to True (otherwise the science output will continue outputting normal)

            cap_data_type: Sets the mode of the capture output. These are:
                - 0: Forward science output directly to capture output.
                - 1: Output the input FFT data.
                - 2: Output raw scaled FFT data, before rounding is applied. The data is saturated, but disregarding symmetric saturation.
                - 3: Output 4+4 bit scaled FFT data (e.g. normal scaler output, even if the science output is set to another mode)
                - 4: Output the input FFT data of the even bins and then the odd bins in an alternating fashion, at double the bitwidth.
                - 5: Output the input FFT data of each Re and Im component one at a time, at the full bitwidth.
                - 6: Output post-gain saturation flags per bin (Re/Im as 0, +1, or -1 in each capture field).
            For modes 4 and 5, one host logical frame is reconstructed from packets produced by consecutive
            physical frames (not from a single fully buffered physical frame).
        '''

        if channels is None:
            channels = self.default_channels

        if bypass == False:
            self.logger.debug(f'Setting scaler bypass to 0 for channel {channels}')
            for ch in channels:
                self.chan[ch].SCALER.set_scaler_output(data_type=0, bypass=False)
        elif bypass_data_type is not None:
            self.logger.debug(f'Setting scaler output data type to {bypass_data_type} for channel {channels}')
            for ch in self._normalize_channels(channels):
                self.chan[ch].SCALER.set_scaler_output(data_type=bypass_data_type, bypass=True)

        if cap_data_type is not None:
            if isinstance(cap_data_type, str):
                if cap_data_type not in SCALER.CAPTURE_DATA_TYPES:
                    valid = ", ".join(sorted(SCALER.CAPTURE_DATA_TYPES.keys()))
                    raise ValueError(
                        f"Unknown scaler capture data type {cap_data_type!r}. Valid names: {valid}."
                    )
                normalized_cap_type = int(SCALER.CAPTURE_DATA_TYPES[cap_data_type])
            else:
                normalized_cap_type = int(cap_data_type)
            self.logger.debug(f'Setting scaler capture data type to {cap_data_type} for channel {channels}')
            for ch in self._normalize_channels(channels):
                self.chan[ch].SCALER.set_capture_data_type(normalized_cap_type)


    def reset_scaler_overflow_flags(self, channels=None):
        """ Resets the SCALER overflow flags.

        Parameters:

            channels (list of int): channels to reset. if None, the default channel list (all channels) is used.
        """
        for ch in channels or self.default_channels:
            self.chan[ch].SCALER.reset_overflow_flags()


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
        filename = f'{self.get_string_id()}.yaml'
        fullpath = os.path.join(delay_table_folder, filename)

        try:
            with open(fullpath, 'r') as yamlfile:
                file_data = yaml.load(yamlfile, Loader=yaml.SafeLoader)
        except IOError:
            print(f'{fullpath} not found')
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
        filename = f'{self.get_string_id()}.yaml'
        fullpath = os.path.join(delay_table_folder, filename)
        print(f'Loading YAML file {filename}')
        try:
            with open(fullpath, 'r') as yamlfile:
                file_data = yaml.load(yamlfile, Loader=yaml.SafeLoader)
        except IOError:
            print(f'{fullpath} not found')
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
            # self.logger.info(f'{self!r}: Reading channel {ch}.')
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
            print(f'CH{ch:02d}: offset={offset:2d} : {q}')

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
                s = f'Bit {int(bit)}: {bit_string} Delay = {int(computed_delay[bit]):2}   (rise @ {int(re + 1):2}, fall @ {int(fe + 2):2})'
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
                    chosendelay = np.nan
                    changood.append(np.nan)
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
        self.logger.info(f'{self!r}: ----------- chFPGA status ---------------')
        self.logger.info(f'{self!r}:  Controller IP address: {self.ip_addr}, port: {int(self.mb.fpga.port_number)} ')
        self.logger.info(f'{self!r}:  Firmware version: {self.get_fpga_firmware_version()}')
        self.logger.info(f'{self!r}:  Number of channel inputs: {int(self.NUMBER_OF_CHANNELIZERS)}')
        self.logger.info(f'{self!r}:  Number of channelizers with FFT: {len(self.LIST_OF_ANTENNAS_WITH_FFT)} (channels {self.LIST_OF_ANTENNAS_WITH_FFT!s})')
        self.logger.info(f'{self!r}:  Number of correlators: {len(self.LIST_OF_IMPLEMENTED_CORRELATORS)} (correlators {self.LIST_OF_IMPLEMENTED_CORRELATORS!s})')

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
            raise ValueError(f'Number of bits {int(width)} is invalid. Only 4 or 8 is allowed')

        # Set the channelizer data width
        self.chan.set_data_width(width)

        # Set the corner-turn engine data width
        if self.CT:
            self.CT.set_data_width(width)

    def get_data_width(self):
        """
        Returns number of bits used to represent the values computed by the channelizers and used by
        the GPU link and FPGA correlators.

        If all the hardware modules are not set in the same mode, an error is raised.
        """
        # get the channelizer and crossbar data width
        chan_data_width = self.chan.get_data_width()

        if self.CT:
            xbar_data_width = self.CT.get_data_width()

            if xbar_data_width and xbar_data_width != chan_data_width:
                raise RuntimeError(f"The channelizers and crossbar are not set to the same data width (chan={int(chan_data_width)} bits, xbar={int(xbar_data_width)} bits). The data stream won't make much sense")
        return chan_data_width

    def configure_crossbar(self, *args, **kwargs):
        self.CROSSBAR.configure(*args, **kwargs)

    def set_offset_binary_encoding(self, offset=True, channels=None):
        """
        Set the output to be encoded in offset binary instead of 2's complement
        if sync is true, perform a sync afterward.  Necessary for data to continue flowing

        Parameters:
            channels (list of int): List of channels to which the command is applied

        """
        if self.CORR and self.CORR.REQUIRES_OFFSET_BINARY_ENCODING and not offset:
            self.logger.warning(f'Offset binary encoding is set to {bool(offset)}. The correlator required it to be {self.CORR.REQUIRES_OFFSET_BINARY_ENCODING}. Correlator output will not make sense.')
            raise RuntimeError()

        if self.CORR is not None and hasattr(self.CORR, "set_offset_binary_encoding"):
            self.CORR.set_offset_binary_encoding(offset)

        if channels is None:
            channels = self.default_channels

        if not isinstance(channels, list):
            raise ValueError("Channels must be a list")
        # Set the scaler to use offset binary
        for channel in channels:
            self.chan[channel].SCALER.set_offset_binary_encoding(offset)

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
        """
        Return a string that represent the board using the provided format list.

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
        """Return the name of the full path and filename of the file containing the gains for this board.
        """
        gain_filename = os.path.join(folder, f'gains_{self.get_formatted_id()}.pkl')
        return gain_filename

    def load_gains(self, folder='.'):
        """Loads the gain file associated with this board and return the gains.

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
            self.logger.debug(f'r: Loaded gains for board {self} from file {gain_filename}')
            # ib.set_gain(g_array, bank=bank)  # *** should this be bank=all_bank
        except IOError:
            self.logger.warning(f"Gain file '{gain_filename}' not found for (crate,slot)= {self.get_id()!r}")
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
            self.logger.warning(f"Gain file '{gain_filename}' could not be saved for (crate,slot)={self.get_id()!r} ")

    def set_scaler_eight_bit(
            self,
            scaler_eight_bit: bool,
            prober_user_flags: bool = None,
            channels: List[int] = None,
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
            """

        if channels is None:
            channels = self.default_channels

        eb_support = [chan.SCALER.EIGHT_BIT_SUPPORT for chan in self.chan]
        if not all(eb_support) and scaler_eight_bit:
            raise RuntimeError(
                f"The scaler of channelizers {[ch for ch in range(len(self.chan)) if not eb_support[ch]]} "
                f"does not support 8-bit mode."
            )

        for ch in range(len(self.chan)):
            if ch in channels:
                self.logger.debug(f"Setting scaler of channel {ch} to 8-bit mode.")
                self.chan[ch].SCALER.set_output_bit_width(8 if scaler_eight_bit else 4)
                self.logger.debug(
                    f"{'Adding' if prober_user_flags else 'Removing'} user flags from scaler data of channel {ch}."
                )
                if prober_user_flags is not None:
                    self.chan[ch].PROBER.PROBER_USER_FLAGS = prober_user_flags
                    if prober_user_flags and not scaler_eight_bit:
                        raise RuntimeError(f"Cannot output prober user flags in a channel not in 8-bit mode")

    def set_scaler_rounding_mode(
            self,
            scaler_rounding_mode: Literal[1, 2, 3],
            channels: List[int] = None,
    ):
        """
            Sets the rounding mode of the scaler. The mode will be set individually for each channelizer.

            Parameters:

                scaler_rounding_mode (int): 0 - truncation, 1 - rounding, 2 - convergent rounding

                channels (list of int): channels to which the specified mode is applied. If 'channels' is None, it is
                applied to the default (active) channels (see set_default_channels()).
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
                self.chan[ch].SCALER.ROUNDING_MODE = rm_code #not rm_code

    def set_symmetric_saturation(self, symmetric_saturation, channels=None):

        if channels is None:
            channels = self.default_channels

        for ch in range(len(self.chan)):
            if ch in channels:
                self.logger.debug(f"{'Enabling' if symmetric_saturation else 'Disabling'} symmetric saturation on channel {ch}")
                self.chan[ch].SCALER.SATURATE_ON_MINUS_7 = symmetric_saturation

    def set_zero_on_sat(self, zero_on_sat, channels=None):

        if channels is None:
            channels = self.default_channels

        for ch in range(len(self.chan)):
            if ch in channels:
                self.logger.debug(f"{'Enabling' if zero_on_sat else 'Disabling'} zeroing of saturated real+complex pairs on channel {ch}")
                self.chan[ch].SCALER.ZERO_ON_SATURATION = zero_on_sat

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
                - G = (Glin_vector, None): same as above
                - G = (Glin_vector, Glog): Gain value for each bin with specified post-scaler value

                where:

                - ``Glin_scalar`` is a real or complex number. The real and imaginary part of the
                  linear gain are integer values ranging from -32768 to 32767.

                - ``Glog`` is the postscaler factor. This is a binary scaling factor, which is an
                  integer between 0 and 31 representing a power of two that multiplies the linear
                  gain. It is common to every bin.

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

            bank (int): The memory bank to which the gains should be applied (0 or 1) Once written,
                the bank is made active. If ``bank`` is None, the currently inactive bank is used.

            when (int): Specifies when the specified gains shall become active. If `when` is 'now', the
                gains are written immediately on the target bank and the bank is made active on the
                next frame. If `when` is an  *int*, the gains are written immediately to the bank  bank,
                but than bank will become active only on frame number (timestamp) specified by when.

            gain_timestamp: Unix timestamp when the gains were calculated.  If not provided,
                defaults to current time.

            use_fixed_gain (bool): (deprecated) if True, enables the use of fixed gain mode of the scaler module. In this
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

            if np.all(Glin.imag == 0.) == True:
                if np.isscalar(Glin):
                    Glin = float(Glin.real)
                else:
                    # If the gain is purely real, recover only the real part and convert to float
                    Glin = np.array(Glin).real.astype(float)

            if Glog == 0:
                # A Glog of 0 indicates that floating point gains were saved to the digital gains archive,
                # and we can therefore set Glog to None so that the gains are set 
                # in floating point mode.
                Glog = None

            # Process each channel
            for ch in channel_list:
                # Ignore channels not in the `channels` parameters
                if ch not in channels:
                    continue

                # Warn and skip if a channel does not exist
                if ch not in self.chan.keys():
                    self.logger.warning(f'{self!r}: Gains on channel {int(ch)} are not set because that channel is not available')
                    continue

                if use_fixed_gain:
                    raise DeprecatedError(f'Fix gain feature is deprecated. Set the table to a constant value instead.')
                
                self.logger.debug(f'Slot {self.slot}, channel {ch}: Glin = {Glin}, Glog = {Glog}')

                self.chan[ch].SCALER.set_gain_table(Glin, bank=bank, gain_timestamp=timestamp_value, log_gain=Glog)
                configured_channels.add(ch)

        self.logger.debug(f"{self!r}: Setting scaler gains for channel {', '.join(str(i) for i in configured_channels)}")

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

                 ``linear_gain_table`` is an array of NBIN complex values.
                 ``log gain`` is an integer.
        """
        gain_list = []
        for chan in self.get_channelizers():

            fp_gains_enabled = chan.SCALER.USE_FLOAT_GAINS

            glin = chan.SCALER.get_gain_table(bank=bank, use_cache=use_cache)

            if fp_gains_enabled:
                '''
                Floating-point gain convention: glin corresponds to the full gain, and the "log" part is returned as -1
                to signify that floating-point gains are used (i.e., the full gain is represented by glin, and it is not
                necessary to use glog).
                '''
                self.logger.debug(f'Floating point gains enabled on slot {self.slot}, channel {chan.chan_number}')
                glog = 0

            else:
                glog = chan.SCALER.get_shift_left(bank)

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
        """

        if channels is None:
            channels = self.default_channels

        for chan in self.chan.values():
            if chan.chan_number in channels:
                self.logger.debug(f'{self!r}: Setting FFT shift of channel {int(chan.chan_number)}')
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

                * 'sync_out' : User-generated SYNC signal (sync_out)
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
        """ Sets the source of the signal that will trigger SYNC events in the REFCLK module.

        If the source matches a user I/O SMA, makes sure that I/O is set an an input.

        Parameters:

            source (str): name of the source. See `REFCLK.set_sync_source()` for valid names.

        """
        if not self.REFCLK:
            self.logger.warning('The platform does not support SYNC sources')
            return

        if source not in self.REFCLK.SYNC_SOURCE_TABLE:
            valid_sources = ', '.join(self.REFCLK.SYNC_SOURCE_TABLE)
            raise ValueError(f'Invalid SYNC source name. Valid names are {valid_sources}')
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

    def get_sync_counter(self):
        """Return the REFCLK SYNC event counter.

        Returns:
            int: 4-bit SYNC counter. ``None`` if this firmware has no REFCLK.
        """
        if not self.REFCLK:
            self.logger.warning('The platform does not have a REFCLK module')
            return None
        return int(self.REFCLK.SYNC_CTR)

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
                print(f'error on transaction #{int(i)}')
            except KeyboardInterrupt:
                break
        t1 = time.time()
        self.mb.fpga.set_timeout(old_timeout)
        print(f'{int(trials)} read operations performed in {t1 - t0:.2f} s ({float(n) / (t1 - t0):.0f} read/s) with {int(errors)} errors ({float(errors) / float(trials) * 100:0.3f}% errors)')


    def get_temperatures(self):
        """ Return the core temperature of the fpga and the mezzanine ADC chips through the FPGA's SYSMON and mezzanine SPI links.

        Returns:

            dict in the format {('target': temperature),...}, with an entry for the FPGA core and each of the ADC chips.

        """
        res = {}
        if hasattr(self, 'SYSMON') and self.SYSMON:
            res['FPGA_core'] = self.SYSMON.temperature()
        if hasattr(self, 'mezzanine') and self.mezzanine:
            for mezz_number, mezz in self.mezzanine.items():
                for (adc_number, adc) in enumerate(mezz.ADC):
                    res['FMC%i ADC%i' % (mezz_number - 1, adc_number)] = adc.get_temperature()
        return res

    async def get_fpga_core_temperature_metrics_async(self):
        """ Get the FPGA core temperature and wrap it in a Metrics object.

        Returns:
            a :class:`Metrics` object containing the FPGA core temperature.
        """
        metrics = Metrics(
            type='GAUGE',
            slot=(self.slot or 0) - 1,
            id=self.get_string_id(),
            crate_id=self.crate.get_string_id() if self.crate else None,
            crate_number=self.crate.crate_number if self.crate else None)
        
        try:
            # Get temperatures from the firmware
            temperatures = self.get_temperatures()
            
            # Extract FPGA core temperature
            if isinstance(temperatures, dict) and 'FPGA_core' in temperatures:
                metrics.add('fpga_core_temperature', temperatures['FPGA_core'])
                
        except Exception as e:
            self.logger.error(f'{self!r}: Error getting FPGA core temperature: {e}')
            
        return metrics

    def reset_gpu_links(self):
        """ Resets the GPU links.

        All links are reset. There is no way to reset an individual QSFP or individual link.
        """
        self.GPU.reset()


    async def get_bp_shuffle_metrics_async(self, reset=True):
        if not self.is_open() or not hasattr(self, 'BP_SHUFFLE') or not self.BP_SHUFFLE:
            return Metrics()
        try:
            await self.clear_fpga_udp_errors()
            # await self.check_command_count(reset=True)
            metrics = await self.BP_SHUFFLE.get_metrics(reset=reset)
        except IOError as e:
            self.logger.error(f'{self!r}: Error getting FPGA backplane link metrics. Error is {e!r}')
            metrics = Metrics()
        return metrics

    async def get_channelizer_metrics_async(self, reset=True):
        metrics = Metrics(
            type='GAUGE',
            slot=(self.slot or 0) - 1,
            id=self.get_string_id(),
            crate_id=self.crate.get_string_id() if self.crate else None,
            crate_number=self.mb.get_crate_number())

        if not self.is_open():
            return metrics
        try:
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

    def get_channel_metrics(self, ch, frame_cnt=300_000, polling_interval=0.0001):
        '''
        Accesses the overflow metrics collected in the scaler.

        Internally, this resets the STATS_CAPTURE flag, sets it, then waits for the STATS_READY flag to be asserted and collects the data.

        Parameters:
            ch: the channel to collect the data from

            frame_cnt: number of data frames to integrate these statistics over

            polling_interval: how long to sleep in between checking if the sSTATS_READY has been set

        Returns:
            (scaler_overflow_count, adc_overflow_count)

        '''

        self.chan[ch].SCALER.STATS_FRAME_COUNT = frame_cnt
        self.chan[ch].SCALER.STATS_CAPTURE = 0
        time.sleep(polling_interval * 10)
        self.chan[ch].SCALER.STATS_CAPTURE = 1
        while True:
            if self.chan[ch].SCALER.STATS_READY:
                self.chan[ch].SCALER.STATS_CAPTURE = 0
                return (self.chan[ch].SCALER.STATS_SCALER_OVERFLOWS, self.chan[ch].SCALER.STATS_ADC_OVERFLOWS)
            time.sleep(polling_interval)

    async def get_crossbar_metrics_async(self, reset=True):
        metrics = Metrics()
        if not self.is_open():
            return metrics
        try:
            # await self.check_command_count(reset=True)
            await self.clear_fpga_udp_errors()
            # Check for crossbar objects in the corner-turn engine
            if self.CT:
                # Try to get crossbar objects from the corner-turn engine
                crossbars = []
                if hasattr(self.CT, 'CROSSBAR'):
                    crossbars.append(self.CT.CROSSBAR)
                if hasattr(self.CT, 'CROSSBAR2'):
                    crossbars.append(self.CT.CROSSBAR2)
                if hasattr(self.CT, 'CROSSBAR3'):
                    crossbars.append(self.CT.CROSSBAR3)
                
                for cb in crossbars:
                    if cb:  # make sure the crossbar is in this firmware
                        metrics += await cb.get_metrics(reset=reset)
        except IOError as e:
            self.logger.error(f'{self!r}: Error getting FPGA crossbar metrics. Error is {e!r}\n\n{traceback.format_exc()}')
        except Exception as e:
            self.logger.error(f'{self!r}: Unhandled error while  getting FPGA crossbar metrics. Error is {e!r}\n\n{traceback.format_exc()}')
        return metrics



    def start_correlator(
            self,
            integration_period=16384,
            autocorr_only=False,
            no_accum=False,
            verbose=1):
        """
        (Re)starts the correlator with the specified integration time.

        Parameters:

            integration_period (32-bit int): Number of frames to integrate before sending the
               correlated products. Lower integration period increase the frequency at which
               correlated frames are sent and increase the require bandwidth. Longer integration
               periods will procuce larger accumulated products that will saturate if they exceed
               the accumulator limits (from -131072 to 131071 for each if the real and imaginary
               component).

            autocorr_only (bool): When 'True', the correlator will only send the autocorrelation
               products, which will reduce bandwidth requirement (12% of the full bandwidth) and
               will allow shorter integration periods. Note that the imaginary parts are always zero
               but are sent anyways to keep the frame format identical despite the waste of
               bandwidth.

            no_accum (bool): When 'True', the correlator does not accumulate products from multiple
                frames. It just returns the product from the last frame on the integration period.

        Returns:
            None

        Note:

            - Sets the offest binary encoding to False. This is not restored
              when the correlator is stopped.

        """
        if not self.CORR:
            raise RuntimeError('The FPGA firmware does not contain a correlator core')

        self.CORR.start_correlator(integration_period=integration_period,
                                   autocorr_only=autocorr_only,
                                   no_accum=no_accum,
                                   verbose=verbose)

    def stop_correlator(self):
        if not self.CORR:
            raise RuntimeError('The FPGA firmware does not contain a correlator core')

        self.CORR.stop_correlator()

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

    def test_correlator_output(self, data, integration_period=32768, verbose=0, receiver=None):
        """ Set the channelizer outputs to `data` and check the correlator output.


        Parameters:
            data (ndarray): Data that should appear at the channelizer
                output, indexed as data[channel, bin] = complex_value. channel
                ranges from 0 to 15, bin from 0 to 1023. The complex value has the
                ranged of a signed (4+4) bit, meaning that the real and imaginary
                part can range from -8 to 7.
            receiver: Array correlator receiver from
                ``FPGAArray.get_corr_receiver_async()``.

        """
        if receiver is None:
            raise RuntimeError(
                f"{self!r}: pass receiver from FPGAArray.get_corr_receiver_async()"
            )
        self.set_channelizer_outputs(data)
        self.start_correlator(integration_period=integration_period, verbose=verbose)
        self.sync()
        p = self.compute_corr_output(data, integration_period=integration_period)
        f = receiver.read_corr_frames(flush=False, complete_set=True, max_trials=100, verbose=verbose)
        return np.all(p == f), p, f

    def test_correlator(self, test_name='rand_complex', integration_period=8192, trials=100, verbose=0):
        if test_name == 'rand_complex':
            for data_set_number in range(trials):
                print(f'Trial #{int(data_set_number)}')
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
                            print(f"[{datetime.now().strftime('%H:%M:%S.%f'):s}]: Test #{data_set_number:d}/{trials:d}; trial {trial:d}")
                            print(f"\t{timestamp:d}")
                        break
                    trial += 1
                    if trial < 10:
                        print(f'Trial {trial:d} frames did not match! Retrying after rewriting the test data again...')
                        if(verbose):
                            print("Difference: ")
                            print(p - f)
                    else:
                        print('Cannot make frames match!')
                        return match, data, p, f

        else:
            raise ValueError(f'Unknown test name {test_name}')

        print('*** TEST PASSED! ***')
        return True, None, None, None
