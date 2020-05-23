#!/usr/bin/python
"""
chime_array.py module. Defines the objects that represent and handles
operations one the whole array of CHIME ICE hardware.
"""
from __future__ import absolute_import

# Python Standard Library packages
import argparse
import logging
import time
import __main__
import os
import sys
import socket  # for gethostbyname()
from collections import OrderedDict
import pickle
import re
import datetime
import functools
import zlib
import base64

# PyPi packages
import numpy as np
import matplotlib.pyplot as plt
from tornado.netutil import Resolver
from tornado.ioloop import IOLoop
from tornado import gen
from tornado.gen import with_timeout, TimeoutError
from sqlalchemy import or_

# For development: delete all fpga modules so fresh ones will be reloaded
if getattr(__main__, '__reload__', False):
    print 'Clearing all pychfpga modules...'
    for n,m in sys.modules.items():
        if n.startswith('pychfpga'):
            del sys.modules[n]

# Local imports

# Automatically update the search path for absolute imports of pychfpga and
# its subpackages. We use absolute imports because 1) if both relative and
# absolute imports are made, then  modules are loaded multiple times and
# SQLAlchemy complains. 2) wa cannot access pychfpga subpackages if we run
# this module as a script because Python refuses to consider the script folder
# as a package.
try:
    import pychfpga
except ImportError:
    ch_acq_path = os.path.realpath(os.path.join(os.path.dirname(__file__), '..'))
    if ch_acq_path not in sys.path:
        sys.path.insert(0, ch_acq_path)


from pychfpga.core.icecore import Ccoll
from pychfpga.core.icecore import HardwareMap, HWMResource
from pychfpga.core.icecore import mdns_discover
from pychfpga.core.icecore import async, async_return, async_sleep, async_moment

from pychfpga.core.icecore import IceBoardPlus
from pychfpga.core.icecore_ext import IceCrateExt
from pychfpga.MGADC08 import MGADC08  # Import to make sure this Mezzanine is registered  so it can be discovered
from pychfpga.core.chFPGA_controller import chFPGA_controller
from pychfpga.Agilent_N5764A import AgilentN5764AHandler
# from pychfpga.gpu_node import GpuNodeHandler
from pychfpga import Metrics
from pychfpga import NameSpace, merge_dict
from pychfpga import load_yaml_config# import logging.handlers

from pychfpga.core.icecore.session import load_session as load_yaml

# Configure Tornado objects
Resolver.configure('tornado.netutil.ThreadedResolver', num_threads=20)

#####################################


class FPGABitstream(object):
    """ Helper object used to load and store a FPGA bitstream. You don't have
    to use it, but it makes the code look nicer"""
    bitstream = None

    def __init__(self, filename, auto_reload=True):
        self.filename = filename
        self.auto_reload = auto_reload
        if not self.auto_reload:
            self._load()

    def __str__(self):
        """ Return the bitstream as a string. """
        if self.auto_reload:
            try:
                self._load()
            except IOError:
                if not self.bitstream:
                    raise
        return self.bitstream

    def _load(self):
        with open(self.filename, 'rb') as file_:
            self.bitstream = file_.read()
        self.crc32 = zlib.crc32(self.bitstream) & 0xFFFFFFFF  # compute CRC32 of the data
        self.base64 = base64.b64encode(self.bitstream)

# Default ADC delays
ADC_DELAYS_MGK7MB_REV2_MGAC08_REV2 = {
    'valid': True,
    'sync_delays': (4, 5),
    0:  {'tap_delays': [16]*8,                           'sample_delay': 3, 'clock_delay': 0},  # CH0
    1:  {'tap_delays': [7]*8,                            'sample_delay': 3, 'clock_delay': 0},  # CH1
    2:  {'tap_delays': [22]*8,                           'sample_delay': 3, 'clock_delay': 0},  # CH2
    3:  {'tap_delays': [19]*8,                           'sample_delay': 3, 'clock_delay': 0},  # CH3
    4:  {'tap_delays': [15]*8,                           'sample_delay': 3, 'clock_delay': 0},  # CH4
    5:  {'tap_delays': [14, 13, 14, 14, 13, 14, 15, 14], 'sample_delay': 3, 'clock_delay': 0},  # CH5
    6:  {'tap_delays': [18]*8,                           'sample_delay': 3, 'clock_delay': 0},  # CH6
    7:  {'tap_delays': [17]*8,                           'sample_delay': 4, 'clock_delay': 0},  # CH7
    8:  {'tap_delays': [15, 17, 15, 18, 17, 14, 17, 15], 'sample_delay': 3, 'clock_delay': 0},  # CH8
    9:  {'tap_delays': [16]*8,                           'sample_delay': 4, 'clock_delay': 0},  # CH9
    10: {'tap_delays': [20]*8,                           'sample_delay': 3, 'clock_delay': 0},  # CH10
    11: {'tap_delays': [18]*8,                           'sample_delay': 3, 'clock_delay': 0},  # CH11
    12: {'tap_delays': [15]*8,                           'sample_delay': 3, 'clock_delay': 0},  # CH12
    13: {'tap_delays': [18]*8,                           'sample_delay': 3, 'clock_delay': 0},  # CH13
    14: {'tap_delays': [18]*8,                           'sample_delay': 3, 'clock_delay': 0},  # CH14
    15: {'tap_delays': [16]*8,                           'sample_delay': 3, 'clock_delay': 0}   # CH15
    }

ADC_DELAY_TABLE = ADC_DELAYS_MGK7MB_REV2_MGAC08_REV2 #ADC_DELAYS_REV2_SN0001 ## select the table corresponding to the FMC serial number

