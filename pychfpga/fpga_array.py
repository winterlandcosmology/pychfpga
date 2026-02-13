#!/usr/bin/python
"""
fpga_array.py module. Defines the objects that represent and handles
operations on an array of FPGA motherboards.
"""


# Python Standard Library packages

import argparse
import logging
import logging.handlers
import time
import __main__
import os
import sys
import socket  # for gethostbyname()
# from collections import OrderedDict
import pickle
import re
import datetime
import functools
import zlib
import base64
import asyncio

# PyPi packages
import numpy as np
import matplotlib.pyplot as plt

# For development: delete all fpga modules so fresh ones will be reloaded
# if getattr(__main__, '__reload__', False):
#     print('Clearing all pychfpga modules...')
#     for n, m in sys.modules.items():
#         if n.startswith('pychfpga'):
#             del sys.modules[n]

# Local imports

# Automatically update the search path for absolute imports of pychfpga and
# its subpackages. We use absolute imports because 1) if both relative and
# absolute imports are made, then  modules are loaded multiple times and
# SQLAlchemy complains. 2) we cannot access pychfpga subpackages if we run
# this module as a script because Python refuses to consider the script folder
# as a package.
try:
    import pychfpga
except ImportError:
    pychfpga_path = os.path.realpath(os.path.join(os.path.dirname(__file__), '..'))
    if pychfpga_path not in sys.path:
        sys.path.insert(0, pychfpga_path)

from wtl import log
from wtl.metrics import Metrics
from wtl.namespace import NameSpace, merge_dict
from wtl.config import load_yaml_config

from pychfpga.common import Ccoll
from pychfpga.common.udp_packet_receiver import UDPPacketReceiver

from pychfpga.mdns_discovery import mdns_discover

from pychfpga.hardware import Motherboard, Crate

# Import platform-specific hardware-handling classes that are to be supported by fpga_array.
# By importing those, we make sure they are registered in the hardware map.
# Once registered, the we can find classes based on model numbers.
from pychfpga.hardware.ice import IceBoard, IceCrate
from pychfpga.hardware.zcu111 import ZCU111
from pychfpga.hardware.crs import CRS
from pychfpga.hardware.zuboard import ZUBoard
from pychfpga.hardware.Agilent_N5764A import AgilentN5764A



#####################################




