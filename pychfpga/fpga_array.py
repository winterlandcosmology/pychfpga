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
import asyncio
from collections import defaultdict

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
from pychfpga.common.perf import Perf

from pychfpga.hardware import HardwareMap, Motherboard, Crate


class FPGAArray(object):
    """Represents and operates on an array of FPGA motherboards."""

    # Map the string to the function that computes the FPGA IP address from the ARM IP address
    FPGA_IP_ADDR_FN_TABLE = {
        '(a,b,3,d)': lambda a, b, c, d: (a, b, 3, d),
        '(a,b,c+1,d)': lambda a, b, c, d: (a, b, c + 1, d),
    }

    # -------------------------------------------------------------------------
    # Array-wide operations (see :class:`pychfpga.common.array_methods.ArrayMethods`).
    #
    # Each Motherboard caches its own set of array-method provider classes in
    # ``Motherboard._array_method_classes`` (a ``frozenset``). ``FPGAArray``
    # caches the intersection of those per-motherboard sets, instantiates one
    # provider for each common class, and exposes the providers' public methods
    # through ``_array_methods`` as ``{method_name: bound_provider_method}``.
    #
    # Class-body defaults below ensure the attributes are always defined,
    # even on ``object.__new__(FPGAArray)`` code paths (tests, unpickle).
    # ``__getattr__`` still guards on ``_ready`` before dispatching array methods.
    # -------------------------------------------------------------------------

    # Define defaults for the following class attributes to prevent
    # recursiveness in __getattr__ in case it tries to access them before they
    # get shadowed by the initialized instance attributes.
    _array_methods = {}
    _array_method_class_sets = ()  # tuple of each motherboard's provider-class frozenset
    _array_method_instances = {}
    _ready = False

    def get_array_methods(self):
        """Return array-wide methods available on every motherboard.

        The result is a dict of ``{method_name: bound_provider_method}``.
        Provider methods have their provider instance already bound, and the
        provider keeps a reference to this :class:`FPGAArray`.
        """
        current = tuple(mb._array_method_classes for mb in (self.ib or ()))
        if self._array_method_class_sets == current:
            return self._array_methods

        common_classes = frozenset.intersection(*current) if current else frozenset()
        array_method_instances = {
            cls: self._array_method_instances.get(cls) or cls(self)
            for cls in common_classes
        }
        array_methods = {}

        for provider in array_method_instances.values():
            for name, method in provider.get_array_methods().items():
                if name in array_methods:
                    raise AttributeError(
                        f"Array-wide method {name!r} is defined by more than "
                        "one ArrayMethods provider")
                array_methods[name] = method

        if current:
            union = frozenset().union(*current)
            partial_classes = union - common_classes
            if partial_classes:
                self.logger.warning(
                    f"Array-wide method provider(s) {partial_classes} are not "
                    "defined on every motherboard")

        self._array_methods = array_methods
        self._array_method_instances = array_method_instances
        self._array_method_class_sets = current
        return self._array_methods

    def get_array_method(self, name):
        """Return an array-wide method bound to its provider instance.

        Parameters:
            name (str): The name of the array-wide method to look up.

        Raises:
            AttributeError: If ``name`` is not an array-wide method available
                on every motherboard.
        """
        array_methods = self.get_array_methods()
        if name in array_methods:
            return array_methods[name]
        raise AttributeError(
            f'{type(self).__name__!r} object has no array-wide method {name!r}'
        )

    def __getattr__(self, name):
        """Dispatch array-wide methods. See :meth:`get_array_method`."""
        if not self._ready:
            raise RuntimeError(
                "FPGAArray is not initialized, probably because it was not await'ed; use "
                "`ca = await FPGAArray(...)`"
            )
        return self.get_array_method(name)

    def __dir__(self):
        """Return the normal attributes plus every array-wide method name."""
        return sorted(set(super().__dir__()) | set(self.get_array_methods()))

    def get_mode(self):
        """Return the operational FPGA mode programmed on the boards.
        """
        return getattr(self, 'mode', None)

    def __init__(
            self,

            hwm=None,
            iceboards=[],
            icecrates=[],
            exclude_iceboards=[],
            ignore_missing_boards=False,

            subarrays=None,
            ping=True,
            mdns_timeout=2,
            platform_firmware_pathname=None,
            no_mezz=False,

            bitfile=None,
            prog=None,
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

            **kwargs):
        """ Create an FPGAArray; caller must ``await`` :meth:`init` to initialize hardware.

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

            platform_firmware_pathname (str): Optional path to platform ARM
                firmware used during network discovery. For CRS, ``None`` uses
                the default ``tcpipe_rtos.elf`` in the CRS ``arm_firmware`` folder.


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
                just a numeric 7 as a convenient shortcut), the board will use its
                platform-specific network discovery method to actively find boards
                that match the serial number.


            Note:

                Selecting boards by Crate serial number uses the platform-specific
                network discovery method for that crate type.

        """
        # Capture constructor args before any other locals are created; do not
        # run hardware init here. Caller must ``await`` this instance.
        init_kwargs = {k: v for k, v in locals().items() if k not in ('self', 'kwargs')}
        init_kwargs.update(kwargs)

        self._ready = False  # Indicate that the array is not initialized yet.
        self._init_args = ()
        self._init_kwargs = init_kwargs

    async def init(self):
        return await self

    def __await__(self):
        yield from self.init_async(**self._init_kwargs).__await__()  # create a generator that passes-on the init_async yields
        self._ready = True
        return self


    async def init_async(self, **kwargs):
        """Co-routine implementation of fpga_array initialization.

        Hardware-map / discovery parameters are consumed here. FPGA
        programming and open (with retry) run here. Remaining ``kwargs`` are
        forwarded to firmware ``array_init_async``. Constructor parameters
        are documented on :meth:`__init__`.
        """
        # Hardware-map / discovery params (not forwarded to array_init)
        hwm = kwargs.pop('hwm', None)
        iceboards = kwargs.pop('iceboards', [])
        icecrates = kwargs.pop('icecrates', [])
        exclude_iceboards = kwargs.pop('exclude_iceboards', [])
        ignore_missing_boards = kwargs.pop('ignore_missing_boards', False)
        subarrays = kwargs.pop('subarrays', None)
        ping = kwargs.pop('ping', True)
        mdns_timeout = kwargs.pop('mdns_timeout', 2)
        no_mezz = kwargs.pop('no_mezz', False)
        stderr_log_level = kwargs.pop('stderr_log_level', None)
        syslog_log_level = kwargs.pop('syslog_log_level', None)
        # Used here for platform firmware load; array_init_async no longer consumes it.
        platform_firmware_pathname = kwargs.pop('platform_firmware_pathname', None)
        # Used here for FPGA programming / open; not forwarded to array_init.
        bitfile = kwargs.pop('bitfile', None)
        prog = kwargs.pop('prog', None)
        udp_retries = kwargs.pop('udp_retries', 10)
        # Bound to locals for repeated use below; left in kwargs for array_init_async.
        mode = kwargs.get('mode')
        init = kwargs.get('init')
        if_ip = kwargs.get('if_ip')
        fpga_ip_addr_fn = kwargs.get('fpga_ip_addr_fn', '(a,b,3,d)')

        self.ib = []  # make sure repr() has always something
        self.ic = []
        # Instance shadows of the class-body defaults; ensures a fresh
        # per-array cache state even if the class attributes were ever
        # mutated in some test or debugging session.
        self._array_methods = {}
        self._array_method_class_sets = ()
        self._array_method_instances = {}

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
        self.logger.info(f'{self!r}:     platform_firmware_pathname = {platform_firmware_pathname}')
        self.logger.info(f'{self!r}:     exclude_iceboards = {exclude_iceboards}')
        self.logger.info(f'{self!r}:     bitfile = {bitfile}')
        self.logger.info(f'{self!r}:     prog = {prog}')
        self.logger.info(f'{self!r}:     init = {init}')
        self.logger.info(f'{self!r}:     no_mezz = {no_mezz}')
        self.logger.info(f'{self!r}:     sampling_frequency = {kwargs.get("sampling_frequency")}')
        # self.logger.info(f'{self!r}:     reference_frequency = {reference_frequency}')
        self.logger.info(f'{self!r}:     sync_method = {kwargs.get("sync_method")}')
        self.logger.info(f'{self!r}:     sync_source = {kwargs.get("sync_source")}')
        self.logger.info(f'{self!r}:     fpga_ip_addr_fn = {fpga_ip_addr_fn}')
        self.logger.info(f'{self!r}: ------------------------')
        self.perf = Perf()
        self.perf.start('hardware map', description='Parse HWM and build the board list')

        __main__._host_interface_ip_addr = if_ip

        # Convert the FPGA IP-setting function name to an actual function.
        # Result stays in kwargs so array_init_async receives the resolved callable.
        if fpga_ip_addr_fn in self.FPGA_IP_ADDR_FN_TABLE:
            kwargs['fpga_ip_addr_fn'] = self.FPGA_IP_ADDR_FN_TABLE[fpga_ip_addr_fn]
        else:
            valid_fn = ', '.join(f"'{fn}'" for fn in self.FPGA_IP_ADDR_FN_TABLE)
            raise AttributeError(
                f"Invalid 'fpga_ip_addr_fn' string '{fpga_ip_addr_fn}'. "
                f"Valid strings are  {valid_fn}")

        HardwareMap.clear_hardware_map()
        # Bind local ``motherboards`` / ``crates`` to the live ``HardwareMap``
        # instance registry lists. ``clear_hardware_map`` empties the lists in
        # place, so motherboards and crates created or deleted later (during
        # ``process_*_hwm`` / discovery) appear here automatically without any
        # re-binding. These are intentionally local: the externally visible
        # collections are the sorted ``self.ib`` / ``self.ic`` set below.
        motherboards = Motherboard.get_all_registered_instances()
        crates = Crate.get_all_registered_instances()
        # Fix up a few parameters for convenience
        # Add `iceboards'
        if isinstance(iceboards, (str, int)):
            iceboards = [iceboards]
        if iceboards:
            self.process_str_hwm(f"MGK7MB {' '.join(str(mb) for mb in iceboards)}")

        # Add `icecrates`
        # Make sure iceboards is a list
        if isinstance(icecrates, (str, int)):
            icecrates = [icecrates]
        if icecrates:
            self.process_str_hwm(f"MGK7BP16 {' '.join(str(cr) for cr in icecrates)}")

        # print(f'HWM before = {motherboards}')

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

        # ``motherboards`` is bound to the live Motherboard instance registry
        # earlier (right after ``clear_hardware_map``), so it already reflects
        # any boards added via ``process_str_hwm`` / ``process_list_hwm``.

        # If subarrays are specified, remove boards that are not in those subarrays
        if subarrays is not None:
            # print('Subarrays are: %r' % subarrays)
            for mb in list(motherboards):  # make a copy to be sure the list does not change during the loop
                if mb.subarray is not None and mb.subarray not in subarrays:
                    mb.delete_instance()
                    self.logger.debug(f"{self!r}: {mb} (subarray '{mb.subarray}') is not in the target subarray list {subarrays}. "
                                      f"It is removed from the YAML hardware map.")

        # If ping=1, remove boards that do not respond to tuber pings
        ping_timeout = 3
        missing_boards = []
        if ping:
            boards_to_ping = [mb for mb in motherboards if mb.hostname]
            if boards_to_ping:
                self.perf.start('ping', description='TCP-ping boards with explicit hostnames')
                # Find iceboards to ping. Use `as_dict` so mb_to_ping does not change as we delete boards from the hwm
                self.logger.info(f'{self!r}: Pinging {len(boards_to_ping)} Motherboards with explicit hostnames')
                # TCP-ping boards. Make is an asynchronous parallel call to all boards
                ping_results = await Ccoll(boards_to_ping).ping_async(timeout=ping_timeout)
                # self.logger.debug('%r: Ping results are %s' % (self, ping_results))
                for mb, ping_successful in zip(boards_to_ping, ping_results):
                    if ping_successful:
                        mb.hostname = socket.gethostbyname(mb.hostname)
                    else:
                        missing_boards.append(
                            f"{mb.hostname} (SN{mb.serial or '????'}, "
                            f"({mb.get_crate_number(default='?')},{mb.slot - 1 if mb.slot else '?'})")
                        self.logger.debug(f'{self!r}: Deleting {mb} from the YAML hardware map')
                        mb.delete_instance()
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
        #     added_mb = []
        #     for (model, hostname) in list(hw_table.iceboards):  # make a copy: we modify in-place
        #         if model is None or '.' in str(hostname):  # if it is actually a hostname. Might be an int serial
        #             # remove it. We'll be left with boards that require mDNS...
        #             hw_table.iceboards.remove((model, hostname))
        #             # ip_addr = socket.gethostbyname(hostname)  # convert hostname to IP address for faster Tuber access
        #             new_mb = IceBoar.get_unique_instance(hostname=hostname)
        #             added_mb.append(new_mb)

        #     # Explicitely listed boards must exist on the network
        #     if ping and added_mb:
        #         self.logger.info('%r: Pinging Iceboards with explicit hostnames '
        #                          'in the hardware description string' % self)
        #         # ping all boards concurrently
        #         ping_results = await asyncio.gather(*[mb.ping_async(timeout=ping_timeout) for mb in added_mb])
        #         # If *ANY* of the boards failed to respond, raise an exception
        #         if not all(ping_results):
        #             raise RuntimeError("%r: The following Iceboards could not be pigned: '%s'" % (
        #                 self,
        #                 ', '.join('%r (%s)' % (mb, mb.tuber_uri)
        #                           for mb, ping_result in zip(added_mb, ping_results))))

        #         # Resolve hostnames into IP addresses This is not concurrent,
        #         # unfortunately... this is why we checked ping first,
        #         # otherwise it blocks for a long time
        #         self.logger.info('%r: Resolving IP addresses of Iceboards that passed the ping test' % self)
        #         t0 = time.time()
        #         for mb, ping_result in zip(added_mb, ping_results):
        #             if ping_result:
        #                 mb.hostname = socket.gethostbyname(mb.hostname)
        #         self.logger.info('%r: Finished resolving IP addresses. It took %s seconds' % (self, time.time() - t0))

        ###########################################################################
        # Network discovery of boards and crates specified by model/serial number only
        ###########################################################################
        # Network discovery adds boards/crates to the hardware map and sets their key attributes based on the available information. The discovery method is platform-specific. 

        discovery_groups = defaultdict(lambda: dict(boards=[], crates=[]))
        # We search for motherboards that are specified by serial number but have no hostname
        # A generic Motherboard (empty part_number) indicates we look for any part number ('*').
        for mb in motherboards:
            if mb.serial and not mb.hostname:
                discovery_fn = mb.get_network_discovery_function()
                discovery_groups[discovery_fn]['boards'].append((mb.part_number or '*', mb.serial))

        # Search for boards on crates that have a serial number but no assigned boards (cr.slot is empty).
        # If the crate has boards already assigned, we assume they are all assigned and we don't need to search
        # for more boards on this crate.
        for cr in crates:
            if cr.serial and not cr.slot:
                discovery_fn = cr.get_network_discovery_function()
                discovery_groups[discovery_fn]['crates'].append((cr.part_number or '*', cr.serial))

        # Remove iceboards with wildcard serial numbers. Those will be re-added to the hardware map by the network discovery function.
        for mb in motherboards:
            if mb.serial == '*':
                mb.delete_instance()
        if discovery_groups:
            self.perf.start('network discovery', description='Find boards and crates by serial number')
            self.logger.info(f'{self!r}: Discovering Motherboards and Crates specified by serial number')
            explicit_board_serials = set()
            for targets in discovery_groups.values():
                for _model, serial in targets['boards']:
                    if serial != '*':
                        explicit_board_serials.add(str(serial))
            for discovery_async_fn, targets in discovery_groups.items():
                mb_to_discover = targets['boards']
                cr_to_discover = targets['crates']
                if not mb_to_discover and not cr_to_discover:
                    continue
                self.logger.info(f'{self!r}:         Calling network discovery function: {discovery_async_fn}')
                self.logger.info(f'{self!r}:         Motherboards to find: {mb_to_discover}')
                self.logger.info(f'{self!r}:         Crates to find: {cr_to_discover}')
                self.print_flush()
                # `inter_reply_timeout=0.5` preserves the historical mDNS
                # retry behavior; platform hooks that do not use it can ignore it.
                await discovery_async_fn(
                    boards=mb_to_discover,
                    crates=cr_to_discover,
                    timeout=mdns_timeout,
                    inter_reply_timeout=0.5)
            # if explicit_board_serials:
            #     from pychfpga.hardware.crs.crs_discovery import serials_equal
            #     for mb in list(motherboards):
            #         if not mb.serial or mb.serial == '*':
            #             continue
            #         if not any(serials_equal(mb.serial, target) for target in explicit_board_serials):
            #             self.logger.warning(
            #                 f'{self!r}: Removing {mb} from hardware map '
            #                 f'(not among requested serials {sorted(explicit_board_serials)})')
            #             mb.delete_instance()

        ########################################################
        # Establish communication with the motherboards
        ########################################################
        # At this point all the motherboards should have a hostname. We need
        # to establish communication with them to self-discover the missing
        # serial/slot/crate information.


        self.logger.debug(f'{self!r}: Hardware map so far:')
        for mb in motherboards:
            self.logger.debug(f'    {mb}(hostname={mb.hostname}, serial={mb.serial}, slot={mb.slot})')

        # Set the platform firmware on the motherboards that have a hostname
        # We separate the motherboards by platform class, then call the platform-specific firmware set function for each class with the list of boards belonging to that class.
        self.perf.start('open platform', description='Load platform firmware and open TCPipe')
        platform_firmware_groups = dict()
        for mb in motherboards:
            if mb.hostname:
                platform_firmware_groups.setdefault(mb.__class__, []).append(mb)
        platform_names = ', '.join(f'{cls.__name__}' for cls in platform_firmware_groups.keys())
        self.logger.debug(f'{self!r}: Loading platform firmware on the following motherboards as needed: {platform_names}')

        for board_cls, boards in platform_firmware_groups.items():
            await board_cls.set_platform_firmware(
                boards=boards,
                firmware_pathname=platform_firmware_pathname)

        if motherboards:
            self.logger.info(f'{self!r}: Establishing communication with the motherboards')
            t0 = time.time()
            mbs_with_hostname = Ccoll(mb for mb in motherboards if mb.hostname)
            if mbs_with_hostname:
                result = await mbs_with_hostname.open_platform_async()
                self.logger.info(f'{self!r}: Connection with {len(result)} Motherboard established. '
                                 f'It took {time.time() - t0:.3f} seconds')

        ########################################################################################
        # Resolve missing serial/crate/slot info through the motherboard's on-board processor
        ########################################################################################
        # Complete serial, crate and slot information on Motherboard that miss
        # that information by talking directly to the ARM
        # (i.e without using mDNS).
        self.perf.start('board discovery', description='Resolve serial, slot, and crate from the boards')
        mb_without_serial = [mb for mb in motherboards if mb.hostname and mb.serial is None]
        if mb_without_serial:
            t0 = time.time()
            self.logger.info(f'{self!r}: Auto-Discovering the serial number of the Motherboards with known hostnames')
            ad_boards = ', '.join(mb.hostname for mb in mb_without_serial)
            self.logger.debug(f'{self!r}: Serial Auto-discovery is performed on the following boards: {ad_boards}')
            # concurrently resolve serials
            await Ccoll(mb_without_serial).discover_serial_async()
            # self.logger.info('%r: Got all discover_serial futures after %f seconds' % (self, time.time() - t0))
            # [mb.discover_serial.async() for mb in mb_without_serial]
            self.logger.debug(f'{self!r}: Finished Auto-Discovering serial number for Motherboards. '
                             f'Took {time.time() - t0:.3f} seconds.')

        if discover_slot:
            # Find all boards with hostname for slot number check. We re-check
            # the slot number of all boards, wether or not they already have
            # one, so we can issue a warning if they differ.
            mb_without_slot = [mb for mb in motherboards if mb.hostname]
            if mb_without_slot:
                self.logger.info(f'{self!r}: Auto-Discovering & validating the slot numbers '
                                 f'for {len(mb_without_slot)} Motherboards with known hostnames...')
                t0 = time.time()
                await Ccoll(mb_without_slot).discover_slot_async()
                self.logger.info(f'{self!r}: Finished Auto-Discovering slot numbers for Motherboards. '
                                 f'Took {time.time() - t0} seconds.')

        self.logger.debug(f'{self!r}: Hardware map so far:')
        for mb in motherboards:
            self.logger.debug(f'    {mb}(hostname={mb.hostname}, serial={mb.serial}, slot={mb.slot})')

        if discover_crate:
            # select boards that do not have a crate, or ones that have a generic crate (no part number)
            mb_without_crate = [mb for mb in motherboards if mb.hostname and (not mb.crate or not mb.crate.part_number)]
            if mb_without_crate:
                t0 = time.time()
                hostnames = ', '.join(mb.hostname for mb in mb_without_crate)
                self.logger.info(f'{self!r}: Auto-Discovering crate information '
                                 f'for Motherboards with known hostnames: {hostnames}')
                await Ccoll(mb_without_crate).discover_crate_async()
                self.logger.info(f'{self!r}: Finished Auto-Discovering crate serial numbers. '
                                 f'Took {time.time() - t0} seconds.')

        ###########################################################################
        # Exclude boards
        ###########################################################################
        for mb in motherboards:
            if mb.serial and exclude_iceboards:
                int_serial = self._to_integer(mb.serial)
                if mb.serial in exclude_iceboards or int_serial in exclude_iceboards:
                    mb.delete_instance()
                    self.logger.info(
                        f'{self!r}: Removing Motherboard {mb} based on '
                        f'exclusion list: {exclude_iceboards}')

        ###########################################################################
        # Cleanup hardware map
        ###########################################################################
        used_icecrates = {mb.crate for mb in motherboards}
        unused_icecrates = set(crates) - used_icecrates
        for cr in unused_icecrates:
            self.logger.warning(f'{self!r}: Removing unused Crate {cr}'
                                f'(part_number={cr.part_number}, serial={cr.serial}, crate_number={cr.crate_number})')
            cr.delete_instance()

        unresolved_iceboards = [mb for mb in motherboards if not mb.hostname]
        if unresolved_iceboards:
            for mb in unresolved_iceboards:
                self.logger.info(f'{self!r}: Unresolved {mb}'
                                 f'(part_number={mb.part_number}, serial={mb.serial}, hostname={mb.hostname})')
            raise RuntimeError(f'Unresolved motherboards {unresolved_iceboards}')

        self.logger.info(f'{self!r}: Hardware map is complete')

        self.logger.debug(f'motherboards={motherboards}')

        #################################
        # Resolve crate slot mapping
        #################################

        # Fill the slot information in all crates
        # for mb in motherboards:
        #     if mb.crate and mb.slot and mb.crate.slot[mb.slot] is not mb:
        #         self.logger.warning(f'{self!r}: Had to assign {mb} to slot {mb.slot} of crate {mb.crate}')
        #         mb.crate.slot[mb.slot] = mb

        #################################
        # Set the crate numbers
        #################################
        # ... using whatever map we could determine from the parameters

        # self.set_crate_numbers(crate_number_map, strict=False)
        # print('Crates:')
        # for cr in crates:
        #     print(f'{cr}(serial={cr.serial}, crate_number={cr.crate_number}')

        #################################
        # Check if all the hardware we wanted is present
        #################################
        # # List all the crates we know about along with their model/serial tuple
        # # Format: {icecrate_object : (model, integer_serial),...}
        # current_crates = {c: (c.part_number, self._to_integer(c.serial)) for c in crates}

        # # Find if explicitely requested crates were not found
        # missing_crates = [(cr.part_number, cr.serial) for cr in crates
        #                   if not cr.slot or not all(mb.hostname for mb in cr.slot.values())]
        # if missing_crates:
        #     raise RuntimeError(
        #         '%r: The following crates are missing: %s'
        #         % (self, ', '.join('%s SN%s' % (model, serial)
        #                            for (model, serial) in missing_crates)))

        # Check for missing boards in explicitely-specified crates
        missing_slots = {
            (cr.part_number, cr.serial, cr.crate_number): set(range(1, cr.NUMBER_OF_SLOTS + 1)) - set(cr.slot)
            for cr in crates}
        if any(missing_slots.values()):
            missing_slots_str = '\n'.join(
                f"    Crate #{number} ({model} SN{serial}): slots {', '.join((str(s) for s in slots))}"
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
        if motherboards and not no_mezz:
            self.perf.start('mezzanines', description='Discover mezzanines on each motherboard')
            self.logger.info(f'{self!r}: Discovering Mezzanines...')
            # make sure we see the previous prints right away so we have a better feeling of what is happening
            self.print_flush()
            await Ccoll(motherboards).discover_mezzanines_async()
        for mb in motherboards:
            mezz_info = ','.join(f'{i}:{m.iceboard}' for i, m in mb.mezzanine.items())
            self.logger.debug(f"Mezzanines after discovery {mb}, {mezz_info}")

        def get_mezz_name(mb, mezz_number):
            m = mb.mezzanine.get(mezz_number, None)
            # return '%s_SN%s' % (m.__ipmi_part_number__, m.serial) if m else '-'
            return f'SN{m.serial}' if m else '-'

        ################################
        # Update generic Motherboard objects with firmware-specific ones
        ################################
        # For now, we assume that all the boards boards with two MGADC08
        # boards are running the chFPGA firmware variant.
        self.logger.debug(f'Before reassignment, motherboards={list(motherboards)}')
        for cr in crates:
            self.logger.debug(f'{cr}(serial={cr.serial}, crate_number={cr.crate_number}')
        # for i, mb in enumerate(list(motherboards)):  # use list() so we can modify self.ib in the loop.
        #     # print('Board %r has mezzanines %s. Is instance of chFPGA_controller: %s'
        #     #       % (mb, ','.join('%i:%s' % (k, m.part_number)
        #                for k,m in mb.mezzanine.items()), isinstance(mb, chFPGA_controller)))
        #     if all(m.part_number == FMCMezzanine_MGADC08.part_number for m in mb.mezzanine.values()
        #            if m) and not isinstance(mb, chFPGA_controller):
        #         new_mb = mb.update_instance(new_class=chFPGA_controller)
        #         self.logger.debug(f'Replaced {mb!r} with {new_mb!r}')
        #         self.logger.debug(f'Mezzanines after {new_mb}, {new_mb.mezzanine}')

        #################################
        # Check the hardware map for generic objects
        #################################
        # print(f'Icecrates are: {any(c.part_number for c in crates)}')

        # Check if there are any crate without part number. We use a list
        # comprehension in case any has been overriden with np.any in a pylab
        # session, which does not work with generators.
        if any([not c.part_number for c in crates]):
            raise RuntimeError('There are generic motherboards left in the hardware map')
        # Same for Motherboards
        if any([not i.part_number for i in motherboards]):
            raise RuntimeError('There are generic crates left in the hardware map')

        #################################
        # Create self.ib and self.ic
        #################################

        # Query the hardware map for all iceboards and icecrates
        # use outerjoin in case there is no crate
        self.ib = sorted(motherboards, key=lambda i: (i.get_crate_number(default=0), i.slot or 0))
        self.ic = sorted({i.crate for i in motherboards if i.crate and i.crate.part_number},
                         key=lambda c: c.crate_number or 0)

        # Courtesy warning
        if not self.ic:
            self.logger.debug(f'{self!r}: There are no crates or backplanes in the hardware map')

        #################################
        # Print the Motherboard table
        #################################

        self.logger.debug(f'New motherboards={motherboards}')
        for mb in motherboards:
            crate_info = (f"{mb.crate}(serial={mb.crate.serial}, "
                          f"crate_number={mb.get_crate_number()})"
                          if mb.crate else None)
            self.logger.debug(f"{mb}, crate={crate_info}")

        n_mezz = max(mb.NUMBER_OF_FMC_SLOTS for mb in self.ib)
        if n_mezz:
            self.print_iceboard_table(
                func = lambda mb: '\n'.join(get_mezz_name(mb, mezz_number+1)  for mb in self.ib for mezz_number in range(n_mezz) if mezz_number < mb.NUMBER_OF_FMC_SLOTS),
                row_labels= '\n'.join(f'Mezz{mezz_number}' for mezz_number in range(n_mezz)),
                add_serial=True)
        else:
            self.print_iceboard_table(add_serial=True)

        self.print_flush()

        # Store as Ccoll collections to allow easy parallel operations
        self.ib = Ccoll(self.ib)
        self.ic = Ccoll(self.ic)

        # We have our final hardware map. Update the `repr` caches and array method lists
        for mb in self.ib:
            mb.update_array_method_classes()
            mb.set_cache()


        self.print_flush()

        self.logger.info(f'{self!r}: Done creating {self!r}')

        if mode:
            if init is None:
                init = 3
            if prog is None:
                prog = 2
        else:
            init = 0
        kwargs['init'] = init

        # Store the operational FPGA mode for the array
        self.mode = mode


        if self.ib:
            # try programming the FPGA and establishing communicaiton with it up to 3 times
            max_trials = 3
            for trial in range(1, max_trials + 1):

                if init >= 1:
                    self.logger.info(f'{self!r}: Configuring FPGAs...')
                    self.perf.start('FPGA programming', description='Init Level 1/prog: Upload bitstreams')
                    if prog:
                        await self.ib.set_fpga_bitstream_async(mode, force=(prog > 1), bitfile_override=bitfile)
                else:
                    break # nothing to do, exit the trial loop

                if init >= 2:
                    self.perf.start('FPGA open', description='Init level 2: Open MMI and PLL_SYNC')
                    try:
                        await self.ib.open_fpga_async(
                                udp_retries=udp_retries,
                                fpga_ip_addr_fn=kwargs.get('fpga_ip_addr_fn'),
                                interface_ip_addr=if_ip,
                                irigb_source=kwargs.get('sync_source'))
                        break  # it worked, exit the trial loop
                    except IOError:
                        self.logger.warning(f'{self!r}: Error while initializing core firmware on trial {trial}/{max_trials}.')
                        if trial == max_trials:
                            raise IOError(
                                f'{self!r}: Unable to initializing FPGA core firmware '
                                f'after {trial} trials. Giving up.')
                        else:
                            prog = 2  # force reprogramming the FPGA on next trial
                            self.logger.warning(f'{self!r}: Reprogramming FPGA and trying again.')
                else:
                    break # We skipped this step, so let's assume it worked, exit the trial loop

        array_init = self.get_array_methods().get('array_init_async')
        if array_init is not None:
            await array_init(**kwargs)
        self.perf.stop()
        self.logger.info(f'{self!r}: Init timing\n{self.perf.summary()}')
        self.logger.info(f'{self!r}: *** Initialization completed in {self.perf.get_delay()}')

        self.print_flush()

        #################################
        # Completed array creation and initialization
        #################################

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
                raise ValueError(f'crate number is not an integer in entry {string}')
        else:
            raise RuntimeError('Multiple crate numbers were specified in entry:')
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
            raise RuntimeError(f"Crates model and serial number must be separated by one (and only one) underscore (e.g. MGK7BP16_023). Got '{sn}'")

        try:
            sn = int(sn)
        except (ValueError, TypeError):
            pass

        return (model, sn, cn)

    @staticmethod
    def _build_crate_id(model, serial):
        if isinstance(serial, int):
            serial = f'{int(serial):03}'
        return f'{model}_SN{serial}'

    def print_table(
            self,
            data=None,
            row_labels=None,
            col_labels=None,
            corner_label=None,
            row_keys=None,
            col_keys=None,
            max_width=180,
            line_sep=False):
        """Print a formatted table where ``data`` is a list of columns."""

        if col_keys is None:
            if isinstance(col_labels, dict):
                col_keys = list(col_labels.keys())
            elif isinstance(data, dict):
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
                if block_width <= max_width:
                    block_col_keys.append(remaining_col_keys.pop(0))
                    block_col_width.append(width)
                else:
                    break

            line_format = (f'| %-{int(row_labels_width)}s'
                           + ' | '
                           + ' | '.join(f'%-{int(width)}s' for width in block_col_width)
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

            if not line_sep:
                print(line_sep_str)

            print()

    def print_iceboard_table(self, func=None, row_labels=None, grid=False, add_serial=True):
        """Print a board/crate summary table using only hardware-map metadata."""

        if not len(self.ib):
            print('[ There are no Motherboards in the hardware map ]')
            return

        if func:
            if isinstance(func, dict):
                data = func
            elif hasattr(func, 'async_map'):
                data = dict(zip(self.ib, func.async_map(self.ib)))
            else:
                data = {mb: func(mb) for mb in self.ib}
        else:
            data = {mb: '' for mb in self.ib}

        iceboards = list(data.keys())

        if row_labels is None:
            row_labels = ''
        if isinstance(row_labels, str):
            row_labels = [row_labels]

        orphan_iceboards = [mb for mb in iceboards if not mb.crate or not mb.crate.serial]
        corner_label = 'Standalone\nMotherboards'
        col_labels = [f'Virt. slot {mb.slot}\n{mb.part_number}_SN{mb.serial}\n{mb.hostname}' for mb in orphan_iceboards]
        table = []
        for mb in orphan_iceboards:
            cell = data[mb] if data else ''
            table.append([cell])
        if not any(table):
            table = []
        if table:
            self.print_table(
                table, row_labels=row_labels, col_labels=col_labels,
                corner_label=corner_label, line_sep=grid)

        valid_crates = list({mb.crate: None for mb in iceboards if mb.crate and mb.crate.serial}.keys())

        for crate in valid_crates:
            corner_label = f'{crate.get_string_id()}\nCrate #{crate.crate_number}'
            slot_range = list(range(1, crate.NUMBER_OF_SLOTS + 1))
            col_labels = [f'{int(s)}' for s in slot_range]

            if add_serial:
                for i, slot in enumerate(slot_range):
                    col_labels[i] += (f'\nSN {crate.slot[slot].serial}') if slot in crate.slot else '\n-'
            if add_serial:
                for i, slot in enumerate(slot_range):
                    col_labels[i] += (f'\n{crate.slot[slot].hostname}') if slot in crate.slot else '\n-'

            table = []
            for slot in slot_range:
                if slot in crate.slot.keys():
                    cell = data[crate.slot[slot]] if data else ''
                else:
                    cell = '-'
                table.append([cell])
            if table:
                self.print_table(
                    table, row_labels=row_labels, col_labels=col_labels,
                    corner_label=corner_label, line_sep=grid)

    def print_flush(self):
        """ Make sure that the test sent previously to stdout shows immediately on the console.
        """
        sys.stdout.flush()

    def __repr__(self):
        """ Short string representing this object and suitable to use as a tag in a syslog entry"""
        # ``self.ib`` / ``self.ic`` are populated near the end of ``init_async()``;
        # before that point we fall back to the live ``HardwareMap`` registry
        # so ``__repr__`` is still useful in early-init log messages.
        # mb = self.ib or Motherboard.get_all_registered_instances() or []
        # cr = self.ic or Crate.get_all_registered_instances() or []
        # return f'{self.__class__.__name__}({len(mb)}_boards,{len(cr)}_crates)'
        return f'{self.__class__.__name__}()'

    def get_hwm_info(self):
        string = f'{self.__class__.__name__} object with the following hardware map:\n'
        for i in self.ib:
            mezz = [f'{m.part_number} SN{m.serial}' if m else 'None'
                    for m in [i.mezzanine.get(1, None), i.mezzanine.get(2, None)]]
            string += f'   Crate SN{i.crate.serial if i.crate else None}, slot {int(i.slot):2}: Motherboard SN{i.serial} at {i.hostname} (ping ={i.ping()}), Mezz1={mezz[0]}, Mezz2={mezz[1]}\n'
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
        for cr in self.ic:
            hwm.append(dict(
                type='crate',
                model=cr.part_number,
                serial=str(cr.serial),
                crate_number=cr.crate_number,
                iceboards=[
                    dict(
                        slot=mb.slot,
                        model=mb.part_number,
                        serial=str(mb.serial)) for mb in cr.slot.values()
                ]
            ))
        for mb in self.ib:
            hwm.append(dict(
                type='iceboard',
                model=mb.part_number,
                serial=str(mb.serial),
                mezzanines=[
                    dict(
                        slot=i,
                        model=m.part_number if m else None,
                        serial=str(m.serial) if m else None)
                    for i, m in enumerate((mb.mezzanine.get(1, None), mb.mezzanine.get(2, None)))
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

        # First pass: check 1) if the class names are valid and 2) create the crate instances
        for hwm_entry in hwm:
            params = dict(hwm_entry)  # make a copy, we'll modify it below
            class_name = params.pop('class')  # extract the class name entry

            # Resolve the class name against the Motherboard/Crate manifests,
            # lazily importing the corresponding platform package on first use.
            crate_cls = Crate.get_class_by_name(class_name)
            mb_cls = Motherboard.get_class_by_name(class_name) if crate_cls is None else None
            if crate_cls is None and mb_cls is None:
                valid_class_names = ', '.join(
                    Motherboard.get_supported_class_names()
                    + Crate.get_supported_class_names())
                raise ValueError(f"Unknown class name '{class_name}' in a list-based hardware map. "
                                 f"Valid class names are: {valid_class_names}")
            if crate_cls is not None:
                # Create the class: it will be registered in the class registry for future use
                # The crate can have a serial number, crate number, or both
                Crate.get_unique_instance(new_class=crate_cls, **params)

        # Second pass: Create the IceBoards, and link them to the crates
        for hwm_entry in hwm:
            self.logger.debug(f'Processing hwm entry {hwm_entry}')

            params = dict(hwm_entry)  # make a copy
            class_name = params.pop('class')
            mb_cls = Motherboard.get_class_by_name(class_name)
            if mb_cls is not None:
                self.logger.debug(f"process_list_hwm: Calling get_unique_instance(new_class={class_name}, {params}")
                mb = Motherboard.get_unique_instance(new_class=mb_cls, **params)
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
                    mb = current_class.get_unique_instance(hostname=hostname, slot=slot, crate_number=crate_number)
                elif allow_generic_motherboard:
                    mb = Motherboard.get_unique_instance(hostname=hostname, slot=slot, crate_number=crate_number)
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
                    mb = Motherboard.get_unique_instance(
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
            elif el[0] == '*':
                if not current_class:
                    raise RuntimeError('A part number must be specified before a wildcard serial number')
                obj = current_class.get_unique_instance(serial='*')

            else:  # otherwise, assume it is a part number
                # ``HardwareMap.get_class_by_part_number`` looks through every
                # hardware base class (Motherboard, Crate, ...), lazy-loads
                # the manifest on a registry miss, and raises a RuntimeError
                # if zero or multiple classes match the suffix.
                current_class = HardwareMap.get_class_by_part_number(el)

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
    #                      % (self, ', '.join(repr(cr) for cr in self.ic)))

    #     for cr in IceCrates:
    #         model = cr.part_number
    #         sn = cr.serial
    #         try:
    #             int_sn = int(cr.serial)
    #         except (ValueError, TypeError):
    #             int_sn = None

    #         new_crate_number = (crate_number_map.get((model, sn), None) or
    #                            crate_number_map.get((model, int_sn), None))

    #         #if not isinstance(new_crate_number, int):
    #         #    raise ValueError('%r: crate number %r is not an interger' % (self, new_crate_number))

    #         if new_crate_number is not None:
    #             cr.update_instance(crate_number = int(new_crate_number))
    #             self.logger.info('%r: Assigning crate number %r to crate %s'
    #                 % (self, new_crate_number, cr.get_string_id()))
    #         elif strict:
    #             raise RuntimeError('Cannot find a crate number for crate %s' % cr.get_string_id())
    #         else:
    #             self.logger.warning('%r: set_crate_number: Cannot find a crate number for crate %s'
    #                 % (self, cr.get_string_id()))


    def get_channel_ids(self):
        """ Returns the channel IDs of every board and channel in the array """
        return [cid for mb in self.ib for cid in mb.get_channel_ids()]

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
                valid_serials = ','.join(f"'{mb.serial}'" for mb in self.ib)
                raise RuntimeError(f'{self!r}: Invalid Board serial number {board}. '
                                   f'Valid serial numbers are {valid_serials}')
        elif isinstance(board, (tuple, list)) and len(board) == 2:
            matches = [mb for mb in self.ib if tuple(board) == mb.get_id()]
            if not matches:
                raise RuntimeError(f"board ID {board} did not match any board "
                                   f"in the array: {[mb.get_id() for mb in self.ib]}")
            elif len(matches) > 1:
                raise RuntimeError(f"board ID {board} Matched multiple "
                                   f"boards: {','.join(mb.get_string_id() for mb in matches)}")
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
        dict specified in `mb` , including wildcards.

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
                        raise RuntimeError(f"Unknown element {k} in motherboard selection item {mb}")
            else:
                raise ValueError(f'Unknown Motherboard selection format {mb}')

            # Convert the wildcard '*' into None
            crate_number = None if crate_number == '*' else crate_number
            slot_number = None if slot_number == '*' else slot_number
            lane_number = None if lane_number == '*' else lane_number

            # print('get_iceboard: looking for ', crate_number, slot_number)
            for mb in self.ib:
                crate, slot = mb.get_id()
                # print('   checking', mb_id)
                if (crate_number is None or crate_number == crate) and (slot_number is None or slot_number == slot):
                    # get the Motherboard's lanes. Create an entry with an emply list if it does not exist
                    lanes = iceboards.setdefault(mb, set())
                    if lane_type is not None:
                        if lane_type in ('channel', 'chan'):
                            valid_lanes = mb.get_channels()
                        elif lane_type in ('gpu', 'mb_qsfp'):
                            valid_lanes = mb.GPU.get_lane_numbers()
                        elif lane_type == 'bp_pcb':
                            valid_lanes = mb.BP.get_lane_numbers('pcb')
                        elif lane_type == 'bp_qsfp':
                            valid_lanes = mb.BP.get_lane_numbers('qsfp')
                        else:
                            raise AttributeError(f'Unknown lane type {lane_type}')
                        if lane_number is None:
                            lanes.update(valid_lanes)
                        elif lane_number not in valid_lanes:
                            raise ValueError(f'Invalid lane number {lane_number} for lane type {lane_type}. Valid values are {valid_lanes}')
                        else:
                            lanes.add(lane_number)
        if lane_type is None:
            return list(iceboards.keys())
        else:
            return iceboards


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
    parser.add_argument('--arm_firmware_pathname', type=str, help="Path to platform ARM firmware used during network discovery. For CRS, defaults to the tcpipe_rtos_rtos_rtos.elf file in the CRS arm_firmware folder.")
    parser.add_argument('--prog',            type=int, nargs='?', const=1,
                        help='Overrides the FPGA programming method. Is meaningful only if --mode is specified'
                        '--prog 0: do not program the FPGA (even if --mode is specified), '
                        '--prog or prog 1: programs the FPGA only if not already programmed (requires --mode). '
                        '--prog 2: always program the FPGA (requires --mode).')
    parser.add_argument('-b', '--bitfile',   type=str, help='Folder in which to search for FPGA bitstream or full path to a bistream to use explicitely (mode must still be specified)')
    parser.add_argument('-m', '--mode',      type=str, help="Operational mode ('shuffle16', 'shuffle256', etc.). When specified, the FPGA is programmed with the proper firmware bitstream (unless blocked with --prog 0) and the mode is initialized (unless blocked with --init 0). If not specified, only a connection to the platform is established")
    parser.add_argument('--init',      type=int, nargs='?', const=None,
                        help='Overrides FPGA initialization level'
                        '--init 0: only connect to platform'
                        '--init 1: program FPGA (requires --mode or --bitfile)'
                        '--init 2: establish connection with FPGA firmware and create firmware objects'
                        '--init 3: initialize the desired operational mode (requires --mode)')

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
        from pychfpga.hardware.Agilent_N5764A import AgilentN5764A
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

    - arm_firmware_pathname: Path to platform ARM firmware used by
        network discovery. For CRS, None uses the default tcpipe_rtos_rtos.elf in the
        CRS arm_firmware folder.

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
            raise AttributeError(f"Invalid power supply state '{power}'")

    #######################################
    # FPGA array
    #######################################
    config_fpga_array_params = (config.get('fpga', {}).get('fpga_array_params', {})
                                or config.get('fpga_array_params', {}))
    cli_fpga_array_params = {k: v for k, v in args['cli_fpga_array'].items() if v is not None}
    fpga_array_params = merge_dict(config_fpga_array_params, cli_fpga_array_params)
    # Constructor only stores args; hardware init requires await (CLI has no running loop).
    fpga_array = asyncio.run(FPGAArray(**fpga_array_params).init())

    return config, fpga_array, Ccoll([]), ps_array


if __name__ == '__main__':
    (config, ca, nodes, ps) = create_fpga_array()

    # -------------------------------
    # Bring some key objects into the current namespace to facilitate interactive use
    # -------------------------------
    mb = ca.ib
    c = ca.ib
    cr = ca.ic