class FPGAArray(object):

    def __init__(self,

                 hwm=None,
                 iceboards=[], icecrates=[], mezzanines=[], exclude_iceboards=[],
                 crate_map={},
                 ignore_missing_boards=False,

                 subarrays=None,
                 ping=True,
                 mdns_timeout=2,
                 no_mezz=False,

                 bitfile=None,
                 prog=None,
                 open=None,
                 if_ip=None,
                 udp_retries=10,
                 fpga_ip_addr_fn='(a,b,3,d)',

                 # sampling_frequency=800e6,
                 # reference_frequency=10e6,
                 # data_width=4,

                 sync_method='distributed_time',
                 sync_source='bp_trig',
                 sync_master=None,
                 sync_master_time_source=None,
                 max_sync_time_difference=100,

                 mode=None,
                 frames_per_packet=2,
                 tx_power=None,
                 integration_period=None,
                 corner_turn_bad_links=None,
                 corner_turn_bin_priority=None,

                 stderr_log_level=None,
                 syslog_log_level=None,

                 ioloop=None,

                 **kwargs
                ):
        """ Create a hardware map describing CHIME hardware and optionally
        initialize the hardware.

        Parameters:

            --------------------Category: **Hardware map creation**--------------------

            hwm (str, list or HardwareMap): Describes the hardware
                map, which lists the IceBoards, IceCrates and Mezzanines
                that are present in the system and their relationship. The hardware
                map creation depending on the type of the `hwm` parameter.

                - *str* or *list of str* : A string or list of strings that describes the hardware to be
                  added to the hardware map in the format:

                  ``hwm := {Iceboard_descriptors | Icecrate_descriptors | mezzanine_descriptors} ...``

                  where

                  ``Iceboard_descriptors := {[MGK7]MB serial [serial] ... | hostname | ip_address}``

                  ``Icecrate_descriptors := {[MGK7]BP16 serial[:crate_number] [serial[:crate_number]] ...}``,

                  Autodiscovery is used as needed to complete the hardware map
                  (see notes below). See `parse_hw_string` for a description of
                  the syntax

                - *list of dict*: list of dicts that describe the hardware map elements to create.

                - *HardwareMap*: Fully formed hardware map, provided directly as a
                  HardwareMap database object. If a fully-formed hardware map is
                  provided, it will additionally be vetted by removing the
                  Iceboards that fail the `ping` test and do not meet the
                  ``subarray`` criteria.

            crate_map (dict): Maps crate numbers to (model, serial). Not
                needed if the crate numbers are already specified in the
                hardware map. Useful if the crates are auto-discovered and the
                crate number is not specified in the hardware description
                string.

            iceboards (list of str) : Iceboard to add to the hardware map,
                specified as an IP address, hostname, or serial number. The
                boards specified here are added to the hardware map specified
                in `hwm` parameter. When a serial number is used, the IceBoard
                model MGK7MB is implied. For example, specifying ``iceboards =
                "003 007"`` is equivalent to adding ``"MGK7MB 003 007"`` to
                the `hwm` parameter.

            exclude_iceboards (list of str) : Serial numbers of Iceboards to be
                excluded in case of autodiscovered boards. Can be useful to specify a
                crate but exclude a few boards.

            icecrates (list of str): Adds all the iceboards from the crates that have the
                serial numbers specified in the provided list of strings.

                Examples

                    ``icecrates='003'`` or ``icecrates=['003']`` will discover and select all boards from crate SN003

                    ``icecrates=['003', '004']`` will select boards from crates SN003 and SN004.

                    ``icecrates=[]`` will select all boards on the network

            ----------Category: **Hardware map filtering**----------

            subarrays : List of integers describing the subarrays to include in
                the default IceBoard set. If None, all
                Iceboards in the hardware map will be selected. Affects only the
                boards specified in the hardware map specified with the ``hwm`` parameter.

            ping : If ``ping=1``, The connection to Iceboards is checked
                by sending a dummy Tuber (http) request to their ARM processors. If a
                YAML-specified iceboards fails, it is simply removed from the
                ``hwm`` hardware map, but an exception is raised if a board listed
                explicitely fails. If ``ping`` is false, the presence of boards is not
                checked.

            ----------Category: **Configuration & initialization**----------

            bitfile (str): Filename of the bitfile used to program the FPGAs

            prog (int): If ``prog=1``, the FPGAs in the selected Iceboards will be
                configured only if they are not already configured with the same
                firmware. If ``prog=2``, they will always be reconfigured. If
                ``prog`` is 0, None or is not specified, the FPGAs are never configured.

            open (int): If ``open=1``, establish communication with the boards and
               initialize the firmware and software. If ``open`` is `None` or is not
               specified, the software and firmware is not initialized.

            if_ip (str): string corresponding to the IP address of adapter through
                which the connection to the FPGA will be established. If not
                specified, the system will assume that the FPGA is reached trough
                the same interface that reaches the ARM processor.


            udp_retries (int): Number of retries performed when UDP command
                packets sent to the FPGA do not receive a response.

            fpga_ip_addr_fn (str): Specifies how the FPGA IP address is
                determined. The valid modes are shown below, and should be
                types exactly without additional spaces. In those modes,
                ``a.b.c.d`` corresponds to the IP address of the IceBoard ARM
                processor.

                   - '(a,b,3,d)': Uses the IP address of the ARM but replaces the third byte by ``3``
                   - '(a,b,c+1,d)': Uses the IP address of the ARM but adds 1 to the third byte


            sync_method (str): Method used to synchronize (to sync) the data acquisition on an array of boards:

                'distributed_time',

            sync_source (str): Source of the signal that is used to synchronize each board

                'bp_trig',

            sync_master=None,

            sync_master_time_source=None,

            max_sync_time_difference (int): Maximum time difference, in nanoseconds, between the time of frame 0 of each board in an array.


            ----------Category: **Corner-turn engine parameters**----------

            mode (str): Operational mode of the Corner-turn engine. The following modes are supported:

                    - 'chan8': Corner-turn engine is mostly bypassed and raw 8-bit data from 8
                      channelizers is sent directly to the 8 CT-Engine outputs.

                    - 'chan4': Corner-turn engine is mostly bypassed and raw 8-bit data from 16
                      channelizers is sent directly to the 8 CT-Engine outputs.

                    - 'shuffle16': A corner-turn operation is applied only within the 16 channelizer
                      outputs of this board.

                    - 'shuffle256': The corner-turn operation is applies between 16 channelizers within
                      a board and between the 16 boards within a crate using the backplane PCB links.

                    - 'shuffle512': The corner-turn operation is applies between 16 channelizers within
                      a board, between the 16 boards within a crate using the backplane PCB links,
                      and between 2 crates using the backplane QSFP links.

                    - 'corr16': The corner-turn engine is configured to feed the
                      internal firmware correlator (only if the firmware was
                      compiled with it).


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

            ----------Category: **Firmware corelator parameters (if implemented in the FPGA)**----------

            integration_period=None,


            ----------Category: **Logging parameters**----------

            If logging is not set up by the top level application, you can
            optionally specify the folowing arguments to create syslog and stderr
            handlers to help interactive operations. If a handler already exists,
            its log level is simply updated to prevent duplication of handlers.
            Log levels can be strings or numerical log levels.

            syslog_log_level: sets up a SYSLOG handler

            stderr_log_level: sets up a handler that prints on stderr


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

                If an IceBoard is specified by IP address (e.g. '10.10.10.7'),
                then the board can be added directly in the hardware map. This
                does *not* rely on the system mDNS client or the Python
                ``pybonjour`` package.

                If an IceBoard is specified by its mDNS hostname (e.g.
                'iceboard0007.local'), the operating system will automatically
                resolve the IP address using mDNS, assuming that a mDNS client
                (Bonjour on Windows or Mac, avahi on Linux) is running on this
                computer. The ``pybonjour`` Python package is *not* needed.

                In both cases, the crate, slot and serial number information will
                be automatically obtained directly through the IceBoard's ARM
                processor if that information not already present in the hardware
                map.

                If an IceBoard is specified by its serial number (e.g. '0007', or
                just a numeric 7 as a convenient shortcut), the board will use the
                ``pybonjour`` package to actively query mDNS and find boards that
                match the serial number.


            Note:

                Selecting boards by IceCrate serial number *always*
                require the ``pybonjour`` package and the system mDNS
                client to automatically probe the network and discover the
                specified Iceboards that advertised themseles along with
                their associated crate number.

        """
        init = functools.partial(
             self.init.async,
             hwm=hwm,
             iceboards=iceboards,
             icecrates=icecrates,
             mezzanines=mezzanines,
             exclude_iceboards=exclude_iceboards,
             crate_map=crate_map,
             ignore_missing_boards=ignore_missing_boards,
             subarrays=subarrays, ping=ping,
             mdns_timeout=mdns_timeout,
             no_mezz=no_mezz,
             bitfile=bitfile,
             prog=prog,
             open=open,
             if_ip=if_ip,
             fpga_ip_addr_fn=fpga_ip_addr_fn,
             sync_method=sync_method,
             sync_source=sync_source,
             sync_master=sync_master,
             sync_master_time_source=sync_master_time_source,
             max_sync_time_difference=max_sync_time_difference,
             mode=mode,
             frames_per_packet=frames_per_packet,
             tx_power=tx_power,
             integration_period=integration_period,
             corner_turn_bad_links=corner_turn_bad_links,
             corner_turn_bin_priority=corner_turn_bin_priority,
             stderr_log_level=stderr_log_level,
             syslog_log_level=syslog_log_level,
             udp_retries=udp_retries,
             **kwargs)


        if not ioloop:
            # Create our own IOLoop so we don't interfere with ipython's own ioloop.
            old_ioloop = IOLoop.current()
            ioloop = IOLoop()
            ioloop.make_current()
            ioloop.run_sync(init)
            old_ioloop.make_current()
        else:
            self._init = init
            #old_ioloop = IOLoop.current()
            #ioloop.make_current()
            #future = init()
            #ioloop.add_future(future)
            #while not future.done():
            #   print('waiting', future, future.done())
            #   time.sleep(.3)
            #   pass
            #old_ioloop.make_current()

        # else:
        #     old_ioloop = None
            # old_ioloop = IOLoop.current
            # IOLoop.set_current(ioloop)

    @async
    def run(self):
       yield self._init()


    # Map the string to the function that computes the FPGA IP address from the ARM IP address
    FPGA_IP_ADDR_FN_TABLE = {
        '(a,b,3,d)': lambda a,b,c,d:(a, b, 3, d),
        '(a,b,c+1,d)': lambda a,b,c,d:(a, b, c + 1, d),
        }

    @async
    def init(self,

             hwm=None,
             iceboards=[], icecrates=[], mezzanines=[], exclude_iceboards=[],
             crate_map={},
             ignore_missing_boards=False,

             subarrays=None, ping=True,
             mdns_timeout=2,
             no_mezz=False,

             bitfile=None,
             prog=None,
             open=None,

             if_ip=None,
             fpga_ip_addr_fn='(a,b,3,d)',
             udp_retries=10,

             sync_method=None,
             sync_source=None,
             sync_master=None,
             sync_master_time_source=None,
             max_sync_time_difference=20,

             mode=None,
             frames_per_packet=2,
             tx_power=None,
             integration_period=None,
             corner_turn_bad_links=None,
             corner_turn_bin_priority=None,

             stderr_log_level=None,
             syslog_log_level=None,

             #ioloop=None,

             **kwargs
            ):

        """
        Co-routine implementation of fpga_array initialization
        """
        self.ib = []  # make sure repr() has always something
        self.ic = []
        self.sync_timestamp = None

        self.max_sync_time_difference = max_sync_time_difference
        self.tx_power = tx_power
        self.mode = mode

        ###########################################
        # setup pychfpga.fpga_array logging
        ###########################################
        self.logger = logging.getLogger(__name__)
        #print('fpga_array loger name is %s' % __name__)
        #print('logger name=%s, level=%s, handlers=%r' % (self.logger.name, self.logger.level, self.logger.handlers))
        #self.logger.warning('This is a warning')
        #ch = logging.StreamHandler(sys.stdout)
        #self.logger.addHandler(ch)
        #self.logger.warning('why dont you log')

        ###########################################
        # setup sqlalchemy logging
        ###########################################
        # Make sure the SQL engine logs only warnings
        sql_log_level = logging.WARNING
        sql_logger = logging.getLogger('sqlalchemy.engine.base.Engine')
        sql_logger.setLevel(sql_log_level)

        ###########################################
        # setup pychfpga package logging
        ###########################################
        # This sets the logging that comes out from *all* the modules within *pychfpga* package
        # (e.g. pychfpga.core.chfpga_controller.chFPGA_controller(), pychfpga.fpga_array.FPGAArray(), etc.)
        # We are careful not to set the root logger, which might have its own
        parent_logger_name = __name__.rsplit('.', 1)[0] if '.' in __name__ else ''
        parent_logger = logging.getLogger(parent_logger_name)
        # Setup logging. If a handler already exists, its log level is simply updated
        #for (handler_type, log_level) in ((logging.StreamHandler, stderr_log_level), (logging.handlers.SysLogHandler, syslog_log_level)):
        #    if log_level:
        #        log_handlers = [h for h in parent_logger.handlers if isinstance(h, handler_type)]
        #        if log_handlers: # if a handler of that type already exist, just use it
        #            log_handler = log_handlers[0]
        #        else:  # otherwise create a new one
        #            log_handler = handler_type()
        #            parent_logger.addHandler(log_handler)
        #        log_handler.setLevel(log_level.upper() if isinstance(log_level, str) else log_level)
        #        parent_logger.setLevel(min(parent_logger.level, log_handler.level))  # make sure all messages from this handler are passed to the parent handler


        # If no bitfile is provided, automaticaly select the bitfile in the
        # current repository based on th eoperational mode.
        if bitfile is None:
            if mode == 'corr16':
                filename = 'SIFPGA_MGK7MB.bit'
            else:
                filename = 'chFPGA_MGK7MB_Rev2.bit'
            bitfile = os.path.join(
                os.path.dirname(__file__),
                'fpga_bitstreams',
                filename)




        self.logger.info('%r: ------------------------' % self)
        self.logger.info('%r: F P G A   A R R A Y' % self)
        self.logger.info('%r: ------------------------' % self)
        self.logger.info('%r: Called with for following parameters' % self)
        self.logger.info('%r:     if_ip = %s' % (self, if_ip))
        self.logger.info('%r:     iceboards = %s' % (self, iceboards))
        self.logger.info('%r:     icecrates = %s' % (self, icecrates))
        self.logger.info('%r:     subarrays = %s' % (self, subarrays))
        self.logger.info('%r:     ping = %s' % (self, ping))
        self.logger.info('%r:     mdns_timeout = %s' % (self, mdns_timeout))
        self.logger.info('%r:     exclude_iceboards = %s' % (self, exclude_iceboards))
        self.logger.info('%r:     bitfile = %s' % (self, bitfile))
        self.logger.info('%r:     prog = %s' % (self, prog))
        self.logger.info('%r:     open = %s' % (self, open))
        self.logger.info('%r:     no_mezz = %s' % (self, no_mezz))
        # self.logger.info('%r:     sampling_frequency = %s' % (self, sampling_frequency))
        # self.logger.info('%r:     reference_frequency = %s' % (self, reference_frequency))
        self.logger.info('%r:     sync_method = %s' % (self, sync_method))
        self.logger.info('%r:     sync_source = %s' % (self, sync_source))
        self.logger.info('%r:     fpga_ip_addr_fn = %s' % (self, fpga_ip_addr_fn))
        self.logger.info('%r: ------------------------' % self)

        __main__._host_interface_ip_addr = if_ip


        # COnvert the FPGA IP-setting function name to an actual function
        if fpga_ip_addr_fn in self.FPGA_IP_ADDR_FN_TABLE:
            fpga_ip_addr_fn = self.FPGA_IP_ADDR_FN_TABLE[fpga_ip_addr_fn]
        else:
            raise AttributeError("Invalid 'fpga_ip_addr_fn' string '%s'. "
                "Valid strings are  %s" %
                (fpga_ip_addr_fn, ', '.join("'%s'" % fn for fn in self.FPGA_IP_ADDR_FN_TABLE)))

        # Fix up a few parameters for convenience

        # Make sure iceboards is a list
        if isinstance(iceboards, (str, int)):
            iceboards = [iceboards]
        if isinstance(icecrates, (str, int)):
            icecrates = [icecrates]
        # iceboards = [self._to_integer(x) for x in iceboards]

        ###########################################
        # hw_string procesing
        ###########################################


        # Build the hardware description string from various sources
        hw_string = ''   # start with an empty string

        # Add `hwm`, if it is a string or a list of strings
        if isinstance(hwm, basestring):
            hw_string  += hwm + ' '
            hwm = None
        elif isinstance(hwm, list) and all(isinstance(elem, basestring) for elem in hwm):
            hw_string  += ' '.join(hwm) + ' '
            hwm = None

        # Add `iceboards'
        if iceboards:
            hw_string += 'MGK7MB '+ ' '.join(iceboards) + ' '

        # Add `icecrates`
        if icecrates:
            hw_string += 'MGK7BP16 '+ ' '.join(icecrates) + ' '
        self.logger.debug("%r: The composite hardware description string is: '%s'" %  (self, hw_string))

        # Parse the hwm string into a hardware table. The hardware table is
        # not the hardware map, but represents the entries that we want to add
        # to the hardware map later.
        remap_table = {'crates' :
            {(crate_number,): ('icecrates', (model, serial, crate_number))
             for crate_number, (model, serial) in crate_map.items()}}
        hw_table = parse_hw_string(hw_string, remap_table)
        self.logger.debug('%r: The hardware description table obtained from hardware description string is:' % self)
        for key, value in hw_table.items():
            self.logger.debug('%r:     %s:%r' % (self, key, value))

        # Check if there were 'crate' entries that were not remapped to icececrate entries
        if hw_table.crates:
            self.logger.warning('The following hardware map crate entries could not be resolved into backplane model/serial: %s' % hw_table.crates)

        # Create a *reverse* crate map ({(model,serial):number} instead of {number:(model,serial)})
        # that will be used later with set_crate_numbers() to assign crate numbers to crates if
        # those were not already expressed explicitely in the hw_string (e.g. if the crate is
        # specified by serial number or was auto-detected from a specified iceboard)
        if crate_map:  # If we already explicitely provided a crate map, use it.
            crate_number_map = {(model, serial): crate_number for crate_number, (model, serial) in crate_map.items()}
        else: # if not, try to build one from the info that was provided in the hw description string
            # If some crate numbers are specified or if there are crate wildcards, just use the
            # crate numbers that user specified
            if any (crate_number is not None or serial is '*' for (model, serial, crate_number) in hw_table.icecrates):
                crate_number_map = {(model, serial) : crate_number
                    for (model, serial, crate_number) in hw_table.icecrates
                    if crate_number is not None and serial != '*'}
            # If no crate number was specified at all and there are no wildcards, assign crate
            # numbers in the order they were specified in the hw description string (first crate =
            # crate 0, second crate is crate 1 etc)
            else:
                crate_number_map = {(model, serial): i
                    for i, (model, serial, crate_number) in enumerate(hw_table.icecrates)}
        self.logger.debug('%r: The Icecrate map is %s' % (self, crate_number_map))

        # We've got our crate numbers. Remove them from the icecrate list so we pass only the (model,
        # serial) to the mdns discovery function.
        hw_table.icecrates = [(model, serial) for (model, serial, crate_number) in hw_table.icecrates]

        ######################################
        # Hardware map processing
        ######################################
        # If a pre-formed hardware map is provided in the form of a
        # hardwareMap object or a list of strings, filter the map for non-
        # responding boards or boards that do not belong to the target
        # subarray.

        #self.logger.debug('hardware map=%s' % hwm)

        # If no hardware map is provided, create an empty one
        if not hwm:
            self.hwm = HardwareMap()  # Create empty hardware map
        # If the hwm parameter is a list of  dicts, create the hardware map by instantiating the
        # object of the type contained in the ``class`` element and passing it the remaining
        # elements as keyword arguments
        elif isinstance(hwm, list):
            self.logger.debug('%r: Creating Hardware Map from list' % self)
            self.hwm = HardwareMap()  # Create empty hardware map
            for obj in hwm:
                params = dict(obj) # make a copy
                #print(params)
                class_name = params.pop('class') # remove the class name from the dict. The rest wil be used as instantiation parameters
                class_ = HWMResource._decl_class_registry[class_name] # look up all the classes from the class registry created with the Base object
                if issubclass(class_, IceBoardPlus):
                    crate_number = params.pop('crate_number', None)
                    #self.logger.debug('%r: Crate %r is in %r' % (self, crate_number, params))
                    if crate_number is not None:
                        crate = self.hwm.query(IceCrateExt).filter(IceCrateExt.crate_number == crate_number).one()
                        #self.logger.debug('%r: Assigning crate %r to board %r' % (self, crate, params))
                        params['crate'] = crate
                    else:
                        raise RuntimeError('%r: In the hwm, crate must be an integer referring to a crate number. It will be converted to a crate object reference' % self)
                self.hwm.add(class_(**params))
                self.hwm.flush()
        # Otherwise use the hardware map as is, hoping it is a HardwareMap object
        else:
            self.hwm = hwm

        # If subarrays are specified, remove boards that are not in those subarrays
        if subarrays is not None:
            print('Subarrays are: %r' % subarrays)
            ib_not_in_subarray = self.hwm.query(IceBoardPlus).filter(~IceBoardPlus.subarray.in_(subarrays))
            for ib in list(ib_not_in_subarray):  # make sure the list does not change during the loop
                self.logger.debug("%r (subarray '%s') is not in the target subarray list %s. It is removed from the YAML hardware map."  # That comment should be if verbose=1
                                   % (ib, ib.subarray, subarrays))
                self.hwm.delete(ib)
            self.hwm.flush()

        # If ping=1, remove boards that do not respond to tuber pings
        ping_timeout = 3
        missing_boards = []
        if ping:
            ib_to_ping = self.hwm.query(IceBoardPlus).as_dict()  # use as_dict so ib_to_ping does not change as we delete boards from the hwm
            if ib_to_ping:
                self.logger.info('%r: Pinging the %i IceBoards specified explicitely in YAML file' % (self, len(ib_to_ping)))
                ping_results = yield [ib.ping.async(timeout=ping_timeout) for ib in ib_to_ping]  # asynchronous parallel call to all boards
                #self.logger.debug('%r: Ping results are %s' % (self, ping_results))
                for i, ping_successful in enumerate(ping_results):
                    ib = ib_to_ping[i]
                    if ping_successful:
                        ib.hostname = socket.gethostbyname(ib.hostname)
                    else:
                        missing_boards.append('%s (SN%s, (%s,%s))' % (
                            ib.hostname,
                            ib.serial or '????',
                            ib.crate.crate_number if ib.crate else '?',
                            ib.slot-1 if ib.slot else '?'))
                        self.logger.debug('%r: Deleting %r from the YAML hardware map' % (self, ib))
                        self.hwm.delete(ib)
                self.hwm.flush()
            else:
                self.logger.info('%r: There are no IceBoards specified explicitely in YAML Hardware map. Ping verification is not performed on those.' % self)
            if missing_boards:
                message ="%r: Could not ping the following boards: %s" % (self, ', '.join(missing_boards))
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

        if hw_table.iceboards:
            self.logger.info('%r: Adding Iceboards listed in hardware description string with explicit hostnames' % self)
            added_ib = []
            for (model, hostname) in list(hw_table.iceboards): # make a copy: we modify in-place
                if model is None or '.' in str(hostname): # if it is actually a hostname. Might be an int serial
                    hw_table.iceboards.remove((model, hostname)) # remove it. We'll be left with boards that require mDNS...
                    # ip_addr = socket.gethostbyname(hostname)  # convert hostname to IP address for faster Tuber access
                    new_ib = IceBoardPlus(hostname=hostname, handler_name='chFPGA_controller')
                    self.hwm.add(new_ib)
                    self.hwm.flush()
                    added_ib.append(new_ib)
            # Explicitely listed boards must exist on the network
            if ping and added_ib:
                self.logger.info('%r: Pinging Iceboards with explicit hostnames in the hardware description string' % self)
                # ping all boards concurrently
                ping_results = yield {ib: ib.ping.async() for ib in added_ib}
                # If some boards failed, raise an exception
                if not all(ping_results.values()):
                    raise RuntimeError("%r: The following Iceboards could not be pigned: '%s'" % (
                        self,
                        ', '.join('%r (%s)' % (ib, ib.tuber_uri)
                                  for ib, ping_result in ping_results.items() if not ping_result)))
                # resolve hostnames into IP addresses
                # this is not concurrent, unfortunately... this is why we checked ping first, otherwise it blocks for a long time

                self.logger.info('%r: Resolving IP addresses of Iceboards that passed the ping test' % self)
                t0 = time.time()
                for ib, ping_result in ping_results.items():
                    if ping_result:
                        ib.hostname = socket.gethostbyname(ib.hostname)
                if any(ping_results.values()):
                    self.hwm.flush()
                self.logger.info('%r: Finished resolving IP addresses. It took %s seconds' % (self, time.time() - t0))

        ########################################################
        # Establish Tuber communication (ARM only)
        ########################################################
        # All the boards in the hardware map should have a hostname now.
        # Let's initialize ARM/Tuber
        # communication becore we start using Tuber methods. We want to cache the Tuber methods
        # asynchnously now instead of letting Tuber do it asynchrunously on the first Tuber command
        # it gets.
        ibs = self.hwm.query(IceBoardPlus)
        if ibs.count():
            self.logger.info('%r: Establishing communication with the %i motherboard ARM processors over the HTTP/Tuber protocol and acquiring list of remote functions' % (self, ibs.count()))
            t0 = time.time()
            yield [ib._tuber_get_meta.async() for ib in ibs]
            self.logger.info('%r: Connection with %i ARM processors established. It took %s seconds' % (self, ibs.count(), time.time() - t0))

        ########################################################
        # Resolve missing serial/crate/slot info through the ARM
        ########################################################
        # Complete serial, crate and slot information on IceBoard that miss
        # that information by talking directly to the ARM
        # (i.e without using mDNS and pybonjour).
        ib_without_serial = self.hwm.query(IceBoardPlus).filter(IceBoardPlus.serial == None)
        if ib_without_serial.count():
            t0 = time.time()
            self.logger.info('%r: Auto-Discovering the serial number of the IceBoards with known hostnames: %s' %
                (self, ', '.join(ib_without_serial.hostname)))
            # concurrently resolve serials
            futures = [ib.discover_serial.async() for ib in ib_without_serial] # Tuber method
            self.logger.info('%r: Got all discover_serial futures after %f seconds' % (self, time.time() - t0))
            yield futures # [ib.discover_serial.async() for ib in ib_without_serial]
            self.logger.info('%r: Finished Auto-Discovering serial number for IceBoards. Took %f seconds.' % (self, time.time() - t0))

        ib_without_slot = self.hwm.query(IceBoardPlus)
        if ib_without_slot.count():
            self.logger.info('%r: Auto-Discovering & validating the slot numbers for %i IceBoards with known hostnames...' % (self, ib_without_slot.count()))
            t0 = time.time()
            yield [ib.discover_slot.async() for ib in ib_without_slot]
            self.logger.info('%r: Finished Auto-Discovering slot numbers for IceBoards. Took %f seconds.' % (self, time.time() - t0))

        ib_without_crate = self.hwm.query(IceBoardPlus).filter(or_(IceBoardPlus.crate == None, IceBoardPlus.slot == None))
        if ib_without_crate.count():
            t0 = time.time()
            self.logger.info('%r: Auto-Discovering crate information for IceBoards with known hostnames: %s' %
                (self, ', '.join(ib_without_crate.hostname)))
            yield [ib.discover_crate.async() for ib in ib_without_crate]
            self.logger.info('%r: Finished Auto-Discovering crate serial numbers. Took %f seconds.' % (self, time.time() - t0))

        ###########################################################################
        # mDNS discovery of boards and crates specified by model/serial number only
        ###########################################################################

        if hw_table.iceboards or hw_table.icecrates :
            self.logger.info('%r: Discovering IceBoards and Icecrates specified by serial number using mDNS' % self)
            self.logger.info('%r:     IceBoards to find: %s' % (self, hw_table.iceboards))
            self.logger.info('%r:     IceCrates to find: %s' % (self, hw_table.icecrates))
            self.print_flush()
            # Perform mDNS discovery. This is done on a separate ioloop, which locks up the current loop for a while
            mdns_discover(self.hwm,
                          icecrates=hw_table.icecrates,
                          iceboards=hw_table.iceboards,
                          timeout=mdns_timeout)

        # Remove iceboards to be excluded (by serial number)
        if exclude_iceboards:
            self.logger.info('%r: Removing IceBoards based on exclusion list: %s' % (self, exclude_iceboards))
            for ib in self.hwm.query(IceBoardPlus):
                try:
                    serial = str(int(ib.serial))
                except (TypeError, ValueError):
                    serial = ib.serial
                if serial in exclude_iceboards or ib.serial in exclude_iceboards:
                    self.hwm.delete(ib)
            self.hwm.flush()

        self.logger.info('%r: Hardware map is complete' % self)

        #################################
        # Create self.ib and self.ic
        #################################

        # Query the hardware map for all iceboards and icecrates
        ib = self.hwm.query(IceBoardPlus).outerjoin(IceCrateExt).order_by(IceCrateExt.serial, IceBoardPlus.slot)  # use outerjoin in case there is no crate
        ic = self.hwm.query(IceCrateExt).order_by(IceCrateExt.crate_number)

        # Courtesy warning
        if not ic.count():
            self.logger.warn('There are no IceCrates in the hardware map!')

        # Store as Ccoll collections to allow easy parallel operations
        self.ib = Ccoll(ib)
        self.ic = Ccoll.unique((c for c in ib.crate if c) if self.ib else [])

        #################################
        # Set the crate numbers
        #################################
        # ... using whatever map we could determine from the parameters

        self.set_crate_numbers(crate_number_map, strict=False)

        #################################
        # Check if all the hardware we wanted is present
        #################################
        # List all the crates we know about along with their model/serial tuple
        # Format: {icecrate_object : (model, integer_serial),...}
        current_crates = {c: (c.part_number, self._to_integer(c.serial)) for c in self.ic}

        # Find if explicitely requested crates were not found
        missing_crates = [model_serial for model_serial in hw_table.icecrates
                          if model_serial not in current_crates.values()]
        if missing_crates:
            raise RuntimeError('%r: The following crates are missing: %s' %
                (self, ', '.join('%s SN%s' % (model, serial) for (model, serial) in missing_crates)))

        # Check for missing boards in explicitely-specified crates
        missing_slots = {
            (model_serial, ic.crate_number): set(range(1, ic.NUMBER_OF_SLOTS + 1)) - set(ic.slot)
            for ic, model_serial in current_crates.items() if model_serial in hw_table.icecrates}
        if any(missing_slots.values()):
            message = '%s: The following slots are missing:\n%s' % (
                self,
                '\n'.join('    Crate #%s (%s SN%s): slots %s' % (number, model, serial, ', '.join(str(s) for s in slots))
                for ((model, serial), number), slots in missing_slots.items() if slots))
            if not ignore_missing_boards:
                raise RuntimeError(message)
            else:
                self.logger.warning(message)
        #################################
        # Discover Mezzanines
        #################################
        # Check if the board is running a compatible ARM firmware. This will
        # trigger the first communication to the ARM, causinng Tuber to fetch
        # the method & property directory from each board.
        self.logger.info('Checking ARM firmware version...')
        if self.ib:
            yield [ib.check_tuber_version.async() for ib in self.ib]

        #################################
        # Discover Mezzanines
        #################################
        # Auto-discover mezzanines and add them to the hardware map.
        if self.ib and not no_mezz:
            self.logger.info('Discovering Mezzanines...')
            self.print_flush()  # make sure we see the previous prints right away so we have a better feeling of what is happening
            yield [ib.discover_mezzanines.async() for ib in self.ib]
            self.hwm.flush()
            self.ib.set_cache()

        def get_mezz_name(ib, mezz_number):
            m = ib.mezzanine.get(mezz_number, None)
            # return '%s_SN%s' % (m.__ipmi_part_number__, m.serial) if m else '-'
            return 'SN%s' % (m.serial) if m else '-'

        #################################
        # Print the IceBoard table
        #################################
        self.print_iceboard_table(lambda ib: '%s\n%s' % (get_mezz_name(ib,1), get_mezz_name(ib,2)), row_labels=['Mezz1\nMezz2'], add_serial=True)
        self.print_flush()

        #################################
        # Program the FPGAs
        #################################

        # Tell the IceBoard to run chFPGA firmware, program the FPGA, and establish communication with it
        if self.ib:
            self.ib.set_handler(chFPGA_controller)
            self.ib.set_cache() # we have a new handler, so update its cached ORM object values
            # ib.set_handler(IceBoardPlusHandler, fpga_bitstream)

            # Configure the FPGA with the bitstream associated with the handler
            if prog:
                self.logger.info('%r: Configuring FPGAs...' % self)
                # Associate the bitstream with the target Handler
                self.fpga_bitstream = FPGABitstream(bitfile, auto_reload=False)
                self.logger.info('%r: Loaded bitfile: %s' % (self, bitfile))
                # str(self.fpga_bitstream)
                self.ib.register_fpga_bitstream(self.fpga_bitstream)
                yield [ib.set_fpga_bitstream.async(force= (prog > 1)) for ib in self.ib]
                self.logger.info('%r: Done configuring FPGAs' % self)

        self.print_flush()

        #################################
        # Initialize
        #################################

        if self.ib and open is not None and open > 0:

            ########################
            # Initialize core FPGA firmware (establish FPGA UDP communications)
            ########################
            @async
            def open_core(ib, max_trials=3):
                """ Try to open the FPGA core firmware (including UDP
                communications) and reprogram the FPGA and retry a number of
                times if this fails.

                Parameters:

                    ib: IceBoard handler

                    max_trials (int): Maximun allowed number of reprogramming and retrials before raising an error.

                Exceptions:

                    IOError: Raised if the iceboard's open_core still raaises an IOError after the maximum number of trials.

                """
                trial = 1
                while True:
                    try:
                        self.logger.info('%r: Initializing core FPGA firmware, including FPGA UDP communications (calling ib.open_core()). Trial %i/%i.' % (self, trial, max_trials))

                        # Initialize FPGA UDP communications. Overrides
                        # default parameters that were temporarily set when
                        # the iceboard handler object was created.
                        yield ib.open_core.async(
                            udp_retries=udp_retries,
                            fpga_ip_addr_fn=fpga_ip_addr_fn,
                            interface_ip_addr=if_ip)
                        return
                    except IOError as e:
                        self.logger.error('%r: Error while initializing core firmware on trial %i/%i. Error is: \n%r' % (self, trial, max_trials, e))
                        if trial >= max_trials:
                            raise IOError('%r: Unable to initializing FPGA core firmware after %i trials. Giving up.' % (self, trial))
                        else:
                            trial += 1
                            self.logger.error('%r: Reprogramming FPGA and trying again.' % (self))
                            yield ib.set_fpga_bitstream.async(force=True)
            yield [open_core.async(ib) for ib in self.ib]

            ########################
            # Initialize application specific FPGA firmware
            ########################

            self.logger.info('%r: Initializing FPGA firmware (calling ib.open())' % self)
            yield [ib.open.async(adc_delay_table=ADC_DELAY_TABLE,
                                 init=open,
                                 **kwargs
                                 # sampling_frequency=sampling_frequency,
                                 # reference_frequency=reference_frequency,
                                 ) for ib in self.ib]

            ########################
            # Initializing SYNC method
            ########################
            if sync_method or sync_source:
                self.logger.info('%r: Setting SYNC method' % self)
                self.set_sync_method(method=sync_method, source=sync_source, master=sync_master, master_time_source=sync_master_time_source)

            ########################
            # Initializing operational mode
            ########################
            if mode:
                self.logger.info('%r: Setting operational mode to %s' % (self, mode))
                self.set_operational_mode(
                    mode=mode,
                    frames_per_packet=frames_per_packet,
                    tx_power=tx_power,
                    integration_period=integration_period,
                    corner_turn_bad_links=corner_turn_bad_links,
                    corner_turn_bin_priority=corner_turn_bin_priority)

            ########################
            # Initializing backplane hardware communication firmware
            ########################

            self.logger.info('%r: Initializing Backplane firmware' % self)
            if self.ic:
                self.ic.init()

        self.print_flush()

        #################################
        # Completed
        #################################

        self.logger.info('%r: Done creating %r' % (self, self))

    @staticmethod
    def _to_integer(x):
        """ If the specified argument has an integer representation then
        return that integer otherwise return the original argument.
        """
        try:
            return int(x)
        except ValueError:
            return x

    @staticmethod
    def _parse_crate_id(string):
        """ Splits a string describing a crate into a (model, serial, crate_number) tuple.
        The serial is converted to an interger if possible; otherwise, it is a string. The crate number must be an integer.
        Missing parameters are returned as None. Every field is converted to uppercase.

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
            raise RuntimeError("Crates model and serial number must be separated by one (and only one) underscore (e.g. MGK7BP16_023). Got '%s'" % sn)

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


    def dns_resolve(self, hostnames='iceboard0077.local', timeout=1):
        """ This is an experimental method to concurrently resolve the  IP
        address of boards without having to contend with the fixed timout of
        getaddrinfo(). This does not work yet, as requests seem to block
        anyway even with the Async resolver.
        """
        if isinstance(hostnames, str):
            hostnames = [hostnames]
        io_loop = IOLoop()
        resolver = Resolver()
        futures = [resolver.resolve(h, 9000) for h in hostnames]

        def stop_when_all_resolved(one_future):
            print [ff.done() for ff in futures]
            self.print_flush()
            return
            # if all(f.done() for f in futures):
            #     io_loop.stop()
            # print one_future.exception() or one_future.result()
        for f in futures:
            io_loop.add_future(f, stop_when_all_resolved)
        io_loop.add_timeout(io_loop.time() + timeout, lambda: io_loop.stop())
        io_loop.start()
        ip_addr = [None if not f.done() or f.exception() else dict(f.result())[socket.AF_INET][0] for f in futures]
        for f in futures:
            f.cancel()
        resolver.close()
        return (resolver, futures, ip_addr)

    # def __getattr__(self, name):
    #     """
    #     Redirects all attributes access to the hardware map (Session) object.
    #     """
    #     return getattr(self.hwm, name)

    # def __dir__(self):
    #     # return type(self).__dict__ + self.__dict__ + dir(self._hwmap)
    #     return dir(self.hwm) + self.__dict__.keys()

    def __repr__(self):
        """ Short string representing this object and suitable to use as a tag in a syslog entry"""
        return '%s(%i_boards,%i_crates)' % (self.__class__.__name__, len(self.ib), len(self.ic))

    def get_hwm_info(self):
        string = '%s object with the following hardware map:\n' % self.__class__.__name__
        for i in self.ib:
            mezz = ['%s SN%s' % (m.__ipmi_part_number__, m.serial) if m else 'None' for m in [i.mezzanine.get(1,None), i.mezzanine.get(2,None)]]
            string +='   Crate SN%s, slot %2i: Iceboard SN%s at %s (ping =%s), Mezz1=%s, Mezz2=%s\n' % (i.crate.serial if i.crate else None, i.slot, i.serial, i.hostname, i.ping(), mezz[0], mezz[1])
        return string


    def set_crate_numbers(self, crate_number_map, strict=True):
        """ Set the crate number of each crate based on the provided crate
        number map.

        Silently overrides any existing crate numbers.

        Parameters:

            crate_number_map (dict): A { (model, serial): number} dict that
                maps a crate ID tuple to a crate number.


            strict (bool): if True, will raise an exception if not all crates
                can be assigned a crate number

        Notes:

            Crate numbers are needed to identify hardware element by simple
            tuples (e.g. (crate_number, slot_numer, lane_number)) and are also
            used to infer which is a master and slave crate when crates are
            interconnected in pairs.
        """

        self.logger.info('%r: Setting crate numbers for the following crates %s' % (self, ', '.join(repr(ic) for ic in self.ic)))

        for ic in self.ic:
            model = ic.part_number
            sn = ic.serial
            try:
                int_sn = int(ic.serial)
            except (ValueError, TypeError):
                int_sn = None

            new_crate_number = (crate_number_map.get((model, sn), None) or
                               crate_number_map.get((model, int_sn), None))

            #if not isinstance(new_crate_number, int):
            #    raise ValueError('%r: crate number %r is not an interger' % (self, new_crate_number))

            if new_crate_number is not None:
                ic.crate_number = int(new_crate_number)
                self.hwm.flush()
                self.logger.info('%r: Assigining crate number %r to crate %s' % (self, new_crate_number, ic.get_string_id()))
            elif strict:
                raise RuntimeError('Cannot find a crate number for crate %s' % ic.get_string_id())
            else:
                self.logger.warning('%r: set_crate_number: Cannot find a crate number for crate %s' % (self, ic.get_string_id()))


    def set_operational_mode(self,
                             mode,
                             frames_per_packet=1,
                             send_flags = True,
                             chan8_channel_map=range(8),
                             tx_power=None,
                             integration_period=16384,
                             corner_turn_bad_links=None,
                             corner_turn_bin_priority=None,
                             ):
        """ Set the operational mode of the array.


        Parameters:

            mode (str): operational mode string.

                - 'chan8': Corner-turn engine is mostly bypassed and raw 8-bit data from 8
                  channelizers is sent directly to the 8 CT-Engine outputs.

                - 'chan4': Corner-turn engine is mostly bypassed and raw 8-bit data from 16
                  channelizers is sent directly to the 8 CT-Engine outputs.

                - 'shuffle16': Acquire, channelize and shuffle data within each Iceboard individually and
                  send the data through the IceBoard QSFP+ ports. There is no data shuffling between boards.
                  This is good for single board operation (or an array of boards operating independently)

                - 'shuffle256': Acquire, channelize and shuffle data within a crate to
                    create a 16-board (256-channel) correlator. The shuffled data is
                    sent through the IceBoard QSFP+ ports. There is no shuffling between
                    crates.

                - 'shuffle512': Acquire, channelize and shuffle data between pair of
                  crates to create a 32-board (512-channel) correlator. The shuffled
                  data is sent through the IceBoard QSFP+ ports. The pairing of crates
                  is based on the crate number: Crate N and N+1 form a pair, whereas N
                  is a even number.

                - 'corr16': The corner-turn engine is configured to feed the
                  internal firmware correlator (only if the firmware was compiled with it).

            frames_per_packet (int): Number of frames to combine in a single
                packet. Is limited by the amount of buffering space inside the
                FPGA.

            send_flags (bool): If True, the Scaler and Frame flags will be
                sent after the data block.

            tx_power (dict): Power levels to be set on the GTXes.

            integration_period (int): (for ``corr16`` mode only):
                Sets the integration period (in frames) of the firmware
                correlator.

            corner_turn_bad_links (list of tuples): List of corner turn engine
                outputs links [(crate, slot, links), ...] that should
                preferably NOT be assigned any frequency bins if possible.

            corner_turn_bin_priority (list of int): List of frequency bins
                that are to be assigned to the corner turn output, in order of
                priority (most desirable frequency first). The lowest priority
                bins will be assigned to the bad links as much as possible,
                depending on the constraints of the corner turn engine
                flexibility.



        Notes:

            Having called get_ber() before initializing the shuffle will lead to errors!

        # data_width : Data width of each Re and Im component of the channelizer output
        # enable_gpu_link : Enables the GPU link transmission

        """
        # use defaults that were set during initialization unless overriden
        mode = mode or self.mode
        tx_power = tx_power or self.tx_power
        self.logger.info('%r: Setting operational mode to %s' % (self, mode))
        self.logger.info('%r: Using tx_power=%r' % (self, tx_power))
        # To make sure that the data acquisition and transmission will be done at the same rate, refuse to operate if there
        # are more than one IceBoard in the array and the boards are not all
        # set to operate on the backplane clock.
        if len(self.ib) > 1:  #self.ic.NUMBER_OF_SLOTS
            clock_sources = self.ib.index_by(repr).get_clock_source()
            target_clock_source = 'CLOCK_SOURCE_BP'
            if set(clock_sources.values()) != set([target_clock_source]):
                raise RuntimeError('The following IceBoards are not configured to use the backplane clock: %s' % (', '.join(repr(ib) for (ib, cs) in clock_sources.items() if cs != target_clock_source)))

        if mode == 'raw_time':
            self.ib.set_fft_bypass(True)
            self.ib.set_scaler_bypass(True)
            self.corner_turn_stream_ids = self.init_corner_turn(
                mode='chan8',
                frames_per_packet=frames_per_packet,
                send_flags=send_flags,
                chan8_channel_map=np.hstack((chan8_channel_map, [16]*8)),
                tx_power=tx_power)

        elif mode in ['shuffle256', 'shuffle512', 'shuffle16']:
            if not all(self.ib.CROSSBAR2) or not all(self.ib.CROSSBAR3):
                raise RuntimeError('All IceBoards must have their CROSSBAR2 and CROSSBAR 3 implemented')
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
                bin_priority=corner_turn_bin_priority)
            self.ib.BP_SHUFFLE.reset_stats()
            self.ib.CROSSBAR2.reset_stats()
            self.ib.CROSSBAR3.reset_stats()

        elif mode =='corr16':
            if not all(self.ib.CORR):
                raise RuntimeError('All IceBoards must have a firmware correlator engine')
            bin_map = self.get_corner_turn_bin_map(
                mode=mode,
                bad_links=corner_turn_bad_links,
                bin_priority=corner_turn_bin_priority)
            self.corner_turn_stream_ids = None
            self.corner_turn_frequency_bins = None
            for ib in self.ib:
                ib.init_crossbars(mode, frames_per_packet=1, bin_map=bin_map[ib.get_id()])
            self.ib.set_offset_binary_encoding(False)  # The firmware correlator engine expects 1's complement encoding
            if integration_period:
                self.ib.start_correlator(integration_period=integration_period)
        else:
            raise ValueError('Unknown operational mode')

    def get_corner_turn_bin_map(self, mode, bad_links=None, bin_priority=None):
        """ Get the bin selection map for every board of the array in order to route the bins toward the  desired output links

        Parameters:

            bad_links: List of corner_turn outputs [(crate, slot, link) , ...]
                that can't process data. low-priotity frequencies will be
                assigned to those bins.

            bin_priority: list of all frequency bins (from 0 to 1023) in order
                of priority, with the useful bins at the beginning of the
                list.


        Returns:
            A dictionary that deescribes the corner-turn bin selection map for every board, in the format
                { (crate, slot):{'cb1':cb1_bins, 'cb2':cb2_bins, 'cb3':cb3_bins}, ... }
            where
                ``cb1_bins``: list of 16 lists describing the bins indices that are selected by each bin selector of the 1st crossbar
                ``cb2_bins``: list of 2 lists describing the bins indices that are selected by each bin selector of the 2nd crossbar
                ``cb3_bins``: list of 8 lists describing the bins indices that are selected by each bin selector of the 3rd crossbar
        """
        # Use default mapping
        bad_links = bad_links or []
        bin_priority = bin_priority or range(1024)
        if mode == 'shuffle512' or mode == 'shuffle256':
            bin_map = {}

            cb3_map = self.shuffle512_cb3_remap(mode=mode,
                                                bad_links=bad_links,
                                                freq_bins=bin_priority,
                                                output_cb3_bins=True)
            # cb3_map describes only odd and even crate numbers. We expand the list to cover all crates explicitely.
            for ib in self.ib:
                cb3_bins = [cb3_map.get((crate & 1, slot, lane), None)
                    for crate, slot, lane in ib.GPU.get_lane_ids()]
                bin_map[ib.get_id()] = {
                    'cb1':None,
                    'cb2':None,
                    'cb3':cb3_bins}
        else: # other modes. Use defaults.
            bin_map = {ib.get_id(): {'cb1':None,'cb2':None, 'cb3':None}
                       for ib in self.ib}
        return bin_map


    @staticmethod
    def shuffle512_cb3_remap(mode, bad_links, freq_bins, output_cb3_bins=False):
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

        freq_bins = np.array(freq_bins) # Make sure freq_bins is an np.array
        freq_bins_priority = np.argsort(freq_bins) # indices that sort freq_bins in ascending order
        bad_links = [tuple(link) for link in bad_links]


        Nfreq = 1024 # Number of frequency bins
        Nslot = 16 # boards per crates
        Nlink = 8  # GPU links per board
        if mode == 'shuffle512':
            Ncrate = 2
            Nbix = 32 # Bins per CB3 input
            Nbins = 4 # Bins per CB3 output
            bix_to_bin = lambda crate, slot, bix: (Nslot * Ncrate * bix) + (Nslot * crate) + slot
        elif mode == 'shuffle256':
            Ncrate = 1
            Nbix = 64
            Nbins=8
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
                available_bins = bix_to_bin(crate, slot, available_bix) # absolute bin number available at the input of CB3 on this (crate, slot)
                available_priority = np.argsort(freq_bins_priority[available_bins]) # indices of available bins/bix, in priority order (first is most important)
                available_bix = list(available_bix[available_priority]) # bin indices, sorted by importance. Convert to a list so we can easily delete items.
                # Indices of allowed freq_bins, sorted by importance
                # available_bins_indices = np.sort(freq_bins_priority[available_bins])
                # available_bins = list(available_bins[available_priority]) # absolute bins, sorted by importance

                # i_top = 0
                # i_bottom = Nbix
                # Assign bix, lane per lane
                for link in range(Nlink):
                    link_id = (crate, slot, link)
                    if link_id in bad_links: # Bad link: assign less important freq. bins
                        bix = available_bix[-Nbins:] # take the bottom (lower priority) bix
                        del available_bix[-Nbins:] # remove from the list
                        # freq_remap[link_id] = list(
                        #     available_bix[i_bottom - Nbins:i_bottom] if output_cb3_bins else
                        #     freq_bins[available_bins_indices[i_bottom - Nbins:i_bottom]])
                        # i_bottom -= Nbins
                    else: # Good link: assign important freq. bins
                        bix = available_bix[:Nbins] # take the top bix (higher priority)
                        del available_bix[:Nbins] # remove from the list
                        # freq_remap[link_id] = list(available_bix[i_top:i_top + Nbins] if
                        #                         output_cb3_bins else
                        #                         freq_bins[available_bins_indices[i_top:i_top + Nbins]])
                        # i_top += Nbins
                    bix = np.sort(bix) # order selected bins in increasing order. Now a numpy array again.  The bins are always transmitted that way.
                    freq_remap[link_id] = list(bix if output_cb3_bins else bix_to_bin(crate, slot, bix))
                if len(available_bix):
                    raise RuntimeError('Not all bins were processed. This should not happen')
        return freq_remap

    def init_corner_turn(self,
                     mode,
                     dsmap=range(16),
                     frames_per_packet=1,
                     send_flags=True,
                     chan8_channel_map=range(16),
                     tx_power=None,
                     bin_map=None,
                     bad_links=None,
                     bin_priority=None,
                     sync=True
                     ):
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
        #     raise RuntimeError('All boards must be in the same crate. The provided set of Iceboards have the following crates: %r' % crate_set)
        # crate = crate_set.pop()

        self.logger.info('%r: Configuring crate-wide data shuffling with frames_per_packet=%i' % (self, frames_per_packet))

        bin_map = self.get_corner_turn_bin_map(
            mode=mode,
            bad_links=bad_links,
            bin_priority=bin_priority)

        #####################
        # Set-up transmitters
        #####################
        self.corner_turn_stream_ids = {}
        for i, ib in enumerate(self.ib):
            self.logger.info('%r: **** Initializing transmitters for IceBoard %r (SN%s) ****' % (self, ib, ib.serial))
            ib.set_corr_reset(0) # Put the corner_turn engine in reset

            tx_list.append((ib.slot, 0))  # Register Bypass lane (lane 0) as a transmitter in this slot
            for j, gtx in enumerate(ib.BP_SHUFFLE.gtx):
                gtx.TXINHIBIT = 0
                tx_list.append((ib.slot, j+1))

            # Initialize the crossbars to select and send data in a specific format
            # ib.init_crossbars(dsmap, frames_per_packet=frames_per_packet, cb1_lanes=cb1_lanes, cb1_bins=cb1_bins, cb1_bypass=cb1_bypass, cb2_lanes=cb2_lanes, cb2_bins=cb2_bins, cb2_bypass=cb2_bypass, remap=remap, bp_bypass=bp_bypass)
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
            raise self.logger.warn('%r: Stream IDs are not unique across the array')

        if ib.crate:

            #####################
            # Set-up receivers
            #####################
            for i, ib in enumerate(self.ib):
                # Disable all receivers for which there are no transmitters
                for j, gtx in enumerate(ib.BP_SHUFFLE.gtx[0:ib.BP_SHUFFLE.NUMBER_OF_PCB_LINKS]):
                    if ib.slot is None:
                        continue
                    rx = (ib.slot, j+1)
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
                    self.logger.info('%r: TX power parameters are: %r (default=%r, exceptions=%r)' % (self, tx_group, default, exceptions))
                    self.set_tx_power(lane_group=lane_group, default_power=default, exceptions=exceptions, index=index)
                if index == 0:
                    time.sleep(0.3)
                    for ib in self.ib:
                        self.ib.BP_SHUFFLE.reset_rx_equalizers()

            self.ib.BP_SHUFFLE.reset_stats()

            # Print links
            for ib in self.ib:
                for i in range(ib.NUMBER_OF_CROSSBAR_OUTPUTS):
                    if ib.slot is None:
                        continue
                    rx = (ib.slot, i)
                    tx = ib.crate.get_matching_tx(rx)
                    if tx in tx_list:
                        pass
                        #self.logger.debug('%r: In %r,  %s is receiving from %s' % (self, ib.crate, rx, tx))
                    else:
                        self.logger.debug('%r: In %r, %s has no corresponding transmitter' % (self, ib.crate.handler, rx))

        # Get the exhaustive frequency map that is implemented by the current corner
        freq_map = self.get_frequency_map(format='l:bb')
        # Retain only one bin number  for each bin
        self.corner_turn_frequency_bins = {lane_id: sorted(set(data['data']))
            for lane_id, data in freq_map.iteritems()}

        # sync boards
        #soft_sync(c, sync_board)
        self.logger.info('%r: Shuffling initialization completed.' % self)
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

        exceptions = {tuple(node_id): power_tuple for node_id, power_tuple in exceptions}
        self.logger.info('%r: Setting GTX power for lane group %s to power index %i' % (self, lane_group, index))
        self.logger.info('%r:    Default power is %s' % (self, default_power))
        self.logger.info('%r:    Power exceptions are %s' % (self, exceptions))

        for ib in self.ib:
            bp = ib.BP_SHUFFLE
            power_tuples = [(lane, exceptions.get(ib.get_id(lane), default_power)[index])
                            for lane, gtx in enumerate(bp.get_gtx(lane_group=lane_group)) if gtx]
            self.logger.info('%r: setting Tx power for %r %s links' % (self, ib.handler, lane_group))
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
                    ib.set_funcgen_function('ab', a=(slot-1)<<4, b=ch<<4, channels=[ch])
                ib.set_data_source('funcgen')

    def set_sync_method(self, method='distributed_time', source='bp_time', master=None, master_time_source=None):
        """ Sets the global syncing method, and setup the boards accordingly.

        Parameters:

        method: (string)
            - 'distributed_time': All boards receive and decode IRIG-B time
              signal and trigger a SYNC event at a target time sent to every
              board in the array.

              The IRIG-B time signal can come from either of the backplane
              SMAs TRIG (``source='bp_trig'``) or TIME (``source='bp_time'``).
              If ``source='bp_gpio_int'``, a master board must be specified
              and the internal GPIO_INT backplane line is used to send the
              master's board IRIG-B signal to all boards in the crate.


              If a master board ``master`` is specified, the master board is
              configured to output an IRIG-B signal on its SMA connector or on
              the backplane GPIO_INT line depending on the value of
              ``source``. ``master_time_source`` determines the source of the
              IRIG-B signal provided by the master board, which can come from one of
              the backplane SMAs or from its IRIG-B test signal generator.

              If no master board is specified, the IRIG_B must be generated by
              an external source (like a GPS receiver) and must be connected
              to the specified backplane SMA.


            - 'centralized_time_trigger': All boards receive a SYNC trigger
              from the master board when its IRIG-B decoder reaches the target
              time.

              Depending on ``source``, the trigger signal can be received from
              the backplane SMA connectors or the BP_GPIO_INT backplane line.

              A master board ``master`` must be specified and is configured to
              generate the time-based trigger signal on its BP_GPIO_INT line
              or on its SMA connector (in which case a cable must connect the
              master board and the backplane). The master board's IRIG-B
              signal source is set by ``master_time_source`` and can be set to
              come from the backplane or its internal test generator.

            - 'centralized_soft_trigger': All boards receive a SYNC trigger
              from the master when it receives a software command to do so.
              The trigger signal is received from the backplane SMA input
              connectors. The master board is configured to generate this
              trigger signal on its SMA connector.

            - 'local_soft_trigger': Each board generates its won SYNC trigger
              when it receives a software command to do so.


        source: (string): source of the time or trigger signal for the slave boards, and destination for the time or trigger signal generated by the master board (if used).
            - 'bp_gpio_int': the signal comes from the internal backplane
              GPIO_INT line. The master board must be specified, and will be
              configured to generate the time or trigger signal on the BP_GPIO_INT line.
            - 'bp_time': the signal comes from the backplane TIME SMA. If a master board is
              specified, it it configured to send the time or trigger signal through the SMA port.
            - 'bp_trig': the signal comes from the backplane TRIG SMA. If a master board is
              specified, it it configured to send the time or trigger signal through the SMA port.

        master (IceBoard object or string): IceBoard that is to be configured to generate the time
            or trigger signals. Can be omitted if an external IRIG-B source
            is used (like a GPS receiver).

        master_time_source: (string): source of the time signal that the
            master board will use to generate the time or trigger signal.
            Not applicable for soft trigger, must be specified for centralized
            time triggers, and may be specified for distributed time syncing.
            - 'bp_time': the time signal comes from the backplane TIME SMA.
            - 'bp_trig': the time signal comes from the backplane TRIG SMA.
            - 'irigb_gen': the time signal comes from the internal IRIG-B test signal generator.



        The boards start their sync process on the next rising edge of the 10
        MHz refecence clock following when either the target irigb time is
        reached or the trigger signal is received. These events are configured
        to occur between two reference clock edges in order to guarantee
        detection on the same edge across the entire array.


          Sync Method                         source                           master     master_time_source
        ------------------------          -------------------------------   ------------  -----------------------------
        distributed_time (ext source)     bp_time | bp_trig                  Not needed           ---
        distributed_time (master source)  bp_time | bp_trig | bp_gpio_int      Needed     bp_time | bp_trig | irigb_gen
        centralized_time_trigger          bp_time | bp_trig | bp_gpio_int      Needed     bp_time | bp_trig | irigb_gen
        centralized_soft_trigger          bp_time | bp_trig | bp_gpio_int      Needed             ---

        """
        if isinstance(master, str):
            master = self.ib.get(serial=master)

        self.sync_master = master
        self.sync_method = method

        if method == 'distributed_time':
            self.ib.set_sync_source('irigb')
            self.ib.set_irigb_source(source)
            if source in ['bp_time', 'bp_trig']:
                if bool(master) != bool(master_time_source):
                    raise ValueError('The master board that generates the the time signal on its SMA connector and its time source must be specified')
                if master:
                    master.set_user_output_source(master_time_source)
            elif source in ['bp_gpio_int']:
                if not master or not master_time_source:
                    raise ValueError('With the bp_io_int source, the master board and its time source must be specified')
                if master_time_source not in ['irigb_gen']:
                    raise  NotImplementedError("The 'bp_gpio_int' source currently only supports the master_time_source=irigb_gen'")
                self.ib.GPIO.BP_GPIO_INT_EN = 0
                master.GPIO.BP_GPIO_INT_EN = 1
                #self.ib.set_bp_gpio_int_output_source(None)  # Make sure no other board is driving the backplane line
                #master.set_bp_gpio_int_output_source(master_time_source)
        elif method == 'centralized_time_trigger':
            if not master or not master_time_source:
                raise ValueError('In the centralized time trigger mode, the master board and its time source must be specified')
            self.ib.set_sync_source(source)
            master.set_irigb_source(master_time_source)
            master.set_user_output_source('irigb_trig')
        elif method == 'centralized_soft_trigger':
            if not master:
                raise ValueError('In the centralized soft trigger mode, a master board must be specified')
            if master_time_source:
                raise ValueError('In the centralized soft trigger mode, no master_time_source must be specified')
            self.ib.set_sync_source(source)
            master.set_user_output_source('sync')
        elif method == 'local_soft_trigger':
            if master:
                raise ValueError('In the local soft trigger mode, a master board should NOT specified')
            if master_time_source:
                raise ValueError('In the local soft trigger mode, a master_time_source should NOT be specified')
            self.ib.sync()
        else:
            raise ValueError("Unknown syncing method '%s'" % method)

    def sync(self, delay=2-0.006556800, check=True, align_to_seconds=True, max_trials=3):
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
        trial = 0
        while True:
            try:
                if check:
                    sync_ctr_before = self.ib.REFCLK.SYNC_CTR

                self.sync_timestamps = []
                self.sync_timestamp = None

                if self.sync_method == 'centralized_soft_trigger':
                    self.sync_master.remote_sync()
                elif self.sync_method == 'centralized_time_trigger':
                    dt = self.sync_master.get_irigb_time()
                    print 'Triggering SYNC at ', dt.isoformat()
                    self.sync_master.set_irigb_trigger_time(dt, delay=delay)
                    t0 = time.time()
                    while self.sync_master.is_irigb_before_trigger_time():
                        if time.time() - t0 > delay+1:
                            raise RuntimeError('Timout while waiting for the IRIG-B-based SYNC to complete')
                elif self.sync_method == 'distributed_time':
                    # Estimate how much time it takes to set the trigger time
                    dt = self.ib[0].get_irigb_time()
                    t0 = time.time()
                    self.ib.set_irigb_trigger_time(dt, delay=300) # set the trigger far enough in time it should not happen before we reprogram another delay
                    setting_time = (time.time() - t0)
                    self.logger.info('%r: It takes %f seconds to set the trigger time across the array' % (self, setting_time))
                    setting_time = round(2*setting_time) + delay
                    # Now set the trigger time using that delay
                    dt = self.ib[0].get_irigb_time()
                    if align_to_seconds:
                        self.logger.info('%r: Rounding trigger time to the second' % self)
                        dt = dt.replace(microsecond=0)
                    self.logger.info('%r: Triggering SYNC %f seconds after %s' % (self, setting_time,  dt.isoformat()))
                    self.print_flush()
                    t0 = time.time()
                    self.sync_start_time = sync_time = self.ib.set_irigb_trigger_time(dt, delay=setting_time)
                    self.logger.info('%r: It took %f seconds to set the final trigger time' % (self, time.time() - t0))
                    t0 = time.time()
                    while any(self.ib.is_irigb_before_trigger_time()):
                        if time.time() - t0 > setting_time + 1:
                            raise RuntimeError('Timout while waiting for the IRIG-B-based SYNC to complete')
                elif self.sync_method == 'local_soft_trigger':
                    self.ib.sync()
                else:
                    raise ValueError("Unknown syncing method '%s'" % self.sync_method)

                if check:
                    sync_ctr_after = self.ib.REFCLK.SYNC_CTR
                    bad_ib = [ib for i,ib in enumerate(self.ib) if (sync_ctr_after[i] - sync_ctr_before[i]) & 0xf != 1]
                    if bad_ib:
                        raise RuntimeError('The following IceBoards did not SYNC properly: %s' % (','.join(repr(ib) for ib in bad_ib)))

                if self.sync_method == 'centralized_time_trigger' or self.sync_method == 'distributed_time':
                    self.sync_timestamps = ts = self.ib.get_irigb_time(trig=False, format='raw')
                    self.sync_timestamp = ts[0]

                    delta_ts = max(ts.nano) - min(ts.nano)
                    self.logger.info('%r: The IRIG-B time for Frame 0 on all boards is:\n%s' %
                        (self, '\n'.join('%r: %s (%i ns since epoch, %i ns after sync)' % (
                            ib.handler,
                            ts[i].isoformat(),
                            ts[i].nano,
                            ts[i].nano - sync_time[i].nano)
                        for i ,ib in enumerate(self.ib))))
                    self.logger.info('%r: The maximum Frame 0 time difference is %i ns' % (self, delta_ts) )
                    if delta_ts > self.max_sync_time_difference:
                        raise RuntimeError('The Frame 0 time difference of %i exceeds the maximum limit of %i' % (delta_ts, self.max_sync_time_difference))

                for ib in self.ib:
                    for ant in ib.ANT:
                        ant.SCALER.OVERFLOW_RESET = 1
                        ant.SCALER.OVERFLOW_RESET = 0
                break
            except Exception as e:
                trial += 1
                if trial >= max_trials:
                    raise RuntimeError('SYNC failed after %i trials. The last exception was:\n%r' % (trial, e))
                self.logger.warn('%r: SYNC failed on trial %i/%i due to the following error. Will retry.\n%r' % (self, trial, max_trials, e))
    @async
    def set_channelizers(self, adc_mode=None, adcdaq_mode=None,
                         data_source=None, function=None, a=1, b=0, freq_test_bins=None,
                         fft_bypass=None, fft_shift=None,
                         scaler_bypass=None, gain=None, postscaler=None, offset_binary_encoding=None,
                         sync=True,
                         channels=None):
        """
            Configures the operations of all channelizers for all boards in the array.

            See IceBoard's set_channelizer(...) for details.
        """
        for ib in self.ib:
            ib.set_channelizer(adc_mode=adc_mode, adcdaq_mode=adcdaq_mode,
                        data_source=data_source, function=function, a=a, b=b, freq_test_bins=freq_test_bins,
                        fft_bypass=fft_bypass, fft_shift=fft_shift,
                        scaler_bypass=scaler_bypass, gain=gain, postscaler=postscaler,
                        offset_binary_encoding=offset_binary_encoding)
            yield async_moment
        if sync:
            self.sync()

    def set_noise_injection(self, board, enable=False, offset=0, high_time=8388608, period=16777216, local_sync=True, output='bp_sma'):
        """ Configure noise injection gating signal.

        ``board`` is either the serial number (as a string) of the target board, or is the target IceBoard object.

        A sync event is necessary to restart the counters so the gating signal will be generated properly.

        """
        board = self.get_iceboard(board)

        if enable:
            board.set_user_output_source('pwm', output=output)

        board.set_pwm(enable=enable, offset=offset, high_time=high_time, period=period, local_sync=local_sync)

    # def get_current_gain_bank(self):
    #     return [ib.get_current_gain_bank() for ib in self.ib]

    def get_stream_id_map(self):
        """ Return the stream_ids if every channel of the array, indexed by channel_id.

        Returns:
            dict if the format {channel_id:stream_id}, where channel_id is a (crate, slot, channel) tuple.
        """

        stream_id_map = {}
        for ib in self.ib:
            stream_id_map.update(ib.get_stream_id_map())

        if len(stream_id_map) != len(set(stream_id_map.values())):
            self.logger.warn('%r: Stream IDs are not unique!')

        return stream_id_map


    def get_iceboard(self, board):
        """ Return the ICEBoard specified by tuple or serial number.

        If board is already an IceBoard object, it should be returned.
        """
        if not self.ib:
            raise RuntimeError('There are no Iceboard to select in the current array')
        elif isinstance(board, type(self.ib[0])):
            return board
        elif isinstance(board, basestring):
            if board in self.ib.serial:
                return self.ib.get(serial=board)
            elif board in self.ib.hostname:
                return self.ib.get(hostname=board)
            else:
                raise RuntimeError('%r: Invalid Board serial number %s. Valid serial numbers are %s' %
                    (self, board, ','.join("'%s'" % ib.serial for ib in self.ib)))
        elif isinstance(board, (tuple, list)) and len(board) == 2:
            matches = [ib for ib in self.ib if tuple(board) == ib.get_id()]
            if not matches:
                raise RuntimeError('board ID %r did not match any board in the array: %s' % (board, [ib.get_id() for ib in self.ib]))
            elif len(matches) > 1:
                raise RuntimeError('board ID %r Matched multiple boards: %s' % ','.join(ib.get_string_id() for ib in matches))
            else:
                return matches[0]
        else:
            raise AttributeError('%r: Invalid Iceboard specification %s' % (self, board))

    def get_iceboard_from_id(self, id):
        """ return the iceboard corresponding to the specified (crate,slot) tuple. ``slot`` is zero-based.
        """
        return self.get_iceboard(id)

    def get_iceboards(self, board_ids=None, lane_type=None):
        """ Return the iceboard object(s) corresponding to the board ID tuples or dict specified in `ib` , including wildcards.

        Parameters:

            board_ids (list of tuple): List of (crate_number, slot_number) tuple describing the an iceboard. A value of None
                is equivalent to a '*' wildcard. Missing tuple entries are considered to be None.

            lane_type (str): type of lane  that can be specified in the spec:
                ('channel', 'gpu', 'bp_pcb', 'bp_qsfp'). If 'None', lane
                numbers are not decoded nor returned.

        Returns:
            if `lane_type` is None: list of iceboard objects
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

        iceboards = {} # List of matching iceboards and corresponding lane/channels
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
                        raise RuntimeError("Unknown element '%s' in iceboard selection item %s" % (k, ib))
            else:
                raise ValueError('Unknown iceboard selection format %s', ib)

            # Convert the wildcard '*' into None
            crate_number = None if crate_number == '*' else crate_number
            slot_number = None if slot_number == '*' else slot_number
            lane_number = None if lane_number == '*' else lane_number

            #print('get_iceboard: looking for ', crate_number, slot_number)
            for ib in self.ib:
                crate, slot = ib.get_id()
                #print('   checking', ib_id)
                if (crate_number is None or crate_number == crate) and (slot_number is None or slot_number == slot):
                    lanes = iceboards.setdefault(ib, set()) # get the iceboard's lanes. Create an entry with an emply list if it does not exist
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
                            raise ValueError('Invalid lane number %s for lane type %s. Valid values are %s' % (lane_number, lane_type, valid_lanes))
                        else:
                            lanes.add(lane_number)
        if lane_type is None:
            return iceboards.keys()
        else:
            return iceboards

#    def init_gains(self):
#        """ Should be deprecated. Use load_gains() instead.
#        """
#        for ib in self.ib:
#            try:
#                g_array = pickle.load(open('/home/chime/ch_acq/gains_'+str(ib.GPIO.FPGA_SERIAL_NUMBER)+'.pkl', 'rb'))
#            except:
#                g_array = pickle.load(open('/home/chime/ch_acq/gains.pkl', 'rb'))
#                print 'Could not find gain settings for %r, sn %i. Using default gain settings.' % (ib, ib.get_fpga_serial_number())
#            print 'Setting gains on IceBoard SN%s' % ib.serial
#            ib.set_gain(g_array)
    @async
    def load_gains(self, bank=0, gain_folder='/home/chime/ch_acq/gains'):
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
            self.logger.info('%r: Reading digital gains for (crate,slot)=%r' %
                             (self, board_id))
            board_gains = ib.load_gains(folder=gain_folder) or default_gains
            yield async_moment

            if not board_gains:
                self.logger.warn('%r: Neither board-specific gain file not default gain file was found for (crate,slot)=%r' % (self, board_id))
                array_gains[board_id] = None
            else:
                array_gains[board_id] = board_gains
        async_return(array_gains)

    @async
    def save_gains(self, gains, gain_folder='/home/chime/ch_acq/gains'):
        """ Save the gains.

        Parameters:

            gains (): Gains in the format {(crate,slot,channel):(glin, glog), ...} or {(crate, slot):{channel:(glin,glog),...},...}

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


    @async
    def get_gains(self, bank=0, use_cache=True):
        """ Return the digital gains programmed in the specified bank for all channels of all boards of the array.

        Parameters:

            `bank`: gain bank from which the gains are read.

            `use_cache`: True to allow the software-cached value to be used (much faster the reading back from the FPGAs)

        Returns:

            Digital gains, in a channel-indexed dict, in the format::

                {chan_id: (glin, glog), ...}

                where:
                     ``chan_id`` is a (crate, slot, channel) tuple that uniquely identifies a channel
                     ``crate``= int or str or None
                     ``slot``= int or str
                     ``glin` = array of 1024 (int16 + 1j* int16) linear gain components
                     ``glog`` = post-scaler factor (applies additional gain of 2**glog to all bins)

        """
        gains = {}
        for ib in self.ib:
            for ch, ch_gains in ib.get_gains(bank=bank, use_cache=use_cache):
                gains[ib.get_id(ch)] = ch_gains
            yield async_moment
        async_return(gains)


    @async
    def get_gain_timestamps(self, bank=0):
        """
        Returns the timestamp at which the gains for each channel of thewas set.

        Parameters:

            bank (int): gain bank from which to get the gains timestamp.

        Returns:

            Timestamp of thd digital gains, in a channel-indexed dict, in the format::

                {chan_id: timestamp, ...}

                where:
                     ``chan_id`` is a (crate, slot, channel) tuple that uniquely identifies a channel
                     ``crate``= int or str or None
                     ``slot``= int or str
                     ``timestamp` = is a time.time() value. None if the gain was not set.
        """
        timestamps = {}
        for ib in self.ib:
            for ch, timestamp in ib.get_gain_timestamps(bank=bank):
                timestamps[ib.get_id(ch)] = timestamp
            yield async_moment
        async_return(timestamps)

    @async
    def set_gains(self, gains, bank=-1,  when='now', gain_timestamps=None):
        """ Set the gains on the boards in the array.

        Parameters:
            'gains': dictionary of gains specified as either:
                - {board_id: {chan: (glin, glog), ...}, ...}
                - {chan_id: (glin, glog), ...}
                where:
                     ``board_id`` is a (crate, slot) tuple that uniquely identifies a board
                     ``channel_id`` is a (crate, slot, channel) tuple that uniquely identifies a channel
                     ``crate``= int or str or None
                     ``slot``= int or str
                     ``glin` = array of 1024 (int16 + 1j* int16) linear gain components
                     ``glog`` = post-scaler factor (applies additional gain of 2**glog to all bins)

            `bank`: gain bank in which the gains are written. If `bank`=-1 or is None, gains are
                    written in the inactive bank (which can be activated later using set_gain_bank()).

            `when`: if `when` is 'now' or a negative integer, the target gains
                    are made active immediately.

                    If `when` is None, the gains are written in the specified bank but the bank switching is not activated.

                    If `when` is an integer, the gains will be activated starting on
                    the target timestamp specified by `when`.

            `gain_timestamps`: dictionary of unix timestamps with same key format as `gains`
                               or single unix timestamp that is applied to all channels.

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
            self.logger.info('%r: Setting digital gains for (crate,slot)=%r (%s)' % (self, board_id, ib.get_formatted_id()))
            ib.set_gains(gain=g, bank=bank, when=None, gain_timestamp=gain_timestamp)
            yield async_moment

        if when is not None:
            self.switch_gains(bank=bank, when=when)



    def group_gains_per_board_id(self, channel_based_gains):
        """ Convert a channel_id based gain table into a board_id-based gain table.

        Parameters:
            channel_based_bains (dict): dict containing channel-based gain  entries in the format {channel_id: gains, ...}, where ``channel_id`` is a 3-element (crate, slot, channel)tuple

        Returns:
            A board_id-based gain table in the format {board_id: {channel:gains,...},...}.

        Note:
            - Entries that are not a 3-element tuples are left untouched, allowing a board-id based table or another dict to be passed.

        """
        board_based_gains = {}
        for id_, gains in channel_based_gains.items():
            if isinstance(id_, (tuple, list)) and len(id_) == 3:
                board_based_gains.setdefault(tuple(id_[:-1]), {})[id_[-1]] = gains
            else:
                board_based_gains[id_] = gains
        return board_based_gains

    @async
    def compute_gains(self, enable=True, slots=None, noise_injection=None, gain_folder='/home/chime/ch_acq/gains'):
        """ Compute the gains of the SCALER module so that the conversion of the FFT output
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
            self.logger.info('Could not import calculate_gains. Missing timestream_receiver in path?')
            return

        # Setup noise injection using noise injection parameters that are specific to the gain calculation operation.
        if noise_injection is not None:
            for source_name, source_params in noise_injection.items():
                if source_params.board:
                    self.logger.info("Setting gain computation noise injection for source '%s' with parameters %s" %
                                    (source_name, source_params))
                    self.set_noise_injection(local_sync=True, **source_params)

        # Loop over boards and compute gains
        self.logger.info("Computing SCALAR gains.")
        for ib in self.ib:
            ch_id = ib.get_id()
            # crate, slot_0based = ch_id[0], ch_id[1]
            if (slots is None) or slots[ch_id[0]][ch_id[1]]:
                yield calculate_gains.calculate_gains.async(ib, gain_folder=gain_folder)

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

    def switch_gains(self, bank=-1, when='now'):
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





    @async
    def reset_corr(self, delay=0.1):
        self.ib.set_corr_reset(1)
        yield async_sleep(0.1)
        self.ib.set_corr_reset(0)

    @async
    def reset_gpu_links(self, board_ids=None):
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

            - It is not possible to selectively reset just one QSFP or a specicfic lane of a QSFP. All lanes for both QSFPs are reset.

        """
        ibs = self.get_iceboards(board_ids)
        for ib in ibs:
            ib.reset_gpu_links()
            yield async_moment
        async_return([ib.get_id() for ib in ibs])

    @async
    def get_fpga_config(self, basic=False):
        """ Concurrently gets the configuration info for each FPGA """
        configs = yield {ib.get_id():ib.get_config.async(basic=basic) for ib in self.ib}
        async_return(configs)



    def get_frequency_map(self, format='cscb'):
        """ Returns a map describing the content (crate, slot, channel, bin) of every packet at the output of the corner turn engine.

        This map is obtained by passing the channelizer identity map through the shuffle map.
        """

        return self.get_shuffle_output(self.get_chan_identity_map(format=format))

    def get_chan_identity_map(self, format='cslb'):
        """ Return an identity map that describes the origin of each of the 1024 samples contained in the channelizer output packets.
        The map is a dict:
            {channelizer_id: [sample_id0, ... sample_id1023]}
        where channelizer_id is represented by the tuple (crate_number, slot_number, channel_number) and
        each sample_id is the tuple (crate_number, slot, channel, bin_number)

        This map can be propagated through the shuffle map (see
        `apply_shuffle_map` method) to obtain the contents of the output of
        the corner-turn engine.
        """
        ch_out = OrderedDict()
        for ib in self.ib:
            for (crate, slot, lane) in ib.get_channel_ids():

                if format == 'l:cscb': # Unique (crate, slot, local_channel)
                    ch_out[(crate, slot, lane)] = [(crate, slot, lane, bin) for bin in xrange(1024)]
                elif format == 'l:cc': # Non-unique global channel numbers (repeated for each bin)
                    ch_out[(crate, slot, lane)] = [crate*256 + slot*16 + lane for bin in xrange(1024)]
                elif format == 'l:cb': # Unique (global channel, lane) tuple
                    ch_out[(crate, slot, lane)] = [(crate*256 + slot*16 + lane, bin) for bin in xrange(1024)]
                elif format == 'l:bb': # Non unique bin_number (repeated for each channel)
                    ch_out[(crate, slot, lane)] = [bin for bin in xrange(1024)]
        return ch_out

    def get_chan_output(self):
        ch_out = OrderedDict()
        for ic in self.ic:
            for ib in ic.slot.values():
                for ant in ib.ANT.values():
                    ch_out[ant.get_id()] = ant.FUNCGEN.get_buffer()
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

        cb1_out = OrderedDict()
        for ib in self.ib:
                cb1_in = {ch: chan_map[(crate, slot, ch)] for (crate, slot, ch) in ib.get_channel_ids()} # extract channels for this inceboard only
                for lane, data in ib.CROSSBAR.map(cb1_in).items():
                    cb1_out[ib.get_id(lane)] = data

        # Apply pcb shuffling
        pcb_shuffle_out = OrderedDict()
        for ic in self.ic:
            pcb_link_map = ic.get_pcb_link_map()
            for (rx_slot, rx_lane), (tx_slot, tx_lane) in pcb_link_map.items():
                pcb_shuffle_out[(ic.crate_number, rx_slot-1, rx_lane)] = cb1_out[(ic.crate_number, tx_slot-1, tx_lane)]

        # Apply CROSSBAR2
        cb2_out = OrderedDict()
        for ib in self.ib:
            (crate, slot) = ib.get_id()
            cb2_in = {lane: pcb_shuffle_out[(crate, slot, lane)] for lane in xrange(ib.BP_SHUFFLE.NUMBER_OF_PCB_LANES)} # extract channels for this inceboard only
            for lane, data in ib.CROSSBAR2.map(cb2_in).iteritems():
                cb2_out[(crate, slot, lane)] = data

        # Apply QSFP shuffling
        qsfp_shuffle_out = OrderedDict()
        for ib in self.ib:
            (crate, slot) = ib.get_id()
            bypass = ib.BP_SHUFFLE.BYPASS_QSFP_SHUFFLE
            number_of_qsfp_lanes = ib.BP_SHUFFLE.NUMBER_OF_QSFP_LANES
            for rx_lane in range(number_of_qsfp_lanes):
                crate_offset = rx_lane * 2 // number_of_qsfp_lanes if not bypass else 0
                qsfp_shuffle_out[(crate, slot, rx_lane)] = cb2_out[(crate ^ crate_offset, slot, rx_lane)]

        # Apply CROSSBAR3
        cb3_out = OrderedDict()
        for ib in self.ib:
            (crate, slot) = ib.get_id()
            cb_in = {lane: qsfp_shuffle_out[(crate, slot, lane)] for lane in xrange(ib.BP_SHUFFLE.NUMBER_OF_QSFP_LANES)} # extract channels for this inceboard only
            for lane, data in ib.CROSSBAR3.map(cb_in).iteritems():
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
        for i,bb in enumerate(c):
            bb.REFCLK.set_sync_source('bp')#bb.REFCLK.SLAVE=1
            sync_ctr[i] = bb.REFCLK.SYNC_CTR

        fail=0
        for test_number in range(10):
            print 'Trial # %i: Sending SYNC pulse from Slot %02i (Iceboard SN%s)' % (test_number+1, sync_board.slot, sync_board.serial)
            sync_board.REFCLK.local_sync()
            for i,bb in enumerate(c):
                new_sync_ctr = bb.REFCLK.SYNC_CTR
                diff = (new_sync_ctr - sync_ctr[i]) & 0xF
                sync_ctr[i] = bb.REFCLK.SYNC_CTR
                fail += bool(diff!=1)
                print '    Slot %02i (Iceboard SN%s): Sync counter = %2i, diff = %2i => %s' % (bb.slot, bb.serial, new_sync_ctr, diff, ('FAILED!', 'PASS')[bool(diff==1)])
            time.sleep(0.2)
        if fail:
            print 'SYNC Test has FAILED!'
        else:
            print 'SYNC Test has PASSED!'


    def soft_sync(self, sync_board):
        """ Synchronize all boards"""
        boards = list(self.ib)
        print 'Masking ADC data before sync'
        for ib in boards:
            for ant in ib.ANT:
                ant.ADCDAQ.BYTE_MASK = 0

        print 'Initiating global sync'
        sync_board.REFCLK.local_sync()

        print 'Unmasking ADC data'
        for ib in boards:
            for ant in ib.ANT:
                ant.ADCDAQ.BYTE_MASK = 255

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
    #     sync_board.set_irigb_trigger_time(current_time, delay)

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
    #         ib.set_irigb_trigger_time(current_time, delay)


    def print_temperatures(self):
        t = [(b.slot, b.serial, b.SYSMON.temperature()) for b in self.ib]
        t.sort()
        for (slot, serial_number, fpga_temp) in t:
            print 'Slot %2i (SN%s): FPGA %2.1f C' % (slot, serial_number, fpga_temp)

    def set_adc_mask(self, value):
        for ib in self.ib:
            for ant in ib.ANT:
                ant.ADCDAQ.BYTE_MASK = value

    def print_fmc_power(self):
        sensor_list = ['FMCA_12V0', 'FMCA_3V3','FMCA_VADJ','FMCB_12V0','FMCB_3V3','FMCB_VADJ']

        for b in self.ib:
            for sensor in sensor_list:
                (voltage, current, power) = b.hw.get_power(sensor)[sensor]
                if voltage is not None:
                    print '%0.1fV@%0.2fA=%0.1fW ' % (voltage, current, power),
                else:
                    print 'None                 ',
            print


    def detect_backplane_links(self, tx_power=7, print_=True):
        """ Setup all boards on the array to send test pattern over the
        backplane link and detect  from which slot/lane every board is
        receiving data.

        The test is performed only on IceBoards that are installed in crates
        and whose FPGA has been programmed and initialized. Other boards are
        ignored.

        For now, this test works only if all boards are in a single crate.
        """
        # select only boards on crates and that are open
        array = Ccoll(ib for ib in self.ib if ib.is_open() and ib.crate and ib.slot)

        if not array:
            raise RuntimeError('There are no boards that are opened() AND connected in a crate')

        # Check if the board are all on a single crate
        if len(set(array.crate)) != 1:
            raise RuntimeError('Sorry, this method currently can work on only one crate. The currently active boards span multiple crates %s' % list(set(array.crate)))

        active_slots = [ib.slot for ib in array]
        if len(set(active_slots)) != len(active_slots):
            raise SystemError('Slot numbers are not unique!')

        print 'Setting Transmitted ID'
        for ib in array:
            ib.BP_SHUFFLE.TX_DATA_MSB = 0xFF00 + ib.slot
            for gtx_number, g in enumerate(ib.BP_SHUFFLE.gtx):
                g.SOURCE_SEL = 1 # 0:Send TXDATA , 1: SEND 10G Ethernet test packet
                g.LOOPBACK = 0
                g.TXPRBSSEL=0
                g.RXPRBSSEL=0
                g.TXDIFFCTRL = tx_power
                g.TX_DATA_LSB = gtx_number + 1
                g.TXHEADER=1
                g.CAPTURE_ENABLE = 1
                g.TXPRECURSOR = 0b00000 #DFE cannot compensate pre-cursor
                g.TXPOSTCURSOR = 0b00000
                g.RXLPMEN = 0 #Go to DFE mode instead of LPM
                # g.RXMONITORSEL = 1 # 1=AGC, 2=UL, 3=VP loop
                # g.RX_DEBUG_CFG = 0b1011 << 2
                #g.DMONITOR_CFG1 = 0
                #g.DMONITOR_CFG0 = (0b1<<15) | (0x0080 <<1) | 1
                # Configure DMONITOR to read the AGC gain
                # g.DMONITOR_SELECT = 1
                # g.PCS_RSVD_ATTR_BIT6 = 1
                #old=g.read_drp(0x6f)
                #g.write_drp(0x6f,old | 1<<6)
                #g.RXDFEOVRD=1
                #g.write_drp(0x1d, 0x00ea)

        print 'Resetting the GTXes'
        for ib in array:
            for g in ib.BP_SHUFFLE.gtx:
                g.RXDFELPMRESET = 1
                g.RXDFELPMRESET = 0
            time.sleep(0.1)

        print 'Checking received data'
        link_list = []
        link_matrix = [[None]*17 for x in range(17)]
        serial_number = ['N/A'] * 17
        for ib in array:
            #print 'Slot %i' % (ib.slot)
            dest_slot = ib.slot
            serial_number[dest_slot-1] = ib.serial
            for gtx_number, g in enumerate(ib.BP_SHUFFLE.gtx[0:ib.BP_SHUFFLE.NUMBER_OF_PCB_LINKS]): # JM: Fixed this bc was getting an error. JF please check
                dest_lane = gtx_number + 1
                dest = (dest_slot, dest_lane)
                expected_source = ib.crate.get_matching_tx(dest)
                source_present = expected_source[0] in active_slots
                for trial in range(3):
                    rxdata = g.get_rxdata()
                    #print '   Slot %i Lane %i received %08X' % (    ib.slot, lane+1,  rxdata)
                    source_slot = int((rxdata >> 8) & 0xFF)
                    source_lane = int((rxdata) & 0xFF)
                    source = (source_slot, source_lane)
                    source_valid = (rxdata >> 16) == 0xFFFF
                    maybe = (rxdata != 0x55555555) and (rxdata != 0xAAAAAAAA)
                    if source_valid:
                        break

                if source_valid:
                    print 'Slot %2i Lane %2i is receiving data from Slot %2i Lane %2i (received word = 0x%08X, RXMONITOROUT= %x, DMONITOROUT=%x)' % (dest_slot, dest_lane, source_slot, source_lane, rxdata, g.RXMONITOR, g.DMONITOROUT)
                    link_matrix[dest_slot][dest_lane] = 'S%02iL%02i' % (source_slot, source_lane)
                elif maybe:
                    print 'Slot %2i Lane %2i is receiving some data but cannot determine source (received word = 0x%08X, RXMONITOROUT= %x, DMONITOROUT=%x)' % (dest_slot, dest_lane, rxdata, g.RXMONITOR, g.DMONITOROUT)
                    link_matrix[dest_slot][dest_lane] = 'S??L??'
                else:
                    link_matrix[dest_slot][dest_lane]='  ()  '

                if not source_valid or not source_present or source != expected_source:
                    if source_present:
                        link_matrix[dest_slot][dest_lane] += '/S%sL%s  ' % expected_source
                    else:
                        link_matrix[dest_slot][dest_lane] += '/(S%sL%s)' % expected_source

                link_list.append((source, dest))
        if print_:
            # Print a slot map
            print 'Slot-> ' + ' '.join(['%-15i' % (slot+1) for slot in range(16)])
            print 'S/N -> ' + ' '.join(['%-15s' % (sn) for sn in serial_number])
            print 'Lane   ' + ' '.join(['%-15s' % '----------' for x in range(16)])

            for dest_lane in range(1,16):
                print '%6i ' % (dest_lane),
                for dest_slot in range(1,17):
                    print '%-15s' % link_matrix[dest_slot][dest_lane],
                print

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

                {('pcb', (tx_crate, tx_slot, tx_lane), (rx_crate, rx_slot, rx_lane)) : (tx_gtx_instance, rx_gtx_instance)}
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

            A resolved, iceboard- and lane-oriented connectivity list in the format:

                [('BP_QSFP', (tx_crate, tx_iceboard_slot, tx_lane), (rx_crate, rx_iceboard_slot, rx_lane) ), ...]

            The list refers to logical lane numbers (including the internal
            bypass lanes). Note that the internal (bypass) links are not added
            to the result.


        """


        # tx_nodes = {}
        # rx_nodes = {}

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
            # If the second node is not already known, search all the links for a corresponding half-link with the same link_id
            if rx_id is None:
                matching_nodes = [nid1 for (lt, nid1, nid2, lid) in raw_links if lt==link_type and nid1 != tx_id and nid2 is None and lid==link_id]
                if len(matching_nodes) == 1:
                    rx_id = matching_nodes[0]
            if tx_id is None or rx_id is None:
                continue
            (source_crate, source_slot, source_lane) = tx_id
            (dest_crate, dest_slot, dest_lane) = rx_id
            ic0 = crates[source_crate]
            ic1 = crates[dest_crate]
            if (source_slot not in ic0.slot) or (dest_slot not in ic1.slot):
                continue
            bp0 = ic0.slot[source_slot].BP_SHUFFLE
            bp1 = ic1.slot[dest_slot].BP_SHUFFLE
            links.append((link_type, (source_crate, source_slot, source_lane + bp0.NUMBER_OF_QSFP_DIRECT_LANES), (dest_crate, dest_slot, dest_lane + bp1.NUMBER_OF_QSFP_DIRECT_LANES)))  # convert from gtx index to logical lane #

        return links

    def get_backplane_qsfp_link_map(self, resolve=True):
        """ Return a dictionary that maps the backplane QSFP links to
        corresponding GTX transmitter and receiver instances.

        Returns:
           A dict, in the format:

                {('qsfp', (tx_crate, tx_slot, tx_lane), (rx_crate, rx_slot, rx_lane)) : (tx_gtx_instance, rx_gtx_instance)}

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

        # Ask each crate the map that matches backplane links to cable ids (this takes time: we need to read the cable identification)
        bp_to_cable_map = {}
        for ic in self.ic:
            bp_to_cable_map.update(ic.get_qsfp_cable_map())

        # Create a map that matches each cable id to a list of correspinding logical link ids (there should be 2 for each link)
        cable_to_link_map  = {} # {cable_id: (crate, slot, logical_lane)}
        for (bp_id, cable_id) in bp_to_cable_map.items():
            cable_to_link_map.setdefault(cable_id, []).append(bp_to_logical_link_map[('qsfp', bp_id)])

        # Crate a map that matches each logical link id with another logical link id.
        link_map = {}
        for cable_id, link_ids in cable_to_link_map.items():
            if len(link_ids) == 1:
                self.logger.warning('Only one end of a QSFP cable is connected; Cable ID %s connects only to %s. The link will be ignored.' % (cable_id, link_ids[0]))
            elif len(link_ids) > 2:
                raise RuntimeError('A QSFP cable connects to more than 2 links. Something is wrong. Cable ID %s connects only to %s.' % (cable_id, link_ids))
            elif len(link_ids) == 2:
                link_map[link_ids[0][1]] = link_ids[1][1]
                link_map[link_ids[1][1]] = link_ids[0][1]
        print link_map

        # Resolve each unresolved link.
        resolved_qsfp_link_map = {}
        for (link_type, tx_id, rx_id), (tx_gtx, rx_gtx) in qsfp_link_map.items():
            if not tx_id and rx_id and rx_id in link_map:
                tx_id = link_map[rx_id]
                tx_gtx,_ = qsfp_link_map[('qsfp', tx_id, None)]
            elif not rx_id and tx_id and tx_id in link_map:
                rx_id = link_map[tx_id]
                _, rx_gtx = qsfp_link_map[('qsfp', None, rx_id)]
            resolved_qsfp_link_map[link_type, tx_id, rx_id] = (tx_gtx, rx_gtx)

        return resolved_qsfp_link_map


    def get_gpu_link_map(self):
        """ Return a dictionary that matches the GPU links to the GTX Rx and Tx instances when connected with a loopback cable.

        Returns:
           A dict, in the format:

                {('GPU', (tx_crate, tx_slot, tx_lane), (rx_crate, rx_slot, rx_lane)) : (tx_gtx_instance, rx_gtx_instance)}
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
        """ Return a dictionary that describes the corner-turn and loopbacked GPU links and the corresponding Tx and RX GTXes.


        Is used by `get_ber()` to identify the links on which Bit Error Rate (BER) tests will be performed.

        Parameters:

            links (str or list of str): select only the link type specified as a string or the link types specified in a list of strings.

        Returns:
           A dict, in the format:

                {(link_type, (tx_crate, tx_slot, tx_lane), (rx_crate, rx_slot, rx_lane)) : (tx_gtx_instance, rx_gtx_instance)}

                `link_type` is either "BP", "BP_QSFP" or "GPU"
        """
        link_map = {}
        link_map.update(self.get_backplane_pcb_link_map())
        link_map.update(self.get_backplane_qsfp_link_map())
        link_map.update(self.get_gpu_link_map()) # loopback links

        if isinstance(links, str):
            link_map = {link: gtxes for link, gtxes in link_map.items() if link[0] == links}
        elif links is not None:
            link_map = {link: gtxes for link, gtxes in link_map.items() if link in links}

        # Remove links that do not exist or have no GTX (direct lanes)
        if gtx_only:
            link_map = {link: gtxes for link, gtxes in link_map.items() if None not in gtxes}

        # link_list.sort(key=lambda (lt, (sc, ss, sl), (dc, ds, dl)): ss * 16 + ds)
        return link_map


    @async
    def get_ber(self, link_list=None, period=0.1, tx_power=None, print_=True):

        links = self.get_link_map(link_list, gtx_only=True)

        for link, (source_gtx, dest_gtx) in links.items():
            # First, make sure we can get errors by setting the wrong RX PRBS Sequence
            if source_gtx is None or dest_gtx is None:
                continue
            if link[0] == 'BP_QSFP':
                source_gtx.TXDIFFCTRL = 12
            if tx_power is not None:
                source_gtx.TXDIFFCTRL = tx_power

            source_gtx.TXPRBSSEL = 4
            dest_gtx.RXPRBSCNTRESET = 1
            dest_gtx.RXPRBSSEL = 3
            dest_gtx.RXPRBSCNTRESET = 0
            t0 = time.time()
            while True:
                if dest_gtx.ERR_CTR:
                    break
                if time.time() - t0 > 1:
                    raise SystemError('Cannot detect errors even with the wrong sequence! Are the links connected as expected?')
            dest_gtx.RXPRBSSEL = 4
            dest_gtx.RXDFELPMRESET = 1
            time.sleep(0.00005)
            dest_gtx.RXDFELPMRESET = 0

        # Perform BER test on a single list, to be run in parallel below
        @async
        def one_link_ber(link):
            (link_type, (sc, ss, sl), (dc, ds, dl)), (source_gtx, dest_gtx) = link

            if tx_power is not None:
                source_gtx.TXDIFFCTRL = tx_power

            source_gtx.TXPRBSSEL = 4
            if print_:
                print 'Measuring BER for link %s' % (link[0],),
                print source_gtx.TXDIFFCTRL
            # dest_gtx.RXPRBSCNTRESET=1
            # dest_gtx.RXPRBSCNTRESET=0
            # dest_gtx.RXPRBSCNTRESET=1
            # dest_gtx.RXPRBSCNTRESET=0
            # t0=time.time()
            # while time.time()-t0 < 13:
            #    print  dest_gtx.ERR_CTR, 'from', dest_gtx
            #    #dest_gtx.RXPRBSCNTRESET=1
            #    #dest_gtx.RXPRBSCNTRESET=0
            #    time.sleep(0.5)
            #    #if not dest_gtx.ERR_CTR:
            #    #    print 'locked',
            #    #    break
            #dest_gtx.RXPRBSCNTRESET=1
            dest_gtx.RXDFELPMRESET = 1
            time.sleep(0.00005)
            dest_gtx.RXDFELPMRESET = 0
            time.sleep(0.00005)
            dest_gtx.RXPRBSCNTRESET = 1
            dest_gtx.RXDFELPMRESET = 1
            time.sleep(0.00005)
            dest_gtx.RXDFELPMRESET = 0
            time.sleep(0.00005)
            dest_gtx.RXPRBSCNTRESET = 0
            yield gen.sleep(period)
            cnt = dest_gtx.ERR_CTR
            err = (float(cnt) * 16) / (period * 10e9)
            err_max = (float(cnt) * 16 + 1) / (period * 10e9)

            print '%r BER = %1.1e (%i errors, BER<%1.1e)' % (link[0], err, cnt, err_max)
            self.print_flush()
            async_return(err)

        # Run BER test on each link in parallel
        ber_table = yield {link: one_link_ber.async((link, gtxes)) for link, gtxes in links.items()}
        async_return(ber_table)

    def get_ber_vs_power(self, links, max_power, period=0.1):

        # links = self.get_link_map(links, gtx_only=True)
        # links = self.scan_links(array, tx_power = max_power)

        power = range(0, max_power+1)
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
            print '%20s %s' % (link, ','.join(['%6.1g' % b for b in ber]))
        plt.figure(1)
        plt.clf()

        for link, (tx_power, ber) in sorted(data.items(), key=str):
            plt.semilogy(tx_power, (np.array(ber)+1e-12), label='Link %s' % (link,))
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
            print  "###### running from slot %i lane %i to slot %i lane %i #######" % (from_slot, from_lane, to_slot, to_lane)
            gtx = self.ib.get(slot=to_slot).BP_SHUFFLE.gtx[to_lane-1]
            e = gtx.get_eye_diagram(range(-32, 32, h_step), range(-127, 128, v_step))
            eye_matrix[link] = e
        return eye_matrix

    @staticmethod
    def plot_eye_matrix(eye_matrix):
        plt.figure(1)
        plt.clf()

        source_slots = [ss for ((ss, sl), (ds, dl)) in eye_matrix.keys()]
        dest_slots = [ds for ((ss, sl), (ds, dl)) in eye_matrix.keys()]
        slots = sorted(set(source_slots + dest_slots))

        # slot_map = {slot: ix for (ix, slot) in enumerate(slots)}
        slot_map = {x+1: x for x in range(16)}

        (fig, ax) = plt.subplots(len(slot_map), len(slot_map), sharex=True, sharey=True, subplot_kw={'axis_bgcolor': 'black'})
        fig.subplots_adjust(wspace=0, hspace=0)
        fig.suptitle('Backplane 10Gbps mesh eye diagrams\n TX slot #: left, Rx slot #: bottom')

        for (slot, ix) in slot_map.items():
            # Bottom images
            a = ax[ix, 0]
            a.tick_params(labelsize=6)
            a.set_ylabel("TX S%02i" % (slot), fontsize=10)
            # Left images
            a = ax[len(slot_map)-1, ix]
            a.tick_params(labelsize=6)
            a.set_xlabel("RX S%02i" % (slot))
            plt.setp(a.xaxis.get_majorticklabels(), rotation=70)
            # Diagonal images
            a = ax[ix, ix]
            a.patch.set_color('black')

        for (((ss, sl), (ds, dl)), eye) in eye_matrix.items():
            a = ax[slot_map[ss], slot_map[ds]]
            a.imshow(np.log10(eye.ber_map+1e-12), origin='lower', extent=(-32, 32, -128, 128), aspect='auto', vmin=-12, vmax=1)
        plt.draw()

    def print_crossbar2_frame_info(self, reset_stats=False, grid=True, width=180):
        slots = Ccoll(self.ib, self.ib.slot) # Get iceboards indexed by slot number

        crate = set(slots.crate)
        if len(crate) == 1:
            crate = crate.pop()
        else:
            raise RuntimeError('Sorry, this method currently can work on one and only one crate. The currently active boards either have no crates or span multiple crates %s' % list(set(slots.crate)))

        slot_range = range(1,17)
        lane_range = range(16)
        slot_labels = [slots[s].serial if s in slots.keys() else 'N/A' for s in slot_range]

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
                frame_number = [f- frame_number[remapped_direct_lane] for f in frame_number]
                captured_source = {remapped_dest_lane: (((s >> 4) & 0xF) + 1 , (s & 0xF)) for remapped_dest_lane, s in enumerate(stream_ids)}
                computed_source = {shuffle_to_bin_sel_lane_map[dest_lane]: crate.get_matching_tx((dest_slot, dest_lane)) for dest_lane in lane_range}
                valid_source = {shuffle_to_bin_sel_lane_map[dest_lane]: v for dest_lane, v in enumerate(cb.get_lane_monitor('INPUT_DETECT'))}
                missing_frame = {shuffle_to_bin_sel_lane_map[dest_lane]: v for dest_lane, v in enumerate(cb.get_lane_monitor('MISSING_FRAME'))}
                align_fifo_overflow = {shuffle_to_bin_sel_lane_map[dest_lane]: v for dest_lane, v in enumerate(cb.get_lane_monitor('ALIGN_FIFO_OVERFLOW'))}
                rx_errors = {shuffle_to_bin_sel_lane_map[dest_lane]: v for dest_lane, v in enumerate(bp.get_rx_lane_monitor('ERROR_CTR'))}
                rx_fifo_overflow = {shuffle_to_bin_sel_lane_map[dest_lane]: v for dest_lane, v in enumerate(bp.get_rx_lane_monitor('FIFO_OVERFLOW'))}
                rx_max_frame_length = {shuffle_to_bin_sel_lane_map[dest_lane]: v for dest_lane, v in enumerate(bp.get_rx_lane_monitor('MAX_FRAME_LENGTH'))}

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
                cell = ' ' if (cap_src and cap_src==comp_src and valid_src) or not data_expected else '!'
                cell += '------' if not cap_src or not data_expected else '--??--' if not valid_src else 'S%02iL%02i' % cap_src
                cell +='/S%02iL%02i' %  comp_src # if not cap_src or data_not_expected or not valid_src else ''
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


        print 'Post-remap, crossbar2 bin selector input lane indentification'

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
            if ib.BP_SHUFFLE: # makesure we have a shuffle block in this firmware
                ib.BP_SHUFFLE.reset_stats()

    def get_shuffle_status(self):

        status = {}
        for crate in self.ic:
            status[crate] = {}

            for (slot, ib) in crate.slot.items():
                status[crate][ib] = {}

                status[crate][ib]['bp'] = ib.BP_SHUFFLE.get_bp_rx_status(0)
                status[crate][ib]['qsfp'] = ib.BP_SHUFFLE.get_bp_rx_status(1)

                status[crate][ib]['cb2'] = {}
                status[crate][ib]['cb2']['align'] = ib.CROSSBAR2.get_align_status()
                status[crate][ib]['cb2']['frame'] = ib.CROSSBAR2.get_frame_alignment_status()
                status[crate][ib]['cb2']['bin'] = ib.CROSSBAR2.get_bin_sel_status()

                status[crate][ib]['cb3'] = {}
                status[crate][ib]['cb2']['align'] = ib.CROSSBAR3.get_align_status()
                status[crate][ib]['cb2']['frame'] = ib.CROSSBAR3.get_frame_alignment_status()
                status[crate][ib]['cb2']['bin'] = ib.CROSSBAR3.get_bin_sel_status()

        return status

    def print_shuffle_status(self, reset_stats=False, verbose=1, grid=False):

        for crate in self.ic:
            slots = crate.slot # Get iceboards indexed by slot number

            slot_range = range(1, crate.NUMBER_OF_SLOTS + 1) or [None]

            info = {}
            for (slot, ib) in crate.slot.items():
                col_data = []
                errs = []
                # Gather status from the backplane PCB and QSFP links
                for lane_group in ib.BP_SHUFFLE.lane_group_names:
                    if reset_stats:
                        ib.BP_SHUFFLE.reset_stats()
                    errs.append(ib.BP_SHUFFLE.get_bp_rx_status(lane_group))

                # Gather status from the crossbars
                for cb in [ib.CROSSBAR2, ib.CROSSBAR3]:
                    if reset_stats:
                        cb.reset_stats()
                    errs.append(cb.get_align_status())
                    errs.append(cb.get_frame_alignment_status())
                    errs.append(cb.get_bin_sel_status())

                for err in errs:
                    if err is None:
                        col_data.append('?')
                    elif not verbose:
                        col_data.append(('-','ERR')[bool(any(err))])
                    elif verbose == 1:
                        col_data.extend(('-', 'ERR')[bool(e)] for e in err)
                    else:
                        col_data.extend(
                            ('\n'.join(['%s=%s' % (k, v) for (k, v) in e.items()]) or '-')
                            for e in err)
                info[slot] = col_data
            print 'Crate %s Crossbar and Shuffle status' % crate.get_string_id()

            # Fill in columns for any missing board in the crate
            number_of_rows = len(info.itervalues().next())
            for slot in slot_range:
                if slot not in info.keys():
                    info[slot] = [''] * number_of_rows

            # Print the table
            corner_label = 'Slot->\nS/N ->\n\\|/Lane'
            slot_labels = ['SN%s'% slots[s].serial if s in slots.keys() else 'N/A' for s in slot_range]
            col_labels = ['%s\n%s' % (slot_range[i], slot_labels[i]) for i in range(len(slot_range))]
            # row_labels = ['BP PCB Rx\nBP QSFP Rx\nCB2 FIFO\nCB2 ALIGN\nCB2 FRAMEnCB3 FIFO\nCB3 ALIGN\nCB3 FRAME\n']
            row_labels = []
            for label, lanes in [('BP PCB Rx', 16), ('BP QSFP Rx', 8), ('CB2 ALIGN', 16), ('CB2 FRAME #', 16), ('CB2 BIN_SELs', 2), ('CB3 ALIGN', 8), ('CB3 FRAME #',8), ('CB3 BIN SELs', 8)]:
                if verbose and lanes:
                    row_labels += ['%s L%02i' % (label, lane) for lane in range(lanes)]
                else:
                    row_labels += [label]
            # return info
            # print 'row_labels=', row_labels
            # print 'col_labels=', col_labels
            # print 'data=', info
            self.print_table(info, row_labels=row_labels, col_labels=col_labels, corner_label=corner_label, line_sep=grid)

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
                col_keys = col_labels.keys()
            elif isinstance(data, dict): #columns are dicts
                col_keys = data.keys()
            else:
                col_keys = range(len(data))

        data = [data[key] for key in col_keys]

        if row_keys is None:
            if isinstance(row_labels, dict):
                row_keys = row_labels.keys()
            else:
                for col_data in data:
                    keys = col_data.keys() if isinstance(col_data, dict) else range(len(col_data))
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

        col_keys = range(len(data))
        row_keys = range(len(data[0]))
        col_width = [max([len(line) for cell_data in [col_labels[col]] + data[col] for line in str(cell_data).splitlines() ]) for col in col_keys]
        row_labels_width = max([len(line) for row_label in [corner_label] + row_labels for line in row_label.splitlines() ])
        col_labels_height = max([len(label.splitlines()) for label in [corner_label] + col_labels])


        remaining_col_keys = list(col_keys)
        while remaining_col_keys:
            block_col_keys = []
            block_col_width = []
            while remaining_col_keys:
                col_key = remaining_col_keys[0]
                width = col_width[col_key]
                block_width = row_labels_width +3 + sum(block_col_width + [width]) + len(block_col_width)*3 + 3 + 1
                # print block_col_keys, col_key, block_col_width, width, block_width, max_width
                if block_width <= max_width:
                    block_col_keys.append(remaining_col_keys.pop(0))
                    block_col_width.append(width)
                else:
                    break

            line_format = '| %%-%is' % row_labels_width +' | ' + ' | '.join('%%-%is' % width for width in block_col_width) + ' |'
            line_sep_str = '+' + '+'.join(['-' * (width+2) for width in [row_labels_width] + block_col_width])+'+'

            print line_sep_str

            for i in range(col_labels_height):
                line_data = [cell.splitlines()[i] if i < len(cell.splitlines()) else '' for cell in [corner_label] + [col_labels[col_key] for col_key in block_col_keys]]
                print line_format % tuple(line_data)

            print line_sep_str

            for row_key in row_keys:
                row_data = [str(cell) for cell in [row_labels[row_key]] + [data[col_key][row_key] for col_key in block_col_keys]]
                row_height = max([len(cell.splitlines()) for cell in row_data])
                for i in range(row_height):
                    line_data = [cell.splitlines()[i] if i < len(cell.splitlines()) else '' for cell in row_data]
                    print line_format % tuple(line_data)
                if line_sep:
                    print line_sep_str

            if not line_sep: # Make sure we have a bottom line if we didn't already printed one
                    print line_sep_str

    def print_iceboard_table(self, func=None, row_labels=None, grid=False, add_serial=True):
        """
        func=function
        func=async function : wll be called concurrently
        func=data, dict, key is iceboard object
        """

        if not len(self.ib):
            print '[ There are no IceBoards in the hardware map ]'
            return

        # Process func and end up with a dict of {iceboard:cell_text}
        if func:
            if isinstance(func, dict):
                data = func
            elif hasattr(func, 'async_map'):
                data = OrderedDict(zip(self.ib, func.async_map(self.ib)))
            else:
                data = OrderedDict((ib, func(ib)) for ib in self.ib)

        iceboards = data.keys()

        if row_labels is None:
            row_labels = ''
        if isinstance(row_labels, str):
            row_labels = [row_labels]

        # Print a table of crate-less (stand-alone) iceboard
        orphan_iceboards = [ib for ib in iceboards if not ib.crate or not ib.crate.serial]
        corner_label = 'Standalone\nICEBoards'
        # col_labels = ['-'] * len(orphan_iceboards)
        col_labels = ['\nSN%s' % ib.serial for ib in orphan_iceboards]
        for i, ib in enumerate(orphan_iceboards):
           col_labels[i] += '\n%s' % ib.hostname
        table = []
        for ib in orphan_iceboards:
            # cell = 'SN' + ib.serial + '\n' if add_serial else ''
            cell = data[ib] if data else ''
            table.append([cell])  # append single-row column
        if table:
            self.print_table(table, row_labels=row_labels, col_labels=col_labels, corner_label=corner_label, line_sep=grid)


        valid_crates = OrderedDict((ib.crate, None) for ib in iceboards if ib.crate and ib.crate.serial).keys()  # trick to impelment an OrderedSet

        for crate in valid_crates:
            corner_label = '%s\nCrate #%s' % (crate.get_string_id(), crate.crate_number)
            slot_range = range(1, crate.NUMBER_OF_SLOTS + 1)
            col_labels = ['%i' % (s) for s in slot_range]
            if add_serial:
                for i, slot in enumerate(slot_range):
                   col_labels[i] += ('\nSN' + crate.slot[slot].serial) if slot in crate.slot else '\n-'
            if add_serial:
                for i, slot in enumerate(slot_range):
                   col_labels[i] += ('\n%s' % crate.slot[slot].hostname) if slot in crate.slot else '\n-'

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
                self.print_table(table, row_labels=row_labels, col_labels=col_labels, corner_label=corner_label, line_sep=grid)

    def print_iceboard_temperatures(self):
        sensor = self.ib[0].TEMPERATURE_SENSOR.MB_FPGA_DIE
        self.print_iceboard_table(lambda ib: '%3.1f' % ib.get_motherboard_temperature(sensor), row_labels='FPGA Die Temp')

    def print_iceboard_power(self):
        self.print_iceboard_table(lambda ib: '%0.1f' % ib.get_total_power())

    def reset_fpga_stats(self):
        """ Resets FFT overflow count for all boards in FPGA array.
        """
        for ib in self.ib:
            ib.reset_fft_overflow_count()

    def get_monitoring_info(self):
        return self.ib.index_by(lambda ib:ib.get_id()).get_status()

    @async
    def get_arm_metrics(self, metrics):
        """ Get the monitoring information on the backplanes & boards that are accessible from the ARM.

        Includes:
            - Backplane metrics, as measured from one board in each crate
            - Iceboard hardware metrics (voltages, temperatures), which also includes mezzanines voltage/current.

        Returns:
            A :cls:`Metrics` object.
        """

        # IceCrate metrics
        self.logger.info('%r: Getting IceBoard backplane hardware metrics (over ARM link)' % self)
        for ic in self.ic:
            slot, ib = ic.slot.items()[0]
            metrics += yield ib.get_backplane_metrics.async()

        # IceBoard metrics
        self.logger.info('%r: Getting IceBoard temperature & power supply metrics (over ARM link)' % self)
        m = yield [ib.get_metrics.async() for ib in self.ib]
        metrics += m
        self.logger.info('%r: Got %i IceBoard temperature & power supply metrics' % (self, len(m)))
        metrics += yield [ib.get_fpga_udp_metrics.async() for ib in self.ib]

    @async
    def get_fpga_metrics(self, metrics, reset=True):
        """ Get the monitoring information on the FPGA firmware status across the array.

        Includes:
            - Backplane receiver/transmitter status with packet statistics for both the PCB and QSFP links.

        Returns:
            A :cls:`Metrics` object.
        """

        # Shuffle status
        self.logger.info('%r: Getting corner-turn links metrics (over FPGA UDP link)' % self)
        metrics += yield [ib.get_bp_shuffle_metrics.async(reset=reset) for ib in self.ib]
        self.logger.info('%r: Getting corner-turn crossbars metrics (over FPGA UDP link)' % self)
        metrics += yield [ib.get_crossbar_metrics.async(reset=False) for ib in self.ib]
        self.logger.info('%r: Getting channelizer metrics (over FPGA UDP link)' % self)
        metrics += yield [ib.get_channelizer_metrics.async(reset=reset) for ib in self.ib]
        self.logger.info('%r: Finished gathering FPGA/backplane metrics' % self)

        # Backplane GTX
        # Errors, signal level

        # Command errors

        if self.sync_timestamps:
            for i, ib in enumerate(self.ib):
                try:
                    fn, ts = yield ib.capture_frame_time.async(format='raw')
                    if not i:
                        fn0, ts0 = (fn, ts)
                    crate, slot = ib.get_id()
                    slot = ib.slot - 1
                    metrics.add('fpga_time_delta', ts.nano - ts0.nano, crate=crate, slot=slot)
                    metrics.add('fpga_frame_number_delta', fn - fn0, crate=crate, slot=slot)
                    metrics.add('fpga_time_error', ts.nano - (self.sync_timestamps[i].nano + fn*2560), crate=crate, slot=slot)
                    metrics.add('fpga_sync_time_delta', self.sync_timestamps[i].nano - self.sync_timestamps[0].nano, crate=crate, slot=slot)
                except RuntimeError:
                    self.logger.error('%r: Timeout while capturing frame time' % ib)

        async_return(metrics)



    def print_iceboard_info(self):
        """ Gather metrics from the iceboards and print them in a nicely formatted table """
        if not self.ib:
            print('There are no IceBoards in the array')
            return
        info_metrics = self.ib.get_status()
        keys = '\n'.join(info_metrics[0][0].keys())
        data = {ib:('\n'.join(info_metrics[i][0].values())) for i, ib in enumerate(self.ib)}
        self.print_iceboard_table(data, row_labels=keys)


    def print_rx_err_map(self, icecrates=None, reset_stats=0, delay=-5, tx_power=None,
                         tx_precursor=None, tx_postcursor=None, lpm = None, dfe_reset=False, stop_on_errors=2, verbose=1):
        """
        Initialize and test the corner turn engine operation.

        Parameters:
            icecrates (IceCrate Ccoll): List of icecrates on which the test is done. If `None`, all icecrates are initialized/tested.

            reset_stats (bool): Reset the statistics counters. Default is false.

            delay (int):

            tx_power (int). transmit power to be set on the GTX. Ranges from 0-15. If ``None``, TX power levels ar enot changed.

            tx_precursor=None,

            tx_postcursor=None

            lpm (bool): If true, enables Low Power Mode in the GTX. Requires a dfe_reset.

            dfe_reset (bool): If true, resets the Dynamix Feedback Equalizer to allow it to find a new equalization solution. Is needed when power levels or operaiton mode are changed

            stop_on_errors=2,

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
                print 'At', time.asctime(), '(%s seconds since the method call)' % (datetime.timedelta(seconds=dt))
                for i, ic in enumerate(icecrates):
                    err_map = [[None] * ic.NUMBER_OF_SLOTS for _ in range(ic.NUMBER_OF_SLOTS)]
                    #print  ic.slot.values()
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
                            err_map[rx_lane_id[0]-1][tx_lane[0]-1] = errs[lane] if errs[lane] else '!DET' if not det[lane] else '-'
                            #print '%s -> %s = %i' % (tx_lane, rx_lane_id, errs[lane])
                    row_labels = ['Tx S%02i SN%s' % (ib.slot, ib.serial) for ib in ic.slot.values()]
                    col_labels = ['Rx S%02i\nSN%s' % (ib.slot, ib.serial) for ib in ic.slot.values()]
                    corner_label = '%s\nCrate #%s' % (ic.get_string_id(), ic.crate_number)
                    print '    %s: %-10s %s %s' % (
                        ic.get_string_id(),
                        'No Frames!' if not worst_det else ('%i errors' % worst_err),
                        '%s without errors' % datetime.timedelta(seconds=int(time_without_error[i])),
                        'so far' if not has_errors[i] else '')
                    if verbose:
                        self.print_table(err_map, row_labels=row_labels, col_labels=col_labels, corner_label=corner_label)
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
                    err_map[rx_lane[0]-1][tx_lane[0]-1] = '%0.1f' % (ic.get_rx_net_length(rx_lane)/1000)
            row_labels = ['Tx S%02i SN%s' % (ib.slot,ib.serial) for ib in ic.slot.values()]
            col_labels = ['Rx S%02i\nSN%s' % (ib.slot,ib.serial) for ib in ic.slot.values()]
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
            print 'Lane %02i: Stream ID=0x%02x, Frame = 0x%02x (delta = %i)' % (i, sid[i], ts[i], ts[i]-ts[0])

    def plot_crate_temperatures(self, figure_number=1):

        sensor = self.ib[0].TEMPERATURE_SENSOR.MB_FPGA_DIE

        plt.figure(figure_number)
        plt.clf()
        plt.hold(1)
        for ic in self.ic:
            ib = Ccoll(ic.slot.values())
            t = ib.get_motherboard_temperature(sensor)
            s = ib.slot
            avg_temp = np.average(t)
            h = plt.plot(s, t, label=ic.get_string_id())
            plt.plot([min(s), max(s)], [avg_temp]*2, ':', color=h[0].get_color(), lw=2)
            print '%s: %fdegC' % (ic.get_string_id(), avg_temp)
        plt.legend(loc='best')
        plt.xlabel('Slot number')
        plt.ylabel('FPGA Die temperature [degC]')
        plt.grid(1)
        plt.title('FPGA die temperatrures for multiple crates')


    def _update_arm_firmware(self, image_filename, power_cycle=True):
        """
        Update the ARM SD card firmware and power cycle all the power supplies. The image must be compressed with bzip2.
        """
        self.ib._update_arm_firmware(image_filename, delay=120)
        if self.ps and power_cycle:
            ps.unlock()
            ps.power_cycle(delay=4)


    @async
    def set_adc_delays(self, **kwargs):
        """
        Set ADC delays for all Mezzanines on all IceBoards of the array. Calls set_adc_delays() on each IceBoard instance with the specified paramaters.
        """

        for ib in self.ib:
            if ib.is_open():
                self.logger.info("%r: Setting ADC delays" % (self))
                ib.set_adc_delays(**kwargs)
                yield async_moment
            else:
                self.logger.warning("%r: Communication with FPGA is not initialized. Cannot set ADC delays" % (self))


ICE_PATTERNS = [
        { 'regex': '(MGK7)?BP1',           'cur_state': None,  'next_state': 'ic',  'entry': ('MGK7BP1', None, None),  'store_in': None        },  # Sets the curent model and type to the One-slot backplane; matches MGK7BP1, BP1
        { 'regex': '(MGK7)?BP16',          'cur_state': None,  'next_state': 'ic',  'entry': ('MGK7BP16', None, None), 'store_in': None        },  # Sets the curent model and type to the 16-slot backplane; matches MGK7BP16, BP16
        { 'regex': '(?:|SN)?(\d+)',        'cur_state': 'ic',  'next_state': 'ic',  'entry': (None, 0, None),          'store_in': 'icecrates' },  # Stores an item with the current model and specified serial number and optional item number (crate number).  Matches 232, 0232, SN232, SN0232, 232:1. Serial number can be prefixed by SN.
        { 'regex': '(?:|SN)?(\d+):(\d*)',  'cur_state': 'ic',  'next_state': 'ic',  'entry': (None, 0, 1),             'store_in': 'icecrates' },  # Stores an item with the current model and specified serial number and optional item number (crate number).  Matches 232, 0232, SN232, SN0232, 232:1. Serial number can be prefixed by SN.
        { 'regex': '\*',                   'cur_state': 'ic',  'next_state': None,  'entry': (None, '*', None),        'store_in': 'icecrates' },  # Stores a an item that selects all units of the current model

        { 'regex': '(\d+\.\d+\.\d+\.\d+)', 'cur_state': None,  'next_state': None,  'entry': (None, 0),                'store_in': 'iceboards' },  # Stores a motherboard item based on its IP address only
        { 'regex': '(\w+\.local)',         'cur_state': None,  'next_state': None,  'entry': (None, 0),                'store_in': 'iceboards' },  # Stores a motherboard item based on its local hostname only
        { 'regex': '(MGK7)?MB',            'cur_state': None,  'next_state': 'ib',  'entry': ('MGK7MB', None),         'store_in': None        },  # Sets the curent model and type to the ICEBoard (motherboard); matches MGK7MB, MB
        { 'regex': '(?:|SN)?(\d+)',        'cur_state': 'ib',  'next_state': 'ib',  'entry': (None, 0),                'store_in': 'iceboards' },  # Stores an item with the current model and specified serial number and optional item number (crate number).  Matches 232, 0232, SN232, SN0232, 232:1. Serial number can be prefixed by SN.
        { 'regex': '\*',                   'cur_state': 'ib',  'next_state': None,  'entry': (None, '*'),              'store_in': 'iceboards' },  # Stores a an item that selects all units of the current model

        { 'regex': '(MG)?ADC08',           'cur_state': None,  'next_state': 'adc', 'entry': ('MGADC08', None),        'store_in': None        },  # Sets the curent model and type to the CHIME Mezzanine;  matches MGADC08, ADC08
        { 'regex': '29821-0000-(\d{4})',   'cur_state': None,  'next_state': None,  'entry': ('MGADC08', 0),           'store_in': 'mezzanines'},  # Stores a MGADC08 mezzanine item based on serial number extracted from the Digico barcodes (29821-000-ssss, where ssss=serial number)
        { 'regex': '35896-0000-(\d{4})',   'cur_state': None,  'next_state': None,  'entry': ('MGADC08', 0),           'store_in': 'mezzanines'},  # Stores a MGADC08 mezzanine item based on serial number extracted from the Digico barcodes
        { 'regex': '(?:|SN)?(\d+)',        'cur_state': 'adc', 'next_state': 'adc', 'entry': (None, 0),                'store_in': 'mezzanines'},  # Stores an item with the current model and specified serial number and optional item number (crate number).  Matches 232, 0232, SN232, SN0232, 232:1. Serial number can be prefixed by SN.

        { 'regex': 'crate',                'cur_state': None,  'next_state': 'cr', 'entry': (None,),                    'store_in': None        },  #
        { 'regex': '(\d+)',                'cur_state': 'cr',  'next_state': 'cr', 'entry': (0,),                        'store_in': 'crates' },
        ]

def parse_hw_string(hw_string, remap_table={}, dut_id_patterns=ICE_PATTERNS):
    """ Parses a string describing ICE hardware elements (motherboards, crates and mezzanines)

    Arguments:
        hw_string (str): string describing the model, serial number/hostname and optionally the sequence number of the desired
           hardware elements

        remap_table (dict): used to remap entries into other entries. Used to implement aliases, and
            map crate numbers into actual model/serial/crate number entries, in the format::

                { type : { entry:(target_type, target_entry), ...} ...}

        dut_id_patterns (list of dict): Describe patterns to match, the type of hardware they match, and the data they provide.

            regex: the regular expression to match, with optional capture groups.

            cur_state (str): The state to which the match apply. ``None`` means it applies to any state.

            next_state (str): The state to move into if there is a match

            entry (tuple): Entry to make or update, as a (model, serial) or (model, serial, number) tuple.  For each
                of the values in the tuple:

                - None: means the value is unchanged
                - *int*: get the regex capture group specified by the *int*
                - *str*: Set the field to *str*

            store_in: the type of hardware in which to store the entry if there is a match


    Returns:
        A NameSpace object describing a the specified hardware elements for each hardware type listed in `dut_id_patterns`::

        .icecrates =  [ (model, serial, crate_number) ...],
        .iceboards =  [ (model, serial) | (None, ip_address) | (None, hostname)]
        .mezzanines: [ (model, serial) ...]

        Serial numbers are converted to integers if possible; otherwise, they are stored as a string.

    Underscore characters are converted to blanks before parsing.

    Examples:
        'MGK7BP16_SN018:3' => ('MGK7BP16', 18, 3)
        'MGK7BP16_018:3' => ('MGK7BP16', 18, 3)
        '18:3' => (None, 18, 3)
        '18' => (None, 18, None)
    """
    logger = logging.getLogger(__name__)
    # If hw_string is a list of string, combine them in one single string
    if isinstance(hw_string, (list, tuple)):
        hw_string = ' '.join(str(s) for s in hw_string)


    # Create a new hw table with all possible hardware categories
    hw_table = NameSpace()
    for t in {d['store_in'] for d in dut_id_patterns if d['store_in'] is not None}:
        hw_table[t] = [] # crate type with empty list if the type does not exist


    # Split the string in ' '- or '_'-separated elements
    elements = str(hw_string).replace('_', ' ').strip().split(' ')

    # Special case: if the hw_string is just '*', we register all units of every known model with
    # '*' as the serial number (second) field
    if len(elements) == 1 and elements[0] == '*':
        for p in dut_id_patterns:
            type_, entry = p['store_in'], p['entry']
            wild_entry = entry[0:1] + ('*',) + entry[2:]
            if wild_entry not in hw_table[type_]:
                hw_table[type_].append(wild_entry)
        return hw_table

    def to_int(s):
        return (int(s) if isinstance(s, str) and s.isdigit() else s)

    pos = 0
    err = None
    current_entry = {} # stores the value for each state
    state = None
    for el in elements:
        pos += len(el) + 1
        if not el:  # if whitespace
            continue
        # print 'checking', el
        matches = 0
        for p in dut_id_patterns:
            regex, cur_state, next_state, entry, store_in = p['regex'], p['cur_state'], p['next_state'], p['entry'], p['store_in']
            if not (cur_state is None or state == cur_state):
                continue
            m = re.match('^' + regex + '$', el, re.I)
            if not m:   # if there is no match
                continue
            matches += 1
            # print 'match %i: ' % matches, regex, el
            groups = m.groups()  # capture groups, in a list

            # Update the current entry with the entries that are not None
            # Always convert to integer if possible
            new_entry = [(to_int(groups[e] if isinstance(e, int) else e))
                         if cur_state is None or e is not None or i>=len(current_entry[state])
                         else current_entry[state][i] for i,e in enumerate(entry)]

            # Store if instructed
            if store_in:
                hw_table[store_in].append(tuple(new_entry))
            elif next_state:
                current_entry[next_state] = new_entry
            state = next_state

        if err:
            break
        if not matches:
            err = "Could not find a match for element '%s' of the hardware description string" % el
            break
        if matches > 1:
            err = 'element found multiple matches'
            break
    if err:
        print 'Error:', err
        print hw_string
        print ' '*(pos-2)+'^'
        raise ValueError(err)
    # Remap
    #print 'remapping with ', remap_table
    for type, entries in list(hw_table.items()):
        #print type, entries
        if type in remap_table:
            for entry in list(entries):
                #print entry
                if entry in remap_table[type]:
                    target_type, target_entry = remap_table[type][entry]
                    hw_table[target_type].append(target_entry)
                    hw_table[type].remove(entry)

    return hw_table




log_levels = {'info': logging.INFO, 'debug': logging.DEBUG, 'warn': logging.WARNING, 'error': logging.ERROR}

def setup_logging(log_target='syslog', log_level='debug', sql_log_level='warn', stderr_log_level='warn'):
    # Make sure SQLAlchemy does not log too much
    sql_logger = logging.getLogger('sqlalchemy.engine.base.Engine')
    sql_logger.setLevel(log_levels[sql_log_level])

    # Set-up main loggers
    if log_target == 'stream':
        log_handler = logging.StreamHandler()
    elif log_target == 'syslog':
        log_handler = logging.handlers.SysLogHandler()
    else:
        log_handler = logging.FileHandler(log_target)
    formatter = logging.Formatter('%(asctime)s %(levelname)s %(name)s:  %(message)s')

    logger = logging.getLogger('')
    logger.handlers = []  # Clear all existing handlers
    logger.setLevel(logging.DEBUG)  # pass all messages to the handlers which will filter what they want

    log_handler.setLevel(log_levels[log_level])
    log_handler.setFormatter(formatter)
    logger.addHandler(log_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    stream_handler.setLevel(log_levels[stderr_log_level])
    logger.addHandler(stream_handler)
    return logger


def add_logging_arguments(parser):
    parser.add_argument('-t', '--log_target', action='store', type=str, default='syslog', help="Logging target ('stream', 'syslog' or a filename)")
    parser.add_argument('-l', '--log_level', action='store', type=str, choices=log_levels, default='debug', help='Logging level')
    parser.add_argument('--sql_log_level', action='store', type=str, choices=log_levels, default='warn', help='SQLAlchemy Logging level')
    parser.add_argument('--stderr_log_level', action='store', type=str, choices=log_levels, default='warn', help='stderr (console) Logging level')

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
    parser.add_argument('--prog',            type=int, nargs='?', const=1, help='Programs the FPGA if not already programmed. --prog or --prog 1 programs the FPGA if the firmware is not already programmed.  --prog 2 forces the FPGA programming even if the firmware is already programmed')
    parser.add_argument('-b', '--bitfile',   type=str, help='Filename of the bitfile used to to program the FPGAs')
    parser.add_argument('-o', '--open',      type=int, nargs='?', const=1, help='Opens communication with the FPGAs, create the Python objects representing the firmware, and initialize the firmware. --open 0 skips the firmware initialization phase')
    parser.add_argument('--sync_method',     type=str, help="Sets the global syncing method ('distributed_time', 'centralized_time_trigger', 'centralized_soft_trigger', 'local_soft_trigger')")
    parser.add_argument('--sync_source',     type=str, help="Sets the global syncing source ('bp_gpio_int', 'bp_time', 'bp_trig')")
    parser.add_argument('--sync_master',     type=str, help="Serial number of the IceBoard that generates the time or trig signal")
    parser.add_argument('--sync_master_time_source', type=str, help="Source of the time signal used by the master board to generate the time or trigger signal ('bp_gpio_int', 'bp_time', 'bp_trig')")
    parser.add_argument('-m', '--mode',      type=str, help="Operational mode ('shuffle16', 'shuffle256', 'shuffle512'). If not specified, set_operational_mode() is not called.")
    parser.add_argument('-f', '--frames_per_packet', '--fpp',     type=int, help="Number of frames per packeet. Default=2.")
    parser.add_argument('-u', '--udp_retries', type=int, help="Number of times UDP packet transmission to the FPGA will be retried.")
    parser.add_argument('--fpga_ip_addr_fn', type=str, help="Method used to set the FPGA IP address relative to the ARM address")
    parser.add_argument('hwm',               type=str, nargs='*', default=argparse.SUPPRESS, help="target hardware")  # allows free-style hardware description string

    defaults = dict(
        sync_method='distributed_time',
        sync_source='bp_trig',
        sync_master=None,
        sync_master_time_source=None,
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



# def GPUArray(gpu_nodes=[]):
#         # Create GPU node array
#         if gpu_nodes:
#             #print gpu_nodes
#             return Ccoll(GpuNodeHandler(hostname=hostname) for hostname in gpu_nodes)
#         else:
#             return Ccoll([])

# Create Power Supply array
def PSArray(power_supplies=[]):
        if not power_supplies:
            return Ccoll([])
        if isinstance(power_supplies, list):
            ps = Ccoll(AgilentN5764AHandler(hostname=hostname) for hostname in power_supplies)
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
        create_fpga_array --iceboards 10.10.10.5 10.10.10.6   # Creates  an array of 2 boards at specified IP addresses
        create_fpga_array --iceboards iceboard0005.local iceboard0006.local   # Creates  an array of 2 boards at specified hostname (assuming the host computer runs a mDNS client)
        create_fpga_array --iceboards 0005 0006   # Creates  an array of 2 boards with specified serial numbers (resolved using a mDNS request on the network)
        create_fpga_array --iceboards 5 6   # Same as above. Works only with purely numeric serial numbers.
        create_fpga_array --icecrates MGK7BP16_003 MGK7BP16_007  # Load all boards in crate serial number 003 and 007
        create_fpga_array --icecrates 3 7  # Same as above. MGK7BP16-type backplane is assumed by default

    In the configuration file, some parameters are grouped in the following sub-dictionaries:

    root object:
        logging:     # Contains all the parameters related to logging
        fpga_array:  # Contains all the parameters related to the creation and
                     # initialization of the FPGA motherboards, crates and mezzanines
        gpu_array:   # Contains all the parameters related to the creation and
                     # initialization of the GPU nodes
        power_supply_array:  # Contains all the parameters related to the creation
                             # and initialization of the power supplies

    Example:
        my_config:
            fpga_array:
                iceboards: ["10.10.10.5", "10.10.10.6"]  # or any other syntax accepted by the comamnd line
                icecrates: [3, 7]
                ...
            logging:
                log_target: "syslog"
            ...

    Generic parameters (root dict)
    ------------------

    yaml: Name of a YAML configuration file to load. One or more root object
       can be specified, in which case the filename and the list of root
       objects must be separated by a single ':'. If multiple root objects are
       specified, they are all combined and lists or dictionaries with similar
       names are all combined.

       Objects can be specified hierarchically using the '.' hierarchy
       separator. An object starting with '.' starts at the same node level as
       the previous object.

    Example:
        --yaml file1.yaml # Load config from the top node of the file
        --yaml file1.yaml:site1 # Load  config from the site1 element
        --yaml file1.yaml:site1 site2# Load config by combining the elements of site1 and site2 objects
        --yaml file1.yaml:site1.boards .gpus .ps  # combine site1.boards, site1.gpus and site1.ps


    FPGA array parameters (``fpga_array`` sub-dict)
    ---------------------
    Here is a summary of the FPGA array creation parameters. Detailed
    description of each parameter is profided in the ``FPGAArray`` object.

        hwm: Contains a hardware map object (config file only, created with HardwareMap! object)
        iceboards: List of IceBoards (IP, hostnames or serial numbers) to add to the hardware map. Their connected IceCrate and Mezzanine is also automatically added.
        icecrates: List of IceCrates (serial numbers) to add to the hardware map. Adds all IceBoards in them.
        exclude_iceboards: Remove the specified IceBoards (serial numbers) from the hardware map.
        mdns_timeout: Time to wait for IceBoard to responds to mDNS queries
        no_mezz: Do not discover nor initialize the mezzanine on the IceBoard
        subarrays: Keep only iceboards that are in the specified subarrays (applicable only to objects created explicitely in the configuration file)
        ping: Keep only boards that respond to requests

        bitfile: pathname of the file containing the CHIME FPGA bitstream
        prog: Configures all the FPGAs in the array. If ``prog 1`` is given, forces programming even if the firmware is already loaded.
        open: Establish communication with the FPGA and Initializes the FPGA firmware and the corresponding Python modules.
        if_ip: address of the interface used to communicate with the FPGA. If not specified, the same interface as the one used for communicate with the ARM processor is used.

        sampling_frequency: Specifies the sampling frequency of the CHIME ADC mezzanine, in Hz (typically 800 MHz)
        reference_frequency: Specifies the frequency of the system's reference clock in Hz (typically 10 MHz)
        data_width: Bit width used after the channelizer's scaler (4 or 8)
        sync_method: string describing the method used to synchronize all the boards in the array
        sync_source: string describing the source of the synchronization signal.

    GPU Array parameters (``gpu_array`` sub_dict)
    --------------------
        gpu_nodes: list of GPU nodes (IP addresses or hostnames) for which GPU node objects are to be created.


    Power Supply Array parameters (``ps_array`` sub_dict)
    -----------------------------
        power_supplies: list of GPU nodes (IP addresses or hostnames) for which power supply objects are to be created.

    Logging parameters: (``logging`` sub-dict)
    -------------------
        log_target : String indicating the logging target (default = 'syslog'). May be
            - 'stream' : logs on stdout (not recommended in interactive sessions)
            - 'syslog': logs on Syslog on localhost
            - any other string: logs to a file specified by the string

        log_level : String indicating the logging level. May be 'info',
            'error', 'warn' , 'debug'. default is 'debug'.

        sql_log_level : String indicating SQLAlchemy logging level. Same
            values as ``log_level``. Defaults to 'warn'.

        stderr_log_level : String indicating what messages to log on stderr
           (usually the console) in addition to the main log target. Is usually
           used to make sure that important messages (warnings and errors) are
           seen immediately by the interactive operator. Values are the same as
           ``log_level``. Defaults to 'warn'.

    """
    # -------------------------------
    # Parse command line arguments
    # -------------------------------
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0]) # description is the first line of the docstring of this module

    # Add logging-related command-line parameters
    logging_group = parser.add_argument_group('logging parameters', 'Specify how and where the logging is done')
    logging_group.sub_dict = 'cli_logging'  # group all arguments in this group in a sub dictionary with this name
    add_logging_arguments(logging_group)


    # Add FPGA Array-related command-line parameters
    fpga_group = parser.add_argument_group('FPGA Array parameters', 'Allows interactive creation of a hardware map and initialization of all its components')
    fpga_group.sub_dict = 'cli_fpga_array'  # group all arguments in this group in a sub dictionary with this name
    fpga_defaults = add_fpga_array_arguments(fpga_group)

    # gpu_group = parser.add_argument_group('GPU Array parameters', 'Allows interactive creation of GPU nodes')
    # gpu_group.sub_dict = 'cli_gpu_array'  # group all arguments in this group in a sub dictionary with this name
    # gpu_group.add_argument('-n', '--gpu_nodes', type=str, nargs='+',  help='List of IP address or hostnames of the GPU node objects to be created.')

    ps_group = parser.add_argument_group('Power Supply Array parameters', 'Allows interactive creation of Power Supply objects')
    ps_group.sub_dict = 'cli_power_supply_array'  # group all arguments in this group in a sub dictionary with this name
    ps_group.add_argument('-p', '--power_supplies', type=str, nargs='+', help='List of IP address or hostnames of the power supply objects (Agilent_N5764A) to be created.')
    ps_group.add_argument('--power', type=str, help='Set the state of the power supplies: ON, OFF or CYCLE')

    # Add generic command-line parameters
    parser.add_argument('-y', '--yaml',  type=str, nargs='+',   help='YAML configuration file name, optionally followed by object names in that file.')

    args = parse_args_as_dict(parser)  # Parse command-line arguments as a dict, with arguments groups stored in separate sub dictionaries
    # args.test = parse_dut_id(args.target)
    #print('args=', args)
    # -------------------------------
    # Load configuration file
    # -------------------------------
    config = load_yaml_config(args.pop('yaml', None))  # Load YAML config
    #config = merge_dict(config, args)     # Add command line arguments to config
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
    config_fpga_array_params = config.get('fpga', {}).get('fpga_array_params', {}) or config.get('fpga_array_params', {})
    cli_fpga_array_params = {k: v for k, v in args['cli_fpga_array'].items() if v is not None}
    #Sprint('merging \n\n%r\n\n with \n\n%r' % (config_fpga_array_params, cli_fpga_array_params))
    fpga_array_params = merge_dict(config_fpga_array_params, cli_fpga_array_params)
    fpga_array = FPGAArray(**fpga_array_params)  # Create FPGA array

    # GPU array
    #gpu_array = GPUArray(**config.get('cli_gpu_array', {}))     # Create FPGA array


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