class FPGAArray(object):

    # Map the string to the function that computes the FPGA IP address from the ARM IP address
    FPGA_IP_ADDR_FN_TABLE = {
        '(a,b,3,d)': lambda a, b, c, d: (a, b, 3, d),
        '(a,b,c+1,d)': lambda a, b, c, d: (a, b, c + 1, d),
    }

    def __init__(
            self,

            hwm=None,
            iceboards=[],
            icecrates=[],
            mezzanines=[],
            exclude_iceboards=[],
            ignore_missing_boards=False,

            subarrays=None,
            ping=True,
            mdns_timeout=2,
            no_mezz=False,

            bitfile=None,
            prog=None,
            open=None,
            init=None,
            if_ip=None,
            udp_retries=10,
            fpga_ip_addr_fn='(a,b,3,d)',

            sampling_frequency=None,
            # reference_frequency=10e6,
            # data_width=4,

            sync_method=None,
            sync_source=None,
            sync_master=None,
            sync_master_source=None,
            sync_master_output=None,
            max_sync_time_difference=100,

            adc_mode=0,
            adc_bandwidth=2,

            mode=None,
            frames_per_packet=None,
            chan8_channel_map=list(range(8)),
            tx_power=None,
            integration_period=None,
            autocorr_only=False,
            corner_turn_bad_links=None,
            corner_turn_bin_priority=None,
            corner_turn_remap_level=0,

            stderr_log_level=None,
            syslog_log_level=None,

            ioloop=None,

            **kwargs):
        """ Create a hardware map describing CHIME hardware and optionally
        initialize the hardware.

        .. centered:: **Hardware map creation parameters**

        Parameters:

            hwm (str, list or HardwareMap): Describes the hardware
                map, which lists the IceBoards, IceCrates and Mezzanines
                that are present in the system and their relationship. The hardware
                map creation depending on the type of the `hwm` parameter.

                - *str* or *list of str* : A string or list of strings that describes the hardware to be
                  added to the hardware map in the format::

                      hwm := {board_descriptors | crate_descriptors | mezzanine_descriptors} ...

                  where

                  ``Iceboard_descriptors := {[MGK7]MB serial [serial] ... | hostname | ip_address}``

                  ``Icecrate_descriptors := {[MGK7]BP16 serial[:crate_number] [serial[:crate_number]] ...}``,

                  Autodiscovery is used as needed to complete the hardware map
                  (see notes below).

                - *list of dict*: list of dicts that describe the hardware map elements to create.

                - *HardwareMap*: Fully formed hardware map, provided directly as a
                  HardwareMap database object. If a fully-formed hardware map is
                  provided, it will additionally be vetted by removing the
                  Iceboards that fail the `ping` test and do not meet the
                  ``subarray`` criteria.


            iceboards (list of str): Motherboard to add to the hardware map,
                specified as an IP address, hostname, or serial number. The
                boards specified here are added to the hardware map specified
                in `hwm` parameter. When a serial number is used, the IceBoard
                model MGK7MB is implied. For example, specifying ``iceboards =
                "003 007"`` is equivalent to adding ``"MGK7MB 003 007"`` to
                the `hwm` parameter.

            exclude_iceboards (list of str): Serial numbers of Iceboards to be
                excluded in case of autodiscovered boards. Can be useful to specify a
                crate but exclude a few boards.

            icecrates (list of str): Adds all the iceboards from the crates that have the
                serial numbers specified in the provided list of strings.

                Examples::

                    ``icecrates='003'`` or ``icecrates=['003']`` will discover and select all boards from crate SN003

                    ``icecrates=['003', '004']`` will select boards from crates SN003 and SN004.

                    ``icecrates=[]`` will select all boards on the network



        .. centered:: **Hardware map filtering parameters**

        Parameters:


            subarrays (list): List of integers describing the subarrays to include in
                the default Motherboard set. If None, all
                Iceboards in the hardware map will be selected. Affects only the
                boards specified in the hardware map specified with the ``hwm`` parameter.

            ping (int): If ``ping=1``, The connection to Iceboards is checked
                by sending a dummy Tuber (http) request to their ARM processors. If a
                YAML-specified iceboards fails, it is simply removed from the
                ``hwm`` hardware map, but an exception is raised if a board listed
                explicitely fails. If ``ping`` is false, the presence of boards is not
                checked.


        .. centered:: **Configuration & initialization parameters**

        Parameters:

            mode (str): Operational mode of required from the Motherboard
                array.

                The mode is used to select which FPGA firmware to load, unless
                overriden by the `bitfile` parameter .

                When `mode` is specified, the FPGA configuration (prog=2) and
                full initialization level (init=2) is implied, unless specifically
                modified by the prog and init commands.

                The following modes are supported:

                - Ice hardware platform

                    - 'chan8': 8-channel, 8-bit data streaming. The output of 8 F-Engine is streamed out on the 8 GPU links. Corner-turn engine is mostly bypassed.

                    - 'chan4': Corner-turn engine is mostly bypassed and raw 8-bit data from 16
                      channelizers is sent directly to the 8 CT-Engine outputs.

                    - 'shuffle16': A corner-turn operation is applied only within the 16 channelizer
                      outputs of this board.

                    - 'shuffle128': A corner-turn operation is applied between the first 8 boards in a crate.

                    - 'shuffle256': The corner-turn operation is applies between 16 channelizers within
                      a board and between the 16 boards within a crate using the backplane PCB links.

                    - 'shuffle512': The corner-turn operation is applies between 16 channelizers within
                      a board, between the 16 boards within a crate using the backplane PCB links,
                      and between 2 crates using the backplane QSFP links.

                    - 'corr16': The corner-turn engine is configured to feed the
                      internal firmware correlator (only if the firmware was
                      compiled with it).

                - ZCU111

                    - 'corr4'

            bitfile (str): If `bitfile` points to a folder, use that folder to look for the default
                FPGA bitstream base don the platform/mode. If `bitfile` points to a file, use that
                file as a bitstream (but mode is still required to properly initialize the python
                object and firmware)

            prog (int): Overrides the FPGA configuration level. If `mode` is specified, `prog`
                defaults to `prog` =2 otherwise `prog` =0.

                - prog=0 or None: The FPGAs are not configured
                - prog=1: the FPGAs are configured only if they are not already configured with the same
                  firmware.
                - prog=2: The FPGAs are always configured, ensuring a clean-state start.

            init (int): Sets the array initialization level. Defaults to ``init=3`` if `mode` is specified, otherwise defaults to ``init=0``

                - init=0: Only creates the array and establish communication with the motherboards.
                - init=1: like init=0, but configure the FPGA using prog=2
                  unless explicitely specified, and creates the ``fpga`` object representing the loaded firmware. `mode` or `bitfile` must be specified so
                  the bitfile to be used can be found.
                - init=2: like init=1, but basic communication with the FPGAs
                  is established and the `fpga` object is populated with the
                  objects representing the firmware modules.
                - init=3: full initialization of the FPGA in the specified `mode` is performed

            open (int): Alternate method to set the initialization
                level:

                - open=None: Use the default init level
                - open=0: sets init=1 (do not open communication with FPGA firmware; only connect to the platform)
                - open=1: sets init=3 (connect with the FPGA firmware and initializes it fully)

            if_ip (str): string corresponding to the IP address of adapter through
                which the connection to the FPGA will be established. If not
                specified, the system will assume that the FPGA is reached trough
                the same interface that reaches the platform on-board processor.


            udp_retries (int): Number of retries performed when UDP command
                packets sent to the FPGA do not receive a response. Applies
                only to platform that feature direct UDP communication with
                the FPGA.

            fpga_ip_addr_fn (str): For platforms featuring direct UDP
                connection with the FPGA, specifies how the FPGA IP address is
                determined. The valid modes are shown below, and should be
                types exactly without additional spaces. In those modes,
                ``a.b.c.d`` corresponds to the IP address of the IceBoard ARM
                processor.

                - '(a,b,3,d)': Uses the IP address of the ARM but replaces the third byte by ``3``
                - '(a,b,c+1,d)': Uses the IP address of the ARM but adds 1 to the third byte


            sync_method (str): Method used to synchronize (to sync) the data acquisition on an array of boards:

                - 'local': Syhchronize each board individually by software trigger. There is no synchronicity between boards.
                - 'irigb': uses the IRIG-B time signal to perform an array-wide synchronization. The source of the IRIG-B signal can be specified in `sync_source`.
                - Other modes are available

            sync_source (str): Source of the signal that is used to synchronize each board


            sync_master (Motherboard or str): Board used to generate the synchrization signal,

            sync_master_source (str): Source of the synchronization signal generated by the master board

            sync_master_output (int): User output on which the synchronization signal is routed on the master board

            max_sync_time_difference (int): Maximum time difference, in
                nanoseconds, between the time of frame 0 of each board in an
                array.

        .. centered:: **Corner-turn engine parameters**

        Parameters:

            frames_per_packet (int): Number of frames that are grouped in each
                packets at the output of the corner turn engine. Default is 2.
                Packets contents is ordered so that the data from both frames
                shows in a single block, followed by the scaler flages for
                both frames, etc. Is limited by the amount of buffering
                available in the FPGA's corner turn engine stages. Contiguous
                data blocks help the receiver node make more efficient memory
                transfers.

            tx_power (dict): Initial training and final TX power levels to be
                assigned to the data links used by the corner turn engine.
                These include the backplane PCB links between boards in a
                crate, backplane QSFP links between crates, or iceboard QSFP
                links that offload  the output of the corner turn engine.


            corner_turn_bad_links (list of tuples): List of corner turn engine
                outputs links [(crate, slot, links), ...] that should
                preferably NOT be assigned any frequency bins if possible.

            corner_turn_bin_priority (list of int): List of frequency bins
                that are to be assigned to the corner turn output, in order of
                priority (most desirable frequency first). The lowest priority
                bins will be assigned to the bad links as much as possible,
                depending on the constraints of the corner turn engine
                flexibility.

            corner_turn_remap_level (int): Sets the aggressivness of the remapping by
                selecting how many crossbar levels are involved. 0:no
                remapping, 1: 3rd crossbar only, 2: crossbars 2 and 3, 3: all
                crossbars.

        .. centered:: **Firmware correlator parameters**

        Parameters:

            integration_period (int): =None,


        .. centered:: **Logging parameters**

        Parameters:

            stderr_log_level (str): sets up a handler that prints logs of specified log level on stderr.

            syslog_log_level (str): sets up a SYSLOG handler


        Logging
        -------

        You can optionally specify log level arguments to create syslog and stderr handlers to help interactive operations. If a
        handler already exists, its log level is simply updated to prevent duplication of
        handlers. Log levels can be strings or numerical log levels.



        Example:

            **Hardware map specifications**

            ``hwm="MGK7BP16 025 026"`` or abbreviated form ``hwm="BP16 25 26"`` selects
            all iceboards on crates SN025 and SN026 with default crate
            numbers 0 and 1 respectively.

            ``hwm="MGK7MB 0125 0330"`` or ``hwm="MB 0125 0330"``, ``hwm="MB 125 330"``, ``hwm="MB
            10.10.10.225 10.10.10.111"`` or ``hwm="MB iceboard0125.local
            iceboard0330.local"`` all select the Iceboards specified by
            serial/hostname/IP address.

            Note:

                If a Motherboard is specified by IP address (e.g. '10.10.10.7'),
                then the board can be added directly in the hardware map. This
                does *not* rely on the system mDNS client or the Python
                ``pybonjour`` package.

                If an Motherboard is specified by its mDNS hostname (e.g.
                'iceboard0007.local'), the operating system will automatically
                resolve the IP address using mDNS, assuming that a mDNS client
                (Bonjour on Windows or Mac, avahi on Linux) is running on this
                computer. The ``pybonjour`` Python package is *not* needed.

                In both cases, the crate, slot and serial number information will
                be automatically obtained directly through the Motherboard's
                processor if that information not already present in the hardware
                map.

                If an Motherboard is specified by its serial number (e.g. '0007', or
                just a numeric 7 as a convenient shortcut), the board will use mDNS to
                actively find and query  boards that match the serial number.


            Note:

                Selecting boards by Crate serial number *always*
                require mDNS queries to discover the
                specified Motherboards that advertised themseles along with
                their associated crate number.

        """
        async_init = functools.partial(
            self.init,
            hwm=hwm,
            iceboards=iceboards,
            icecrates=icecrates,
            mezzanines=mezzanines,
            exclude_iceboards=exclude_iceboards,
            ignore_missing_boards=ignore_missing_boards,
            subarrays=subarrays, ping=ping,
            mdns_timeout=mdns_timeout,
            no_mezz=no_mezz,
            bitfile=bitfile,
            prog=prog,
            open=open,
            init=init,
            if_ip=if_ip,
            fpga_ip_addr_fn=fpga_ip_addr_fn,
            sync_method=sync_method,
            sync_source=sync_source,
            sync_master=sync_master,
            sync_master_source=sync_master_source,
            sync_master_output=sync_master_output,
            max_sync_time_difference=max_sync_time_difference,
            adc_mode=adc_mode,
            adc_bandwidth=adc_bandwidth,
            mode=mode,
            frames_per_packet=frames_per_packet,
            chan8_channel_map=chan8_channel_map,
            sampling_frequency=sampling_frequency,
            tx_power=tx_power,
            integration_period=integration_period,
            autocorr_only=autocorr_only,
            corner_turn_bad_links=corner_turn_bad_links,
            corner_turn_bin_priority=corner_turn_bin_priority,
            corner_turn_remap_level=corner_turn_remap_level,
            stderr_log_level=stderr_log_level,
            syslog_log_level=syslog_log_level,
            udp_retries=udp_retries,
            **kwargs)

        if not ioloop:
            # Create our own IOLoop so we don't interfere with ipython's own ioloop.
            # old_ioloop = IOLoop.current()
            # ioloop = IOLoop()
            # ioloop.make_current()
            # ioloop.run_sync(init)
            # old_ioloop.make_current()
            try:
                asyncio.run(async_init(), debug=True)
            except BaseException as e:
                # print(f'fpga_array: run got exception {e}')  # maybe add message to log?
                raise
        else:
            self._async_init = async_init
            # old_ioloop = IOLoop.current()
            # ioloop.make_current()
            # future = init()
            # ioloop.add_future(future)
            # while not future.done():
            #   print('waiting', future, future.done())
            #   time.sleep(.3)
            #   pass
            # old_ioloop.make_current()

        # else:
        #     old_ioloop = None
            # old_ioloop = IOLoop.current
            # IOLoop.set_current(ioloop)

    async def run(self):
        await self._async_init()

    async def init(
            self,

            hwm=None,
            iceboards=[],
            icecrates=[],
            mezzanines=[],
            exclude_iceboards=[],
            ignore_missing_boards=False,

            subarrays=None, ping=True,
            mdns_timeout=2,
            no_mezz=False,

            mode=None,
            bitfile=None,
            prog=None,
            open=None,
            init=None,

            if_ip=None,
            fpga_ip_addr_fn='(a,b,3,d)',
            udp_retries=10,

            sync_method=None,
            sync_source=None,
            sync_master=None,
            sync_master_source=None,
            sync_master_output=None,
            max_sync_time_difference=20,

            adc_mode=0,
            adc_bandwidth=2,

            frames_per_packet=2,
            chan8_channel_map=list(range(8)),
            sampling_frequency=None,
            tx_power=None,
            integration_period=None,
            autocorr_only=None,
            corner_turn_bad_links=None,
            corner_turn_bin_priority=None,
            corner_turn_remap_level=0,

            stderr_log_level=None,
            syslog_log_level=None,

            # ioloop=None,

            **kwargs):

        """
        Co-routine implementation of fpga_array initialization
        """
        self.ib = []  # make sure repr() has always something
        self.ic = []
        self.hwm = {}
        self.sync_timestamp = None

        self.max_sync_time_difference = max_sync_time_difference
        self.tx_power = tx_power
        self.mode = mode

        self.packet_receiver = None
        self.corr_recv = None
        self.data_recv = None

        discover_slot = True
        discover_crate = True
        ###########################################
        # setup pychfpga.fpga_array logging
        ###########################################
        # self.logger = logging.getLogger(__name__)
        self.logger = log.get_logger(self)

        ###########################################
        # setup pychfpga package logging
        ###########################################
        # This sets the logging that comes out from *all* the modules within *pychfpga* package
        # We are careful not to set the root logger, which might have its own
        if stderr_log_level or syslog_log_level:
            parent_logger_name = __name__.rsplit('.', 1)[0] if '.' in __name__ else ''
            parent_logger = logging.getLogger(parent_logger_name)
            # Setup logging. If a handler already exists, its log level is simply updated
            for (handler_type, log_level) in (
                    (logging.StreamHandler, stderr_log_level),
                    (logging.handlers.SysLogHandler, syslog_log_level)):
                if log_level:
                    log_handlers = [h for h in parent_logger.handlers if isinstance(h, handler_type)]
                    if log_handlers:  # if a handler of that type already exist, just use it
                        print(f'Updating existing log handlers of {parent_logger} to {log_level}')
                    else:  # otherwise create a new one
                        log_handler = handler_type()
                        parent_logger.addHandler(log_handler)
                        print(f'Adding new log handlers of {parent_logger} with level {log_level}')
                    # set level for all handlers of this type
                    for h in parent_logger.handlers:
                        if isinstance(h, handler_type):
                            h.setLevel(log_level.upper() if isinstance(log_level, str) else log_level)
                            # make sure all messages from this handler are passed to the parent handler
                            parent_logger.setLevel(min(parent_logger.level, h.level))

        self.logger.info(f'{self!r}: ------------------------')
        self.logger.info(f'{self!r}: F P G A   A R R A Y')
        self.logger.info(f'{self!r}: ------------------------')
        self.logger.info(f'{self!r}: Called with for following parameters')
        self.logger.info(f'{self!r}:     if_ip = {if_ip}')
        self.logger.info(f'{self!r}:     iceboards = {iceboards}')
        self.logger.info(f'{self!r}:     icecrates = {icecrates}')
        self.logger.info(f'{self!r}:     subarrays = {subarrays}')
        self.logger.info(f'{self!r}:     ping = {ping}')
        self.logger.info(f'{self!r}:     mdns_timeout = {mdns_timeout}')
        self.logger.info(f'{self!r}:     exclude_iceboards = {exclude_iceboards}')
        self.logger.info(f'{self!r}:     bitfile = {bitfile}')
        self.logger.info(f'{self!r}:     prog = {prog}')
        self.logger.info(f'{self!r}:     open = {open}')
        self.logger.info(f'{self!r}:     init = {init}')
        self.logger.info(f'{self!r}:     no_mezz = {no_mezz}')
        self.logger.info(f'{self!r}:     sampling_frequency = {sampling_frequency}')
        # self.logger.info(f'{self!r}:     reference_frequency = {reference_frequency}')
        self.logger.info(f'{self!r}:     sync_method = {sync_method}')
        self.logger.info(f'{self!r}:     sync_source = {sync_source}')
        self.logger.info(f'{self!r}:     fpga_ip_addr_fn = {fpga_ip_addr_fn}')
        self.logger.info(f'{self!r}: ------------------------')

        __main__._host_interface_ip_addr = if_ip

        # COnvert the FPGA IP-setting function name to an actual function
        if fpga_ip_addr_fn in self.FPGA_IP_ADDR_FN_TABLE:
            fpga_ip_addr_fn = self.FPGA_IP_ADDR_FN_TABLE[fpga_ip_addr_fn]
        else:
            valid_fn = ', '.join(f"'{fn}'" for fn in self.FPGA_IP_ADDR_FN_TABLE)
            raise AttributeError(
                f"Invalid 'fpga_ip_addr_fn' string '{fpga_ip_addr_fn}'. "
                f"Valid strings are  {valid_fn}")

        Motherboard.clear_hardware_map()
        # Fix up a few parameters for convenience
        # Add `iceboards'
        if isinstance(iceboards, (str, int)):
            iceboards = [iceboards]
        if iceboards:
            self.process_str_hwm(f"MGK7MB {' '.join(str(ib) for ib in iceboards)}")

        # Add `icecrates`
        # Make sure iceboards is a list
        if isinstance(icecrates, (str, int)):
            icecrates = [icecrates]
        if icecrates:
            self.process_str_hwm(f"MGK7BP16 {' '.join(str(ic) for ic in icecrates)}")

        # print(f'HWM before = {Motherboard.get_all_instances()}')

        # iceboards = [self._to_integer(x) for x in iceboards]

        ###########################################
        # String-based hardware map processing
        ###########################################
        # convert list of strings into a single string
        if isinstance(hwm, list) and all(isinstance(i, str) for i in hwm):
            hwm = ' '.join(hwm)

        if isinstance(hwm, str):
            self.process_str_hwm(hwm)
        elif isinstance(hwm, list):
            self.process_list_hwm(hwm)
        elif not hwm:
            pass
        else:
            raise TypeError('hwm must be either a str or a list of dict')

        ######################################
        # Hardware map processing
        ######################################
        # If a pre-formed hardware map is provided in the form of a
        # hardwareMap object or a list of strings, filter the map for non-
        # responding boards or boards that do not belong to the target
        # subarray.

        # self.logger.debug('hardware map=%s' % hwm)

        # If no hardware map is provided, create an empty one
        # if not hwm:
        #     # self.hwm = HardwareMap()  # Create empty hardware map
        #     self.hwm = []

        self.hwm = Motherboard._instance_registry
        # If the hwm parameter is a non-empty list of  dicts, create the hardware map by instantiating the
        # object of the type contained in the ``class`` element and passing it the remaining
        # elements as keyword arguments

        # Otherwise use the hardware map as is, hoping it is a valid hardware map
        # else:
        #     self.hwm = hwm

        # If subarrays are specified, remove boards that are not in those subarrays
        if subarrays is not None:
            # print('Subarrays are: %r' % subarrays)
            for ib in list(self.hwm):  # make a copy to be sure the list does not change during the loop
                if ib.subarray is not None and ib.subarray not in subarrays:
                    ib.delete_instance()
                    self.logger.debug(f"{self!r}: {ib} (subarray '{ib.subarray}') is not in the target subarray list {subarrays}. "
                                      f"It is removed from the YAML hardware map.")

        # If ping=1, remove boards that do not respond to tuber pings
        ping_timeout = 3
        missing_boards = []
        if ping:
            boards_to_ping = [ib for ib in self.hwm if ib.hostname]
            if boards_to_ping:
                # Find iceboards to ping. Use `as_dict` so ib_to_ping does not change as we delete boards from the hwm
                self.logger.info(f'{self!r}: Pinging {len(boards_to_ping)} Motherboards with explicit hostnames')
                # TCP-ping boards. Make is an asynchronous parallel call to all boards
                ping_results = await asyncio.gather(*[ib.ping_async(timeout=ping_timeout) for ib in boards_to_ping])
                # self.logger.debug('%r: Ping results are %s' % (self, ping_results))
                for ib, ping_successful in zip(boards_to_ping, ping_results):
                    if ping_successful:
                        ib.hostname = socket.gethostbyname(ib.hostname)
                    else:
                        missing_boards.append(
                            f"{ib.hostname} (SN{ib.serial or '????'}, "
                            f"({ib.crate.crate_number if ib.crate else '?'},{ib.slot - 1 if ib.slot else '?'})")
                        self.logger.debug(f'{self!r}: Deleting {ib} from the YAML hardware map')
                        ib.delete_instance()
        else:
            self.logger.info(f'{self!r}: Ping not performed on IceBoards with explicit hostnames. ')
        if missing_boards:
            message = f"{self!r}: Could not ping the following boards: {', '.join(missing_boards)}"
            if ignore_missing_boards:
                self.logger.warning(message)
            else:
                raise RuntimeError(message)

        #######################################################
        # Adding iceboards with explicit hostnames/IP addresses
        #######################################################
        # We now add to the hwm the boards that were specified in the hardware description string.
        #
        # We first extract iceboards that are explicitely listed with IP addresses or hostname instead of
        # serial numbers. We can talk to these boards, which means we can figure out everything we
        # need from these boards (serial, crate, slot  etc) right away without requiring us to do
        # mDNS query, which is the last resort (because not all systems might have mDNS support).

        # if hw_table.iceboards:
        #     self.logger.info('%r: Adding Iceboards listed in hardware description string '
        #                      'with explicit hostnames' % self)
        #     added_ib = []
        #     for (model, hostname) in list(hw_table.iceboards):  # make a copy: we modify in-place
        #         if model is None or '.' in str(hostname):  # if it is actually a hostname. Might be an int serial
        #             # remove it. We'll be left with boards that require mDNS...
        #             hw_table.iceboards.remove((model, hostname))
        #             # ip_addr = socket.gethostbyname(hostname)  # convert hostname to IP address for faster Tuber access
        #             new_ib = IceBoar.get_unique_instance(hostname=hostname)
        #             added_ib.append(new_ib)

        #     # Explicitely listed boards must exist on the network
        #     if ping and added_ib:
        #         self.logger.info('%r: Pinging Iceboards with explicit hostnames '
        #                          'in the hardware description string' % self)
        #         # ping all boards concurrently
        #         ping_results = await asyncio.gather(*[ib.ping_async(timeout=ping_timeout) for ib in added_ib])
        #         # If *ANY* of the boards failed to respond, raise an exception
        #         if not all(ping_results):
        #             raise RuntimeError("%r: The following Iceboards could not be pigned: '%s'" % (
        #                 self,
        #                 ', '.join('%r (%s)' % (ib, ib.tuber_uri)
        #                           for ib, ping_result in zip(added_ib, ping_results))))

        #         # Resolve hostnames into IP addresses This is not concurrent,
        #         # unfortunately... this is why we checked ping first,
        #         # otherwise it blocks for a long time
        #         self.logger.info('%r: Resolving IP addresses of Iceboards that passed the ping test' % self)
        #         t0 = time.time()
        #         for ib, ping_result in zip(added_ib, ping_results):
        #             if ping_result:
        #                 ib.hostname = socket.gethostbyname(ib.hostname)
        #         self.logger.info('%r: Finished resolving IP addresses. It took %s seconds' % (self, time.time() - t0))

        ###########################################################################
        # mDNS discovery of boards and crates specified by model/serial number only
        ###########################################################################
        # The serial and slot numbers will be set based on the data returned by the MDNS TXT fields

        # We search for motherboards that are specified by serial number but have no hostname
        # A generic Motherboard (empty part_number) indicates we look for any part number ('*').
        ib_to_discover = [(ib.part_number or '*', ib.serial)
                          for ib in Motherboard.get_all_instances()
                          if ib.serial and not ib.hostname]
        # Search for boards on crates that have a serial number but no assigned boards (ic.slot is empty).
        # If the crate has boards already assigned, we assume they are all assigned and we don't need to search
        # for more boards on this crate.
        ic_to_discover = [(ic.part_number, ic.serial)
                          for ic in Crate.get_all_instances()
                          if ic.serial and not ic.slot]
        # Remove iceboards with wildcard serial numbers
        for ib in Motherboard.get_all_instances():
            if ib.serial == '*':
                ib.delete_instance()
        if ib_to_discover or ic_to_discover:
            self.logger.info(f'{self!r}: Discovering Motherboards and Crates specified by serial number using mDNS')
            self.logger.info(f'{self!r}:     Motherboards to find: {ib_to_discover}')
            self.logger.info(f'{self!r}:     Crates to find: {ic_to_discover}')
            self.print_flush()
            # Perform mDNS discovery. This is done on a separate ioloop, which locks up the current loop for a while
            await mdns_discover(
                iceboards=ib_to_discover,
                icecrates=ic_to_discover,
                timeout=mdns_timeout)

        ########################################################
        # Establish communication with the motherboards
        ########################################################
        # At this point all the motherboards should have a hostname. We need
        # to establish communication with them to self-discover the missing
        # serial/slot/crate information.


        self.logger.debug(f'{self!r}: Hardware map so far:')
        for ib in self.hwm:
            self.logger.debug(f'    {ib}(hostname={ib.hostname}, serial={ib.serial}, slot={ib.slot})')

        if self.hwm:
            self.logger.info(f'{self!r}: Establishing communication with the motherboards')
            t0 = time.time()
            result = await asyncio.gather(*[ib.open_platform_async() for ib in self.hwm if ib.hostname])
            self.logger.info(f'{self!r}: Connection with {len(result)} Motherboard established. '
                             f'It took {time.time() - t0:.3f} seconds')

        ########################################################################################
        # Resolve missing serial/crate/slot info through the motherboard's on-board processor
        ########################################################################################
        # Complete serial, crate and slot information on Motherboard that miss
        # that information by talking directly to the ARM
        # (i.e without using mDNS).
        ib_without_serial = [ib for ib in self.hwm if ib.hostname and ib.serial is None]
        if ib_without_serial:
            t0 = time.time()
            self.logger.info(f'{self!r}: Auto-Discovering the serial number of the Motherboards with known hostnames')
            ad_boards = ', '.join(ib.hostname for ib in ib_without_serial)
            self.logger.debug(f'{self!r}: Serial Auto-discovery is performed on the following boards: {ad_boards}')
            # concurrently resolve serials
            await asyncio.gather(*[ib.discover_serial_async() for ib in ib_without_serial])
            # self.logger.info('%r: Got all discover_serial futures after %f seconds' % (self, time.time() - t0))
            # [ib.discover_serial.async() for ib in ib_without_serial]
            self.logger.debug(f'{self!r}: Finished Auto-Discovering serial number for Motherboards. '
                             f'Took {time.time() - t0:.3f} seconds.')

        if discover_slot:
            # Find all boards with hostname for slot number check. We re-check
            # the slot number of all boards, wether or not they already have
            # one, so we can issue a warning if they differ.
            ib_without_slot = [ib for ib in self.hwm if ib.hostname]
            if ib_without_slot:
                self.logger.info(f'{self!r}: Auto-Discovering & validating the slot numbers '
                                 f'for {len(ib_without_slot)} Motherboards with known hostnames...')
                t0 = time.time()
                await asyncio.gather(*[ib.discover_slot_async() for ib in ib_without_slot])
                self.logger.info(f'{self!r}: Finished Auto-Discovering slot numbers for Motherboards. '
                                 f'Took {time.time() - t0} seconds.')

        self.logger.debug(f'{self!r}: Hardware map so far:')
        for ib in self.hwm:
            self.logger.debug(f'    {ib}(hostname={ib.hostname}, serial={ib.serial}, slot={ib.slot})')

        if discover_crate:
            # select boards that do not have a crate, or ones that have a generic crate (no part number)
            ib_without_crate = [ib for ib in self.hwm if ib.hostname and (not ib.crate or not ib.crate.part_number)]
            if ib_without_crate:
                t0 = time.time()
                hostnames = ', '.join(ib.hostname for ib in ib_without_crate)
                self.logger.info(f'{self!r}: Auto-Discovering crate information '
                                 f'for Motherboards with known hostnames: {hostnames}')
                await asyncio.gather(*[ib.discover_crate_async() for ib in ib_without_crate])
                self.logger.info(f'{self!r}: Finished Auto-Discovering crate serial numbers. '
                                 f'Took {time.time() - t0} seconds.')

        ###########################################################################
        # Exclude boards
        ###########################################################################
        for ib in Motherboard.get_all_instances():
            if ib.serial and exclude_iceboards:
                int_serial = self._to_integer(ib.serial)
                if ib.serial in exclude_iceboards or int_serial in exclude_iceboards:
                    ib.delete_instance()
                    self.logger.info(
                        f'{self!r}: Removing Motherboard {ib} based on '
                        f'exclusion list: {exclude_iceboards}')

        ###########################################################################
        # Cleanup hardware map
        ###########################################################################
        used_icecrates = {ib.crate for ib in Motherboard.get_all_instances()}
        unused_icecrates = set(Crate.get_all_instances()) - used_icecrates
        for ic in unused_icecrates:
            self.logger.warning(f'{self!r}: Removing unused Crate {ic}'
                                f'(part_number={ic.part_number}, serial={ic.serial}, crate_number={ic.crate_number})')
            ic.delete_instance()

        unresolved_iceboards = [ib for ib in Motherboard.get_all_instances() if not ib.hostname]
        if unresolved_iceboards:
            for ib in unresolved_iceboards:
                self.logger.info(f'{self!r}: Unresolved {ib}'
                                 f'(part_number={ib.part_number}, serial={ib.serial}, hostname={ib.hostname})')
            raise RuntimeError(f'Unresolved motherboards {unresolved_iceboards}')

        self.logger.info(f'{self!r}: Hardware map is complete')

        self.logger.debug(f'HWM={self.hwm}')

        #################################
        # Resolve crate slot mapping
        #################################

        # Fill the slot information in all crates
        # for ib in self.hwm:
        #     if ib.crate and ib.slot and ib.crate.slot[ib.slot] is not ib:
        #         self.logger.warning(f'{self!r}: Had to assign {ib} to slot {ib.slot} of crate {ib.crate}')
        #         ib.crate.slot[ib.slot] = ib

        #################################
        # Set the crate numbers
        #################################
        # ... using whatever map we could determine from the parameters

        # self.set_crate_numbers(crate_number_map, strict=False)
        # print('Crates:')
        # for ic in Crate.get_all_instances():
        #     print(f'{ic}(serial={ic.serial}, crate_number={ic.crate_number}')

        #################################
        # Check if all the hardware we wanted is present
        #################################
        # # List all the crates we know about along with their model/serial tuple
        # # Format: {icecrate_object : (model, integer_serial),...}
        # current_crates = {c: (c.part_number, self._to_integer(c.serial)) for c in Crate.get_all_instances()}

        # # Find if explicitely requested crates were not found
        # missing_crates = [(ic.part_number, ic.serial) for ic in Crate.get_all_instances()
        #                   if not ic.slot or not all(ib.hostname for ib in ic.slot.values())]
        # if missing_crates:
        #     raise RuntimeError(
        #         '%r: The following crates are missing: %s'
        #         % (self, ', '.join('%s SN%s' % (model, serial)
        #                            for (model, serial) in missing_crates)))

        # Check for missing boards in explicitely-specified crates
        missing_slots = {
            (ic.part_number, ic.serial, ic.crate_number): set(range(1, ic.NUMBER_OF_SLOTS + 1)) - set(ic.slot)
            for ic in Crate.get_all_instances()}
        if any(missing_slots.values()):
            missing_slots_str = '\n'.join(
                '    Crate #{number} ({model} SN{serial}): slots {slots}'.format(
                    number=number,
                    model=model,
                    serial=serial,
                    slots=', '.join(str(s) for s in slots))
                for ((model, serial, number), slots) in missing_slots.items() if slots)
            message = f'{self!r}: The following slots are missing:\n{missing_slots_str}'
            if not ignore_missing_boards:
                raise RuntimeError(message)
            else:
                self.logger.warning(message)

        #################################
        # Discover Mezzanines
        #################################
        # Auto-discover mezzanines and add them to the hardware map.
        if self.hwm and not no_mezz:
            self.logger.info(f'{self!r}: Discovering Mezzanines...')
            # make sure we see the previous prints right away so we have a better feeling of what is happening
            self.print_flush()
            await asyncio.gather(*[ib.discover_mezzanines_async() for ib in self.hwm])
        for ib in self.hwm:
            mezz_info = ','.join(f'{i}:{m.iceboard}' for i, m in ib.mezzanine.items())
            self.logger.debug(f"Mezzanines after discovery {ib}, {mezz_info}")

        def get_mezz_name(ib, mezz_number):
            m = ib.mezzanine.get(mezz_number, None)
            # return '%s_SN%s' % (m.__ipmi_part_number__, m.serial) if m else '-'
            return 'SN%s' % (m.serial) if m else '-'

        ################################
        # Update generic Motherboard objects with firmware-specific ones
        ################################
        # For now, we assume that all the boards boards with two MGADC08
        # boards are running the chFPGA firmware variant.
        self.logger.debug(f'Before reassignment, HWM={list(self.hwm)}')
        for ic in Crate.get_all_instances():
            self.logger.debug(f'{ic}(serial={ic.serial}, crate_number={ic.crate_number}')
        # for i, ib in enumerate(list(self.hwm)):  # use list() so we can modify self.ib in the loop.
        #     # print('Board %r has mezzanines %s. Is instance of chFPGA_controller: %s'
        #     #       % (ib, ','.join('%i:%s' % (k, m.part_number)
        #                for k,m in ib.mezzanine.items()), isinstance(ib, chFPGA_controller)))
        #     if all(m.part_number == FMCMezzanine_MGADC08.part_number for m in ib.mezzanine.values()
        #            if m) and not isinstance(ib, chFPGA_controller):
        #         new_ib = ib.update_instance(new_class=chFPGA_controller)
        #         self.logger.debug(f'Replaced {ib!r} with {new_ib!r}')
        #         self.logger.debug(f'Mezzanines after {new_ib}, {new_ib.mezzanine}')

        #################################
        # Check the hardware map for generic objects
        #################################
        # print(f'Icecrates are: {any(c.part_number for c in Crate.get_all_instances())}')

        # Check if there are any crate without part number. We use a list
        # comprehension in case any has been overriden with np.any in a pylab
        # session, which does not work with generators.
        if any([not c.part_number for c in Crate.get_all_instances()]):
            raise RuntimeError('There are generic motherboards left in the hardware map')
        # Same for Motherboards
        if any([not i.part_number for i in Motherboard.get_all_instances()]):
            raise RuntimeError('There are generic crates left in the hardware map')

        #################################
        # Create self.ib and self.ic
        #################################

        # Query the hardware map for all iceboards and icecrates
        # use outerjoin in case there is no crate
        self.ib = sorted(self.hwm, key=lambda i: (i.crate.crate_number or 0 if i.crate else 0, i.slot or 0))
        self.ic = sorted({i.crate for i in self.hwm if i.crate and i.crate.part_number},
                         key=lambda c: c.crate_number or 0)

        # Courtesy warning
        if not self.ic:
            self.logger.debug(f'{self!r}: There are no crates or backplanes in the hardware map')

        #################################
        # Print the Motherboard table
        #################################

        self.logger.debug(f'New HWM={self.hwm}')
        for ib in self.hwm:
            crate_info = (f"{ib.crate}(serial={ib.crate.serial}, "
                          f"crate_number={ib.crate.crate_number})"
                          if ib.crate else None)
            self.logger.debug(f"{ib}, crate={crate_info}")

        n_mezz = max(ib.NUMBER_OF_FMC_SLOTS for ib in self.ib)
        if n_mezz:
            self.print_iceboard_table(
                func = lambda ib: '\n'.join(get_mezz_name(ib, mezz_number+1)  for ib in self.ib for mezz_number in range(n_mezz) if mezz_number < ib.NUMBER_OF_FMC_SLOTS),
                row_labels= '\n'.join(f'Mezz{mezz_number}' for mezz_number in range(n_mezz)),
                add_serial=True)
        else:
            self.print_iceboard_table(add_serial=True)

        self.print_flush()

        # Store as Ccoll collections to allow easy parallel operations
        self.ib = Ccoll(self.ib)
        self.ic = Ccoll(self.ic)

        # We have our final hardware map. Update the `repr` caches
        for ib in self.ib:
            ib.set_cache()

        self.print_flush()

        self.logger.info(f'{self!r}: Done creating {self!r}')

        # Determine if we program and initialize the FPGA based on the mode and the overrides
        # specified by `prog` , `init` and `open`.

        # translate the `open` parameter into `init`
        if open is not None and init is None:
            if open == 0:
                init = 1  # program FPGA only
            elif open == 1:
                init = 3 # full initialization

        if mode:
            # automatically fully initializes the FPGA unless we
            # explicitely specify the init (or open) parameter
            if init is None:
                init = 3  # full initialization unless specified explicitely

            # automatically program the FPGA if `force` mode (prog=2) unless we
            # explicitely specify the prog parameter
            if prog is None:
                prog = 2  # force FPGA programming
        else:
            # No firmware operational mode specified. Do not program or initialize.
            init = 0

        if self.ib:
            if init >= 1:
                ####################################
                # Program the FPGAs and open communication link to firmware
                ####################################
                self.logger.info(f'{self!r}: Configuring FPGAs and connecting to them...')
                await asyncio.gather(*[
                    self.prog_and_open_fpga(
                        ib=ib,
                        prog=prog,
                        init=init,
                        mode=mode,
                        bitfile=bitfile,
                        udp_retries=udp_retries,
                        fpga_ip_addr_fn=fpga_ip_addr_fn,
                        interface_ip_addr=if_ip,
                        **ib.extra_args)
                    for ib in self.ib])



            if init >= 3: # initialize the FPGA subsystems
                ################################################
                # Initialize the FPGA firmware
                ################################################
                self.logger.info(f'{self!r}: Initializing FPGA firmware')
                await asyncio.gather(*[
                    ib.init_fpga_async(
                        init=open,
                        adc_mode=adc_mode,
                        adc_bandwidth=adc_bandwidth,
                        sampling_frequency=sampling_frequency,
                        # group_frames = frames_per_packet,  # Removed because not needed by CRS. is also passed to set_operational_mode
                        **kwargs
                    ) for ib in self.ib])


            if init >= 3: # initialize the backplane
                ########################
                # Initializing backplane hardware communication firmware
                ########################
                # The FPGA starts genereting significant heat now that is is initialized.
                # Let's give the backplane the opportunoty to update the fan speed.
                self.logger.info(f'{self!r}: Initializing Backplane')
                if self.ic:
                    self.ic.init()

            if init >= 3: # configure and initialize the selected operational mode

                if not mode:
                    raise RuntimeError(f'An operational mode must be specified for init={init}')

                ########################
                # Initializing SYNC method
                ########################
                # As a convenience, unless `sync_method` is explicitely specified,
                # set default sync method to `irig-b` if *all* boards
                # are in a crate or to 'local' if we *only* have standalone boards.
                if sync_method is None:
                    if all(self.ib.crate):
                        sync_method = 'irig-b'
                        self.logger.info(f'{self!r}: sync_method is not specified but was automatically set to "irig-b" since all boards are in crates')
                    elif not any(self.ib.crate):
                        sync_method = 'local'
                        self.logger.info(f'{self!r}: sync_method is not specified but was automatically set to "local" since no board is in a crate')
                    else:
                        self.logger.warning(f'{self!r}: Some motherboards are in crates and some are not. `sync_method` is not set.')


                if sync_method or sync_source:
                    self.logger.info(f'{self!r}: Setting SYNC method to {sync_method}')
                    self.set_sync_method(
                        method=sync_method,
                        source=sync_source,
                        master=sync_master,
                        master_source=sync_master_source,
                        master_output=sync_master_output)

                ########################
                # Initializing operational mode
                ########################
                self.set_operational_mode(
                    mode=mode,
                    frames_per_packet=frames_per_packet,
                    chan8_channel_map=chan8_channel_map,
                    tx_power=tx_power,
                    integration_period=integration_period,
                    autocorr_only=autocorr_only,
                    corner_turn_bad_links=corner_turn_bad_links,
                    corner_turn_bin_priority=corner_turn_bin_priority,
                    corner_turn_remap_level=corner_turn_remap_level)

        self.print_flush()

        #################################
        # Completed array creation and initialization
        #################################

    async def prog_and_open_fpga(self, ib, prog, init, mode, bitfile=None, max_trials=3, **kwargs):
        """ Try to open communication with the FPGA and reprogram the FPGA and retry a number of
        times if this fails.

        Parameters:

            ib: Motherboard object to program and open


            init (int): Initialization level

                - 1: program FPGA only (calls set_fpga_bitstream() and creates fpga object)
                - 2: like init=1, plus connects to FPGA (call open_fpga_async(...))

            prog (int): FPGA bitstream configuration level

                0: do not program the FPGA
                1: program FPGA only if needed
                2: always program the FPGA

            mode (str): operational mode. Is needed to find the proper bitstream for the platform

            bitfile (str): If `bitfile` is a path pointing to a directory, override the bitstream
                 search path (but still uses the default bitstream filename). If `bitfile`  is a
                 path to a file, use this specific bitstream (mode still has to be specified to
                 properly initialize the python objects and firmware)

            max_trials (int): Maximun allowed number of reprogramming and retrials before raising an error.

            kwargs: Any extra argument is passed to open_fpga_async(...)

        Exceptions:

            IOError: Raised if the motherboard's open() method still raises
                an IOError after the maximum number of trials.

        """
        for trial in range(1, max_trials + 1):
            if init >= 1 and prog:
                await ib.set_fpga_bitstream_async(mode, force=(prog > 1) or (trial > 1), bitfile_override=bitfile)
                self.logger.debug(f'{self!r}: Done configuring FPGAs on {ib!r}')


            if init >= 2:
                try:
                    self.logger.info(
                        f'{self!r}: Initializing core FPGA firmware for {ib!r}: '
                        f'Trial {trial}/{max_trials}.')

                    # Initialize FPGA UDP communications. Overrides
                    # default parameters that were temporarily set when
                    # the motherboard handler object was created.
                    await ib.open_fpga_async(**kwargs)
                    break
                except IOError as e:
                    raise
                    self.logger.warning(f'{self!r}: Error while initializing core firmware on trial {trial}/{max_trials}. '
                                        f'Error is: \n{e!r}')
                    if trial == max_trials:
                        raise IOError(f'{self!r}: Unable to initializing FPGA core firmware after {trial} trials. Giving up.')
                    else:
                        self.logger.warning(f'{self!r}: Reprogramming FPGA and trying again.')
            else:
                break

    @staticmethod
    def _to_integer(x):
        """ If the specified argument has an integer representation then
        return that integer otherwise return the original argument.
        """
        try:
            return int(x)
        except (ValueError, TypeError):
            return x

    @staticmethod
    def _parse_crate_id(string):
        """ Splits a string describing a crate into a (model, serial, crate_number) tuple.

        The serial is converted to an integer if possible; otherwise, it is a
        string. The crate number must be an integer. Missing parameters are
        returned as None. Every field is converted to uppercase.

        Examples:
            'MGK7BP16_SN018:3' => ('MGK7BP16', 18, 3)
            'MGK7BP16_018:3' => ('MGK7BP16', 18, 3)
            '18:3' => (None, 18, 3)
            '18' => (None, 18, None)
        """
        # Extract the crate number
        s = str(string).upper().split(':')
        if len(s) == 1:
            sn = s[0]
            cn = None
        elif len(s) == 2:
            try:
                sn = s[0]
                cn = int(s[1])
            except ValueError:
                raise ValueError('crate number is not an integer in entry %s' % string)
        else:
            raise RuntimeError('Multiple crate numbers were specified in entry:' % string)
        # Check if a model number is specified
        s = sn.split('_')
        if len(s) == 1:
            model = None
            sn = s[0]
        elif len(s) == 2:
            model = s[0]
            sn = s[1]
            if sn.startswith('SN'):
                sn = sn[2:]
        else:
            raise RuntimeError("Crates model and serial number must be separated by one (and only one) "
                               "underscore (e.g. MGK7BP16_023). Got '%s'" % sn)

        try:
            sn = int(sn)
        except (ValueError, TypeError):
            pass

        return (model, sn, cn)

    @staticmethod
    def _build_crate_id(model, serial):
        if isinstance(serial, int):
            serial = '%03i' % serial
        return '%s_SN%s' % (model, serial)

    def print_flush(self):
        """ Make sure that the test sent previously to stdout shows immediately on the console.
        """
        sys.stdout.flush()

    def __repr__(self):
        """ Short string representing this object and suitable to use as a tag in a syslog entry"""
        ib = self.ib or self.hwm or []
        ic = self.ic or {b.crate for b in self.hwm if b.crate} or []
        return '%s(%i_boards,%i_crates)' % (self.__class__.__name__, len(ib), len(ic))

    def get_hwm_info(self):
        string = '%s object with the following hardware map:\n' % self.__class__.__name__
        for i in self.ib:
            mezz = ['%s SN%s' % (m.part_number, m.serial) if m else 'None'
                    for m in [i.mezzanine.get(1, None), i.mezzanine.get(2, None)]]
            string += '   Crate SN%s, slot %2i: Motherboard SN%s at %s (ping =%s), Mezz1=%s, Mezz2=%s\n' % (
                i.crate.serial if i.crate else None,
                i.slot, i.serial, i.hostname,
                i.ping(), mezz[0], mezz[1])
        return string

    def get_hwm(self):
        """ Returns the current hardware map as a dict.

        Returns:

            hardware map dict in the format::

                'crate': {'number':n, 'model':m, 'serial':s,
                    'iceboards': [
                    'slot':s, 'model':m, 'serial':s, 'mezzanines':[
                        {'slot':s, 'model':m, 'serial':s}, ...]}, ...]
        """
        hwm = []
        for ic in self.ic:
            hwm.append(dict(
                type='crate',
                model=ic.part_number,
                serial=str(ic.serial),
                crate_number=ic.crate_number,
                iceboards=[
                    dict(
                        slot=ib.slot,
                        model=ib.part_number,
                        serial=str(ib.serial)) for ib in ic.slot.values()
                ]
            ))
        for ib in self.ib:
            hwm.append(dict(
                type='iceboard',
                model=ib.part_number,
                serial=str(ib.serial),
                mezzanines=[
                    dict(
                        slot=i,
                        model=m.part_number if m else None,
                        serial=str(m.serial) if m else None)
                    for i, m in enumerate((ib.mezzanine.get(1, None), ib.mezzanine.get(2, None)))
                ]
            ))
        return hwm

    def process_list_hwm(self, hwm):
        """
        Create the hardware map objects that are specified in a list-based hardware map `hwm`


        Parameters:

            hwm (list): list containing hardware items, each of which is in the form

                ::

                   {"class": class_name, "arg1":arg1, ...}

                ``class_name`` is a string describing the name of a Motherboard or Crate subclass (e..g IceBoard, ZCU111, IceCrate etc.).

                ``"arg1":arg1`` are the arguments passed to the class
                constructor. Arguments that are not needed directly by the
                class constructor are stored by that constructor to be used
                later as arguments for further initializing the platform or
                the FPGA.

        Returns:
            Nothing. The new objects are added in the instance registry of each hardware base class.

        Notes:

            Hardware map objects can be generic base objects ('Motherboard',
            'Crate' etc.).  In this case, their class will be updated based on
            self-discovery.

            'IceBoard'-type of objects can take the additional ``crate_number``
            object, which will link to an existing crate object with the same
            number, or will create a generic one that will be resolved in the
            self-discovery process.
        """
        self.logger.debug(f'{self!r}: Creating Hardware Map from list {hwm}')
        # self.hwm = []  # Create empty hardware map
        crate_classes = {c.__name__: c for c in Crate.get_all_classes()}
        mb_classes = {c.__name__: c for c in Motherboard.get_all_classes()}

        # First pass: check 1) if the class names are valid and 2) create the crate instances
        for hwm_entry in hwm:
            params = dict(hwm_entry)  # make a copy, we'll modify it below
            class_name = params.pop('class') # exctract the class name entry

            # check if the class name is valid
            if class_name not in crate_classes and class_name not in mb_classes:
                valid_class_names = ','.join(list(crate_classes) + list(mb_classes))
                raise ValueError(f"Unknown class name '{class_name}' in a list-based hardware map. "
                                 f"Valid class names are: {valid_class_names}")
            # It it is a crate, create an instance
            if class_name in crate_classes:
                # Create the class: it will be registered in the class registry for future use
                # The crate can have a serial number, crate number, or both
                Crate.get_unique_instance(new_class=crate_classes[class_name], **params)

        # Second pass: Create the IceBoards, and link them to the crates
        for hwm_entry in hwm:
            self.logger.debug(f'Processing hwm entry {hwm_entry}')

            params = dict(hwm_entry)  # make a copy
            class_name = params.pop('class')
            if class_name in mb_classes:
                self.logger.debug(f"process_list_hwm: Calling get_unique_instance(new_class={class_name}, {params}")
                ib = Motherboard.get_unique_instance(new_class=mb_classes[class_name], **params)
                # self.logger.debug('%r: Crate %r is in %r' % (self, crate_number, params))

    def process_str_hwm(self, hwm, allow_generic_motherboard=False):
        """
        Create the hardware map objects that are specified in a string-based hardware map `hwm`

        The Motherboard and Crate objects created from the string description
        are added directly in the global hardware map.

        """
        # Build the hardware description string from various sources
        hw_string = hwm

        self.logger.debug(f"{self!r}: Processing the hardware description string is: '{hw_string}'")
        # Parse the hwm string into a hardware table. The hardware table is
        # not the hardware map, but represents the entries that we want to add
        # to the hardware map later.
        # remap_table = {
        #     'crates': {(crate_number,): ('icecrates', (model, serial, crate_number))
        #                for crate_number, (model, serial) in crate_map.items()}}

        logger = self.logger
        # If hw_string is a list of string, combine them in one single string
        if isinstance(hw_string, (list, tuple)):
            hw_string = ' '.join(str(s) for s in hw_string)

        # if the hw_string is just '*', we want to find all boards.
        # We therefore create a generic Motherboard with serial '*'.
        # This will be a signal that says we want to match any board.
        if hw_string == '*':
            Motherboard.get_unique_instance(serial='*')
            return
        # Split the string in ' '- or '_'-separated elements
        elements = str(hw_string).replace('_', ' ').strip().split(' ')

        # # Special case: if the hw_string is just '*', we register all units of every known model with
        # # '*' as the serial number (second) field
        # if len(elements) == 1 and elements[0] == '*':
        #     for p in dut_id_patterns:
        #         type_, entry = p['store_in'], p['entry']
        #         wild_entry = entry[0:1] + ('*',) + entry[2:]
        #         if wild_entry not in hw_table[type_]:
        #             hw_table[type_].append(wild_entry)
        #     return hw_table

        def to_int(s):
            """ Returns string s as an integer if it represents an integer that is not previxed with '0', otherwise returns the original string"""
            return (int(s) if isinstance(s, str) and s.isdigit() and not (len(s) > 1 and s.startswith('0')) else s)

        def split_fields(s, n):
            fields = el.split(':')
            numeric_fields = [to_int(fields[i]) if i < len(fields) else None for i in range(3)]
            return numeric_fields[:n]

        current_class = None
        for el in elements:
            if not el:
                continue
            elif '.' in el:  # if hostname (has a '.' somewhere)
                hostname, slot, crate_number = split_fields(el, 3)
                # print(f'Adding Motherboard {hostname}, {slot}, {crate_number}')
                if current_class:
                    ib = current_class.get_unique_instance(hostname=hostname, slot=slot, crate_number=crate_number)
                elif allow_generic_motherboard:
                    ib = Motherboard.get_unique_instance(hostname=hostname, slot=slot, crate_number=crate_number)
                else:
                    raise RuntimeError('Must specify model number before an IP address')
                # current_class = None
            elif el[0].isdigit():  # if a serial (is only digits)
                if not current_class:
                    raise RuntimeError('A part number must be specified before a target serial number')
                if issubclass(current_class, Motherboard):
                    serial, slot, crate_number = split_fields(el, 3)
                    if slot is not None and slot < 1:
                        raise ValueError('Slot number cannot be zero')  # because zero means 'no slot'
                    logger.debug(f'Adding Motherboard {current_class.part_number} {serial!r}, slot={slot!r}, crate={crate_number!r}')
                    ib = Motherboard.get_unique_instance(
                        new_class=current_class,
                        serial=serial,
                        slot=slot,
                        crate_number=crate_number)
                elif issubclass(current_class, Crate):
                    serial, crate_number = split_fields(el, 2)
                    logger.debug(f'Adding Crate {serial}, crate_number={crate_number}')
                    Crate.get_unique_instance(new_class=current_class, serial=serial, crate_number=crate_number)
                else:
                    raise TypeError(f'Trying to create object {current_class} that '
                                    f'is other than Motherboard or Crate')
            else:  # otherwise, assume it is a part number
                matching_classes = [c
                                    for c in Motherboard.get_all_classes() + Crate.get_all_classes()
                                    if c.part_number and c.part_number.upper().endswith(el.upper())]
                if not matching_classes:
                    raise RuntimeError(f'Cannot find a part number that ends in {el}')
                elif len(matching_classes) == 1:
                    current_class = matching_classes[0]
                else:
                    raise RuntimeError(f'Found multiple part numbers that end in {el}')

        # breakpoint()
        # return hw_table

        # hw_table = parse_hw_string(hw_string)

        # self.logger.debug('%r: The hardware description table obtained from hardware description string is:' % self)
        # for key, value in hw_table.items():
        #     self.logger.debug('%r:     %s:%r' % (self, key, value))

        # # Check if there were 'crate' entries that were not remapped to icececrate entries
        # if hw_table.crates:
        #     self.logger.warning('The following hardware map crate entries could not be resolved '
        #                         ' into backplane model/serial: %s' % hw_table.crates)

        # # Create a *reverse* crate map ({(model,serial):number} instead of {number:(model,serial)})
        # # that will be used later with set_crate_numbers() to assign crate numbers to crates if
        # # those were not already expressed explicitely in the hw_string (e.g. if the crate is
        # # specified by serial number or was auto-detected from a specified iceboard)
        # if crate_map:  # If we already explicitely provided a crate map, use it.
        #     crate_number_map = {(model, serial): crate_number for crate_number, (model, serial) in crate_map.items()}
        # else:  # if not, try to build one from the info that was provided in the hw description string
        #     # If some crate numbers are specified or if there are crate wildcards, just use the
        #     # crate numbers that user specified
        #     if any(crate_number is not None or serial is '*' for (model, serial, crate_number) in hw_table.icecrates):
        #         crate_number_map = {
        #             (model, serial): crate_number
        #             for (model, serial, crate_number) in hw_table.icecrates
        #             if crate_number is not None and serial != '*'}
        #     # If no crate number was specified at all and there are no wildcards, assign crate
        #     # numbers in the order they were specified in the hw description string (first crate =
        #     # crate 0, second crate is crate 1 etc)
        #     else:
        #         crate_number_map = {
        #             (model, serial): i
        #             for i, (model, serial, crate_number) in enumerate(hw_table.icecrates)}
        # self.logger.debug('%r: The Icecrate map is %s' % (self, crate_number_map))

        # # We've got our crate numbers. Remove them from the icecrate list so we pass only the (model,
        # # serial) to the mdns discovery function.
        # hw_table.icecrates = [(model, serial) for (model, serial, crate_number) in hw_table.icecrates]

    # def set_crate_numbers(self, crate_number_map, strict=True):
    #     """ Set the crate number of each crate based on the provided crate
    #     number map.

    #     Silently overrides any existing crate numbers.

    #     Parameters:

    #         crate_number_map (dict): A { (model, serial): number} dict that
    #             maps a crate ID tuple to a crate number.

    #         strict (bool): if True, will raise an exception if not all crates
    #             can be assigned a crate number

    #     Notes:

    #         Crate numbers are needed to identify hardware element by simple
    #         tuples (e.g. (crate_number, slot_numer, lane_number)) and are also
    #         used to infer which is a master and slave crate when crates are
    #         interconnected in pairs.
    #     """

    #     self.logger.info('%r: Setting crate numbers for the following crates %s'
    #                      % (self, ', '.join(repr(ic) for ic in self.ic)))

    #     for ic in IceCrate.get_all_instances():
    #         model = ic.part_number
    #         sn = ic.serial
    #         try:
    #             int_sn = int(ic.serial)
    #         except (ValueError, TypeError):
    #             int_sn = None

    #         new_crate_number = (crate_number_map.get((model, sn), None) or
    #                            crate_number_map.get((model, int_sn), None))

    #         #if not isinstance(new_crate_number, int):
    #         #    raise ValueError('%r: crate number %r is not an interger' % (self, new_crate_number))

    #         if new_crate_number is not None:
    #             ic.update_instance(crate_number = int(new_crate_number))
    #             self.logger.info('%r: Assigning crate number %r to crate %s'
    #                 % (self, new_crate_number, ic.get_string_id()))
    #         elif strict:
    #             raise RuntimeError('Cannot find a crate number for crate %s' % ic.get_string_id())
    #         else:
    #             self.logger.warning('%r: set_crate_number: Cannot find a crate number for crate %s'
    #                 % (self, ic.get_string_id()))

    def set_operational_mode(self,
                             mode,
                             frames_per_packet=1,
                             send_flags=True,
                             chan8_channel_map=list(range(8)),
                             tx_power=None,
                             integration_period=16384,
                             autocorr_only=False,
                             corner_turn_bad_links=None,
                             corner_turn_bin_priority=None,
                             corner_turn_remap_level=0,
                             ):
        """ Set the operational mode of the array.


        Parameters:

            mode (str): operational mode string.

                - 'chan8': Corner-turn engine is bypassed and raw 8-bit data from 8
                  channelizers is sent directly to the 8 10G Ethernet links.

                - 'shuffle16': Acquire, channelize and shuffle data within
                  each Iceboard individually and send the data through the
                  IceBoard QSFP+ ports. There is no data shuffling between
                  boards. This is good for single board operation (or an array
                  of boards operating independently)

                - 'shuffle128': Acquire, channelize and shuffle data within a
                  crate to create a 8-board (128-channel) correlator. The
                  shuffled data is sent through the IceBoard QSFP+ ports.
                  There is no shuffling between crates.

                - 'shuffle256': Acquire, channelize and shuffle data within a
                  crate to create a 16-board (256-channel) correlator. The
                  shuffled data is sent through the IceBoard QSFP+ ports.
                  There is no shuffling between crates.

                - 'shuffle512': Acquire, channelize and shuffle data between pair of
                  crates to create a 32-board (512-channel) correlator. The shuffled
                  data is sent through the IceBoard QSFP+ ports. The pairing of crates
                  is based on the crate number: Crate N and N+1 form a pair, whereas N
                  is a even number.

                - 'corr4', 'corr8', 'corr16', 'corr32': The corner-turn engine is configured to feed the
                  internal firmware correlator (only if the firmware was compiled with it).

            frames_per_packet (int): Number of frames to combine in a single
                packet. Is limited by the amount of buffering space inside the
                FPGA.

            send_flags (bool): If True, the Scaler and Frame flags will be
                sent after the data block.

            tx_power (dict): Power levels to be set on the GTXes.

            integration_period (int): (for ``corr`` modes only):
                Sets the integration period (in frames) of the firmware
                correlator.

            autocorr_only (bool): (for ``corr`` mored only. If True, only the
                autocorrelation products are sent.

            corner_turn_bad_links (list of tuples): List of corner turn engine
                outputs links [(crate, slot, links), ...] that should
                preferably NOT be assigned any frequency bins if possible.

            corner_turn_bin_priority (list of int): List of frequency bins
                that are to be assigned to the corner turn output, in order of
                priority (most desirable frequency first). The lowest priority
                bins will be assigned to the bad links as much as possible,
                depending on the constraints of the corner turn engine
                flexibility.

            corner_turn_remap_level (int): Sets the aggressivness of the remapping by
                selecting how many crossbar levels are involved. 0:no
                remapping, 1: 3rd crossbar only, 2: crossbars 2 and 3, 3: all
                crossbars.



        Notes:

            Having called get_ber() before initializing the shuffle will lead to errors!

        # data_width : Data width of each Re and Im component of the channelizer output
        # enable_gpu_link : Enables the GPU link transmission

        """
        # use defaults that were set during initialization unless overriden
        mode = mode or self.mode
        tx_power = tx_power or self.tx_power
        self.logger.info(f'{self!r}: Setting operational mode to {mode}')

        # To make sure that the data acquisition and transmission will be done
        # at the same rate, refuse to operate if
        #   - an Iceboard that is in a crate is not using the backplane clock
        #   - an iceboard that is not in a crate is using the backplane clock
        clock_sources = self.ib.get_iceboard_clock_source_async()
        bad_clock_source_without_crate = [i for i, source in enumerate(clock_sources) if not self.ib[i].crate and source == 'CLOCK_SOURCE_BP']
        bad_clock_source_with_crate = [i for i, source in enumerate(clock_sources) if self.ib[i].crate and source != 'CLOCK_SOURCE_BP']

        # We do not raise an exception if we are using the backplane clock without crate in case the crate information is not discovered
        if bad_clock_source_without_crate:
            msg = ', '.join(f'{self.ib[i]!r}=>{clock_sources[i]}' for i in bad_clock_source_without_crate)
            self.logger.warning(f'The following IceBoards are in not in crates but are configured to use the backplane clock: {msg}')

        if bad_clock_source_with_crate:
            msg = ', '.join(f'{self.ib[i]!r}=>{clock_sources[i]}' for i in bad_clock_source_with_crate)
            raise RuntimeError(f'The following IceBoards are in crates but are not configured to use the backplane clock: {msg}')

        if mode == 'chan8':
            self.ib.set_fft_bypass(True)
            self.ib.set_scaler_bypass(True)
            # self.corner_turn_stream_ids = self.init_corner_turn(
            #     mode='chan8',
            #     frames_per_packet=frames_per_packet,
            #     send_flags=send_flags,
            #     chan8_channel_map=np.hstack((chan8_channel_map, chan8_channel_map)),
            #     tx_power=tx_power)
            self.corner_turn_frequency_bins = {}
            self.corner_turn_stream_ids = {}
            for ib in self.ib:
                stream_ids = ib.init_crossbars(
                    mode='chan8',
                    frames_per_packet=frames_per_packet,
                    chan8_channel_map=chan8_channel_map)
                for lane, stream_id in enumerate(stream_ids):
                    self.corner_turn_stream_ids[ib.get_id(lane)] = stream_id
            # Sync board(s)
            self.sync()

        elif mode in ('shuffle8',):
            # For use with the CRS
            for mb in self.ib:
                mb.set_corr_reset(0)

        elif mode in ('shuffle256', 'shuffle512', 'shuffle16', 'shuffle128', 'chord16'):
            if not all(self.ib.CROSSBAR2) or not all(self.ib.CROSSBAR3):
                raise RuntimeError(f' Mode {mode} requires all boards to have their CROSSBAR2 and CROSSBAR 3 implemented')
            self.ib.BP_SHUFFLE.set_tx_power(13)
            self.ib.CROSSBAR3.SOF_WINDOW_STOP = 110
            self.ib.CROSSBAR3.TIMEOUT_PERIOD = 0
            self.ib.BP_SHUFFLE.reset_rx_equalizers()

            self.init_corner_turn(
                mode=mode,
                frames_per_packet=frames_per_packet,
                send_flags=send_flags,
                tx_power=tx_power,
                bad_links=corner_turn_bad_links,
                bin_priority=corner_turn_bin_priority,
                remap_level=corner_turn_remap_level)
            self.ib.BP_SHUFFLE.reset_stats()
            self.ib.CROSSBAR2.reset_stats()
            self.ib.CROSSBAR3.reset_stats()
            # Sync was performed by init_corner_turn()

        elif mode in ('corr4', 'corr8', 'corr16', 'corr32', 'corr64'):
            #if not all(self.ib.CORR):
            #    raise RuntimeError(f'Mode {mode} requires all boards to have a firmware correlator engine')
            bin_map = self.get_corner_turn_bin_map(
                mode=mode,
                bad_links=corner_turn_bad_links,
                bin_priority=corner_turn_bin_priority,
                remap_level=corner_turn_remap_level)
            self.corner_turn_stream_ids = None
            self.corner_turn_frequency_bins = None
            for ib in self.ib:
                ib.set_corr_reset(0)
                ib.init_crossbars(mode=mode, frames_per_packet=1, bin_map=bin_map[ib.get_id()])
            # self.ib.set_offset_binary_encoding()  # The firmware correlator engine expects offset encoding
            if integration_period:
                self.ib.start_correlator(integration_period=integration_period, autocorr_only=autocorr_only)
            # Sync board(s)
            self.sync()
        else:
            raise ValueError(f"Unknown operational mode '{mode}'")

    def get_corner_turn_bin_map(self, mode, bad_links=None, bin_priority=None, remap_level=0, verbose=0):
        """
        Get the bin selection map for every board of the array in order to
        route the bins toward the  desired output links

        Parameters:

            mode (str): opertional mode. 'shuffle256' , 'shuffle512', 'shuffle16' and 'chord16'
                implement frequency-remaping. Other modes return de default
                map.

            bad_links: List of corner_turn outputs [(crate, slot, link) , ...]
                that can't process data. low-priotity frequencies will be
                assigned to those bins.

            bin_priority: list of all frequency bins (from 0 to 1023) in order
                of priority, with the useful bins at the beginning of the
                list.

            remap_level (int): Sets the aggressivness of the remapping by
                selecting how many crossbar levels are involved. 0:no
                remapping, 1: 3rd crossbar only, 2: crossbars 2 and 3, 3: all
                crossbars.

        Returns:

            A dictionary that describes the corner-turn bin selection map for
            every board, in the format

                { (crate, slot):{'cb1':cb1_bins, 'cb2':cb2_bins, 'cb3':cb3_bins}, ... }

            where

                ``crate``: crate id as returned by ``ib.get_id()``. It is
                typically the crate number, but could be a string with the
                crate model/serial.

                ``slot``: slot id as returned by ``ib.get_id()``.

                ``cb1_bins``: list of 16 lists describing the bins indices
                    that are selected by each bin selector of the 1st crossbar

                ``cb2_bins``: list of 2 lists describing the bins indices that
                    are selected by each bin selector of the 2nd crossbar

                ``cb3_bins``: list of 8 lists describing the bins indices that
                    are selected by each bin selector of the 3rd crossbar
        """

        # Process bad link
        bad_links = bad_links or []
        bad_links = [tuple(id) for id in bad_links]
        if len(set(bad_links)) != len(bad_links):
            raise RuntimeError('bad links tuples are not unique!')
        bad_bad_links = [(crate, slot, lane) for (crate, slot, lane) in bad_links
                         if (not (0 <= crate <= 1)
                             or not (0 <= slot <= 15)
                             or not (0 <= lane <= 7))]
        if bad_bad_links:
            raise RuntimeError('The following bad links tuples are invalid: %s' % bad_bad_links)

        # Process bin priority
        bin_priority = bin_priority or list(range(1024))
        if set(bin_priority) != set(range(1024)):
            raise RuntimeError('Bin priority must contain every bin from 0 to 1023 exactly once')
        bin_priority = np.argsort(bin_priority)  # priority for each bin from 0 to 1023

        if mode == 'shuffle512':
            # start with the default bin map
            bin_map = {(crate, slot): dict(
                cb1=np.arange(1024).reshape((16, 64), order='F'),
                cb2=np.arange(64).reshape((2, 32), order='F'),
                cb3=np.arange(32).reshape((8, 4), order='F'))
                for crate in range(2) for slot in range(16)}
            if remap_level >= 3:
                self.compute_cb1_bin_map(bin_map, bad_links, bin_priority, verbose=verbose)
            if remap_level >= 2:
                self.compute_cb2_bin_map(bin_map, bad_links, bin_priority, verbose=verbose)
            if remap_level >= 1:
                self.compute_cb3_bin_map(bin_map, bad_links, bin_priority, verbose=verbose)
                # self.shuffle512_cb3_freq_remap(bin_map, bad_links, bin_priority)
            # Apply crates 0 & 1 map to all pair of crates
            bin_map = {(crate, slot): bin_map[((crate or 0) & 1, slot)]
                       for (crate, slot) in self.ib.get_id()}

            # cb3_map = self.shuffle512_cb3_remap(mode=mode,
            #                                     bad_links=bad_links,
            #                                     freq_bins=bin_priority,
            #                                     output_cb3_bins=True)
            # # cb3_map describes only odd and even crate numbers. We expand the list to cover all crates explicitely.
            # for ib in self.ib:
            #     cb3_bins = [cb3_map.get((crate & 1, slot, lane), None)
            #         for crate, slot, lane in ib.GPU.get_lane_ids()]
            #     bin_map[ib.get_id()] = {
            #         'cb1':None,
            #         'cb2':None,
            #         'cb3':cb3_bins}
        elif mode == 'shuffle256':
            # start with the default bin map for a single crate
            bin_map = {(crate, slot): dict(
                cb1=np.arange(1024).reshape((16, 64), order='F'),
                cb2=np.arange(64).repeat(2).reshape(2, 64, order='F'),
                cb3=np.arange(64).reshape((8, 8), order='F'))
                for crate in range(1) for slot in range(16)}
            if remap_level >= 3:
                self.compute_cb1_bin_map(bin_map, bad_links, bin_priority, verbose=verbose)
            if remap_level >= 1:
                self.compute_cb3_bin_map(bin_map, bad_links, bin_priority, verbose=verbose)
            # Apply crates 0 map to all other crates
            bin_map = {(crate, slot): bin_map[(0, slot)]
                       for (crate, slot) in self.ib.get_id()}
        # elif mode == 'shuffle16':
        #     # use only the default bin map: we have no flexibility. All we can do is to remap the outputs.
        #     bin_map = dict(
        #             cb1=np.arange(1024).reshape((16, 64), order='F'),
        #             cb2=np.arange(64).repeat(2).reshape(2, 64, order='F'), # bypassed
        #             cb3=np.arange(64).reshape((8, 8), order='F')) # bypassed
        #     # Assign the same bin map to all boards in the array
        #     bin_map = {(crate, slot):bin_map
        #                 for (crate, slot) in self.ib.get_id()}
        else:  # other modes. Use defaults.
            bin_map = {ib.get_id(): {'cb1': None, 'cb2': None, 'cb3': None}
                       for ib in self.ib}
        return bin_map

    @staticmethod
    def compute_cb1_bin_map(bin_map, bad_links, bin_priority, verbose=1):
        """ Compute a first crossbar bin map that assings bins to
        each slot in order to maximize the allocation of low-priority (rfi) bins to bad gpu links.

        Parameters:

          bin_map (dict): Dict that contains the bin maps for all crossbars. In the format

            {(crate, slot):'cb1':cb1_map, 'cb2':cb2_map, 'cb3':cb3_map},...}

            `bin_map` is modified in place with the new optimized map.

          bad_links (list of tuple): List of (crate,slot,lane) GPU links that are inoperative

          bin_priority (ndarray): 1024-element vector indicating the priority of each bin.
              Element 0 is the priority for bin 0. A lower value has a higher priority.

        Returns:

            Nothing. bin_map['cb1'] is modified in place with the new optimized map.

        Description:

        In the 'shuffle512' and 'shuffle256' mode of the corner-turn engine,
        each of the 16 bin selector (BIN_SEL) of the first crossbar (crossbar 1) selects
        64 bins. These bins are then sent to specific slots. This is the only
        crossbar that can assign bins to slots; other subsequent crossbars
        operate within the same slot.

        The crossbar 1 bin selector has a constraint: bins that are selected
        by a bin selector must be separated by at least 8 bins. This limits
        the bin assignment possibilities. We cannot simply assign the N worst
        bins to the M worst GPU links, so we have to find the best compromise
        in a deterministic way.

        The algorithms is as follows:

          - We create 8 patterns of 128 bins separated by 8 bins. A pattern
            will be used by exactly two slots (each slot using 64 bins)
          - We process each slot one by one, starting with the slots that has
            the worst number of unprocessable bins
          - For each slot, we find the pattern that offers the worst N bins,
            where N is the number of unprocessable bins for that slot
          - We select assign the worst N bins from that pattern to the slot
          - we select the remaining 64-N best bins from that pattern to the slot
          - The resulting 64 selected bins are masked so they cannot be used anymore in that pattern
          - We repeat for each slot

        The crossbar 1 bin map does not attempt to route bad bins to specific
        crates or to specific GPU links. It just assigns the optimal number of
        bad bins that matches the total number of bad GPUs on that slot. It is
        the job of the following crossbars to route the bad bins to the
        correct crate and GPU. The downstream crossbars can do this routing
        perfectly since there are no restrictions on the bin assignments (no
        minimum bin spacing).

        This algorithm is somewhat restrictive and forgoes some of the
        flexibility we have in selecting bins.  Indeed, the bins do not have
        to be on a grid of 8: they can have *any* spacing greater or equal to 8.
        But all the algorithms we tried that took advantage of this flexibility would
        generally make excellent bin choices for the first slots, and the
        bins that remained for the last couple of slots wound no longer meets
        the spacing requirements; the algorithm would then fail.

        The algorithm we present here is restricted to 8 fixed grids, but it
        is deterministic and cannot fail. If we have a bad combination of bad
        links and bid priority, it will only provide less optimal solutions.
        Empirical data shows it is more than enough to salvage otherwise lost
        bins if the number of bad links is reasonable.

        """
        # Compute the number of unprocessable bins for each slot (both crates combined)
        Nbad = np.zeros(16, dtype=int)
        for (crate, slot, lane) in bad_links:
            Nbad[slot] += 4

        # Create a 8x128 array of bin numbers that represents 8 patterns of 128 bins that are spaced
        # by 8 bins. All patterns are exclusive, i.e. each bin is represented only once in the whole array
        bins = np.arange(1024).reshape((8, 128), order='F')  # 8 crossbar-compatible bin pattern of 128 bins

        # Create an equivalent matrix, now listing the priority level of each of the bins in the pattern array.
        # This is a masked array so we can mark which bins have been used.
        pri = np.ma.array(bin_priority[bins], mask=bin_priority[bins] * 0)  # priority level of bins in the pattern

        # bin_map = np.empty((16,64), dtype=int) # final bin assignments
        s = set()
        # Process each slot, starting with the one that has the worst number of unprocessable bins
        for j, slot in enumerate(np.argsort(Nbad)[::-1]):
            # We start by assigning the worst bins. We'll fill up the best bins after.
            nbad = Nbad[slot]
            if verbose:
                print('**** Interation #%i, Slot %i (has %i unprocessable bins)' % (j, slot, nbad))

            # Compute the number of remaining bins for each pattern (count() ignores masked entries)
            # It should normally be 128, 64 or 0
            Nbins = pri.count(axis=-1)
            if verbose:
                print('Number of remaining bins=', Nbins)

            # Find the index of the available bins for each pattern, in order of bin priority.
            # ix[i] is the index of elements of pri[i], with the highest priority (lower value) appearing first.
            # Masked bins end up at the end of the sorted list, so
            # only the first Nbins[i] bins are valid for pattern i.
            ix = pri.argsort(axis=-1)

            # Find the  best priority level we we end up with if we have to
            # take the `nbad` worst bins for each pattern. That corresponds to
            # taking the `nbad`-th element before last. We will start eating
            # into good (or higher priority) bins if we are running out of bad
            # (lower priority) bins in that pattern. Since we are selecting
            # bins that well end up on bad GPUs, we want these `nbad` bins to
            # contain as little good bins as possible.
            worst_pri = [(pri[s, x[np.clip(Nbins[s] - nbad, a_min=0, a_max=Nbins[s] - 1)]] if Nbins[s] else 0)
                         for s, x in enumerate(ix)]
            if verbose:
                print('Worst bin priority for all patterns are', worst_pri)

            # Find the pattern number that contained the worst nbad bins
            wo = np.argmax(worst_pri)  # 2 lost bins in typical data
            # We tried alternate pattern selection methods below, not of which were as good:
            # wo = np.argmax(Nbins) # Select the pattern with the most remaining bins: 14 lost bins with typical data
            # wo = slot//2 # Select the pattern based on slot number: 28 lost bins with typical data

            if verbose:
                print('slot %i GPUs cannot process %i bins, using pattern #%i' % (slot, Nbad[slot], wo))

            # Algorithmic check: check that there are bins available in this pattern.
            # This should always be true with this deterministic algorithm.
            if not Nbins[wo]:
                print('***** There are not enough frequencies left in the selected offset')
                raise RuntimeError('***** There are no frequencies left in the selected pattern')

            # Create a list indices containing the worst nbad bins and best 64-nbad bins for the selected pattern
            # pri[wo, bix] is the priority level of the selected bins in the chosen pattern
            # bins[wo,bix] is the number of the selected bins in the chosen pattern
            bix = ix[wo][list(range(64 - nbad)) + list(range(Nbins[wo] - nbad, Nbins[wo]))]
            # get the corresponding bin numbers. Sort them.
            b = sorted(bins[wo, bix])

            # Another algorithmic check: should never happen with our algorithm
            if any(pri[wo, bix].mask):  # sanity check, cannot happen in theory
                raise RuntimeError('Assigned a bin that was already assigned in another slot!')

            # Mask used pattern entries so they won't be used in the next slot
            pri[wo, bix] = np.ma.masked

            # Store the map for each bin_selector. Here, we assume that  bin
            # selector 0 selects bins for slot 0 etc. The init_crossbar()
            # method will reorder those to take into account the backplane
            # connectivity. We store this map for board (0,0), but it will be
            # the same for all boards.
            bin_map[(0, 0)]['cb1'][slot] = b
            # print 'Assignling %i bins:' % len(b), b

        # Finished computing the bin map for one slot of one crate
        # Apply the bin selection to all slots of all crates in the array
        for bmap in bin_map.values():
            bmap['cb1'][:] = bin_map[(0, 0)]['cb1']

        # Check the integrity of the result. Should always be good.
        if set(bin_map[(0, 0)]['cb1'].flatten()) != set(range(1024)):
            raise RuntimeError('Invalid bin map')

    @staticmethod
    def compute_cb2_bin_map(bin_map, bad_links, bin_priority, verbose=1):
        """ Compute crossbar 2 frequency mapping that routes the optimal
        number of best and worst bins to crates knowing how many can be
        processed by the GPU nodes connected to that crate.

        Parameters:

          bin_map (dict): Dict that contains the bin maps for all crossbars. In the format

            {(crate, slot):'cb1':cb1_map, 'cb2':cb2_map, 'cb3':cb3_map},...}

            `bin_map` is modified in place with the new optimized map.

          bad_links (list of tuple): List of (crate,slot,lane) GPU links that are inoperative

          bin_priority (ndarray): 1024-element vector indicating the priority of each bin.
              Element 0 is the priority for bin 0. A lower value has a higher priority.

        Returns:
          None. `bin_map` is modified in-place.

        Algorithm:

        This mapper decides on which crate (0 or 1) will be routed the bins
        selected for the current slot by the previous crossbar. We have full
        flexibility here: any bin can go on any crate.

        For each slot, we look at how many unprocessable (`nbad`) bins there
        are in each crate. We assign `nbad` worst available bins to that
        crate, and the rest is filled with the best available bins. Used bins
        are removed from the available bins. We repeat for the other crate.


        """
        Nslots, _ = bin_map[(0, 0)]['cb1'].shape
        Ncrates, Nbins_out = bin_map[(0, 0)]['cb2'].shape
        # Compute number of unprocessable bins
        Nbad = np.zeros((Ncrates, Nslots), dtype=int)
        for (crate, slot, link) in bad_links:
            Nbad[crate, slot] += 4
        for slot in range(Nslots):
            # CB1 map is the for all slots in both crates.  # cb1 has absolute
            # bin numbers. Assumes that cb2 remap is such as cb1[0] goes to
            # slot 0, etc.
            bins = bin_map[(0, 0)]['cb1'][slot]
            bix = list(bin_priority[bins].argsort())  # bin index order by bin_priority
            for crate, bs_map in enumerate(bin_map[(0, slot)]['cb2']):
                nbad = Nbad[(crate, slot)]
                ngood = len(bs_map) - nbad
                bs_map[:ngood] = bix[:ngood]
                del bix[:ngood]
                # print bs_map
                # print 'good=', ngood, 'bad=', nbad
                if nbad:
                    bs_map[-nbad:] = bix[-nbad:]
                    del bix[-nbad:]
                bs_map[:] = sorted(bs_map)
            bin_map[(1, slot)]['cb2'][:] = bin_map[(0, slot)]['cb2']
            if set(bin_map[(0, slot)]['cb2'].flatten()) != set(range(Ncrates * Nbins_out)):
                print('Selected', bin_map[(0, slot)]['cb2'])
                raise RuntimeError('Invalid crossbar 2 bin selection')

    @staticmethod
    def compute_cb3_bin_map(bin_map, bad_links, bin_priority, verbose=1):
        """ Compute the Crossbar 3 frequency map that optimally routes the
        bins to each GPU link by sending the worst bins to the GPUs that are
        inoperative and the remaining bins to the others.


        Parameters:

          bin_map (dict): Dict that contains the bin maps for all crossbars. In the format

            {(crate, slot):'cb1':cb1_map, 'cb2':cb2_map, 'cb3':cb3_map},...}

            `bin_map` is modified in place with the new optimized map.

          bad_links (list of tuple): List of (crate,slot,lane) GPU links that are inoperative

          bin_priority (ndarray): 1024-element vector indicating the priority of each bin.
              Element 0 is the priority for bin 0. A lower value has a higher priority.

        Returns:
          None. `bin_map` is modified in-place.

        The algorithm is identical to the crossbar2 bin mapper.

        """
        # Ncrates, _ = bin_map['cb2'].shape # (8,4) or (8,8)
        Nlanes, Nbins = bin_map[(0, 0)]['cb3'].shape  # (8,4) or (8,8)

        # Distribute bins in each CB3 output lanes
        for (crate, slot), bmap in bin_map.items():
            cb2_bix = bmap['cb2'][crate]  # Bin indices for crate
            bins = bmap['cb1'][slot][cb2_bix]  # absolute bins for slot
            bix = list(bin_priority[bins].argsort())  # bin index order by bin_priority
            for lane in range(Nlanes):
                if (crate, slot, lane) in bad_links:
                    # bs_map[:] = bix[-Nbins:]
                    bmap['cb3'][lane] = sorted(bix[-Nbins:])
                    del bix[-Nbins:]
                else:
                    # bs_map[:] = bix[:Nbins]
                    bmap['cb3'][lane] = sorted(bix[:Nbins])
                    del bix[:Nbins]
                # print '(%i,%i,%i)' % (crate, slot, lane), bmap['cb3'][lane], bin_map[(crate,slot)]['cb3'], bix
                # print '   -> (%i,%i)' % (0, 0), bin_map[(0,0)]['cb3']
            if set(bmap['cb3'].flatten()) != set(range(Nlanes * Nbins)):
                print('Selected', bmap['cb3'])
                raise RuntimeError('Invalid crossbar 3 bin selection')
        # for (crate, slot), bmap in bin_map.items():
        #     print '--_>(%i,%i)' % (crate, slot), bmap['cb3']

    @staticmethod
    def shuffle512_cb3_freq_remap(bin_map, bad_links, bin_priority):
        """
        Generates a frequency map by assigning flagged/less important frequency bins to
        links connected to bad/down GPU nodes. The remapping is
        restricted to changes at the third crossbar for 'shuffle512' operation.

        Parameters:
        -----------
        bin_map: dict
            Dict that contains the bin maps for all crossbars. In the format
            {(crate, slot):'cb1':cb1_map, 'cb2':cb2_map, 'cb3':cb3_map},...}
            `bin_map` is modified in place with the new optimized map.
        bad_links: list of (crate parity, slot, link) tuples
            List of links connected to bad/down GPU nodes. Least important frequencies are
            assigned to these links. Crate parity is either 0 (even) or 1 (odd). Slot
            is an integer between 0 and 15, and link is an integer between 0 and 7
        bin_priority: list or np.array
            1024-long array with the frequency priority of each frequency bin.
            bin_priority[i] is the priority of the ith frequency bin.
            A lower value has a higher priority.
        """
        import itertools

        # Number of freq. bins, crates (per crate pair), slots (per crate), links (per board)
        # (SHOULD BE ABLE TO GET THIS FROM FPGA ARRAY OBJECT)
        Nfreq, Ncrate, Nslot, Nlink = 1024, 2, 16, 8
        Nfreq_cs = Nfreq // (Ncrate * Nslot)  # Freq. bins per (crate, slot)
        Nfreq_link = Nfreq_cs // Nlink   # Freq. bins per (crate, slot, link)

        # Order links by how easy it is to assign RFI bins to them (easier for middle links
        # according to FIFO constraints) links at the top of the list will have more RFI bins
        link_rfi_assign_order = np.array([3, 4, 2, 5, 1, 6, 0, 7])
        for (crate, slot), bmap in bin_map.items():
            # Standard CB3 bin assignment (each row is a link)
            cb3_bins = np.arange(Nfreq_cs).reshape((Nlink, Nfreq_link), order='F')
            # Standard absolute frequency assignment (each row is a link)
            freq_bins_cs = np.arange(crate * Nslot + slot, Nfreq, Nfreq_cs).reshape(
                (Nlink, Nfreq_link), order='F')
            # Importance of each CB3 bin
            cb3_bin_order = bin_priority[freq_bins_cs.ravel()].reshape((Nlink, Nfreq_link))
            # Start clustering RFI bins on nodes according to link_rfi_assign_order
            # Assumes that there are no overflows in cb3 FIFOs as long as the
            # separation between cb3 bins (in range(31)) for a given lane is at least 4
            for gpu in range(Nfreq_link):
                # Sort all links of given gpu by lowest priority (RFI first)
                link_priority = np.argsort(cb3_bin_order[:, gpu])[::-1]
                # Go over all permutations of links until we find one
                # that meets the requirement of at least 4 cb3 bin separation.
                # By the way itertools.permutations works, it will start
                # with the permutations that cluster RFI bins on the links
                # according to link_rfi_assign_order
                for lp in itertools.permutations(link_priority):
                    if np.all(abs(np.array(lp) - link_rfi_assign_order) <= 4):
                        # The permutation is allowed. done
                        break
                cb3_bins[link_rfi_assign_order, gpu] = cb3_bins[lp, gpu]
                freq_bins_cs[link_rfi_assign_order, gpu] = freq_bins_cs[lp, gpu]

            i_top, i_bottom = 0, Nlink
            for link in range(Nlink):
                stream_id = (crate, slot, link)
                if stream_id in bad_links:  # Bad link: assign less important freq. bins
                    bmap['cb3'][link] = cb3_bins[link_rfi_assign_order[i_top]]
                    i_top += 1
                else:  # Good link: assign important freq. bins
                    bmap['cb3'][link] = cb3_bins[link_rfi_assign_order[i_bottom - 1]]
                    i_bottom -= 1

    @staticmethod
    def shuffle512_cb3_lane_remap(bin_map, bad_links, bin_priority):
        """
        Generates a frequency map by assigning flagged/less important frequency bins to
        links connected to bad/down GPU nodes. The remapping is
        restricted to changes at the third crossbar for 'shuffle512' operation.
        Also, this remapping is limited to swap frequency lists between links
        (it does not alter the content of the frequency lists).

        Parameters:
        -----------
        bin_map: dict
            Dict that contains the bin maps for all crossbars. In the format
            {(crate, slot):'cb1':cb1_map, 'cb2':cb2_map, 'cb3':cb3_map},...}
            `bin_map` is modified in place with the new optimized map.
        bad_links: list of (crate parity, slot, link) tuples
            List of links connected to bad/down GPU nodes. Least important frequencies are
            assigned to these links. Crate parity is either 0 (even) or 1 (odd). Slot
            is an integer between 0 and 15, and link is an integer between 0 and 7
        bin_priority: list or np.array
            1024-long array with the frequency priority of each frequency bin.
            bin_priority[i] is the priority of the ith frequency bin.
            A lower value has a higher priority.
        """

        # Number of freq. bins, crates (per crate pair), slots (per crate), links (per board)
        # (SHOULD BE ABLE TO GET THIS FROM FPGA ARRAY OBJECT)
        Nfreq, Ncrate, Nslot, Nlink = 1024, 2, 16, 8
        Nfreq_cs = Nfreq // (Ncrate * Nslot)  # Freq. bins per (crate, slot)
        Nfreq_link = Nfreq_cs // Nlink   # Freq. bins per (crate, slot, link)

        # Standard CB3 bin assginment (each row is a link)
        cb3_bins = np.arange(Nfreq_cs).reshape((Nlink, Nfreq_link), order='F')
        for (crate, slot), bmap in bin_map.items():
            # Standard absolute frequency assignment (each row is a link)
            freq_bins_cs = np.arange(crate * Nslot + slot, Nfreq, Nfreq_cs).reshape(
                (Nlink, Nfreq_link), order='F')
            # Importance of each CB3 bin
            cb3_bin_order = bin_priority[freq_bins_cs.ravel()].reshape((Nlink, Nfreq_link))
            # Check which cb3_bins are RFI.
            # Assumes freq_bins with order > 779 are RFI (expected from static RFI mask)
            cb3_bin_rfi_mask = cb3_bin_order > 779
            # Number of rfi bins per link
            rfi_per_link = np.sum(cb3_bin_rfi_mask, axis=1)
            # Indices that sort links by increasing number of RFI bins (links at the
            # of the list have more RFI bins)
            link_priority = np.argsort(rfi_per_link)
            i_top, i_bottom = 0, Nlink
            for link in range(Nlink):
                stream_id = (crate, slot, link)
                if stream_id in bad_links:  # Bad link: assign less important freq. bins
                    bmap['cb3'][link] = cb3_bins[link_priority[i_bottom - 1]]
                    i_bottom -= 1
                else:  # Good link: assign important freq. bins
                    bmap['cb3'][link] = cb3_bins[link_priority[i_top]]
                    i_top += 1

    @staticmethod
    def shuffle512_cb3_remap(mode, bin_map, bad_links, freq_bins, output_cb3_bins=False):
        """
        Generates a frequency map by assigning flagged/less important frequency bins to
        links connected to bad/down GPU nodes. The remapping is
        restricted to changes at the third crossbar for 'shuffle512' and 'shuffle256' operation.

        Parameters:

        mode (str): operational mode of the corner turn engine. Either
            `'shuffle512'` or '`shuffle256'`.

        bad_links (list of tuples): list of [crate parity, slot, link] lists
            List of links connected to bad/down GPU nodes. Least important frequencies are
            assigned to these links. Crate parity is either 0 (even) or 1 (odd). Slot
            is an integer between 0 and 15, and link is an integer between 0 and 7

        freq_bins (list of int): list or np.array
            1024-long array with frequency bins ordered by importance (important bins first).
            Frequency bins are assigned to good/up links/nodes when available based on their
            importance.

        output_cb3_bins: bool (optional)

            If False, the frequency assignment is given as a relative bin
            index that address the 32 bins at the input of the 3rd crossbar
            (in the range 0-31).

            If True, the frequency assignment is given as frequency bins (in the range
            0-1023)


        Returns

        freq_map: dict
            Describes the frequency bin assignment for each link. Its items have the form

            {..., (crate parity, slot, link): [freq. bin 0, ..., freq. bin 3], ...} if
            output_cb3_bins=False, or

            {..., (crate parity, slot, link): [cb3 bin 0, ..., cb3 bin 3], ...} if
            output_cb3_bins=True.
        """

        freq_bins = np.array(freq_bins)  # Make sure freq_bins is an np.array
        freq_bins_priority = np.argsort(freq_bins)  # indices that sort freq_bins in ascending order
        bad_links = [tuple(link) for link in bad_links]

        Nfreq = 1024  # Number of frequency bins
        Nslot = 16  # boards per crates
        Nlink = 8  # GPU links per board
        if mode == 'shuffle512':
            Ncrate = 2
            Nbix = 32  # Bins per CB3 input
            Nbins = 4  # Bins per CB3 output
            bix_to_bin = lambda crate, slot, bix: (Nslot * Ncrate * bix) + (Nslot * crate) + slot
        elif mode == 'shuffle256':
            Ncrate = 1
            Nbix = 64
            Nbins = 8
            bix_to_bin = lambda crate, slot, bix: (Nslot * bix) + slot
        else:
            raise ValueError('Invalid shuffle mode %s' % mode)

        # Number of freq. bins, crates (per crate pair), slots (per crate), links (per board)
        # (SHOULD BE ABLE TO GET THIS FROM FPGA ARRAY OBJECT)
        # Nfreq_cs = Nfreq // (Ncrate * Nslot) # Freq. bins per (crate, slot) = 32 (4 per lane) (Now Nbix)
        # Nbins = Nfreq_cs // Nlink   # Freq. bins per (crate, slot, link) = 4 per lane (now Nbins)

        freq_remap = {}
        for slot in range(Nslot):
            for crate in range(Ncrate):
                # Freq bins that can be assigned to (crate parity, slot) under standard map
                # cb3 output, shuffle512: bin = 16*2*8*bix + 16*2*lane + 16*crate + slot, bix=0..3
                # cb3 output, shuffle256: bin = 16*8*bix + 16*lane  + slot, bix=0...7, bix=0..7

                # available_bins = np.arange(crate * Nslot + slot, Nfreq, Nfreq_cs)
                available_bix = np.arange(Nbix)
                # Compute absolute bin number available at the input of CB3 on this (crate, slot)
                available_bins = bix_to_bin(crate, slot, available_bix)
                # Compute indices of available bins/bix, in priority order (first is most important)
                available_priority = np.argsort(freq_bins_priority[available_bins])
                # Compute bin indices, sorted by importance. Convert to a list so we can easily delete items.
                available_bix = list(available_bix[available_priority])
                # Indices of allowed freq_bins, sorted by importance
                # available_bins_indices = np.sort(freq_bins_priority[available_bins])
                # available_bins = list(available_bins[available_priority]) # absolute bins, sorted by importance

                # i_top = 0
                # i_bottom = Nbix
                # Assign bix, lane per lane
                for link in range(Nlink):
                    link_id = (crate, slot, link)
                    if link_id in bad_links:  # Bad link: assign less important freq. bins
                        bix = available_bix[-Nbins:]  # take the bottom (lower priority) bix
                        del available_bix[-Nbins:]  # remove from the list
                        # freq_remap[link_id] = list(
                        #     available_bix[i_bottom - Nbins:i_bottom] if output_cb3_bins else
                        #     freq_bins[available_bins_indices[i_bottom - Nbins:i_bottom]])
                        # i_bottom -= Nbins
                    else:  # Good link: assign important freq. bins
                        bix = available_bix[:Nbins]  # take the top bix (higher priority)
                        del available_bix[:Nbins]  # remove from the list
                        # freq_remap[link_id] = list(available_bix[i_top:i_top + Nbins] if
                        #                         output_cb3_bins else
                        #                         freq_bins[available_bins_indices[i_top:i_top + Nbins]])
                        # i_top += Nbins
                    # Order selected bins in increasing order. Now a numpy
                    # array again.  The bins are always transmitted that way.
                    bix = np.sort(bix)
                    freq_remap[link_id] = list(bix if output_cb3_bins else bix_to_bin(crate, slot, bix))
                if len(available_bix):
                    raise RuntimeError('Not all bins were processed. This should not happen')
        return freq_remap

    def init_corner_turn(
            self,
            mode,
            dsmap=list(range(16)),
            frames_per_packet=1,
            send_flags=True,
            chan8_channel_map=list(range(16)),
            tx_power=None,
            bin_map=None,
            bad_links=None,
            bin_priority=None,
            remap_level=0,
            sync=True):
        """ Setup the crossbars and data shuffling in every board of the array.

        Parameters:

            mode (str): One of the crossbar engine operational mode
                ('shuffle16', 'shuffle256' etc.). Is passed to
                ib.init_crossbar().

            dsmap (list of int): Shuffle remap that is passed to
                ib.init_crossbar(). Defaults to `range(16)`.

            frames_per_packet (int): Number of frames per packet. Defaults to
                1. Is passed to ib.init_crossbar().

            chan8_channel_map (list): Map that is passed to
                ib.init_crossbar(). Defaults to `range(16)`.

            tx_power (dict): Describes the initial (training) and final TX
                 power to be used by the backplane PCB and QSFP links. applied
                 to the backplane PCB GTX links.


        The GTX receivers that have no corresponding transmitter is put in
        reset so it won't generate random packets into the following crossbar.
        """
        if tx_power is None:
            tx_power = {'corner_turn': [
                dict(lane_group='pcb', default=(10, 15)),
                dict(lane_group='qsfp', default=(10, 15))]}

        tx_list = []

        # crate_set = set(ib.crate for ib in self.ib)
        # if len(crate_set) != 1:
        #     raise RuntimeError('All boards must be in the same crate. The provided set of '
        #                        'Iceboards have the following crates: %r' % crate_set)
        # crate = crate_set.pop()

        self.logger.info(f'{self!r}: Configuring crate-wide data shuffling '
                         f'with frames_per_packet={frames_per_packet}')

        bin_map = self.get_corner_turn_bin_map(
            mode=mode,
            bad_links=bad_links,
            bin_priority=bin_priority,
            remap_level=remap_level)

        #####################
        # Set-up transmitters
        #####################
        self.logger.info(f'{self!r}: Setting GTX transmit power for all boards')
        self.logger.debug(f'{self!r}: Using tx_power={tx_power!r}')

        self.corner_turn_stream_ids = {}
        for i, ib in enumerate(self.ib):
            self.logger.debug(f'{self!r}: **** Initializing transmitters for IceBoard {ib} (SN{ib.serial}) ****')
            ib.set_corr_reset(0)  # Put the corner_turn engine out of reset

            tx_list.append((ib.slot, 0))  # Register Bypass lane (lane 0) as a transmitter in this slot
            for j, gtx in enumerate(ib.BP_SHUFFLE.gtx):
                gtx.TXINHIBIT = 0
                tx_list.append((ib.slot, j + 1))

            # Initialize the crossbars to select and send data in a specific format
            # ib.init_crossbars(dsmap, frames_per_packet=frames_per_packet, cb1_lanes=cb1_lanes, cb1_bins=cb1_bins,
            #                   cb1_bypass=cb1_bypass, cb2_lanes=cb2_lanes, cb2_bins=cb2_bins, cb2_bypass=cb2_bypass,
            #                   remap=remap, bp_bypass=bp_bypass)
            stream_ids = ib.init_crossbars(
                mode,
                dsmap=dsmap,
                frames_per_packet=frames_per_packet,
                send_flags=send_flags,
                chan8_channel_map=chan8_channel_map,
                bin_map=bin_map[ib.get_id()])

            for lane, stream_id in enumerate(stream_ids):
                self.corner_turn_stream_ids[ib.get_id(lane)] = stream_id

        # Check if stream IDs are unique
        if len(self.corner_turn_stream_ids) != len(set(self.corner_turn_stream_ids.values())):
            raise self.logger.warning(f'{self!r}: Stream IDs are not unique across the array')

        if ib.crate:

            #####################
            # Set-up receivers
            #####################
            for i, ib in enumerate(self.ib):
                # Disable all receivers for which there are no transmitters
                for j, gtx in enumerate(ib.BP_SHUFFLE.gtx[0:ib.BP_SHUFFLE.NUMBER_OF_PCB_LINKS]):
                    if ib.slot is None:
                        continue
                    rx = (ib.slot, j + 1)
                    tx = ib.crate.get_matching_tx(rx)

                    # disable receivers that have no corresponding transmitters
                    if tx in tx_list:
                        gtx.USER_GTRXRESET = 0
                    else:
                        gtx.USER_GTRXRESET = 1
                        # gtx.USER_RESET = 1

            # reset DFE at low power, then increase power
            for index in (0, 1):
                for tx_group in tx_power['corner_turn']:
                    lane_group = tx_group['lane_group']
                    default = tx_group['default']
                    exceptions = tx_group.get('exceptions', [])
                    self.logger.debug(
                        f'{self!r}: TX power parameters are: {tx_group!r} '
                        f'(default={default!r}, exceptions={exceptions!r})')
                    self.set_tx_power(lane_group=lane_group, default_power=default, exceptions=exceptions, index=index)
                if index == 0:
                    time.sleep(0.3)
                    for ib in self.ib:
                        self.ib.BP_SHUFFLE.reset_rx_equalizers()

            self.ib.BP_SHUFFLE.reset_stats()

            # Print links
            for ib in self.ib:
                for i in range(ib.NUMBER_OF_CROSSBAR1_OUTPUTS):
                    if ib.slot is None:
                        continue
                    rx = (ib.slot, i)
                    tx = ib.crate.get_matching_tx(rx)
                    if tx not in tx_list:
                        self.logger.debug(f'{self!r}: In {ib.crate!r}, {rx} has no corresponding transmitter')

        if mode not in ('shuffle128', 'shuffle16', 'chord16'):  # ***JFC: temporary hack to exclude modes that have no crate info an dget_frequency_map() crashes.
            # Get the exhaustive frequency map that is implemented by the current actual corner turn engine.
            # The map is in the format (crate, slot, lane): freq_bin_list. crate and slot might not be numeric if there is no crate or crate number.
            freq_map = self.get_frequency_map(format='l:bb')
            # Retain only one bin number  for each bin
            self.corner_turn_frequency_bins = {lane_id: sorted(set(data['data']) - set([None]))
                                               for lane_id, data in freq_map.items()}

        # Double check that the frequency map that we obtained matches our target bin map.
        if mode in ('shuffle256', 'shuffle512'):
            errors = 0
            for (crate, slot, lane), actual_bins in list(self.corner_turn_frequency_bins.items()):
                bs = bin_map[(crate % 2, slot)]
                expected_bins = bs['cb1'][slot][bs['cb2'][crate % 2]][bs['cb3'][lane]]
                if not all(np.equal(expected_bins, actual_bins)):
                    print(f'Link {(crate, slot, lane)} do not match: '
                          f'Expected bins: {expected_bins!r}, got bins {actual_bins!r}')
                    errors += 1

            if errors:
                raise RuntimeError('Actual frequency mapping does not match the expected one')

        # sync boards
        # soft_sync(c, sync_board)

        self.logger.debug(f'{self!r}: Resetting the GPU transmitters.')
        self.ib.GPU.CORE_RESET = 1
        time.sleep(.1)
        self.ib.GPU.CORE_RESET = 0
        time.sleep(.1)

        self.logger.info(f'{self!r}: Shuffling initialization completed.')
        if sync:
            self.sync()

    def set_tx_power(self, default_power=(5, 10), lane_group=None, exceptions=[], index=0):
        """ Set the power level of the corner-turn engine GTX transmitters.

        Parameters:

            default_power (int or tuple): power level to use for all gtx that
                are not exception list. An int is interpreted as a
                single-element tuple. In the case where multiple power values
                are to be specified, the value within the tuple is selected by
                `index`.

            lane_group (str): lane group for which the power is set ('pcb' or
                'qsfp').

            exceptions (list): list of the lane-specific power level exceptin, in the form:

                [((crate, slot, lane), power_tuple), ...}

            index (int): used to select which value within a power tuple, list
                or map will be used to set power.
        """
        if isinstance(default_power, int):
            default_power = (default_power, )

        exceptions = {tuple(node_id): power_tuple for node_id, power_tuple in (exceptions or [])}
        self.logger.info(f'{self!r}: Setting GTX power for lane group {lane_group} to power index {index}')
        self.logger.debug(f'{self!r}:    Default power is {default_power}')
        self.logger.debug(f'{self!r}:    Power exceptions are {exceptions}')

        for ib in self.ib:
            bp = ib.BP_SHUFFLE
            power_tuples = [(lane, exceptions.get(ib.get_id(lane), default_power)[index])
                            for lane, gtx in enumerate(bp.get_gtx(lane_group=lane_group)) if gtx]
            self.logger.debug(f'{self!r}: setting Tx power for {ib} {lane_group} links')
            bp.set_tx_power(power_tuples, lane_group)

    # def set_tx_power(self, pmin=6, pmax=13, pre=3):
    #     """ Set the transmit power of each transceiver based on the link length to minimize crosstalk.
    #     The shortest link pas the power ``pmin``, and the longest link has ``pmax''.
    #     The precursor value can also be set to ``pre`` if it is not ``None``.
    #     """
    #     for ib in self.ib:
    #         for gg in ib.BP_SHUFFLE.gtx:
    #             if pre is not None:
    #                 gg.TXPRECURSOR = pre
    #             rx_id = (ib.slot, gg.instance_number + 1)
    #             if gg.instance_number <= 14:
    #                 net_length = gg.fpga.crate.get_rx_net_length(rx_id)
    #                 p = pmin + int((pmax-pmin)*(net_length-1515.)/(16081-1515))
    #                 print '%s, len=%f, power=%i' % (rx_id, net_length, p)
    #                 gg.TXDIFFCTRL = p
    #             else:
    #                 gg.TXDIFFCTRL = pmax

    def set_test_pattern(self):
        for ic in self.ic:
            for (slot, ib) in ic.slot.items():
                for ch in range(16):
                    ib.set_funcgen_function('ab', a=(slot - 1) << 4, b=ch << 4, channels=[ch])
                ib.set_data_source('funcgen')

    IRIGB_SYNC_METHODS = ('irig-b', 'irigb', 'distributed_time')
    TRIG_SYNC_METHODS = ('trig',  'centralized_soft_trigger')
    LOCAL_SYNC_METHODS = ('local', 'local_soft_trigger')

    def set_sync_method(self, method='irig-b', source=None, master=None, master_source=None, master_output=0):
        """ Sets the global syncing method, and setup the boards accordingly.

        Parameters:

            method (string): Specifies the synchronization method:

                - ``'irig-b'`` (or ``'irigb'``, ``'distributed_time'``): All boards sychronize when
                  the target IRIG-B time is reached. `source` specifies the source of the IRIG-B
                  signal that feeds the IRIG-B decoder module.

                - ``'trig'`` (or ``'centralized_soft_trigger'``): All boards synchronize on the
                  rising edge of the signal specified in ``source``.

                - ``'local'`` (or ``'local_soft_trigger'``): Each board generates its own
                  software-generated SYNC trigger when the `sync()` method is called. This can be
                  used to reset the processing pipeline of each board, but does not allow
                  synchronized capture packed capture between board or use of multi-board
                  corner-turning operational modes.

            source: (string): source of synchronization signal.
                - If ``method='irig-b'``, `source` must match one of the available IRIG-B sources (see motherboard method `set_irigb_source_async()`).

                - If ``method='trig'``, `source` must match one of the available REFCLK sources (see REFCLK method `set_sync_source`).

            master (Motherboard, string, int): Motherboard that is to be configured to generate the time
                or trigger signals.

                - None: No master board is set up.
                - `Motherboard` object: use the provided Motherboard object
                - str: Use board with specified serial number (must include all leading zeros)
                - int: Use board in specified slot number (starts with index 1)

            master_source: (string): source of the synchornization signal that the master board will
                send to the master output. Used as ``source`` paramater in method
                `set_user_output_source` called on the master board.

            master_output: (int): User output to which the synchronization signal will be sent. Used
                as ``output`` paramater in method `set_user_output_source` called on the master
                board.

        The boards start their sync process on the next rising edge of the 10
        MHz reference after the trigger event.  This events shoudld
        to occur between two reference clock edges in order to guarantee
        detection on the same edge across the entire array.

        """
        if master:
            if isinstance(master, str):
                master = self.ib.get(serial=master)
            elif isinstance(master, int):
                master = self.ib.get(slot=master)
            master.set_user_output_source(source=master_source, output=master_output or 0)
            if source == 'bp_gpio_int':
                # Enable GPIO_INT as an output only on the master board
                # TODO: JFC: This code should be moved to lower-level methods
                self.ib.GPIO.BP_GPIO_INT_EN = 0
                master.GPIO.BP_GPIO_INT_EN = 1

        self.sync_master = master
        self.sync_method = method

        if source == 'bp_gpio_int' and not master:
            raise RuntimeError('A master board should be specified when bp_gpio_int is used as a source')

        if method in self.IRIGB_SYNC_METHODS: # REFCLK trigs on the output of the IRIG-B time comparator
            source = source or 'bp_time'
            self.ib.set_sync_source('irigb')
            self.ib.set_irigb_source_sync(source)
        # elif method == 'centralized_time_trigger':
        #     source = source or 'bp_time'
        #     # if not master or not master_source:
        #     #     raise ValueError('In the centralized time trigger mode, the master board '
        #     #                      'and its time source must be specified')
        #     # self.ib.set_sync_source(source)
        #     master.set_irigb_source_sync(master_source)
        #     master.set_user_output_source('irigb_trig')
        elif method in self.TRIG_SYNC_METHODS: # REFCLK trigs directly on the rising edge of the specified source signal
            source = source or 'bp_time'
            self.ib.set_sync_source(source)
        elif method in self.LOCAL_SYNC_METHODS:
            self.ib.set_sync_source('local')
            self.ib.sync()
        # elif method == 'external':
        #     source = source or 'sma_a'  # if no source is specified
        #     if master:
        #         raise ValueError('In the local soft trigger mode, a master board should NOT specified')
        #     if master_source:
        #         raise ValueError('In the local soft trigger mode, a master_source should NOT be specified')
        #     self.ib.set_sync_source(source)
        else:
            raise ValueError(f"Unknown syncing method '{method}'")

    def sync(self, delay=2 - 0.006556800, check=True, align_to_seconds=True, max_trials=3, max_sync_time_difference=None):
        """ Generate a SYNC event across the whole array based on the syncing method set by ``set_sync_method()``.

        Parameters:

        delay (float): Sets in how much time in the future after the current
            time the sync time  will happen. The system will determine and the
            time it takes to issue the command across the array. If
            ``align_to_seconds`` is True, the delay is applied after the
            current time + propagation time is rounded to the second, allowing
            a find tuning of the trigger time down to 10 nanosecond
            increments.

        check (bool): If True, the method will read the SYNC
            counters on every board to confirm that the SYNC really happened
            everywhere.

        align_to_seconds (bool): if True, the trigger time **before** the
            ``delay`` is applied is rounded to the closest integer second.
        """

        # Don't try to check the SYNC status if we don't have a SYNC engine (REFCLK) in the firmware of all boards

        if check and not all(list(ib.REFCLK for ib in self.ib)):
            check = False
            self.logger. warning(f'{self!r}: SYNC will not be checked as not all firmware have SYNC logic')

        trial = 0
        while True:
            try:
                if check:
                    sync_ctr_before = self.ib.REFCLK.SYNC_CTR

                self.sync_timestamps = []
                self.sync_timestamp = None

                if self.sync_method in self.TRIG_SYNC_METHODS:
                    self.sync_master.generate_sync()

                # elif self.sync_method == 'centralized_time_trigger':
                #     dt = self.sync_master.get_irigb_time_sync()
                #     print('Triggering SYNC at ', dt.isoformat())
                #     self.sync_master.set_irigb_trigger_time_sync(dt, delay=delay)
                #     t0 = time.time()
                #     while self.sync_master.is_irigb_before_trigger_time_sync():
                #         if time.time() - t0 > delay + 1:
                #             raise RuntimeError('Timout while waiting for the IRIG-B-based SYNC to complete')

                elif self.sync_method in self.IRIGB_SYNC_METHODS:
                    # Estimate how much time it takes to set the trigger time
                    dt = self.ib[0].get_irigb_time_sync()
                    t0 = time.time()
                    # set the trigger far enough in time it should not happen before we reprogram another delay
                    self.ib.set_irigb_trigger_time_sync(dt, delay=300)
                    setting_time = (time.time() - t0)
                    self.logger.info(f'{self!r}: It takes {setting_time:0.3f} seconds to set the trigger time across the array')
                    setting_time = round(2 * setting_time)
                    # Now set the trigger time using that delay
                    dt = self.ib[0].get_irigb_time_sync()
                    if align_to_seconds:
                        self.logger.info(f'{self!r}: Rounding trigger time to the second')
                        dt = dt.replace(microsecond=0)
                    # self.print_flush()
                    t0 = time.time()
                    if isinstance(delay, (list, tuple)):
                        max_delay = max(delay)
                        sync_time = []
                        for ib,dly in zip(self.ib, delay):
                            self.logger.info(f'{self!r}: Triggering SYNC on {ib!r} {setting_time+dly:0.9f} seconds after {dt.isoformat()}')
                            sync_time.append(ib.set_irigb_trigger_time_sync(dt, delay=setting_time+dly))
                        self.sync_start_time = sync_time
                    else:
                        max_delay = delay
                        self.logger.info(f'{self!r}: Triggering SYNC {setting_time+delay:0.9f} seconds after {dt.isoformat()}')
                        self.sync_start_time = sync_time = self.ib.set_irigb_trigger_time_sync(dt, delay=setting_time+delay)
                    self.logger.info(f'{self!r}: It took {time.time() - t0:0.3f} seconds to set the final trigger time')
                    t0 = time.time()
                    # for _ in range(30):
                    while any(self.ib.is_irigb_before_trigger_time_async()):
                        #     print(f'trig={any(self.ib.is_irigb_before_trigger_time_async())}, sync={self.ib.REFCLK.SYNC_CTR}')
                        if time.time() - t0 > max_delay + 1:
                            raise RuntimeError('Timout while waiting for the IRIG-B-based SYNC to complete')
                        time.sleep(0.1)
                    time.sleep(0.1) # make sure the sync sequence has time to finish
                elif self.sync_method in self.LOCAL_SYNC_METHODS:
                    self.ib.sync()
                # elif self.sync_method == 'external':
                #     # Wait until one of the board sees a trig. We assume all
                #     # of them will have triggered by the time that is checked
                #     # later.
                #     t0 = time.time()
                #     while all([ib.REFCLK.SYNC_CTR == sync_ctr_before[i] for i, ib in enumerate(self.ib)]):
                #         if time.time() - t0 > 5 + 1:
                #             raise RuntimeError('Timout while waiting for the external sync signal')
                else:
                    valid_methods = ', '.join((self.IRIGB_SYNC_METHODS[0], sync.TRIG_SYNC_METHODS[0], self.LOCAL_SYNC_METHODS[0]))
                    raise ValueError(f"Unknown syncing method '{self.sync_method}'. Valid methods are {valid_methods}")

                # Check if all boards of the array have sync'ed by looking at the sync counter
                if check:
                    sync_ctr_after = self.ib.REFCLK.SYNC_CTR
                    bad_ib = [ib for i, ib in enumerate(self.ib) if (sync_ctr_after[i] - sync_ctr_before[i]) & 0xf != 1]
                    if bad_ib:
                        bad_boards_str = ','.join(repr(ib) for ib in bad_ib)
                        raise RuntimeError(f'The following IceBoards did not SYNC properly: {bad_boards_str}')

                if self.sync_method in self.IRIGB_SYNC_METHODS:
                    self.sync_timestamps = ts = self.ib.get_irigb_time_sync(trig=False, format='raw')
                    self.sync_timestamp = ts[0]

                    delta_ts = max(ts.nano) - min(ts.nano)
                    irigb_times = '\n'.join(
                        f'         {ib!r}: {ts[i].isoformat()} '
                        f'({ts[i].nano} ns since epoch, '
                        f'{ts[i].nano - sync_time[i].nano} ns after sync)'
                        for i, ib in enumerate(self.ib))
                    self.logger.info(f'{self!r}: The IRIG-B time for Frame 0 on all boards is:\n{irigb_times}')
                    self.logger.info(f'{self!r}: The maximum Frame 0 time difference is {delta_ts} ns')
                    max_sync_time_difference = max_sync_time_difference or self.max_sync_time_difference
                    if delta_ts > max_sync_time_difference:
                        raise RuntimeError(f'The Frame 0 time difference of {delta_ts} '
                                           f'exceeds the maximum limit of {max_sync_time_difference}')
                for ib in self.ib:
                    ib.reset_scaler_overflow_flags()

                break # we made it all the way through successfulle, so exit the trial loop

            except Exception as e:
                trial += 1
                if trial >= max_trials:
                    raise RuntimeError(f'SYNC failed after {trial} trials. The last exception was:\n{e!r}')
                self.logger.warning(f'{self!r}: SYNC failed on trial {trial}/{max_trials} '
                                    f'due to the following error. Will retry.\n{e!r}')

    def set_channelizers(self, channels=None, local_sync=None, sync=True, **kwargs):
        """
        Configures all channelizers in the array.

        See firmware set_channelizer(...) for details.
        """
        if local_sync is not None:
            raise RuntimeError('local_sync parameter is not supported on array-wide set_channelizer call')
        for ib in self.ib:
            ib.set_channelizer(**kwargs, channels=channels, local_sync=False)
        if sync:
            self.sync()

    async def set_channelizers_async(channels=None, local_sync=None, sync=True, **kwargs):
        """
        Configures all channelizers in the array.

        See firmware set_channelizer(...) for details.
        """
        if local_sync is not None:
            raise RuntimeError('local_sync parameter is not supported on array-wide set_channelizer call')
        for ib in self.ib:
            ib.set_channelizer(**kwargs, channels=channels, local_sync=False)
            await asyncio.sleep(0)
        if sync:
            self.sync()

    def set_noise_injection(
            self,
            board,
            enable=False,
            offset=0,
            high_time=8388608,
            period=16777216,
            local_sync=True,
            output='bp_sma'):
        """ Configure noise injection gating signal.

        Parameters:

            board:  is either the serial number (as a string) of the target
                board, or is the target motherboard object. If board evaluates to
                False (empty string), the noise injection setup is skipped.

        Notes:

            A sync event is necessary to restart the counters so the gating signal will be generated properly.

        """
        if not board:
            return

        ib = self.get_iceboard(board)

        if enable:
            ib.set_user_output_source('pwm', output=output)

        ib.set_pwm(enable=enable, offset=offset, high_time=high_time,
                   period=period, local_sync=local_sync)

    async def get_user_output_state_async(self, board, output='bp_sma'):
        """ Get the current state of the specified user output and the
        information about its source.

        Parameters:

            board (tuple/list, str, Motherboard):  is either a 2-element tuple or
                list describing the board, or a string containing the board's
                serial number or hostname.

            output (str): name of the user output to which the noise injection
                hardware is connected. This will be used to verify if the PWM
                signal is actually sent to that output.

        Returns:

            A dict containing the following fields:

                board (str or tuple/list): the target board, as specified in the `board` parameter

                output (str): output to which the noise injection electronics
                    is connected, as specified by the `output` parameter.

                output_source (str): name of the source driving the specified
                    'output'.

                pwm_offset (int): the PWM generator waveform offset from frame 0, in frames

                pwm_high_time (int): the high time of the PWM generator waveform, in frames

                pwm_period (int): the period of the PWM generator waveform, in frames

                pwm_enabled (bool): True when the PWM generator is not in reset. Useful when output_source is 'pwm'.

                user_bit0 (int): State of the user bit 0. Useful when output_source is 'user_bit0'.

                user_bit1 (int): State of the user bit 1. Useful when output_source is 'user_bit1'.


            If `board` evaluates to False, only the first 3 fields are returned, with
            output_source set to None.

        """
        status = dict(board=board,
                      output=output,
                      output_source=None)
        if not board:
            return status

        ib = self.get_iceboard(board)
        output_source = ib.get_user_output_source(output)
        offset, high_time, period, reset = ib.get_pwm()
        user_bit0, user_bit1 = ib.get_user_bits()
        status.update(
            output_source=output_source,
            pwm_offset=offset,
            pwm_high_time=high_time,
            pwm_period=period,
            pwm_enabled=not reset,
            user_bit0=user_bit0,
            user_bit1=user_bit1)
        return status

    def get_stream_id_map(self):
        """ Return the stream_ids if every channel of the array, indexed by channel_id.

        Returns:
            dict if the format {channel_id:stream_id}, where channel_id is a (crate, slot, channel) tuple.
        """

        stream_id_map = {}
        for ib in self.ib:
            stream_id_map.update(ib.get_stream_id_map())

        if len(stream_id_map) != len(set(stream_id_map.values())):
            self.logger.warning(f'{self!r}: Stream IDs are not unique!')

        return stream_id_map

    # ---------------------
    # -- Correlator methods
    # ---------------------

    def get_data_socket(self, port_number=None):
        """
        Return a UDP socket that receives the raw/correlator data, and setup all boards to use that same socket.

        If the socket does not already exists, the FPGA will be configured to send the data to the returned socket.

        Parameters:

            port_number (int): Port number to use:
                - If ``None``, attempts to open a socket at the destination port currently programmed in the FPGA. If that port is zero, act as if ``port_number`` =0.
                - If zero, open a socket at a random  (OS-provided) port, and set the corresponding destination port in the FPGA.
                - If non-zero, get a socket bound to the specified port. An exception will be raised if that port is already used by another program.

        Returns:

            socket.socket(): a UDP socket.
        """
        # get socket from one board in the array
        sock = self.ib[0].get_data_socket(port_number=port_number)
        for b in self.ib:
            b.set_data_socket(sock)
        return sock


    def get_correlator_params(self):
        """ Returns the correlator geometry and configuration """
        ib = self.ib[0]  # we assume all boards have correlators and they are all the same
        return ib.get_correlator_params()

    def get_packet_receiver(self, port_number=None):
        """ Returns an existing UDP packet receiver or create a new one if none has been created yet.

        Parameters:

            port_number (int): Specifies the port to use if a socket has not already been allocated.
                - If ``None``, attempts to open a socket at the destination port currently programmed in the FPGA. If that port is zero, act as if ``port_number`` =0.
                - If zero, open a socket at a random  (OS-provided) port, and set the corresponding destination port in the FPGA.
                - If non-zero, get a socket bound to the specified port. An exception will be raised if that port is already used by another program.

        Returns:

            UDPPacketReceiver instance
        """
        if not self.packet_receiver:
            sock = self.get_data_socket(port_number=port_number)
            self.packet_receiver = UDPPacketReceiver(sock) # ***todo: move packet receiver to utils (can be used by raw capture also)
        return self.packet_receiver

    def get_data_receiver(self, port_number=None):
        """ Returns a raw data receiver.

        A UDP packet receiver and associated socket will be created if none already exist, otherwise the existing ones will be reused.
        The correlator packet receiver and the raw data receiver share the same socket and UDP packet receiver.


        Parameters:

            port_number (int): Specifies the port to use if a socket has not already been allocated for the UDP packet receiver.
                - If ``None``, attempts to open a socket at the destination port currently programmed in the FPGA. If that port is zero, act as if ``port_number`` =0.
                - If zero, open a socket at a random  (OS-provided) port, and set the corresponding destination port in the FPGA.
                - If non-zero, get a socket bound to the specified port. An exception will be raised if that port is already used by another program.

        Returns:

            A PROBER or UCAP data receiver instance (RawFrameReceiver)
        """

        if not self.data_recv:
            pr = self.get_packet_receiver(port_number=port_number)
            self.data_recv = self.ib[0].UCAP.get_data_receiver(pr)
        return self.data_recv

    def start_data_capture(self, *args, sync=None, **kwargs):
        """ Starts data capture on all channelizers of all boards in the array
        """
        for b in self.ib:
            b.start_data_capture(*args, **kwargs)
        if sync:
            self.sync()

    def get_corr_receiver(self, port_number=None):
        """ Returns a correlator packet receiver.

        A UDP packet receiver and associated socket will be created if none already exist, otherwise the existing ones will be reused.
        The correlator packet receiver and the raw data receiver share the same socket and UDP packet receiver.


        Parameters:

            port_number (int): Specifies the port to use if a socket has not already been allocated for the UDP packet receiver.
                - If ``None``, attempts to open a socket at the destination port currently programmed in the FPGA. If that port is zero, act as if ``port_number`` =0.
                - If zero, open a socket at a random  (OS-provided) port, and set the corresponding destination port in the FPGA.
                - If non-zero, get a socket bound to the specified port. An exception will be raised if that port is already used by another program.

        Returns:

            UCorrFrameReceiver instance
        """

        def select_func(cr):
            for b in self.ib:
                b.CORR.select()

            corr_config = self.ib[0].CORR.get_config()
            cr.update_config(corr_config)

        corr_config = self.ib[0].CORR.get_config()

        if not self.corr_recv:
            pr = self.get_packet_receiver(port_number=port_number)
            self.corr_recv = self.ib[0].CORR.get_corr_receiver(packet_receiver=pr, config=corr_config, pre_func=select_func)
        return self.corr_recv


    def start_correlators(self, integration_period, autocorr_only=False, no_accum=False,
                          bandwidth_limit=0.5e9, verbose=0):
        """
        Start all the correlators in the array.


        Parameters:

            integration_period (32-bit int): Number of frames to integrate before sending the
               correlated products.

            autocorr_only (bool): When 'True', the correlator will only send the autocorrelation
               products

            no_accum (bool): When 'True', the correlator does not accumulate products from multiple
                frames. It just returns the product from the last frame on the integration period.

            bandwidth_limit (float): Maximum acceptable data bandwidth that each correlator can
               produce, in bits/s. Default is 0.5 Gbps/number_of_boards. An exception will be raised if the correlator parameters are to make the
               data exceed this bandwidth in order to avoid operating in a high packet loss environment

        """
        for ib in self.ib:
            ib.start_correlator(integration_period=integration_period,
                                autocorr_only=autocorr_only,
                                no_accum=no_accum,
                                bandwidth_limit=bandwidth_limit/len(self.ib),
                                verbose=verbose)

    async def start_correlators_async(self, integration_period, autocorr_only=False):
        """
        """
        for ib in self.ib:
            ib.start_correlator(integration_period, autocorr_only=autocorr_only)
            await asyncio.sleep(0)

    async def set_offset_binary_encoding_async(self, offset_encoding_enabled):
        """
        """
        self.logger.warning(f'{self!r}: set_offset_binary_encoding() is called. This should be removed')  # ***JFC
        for ib in self.ib:
            ib.set_offset_binary_encoding(offset_encoding_enabled)
            await asyncio.sleep(0)

    def get_channel_ids(self):
        """ Returns the channel IDs of every board and channel in the array """
        return [cid for ib in self.ib for cid in ib.get_channel_ids()]

    def get_iceboard(self, board):
        """ Return the Motherboard specified by tuple or serial number.

        Parameters:

            board (tuple, str or Motherboard): board identifier.

                - If `board` is a str, it will be matched with either the
                  board serial number string (with leading zeros) or with the
                  board hostname.
                - If `board` is a tuple or a list,  describing a (crate,
                  slot), it will be matched with the board ID as return by the
                  board's get_id().
                - If `board` is already an Motherboard object, `board` is
                  returned directly. An exception is raised if there are
                  multiple matches.
        """
        if not self.ib:
            raise RuntimeError('There are no Motherboards to select in the current array')
        elif isinstance(board, type(self.ib[0])):
            return board
        elif isinstance(board, str):
            if board in self.ib.serial:
                return self.ib.get(serial=board)
            elif board in self.ib.hostname:
                return self.ib.get(hostname=board)
            else:
                valid_serials = ','.join("'%s'" % ib.serial for ib in self.ib)
                raise RuntimeError(f'{self!r}: Invalid Board serial number {board}. '
                                   f'Valid serial numbers are {valid_serials}')
        elif isinstance(board, (tuple, list)) and len(board) == 2:
            matches = [ib for ib in self.ib if tuple(board) == ib.get_id()]
            if not matches:
                raise RuntimeError(f"board ID {board} did not match any board "
                                   f"in the array: {[ib.get_id() for ib in self.ib]}")
            elif len(matches) > 1:
                raise RuntimeError(f"board ID {board} Matched multiple "
                                   f"boards: {','.join(ib.get_string_id() for ib in matches)}")
            else:
                return matches[0]
        else:
            raise AttributeError(f'{self!r}: Invalid Motherboard specification {board}')

    def get_iceboard_from_id(self, id):
        """ return the Motherboard corresponding to the specified (crate,slot) tuple. ``slot`` is zero-based.
        """
        return self.get_iceboard(id)

    def get_iceboards(self, board_ids=None, lane_type=None):
        """
        Return the Motherboard object(s) corresponding to the board ID tuples or
        dict specified in `ib` , including wildcards.

        Parameters:

            board_ids (list of tuple): List of (crate_number, slot_number)
                tuple describing the a Motherboard. A value of None is
                equivalent to a '*' wildcard. Missing tuple entries are
                considered to be None.

            lane_type (str): type of lane  that can be specified in the spec:
                ('channel', 'gpu', 'bp_pcb', 'bp_qsfp'). If 'None', lane
                numbers are not decoded nor returned.

        Returns:
            if `lane_type` is None: list of Motherboard objects
            otherwise: a dict of {iceboard_object:set_of_lanes, ...}

        Notes:

            A board id can be specified as follows:
                - ('*',) or ('*', '*') # All boards in the array
                - (0,) or (0, None) or (0, '*') # All boards in crate 0
                - {crate=0} or {crate=0, slot=None} or {crate=0, slot='*'} # The same
                - (0, 3, 2) # crate 0, slot 3, lane/channel 2
        """
        if board_ids is None:
            return self.ib

        iceboards = {}  # List of matching iceboards and corresponding lane/channels
        for board_id in board_ids:
            # First extract the crate/slot/lane from the identifier
            crate_number = None
            slot_number = None
            lane_number = None
            # If the spec is a tuple"
            if isinstance(board_id, (tuple, list)):
                crate_number = board_id[0] if len(board_id) >= 1 else None
                slot_number = board_id[1] if len(board_id) >= 2 else None
                lane_number = board_id[2] if len(board_id) >= 3 else None
            elif isinstance(board_id, dict):
                for k, v in board_id.items():
                    k = k.lower()
                    if k == 'crate':
                        crate_number = v
                    elif k == 'slot':
                        slot_number = v
                    elif k in ('lane', 'chan', 'channel'):
                        lane_number = v
                    else:
                        raise RuntimeError(f"Unknown element {k} in motherboard selection item {ib}")
            else:
                raise ValueError(f'Unknown Motherboard selection format {ib}')

            # Convert the wildcard '*' into None
            crate_number = None if crate_number == '*' else crate_number
            slot_number = None if slot_number == '*' else slot_number
            lane_number = None if lane_number == '*' else lane_number

            # print('get_iceboard: looking for ', crate_number, slot_number)
            for ib in self.ib:
                crate, slot = ib.get_id()
                # print('   checking', ib_id)
                if (crate_number is None or crate_number == crate) and (slot_number is None or slot_number == slot):
                    # get the Motherboard's lanes. Create an entry with an emply list if it does not exist
                    lanes = iceboards.setdefault(ib, set())
                    if lane_type is not None:
                        if lane_type in ('channel', 'chan'):
                            valid_lanes = ib.get_channels()
                        elif lane_type in ('gpu', 'mb_qsfp'):
                            valid_lanes = ib.GPU.get_lane_numbers()
                        elif lane_type == 'bp_pcb':
                            valid_lanes = ib.BP.get_lane_numbers('pcb')
                        elif lane_type == 'bp_qsfp':
                            valid_lanes = ib.BP.get_lane_numbers('qsfp')
                        else:
                            raise AttributeError('Unknown lane type %s' % lane_type)
                        if lane_number is None:
                            lanes.update(valid_lanes)
                        elif lane_number not in valid_lanes:
                            raise ValueError('Invalid lane number %s for lane type %s. Valid values are %s'
                                             % (lane_number, lane_type, valid_lanes))
                        else:
                            lanes.add(lane_number)
        if lane_type is None:
            return list(iceboards.keys())
        else:
            return iceboards


    async def load_gains_async(self, bank=0, gain_folder='/home/chime/ch_acq/gains'):
        """ Returns the gains from the gain files associated with every board of the array.

        If a gain file is not found for a specific board, the default gains
        found in the file 'default_gains.pkl' will be used for that board.

        If a gain file nor a default gains file are found, the gain data for
        that board will be `None` and a warning will be logged.

        Parameters:

            bank (int): Unused

            gain_folder (str): FOnder in which the gain files are located.

        Returns:

            The gain map in the format ``{board_id: gains}``, where
            ``board_id`` is a two-element tuple uniquely identifying the board
            (taken from the board's get_id()), and ``gains`` is a dict
            containing the digital gains to be applied to the channels on that
            board (from the board's load_gains(...))

        Note:

            This method does not set the digital gains in the FPGAs; it only
            loads them from the files. See `set_gains` to set the gains using
            the dict returned by this method.
        """

        # get the default gains, just in case we need them
        try:
            default_gains_filename = os.path.join(gain_folder, 'default_gains.pkl')  # filename of the default gains
            with open(default_gains_filename, 'rb') as f:
                default_gains = pickle.load(f)
        except IOError:
            default_gains = None

        array_gains = {}
        for ib in self.ib:
            board_id = ib.get_id()
            self.logger.debug(f'{self!r}: Reading digital gains for (crate,slot)={board_id}')
            board_gains = ib.load_gains(folder=gain_folder) or default_gains
            await asyncio.sleep(0)

            if not board_gains:
                self.logger.warning(f'{self!r}: Neither board-specific gain file not default '
                                    f'gain file was found for (crate,slot)={board_id}')
                array_gains[board_id] = None
            else:
                array_gains[board_id] = board_gains
        return (array_gains)

    async def save_gains_async(self, gains, gain_folder='/home/chime/ch_acq/gains'):
        """ Save the gains.

        Parameters:

            gains (): Gains in the format

                {(crate,slot,channel):(glin, glog), ...} or {(crate, slot):{channel:(glin,glog),...},...}

            gain_folder (str): Folder in which the gain files are located.

        Returns:

            The gain map in the format ``{board_id: gains}``, where
            ``board_id`` is a two-element tuple uniquely identifying the board
            (taken from the board's get_id()), and ``gains`` is a dict
            containing the digital gains to be applied to the channels on that
            board (from the board's load_gains(...))

        Note:

            This method does not set the digital gains in the FPGAs; it only
            loads them from the files. See `set_gains` to set the gains using
            the dict returned by this method.
        """

        # make sure we have a board-id-based gain table
        gains = self.group_gains_per_board_id(gains)
        for board_id, board_gains in gains.items():
            ib = self.get_iceboard(board_id)
            ib.save_gains(gains=board_gains, folder=gain_folder)

    async def get_gains_async(self, bank=0, use_cache=True):
        """ Return the digital gains programmed in the specified bank for all channels of all boards of the array.

        Parameters:

            `bank`: gain bank from which the gains are read.

            `use_cache`: True to allow the software-cached value to be used
                (much faster the reading back from the FPGAs)

        Returns:

            Digital gains, in a channel-indexed dict, in the format::

                {chan_id: (glin, glog), ...}

                where:
                     ``chan_id`` is a (crate, slot, channel) tuple that uniquely identifies a channel
                     ``crate`` = int or str or None
                     ``slot`` = int or str
                     ``glin` = array of 1024 (int16 + 1j* int16) linear gain components
                     ``glog`` = post-scaler factor (applies additional gain of 2**glog to all bins)

        """
        gains = {}
        for ib in self.ib:
            for ch, ch_gains in ib.get_gains(bank=bank, use_cache=use_cache):
                gains[ib.get_id(ch)] = ch_gains
            await asyncio.sleep(0)
        return (gains)

    async def get_gain_timestamps_async(self, bank=0):
        """
        Returns the timestamp at which the gains for each channel of thewas set.

        Parameters:

            bank (int): gain bank from which to get the gains timestamp.

        Returns:

            Timestamp of the digital gains, in a channel-indexed dict, in the format::

                {chan_id: timestamp, ...}

                where:
                     ``chan_id`` is a (crate, slot, channel) tuple that uniquely identifies a channel
                     ``crate`` = int or str or None
                     ``slot`` = int or str
                     ``timestamp`` = is a time.time() value. None if the gain was not set.
        """
        timestamps = {}
        for ib in self.ib:
            for ch, timestamp in ib.get_gain_timestamps(bank=bank):
                timestamps[ib.get_id(ch)] = timestamp
            await asyncio.sleep(0)
        return (timestamps)

    async def set_gains_async(self, gains, bank=-1, when='now', gain_timestamps=None):
        """ Set the on-board post-FFT digital gains for specified board/channel.

        Parameters:

            gains (dict): dictionary of gains specified as either:

                - ``{board_id: {chan: (glin, glog), ...}, ...}``
                - ``{chan_id: (glin, glog), ...}``

                where:

                - ``board_id`` is a (crate, slot) tuple that uniquely identifies a board

                - ``channel_id`` is a (crate, slot, channel) tuple that uniquely identifies a channel

                - ``crate`` int or str or None

                - ``slot`` int or str

                - ``glin`` array of 1024 ``(int16 + 1j* int16)`` linear gain components

                - ``glog`` post-scaler factor (applies additional gain of ``2**glog`` to all bins)

            bank (int): gain bank in which the gains are written. If `bank` =-1 or is None, gains are
                    written in the inactive bank (which can be activated later using set_gain_bank()).

            when (int, str or None): Specifies when the new gain table must be made active:

                    - If `when` is the string 'now' or a negative integer, the target gains
                      are made active immediately.

                    - If `when` is None, the gains are written in the specified
                      bank but the bank switching is not activated.

                    - If `when` is an integer, the gains will be activated starting on the target
                      timestamp specified by `when`.

            gain_timestamps (dict): dictionary of unix timestamps with same key format as `gains` or
                   single unix timestamp that is applied to all channels.

                   If not provided, then defaults to current time.

        """
        # make sure we have a board-id-based gain table
        gains = self.group_gains_per_board_id(gains)

        # format the timestamps dictionary identically to gains
        if isinstance(gain_timestamps, dict):
            gain_timestamps = self.group_gains_per_board_id(gain_timestamps)

        # set the gains for each board
        for board_id, g in gains.items():
            gain_timestamp = gain_timestamps[board_id] if isinstance(gain_timestamps, dict) else gain_timestamps
            ib = self.get_iceboard_from_id(board_id)
            self.logger.debug(f'{self!r}: Setting digital gains for (crate,slot)={board_id} ({ib.get_formatted_id()})')
            ib.set_gains(gain=g, bank=bank, when=None, gain_timestamp=gain_timestamp)
            await asyncio.sleep(0)

        if when is not None:
            await self.switch_gains_async(bank=bank, when=when)

    def group_gains_per_board_id(self, channel_based_gains):
        """ Convert a channel_id based gain table into a board_id-based gain table.

        Parameters:

            channel_based_bains (dict): dict containing channel-based gain
                entries in the format {channel_id: gains, ...}, where
                ``channel_id`` is a 3-element (crate, slot, channel)tuple

        Returns:
            A board_id-based gain table in the format {board_id: {channel:gains,...},...}.

        Note:
            - Entries that are not a 3-element tuples are left untouched,
              allowing a board-id based table or another dict to be passed.

        """
        board_based_gains = {}
        for id_, gains in channel_based_gains.items():
            if isinstance(id_, (tuple, list)) and len(id_) == 3:
                board_based_gains.setdefault(tuple(id_[:-1]), {})[id_[-1]] = gains
            else:
                board_based_gains[id_] = gains
        return board_based_gains

    async def compute_gains(
            self,
            enable=True,
            slots=None,
            noise_injection=None,
            gain_folder='/home/chime/ch_acq/gains'):
        """
        *** NOT FUNCTIONAL ***
        Should be updated to use the new GainCalc object

        Compute the gains of the SCALER module so that the conversion of the FFT output
        to (4+4) bit complex values stays within range for the current signal conditions.

        This method will have to be rewritten to use data obtained over REST-based raw data receivers.



        """

        # Make sure gain calculation is enabled
        if not enable:
            return

        # Try to import gain computation module
        try:
            from pychfpga import calculate_gains
        except ImportError:
            self.logger.error('Could not import calculate_gains. Missing timestream_receiver in path?')
            return

        # Setup noise injection using noise injection parameters that are specific to the gain calculation operation.
        if noise_injection is not None:
            for source_name, source_params in noise_injection.items():
                if source_params.board:
                    self.logger.debug(f"Setting gain computation noise injection for "
                                      f"source '{source_name}' with parameters {source_params}")
                    self.set_noise_injection(local_sync=True, **source_params)

        # Loop over boards and compute gains
        self.logger.info("Computing SCALER gains.")
        for ib in self.ib:
            ch_id = ib.get_id()
            # crate, slot_0based = ch_id[0], ch_id[1]
            if (slots is None) or slots[ch_id[0]][ch_id[1]]:
                await calculate_gains.calculate_gains_async(ib, gain_folder=gain_folder)

    def get_next_gain_bank(self):
        """
        Return the next gain bank number to be used (i.e. the currently unused
        bank number of channel 0 of the first board of the array).


        It is the responsability of the user to make sure that no gain switch will occur once this
        method is called. .

        Returns:
            The bank number of the currently inactive bank of the first channel of the first board of
            the array (not the inactive bank for every channel of every boards).


        """
        self.ib[0].get_next_gain_bank()[0]

    async def switch_gains_async(self, bank=-1, when='now'):
        self.ib.switch_gains(bank=bank, when=when)

    # def set_synchronized_gain_switching_mode(self, enable):
    #     """ Enable or disable synchronized gain switching for all boards of the array. """
    #     self.ib.set_synchronized_gain_switching(enable=enable)

    # def set_next_gain_bank(self, bank):
    #     """ Sets the next gain bank to use on all channelizers of the array.

    #     The switch will be done imeediately or not, depending on the gain switching mode (see
    #     set_synchronized_gain_switching_mode())
    #     """
    #     self.ib.set_next_gain_bank(bank=bank)

    # def set_gain_switch_frame_number(self, frame):
    #     """
    #     Sets the frame number at which the new gain bank (set by set_next_gain_bank()) will be used.

    #     Is used only if synchronized gain switching mode is enabled(see
    #     set_synchronized_gain_switching_mode())

    #     """
    #     self.ib.set_gain_switch_frame_number(frame=frame)

    async def _async(self, delay=0.1):
        self.ib.set_corr_reset(1)
        await asyncio.sleep(0.1)
        self.ib.set_corr_reset(0)

    async def reset_gpu_links_async(self, board_ids=None):
        """ Reset the GPU links for the boards specified in `board_ids`

        Parameters:

            board_ids (list of tuple/dict): List of board descriptors in the
                form of (crate_number) or (crate_number, slot_number) tuples
                or {crate:crate_number} / {crate:crate_number,
                slot:slot_number} dicts. Crate and slot number can be  '*' or
                Null to match every instance.

        Returns:

            List of board_ids that were actually resetted.

        Notes:

            - It is not possible to selectively reset just one QSFP or a
              specific lane of a QSFP. All lanes for both QSFPs are reset.

        """
        ibs = self.get_iceboards(board_ids)
        for ib in ibs:
            ib.reset_gpu_links()
            await asyncio.sleep(0)
        return ([ib.get_id() for ib in ibs])

    async def get_fpga_config_async(self, basic=False):
        """ Concurrently gets the configuration info for each FPGA """
        configs = await asyncio.gather(*[ib.get_config_async(basic=basic) for ib in self.ib])
        return dict(zip(self.ib.get_id(), configs))

    def get_frequency_map(self, format='l:cscb'):
        """
        Returns a map describing the content (crate, slot, channel, bin) of
        every packet at the output of the corner turn engine.

        This map is obtained by passing the channelizer identity map through
        the shuffle map.
        """

        return self.get_shuffle_output(self.get_chan_identity_map(format=format))

    def get_chan_identity_map(self, format='l:cscb'):
        """
        Return an identity map that describes the origin of each of the 1024
        samples contained in the channelizer output packets.


        Returns:

            The map as a dict:

                {(crate, slot, channel): [sample_id0, ... sample_id1023]}

            where:

                (crate, slot, lane) tuple is the key from get_channel_ids(),
                   which derives its (crate, slot) from ib.get_id(). ``crate`
                   and ``slot`` could therefore be non-numeric if there is no
                   backplane or if a crate number is not assigned to a crate.

                ``sample_id`` describes the channelizer output samples. It varies depending on `format`:

                    "l:cscb": sample_id[bin_number] =  (crate, slot, channel, bin_number), where
                       ``crate``, ``slot``, ``channel`` are the same as in the
                       key, and can be non-numeric. ``bin_number`` is an integer from 0 to 1023.

                    "l:cc": sample_id[:] = global_chan_number, which is an integer representing the global crate, slot
                        and channel. It is the same for all bins. crate, slot
                        and channel must be numeric (there must be a crate and
                        crate number).

                    "l:cb": sample_id[bin_number] = (global_chan_number, bin_number).
                        See abive for ``global_chan_number``. ``bin_number`` is an integer from 0 to 1023. This tuple
                        uniquely represent every sample of every output in the
                        array.

                    "l:cb": sample_id[bin_number] = bin_number.
                        ``bin_number`` is an integer from 0 to 1023. This list is the same for every channel.


        This map can be propagated through the shuffle map (see
        `apply_shuffle_map` method) to obtain the contents of the output of
        the corner-turn engine.
        """
        ch_out = {}
        for ib in self.ib:
            for (crate, slot, lane) in ib.get_channel_ids():

                if format == 'l:cscb':  # Unique (crate, slot, local_channel)
                    ch_out[(crate, slot, lane)] = [(crate, slot, lane, bin) for bin in range(1024)]
                elif format == 'l:cc':  # Non-unique global channel numbers (repeated for each bin)
                    if not all(isinstance(i, int) for i in (crate, slot, lane)):
                        raise RuntimeError('get_channel_identity_map requires numeric crate and slot numbers')
                    ch_out[(crate, slot, lane)] = [crate * 256 + slot * 16 + lane for bin in range(1024)]
                elif format == 'l:cb':  # Unique (global channel, lane) tuple
                    if not all(isinstance(i, int) for i in (crate, slot, lane)):
                        raise RuntimeError('get_channel_identity_map requires numeric crate and slot numbers')
                    ch_out[(crate, slot, lane)] = [(crate * 256 + slot * 16 + lane, bin) for bin in range(1024)]
                elif format == 'l:bb':  # Non unique bin_number (repeated for each channel)
                    ch_out[(crate, slot, lane)] = [bin for bin in range(1024)]
        return ch_out

    async def get_funcgen_buffer_async(self):
        """ Return the waveform produced by the function generator.

        This will correspond to the function generator output if it is
        configured to send its buffer, and will correspond to the channelizer
        output if the FFT and SCALER are bypassed.
        """
        ch_out = {}
        for ic in self.ic:
            for ib in ic.slot.values():
                ch_out.update(ib.get_funcgen_buffer())
                await asyncio.sleep(0)
        return ch_out

    def get_shuffle_output(self, chan_map):
        """
        Takes the channelizer data map `chan_data` and propagates it through the
        shuffle stages as they are currently configured in the FPGA, and return the resulting data.

        ``chan_data`` is a dictionary of the format {(crate_number, slot, channel_number): [1024 elements],...}

        The elements describing the channel contents can by of any type (complex number, channel & bin tuple, etc.)

        """

        # Get first crossbar map
        # Channels are converted to (crate_number, slot, input_number)
        # Output lane id is converted to (crate_number, slot, lane)

        cb1_out = {}
        for ib in self.ib:
            # extract channels for this motherboard only
            cb1_in = {ch: chan_map[(crate, slot, ch)] for (crate, slot, ch) in ib.get_channel_ids()}
            for lane, data in ib.CROSSBAR.map(cb1_in).items():
                cb1_out[ib.get_id(lane)] = data

        # Apply pcb shuffling
        pcb_shuffle_out = {}
        for ic in self.ic:
            pcb_link_map = ic.get_pcb_link_map()
            for (rx_slot, rx_lane), (tx_slot, tx_lane) in pcb_link_map.items():
                (crate, ) = ic.get_id()
                pcb_shuffle_out[(crate, rx_slot - 1, rx_lane)] = (
                    cb1_out[(crate, tx_slot - 1, tx_lane)] if (crate, tx_slot - 1, tx_lane) in cb1_out
                    else dict(data=[None] * 1024))

        # Apply CROSSBAR2
        cb2_out = {}
        for ib in self.ib:
            (crate, slot) = ib.get_id()
            # extract channels for this motherboard only
            cb2_in = {lane: pcb_shuffle_out[(crate, slot, lane)]
                      for lane in range(ib.BP_SHUFFLE.NUMBER_OF_PCB_LANES)}
            for lane, data in ib.CROSSBAR2.map(cb2_in).items():
                cb2_out[(crate, slot, lane)] = data

        # Apply QSFP shuffling
        qsfp_shuffle_out = {}
        for ib in self.ib:
            (crate, slot) = ib.get_id()
            bypass = ib.BP_SHUFFLE.BYPASS_QSFP_SHUFFLE
            number_of_qsfp_lanes = ib.BP_SHUFFLE.NUMBER_OF_QSFP_LANES
            for rx_lane in range(number_of_qsfp_lanes):
                if bypass:
                    qsfp_shuffle_out[(crate, slot, rx_lane)] = cb2_out[(crate, slot, rx_lane)]
                else:
                    crate_offset = rx_lane * 2 // number_of_qsfp_lanes
                    qsfp_shuffle_out[(crate, slot, rx_lane)] = cb2_out.get(
                        (crate ^ crate_offset, slot, rx_lane), dict(data=[None] * 2048))

        # Apply CROSSBAR3
        cb3_out = {}
        for ib in self.ib:
            (crate, slot) = ib.get_id()
            # extract channels for this motherboard only
            cb_in = {lane: qsfp_shuffle_out[(crate, slot, lane)]
                     for lane in range(ib.BP_SHUFFLE.NUMBER_OF_QSFP_LANES)}
            for lane, data in ib.CROSSBAR3.map(cb_in).items():
                cb3_out[(crate, slot, lane)] = data

        return cb3_out

    # def get_map(self):
    #     cb1_map = self.CROSSBAR.get_map()
    #     shuffle_map
    #     cb2_map = self.CROSSBAR2.get_map()
    #     return cb1_map

    def test_sync(self):
        c = list(self.ib)
        sync_board = self.sync_master

        sync_ctr = np.zeros(len(c), dtype=int)
        for i, bb in enumerate(c):
            bb.REFCLK.set_sync_source('bp')  # bb.REFCLK.SLAVE=1
            sync_ctr[i] = bb.REFCLK.SYNC_CTR

        fail = 0
        for test_number in range(10):
            print(f'Trial # {test_number + 1}: Sending SYNC pulse from Slot {sync_board.slot:02i} (Motherboard SN{sync_board.serial})')
            sync_board.REFCLK.local_sync()
            for i, bb in enumerate(c):
                new_sync_ctr = bb.REFCLK.SYNC_CTR
                diff = (new_sync_ctr - sync_ctr[i]) & 0xF
                sync_ctr[i] = bb.REFCLK.SYNC_CTR
                fail += bool(diff != 1)
                print(f'    Slot {bb.slot:02d} (Motherboard SN{bb.serial}): Sync counter = {new_sync_ctr:2d}, diff = {diff:2d} => {("FAILED!", "PASS")[diff == 1]}')
            time.sleep(0.2)
        if fail:
            print('SYNC Test has FAILED!')
        else:
            print('SYNC Test has PASSED!')

    # def soft_sync(self, sync_board):
    #     """ Synchronize all boards"""
    #     boards = list(self.ib)
    #     print('Masking ADC data before sync')
    #     for ib in boards:
    #         for ant in ib.ANT:
    #             ant.ADCDAQ.BYTE_MASK = 0

    #     print('Initiating global sync')
    #     sync_board.REFCLK.local_sync()

    #     print('Unmasking ADC data')
    #     for ib in boards:
    #         for ant in ib.ANT:
    #             ant.ADCDAQ.BYTE_MASK = 255

    # def time_soft_sync(self, sync_board, delay):
    #     """ Synchronize all boards"""

    #     boards = list(self.ib)
    #     print 'Masking ADC data before sync'
    #     for ib in boards:
    #         for ant in ib.ANT:
    #             ant.ADCDAQ.BYTE_MASK = 0

    #     # Get current time
    #     current_time = sync_board.get_irigb_time()
    #     print 'Setting IRIG-B sync after %d seconds' %delay
    #     # Send sync pulse delay seconds in the future
    #     sync_board.set_irigb_trigger_time_sync(current_time, delay)

    #     print 'Unmasking ADC data'
    #     for ib in boards:
    #         for ant in ib.ANT:
    #             ant.ADCDAQ.BYTE_MASK = 255

    # def irigb_sync(self, delay):
    #     """ Synchronize all boards"""

    #     # Get current time
    #     current_time = self.ib[0].get_irigb_time()
    #     print 'Setting IRIG-B sync after %d seconds' %delay
    #     for ib in self.ib:
    #         ib.set_irigb_trigger_time_sync(current_time, delay)

    def print_temperatures(self):
        t = [(b.slot, b.serial, b.SYSMON.temperature()) for b in self.ib]
        t.sort()
        for (slot, serial_number, fpga_temp) in t:
            print('Slot %2i (SN%s): FPGA %2.1f C' % (slot, serial_number, fpga_temp))

    def set_adc_mask(self, value):
        for ib in self.ib:
            for ant in ib.ANT:
                ant.ADCDAQ.BYTE_MASK = value

    def print_fmc_power(self):
        sensor_list = ['FMCA_12V0', 'FMCA_3V3', 'FMCA_VADJ',
                       'FMCB_12V0', 'FMCB_3V3', 'FMCB_VADJ']

        for b in self.ib:
            for sensor in sensor_list:
                (voltage, current, power) = b.hw.get_power(sensor)[sensor]
                if voltage is not None:
                    print('%0.1fV@%0.2fA=%0.1fW ' % (voltage, current, power), end=' ')
                else:
                    print('None                 ', end=' ')
            print()

    def detect_backplane_links(self, tx_power=7, print_=True):
        """ Setup all boards on the array to send test pattern over the
        backplane link and detect  from which slot/lane every board is
        receiving data.

        The test is performed only on IceBoards that are installed in crates
        and whose FPGA has been programmed and initialized. Other boards are
        ignored.

        Parameters:

            tx_power (int):

            print_ (bool):


        For now, this test works only if all boards are in a single crate.
        """
        # select only boards on crates and that are open
        array = Ccoll(ib for ib in self.ib if ib.is_open() and ib.crate and ib.slot)

        if not array:
            raise RuntimeError('There are no boards that are opened() AND connected in a crate')

        # Check if the board are all on a single crate
        if len(set(array.crate)) != 1:
            raise RuntimeError('Sorry, this method currently can work on only one crate. '
                               'The currently active boards span multiple crates %s'
                               % list(set(array.crate)))

        active_slots = [ib.slot for ib in array]
        if len(set(active_slots)) != len(active_slots):
            raise SystemError('Slot numbers are not unique!')

        print('Setting Transmitted ID')
        for ib in array:
            ib.BP_SHUFFLE.TX_DATA_MSB = 0xFF00 + ib.slot
            for gtx_number, g in enumerate(ib.BP_SHUFFLE.gtx):
                g.SOURCE_SEL = 1  # 0:Send TXDATA , 1: SEND 10G Ethernet test packet
                g.LOOPBACK = 0
                g.TXPRBSSEL = 0
                g.RXPRBSSEL = 0
                g.TXDIFFCTRL = tx_power
                g.TX_DATA_LSB = gtx_number + 1
                g.TXHEADER = 1
                g.CAPTURE_ENABLE = 1
                g.TXPRECURSOR = 0b00000  # DFE cannot compensate pre-cursor
                g.TXPOSTCURSOR = 0b00000
                g.RXLPMEN = 0  # Go to DFE mode instead of LPM
                # g.RXMONITORSEL = 1 # 1=AGC, 2=UL, 3=VP loop
                # g.RX_DEBUG_CFG = 0b1011 << 2
                # g.DMONITOR_CFG1 = 0
                # g.DMONITOR_CFG0 = (0b1<<15) | (0x0080 <<1) | 1
                # Configure DMONITOR to read the AGC gain
                # g.DMONITOR_SELECT = 1
                # g.PCS_RSVD_ATTR_BIT6 = 1
                # old=g.read_drp(0x6f)
                # g.write_drp(0x6f,old | 1<<6)
                # g.RXDFEOVRD=1
                # g.write_drp(0x1d, 0x00ea)

        print('Resetting the GTXes')
        for ib in array:
            for g in ib.BP_SHUFFLE.gtx:
                g.RXDFELPMRESET = 1
                g.RXDFELPMRESET = 0
            time.sleep(0.1)

        print('Checking received data')
        link_list = []
        link_matrix = [[None] * 17 for x in range(17)]
        serial_number = ['N/A'] * 17
        for ib in array:
            # print 'Slot %i' % (ib.slot)
            dest_slot = ib.slot
            serial_number[dest_slot - 1] = ib.serial
            # JM: Fixed this below bc was getting an error. JF please check
            for gtx_number, g in enumerate(ib.BP_SHUFFLE.gtx[0:ib.BP_SHUFFLE.NUMBER_OF_PCB_LINKS]):
                dest_lane = gtx_number + 1
                dest = (dest_slot, dest_lane)
                expected_source = ib.crate.get_matching_tx(dest)
                source_present = expected_source[0] in active_slots
                for trial in range(3):
                    rxdata = g.get_rxdata()
                    # print '   Slot %i Lane %i received %08X' % (    ib.slot, lane+1,  rxdata)
                    source_slot = int((rxdata >> 8) & 0xFF)
                    source_lane = int((rxdata) & 0xFF)
                    source = (source_slot, source_lane)
                    source_valid = (rxdata >> 16) == 0xFFFF
                    maybe = (rxdata != 0x55555555) and (rxdata != 0xAAAAAAAA)
                    if source_valid:
                        break

                if source_valid:
                    print('Slot %2i Lane %2i is receiving data from Slot %2i Lane %2i '
                          '(received word = 0x%08X, RXMONITOROUT= %x, DMONITOROUT=%x)'
                          % (dest_slot, dest_lane, source_slot, source_lane, rxdata, g.RXMONITOR, g.DMONITOROUT))
                    link_matrix[dest_slot][dest_lane] = 'S%02iL%02i' % (source_slot, source_lane)
                elif maybe:
                    print('Slot %2i Lane %2i is receiving some data but cannot determine source '
                          '(received word = 0x%08X, RXMONITOROUT= %x, DMONITOROUT=%x)'
                          % (dest_slot, dest_lane, rxdata, g.RXMONITOR, g.DMONITOROUT))
                    link_matrix[dest_slot][dest_lane] = 'S??L??'
                else:
                    link_matrix[dest_slot][dest_lane] = '  ()  '

                if not source_valid or not source_present or source != expected_source:
                    if source_present:
                        link_matrix[dest_slot][dest_lane] += '/S%sL%s  ' % expected_source
                    else:
                        link_matrix[dest_slot][dest_lane] += '/(S%sL%s)' % expected_source

                link_list.append((source, dest))
        if print_:
            # Print a slot map
            print('Slot-> ' + ' '.join(['%-15i' % (slot + 1) for slot in range(16)]))
            print('S/N -> ' + ' '.join(['%-15s' % (sn) for sn in serial_number]))
            print('Lane   ' + ' '.join(['%-15s' % '----------' for x in range(16)]))

            for dest_lane in range(1, 16):
                print('%6i ' % (dest_lane), end=' ')
                for dest_slot in range(1, 17):
                    print('%-15s' % link_matrix[dest_slot][dest_lane], end=' ')
                print()

        return link_list

    def get_backplane_pcb_link_map(self):
        """ Return a dictionary that lists all the backplane PCB links and
        their corresponding GTXes for every boards in the array.

        The list covers every transmitter and receivers on the borads that are
        currently in the array. Each GTX therefore has two entries, one in
        which it is the transmitter, and one in which it is the reciever. The
        connectivity is resolved by relying on the connectivity information
        that is probiced by the IceCrate object, so there is no need for
        additional resolving.

        Parameters:

            None

        Returns:
            A dict, in the format:

            {('pcb', (tx_crate, tx_slot, tx_lane), (rx_crate, rx_slot, rx_lane)): (tx_gtx_instance, rx_gtx_instance)}
        """
        link_map = {}
        for ib in self.ib:
            link_map.update(ib.BP_SHUFFLE.get_link_map('pcb'))
        return link_map

    def get_backplane_qsfp_links(self):
        """ Get the list of backplane QSFP links and resolve their
        connectivity to return a list of lane connectivity.

        We achieve this by getting the list that matches the QSFP-connected
        GTXes to a cable link ID by calling each crate's get_qsfp_links(),
        which returns a list in the format:

            [('BP_QSFP', (crate_id, iceboard_slot, gtx_index), None, cable_link_id), ...]


        Returns:

            A resolved, motherboard- and lane-oriented connectivity list in the format:

                [('BP_QSFP', (tx_crate, tx_iceboard_slot, tx_lane), (rx_crate, rx_iceboard_slot, rx_lane) ), ...]

            The list refers to logical lane numbers (including the internal
            bypass lanes). Note that the internal (bypass) links are not added
            to the result.


        """
        # Combine TX and RX link dicts from all crates
        raw_links = [link for ic in self.ic
                     for qsfp_link_list in ic.get_qsfp_links()
                     for link in qsfp_link_list]

        for ic in self.ic:
            raw_links += ic.get_qsfp_links()

        # Make a map of crates objects indexed by crate_number
        crate_map = {crate.get_id()[0]: crate for crate in self.ic}

        # Visit each link and find the attached nodes
        links = []
        for (link_type, tx_id, rx_id, link_id) in raw_links:
            # If the second node is not already known, search all the links
            # for a corresponding half-link with the same link_id
            if rx_id is None:
                matching_nodes = [nid1 for (lt, nid1, nid2, lid) in raw_links
                                  if lt == link_type and nid1 != tx_id and nid2 is None and lid == link_id]
                if len(matching_nodes) == 1:
                    rx_id = matching_nodes[0]
            if tx_id is None or rx_id is None:
                continue
            (source_crate, source_slot, source_lane) = tx_id
            (dest_crate, dest_slot, dest_lane) = rx_id
            ic0 = crate_map[source_crate]
            ic1 = crate_map[dest_crate]
            if (source_slot not in ic0.slot) or (dest_slot not in ic1.slot):
                continue
            bp0 = ic0.slot[source_slot].BP_SHUFFLE
            bp1 = ic1.slot[dest_slot].BP_SHUFFLE
            # Append entry. We convert from gtx index to logical lane #
            links.append((
                link_type,
                (source_crate, source_slot, source_lane + bp0.NUMBER_OF_QSFP_DIRECT_LANES),
                (dest_crate, dest_slot, dest_lane + bp1.NUMBER_OF_QSFP_DIRECT_LANES)))

        return links

    def get_backplane_qsfp_link_map(self, resolve=True):
        """ Return a dictionary that maps the backplane QSFP links to
        corresponding GTX transmitter and receiver instances.

        Returns:
           A dict, in the format:

            {('qsfp', (tx_crate, tx_slot, tx_lane), (rx_crate, rx_slot, rx_lane)): (tx_gtx_instance, rx_gtx_instance)}

        """
        qsfp_link_map = {}
        for ib in self.ib:
            qsfp_link_map.update(ib.BP_SHUFFLE.get_link_map('qsfp'))

        if not resolve:
            return qsfp_link_map

        # Ask each board a map that describe how each logical link is connected to the backplane links
        bp_to_logical_link_map = {}
        for ib in self.ib:
            bp_to_logical_link_map.update(ib.BP_SHUFFLE.get_bp_to_logical_link_map('qsfp'))

        # Ask each crate the map that matches backplane links to cable ids
        # (this takes time: we need to read the cable identification)
        bp_to_cable_map = {}
        for ic in self.ic:
            bp_to_cable_map.update(ic.get_qsfp_cable_map())

        # Create a map that matches each cable id to a list of corresponding
        # logical link ids (there should be 2 for each link)
        cable_to_link_map = {}  # {cable_id: (crate, slot, logical_lane)}
        for (bp_id, cable_id) in bp_to_cable_map.items():
            cable_to_link_map.setdefault(cable_id, []).append(bp_to_logical_link_map[('qsfp', bp_id)])

        # Crate a map that matches each logical link id with another logical link id.
        link_map = {}
        for cable_id, link_ids in cable_to_link_map.items():
            if len(link_ids) == 1:
                self.logger.warning('Only one end of a QSFP cable is connected; '
                                    'Cable ID %s connects only to %s. The link will be ignored.'
                                    % (cable_id, link_ids[0]))
            elif len(link_ids) > 2:
                raise RuntimeError('A QSFP cable connects to more than 2 links. '
                                   'Something is wrong. Cable ID %s connects only to %s.'
                                   % (cable_id, link_ids))
            elif len(link_ids) == 2:
                link_map[link_ids[0][1]] = link_ids[1][1]
                link_map[link_ids[1][1]] = link_ids[0][1]
        print(link_map)

        # Resolve each unresolved link.
        resolved_qsfp_link_map = {}
        for (link_type, tx_id, rx_id), (tx_gtx, rx_gtx) in qsfp_link_map.items():
            if not tx_id and rx_id and rx_id in link_map:
                tx_id = link_map[rx_id]
                tx_gtx, _ = qsfp_link_map[('qsfp', tx_id, None)]
            elif not rx_id and tx_id and tx_id in link_map:
                rx_id = link_map[tx_id]
                _, rx_gtx = qsfp_link_map[('qsfp', None, rx_id)]
            resolved_qsfp_link_map[link_type, tx_id, rx_id] = (tx_gtx, rx_gtx)

        return resolved_qsfp_link_map

    def get_gpu_link_map(self):
        """
        Return a dictionary that matches the GPU links to the GTX Rx and Tx
        instances when connected with a loopback cable.

        Returns:
           A dict, in the format:

            {('GPU', (tx_crate, tx_slot, tx_lane), (rx_crate, rx_slot, rx_lane)): (tx_gtx_instance, rx_gtx_instance)}
        """
        link_map = {}
        for ib in self.ib:
            crate_id = ib.get_crate_id()
            slot = ib.slot
            for tx_lane, gtx in enumerate(ib.GPU.gtx):
                rx_lane = (tx_lane + 4) % 8
                link = ('GPU', (crate_id, slot, tx_lane), (crate_id, slot, rx_lane))
                tx = ib.GPU.gtx[tx_lane]
                rx = ib.GPU.gtx[rx_lane]
                link_map[link] = (tx, rx)
        return link_map

    def get_link_map(self, links=None, gtx_only=False):
        """
        Return a dictionary that describes the corner-turn and loopbacked GPU
        links and the corresponding Tx and RX GTXes.


        Is used by `get_ber()` to identify the links on which Bit Error Rate (BER) tests will be performed.

        Parameters:

            links (str or list of str): select only the link type specified as
                a string or the link types specified in a list of strings.

        Returns:

            A dict, in the format::

                {(link_type, (tx_crate, tx_slot, tx_lane), (rx_crate, rx_slot, rx_lane)): (tx_gtx_instance, rx_gtx_instance)}

            `link_type` is either "BP", "BP_QSFP" or "GPU"
        """
        link_map = {}
        link_map.update(self.get_backplane_pcb_link_map())
        link_map.update(self.get_backplane_qsfp_link_map())
        link_map.update(self.get_gpu_link_map())  # loopback links

        if isinstance(links, str):
            link_map = {link: gtxes for link, gtxes in link_map.items() if link[0] == links}
        elif links is not None:
            link_map = {link: gtxes for link, gtxes in link_map.items() if link in links}

        # Remove links that do not exist or have no GTX (direct lanes)
        if gtx_only:
            link_map = {link: gtxes for link, gtxes in link_map.items() if (('int' not in gtxes) and (None not in gtxes))}

        # link_list.sort(key=lambda (lt, (sc, ss, sl), (dc, ds, dl)): ss * 16 + ds)
        return link_map


    async def get_ber(self, link_list=None, period=0.1, tx_power=12, print_=True, tx_init_power=7, tx_qsfp_power=15, minerrors_toprint=0):

        links = self.get_link_map(link_list, gtx_only=True)

        for link, (source_gtx, dest_gtx) in links.items():
            # First, make sure we can get errors by setting the wrong RX PRBS Sequence
            if source_gtx is None or dest_gtx is None or source_gtx == 'int' or dest_gtx == 'int':
                continue

            source_gtx.TXPRBSSEL = 4
            dest_gtx.RXPRBSCNTRESET = 1
            dest_gtx.RXPRBSSEL = 3
            dest_gtx.RXPRBSCNTRESET = 0
            t0 = time.time()
            while True:
                if dest_gtx.ERR_CTR:
                    break
                if time.time() - t0 > 1:
                    raise SystemError('Cannot detect errors even with the wrong sequence! '
                                      'Are the links connected as expected?')

        # Perform BER test on a single list, to be run in parallel below
        async def one_link_ber_async(link):
            (link_type, (sc, ss, sl), (dc, ds, dl)), (source_gtx, dest_gtx) = link

            dest_gtx.RXPRBSSEL = 4
            source_gtx.TXPRBSSEL = 4
            source_gtx.TXDIFFCTRL = tx_init_power  # Setting initial power level

            dest_gtx.RXDFELPMRESET = 1  # Retting the Reciever DFE
            await asyncio.sleep(0.5)
            dest_gtx.RXDFELPMRESET = 0

            if (link_type == 'pcb'):
                source_gtx.TXDIFFCTRL = tx_power
            else:
                source_gtx.TXDIFFCTRL = tx_qsfp_power

            # if print_:
            #    print 'Measuring BER for link %s' % (link[0],),
            #    print source_gtx.TXDIFFCTRL
            dest_gtx.RXPRBSCNTRESET = 1  # Resetting errors
            await asyncio.sleep(0.5)
            dest_gtx.RXPRBSCNTRESET = 0

            await asyncio.sleep(period)

            cnt = dest_gtx.ERR_CTR
            err = (float(cnt) * 16) / (period * 10e9)
            err_max = (float(cnt) * 16 + 1) / (period * 10e9)

            # source_gtx.TXPRBSSEL = 0  #Setting PRBS to 0 (data path)
            # dest_gtx.TXPRBSSEL = 0  #Setting PRBS to 0 (data path)
            if print_ and (cnt > minerrors_toprint):
                print(f'{link[0]!r} BER = {err:1.1e} ({cnt} errors, BER<{err_max:1.1e})')
            self.print_flush()
            return err

        # Run BER test on each link in parallel
        ber_table = await asyncio.gather(*[one_link_ber_async((link, gtxes)) for link, gtxes in links.items()])
        return dict(zip(links.keys(), ber_table))

    def get_ber_vs_power(self, links, max_power, period=0.1):

        # links = self.get_link_map(links, gtx_only=True)
        # links = self.scan_links(array, tx_power = max_power)

        power = list(range(0, max_power + 1))
        data = {}
        for tx_power in power:
            e = self.get_ber(links, period=period, tx_power=tx_power)
            for (link, ber) in e.items():
                if link in data:
                    data[link][0].append(tx_power)
                    data[link][1].append(ber)
                else:
                    data[link] = [[tx_power], [ber]]
        return data

    def plot_ber_vs_power(self, data=None, period=0.1, **kwargs):

        if isinstance(data, str):
            data = self.get_ber_vs_power(links=data, period=period, **kwargs)

        for link, (tx_power, ber) in data.items():
            print('%20s %s' % (link, ','.join(['%6.1g' % b for b in ber])))
        plt.figure(1)
        plt.clf()

        for link, (tx_power, ber) in sorted(data.items(), key=str):
            plt.semilogy(tx_power, (np.array(ber) + 1e-12), label='Link %s' % (link,))
        plt.title('BER of links as a function of TX power (period=%0.1f s)' % period)
        plt.xlabel('TX power (0-15)')
        plt.ylabel('BER')
        leg = plt.legend(loc='best', fontsize='small', markerscale=3, framealpha=0.6, shadow=True)
        plt.setp(leg.get_lines(), linewidth=2)  # make legend lines thicker so we can see the color better
        plt.grid(1)

    def get_eye_matrix(self, h_step=10, v_step=40):
        link_map = self.detect_backplane_links()
        eye_matrix = {}

        for link in link_map:
            ((from_slot, from_lane), (to_slot, to_lane)) = link
            print("###### running from slot %i lane %i to slot %i lane %i #######"
                  % (from_slot, from_lane, to_slot, to_lane))
            gtx = self.ib.get(slot=to_slot).BP_SHUFFLE.gtx[to_lane - 1]
            e = gtx.get_eye_diagram(list(range(-32, 32, h_step)), list(range(-127, 128, v_step)))
            eye_matrix[link] = e
        return eye_matrix

    @staticmethod
    def plot_eye_matrix(eye_matrix):
        plt.figure(1)
        plt.clf()

        source_slots = [ss for ((ss, sl), (ds, dl)) in list(eye_matrix.keys())]
        dest_slots = [ds for ((ss, sl), (ds, dl)) in list(eye_matrix.keys())]
        slots = sorted(set(source_slots + dest_slots))

        # slot_map = {slot: ix for (ix, slot) in enumerate(slots)}
        slot_map = {x + 1: x for x in range(16)}

        (fig, ax) = plt.subplots(
            len(slot_map), len(slot_map),
            sharex=True, sharey=True, subplot_kw={'axis_bgcolor': 'black'})
        fig.subplots_adjust(wspace=0, hspace=0)
        fig.suptitle('Backplane 10Gbps mesh eye diagrams\n TX slot #: left, Rx slot #: bottom')

        for (slot, ix) in slot_map.items():
            # Bottom images
            a = ax[ix, 0]
            a.tick_params(labelsize=6)
            a.set_ylabel("TX S%02i" % (slot), fontsize=10)
            # Left images
            a = ax[len(slot_map) - 1, ix]
            a.tick_params(labelsize=6)
            a.set_xlabel("RX S%02i" % (slot))
            plt.setp(a.xaxis.get_majorticklabels(), rotation=70)
            # Diagonal images
            a = ax[ix, ix]
            a.patch.set_color('black')

        for (((ss, sl), (ds, dl)), eye) in eye_matrix.items():
            a = ax[slot_map[ss], slot_map[ds]]
            a.imshow(
                np.log10(eye.ber_map + 1e-12),
                origin='lower', extent=(-32, 32, -128, 128), aspect='auto', vmin=-12, vmax=1)
        plt.draw()

    def print_crossbar2_frame_info(self, reset_stats=False, grid=True, width=180):
        slots = Ccoll(self.ib, self.ib.slot)  # Get iceboards indexed by slot number

        crate = set(slots.crate)
        if len(crate) == 1:
            crate = crate.pop()
        else:
            raise RuntimeError('Sorry, this method currently can work on one and only one crate. '
                               'The currently active boards either have no crates or span multiple crates %s'
                               % list(set(slots.crate)))

        slot_range = list(range(1, 17))
        lane_range = list(range(16))
        slot_labels = [slots[s].serial if s in list(slots.keys()) else 'N/A' for s in slot_range]

        captured_source = {}
        computed_source = {}
        valid_source = {}
        info = {}
        expected_bp_frame_size = 37
        for dest_slot in slot_range:
            if dest_slot in slots.keys():
                cb = slots[dest_slot].CROSSBAR2
                bp = slots[dest_slot].BP_SHUFFLE
                bin_sel = cb.BIN_SEL[0]
                shuffle_to_bin_sel_lane_map = cb.get_reverse_lane_map()
                remapped_direct_lane = shuffle_to_bin_sel_lane_map[0]
                if reset_stats:
                    bp.reset_stats()
                stream_ids = bin_sel.capture_stream_id()
                frame_number = bin_sel.capture_frame_number()
                frame_number = [f - frame_number[remapped_direct_lane] for f in frame_number]
                captured_source = {remapped_dest_lane: (((s >> 4) & 0xF) + 1, (s & 0xF))
                                   for remapped_dest_lane, s in enumerate(stream_ids)}
                computed_source = {shuffle_to_bin_sel_lane_map[dest_lane]: crate.get_matching_tx((dest_slot, dest_lane))
                                   for dest_lane in lane_range}
                valid_source = {shuffle_to_bin_sel_lane_map[dest_lane]: v
                                for dest_lane, v in enumerate(cb.get_lane_monitor('INPUT_DETECT'))}
                missing_frame = {shuffle_to_bin_sel_lane_map[dest_lane]: v
                                 for dest_lane, v in enumerate(cb.get_lane_monitor('MISSING_FRAME'))}
                align_fifo_overflow = {shuffle_to_bin_sel_lane_map[dest_lane]: v
                                       for dest_lane, v in enumerate(cb.get_lane_monitor('ALIGN_FIFO_OVERFLOW'))}
                rx_errors = {shuffle_to_bin_sel_lane_map[dest_lane]: v
                             for dest_lane, v in enumerate(bp.get_rx_lane_monitor('ERROR_CTR'))}
                rx_fifo_overflow = {shuffle_to_bin_sel_lane_map[dest_lane]: v
                                    for dest_lane, v in enumerate(bp.get_rx_lane_monitor('FIFO_OVERFLOW'))}
                rx_max_frame_length = {shuffle_to_bin_sel_lane_map[dest_lane]: v
                                       for dest_lane, v in enumerate(bp.get_rx_lane_monitor('MAX_FRAME_LENGTH'))}

            else:
                captured_source = {dest_lane: None for dest_lane in lane_range}
                computed_source = {dest_lane: crate.get_matching_tx((dest_slot, dest_lane)) for dest_lane in lane_range}
                valid_source = {dest_lane: False for dest_lane in lane_range}
                missing_frame = {dest_lane: False for dest_lane in lane_range}
                align_fifo_overflow = {dest_lane: False for dest_lane in lane_range}
                rx_fifo_overflow = {dest_lane: False for dest_lane in lane_range}
                rx_errors = {dest_lane: 0 for dest_lane in lane_range}
                rx_max_frame_length = {dest_lane: expected_bp_frame_size for dest_lane in lane_range}
                frame_number = None

            info[dest_slot] = {}
            for remapped_dest_lane in lane_range:
                cap_src = captured_source[remapped_dest_lane]
                comp_src = computed_source[remapped_dest_lane]
                valid_src = valid_source[remapped_dest_lane]
                data_expected = not ((comp_src[0] not in slots.keys()) or (dest_slot not in slots.keys()))
                cell = ' ' if (cap_src and cap_src == comp_src and valid_src) or not data_expected else '!'
                cell += ('------' if not cap_src or not data_expected
                         else '--??--' if not valid_src else 'S%02iL%02i' % cap_src)
                cell += '/S%02iL%02i' % comp_src
                if frame_number is not None and data_expected and any(frame_number):
                    cell += ' F=%02x' % frame_number[remapped_dest_lane]
                if data_expected:
                    if missing_frame[remapped_dest_lane]:
                        cell += '\n  MISSING FRAMES'
                    if align_fifo_overflow[remapped_dest_lane]:
                        cell += '\n  ALIGN FIFO OVERFLOW'
                    if rx_fifo_overflow[remapped_dest_lane]:
                        cell += '\n  RX FIFO OVERFLOW'
                    if rx_errors[remapped_dest_lane]:
                        cell += '\n  RX ERR = %i' % rx_errors[remapped_dest_lane]
                    if rx_max_frame_length[remapped_dest_lane] != expected_bp_frame_size:
                        cell += '\n  RX FRAME = %i Words' % rx_max_frame_length[remapped_dest_lane]
                info[dest_slot][remapped_dest_lane] = cell

        print('Post-remap, crossbar2 bin selector input lane indentification')

        corner_label = 'Slot->\nS/N ->\n\\|/Lane'
        col_labels = ['%i\n%s' % (slot_range[i], slot_labels[i]) for i in range(len(slot_range))]
        row_labels = lane_range
        self.print_table(info,
                         row_labels=row_labels, col_labels=col_labels, corner_label=corner_label,
                         line_sep=grid, max_width=width)

    def reset_crossbar_stats(self):
        """ Reset error statistics for the crossbar, crossbar2, and crossbar3.
        """

        for ib in self.ib:
            for cb in [ib.CROSSBAR, ib.CROSSBAR2, ib.CROSSBAR3]:
                if cb:  # make sure the crossbar exists in this firmware
                    cb.reset_stats()

    def reset_bp_shuffle_stats(self):
        """ Reset error statistics for the backplane shuffle.
        """

        for ib in self.ib:
            if ib.BP_SHUFFLE:  # makesure we have a shuffle block in this firmware
                ib.BP_SHUFFLE.reset_stats()

    # def get_shuffle_status(self):
    # """ Should be made async"""

    #     status = {}
    #     for crate in self.ic:
    #         status[crate] = {}

    #         for (slot, ib) in crate.slot.items():
    #             status[crate][ib] = {}

    #             status[crate][ib]['bp'] = await ib.BP_SHUFFLE.get_bp_rx_status(0)
    #             status[crate][ib]['qsfp'] = await ib.BP_SHUFFLE.get_bp_rx_status(1)

    #             status[crate][ib]['cb2'] = {}
    #             status[crate][ib]['cb2']['align'] = ib.CROSSBAR2.get_align_status()
    #             status[crate][ib]['cb2']['frame'] = ib.CROSSBAR2.get_frame_alignment_status()
    #             status[crate][ib]['cb2']['bin'] = ib.CROSSBAR2.get_bin_sel_status()

    #             status[crate][ib]['cb3'] = {}
    #             status[crate][ib]['cb2']['align'] = ib.CROSSBAR3.get_align_status()
    #             status[crate][ib]['cb2']['frame'] = ib.CROSSBAR3.get_frame_alignment_status()
    #             status[crate][ib]['cb2']['bin'] = ib.CROSSBAR3.get_bin_sel_status()

    #     return status

    async def get_corner_turn_engine_status_async(self, reset_stats=False):
        """ Gather info on the status of the corner-turn engine for all boards of the array installed in crates.

        The information includes status info on every lane of  the following corner turn subsystems:

            - Every lane (16) of the backplane PCB shuffle (shuffle between boards in a crate)
            - Every lane (8)  of the backplane QSFP shuffle (shuffle between crates)
            - Every lane (16) of crossbar 2
            - Every lane (8) of crossbar 3

        Parameters:

            reset_stats (bool): if True, the statistics on the corner turn subsystems will be reset before being measured.

        Returns:

            A list of dict containing the corner-turn engine status flags for each crate, each dist following the schema::

                "ic": <crate_object>: # Crate object
                "slots":
                    slot_number1: # Slot number as found in Motherboard.slot (int)
                        "ib": <ib_object> # Motherbpard object
                        "serial": <serial_number>  # Motherboard Serial number, as found in Motherboard.serial (str)
                        "subsystems":
                            subsystem1: # Subsystem name (str)
                                "status": <subsystem_status>  # Subsystem aggregate status for all lanes (bool)
                                "lanes":
                                    lane_number1:  # Lane number (int)
                                        "status": Lane status (True if there are any flags) (bool)
                                        "label": Full length label for the lane (subsystem & lane) (str)
                                        "fields":
                                            "Tx": # Tuple identifying the transmitter (tuple)
                                            "Rx": # Tuple identifying the receiver (tuple)
                                            field1_name: <field1_value> # field status. Field name is a str. Field value can be anything
                                            field2_name: <field2_value>
                                        ...
                                    lane_number2:
                                    ...
                            subsystem2:
                        ...
                    slot_number2:
                    ...

        Example:

            ::

                info[0]['slots'][2]['subsystems']['BP PCB']['status']
                info[0]['slots'][2]['subsystems']['BP PCB']['lanes'][3]['status']

            Print the summary of every lanes of every subsystem for every slot::

                info = await ca.get_corner_turn_engine_status_async(reset_stats=True)
                [f"Crate {c} Slot {slot_number} {sub_name} { [lane['status'] for lane in sub['lanes'].values()]}"
                   for c in info
                   for slot_number, slot in c['slots'].items()
                   for sub_name, sub in slot['subsystems'].items()]

            Get the status of the first crate as a dict ``{slot_number:{subsystem:status, ...}, ...}``::

                status =  {slot_number:{sub_name:sub['status'] for sub_name, sub in slot['subsystems'].items()}
                           for c in info[0] for slot_number, slot in c['slots'].items()}
        """
        info = []

        for crate_obj in self.ic:
            # initialize the dictionary the will contain the infor for each slot in the current crate
            crate = {}  # {crate_object: slot_dict}
            info.append(crate)
            slot_range = list(range(1, crate_obj.NUMBER_OF_SLOTS + 1)) or [None]

            crate['ic'] = crate_obj
            # gather info for each currently populated slot
            crate['slots'] = {}
            for slot_number in slot_range:
                crate['slots'][slot_number] = slot = {}
                slot['ib'] = None
                slot['serial'] = None
                slot['subsystems'] = {}
                if slot_number not in crate_obj.slot:
                    continue
                # initialize the dict for each lane
                ib = crate_obj.slot[slot_number]
                slot['ib'] = ib  # motherboard object in this slot
                slot['serial'] = ib.serial  # motherboard object in this slot
                # Gather status from the backplane PCB and QSFP links
                for lane_group in ib.BP_SHUFFLE.lane_group_names:
                    subsystem_name = f'BP {lane_group.upper()}'
                    slot['subsystems'][subsystem_name] = subsystem = {}  # {lane:status_dict, ...}
                    subsystem['lanes'] = {}
                    if reset_stats:
                        ib.BP_SHUFFLE.reset_stats()
                    # ee: list of N error status dicts  [lane0_errors, lane1 errors, ...]
                    status_dicts = await ib.BP_SHUFFLE.get_bp_rx_status(lane_group)
                    for lane_number, status_dict in enumerate(status_dicts):
                        subsystem['lanes'][lane_number] = lane = {}
                        lane['status'] = bool(status_dict) # True if there were any error flags returned for this lane
                        lane['label'] = f'{subsystem_name} L{lane_number:02d}'
                        if lane_group == 'pcb':  # if verbose, add the matching TX (slot,lane) for each rx lane on that slot
                            status_dict['Tx'] = f'{ib.crate.get_matching_tx((slot_number, lane_number))}'
                            status_dict['Rx'] = f'{(slot_number, lane_number)}'
                        lane['fields'] = status_dict
                        # print(f'added {subsystem_name} {lane_number} = {status_dict}')
                    # generate a subsystem status summary
                    subsystem['status'] = any(status_dict['status'] for status_dict in subsystem['lanes'].values())

                    # errs.append(ee)

                # Gather status from the crossbars
                for subsystem_prefix, cb in (('CB2', ib.CROSSBAR2), ('CB3', ib.CROSSBAR3)):
                    if reset_stats:
                        cb.reset_stats()

                    # Get the status flags. These are returned as a list of dict, one dict per lane.
                    align_status_dicts = await cb.get_align_status()
                    frame_status_dicts = await cb.get_frame_alignment_status()
                    bin_sel_status_dicts = await cb.get_bin_sel_status()
                    for subsystem_suffix, status_dicts in (('ALIGN', align_status_dicts),
                                                           ('FRAME #', frame_status_dicts),
                                                           ('BIN_SEL', bin_sel_status_dicts)):
                        subsystem_name = f'{subsystem_prefix} {subsystem_suffix}'
                        slot['subsystems'][subsystem_name] = subsystem = {}  # {lane:status_dict, ...}
                        subsystem['lanes'] = {}
                        for lane_number, status_dict in enumerate(status_dicts):
                            subsystem['lanes'][lane_number] = lane = {}
                            lane['status'] = bool(status_dict) # True if there were any error flags returned for this lane
                            lane['label'] = f'{subsystem_name} L{lane_number:02d}'
                            lane['fields'] = status_dict
                        # generate a subsystem status summary
                        subsystem['status'] = any(status_dict['status'] for status_dict in subsystem['lanes'].values())
        return info

    def _print_shuffle_status(self, info, verbose=1, grid=False) -> None:
        """ Formats and prints the status information of the corner-turn engine


        Parameters:

            info (list[dict]): output of get_corner_turn_engine_status_async that we wish to print

            reset_stats (bool): if True, the statistics on the corner turn subsystems will be reset before being measured.

            verbose (int): Determine how much status information is returned

                verbose=0: Will provide the '-' or 'ERR' for each subsystem  depending on whether there are any errors on any lane in the subsystem

                verbose=1: Will provide '-' or 'ERR' for each lane of the sybsystem depending on whether there are any errors on for each lane in the subsystem

                verbose=2: Will provide detailed status info on each lane of the subsystem in the form of a dict. Will also add the coordinates of the Tx/Rx pairs involved in backplane links.

            grid (bool): If true, line separators will be used

        """
        for crate in info:
            # Print the table
            corner_label = 'Slot->\nS/N ->\n\\|/Lane'
            # slot_labels = ['SN%s' % slot_dict['ib'].serial if slot_dict['ib'] else 'N/A' for slot_dicts in info[crate].values()]
            col_labels = [f"SN{slot['serial']}\n{slot_number}" for slot_number, slot in crate['slots'].items()]
            # row_labels = ['BP PCB Rx\nBP QSFP Rx\nCB2 FIFO\nCB2 ALIGN\nCB2 FRAMEnCB3 FIFO\nCB3 ALIGN\nCB3 FRAME\n']
            row_labels = []
            data = []

            slots = list(crate['slots'].values())
            first_slot = [slot for slot in slots if slot['ib']][0]
            if not verbose:
                # verbose = 0. We print summary status info only for each subsystem
                row_labels = list(first_slot['subsystems'].keys())
                data = [[('-', 'ERR')[subsystem['status']] for subsystem in slot['subsystems'].values()] for slot in slots]
            else:
                # verbose != 0 : We print info on each lane
                row_labels = [lane['label'] for subsystem in first_slot['subsystems'].values() for lane in subsystem['lanes'].values()]
                # create an 2-dimensional array of lane dicts
                all_lanes = [[lane for subsystem in slot['subsystems'].values()
                                   for lane in subsystem['lanes'].values()
                            ] for slot in slots]
                if verbose == 1:
                    # verbose = 1: we print summary info for each lane
                    data = [[('-', 'ERR')[lane['status']] for lane in lanes]
                            for lanes in all_lanes]
                elif verbose > 1:
                    # verbose > 1: we print all status info for each lane
                    data = [['\n'.join(f'{key}={value}' for key, value in lane['fields'].items())
                             for lane in lanes] for lanes in all_lanes]

            # for label, lanes in [('BP PCB Rx', 16), ('BP QSFP Rx', 8), ('CB2 ALIGN', 16),
            #                      ('CB2 FRAME #', 16), ('CB2 BIN_SELs', 2), ('CB3 ALIGN', 8),
            #                      ('CB3 FRAME #', 8), ('CB3 BIN SELs', 8)]:
            #     if verbose and lanes:
            #         row_labels += ['%s L%02i' % (label, lane) for lane in range(lanes)]
            #     else:
            #         row_labels += [label]
            # return info
            # print 'row_labels=', row_labels
            # print 'col_labels=', col_labels
            # print 'data=', info
            self.print_table(
                data, row_labels=row_labels, col_labels=col_labels,
                corner_label=corner_label, line_sep=grid)

    async def print_shuffle_status(self, reset_stats=False, verbose=1, grid=False):
        """ Prints status information of the corner-turn engine


        Parameters:

            reset_stats (bool): if True, the statistics on the corner turn subsystems will be reset before being measured.

            verbose (int): Determine how much status information is returned

                verbose=0: Will provide the '-' or 'ERR' for each subsystem  depending on whether there are any errors on any lane in the subsystem

                verbose=1: Will provide '-' or 'ERR' for each lane of the sybsystem depending on whether there are any errors on for each lane in the subsystem

                verbose=2: Will provide detailed status info on each lane of the subsystem in the form of a dict. Will also add the coordinates of the Tx/Rx pairs involved in backplane links.

            grid (bool): If true, line separators will be used

        """

        info = await self.get_corner_turn_engine_status_async(reset_stats=reset_stats)
        self._print_shuffle_status(info, verbose=verbose, grid=grid)


    def print_table(self, data=None,
                    row_labels=None, col_labels=None, corner_label=None,
                    row_keys=None, col_keys=None,
                    max_width=180, line_sep=False):
        """
        Prints a nicely formatted table of data, where data is a list of column contents.
        """

        # If we provide no row/col keys, and labels are dict, use the label keys as the row/col keys
        if col_keys is None:
            if isinstance(col_labels, dict):
                col_keys = list(col_labels.keys())
            elif isinstance(data, dict):  # columns are dicts
                col_keys = list(data.keys())
            else:
                col_keys = list(range(len(data)))

        data = [data[key] for key in col_keys]

        if row_keys is None:
            if isinstance(row_labels, dict):
                row_keys = row_labels.keys()
            else:
                for col_data in data:
                    keys = list(col_data.keys()) if isinstance(col_data, dict) else list(range(len(col_data)))
                    if row_keys is None:
                        row_keys = keys
                    elif keys != row_keys:
                        raise ValueError('Row keys are not identical for every column')

        data = [[col_data[row_key] for row_key in row_keys] for col_data in data]

        if col_labels is None:
            col_labels = [str(key) for key in col_keys]
        else:
            col_labels = [str(label) for label in col_labels]
        if row_labels is None:
            row_labels = [str(key) for key in row_keys]
        else:
            row_labels = [str(label) for label in row_labels]

        if corner_label is None:
            corner_label = ''

        col_keys = list(range(len(data)))
        row_keys = list(range(len(data[0])))
        col_width = [max([len(line) for cell_data in [col_labels[col]] + data[col]
                     for line in str(cell_data).splitlines()]) for col in col_keys]
        row_labels_width = max([len(line) for row_label in [corner_label] + row_labels
                               for line in row_label.splitlines()])
        col_labels_height = max([len(label.splitlines()) for label in [corner_label] + col_labels])

        remaining_col_keys = list(col_keys)
        while remaining_col_keys:
            block_col_keys = []
            block_col_width = []
            while remaining_col_keys:
                col_key = remaining_col_keys[0]
                width = col_width[col_key]
                block_width = row_labels_width + 3 + sum(block_col_width + [width]) + len(block_col_width) * 3 + 3 + 1
                # print block_col_keys, col_key, block_col_width, width, block_width, max_width
                if block_width <= max_width:
                    block_col_keys.append(remaining_col_keys.pop(0))
                    block_col_width.append(width)
                else:
                    break

            line_format = ('| %%-%is' % row_labels_width
                           + ' | '
                           + ' | '.join('%%-%is' % width for width in block_col_width)
                           + ' |')
            line_sep_str = '+' + '+'.join(['-' * (width + 2) for width in [row_labels_width] + block_col_width]) + '+'

            print(line_sep_str)

            for i in range(col_labels_height):
                line_data = [cell.splitlines()[i] if i < len(cell.splitlines()) else ''
                             for cell in [corner_label] + [col_labels[col_key] for col_key in block_col_keys]]
                print(line_format % tuple(line_data))

            print(line_sep_str)

            for row_key in row_keys:
                row_data = [str(cell) for cell in [row_labels[row_key]] + [data[col_key][row_key]
                            for col_key in block_col_keys]]
                row_height = max([len(cell.splitlines()) for cell in row_data])
                for i in range(row_height):
                    line_data = [cell.splitlines()[i] if i < len(cell.splitlines()) else '' for cell in row_data]
                    print(line_format % tuple(line_data))
                if line_sep:
                    print(line_sep_str)

            if not line_sep:  # Make sure we have a bottom line if we didn't already printed one
                print(line_sep_str)

            print() # make sure we have a blank line between tables

    def print_iceboard_table(self, func=None, row_labels=None, grid=False, add_serial=True):
        """
        func=function
        func=async function : wll be called concurrently
        func=data, dict, key is motherboard object
        """

        if not len(self.ib):
            print('[ There are no Motherboards in the hardware map ]')
            return

        # Process func and end up with a dict of {motherboard:cell_text}
        if func:
            if isinstance(func, dict):
                data = func
            elif hasattr(func, 'async_map'):
                data = dict(zip(self.ib, func.async_map(self.ib)))
            else:
                data = {ib: func(ib) for ib in self.ib}
        else:
            data = {ib: '' for ib in self.ib}

        iceboards = list(data.keys())

        if row_labels is None:
            row_labels = ''
        if isinstance(row_labels, str):
            row_labels = [row_labels]

        # Print a table of crate-less (stand-alone) motherboards
        orphan_iceboards = [ib for ib in iceboards if not ib.crate or not ib.crate.serial]
        corner_label = 'Standalone\nMotherboards'
        # col_labels = ['-'] * len(orphan_iceboards)
        col_labels = [f'Virt. slot {ib.slot}\n{ib.part_number}_SN{ib.serial}\n{ib.hostname}' for ib in orphan_iceboards]
        # for i, ib in enumerate(orphan_iceboards):
        #    col_labels[i] += '\n%s' % ib.hostname
        table = []
        for ib in orphan_iceboards:
            # cell = 'SN' + ib.serial + '\n' if add_serial else ''
            cell = data[ib] if data else ''
            table.append([cell])  # append single-row column
        if not any(table):
            table = []
        if table:
            self.print_table(
                table, row_labels=row_labels, col_labels=col_labels,
                corner_label=corner_label, line_sep=grid)

        # Create list of unique crates, preserving order. We use the keys of a dict to implement a de-facto ordered set (Python 3 dicts are ordered, sets ar enot)
        valid_crates = list({ib.crate: None for ib in iceboards if ib.crate and ib.crate.serial}.keys())

        for crate in valid_crates:
            corner_label = '%s\nCrate #%s' % (crate.get_string_id(), crate.crate_number)
            slot_range = list(range(1, crate.NUMBER_OF_SLOTS + 1))
            col_labels = ['%i' % (s) for s in slot_range]



            if add_serial:
                for i, slot in enumerate(slot_range):
                    col_labels[i] += (f'\nSN {crate.slot[slot].serial}') if slot in crate.slot else '\n-'
            if add_serial:
                for i, slot in enumerate(slot_range):
                    col_labels[i] += (f'\n{crate.slot[slot].hostname}') if slot in crate.slot else '\n-'

            table = []
            # local_row_labels = [row_labels for crate in valid_crates]
            for slot in slot_range:

                # col_data = []
                if slot in crate.slot.keys():
                    cell = data[crate.slot[slot]] if data else ''
                else:
                    cell = '-'
                # col_data.append(cell)
                table.append([cell])
            if table:
                self.print_table(
                    table, row_labels=row_labels, col_labels=col_labels,
                    corner_label=corner_label, line_sep=grid)

    def print_iceboard_temperatures(self):
        sensor = self.ib[0].TEMPERATURE_SENSOR.MB_FPGA_DIE
        self.print_iceboard_table(
            lambda ib: '%3.1f' % ib.get_motherboard_temperature(sensor),
            row_labels='FPGA Die Temp')

    def print_iceboard_power(self):
        self.print_iceboard_table(lambda ib: '%0.1f' % ib.get_total_power())

    def reset_fpga_stats(self):
        """ Resets FFT overflow count for all boards in FPGA array.
        """
        for ib in self.ib:
            ib.reset_fft_overflow_count()

    # def get_monitoring_info(self):
    #     return self.ib.index_by(lambda ib: ib.get_id()).get_status()

    async def get_arm_metrics_async(self, metrics):
        """ Get the monitoring information on the backplanes & boards that are accessible from the ARM.

        Includes:

            - Backplane metrics, as measured from one board in each crate
            - Motherboard hardware metrics (voltages, temperatures), which also includes mezzanines voltage/current.

        Returns:
            A :class:`Metrics` object.
        """

        # IceCrate metrics
        self.logger.debug(f'{self!r}: Getting Motherboard backplane hardware metrics')
        for ic in self.ic:
            slot, ib = list(ic.slot.items())[0]
            metrics += await ib.get_backplane_metrics_async()

        # IceBoard metrics
        self.logger.debug(f'{self!r}: Getting Motherboard temperature & power supply metrics')
        m = await asyncio.gather(*[ib.get_metrics_async() for ib in self.ib])
        metrics += m
        self.logger.debug(f'{self!r}: Got {len(m)} Motherboard temperature & power supply metrics')
        metrics += await asyncio.gather(*[ib.get_fpga_udp_metrics_async() for ib in self.ib if ib.fpga])

    async def get_fpga_metrics_async(self, metrics, reset=True):
        """ Get the monitoring information on the FPGA firmware status across the array.

        Includes:

            - Backplane receiver/transmitter status with packet statistics for both the PCB and QSFP links.

        Returns:
            A :class:`Metrics` object.
        """

        # Shuffle status
        self.logger.debug(f'{self!r}: Getting corner-turn links metrics (over FPGA UDP link)')
        metrics += await asyncio.gather(*[ib.get_bp_shuffle_metrics_async(reset=reset) for ib in self.ib])
        self.logger.debug(f'{self!r}: Getting corner-turn crossbars metrics (over FPGA UDP link)')
        metrics += await asyncio.gather(*[ib.get_crossbar_metrics_async(reset=False) for ib in self.ib])
        self.logger.debug(f'{self!r}: Getting channelizer metrics (over FPGA UDP link)')
        metrics += await asyncio.gather(*[ib.get_channelizer_metrics_async(reset=reset) for ib in self.ib])
        self.logger.debug(f'{self!r}: Finished gathering FPGA/backplane metrics')

        # Backplane GTX
        # Errors, signal level

        # Command errors

        if self.sync_timestamps:
            for i, ib in enumerate(self.ib):
                try:
                    fn, ts = await ib.capture_frame_time_async(format='raw')
                    if not i:
                        fn0, ts0 = (fn, ts)
                    crate, slot = ib.get_id()
                    slot = ib.slot - 1
                    metrics.add('fpga_time_delta', ts.nano - ts0.nano, crate=crate, slot=slot)
                    metrics.add('fpga_frame_number_delta', fn - fn0, crate=crate, slot=slot)
                    metrics.add('fpga_time_error',
                                ts.nano - (self.sync_timestamps[i].nano + fn * 2560),
                                crate=crate, slot=slot)
                    metrics.add('fpga_sync_time_delta',
                                self.sync_timestamps[i].nano - self.sync_timestamps[0].nano,
                                crate=crate, slot=slot)
                    metrics.add('fpga_sync_time_integer_second_offset',
                                (self.sync_timestamps[i].nano % 1000000000) - 1000000000,
                                crate=crate, slot=slot)
                except RuntimeError:
                    self.logger.error(f'{ib!r}: Timeout while capturing frame time')

        return metrics

    def print_iceboard_info(self):
        """ Gather metrics from the iceboards and print them in a nicely formatted table """
        if not self.ib:
            print('There are no IceBoards in the array')
            return
        info = [i for (i, _) in self.ib._get_motherboard_metrics_async()]
        keys = '\n'.join(info[0].keys())
        data = {ib: ('\n'.join(info[i].values())) for i, ib in enumerate(self.ib)}
        self.print_iceboard_table(data, row_labels=keys)

    def print_rx_err_map(
            self,
            icecrates=None,
            reset_stats=0,
            delay=-5,
            tx_power=None,
            tx_precursor=None,
            tx_postcursor=None,
            lpm=None,
            dfe_reset=False,
            stop_on_errors=2,
            verbose=1):
        """
        Test the backplane PCB and QSFP link operation. Allows various
        parameters of the GTX transceivers to be changed to measure their
        effect.

        Parameters:

            icecrates (IceCrate Ccoll): List of icecrates on which the test is
                done. If `None`, all icecrates are initialized/tested.

            reset_stats (bool): Reset the statistics counters. Default is false.

            delay (int):

            tx_power (int): transmit power to be set on the GTX. Ranges from
                0-15. If ``None``, TX power levels ar enot changed.

            tx_precursor: None,

            tx_postcursor: None

            lpm (bool): If true, enables Low Power Mode in the GTX. Requires a dfe_reset.

            dfe_reset (bool): If true, resets the Dynamix Feedback Equalizer
                to allow it to find a new equalization solution. Is needed
                when power levels or operaiton mode are changed

            stop_on_errors : =2,

            verbose=1
        """
        if icecrates is None:
            icecrates = self.ic

        ibs = Ccoll.chain(*icecrates.slot.item_values())
        gtx = Ccoll.chain(*ibs.BP_SHUFFLE.gtx)

        time_without_error = [None] * len(icecrates)
        has_errors = [False] * len(icecrates)

        if lpm is not None:
            gtx.RXLPMEN = lpm

        if tx_power is not None:
            ibs.BP_SHUFFLE.set_tx_power(tx_power)

        if tx_precursor is not None:
            gtx.TXPRECURSOR = tx_precursor

        if tx_postcursor is not None:
            gtx.TXPOSTCURSOR = tx_postcursor

        if dfe_reset:
            gtx.reset_rx_equalizer()
            time.sleep(0.1)

        if reset_stats:
            self.ib.BP_SHUFFLE.reset_stats()

        t0 = time.time()

        finished = False
        try:
            while not finished:
                time.sleep(abs(delay))
                dt = time.time() - t0
                print('At', time.asctime(), '(%s seconds since the method call)' % (datetime.timedelta(seconds=dt)))
                for i, ic in enumerate(icecrates):
                    err_map = [[None] * ic.NUMBER_OF_SLOTS for _ in range(ic.NUMBER_OF_SLOTS)]
                    # print  ic.slot.values()
                    worst_err = 0
                    worst_det = 1
                    for slot, ib in ic.slot.items():
                        errs, det = ib.BP_SHUFFLE.get_rx_lane_monitor(['ERROR_CTR', 'FRAME_DETECT'], lane_group='pcb')
                        worst_err = max(worst_err, max(errs))
                        worst_det = min(worst_det, min(det))

                        if not has_errors[i]:
                            time_without_error[i] = dt

                        if (any(errs) or not all(det)):
                            has_errors[i] = True

                        for lane in range(16):
                            rx_lane_id = (slot, lane)
                            tx_lane = ib.BP_SHUFFLE.get_matching_tx_node_id(rx_lane_id)
                            err_map[rx_lane_id[0] - 1][tx_lane[0] - 1] = (
                                errs[lane] if errs[lane] else '!DET' if not det[lane] else '-')
                            # print '%s -> %s = %i' % (tx_lane, rx_lane_id, errs[lane])
                    row_labels = ['Tx S%02i SN%s' % (ib.slot, ib.serial) for ib in ic.slot.values()]
                    col_labels = ['Rx S%02i\nSN%s' % (ib.slot, ib.serial) for ib in ic.slot.values()]
                    corner_label = '%s\nCrate #%s' % (ic.get_string_id(), ic.crate_number)
                    print('    %s: %-10s %s %s' % (
                        ic.get_string_id(),
                        'No Frames!' if not worst_det else ('%i errors' % worst_err),
                        '%s without errors' % datetime.timedelta(seconds=int(time_without_error[i])),
                        'so far' if not has_errors[i] else ''))
                    if verbose:
                        self.print_table(
                            err_map, row_labels=row_labels, col_labels=col_labels,
                            corner_label=corner_label)
                if (stop_on_errors == 1 and any(has_errors)) or (stop_on_errors > 1 and all(has_errors)):
                    break
        except KeyboardInterrupt:
            pass

    def print_net_length_map(self):
        for ic in self.ic:
            err_map = [[None] * ic.NUMBER_OF_SLOTS for _ in range(ic.NUMBER_OF_SLOTS)]
            for slot, ib in ic.slot.items():
                for lane in range(16):
                    rx_lane = (slot, lane)
                    tx_lane = ic.get_matching_tx(rx_lane)
                    err_map[rx_lane[0] - 1][tx_lane[0] - 1] = '%0.1f' % (ic.get_rx_net_length(rx_lane) / 1000)
            row_labels = ['Tx S%02i SN%s' % (ib.slot, ib.serial) for ib in list(ic.slot.values())]
            col_labels = ['Rx S%02i\nSN%s' % (ib.slot, ib.serial) for ib in list(ic.slot.values())]
            corner_label = '%s\nCrate #%s' % (ic.get_string_id(), ic.crate_number)
            self.print_table(err_map, row_labels=row_labels, col_labels=col_labels, corner_label=corner_label)

    def print_iceboard_qsfp(self):
        self.print_iceboard_table(lambda ib: '\n'.join(ib.hw.qsfp.get_serial_number().map(str)), grid=1)

    def print_frame_info(self):
        ts = []
        sid = []

        # get 8 bits of stream ID
        self.HEADER_CAPTURE_DATA_SEL = 0
        self.HEADER_CAPTURE_EN = 1
        self.HEADER_CAPTURE_EN = 0
        for i in range(16):
            self.HEADER_CAPTURE_LANE_SEL = i
            sid.append(self.HEADER_CAPTURE_DATA)

        # get lsb of timestamp
        self.HEADER_CAPTURE_DATA_SEL = 1
        self.HEADER_CAPTURE_EN = 1
        self.HEADER_CAPTURE_EN = 0
        for i in range(16):
            self.HEADER_CAPTURE_LANE_SEL = i
            ts.append(self.HEADER_CAPTURE_DATA)

        for i in range(len(ts)):
            print('Lane %02i: Stream ID=0x%02x, Frame = 0x%02x (delta = %i)' % (i, sid[i], ts[i], ts[i] - ts[0]))

    def plot_crate_temperatures(self, figure_number=1):

        sensor = self.ib[0].TEMPERATURE_SENSOR.MB_FPGA_DIE

        plt.figure(figure_number)
        plt.clf()
        # plt.hold(1)
        for ic in self.ic:
            ib = Ccoll(ic.slot.values())
            t = ib.get_motherboard_temperature(sensor)
            s = ib.slot
            avg_temp = np.average(t)
            h = plt.plot(s, t, label=ic.get_string_id())
            plt.plot([min(s), max(s)], [avg_temp] * 2, ':', color=h[0].get_color(), lw=2)
            print('%s: %fdegC' % (ic.get_string_id(), avg_temp))
        plt.legend(loc='best')
        plt.xlabel('Slot number')
        plt.ylabel('FPGA Die temperature [degC]')
        plt.grid(1)
        plt.title('FPGA die temperatrures for multiple crates')

    def _update_arm_firmware(self, image_filename, power_cycle=True):
        """
        Update the ARM SD card firmware. The image must be compressed with bzip2 and the boards must be power cycled.
        """
        self.ib._update_arm_firmware(image_filename, delay=120)
        # if self.ps and power_cycle:
        #     self.ps.unlock()
        #     self.ps.power_cycle(delay=4)

    async def set_adc_delays_async(self, **kwargs):
        """
        Set ADC delays for all Mezzanines on all Motherboards of the array. Calls
        set_adc_delays() on each Motherboard instance with the specified
        paramaters.
        """

        for ib in self.ib:
            if ib.is_open():
                self.logger.debug(f"{self!r}: Setting ADC delays")
                ib.set_adc_delays(**kwargs)
                await asyncio.sleep(0)
            else:
                self.logger.warning(f"{self!r}: Communication with FPGA is not initialized. Cannot set ADC delays")

    def print_adc_frequencies(self):
        """ Displays the frequency of the clocks coming out of each AC chip.
        """

        for ib in self.ib:
            freqs = ', '.join('{:7.3f}'.format(ib.FreqCtr.read_frequency(f'ADC_CLK{c}', gate_time=1e-3)/1e6) for c in (0,4,8,12))
            print(f'{ib!r}: {freqs}')


log_levels = {'info': logging.INFO,
              'debug': logging.DEBUG,
              'warn': logging.WARNING,
              'warning': logging.WARNING,
              'error': logging.ERROR}


def setup_logging(stderr_log_level='debug', syslog_log_level=None):
    """Configure logging levels when using interactive iPython sessions.

    Parameters:
        std_err_log_level (str or int): Sets the logging level for the log messages displayed on the screen (on the stderr pipe)

        syslog_log_level (str or int): Sets the logging level for the log messages sent to syslog

    Returns:

        root logger object

    """
    # logging.getLogger('parso.python.diff').disabled = True  # disable ipython logging in interactive sessions
    # logging.getLogger('parso.cache').disabled = True  # disable ipython logging in interactive sessions
    logging.getLogger('parso').setLevel(logging.WARNING) # disable ipython logging in interactive sessions
    logging.getLogger('asyncio').setLevel(logging.WARNING)

    formatter = logging.Formatter('%(asctime)s %(levelname)s %(name)s:  %(message)s')




    # Set-up main logger
    logger = logging.getLogger('')
    logger.handlers = []  # Clear all existing handlers
    logger.setLevel(logging.DEBUG)  # pass all messages to the handlers which will filter what they want

    if syslog_log_level:
        syslog_handler = logging.handlers.SysLogHandler()
        syslog_handler.setLevel(log_levels[syslog_log_level])
        syslog_handler.setFormatter(formatter)
        logger.addHandler(syslog_handler)

    if stderr_log_level:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        stream_handler.setLevel(log_levels[stderr_log_level])
        logger.addHandler(stream_handler)
    return logger


def add_logging_arguments(parser):
    # parser.add_argument('-t', '--log_target', action='store', type=str, default='syslog', help="Logging target ('stream', 'syslog' or a filename)")
    # parser.add_argument('-l', '--log_level', action='store', type=str, choices=log_levels, default='debug', help='Logging level')
    parser.add_argument('--syslog_log_level', action='store', type=str, choices=log_levels, default=None, help='SYSLOG logging level')
    parser.add_argument('-l', '--stderr_log_level', '--log_level', action='store', type=str, choices=log_levels, default='warning', help='stderr (console) Logging level')

def add_fpga_array_arguments(parser):
    parser.add_argument('--if_ip',           type=str, help='IP address of adapter through which the connection to the FPGA will be established. This is used solely for direct UDP communications with the FPGA. If not specified, the system will use the same interface that communicates with the ARM processor.')
    parser.add_argument('-i', '--iceboards', type=str, nargs='*', help="Space-separated list of iceboards, which can be specified byip address (e.g. 10.10.10.7), hostname (e.g. iceboard0007.local) if a mDNS client is running locally, or by serial number (e.g. 0007 or simply 7) in which case active mDNS discovery will be done")
    parser.add_argument('-c', '--icecrates', type=str, nargs='*', help="Space-separated list of icecrate serial numbers.  Discover and adds all boards in the specified serial number")
    parser.add_argument('--subarrays',       type=int, nargs='*', help='Keep in the hardware map only the boards that are in the specified subarrays. This applies only to iceboards that are specified in a YAML file.')
    parser.add_argument('-x', '--exclude_iceboards', type=str, nargs='*', help="Space-separated list of iceboards serials to exclude ")
    parser.add_argument('--ignore_missing_boards', action='store_true', help='Do not fail if boards are not found')
    parser.add_argument('--no_mezz',         action='store_true', help='Do not attempt to auto-detect the mezzanines')
    parser.add_argument('--ping',            type=int, help="1: Check if Tuber is responding. 0: Check but ignore. ")
    parser.add_argument('--mdns_timeout',    type=float, help="Time to wait for mDNS discovery replies")
    parser.add_argument('--prog',            type=int, nargs='?', const=1,
                        help='Overrides the FPGA programming method. Is meaningful only if --mode is specified'
                        '--prog 0: do not program the FPGA (even if --mode is specified), '
                        '--prog or prog 1: programs the FPGA only if not already programmed (requires --mode). '
                        '--prog 2: always program the FPGA (requires --mode).')
    parser.add_argument('-b', '--bitfile',   type=str, help='Folder in which to search for FPGA bitstream or full path to a bistream to use explicitely (mode must still be specified)')
    parser.add_argument('-m', '--mode',      type=str, help="Operational mode ('shuffle16', 'shuffle256', etc.). When specified, the FPGA is programmed with the proper firmware bitstream (unless blocked with --prog 0) and the mode is initialized (unless blocked with -open 0). If not specified, only a connection to the platform is established")
    parser.add_argument('--init',      type=int, nargs='?', const=None,
                        help='Overrides FPGA initialization level'
                        '--init 0: only connect to platform'
                        '--init 1: program FPGA (requires --mode or --bitfile)'
                        '--init 2: establish connection with FPGA firmware and create firmware objects'
                        '--init 3: initialize the desired operational mode (requires --mode)')

    parser.add_argument('-o', '--open',      type=int, nargs='?', const=None,
                        help='Alternate way of overriding the array initialization level'
                        '--open 0: equivalent to --init 0'
                        '--open 1: equivalent to --init 3')
    parser.add_argument('--sync_method',     type=str, help="Sets the global syncing method ('distributed_time', 'centralized_time_trigger', 'centralized_soft_trigger', 'local_soft_trigger')")
    parser.add_argument('--sync_source',     type=str, help="Sets the global syncing source ('bp_gpio_int', 'bp_time', 'bp_trig')")
    parser.add_argument('--sync_master',     type=str, help="Serial number of the Motherboard that generates the time or trig signal")
    parser.add_argument('--sync_master_source', type=str, help="Source of the synchronization signal generated by the master board")
    parser.add_argument('--sync_master_output', type=int, help="user output on which the synchronizatoin signal is routed on the master board")
    parser.add_argument('-f', '--frames_per_packet', '--fpp',     type=int, help="Number of frames per packeet. Default=2.")
    parser.add_argument('-s', '--sampling_frequency', type=float, help="Sampling frequency of the ADC in Hz. Default=800e6.")
    parser.add_argument('--integration_period', type=int, help="Integration period (in frames) of the firmware correlator (if present). Defaults to 65536. ")
    parser.add_argument('--autocorr_only', type=int, nargs='?',const=1, default=0, help="If set, the firmware correlator will send only the autocorrelation products")
    parser.add_argument('-u', '--udp_retries', type=int, help="Number of times UDP packet transmission to the FPGA will be retried.")
    parser.add_argument('--fpga_ip_addr_fn', type=str, help="Method used to set the FPGA IP address relative to the ARM address")
    parser.add_argument('hwm',               type=str, nargs='*', default=argparse.SUPPRESS, help="target hardware")  # allows free-style hardware description string

    defaults = dict(
        sync_method='distributed_time',
        sync_source='bp_trig',
        sync_master=None,
        sync_master_source=None,
        sync_master_output=None,
        mode=None,
        frames_per_packet=2,
        udp_retries=3)
    return defaults


def parse_args_as_dict(parser, *args, **kwargs):
    """
    Parses arguments like argparse.parse_args(...), with the following differences:

       - The results are returned as a dictionary instead of a argparse namespace.
       - If an argument is part of a group that has the ``sub_dict`` attribute, all the argument
         values of this group are stored in a subdictionary named by that attribute.
    """

    # Create a dictionary that maps command line arguments to their group name.
    group_map = {action.dest: getattr(group, 'sub_dict', '')
                 for group in parser._action_groups
                 for action in group._group_actions}

    args = parser.parse_args(*args, **kwargs)

    args_dict = {}
    for k, v in vars(args).items():
        # if v is not None:
        sub_dict = group_map[k]
        if sub_dict:  # if a sub dict was specified
            args_dict.setdefault(sub_dict, {})[k] = v
        else:
            args_dict[k] = v
    return args_dict


# Create Power Supply array
def PSArray(power_supplies=[]):
    """
    """
    if not power_supplies:
        return Ccoll([])
    if isinstance(power_supplies, list):
        ps = Ccoll(AgilentN5764A(hostname=hostname) for hostname in power_supplies)
        ps.open()
        return ps
    else:
        raise TypeError('Wrong type to PSArray')


def create_fpga_array(args=None):
    """
    Creates FPGAArray object interactively from the command line and/or a YAML
    configuration file, mainly for debugging and testing. All command-line
    options can also be specified directly in the YAML file.

    The function can also create simple GPU nodes and power supply objects to
    assist testing of the FPGA array.

    The hardware map describing all the components of the FPGA array
    (motherboards, backplanes, mezzanines) can be specified by explicitrly
    instantiating the corresponding objects in the YAML file (e.g.
    IceBoardPlus!{...}).

    Alternatively, the hardware can  be specified  using the command line
    arguments or their equivalent entries in the configuration file, which
    allow those objects to be created from the motherboard IP address,
    hostname or serial number, or just the crate serial number. When serial
    numbers are specified, boards and crates and are looked up on the locan
    network using mDNS.

    Examples:

    - create_fpga_array --iceboards 10.10.10.5 10.10.10.6   # Creates  an array of 2 boards at specified IP addresses
    - create_fpga_array --iceboards iceboard0005.local iceboard0006.local   # Creates  an array of 2 boards at specified hostname (assuming the host computer runs a mDNS client)
    - create_fpga_array --iceboards 0005 0006   # Creates  an array of 2 boards with specified serial numbers (resolved using a mDNS request on the network)
    - create_fpga_array --iceboards 5 6   # Same as above. Works only with purely numeric serial numbers.
    - create_fpga_array --icecrates MGK7BP16_003 MGK7BP16_007  # Load all boards in crate serial number 003 and 007
    - create_fpga_array --icecrates 3 7  # Same as above. MGK7BP16-type backplane is assumed by default

    In the configuration file, some parameters are grouped in the following sub-dictionaries:

    root object::

        logging:     # Contains all the parameters related to logging
        fpga_array:  # Contains all the parameters related to the creation and
                     # initialization of the FPGA motherboards, crates and mezzanines
        gpu_array:   # Contains all the parameters related to the creation and
                     # initialization of the GPU nodes
        power_supply_array:  # Contains all the parameters related to the creation
                             # and initialization of the power supplies

    Example::

        my_config:
            fpga_array:
                iceboards: ["10.10.10.5", "10.10.10.6"]  # or any other syntax accepted by the comamnd line
                icecrates: [3, 7]
                ...
            logging:
                log_target: "syslog"
            ...

    Generic parameters (root dict)
    ------------------------------

    yaml: Name of a YAML configuration file to load. One or more root object
       can be specified, in which case the filename and the list of root
       objects must be separated by a single ':'. If multiple root objects are
       specified, they are all combined and lists or dictionaries with similar
       names are all combined.

       Objects can be specified hierarchically using the '.' hierarchy
       separator. An object starting with '.' starts at the same node level as
       the previous object.

    Example::

        --yaml file1.yaml # Load config from the top node of the file
        --yaml file1.yaml:site1 # Load  config from the site1 element
        --yaml file1.yaml:site1 site2# Load config by combining the elements of site1 and site2 objects
        --yaml file1.yaml:site1.boards .gpus .ps  # combine site1.boards, site1.gpus and site1.ps


    FPGA array parameters (``fpga_array`` sub-dict)
    -----------------------------------------------

    Here is a summary of the FPGA array creation parameters. Detailed
    description of each parameter is profided in the ``FPGAArray`` object.

    - hwm: Contains a hardware map object (config file only, created with HardwareMap! object)

    - iceboards: List of IceBoards (IP, hostnames or serial numbers) to add
        to the hardware map. Their connected IceCrate and Mezzanine is
        also automatically added.

    - icecrates: List of IceCrates (serial numbers) to add to the hardware map. Adds all IceBoards in them.

    - exclude_iceboards: Remove the specified IceBoards (serial numbers) from the hardware map.

    - mdns_timeout: Time to wait for motherboards to responds to mDNS queries

    - no_mezz: Do not discover nor initialize the mezzanine on the motherboards

    - subarrays: Keep only iceboards that are in the specified subarrays
        (applicable only to objects created explicitely in the
        configuration file)

    - ping: Keep only boards that respond to requests

    - bitfile: path of the folder in which to search for default FPGA
        bitstream files (based on the platform and mode), or full pathname
        to a FPGA bitstream to use (mode must still be specified to allow
        proper initialization)


    - prog: Overrides the default FPGA bitstream configuration level. 0=Do not program, 1=program if needed, 2=always program.

    - init: Overrides the array initialization level. 0=just connect to platform, 1= program FPGA, 2= connect to firmware and configure firmware object, 3=initialize firmware indesired mode.

    - open: Alternate way of specified init. 0: just connect to platform, 1: program FPGA, connect to firmware and configure objects and initialize operational mode.

    - if_ip: address of the interface used to communicate with the FPGA. If
        not specified, the same interface as the one used for communicate
        with the ARM processor is used.

    - sampling_frequency: Specifies the sampling frequency of the CHIME ADC mezzanine, in Hz (typically 800 MHz). If `None`, the platform/firmware mode default is used.

    - reference_frequency: Specifies the frequency of the system's reference clock in Hz (typically 10 MHz). If `None`, the platform/firmware mode default is used.

    - data_width: Bit width used after the channelizer's scaler (4 or 8)

    - sync_method: string describing the method used to synchronize all the boards in the array

    - sync_source: string describing the source of the synchronization signal.


    Power Supply Array parameters (``ps_array`` sub_dict)
    -----------------------------------------------------

    - power_supplies: list of GPU nodes (IP addresses or hostnames) for which power supply objects are to be created.

    Logging parameters: (``logging`` sub-dict)
    ------------------------------------------

    - log_target : String indicating the logging target (default = 'syslog'). May be

        - 'stream' : logs on stdout (not recommended in interactive sessions)
        - 'syslog': logs on Syslog on localhost
        - any other string: logs to a file specified by the string

    - log_level : String indicating the logging level. May be 'info',
        'error', 'warning' , 'debug'. default is 'debug'.

    - sql_log_level : String indicating SQLAlchemy logging level. Same
        values as ``log_level``. Defaults to 'warning'.

    - stderr_log_level : String indicating what messages to log on stderr
       (usually the console) in addition to the main log target. Is usually
       used to make sure that important messages (warnings and errors) are
       seen immediately by the interactive operator. Values are the same as
       ``log_level``. Defaults to 'warning'.

    """
    # -------------------------------
    # Parse command line arguments
    # -------------------------------
    # description is the first line of the docstring of this module
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])

    # Add logging-related command-line parameters
    logging_group = parser.add_argument_group('logging parameters', 'Specify how and where the logging is done')
    logging_group.sub_dict = 'cli_logging'  # group all arguments in this group in a sub dictionary with this name
    add_logging_arguments(logging_group)

    # Add FPGA Array-related command-line parameters
    fpga_group = parser.add_argument_group(
        'FPGA Array parameters',
        'Allows interactive creation of a hardware map and initialization of all its components')
    fpga_group.sub_dict = 'cli_fpga_array'  # group all arguments in this group in a sub dictionary with this name
    add_fpga_array_arguments(fpga_group)

    ps_group = parser.add_argument_group(
        'Power Supply Array parameters', 'Allows interactive creation of Power Supply objects')
    ps_group.sub_dict = 'cli_power_supply_array'  # group all arguments in this group in a sub dictionary with this name
    ps_group.add_argument('-p', '--power_supplies', type=str, nargs='+',
                          help='List of IP address or hostnames of the power supply objects '
                               '(Agilent_N5764A) to be created.')
    ps_group.add_argument('--power', type=str, help='Set the state of the power supplies: ON, OFF or CYCLE')

    # Add generic command-line parameters
    parser.add_argument('-y', '--yaml', type=str, nargs='+',
                        help='YAML configuration file name, optionally followed by object names in that file.')

    # Parse command-line arguments as a dict, with arguments groups stored in separate sub dictionaries
    args = parse_args_as_dict(parser)
    # args.test = parse_dut_id(args.target)
    # print('args=', args)
    # -------------------------------
    # Load configuration file
    # -------------------------------
    config = load_yaml_config(args.pop('yaml', None))  # Load YAML config
    # config = merge_dict(config, args)     # Add command line arguments to config
    # config['test'] = parse_dut_id(' '.join(config['target']))

    logger = setup_logging(**args.get('cli_logging', {}))
    #######################################
    # Power supply array
    #######################################
    # ps_array_params = merge_dict(config.get('power_supply_array', {}), args['cli_power_supply_array'])
    ps_array_params = args['cli_power_supply_array']
    power = ps_array_params.pop('power')
    ps_array = PSArray(**ps_array_params)  # Create Power supply array
    if power is not None:
        if power.lower() == 'on':
            ps_array.unlock()
            ps_array.power_on()
        elif power.lower() == 'off':
            ps_array.unlock()
            ps_array.power_off()
        elif power.lower() == 'cycle':
            ps_array.unlock()
            ps_array.power_cycle()
        else:
            raise AttributeError("Invalid power supply state '%s'" % power)

    #######################################
    # FPGA array
    #######################################
    config_fpga_array_params = (config.get('fpga', {}).get('fpga_array_params', {})
                                or config.get('fpga_array_params', {}))
    cli_fpga_array_params = {k: v for k, v in args['cli_fpga_array'].items() if v is not None}
    fpga_array_params = merge_dict(config_fpga_array_params, cli_fpga_array_params)
    fpga_array = FPGAArray(**fpga_array_params)  # Create FPGA array

    return config, fpga_array, Ccoll([]), ps_array


if __name__ == '__main__':
    (config, ca, nodes, ps) = create_fpga_array()

    # -------------------------------
    # Bring some key objects into the current namespace to facilitate interactive use
    # -------------------------------
    hwm = ca.hwm
    ib = ca.ib
    c = ca.ib
    ic = ca.ic
