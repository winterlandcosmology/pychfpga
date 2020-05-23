#!/usr/bin/env python


"""
Module that provide the classes used to run the top-level ChimeMaster object used to initialize and operate the CHIME telescope.

"""

from __future__ import absolute_import, division, print_function

# Python Standard Library packages
import collections
import numpy
import os
import re
import traceback
import sys
import time
import json
import functools
import pickle
import datetime

# PyPI packages
import psutil
import tornado
import tornado.tcpclient
import tornado.web
import tornado.locks
from tornado.escape import native_str
import numpy as np


# External private packages
from wtl import log
from wtl.rest import RESTClient, AsyncRESTServer, AsyncRESTClient, HTTPError # generic REST servers and clients
from wtl.rest import endpoint, coroutine, coroutine_return, sleep, moment
from wtl.rest import RunSyncWrapper, IOLoop, run_client
from wtl.namespace import NameSpace, merge_dict
from wtl.config import load_yaml_config
from wtl.metrics import Metrics
try:
    import comet
except ImportError:
    comet = None


# Local imports
from pychfpga import calculate_gains
from pychfpga import __version__, get_git_version
from pychfpga import FPGAArray
from ps import PowerSupplyAsyncRESTClient
from raw_acq import RawAcqAsyncRESTClient
from pychfpga.digital_gain import DigitalGainArchive


def convert_types(val):
    """
    Do the annoying conversion of numpy types to native Python types. Sigh.
    """

    def flatten(x):
        """ Flatten arbitrarily deep nested lists. (inspired from stack overflow)"""
        result = []
        for el in x:
            if hasattr(el, "__iter__") and not isinstance(el, basestring):
                result.extend(flatten(el))
            else:
                result.append(el)
        return result

    found_complex = False
    if isinstance(val, (list, tuple)):
        if len(val) == 0:
            val = [0]
        #if isinstance(val[0], (list, tuple)):
        val = flatten(val)
        if not isinstance(val[0], str):
            try:
                if val[0].dtype.kind in ('i', 'u', 'f'):
                    val = list(numpy.asscalar(x) for x in val)
            except:
                if type(val[0]) == bool:
                    val = list(int(x) for x in val)
                else:
                    val = list(x for x in val)
            for i, val_element in enumerate(val):
                #print val_element
                if isinstance(val_element, complex):
                        val[i] = [val_element.real, val_element.imag]
                        found_complex = True
                if isinstance(val_element, (int,numpy.uint8)):
                        val[i] = float(val_element)
            if found_complex:
                val = flatten(val)
            else:
                val = list(int(x) for x in val)
    else:
        if isinstance(val, long):
            val = int(val)
        elif isinstance(val, bool):
            val = int(val)
        elif isinstance(val, unicode):
            val = str(val)
        elif isinstance(val, int):
            pass
        elif isinstance(val, float):
            pass
        elif not isinstance(val, str):
            try:
                if val.dtype.kind in ('i', 'u', 'f', 'b'):
                    val = numpy.asscalar(val)
            except:
                    # Hopefully already a int/float
                    pass
    return val

def sanitize_for_json(obj):
    """
    Modify an object to make it JSON-compatible. Contents of dicts and
    lists contained in the object are recursively converted.

        - dict-like object with string keys are converted to Python dict
        - dict-like objects with non-string keys are converted into a Python list of (key,value) tuples.
        - list objects are converted into Python list
        - other objects stay the same.

    """
    if isinstance(obj, collections.Mapping) or hasattr(obj, 'items'):
        if not all(isinstance(k,str) for k in obj.keys()):
            return [(k, sanitize_for_json(v)) for k, v in obj.items()]
        else:
            return {str(k): sanitize_for_json(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [sanitize_for_json(v) for v in obj]
    else:
        return obj


def reap_cached_sockets():
    import __main__
    logger = log.get_logger(__name__, 'reap_cached_sockets()')
    if hasattr(__main__, '__opened_sockets__'):
        for port, socket in __main__.__opened_sockets__.items():
            logger.debug("closing cached socket on port %d" % port)
            socket.close()
        del __main__.__opened_sockets__


class ChimeMaster(object):
    """ Object that provide methods to initialize, control, monitor and shutdown a CHIME telescope
    array (or subarray)
    """

    # Define the minimum logging setup to be used until we have properly set-up logging from the config file
    DEFAULT_LOGGING = {
        'handlers': {
            'stderr': {'class': 'logging.StreamHandler', 'level': 'INFO'}
            },
        'loggers': {
            '': {'handlers': ['stderr']}  # root logger

            }
        }

    def __init__(self):

        # Setup logging for all the ch_acq package. This will apply to logs generated by raw_acq, kotekan, fpga_master etc.
        log.setup_logging(self.DEFAULT_LOGGING)

        self.log = log.get_logger(self) # i.e. fpga_master.ChimeMaster
        self.log.debug('%r: Creating ChimeMaster instance' % self)
        self.state = 'off'
        self.config = None
        self.start_time = None

        # Remote service provider objects
        self.raw_acq = {} # Raw FPGA data acquisitoin REST clients
        # self.kotekan = None # Kotekan REST clients
        self.fpgas = None # fpga_array object
        self.power_supply_servers = None # power supply REST server

        self.PROGRAM = os.path.realpath(__file__) # absolute path name to this module
        self.GIT_VERSION = get_git_version()
        self.startup_time = datetime.datetime.utcnow()

        self.log.info("program %s" % self.PROGRAM)
        self.log.info("version %s" % self.GIT_VERSION)

        self.gain_calc_metrics = Metrics()
        self.gain_hdf5 = None

    def set_config(self, config):
        self.config = NameSpace(config)

    #####################################
    # Power supply management
    #####################################
    # Operates the power supplies via the power supply server(s)

    # @coroutine
    # def create_power_supply_clients(self):
    #     """ create clients object that operate on the power supply server
    #     """
    #     ps_config = self.config.power_supplies

    #     # First create the client to the servers, and start the server if it is not already started
    #     self.power_supply_servers = {}
    #     server_nodes = ps_config.servers or {}
    #     for server_name, server_params in server_nodes.items():
    #         ps = PowerSupplyAsyncRESTClient(hostname=server_params.hostname, port=server_params.port) # we pass the whole server config to the client in case it needs th create and/or start the server
    #         yield ps.start(server_params)
    #         self.power_supply_servers[server_name] = ps

    #     # figure out which servers controls the power supply units we want to use in this experiment
    #     units = ps_config.power_on.units or []  # units used by fpga_master
    #     ps_names = set(units)
    #     self.power_supply_units = {}
    #     for server_name, server in self.power_supply_servers.items():
    #         server_ps_names = set((yield server.list_names()))
    #         common_ps_names = ps_names & server_ps_names # set intersection
    #         if common_ps_names:
    #            self.power_supply_units[server] = list(common_ps_names)
    #            ps_names -= common_ps_names
    #     if ps_names:
    #         raise RuntimeError('%r: Could not find a power supply server to handle the following supplies: %s' % (self, ps_names))

    # @coroutine
    # def power_on(self):
    #     """ Turn on the power supplies listed in the `power_supplies.power_on.units` config field.

    #     If the power supply is already ON, no action is taken. If not, it is turned on, and we wait
    #     for the power on delay specified in `power_supplies.power_on.delay`.
    #     """

    #     yield [ps.power_on(*ps_names) for ps, ps_names in self.power_supply_units.items()]

    # @coroutine
    # def power_off(self):
    #     """ Turn off the power supplies listed in the `power_supplies.power_on.units` config field.
    #     """
    #     yield [ps.power_off(*ps_names) for ps, ps_names in self.power_supply_units.items()]

    # @coroutine
    # def is_power_supply_ready(self):
    #     """ Check is all power supplies listed in the `power_supplies.power_on.units` config field are ready.
    #     """
    #     # Get the is_ready dict for each power supply server as [ {ps_name: state,...}, {ps_name: state, ...}]
    #     is_ready = yield [ps.is_ready() for ps, ps_names in self.power_supply_units.items()]
    #     # Check if the flag for each supply associated with each server is True
    #     coroutine_return(all(is_ready[i][ps_name]
    #                          for i, ps_names in enumerate(self.power_supply_units.values())
    #                          for ps_name in ps_names))

    # @coroutine
    # def wait_for_power_supply(self):
    #     while not (yield self.is_power_supply_ready()):
    #         self.log.warn('Waiting for power supplies')
    #         yield sleep(5)


    #####################################
    # RAW_ACQ management methods
    #####################################

    @coroutine
    def start_raw_acq_servers(self):
        """ Start raw data acquisition servers and set the FPGAs raw data transmit addresses.

        Requires the FPGAs to be initialized.

        Creates

             self.raw_acq_ports (dict):  List of port entries {port, iceboards)  associated with each RawAcq server.
             self.raw_acq_ibs (dict): lists the iceboards objects associated with each RawAcq server.

        """
        self.log.info('%r: starting raw_acq servers' % self)

        # Create RawAcq REST clients.
        self.raw_acq = {}
        nodes = self.config.raw_acq.servers or {}

        if not nodes:
            return

        for node_name, node_params in nodes.items():
            self.raw_acq[node_name] = RawAcqAsyncRESTClient(name=node_name, create_server=False, **node_params)

        # Check if server is running
        for raw_acq_server_name, raw_acq_client in self.raw_acq.items():
            present = yield raw_acq_client.ping()
            if not present:
                raise RuntimeError('%r: raw_acq server %s (%r) is not running' % (self, raw_acq_server_name, raw_acq_client))

        conf = self.config.raw_acq
        #print(conf)

        # Create a list of IceBoard objects that correspond to each port entry of
        # each server. Check that an iceboard is not allocated twice while
        # doing that.
        self.raw_acq_ports = {}  # list of ports associated with each receiver
        self.raw_acq_ibs = {}  # iceboard objects associated with each receiver
        self.raw_acq_stream_ids = {} # stream ID that each receiver should expect. This is used by raw_acq to pre-allocate the buffers and create the mapping tables.
        all_ibs = set()  # keeps track of Iceboard objects used so far so we can detect multiple assignments
        for server_name, server_conf in (conf.servers or {}).items():
            self.raw_acq_ports[server_name] = []  # [{'port':port_name, 'iceboards':[ib1, ib2, ]}, ...]
            self.raw_acq_ibs[server_name] = []  # [{'port':port_name, 'iceboards':[ib1, ib2, ]}, ...]
            self.raw_acq_stream_ids[server_name] = []  # [int0, int1, ...]
            for port_config in server_conf.receiver_ports:
                # Get a set of iceboard objects specified in sources for this port
                ibs = self.fpgas.get_iceboards(port_config['sources'])
                # Make sure no iceboard was already assigned
                if not set(ibs).isdisjoint(all_ibs):
                    raise RuntimeError('Some FPGA board(s) are assigned to multiple RawAcq ports. Check your config.')
                all_ibs.update(ibs)
                self.raw_acq_ibs[server_name].extend(ibs)
                # Store the entry
                self.raw_acq_ports[server_name].append(NameSpace(port=port_config['port'], iceboards=ibs))
                for ib in ibs:
                    self.raw_acq_stream_ids[server_name].extend(ib.get_stream_ids())

        # Start each RawAcq server with a port for each assigned iceboard. For each port, we provide
        # the address of the (only) source FPGA board. The server will ping this address back to
        # set-up the switches routing tables and figure out on which interface the data will be
        # arriving. It will then return the addresses (ip_addr, port, mac_addr) to which the data
        # should be sent.

        # Process the server/port list to generate the receiver port parameters
        #
        # If conf.use_fixed_port_numbers=True and the port name is 0 or None, then each board is assigned a fixed receiver port number
        # That info is stored in::
        #
        #   recv_ports[server_name] = [ {'port': port_number, sources: list_of_sources}]
        #
        # and will be passed later to the server to
        # initialize the receiver.
        #
        # [{port_number: [source1, source2 ...]} dictionary
        recv_ports = {}  # list of ports and associated sources to open on each server
        recv_names = {}  # name of the receiver assigned to each server
        for server_name, port_configs in self.raw_acq_ports.items():  # for each raw_acq server
            # Name of the receiver object, which can hande multiple ports.
            recv_names[server_name] = '%sRecv' % server_name
            recv_ports[server_name] = []
            for port_entry in port_configs: # for each port definition entry
                # If port=0 amd we want fixed port number, create an entry for each board with the appropriate numeric port derived from the crate and slot number
                if not port_entry.port and conf.use_fixed_port_numbers:
                    for ib in port_entry.iceboards:
                        (crate, slot) = ib.get_id(default_crate=0, default_slot=0)
                        port_id = 42400 + 100 * crate + slot
                        recv_ports[server_name].append(dict(port=port_id, sources=[(ib.hostname, 80)]))
                else:
                    # Port number is non-zero, so we ask the receiver to use this exact port
                    port_id = port_entry.port or 0
                    src_addresses = [(ib.hostname, 80) for ib in port_entry.iceboards]
                    recv_ports[server_name].append(dict(port=port_id, sources=src_addresses))



        # Start the receivers concurrently
        start_results = yield {server_name: raw_acq_server.start(
                name=recv_names[server_name],
                ports=recv_ports[server_name],
                stream_ids=self.raw_acq_stream_ids[server_name],
                comet_broker=conf.common_config.comet_broker.as_dict(),
                jump_thresholds=conf.common_config.jump_thresholds,
                metrics_refresh_time=conf.common_config.metrics_refresh_time,
                adc_rms_refresh_count=conf.common_config.adc_rms_refresh_count,
                data_folder=self.data_folder,
                run_folder=self.run_folder,
                run_name=self.run_name,
                corr_name=self.corr_name
                )
            for server_name, raw_acq_server in self.raw_acq.items()}

        # Configure the FPGA transmit addresses based on what the receiver returned
        for server_name, start_result in start_results.items(): # for each RawAcq server
            # The start command returned the target address to use for each data source as a list in the format
            #    [ ((src_ip, src_port), (if_ip, port, mac)) ...].
            # We convert this to a dict {(src_ip, src_port):(if_ip, port, mac),...} for easy lookup
            targets = {tuple(src_addr): target_addr for src_addr,target_addr in start_result['target_addr']}
            for ib in self.raw_acq_ibs[server_name]:
                    ip_addr, port, eth_addr = targets[(ib.hostname, 80)]
                    self.log.info('%r: Setting data transmission address if board %s to %s:%i (%s)' % (self, ib.get_id(), ip_addr, port, eth_addr))
                    ib.set_data_target_address(ip_addr, port, eth_addr)
        self.log.info('%r: RawAcq server setup successfully' % self)

    @coroutine
    def start_fpga_raw_data_transmission(self, capture_rate=None, capture_source=None, tmux_factor=None, sync=False):
        """ Configure the FPGAs to transmit raw data.

        This is the static baseline data capture configuration that cannot be
        dynamically changed without sync(). See set_fpga_data_capture() for
        on-the-fly rource and rate changes.

        Parameters:

            capture_rate (float): Number of frames to send per second.

            capture_source (str): selects the data source. 'adc':  the data is taken after the function generator (sorry, non
                intuitive). `scaler`: the data is taken after the scaler. Default is 'scaler'.

            tmux_factor (int): Number between 0 and 64.  Raw data transmission is staggered across FPGAs in the array
                with a step size equal to tmux_factor * 524.288 microsec.

        If no arguments are provided, the FPGA will be set to transmit data at the idle rate and from source defined in the config file.
        """
        conf = self.config.fpga.raw_data_capture
        capture_source = capture_source or conf.capture_source
        capture_rate = capture_rate or conf.baseline_capture_rate
        tmux_factor = tmux_factor or conf.tmux_factor
        capture_period = 1.0 / float(capture_rate)

        for server_name, ibs in self.raw_acq_ibs.items():
            # Compute a transmission delay for each board to prevent them from sending their data all at the same time
            for ib in ibs:
                (crate, slot) = ib.get_id(default_crate=0, default_slot=0)
                # send_delay = int(tmux_factor * (16 * crate + slot))
                send_delay = int(tmux_factor * (slot))

                self.log.info('%r: Starting data capture on %r with period=%f, source=%s, send_delay=%d' %
                             (self, ib, capture_period, capture_source, send_delay))

                ib.start_data_capture(period=capture_period, source=capture_source, send_delay=send_delay)

        # If not done explicitely later, we must issue sync command after starting raw data capture,
        # otherwise raw frames will not be synced across boards.
        if sync:
            self.fpgas.sync()


    @coroutine
    def set_fpga_data_capture(self, chan_ids=None, capture_rate=23, source=None):
        """
        Sets the data source and capture rate for the specified channels. This
        can be called at any time after array initializationand does not
        require sync.


        chan_id: channels to be configured. Is processed through ca.get_iceboards()


        """

        if isinstance(source, basestring):
            source = str(source) # make sure we don't have unicode

        conf = self.config.fpga.raw_data_capture
        capture_source = source or conf.capture_source

        ib_chans = self.fpgas.get_iceboards(chan_ids, lane_type='chan').items()

        for (ib, channels) in ib_chans:
            ib.set_data_capture(channels=channels, sub_period=capture_rate, source=capture_source)
            yield moment


    @coroutine
    def set_gains(self, gains=None):
        """
        Sets the data source and capture rate for the specified channels. This
        can be called at any time after array initializationand does not
        require sync.


        Parameters:

            gains (dict or list): list describing which channels are involved
            and what gains are applied to them. In the format::

                [ (target, (glin, glog)), ...]

        or::

                { target: (glin, glog), ...}


        """
        if isinstance(gains, dict):
            gains = gains.items()

        for target, gain in gains: # format: [ (target, (glin, glog)), ...]
            ib_chans = self.fpgas.get_iceboards([target], lane_type='chan').items()  # Returns [(ib, [chan, ...]), ...]
            self.fpgas.set_gains({ib.get_id(chan):gain for ib,chans in ib_chans for chan in chans}, bank=0, when='now')

    @coroutine
    def compute_gains(self,
                     targets=None,
                     capture_rate = 23,
                     save_gains=False,
                     enable=True,
                     noise_injection=None,
                     number_of_fft_averages=100,
                     number_of_gain_update_iterations=20,
                     weight=0.2,
                     initial_gains=[ ('*', [1.0, 22])]):
        """
        Starts the background iteratove process of computing the optimal digital gains on the FPGA to acheive the target RMS.

        Parameters:

            targets: list of tuples (or dict) describing the (crate, board,
                channel) (or {crate:c, board:b, channel:ch}) whose gains needs
                to be recomputed. Missing elements, `None` or  ``"*"`` is treated
                as a wildcard.

            capture_rate (int): Sets how fast the data is to be temporarily
                transmitted and captured for the selected channel. This sets
                the number of frames between captures, which is a power of 2
                set by ``Nframes=2**(capture_rate+1)``. Independently of this,
                the rate cannot be slower than the promary capture rate set at
                FPGA initialization.

            enable (bool): if False, nothing is done.

            noise_injection: noise injection parameters to be set for these
                gains computations. If it evaluates to False, noise injection
                parameters are not set. Note that multiple channels might be
                affected by this, not just the sleected channels.

            number_of_fft_averages (int): Number of FFT frames that will be
                captured and averaged before returning the averages spectrum
                that will be used to perform a gain update iteration. Defaults
                to 100. This parameter and the capture rate affects the speed
                at which the gain computations will occur

            number_of_gain_update_iterations: Number of incremental gain
                updates that will be performed before the final gain solution.

            weight (float). NUmber between 0 and 1. Indicates the weigh of the
                new data in theevolving gain solution.

            initial_gains (list):  list of [(target, (glin, glog)),...] describing the initial
                gains to be used to start computing new gains.


        Examples:

            chan_id = [(0,1), (1,3,4)] or [{crate:0, slot:1}, {crate:1, slot:3, channel:4}] # Select all channels of board in crate 0 slot 1, and channel 4 of crate 1 slot 3.
            chan_id = None # Selects all boards and channels in the array
            chan_id = [(4,'*', 5)] or [(4, None, 5)] or [{crate:4, channel:5}, {crate:4, slot:'*', channel:5}  # select channel 5 of all boards in crate 4

        """

        # Make sure gain calculation is enabled
        if not enable:
            return

        # Get a {iceboard:[list_of_channels]} dict of selected channels
        ib_chans = self.fpgas.get_iceboards(targets, lane_type='chan').items()

        if not ib_chans:
            raise RuntimeError('No target board was found for the specified patterns')

        self.log.info('%r: *** Gain calculator : Starting compute_gains() on the following channels: %s' % (self, ', '.join(str(ib.get_id()) + str(ch) for ib,ch in ib_chans)))

        # Set the source and data capture rate for target channels

        for (ib, channels) in ib_chans:
            ib.set_data_capture(channels=channels, sub_period=capture_rate, source='scaler')

        if noise_injection is not None:
            raise AttributeError('Noise injection settings are not yet supported for gain computations')

        # find the channel ID and stream ID associated with each raw_acq server
        server_channel_ids = {} # will be returned with the new gains so set_gains can apply gains to the proper board
        server_stream_ids = {} # will be used by raw_acq to select the proper channels
        all_channel_ids = []
        all_stream_ids = []
        for server_name, ibs in self.raw_acq_ibs.items():
            server_channel_ids[server_name] = []
            server_stream_ids[server_name] = []
            for (ib, channels) in ib_chans:
                if ib in ibs:
                    cids = ib.get_channel_ids(channels)
                    sids = ib.get_stream_ids(channels)
                    server_channel_ids[server_name].extend(cids)
                    server_stream_ids[server_name].extend(sids)
                    all_channel_ids.extend(cids)
                    all_stream_ids.extend(sids)
        sid_index_map = {sid:i for i,sid in enumerate(all_stream_ids)}

        # load the current gains as initial gains if an initial gain table is not provided.
        if not initial_gains:
            initial_gains = [(cid, gains) for cid, gains in self.fpgas.get_gains(bank=0).items() if cid in all_channel_ids]

        # compute an approxitame amount of time to wait for the data, which is 1/2 of the time it should date to accumulate
        wait_time = min(2.56e-6 * 2**(capture_rate + 1) * number_of_fft_averages / 2, 1)

        @coroutine
        def iterate_gains(server, channel_ids, stream_ids):

            self.log.info('%r: *** Gain calculator : Starting gain calculator iterator process' % self)
            # Greate a gain calculator engine
            gc = calculate_gains.GainCalc(
                channel_ids=channel_ids,
                stream_ids=stream_ids,
                n_iterations=number_of_gain_update_iterations,
                weight=weight,
                initial_gains=initial_gains)
            # Set all the initial gains on bank 0
            bank = 0
            yield self.fpgas.set_gains.async(gains=gc.get_gains(), bank=bank, when='now')
            # start the integration of FFT data for specified channels
            # We will iterate until all channels have a solution, or until we have reached an iteration limit
            iteration = 0
            fft_rms_requested = {sid:False for sid in stream_ids}

            while True:
                self.log.info('%r: *** Gain calculator : Acquiring data block %i' % (self, iteration))
                required_sids = [sid for sid, requested in fft_rms_requested.items() if not requested]
                yield server.start_fft_rms(
                    stream_ids=required_sids,
                    target_gain_bank=bank,
                    number_of_frames=number_of_fft_averages)
                # Flag the stream IDs that that we required. They may or may not come in immediatly on the next poll.
                for sid in required_sids:
                    fft_rms_requested[sid] = True

                while True:
                    self.log.info('%r: *** Gain calculator : waiting for averaged FFT data from raw acq for %.3f s' % (self, wait_time))
                    yield sleep(wait_time)
                    sids, rms = yield server.get_fft_rms()
                    if sids:
                        break
                # self.log.info('%r: *** Gain calculator : Got FFT RMS values for Channel ID: Stream ID%s' % (self,
                #     ', '.join('%s:%i' % (channel_ids[sid_index_map[sid]], sid) for sid in sids if sid in sid_index_map)))
                self.log.info('%r: *** Gain calculator : Got FFT RMS values for %i channels' % (self, len(sids)))
                new_gains = gc.update_gains(np.array(sids), np.array(rms))
                # bank ^= 1 # switch bank  # Can't do that right now: the formware does not switch glog
                yield self.fpgas.set_gains.async(gains=new_gains, bank=bank, when='now')
                # Indicate we need to request new rms values for the channels that were just processed
                for sid in sids:
                    fft_rms_requested[sid] = False

                self.gain_calc_metrics.add('fpga_gains_done', value=np.sum(gc.done))
                # generate some metrics

                for cid, (glin, glog) in new_gains.items():
                    self.gain_calc_metrics.add('fpga_gain_value',
                        channel_id=cid,
                        value=np.mean(glin[1:]) * 2**glog)

                for j, sid in enumerate(sids):
                    if sid not in sid_index_map:
                        continue
                    bix = sid_index_map[sid]
                    cid = channel_ids[bix]
                    self.gain_calc_metrics.add('fpga_gain_calc_rms',
                        stream_id=sid, channel_id=cid,
                        value=np.mean(np.array(rms)[j, 1:]))
                    self.gain_calc_metrics.add('fpga_gain_calc_iteration',
                        value=iteration)
                    self.gain_calc_metrics.add('fpga_gain_calc_channel_iteration',
                        stream_id=sid, channel_id=cid,
                        value=gc.iteration_number[bix])
                    self.gain_calc_metrics.add('fpga_gain_calc_percent_complete',
                        stream_id=sid, channel_id=cid,
                        value=gc.iteration_number[bix].astype(np.float32) / number_of_gain_update_iterations * 100.0)
                # if gc.is_done() or (iteration > 2 * number_of_gain_update_iterations):
                if gc.is_done():
                    break
                iteration += 1
                yield sleep(1)

            filtered_gains, mask = gc.get_filtered_gains()
            yield self.fpgas.set_gains.async(gains=filtered_gains, bank=0, when='now')

            self.log.info('%r: *** Gain calculator : Finished computing gains. Gain calculations completed successfully for %d channels.' % (self, len(filtered_gains)))

        # Perform the gain iterations in parallel on all raw acq servers
        yield [iterate_gains(raw_acq_server, server_channel_ids[raw_acq_server_name], server_stream_ids[raw_acq_server_name])
               for raw_acq_server_name, raw_acq_server in self.raw_acq.items()]

        # Return the data capture of the selected channels to the adc source and baseline capture rate
        yield self.set_fpga_data_capture(targets)

        # If requested save the gains
        if save_gains:
            self.log.info('%r: *** Gain calculator : saving gains' % (self,))
            yield self.save_gains(bank=0)

    @coroutine
    def save_gains(self, bank=0):
        """
        Read gains from FPGAs and save them to the HDF5 archive.

        Parameters:

            bank : 0 or 1
                Read the gains from this bank.

        """
        if self.gain_hdf5 is None:
            msg = 'Digital gain archive not yet initialized. Cannot save digital gains.'
            self.log.error('%r: %s' % (self,  msg))
            raise RuntimeError(msg)

        gains = yield self.fpgas.get_gains.async(bank=bank, use_cache=True)
        gains = {self._chan_id_to_serial_number(key): val for key, val in gains.items()}

        gain_timestamps = yield self.fpgas.get_gain_timestamps.async(bank=bank)
        gain_timestamps = {self._chan_id_to_serial_number(key): val for key, val in gain_timestamps.items()}

        self.gain_hdf5.set_gain(gains, compute_time=gain_timestamps)
        self.gain_hdf5.write(smp=time.time(), run_name=self.run_name)
        self.log.info('%r: saved current gains to file %s.' % (self, self.gain_hdf5.archive_files[-1]))

    @coroutine
    def serial_compute_gains(self, **params):
        """
        Wrapper for `compute_gains` that computes the gains for the target channels in serial.

        Parameters:

            targets: list of tuples (or dict) describing the (crate, board,
                channel) (or {crate:c, board:b, channel:ch}) whose gains needs
                to be recomputed. Missing elements, `None` or ``"*"`` are treated
                as a wildcard.  List will be iterated over and the channels matching
                each element of the list will have their gains computed in parallel.
                If not provided, then will default to  a list of the crates.

            **accepts all other parameters for `compute_gains`**

        """

        targets = params.pop('targets', None)
        # If no targets were provided then we default to computing gains for one crate at a time
        if targets is None:
            targets = [[icecrate.crate_number, "*", "*"] for icecrate in self.fpgas.ic]

        # Loop over targets
        for group in targets:
            self.log.info("%r: Computing gains for target: %s" % (self, group))
            yield self.compute_gains(targets=[group], **params)

    @coroutine
    def start_hdf5_capture(self, capture_folder=None, capture_filename=None, capture_refresh_time=None,
                           capture_duration=None, capture_elements_per_file=None):
        """
        Instructs the raw_acq server to start storing raw data in HDF5 files
        at a specified rate, duration and in the specified folder.


        Parameters:

            capture_folder (str): path to the folder where the raw data folder will be created. If it is
                a relative path, it will be relative to the run folder. If not specified or `None`, it
                will be taken from the config file.

            capture_filename (str): name of the folder in which the HDF5 files ``nnnnnn.h5`` will be
                created. Is prepended with the time. If not specified or `None`, it will be taken
                from the config file.

            capture_refresh_time (float): cadence in seconds at which raw data is written to the hdf5 file.

            capture_duration (float): period of time (in seconds) during which the captured data
                will be stored to HDF5 files. After which the capture will revert to the idle rate.
                if ``0``, the capture will continue indefinitely.  If not specified or `None`, it will be taken
                from the config file.

            capture_elements_per_file (int): Number of frames to store in each HDF5 files. If not
            specified or `None`, the parameter is taken from the config file.

        """
        conf = self.config.raw_acq.common_config
        capture_folder = capture_folder or conf.hdf5_capture_folder
        capture_filename = capture_filename or conf.hdf5_capture_filename
        capture_duration = capture_duration or conf.hdf5_capture_duration
        capture_elements_per_file = capture_elements_per_file or conf.hdf5_capture_elements_per_file
        capture_refresh_time = capture_refresh_time or conf.hdf5_capture_refresh_time

        if capture_duration is not None:
            self.log.info('%r: Starting HDF5 data capture for %f seconds (0 = infinite)' % (self, capture_duration))

            yield [server.start_raw_hdf5(
                base_dir=capture_folder,
                base_filename=capture_filename,
                capture_duration=capture_duration,
                capture_refresh_time=capture_refresh_time,
                elements_per_file=capture_elements_per_file
                )         for server_name, server in self.raw_acq.items()]

    @coroutine
    def start_corr_hdf5_capture(self,
                           capture_folder=None,
                           capture_filename=None,
                           capture_duration=None,
                           capture_n_inputs=None,
                           capture_elements_per_file=None):
        """
        Instructs the raw_acq server to start storing raw data in HDF5 files
        at a specified rate, duration and in the specified folder.


        Parameters:

            capture_folder (str): path to the folder where the raw data folder will be created. If it is
                a relative path, it will be relative to the run folder. If not specified or `None`, it
                will be taken from the config file.

            capture_filename (str): name of the folder in which the HDF5 files ``nnnnnn.h5`` will be
                created. Is prepended with the time. If not specified or `None`, it will be taken
                from the config file.

            capture_refresh_time (float): cadence in seconds at which raw data is written to the hdf5 file.

            capture_duration (float): period of time (in seconds) during which the captured data
                will be stored to HDF5 files. After which the capture will revert to the idle rate.
                if ``0``, the capture will continue indefinitely.  If not specified or `None`, it will be taken
                from the config file.

            capture_elements_per_file (int): Number of frames to store in each HDF5 files. If not
            specified or `None`, the parameter is taken from the config file.

        """
        conf = self.config.fpga.firmware_correlator
        capture_folder = capture_folder or conf.hdf5_capture_folder or '.'
        capture_filename = capture_filename or conf.hdf5_capture_filename
        capture_duration = capture_duration or conf.hdf5_capture_duration
        capture_elements_per_file = capture_elements_per_file or conf.hdf5_capture_elements_per_file
        capture_n_inputs = capture_n_inputs or conf.hdf5_capture_n_inputs
        software_integration_period = conf.software_integration_period

        if conf.enable and capture_duration is not None:
            self.log.info('%r: Starting HDF5 data capture for %f seconds (0 = infinite)' % (self, capture_duration))

            print('************#### firm integ=%s'% self.corr_firmware_integration_period)
            yield [server.start_corr_hdf5(
                base_dir=capture_folder,
                base_filename=capture_filename,
                capture_duration=capture_duration,
                capture_n_inputs=capture_n_inputs,
                elements_per_file=capture_elements_per_file,
                software_integration_period=software_integration_period,
                firmware_integration_period=self.corr_firmware_integration_period, # also for time computation only
                frame0_irigb_time=self.frame0_irigb_time.nano if self.frame0_irigb_time else 0,  # update frame 0 time from last sync

                )         for server_name, server in self.raw_acq.items()]


    def set_state(self, new_state):
        """ Sets the state to a specified value. Used for debugging. """
        self.state = new_state

    def expand_path(self, pattern, extra_fields={}):
        """ Expand fields in a string.
        """
        fields = {
            'start_time': self.start_time,
            'isotime': self.run_isotime,
            'localtime': self.run_localtime,
            'corr_name': self.corr_name,
            'data_folder': self.data_folder,
            'run_name': self.run_name,
            'run_folder': self.run_folder
            }
        fields.update(extra_fields)
        return os.path.expanduser(pattern % fields)

        # Register configuration with the Comet server
    def register_config(self):

        config = self.config.as_dict()

        # Register config with comet broker
        try:
            enable_comet = config['comet_broker']['enabled']
        except KeyError:
            msg = "Missing config value 'comet_broker/enabled'."
            self.log.error('%r: %s' % (self, msg))
            raise RuntimeError(msg)
        if enable_comet:
            if comet is None:
                msg = "Failure importing comet for configuration tracking.  Please install the " \
                      "comet package or set 'comet_broker/enabled' to False in config."
                self.log.error('%r: %s' % (self, msg))
                raise RuntimeError(msg)
            try:
                comet_host = config['comet_broker']['host']
                comet_port = config['comet_broker']['port']
            except KeyError as exc:
                msg = "Failure registering initial config with comet broker: 'comet_broker/{}' " \
                      "not defined in config.".format(exc[0])
                self.log.error('%r: %s' % (self, msg))
                raise RuntimeError(msg)
            comet_manager = comet.Manager(comet_host, comet_port)
            try:
                comet_manager.register_start(self.startup_time, self.GIT_VERSION)
                comet_manager.register_config(config)
            except comet.CometError as exc:
                msg = 'Comet failed registering fpga_master start and initial config: {}'.format(exc)
                self.log.error('%r: %s' % (self, msg))
                raise RuntimeError(msg)
        else:
            self.log.warning("Config registration DISABLED. This is only OK for testing.")

    @coroutine
    def start(self, **config):
        """ Make the telescope operational by starting and initializing the FPGA F-Engine and the GPU X Engine (Kotekan), CHRX, and raw_acq remote processes. """
        self.log.debug('%r: Starting ChimeMaster instance' % (self))
        self.log.info('%r: Starting fpga_master.start()', self)

        if self.state != 'off':
            coroutine_return(dict(error='already started'))

        # Set/Get configuration
        if config:
            self.set_config(config)
        conf = config = self.config # Shortcut. We use `conf` a lot below.
        self.state = 'starting'


        self.start_time = time.time()
        self.run_isotime = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(self.start_time))
        self.run_localtime = time.strftime("%Y/%m/%d %H:%M:%S", time.localtime(self.start_time))

        # Give a more meaningful error if the user did not provide a valid config file
        if not hasattr(conf, 'corr_name'):
            raise RuntimeError('CHIME master configuration data does not define the correlator name. Was the correct object selected in the configuration file (i.e. config.yaml:object)')

        self.corr_name = conf.corr_name

        # Initialize run variables so they all exist for  expand_path
        self.data_folder = None
        self.run_name = None
        self.run_folder = None
        self.current_folder = None


        # Create run variables
        self.data_folder = self.expand_path(conf.data_folder)
        self.run_name = self.expand_path(conf.run_name)
        self.run_folder = self.expand_path(conf.run_folder)
        self.current_folder = self.expand_path(conf.current_folder)

        self.log.info('%r: Run parameters:')
        self.log.info('%r:    Correlator name: %s' % (self, self.corr_name))
        self.log.info('%r:    data folder: %s' % (self, self.data_folder))
        self.log.info('%r:    run folder: %s' % (self, self.run_folder))
        self.log.info('%r:    current folder symlink: %s' % (self, self.current_folder))

        # Register configuration with the Comet server
        self.register_config()


        # Create the run folder
        try:
            os.makedirs(self.run_folder)
        except OSError:
            errmsg = "Could not create directory '%s'!" % self.run_folder
            self.log.critical(errmsg)
            raise RuntimeError(errmsg)

        # Make a symlink to the run folder
        if hasattr(os, 'symlink'):
            try:
                os.remove(self.current_folder)
            except OSError as e:
                self.log.warn("%r: Could not remove current symlink '%s'. The error isn%s" % (self, self.current_folder, e))
            try:
                os.symlink(self.run_folder, self.current_folder)
            except OSError as e:
                self.log.warning("%r: Could not create a symlink '%s' to the run folder '%s'. The error is:\n%s" % (self, self.current_folder, self.run_folder, e))


        # log_filename = os.path.join(self.run_folder, "fpga_master.log")
        #import logging
        #print('exists: %s' % ('pychfpga.fpga_array' in logging.Logger.manager.loggerDict))
        #lo=logging.getLogger('pychfpga.fpga_array')
        #print('before setup: logger name=%s, level=%s, handlers=%s, disabled=%r' %(lo.name, lo.level, lo.handlers, lo.disabled))
        self.logging_handlers = log.setup_logging(
            conf.logging.dict_config,
            conf.logging.log_levels,
            base_package_name=conf.logging.base_package_name,
            actual_package_name=__name__.rpartition('.')[0], # full package path up to ch_acq (note: __package__ exists but is not consistently defined)
            script_name=conf.logging.script_name,
            run_folder=self.run_folder) # path will be inserted in filename strings containing "%(path)"
        #lo=logging.getLogger('pychfpga.fpga_array')
        #print('before setup: logger name=%s, level=%s, handlers=%s, disabled=%r' %(lo.name, lo.level, lo.handlers,lo.disabled))
        #lo.warning('Trop seche')
        self.log.info('%r: Logging configured'% self)
        # Now that the housekeeping is done, let's start the real work
        #print('LOGGING config before is %s' % self.config.logging)

        #print('YAML config is %s' % conf.logging.dict_config.as_dict())

        # Store the basic run info in the run folder
        filename = os.path.join(self.run_folder, 'config.yaml')
        #print('YAML config is %r' % self.config.logging.as_dict())
        with open(filename, 'w') as h:
            h.write(self.config.as_yaml())

        filename = os.path.join(self.run_folder, 'info.txt')
        with open(filename, 'w') as h:
            h.write('Run name: %s\n' % self.run_name)
            h.write('Run start time (local): %s\n' % self.run_localtime)
            h.write('Run start time (UTC): %s\n' % self.run_isotime)
            h.write('Correlator/config name: %s\n' % self.corr_name)
            h.write('Run folder: %s\n' % self.run_folder)

        # Create objects to communicates to the remote processes needed to run the array
        # yield self.create_power_supply_clients()
        # yield self.create_chrx_clients()  # CHRX nodes receive data processed by the GPU nodes
        # yield self.create_kotekan_clients()  # Kotekan processes run on the GPU nodes; they receive the data from the FPGAs over dedicated point-to-point FPGA-GPU 10G Ethernet links, perform the correlation on the data, and forward the processed data to the CHRX nodes
        # yield self.start_kotekan_servers()

        # power on the array
        # yield self.power_on()
        # yield self.wait_for_power_supply()


        ########################
        # Initializing the FPGAs
        ########################


        # Create FPGA Array object and and initialize FPGAs
        self.log.info("initializing FPGAs...")

        # shortcuts
        conf = self.config  # shortcut to shorten the code below
        fpga_array_params = conf.fpga.fpga_array_params

        #Define some FPGA-related system constants
        self.SAMPLING_FREQUENCY = float(fpga_array_params.samp_freq) * 1e6  # frequency in Hz
        self.SAMPLES_PER_FRAME = 2048
        self.SECONDS_PER_FRAME = self.SAMPLES_PER_FRAME / self.SAMPLING_FREQUENCY

        self.log.info("Sampling frequency is %0.3f MHz." % (self.SAMPLING_FREQUENCY / 1e6))

        # Create the FPGAArray object. This object will create a database of all FPGA boards, crates and
        # mezzanines as described by the ``fpga_array_params`` parameters.fpga_array_params If specified
        # in the parameters, the FPGAs will be loaded with their bitstream, communication with the FPGAs
        # will be established and all the Python objects needed to operate the FPGA firmware will be
        # created and initialized.
        self.fpgas = ca = FPGAArray(ioloop=IOLoop.current(), **fpga_array_params)  # Starts an independent ioloop while initializing. Web clients/server stop while
        yield ca.run.async()

        if not ca.ib: # if there are no boards in the array
            if conf.debug.get('allow_empty_fpga_array', False):
                self.log.warning('%r: There are no FPGAs in the array.' % self)
                coroutine_return()
            else:
                raise RuntimeError('No IceBoard could be found. Are the boards powered up? Is the network connection functional?')

        if not fpga_array_params.open:
            self.log.warning("fpga_array is initialized with open=0. Aborting the rest of the FPGA array initialization.")
            coroutine_return()

        # Set ADC delays from delay files. Recompute and save new delays if the files do not exist or if
        # the delays loaded from them do not work.
        yield ca.set_adc_delays.async(**conf.fpga.adc_delay_params)

        # Reset the correlator. Not sure if this is necesssary?
        # ca.ib.set_corr_reset(1)
        # time.sleep(0.1)
        # ca.ib.set_corr_reset(0)

        # Set-up channelizers to process data normally
        self.log.info("Setting-up channelizers")
        yield ca.set_channelizers.async(**conf.fpga.channelizer_params)


        # Set-up raw_acq servers to receive data from the boards specified in
        # the config. This will set-up the FPGA data transmission ports.
        self.log.info("Starting up raw_acq server(s)")
        yield self.start_raw_acq_servers()


        # Setup noise injection for normal operation
        self.setup_noise_injection(conf.fpga.noise_injection)

        # Start correlator data transmission if present in the FPGA-based
        # firmware correlationlator is present in the FPGA

        corr_config = self.config.fpga.get('firmware_correlator', {})

        if corr_config and corr_config.enable:
            self.corr_firmware_integration_period = corr_config.firmware_integration_period
            self.fpgas.ib.start_correlator(self.corr_firmware_integration_period)
            print('******************** Enabling corr with integ=', self.corr_firmware_integration_period)
        else:
            self.corr_firmware_integration_period = None
            print('******************** Corr is not enabled. Corr_config=%r, enable=%r' % (corr_config, corr_config.enable if corr_config else 'none'))

        # Start raw_data capture
        self.log.info("Starting baseline raw data data capture")
        yield self.start_fpga_raw_data_transmission(sync=False)

        self.log.info("Synchronizing the array...")
        self.log.info("%%%%%%%%%%%%%%%%%%%%%%%%%%%%% This is the last SYNC %%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%")
        ca.sync()  # synchronize all the boards in the array
        self.log.info("%%%%%%%%%%%%%%%%%%%%%%%%%%%%% Last SYNC is done %%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%")

        # save the time of frame0 after sync
        self.frame0_irigb_time = self.fpgas.sync_timestamp


        #######
        # Fom now on, we do not have to sync the array anymore
        #######


        # Clear errors accumulated during start and initialization
        self.log.info("Resetting FPGA statistics counters")
        self.reset_fpga_stats()
        self.reset_crossbar_stats()
        self.reset_bp_shuffle_stats()



        # Set default initial gains. Will be overriden below
        if 'initial_gains' in conf.fpga:
            # Get a {iceboard:[list_of_channels]} dict of selected channels
            self.log.info("%r: Overiding the following gains: %r" % (self, conf.fpga.initial_gains))
            yield self.set_gains(gains=conf.fpga.initial_gains)


        # Initialize the digital gain hdf5 writer
        self.log.info("%r: Initializing HDF5 gain archive reader/writer" % (self,))
        self.initialize_gain_hdf5()

        # Load most recent gains from archive into gain bank #0
        if conf.fpga.load_initial_gains and self.gain_hdf5:
            yield self.load_gains(update_id=None, bank=0, when='now')

        # Enable offset encoding for gain calculation
        if corr_config and corr_config.enable:
            self.log.info("*** Enabling offset encoding for gain calculation")
            # self.fpgas.ib.set_gains((0,0), bank=0, when='now')
            self.fpgas.ib.set_offset_binary_encoding(True)

        # Compute new gains if requested
        yield self.compute_gains(**conf.fpga.compute_gains)

        # self.log.info("Waiting for 2 seconds")
        # yield sleep(2)
        self.log.info("Finished initializing FPGAs")

        # Read the FPGA setting back from the FPGA
        self.log.info("Getting configuration data from all FPGAs")
        self.fpga_conf = yield self.fpgas.get_fpga_config.async(basic=True)


        # Start storage of raw_data received by the raw_acq server in HDF5 files
        self.log.info("Starting Raw data HDF5 data capture")
        yield self.start_hdf5_capture()

        # Disable offset encoding if correlating
        if corr_config and corr_config.enable:
            self.log.info("*** Disabling offset encoding")
            # self.fpgas.ib.set_gains((0,0), bank=0, when='now')
            self.fpgas.ib.set_offset_binary_encoding(False)

            self.log.info("Starting Correlator HDF5 data capture")
            yield self.start_corr_hdf5_capture()


        # Finished with initialization.
        self.log.info("Finished ch_master.start()")
        self.state = 'on'
        coroutine_return({})

    #def call_later(self, delay, callback):
    #    return IOLoop.current().call_later(delay, callback)


    @coroutine
    def update_channelizers(self, **params):

        # Log the new parameters
        output_str = ["  %-32s  %s" % (key + ':',  params[key]) for key in sorted(params.keys())]
        output_str.insert(0, 'Updating channelizer parameters:')
        self.log.info('\n'.join(output_str))

        # Sync if we are changing data source
        requested_dsrc = params['data_source']
        if requested_dsrc == 'funcgen':
            self.log.warning("Data source 'funcgen' is deprecated, use 'buffer' in future.")
            requested_dsrc = 'buffer'

        dsrc = self.fpgas.ib[0].get_data_source()[0]
        sync = (dsrc != requested_dsrc)

        self.log.info('Requested data source: %s | Current data source: %s | SYNC: %s' %
                      (requested_dsrc, dsrc, sync))

        # Set channelizers
        yield self.fpgas.set_channelizers.async(sync=sync, **params)

        # Update configuration with new channelizer params
        self.config.fpga.channelizer_params = NameSpace(params)



    def setup_noise_injection(self, ni_params):
        """
        Setup the noise gating PWM signals for all the boards specified in `ni_params`.

        `ni_params` is a dictionary containing the parameters passed to the fpga_array's setup_noise_injection() method.

        If no board is specified for an entry (.board evaluates to False), the parameters are ignored.
        """
        if not ni_params:
            self.log.info("%r: No noise injection settings; setup skipped." % (self))
        else:
            for source_name, source_params in ni_params.items():
                self.log.info("%r: Setting noise injection for source '%s' with parameters %s" % (self, source_name, source_params))
                if source_params.board:
                    self.fpgas.set_noise_injection(local_sync=True, **source_params)

    ###################################
    # Gains management
    ###################################


    def get_next_gain_switch_frame(self):
        """ Get the frame number of first frame of the next integration period and the remaining time before this frame occurs.
        """
        ib = self.fpgas.ib[0]
        gain_switch_delay = self.config.fpga.gain_switch_delay # how much extra time do we need to set-up the gains before switching
        gpu_integration_period = self.config.gpu.gpu_integration_period

        current_frame_number = ib.get_frame_number()
        next_gain_switch_frame = (1 + (current_frame_number + gain_switch_delay)//gpu_integration_period)*gpu_integration_period
        time_until_switch = (next_gain_switch_frame - current_frame_number)*self.SECONDS_PER_FRAME
        return (next_frame_number, time_until_switch)

    @coroutine
    def switch_gains(self, gain_map):
        """ Start using the specified gain map for the next available integration period and inform CHRX of the new gains.
        """
        if self.state != 'on':
            coroutine_return(dict(error='not started'))

        # get the currently inactive active gain bank from one single board. We want all boards to
        # use the same bank number to make the system more robust to gain qdesynchronization (if one
        # board misses its gain switch for instance).
        next_bank = self.fpgas.get_next_gain_bank()

        # Set the gains in the unused gain bank, but don't switch to them yet. This will take some unknown time
        self.fpgas.set_gains(gain_map, bank=next_bank)

        # Now that all the gains are stored, find out when we can switch them in.
        # This will be the next integer number of interation period. This includes a guard period to leave us time to instruct the FPGAs when to switch.
        (next_gain_switch_frame_number, time_until_switch) = self.get_next_gain_switch_frame()

        # Tell all the channels to switch to the currently unused bank at that frame number. That should be done within the guard period.
        self.fpgas.switch_gains(bank=next_bank, when=next_gain_switch_frame_number)

        # Tell chrx which gains are coming and when
        # yield self.pass_gains_to_chrx(next_gain_switch_frame_number, gain_map)


    def status(self):
        """ Get the operational status of the telescope as a dictionary"""
        status = dict(state=self.state)
        if self.state == 'on':
            status['config'] = self.config.as_dict()
        return status


    @coroutine
    def stop(self):
        """ Stop the F-engine and the correlator data acquisition processes"""
        if self.state == 'on':
            self.state = 'stopping'
            self.log.info("stopping acquisition")
            self.iceboard_cb.stop()
            log.stop_logging(self.logging_handlers) # remove the handlers that were created by setup_logging()
            reap_cached_sockets()

            # Close interface to gain archive
            if self.gain_hdf5 is not None:
                self.log.info('%r:  closing %s.' % (self, self.gain_hdf5.current_file))
                self.gain_hdf5.close_all()
                self.gain_hdf5 = None

            # Stop writing raw_acq to hdf5
            while self.raw_acq:
                server_name, server = self.raw_acq.popitem()
                msg = yield server.stop_raw_hdf5()
                self.log.info('%r:  stopping hdf5 writing for %s:  %s' % (self, server_name, msg))

            self.start_time = None
            self.state = 'off'
        coroutine_return({})


    def get_frequency_map(self, format):
        if format == 's:b': # stream_id:bins
            fmap = {self.fpgas.corner_turn_stream_ids[lane_id]:bins
                for lane_id, bins in self.fpgas.corner_turn_frequency_bins.items()}
        elif format == 'l:b': #lane_tuple: bins
            fmap = self.fpgas.corner_turn_frequency_bins
        elif format == 'l:s': #lane_tuple: stream_id
            fmap = self.fpgas.corner_turn_stream_ids
        elif format in ('l:cscb', 'l:cc', 'l:cb','l:bb'):
            fmap = self.fpgas. get_frequency_map(format=format)

        return fmap

    def get_channelizer_output(self):

        # Query each FPGA for its current buffer
        fpga_buffer = self.fpgas.get_chan_output()

        # Convert the input identifier and buffer
        # to a format that can be easily interpreted
        out_buffer = collections.OrderedDict()

        # Loop over correlator inputs
        for corr_loc, buff in fpga_buffer.items():

            # Create the input serial number using the format
            # specified in the config file
            input_sn = self._chan_id_to_serial_number(corr_loc)

            # Undo scaling and offset encoding.  Converts the buffer
            # from uint8 to float ranging from -8 to 7.
            out_buffer[input_sn] = [float((bf >> 4) - 8) for bf in buff]

        return out_buffer

    def reset_fpga_stats(self):
        self.fpgas.reset_fpga_stats()

    def reset_crossbar_stats(self):
        self.fpgas.reset_crossbar_stats()

    def reset_bp_shuffle_stats(self):
        self.fpgas.reset_bp_shuffle_stats()

    def run_sync(self, method_name, *args, **kwargs):
        """ Runs `method_name` in a ioloop and returns when completed"""

        def heartbeat_callback():
            print('M', end='')
        tornado.ioloop.PeriodicCallback(heartbeat_callback, 1000).start()
        return IOLoop.current().run_sync(functools.partial(getattr(self, method_name), *args, **kwargs))

    def run(self):
        """ Run the IOLoop until interrupted """
        IOLoop.current().start()

    @coroutine
    def load_gains(self, update_id=None, bank=0, when='now'):
        """Read gains from the archive and load on FPGAs.

        Parameters:
            update_id : str or float
                Either a unique update_id string or a unix timestamp.  If unix timestamp
                then the most recent update occuring before that timestamp will be loaded.
                Defaults to the last update_id.
            bank : 0 or 1
                Bank where there gains will be loaded.
            when : 'now' or int
                If `when` is 'now' or a negative integer, the target gains are made active immediately.
                If `when` is None, the gains are written in the specified bank but the bank switching is not activated.
                If `when` is a positive integer, the gains will be activated starting on the unix timestamp specified by `when`.
        """
        if not self.fpgas:
            msg = 'FPGA array not yet initialized. Cannot load digital gains.'
            self.log.error('%r: %s' % (self, msg))
            raise RuntimeError(msg)

        if not self.gain_hdf5:
            msg = 'Digital gain archive not yet initialized.  Cannot load digital gains.'
            self.log.error('%r: %s' % (self, msg))
            raise RuntimeError(msg)

        # If update_id not provided, then load the most recent gains.
        if update_id is None:
            update_id = self.gain_hdf5.last_update

        # Get the unique identifier for the requested gains
        uid = self.gain_hdf5.read(update_id, 'update_id')

        # Read the gains
        self.log.info("%r:  Reading digital gains from archive (update_id = %s)" % (self, uid))
        gains, gain_timestamps = self.gain_hdf5.read_gain(update_id=uid)

        # Convert the keys from serial numbers to (crate, slot, chan) tuples
        gains = {self._serial_number_to_chan_id(key): val for key, val in gains.items()}
        gain_timestamps = {self._serial_number_to_chan_id(key): val for key, val in gain_timestamps.items()}

        # Load to requested bank
        self.log.info("%r:  Loading digital gains in bank #%d" % (self, bank))
        yield self.fpgas.set_gains.async(gains, bank=bank, when=when, gain_timestamps=gain_timestamps)

        # Return the unique identifier of the gains that were loaded
        coroutine_return(uid)

    @coroutine
    def switch_digital_gains(self, delta_t_seconds=100):
        if self.fpgas:
            # Figure out gain switch frame number
            # Figure out integration period in frames. Currently just by checking the kotekan config file
            samples_per_data_set = 32768
            num_gpu_frames = 128
            frames_per_gpu_integration = samples_per_data_set*num_gpu_frames
            # Get current frame number
            current_frame_number = self.fpgas.ib[0].get_frame_number()
            self.log.info('The current FPGA frame number is %i' %current_frame_number)
            current_gpu_frame = int(current_frame_number/frames_per_gpu_integration)
            # Figure out frame number at which gains are switched
            frame_period_seconds = 2.56e-6 # Frame period in seconds = 2048/800e6. Should be read from config
            delta_t_frames = int(np.ceil(delta_t_seconds/frame_period_seconds)) # Number of frames to switch gains
            # The gain_switch_frame_number must be a multiple of frames_per_gpu_integration to switch at start of integration
            gain_switch_gpu_frame = int((current_frame_number + delta_t_frames)/frames_per_gpu_integration)
            # I assume that setting the gain_switch_frame_number for all boards takes ~1 integration period, so make sure there's enough time
            if (gain_switch_gpu_frame-current_gpu_frame)<2:
                # If gain_switch_gpu_frame-current_gpu_frame == 0 the gain_switch_frame_number already passed
                # If gain_switch_gpu_frame-current_gpu_frame == 1 the gain_switch_frame_number is the start of next gpu integration
                # which may not be enough time to set gain_switch_frame_number for all the boards
                gain_switch_gpu_frame = current_gpu_frame + 2
            gain_switch_frame_number = gain_switch_gpu_frame*frames_per_gpu_integration
            self.fpgas.switch_gains(when=gain_switch_frame_number)
            # Update and print the actual delta_t for switching gains
            delta_t_frames = gain_switch_frame_number - current_frame_number
            delta_t_seconds = delta_t_frames*frame_period_seconds
            self.log.info('new gains will be active on frame %i (in %.2f seconds) at the closest GPU integration start.' %(gain_switch_frame_number,
                delta_t_seconds))
            #coroutine_return(None) # Probably don't need this
        else:
            self.log.info('FPGA array not yet initialized. Cannot load digital gains.')

    def initialize_gain_hdf5(self):

        # Create frequency axis
        freq = self.SAMPLING_FREQUENCY - np.fft.fftfreq(self.SAMPLES_PER_FRAME, 1.0 / self.SAMPLING_FREQUENCY)
        freq = 1e-6 * freq[0:self.SAMPLES_PER_FRAME//2]
        freq = np.array(zip(freq, [np.median(np.abs(np.diff(freq)))] * freq.size),
                        dtype=[('centre', '<f8'), ('width', '<f8')])

        # Create input axis
        if self.config.input_reorder:
            inputs = np.array([(chan_id, input_sn) for reorder, chan_id, input_sn in self.config.input_reorder],
                              dtype=[('chan_id', 'u2'), ('correlator_input', 'S32')])
        else:
            inputs = np.array([(stream_id, self._chan_id_to_serial_number(chan_id))
                               for chan_id, stream_id in sorted(self.fpgas.get_stream_id_map().items(), key=lambda x:x[1])],
                              dtype=[('chan_id', 'u2'), ('correlator_input', 'S32')])

        # Initialize writer
        hdf5_conf = self.config.fpga.gain_hdf5.copy()
        hdf5_conf['output_dir'] = os.path.expanduser(hdf5_conf.get('output_dir', '.'))
        self.gain_hdf5 = DigitalGainArchive(freq=freq, input=inputs,
                                            instrument_name=self.config.corr_name,
                                            attrs={'git_version_tag': self.GIT_VERSION},
                                            **hdf5_conf)

    def _chan_id_to_serial_number(self, chan_id):

        crate, slot, chan = chan_id
        args_sn = {'corr_sn': self.config.corr_sn,
                   'crate': crate if not isinstance(crate, basestring) else 0,
                   'slot': slot + 1 if not isinstance(slot, basestring) else 1,
                   'slot_zero_based': slot if not isinstance(slot, basestring) else 0,
                   'chan': chan,
                   'input': self.config.input_number_map[chan]}

        return self.config.input_sn % args_sn

    def _serial_number_to_chan_id(self, sn):

        mo = re.match('%s(\d{2})(\d{2})(\d{2})' % self.config.corr_sn, sn)
        crate = int(mo.group(1))
        slot = int(mo.group(2))
        inp = int(mo.group(3))
        chan = self.config.input_number_map.index(inp)

        return (crate, slot, chan)



class DummyChimeMaster(ChimeMaster):
    """
    A variant of ChimeMaster that doesn't do anything hardware related.
    """

    def start(self, **config):
        self.config = NameSpace(config)
        return config

    def status(self):
        return self.config.as_dict()

    def stop(self):
        return {}


class ChimeMasterAsyncRESTServer(AsyncRESTServer):
    """ Wraps the ChimeMaster into a REST server which receives HTTP GET or POST requests and calls
    the correspnding ChimeMaster methods.
    """
    DEFAULT_PORT = 54321

    def __init__(self, address='', port=DEFAULT_PORT, dummy=False):

        super(ChimeMasterAsyncRESTServer, self).__init__(
            address=address,
            port=port,
            heartbeat_string='Cs')

        # self.port = port # port on which the web server will be run
        self.dummy = dummy


        # Use a dummy CHIME Master object if dummy is True
        ChimeMasterClass = DummyChimeMaster if self.dummy else ChimeMaster

        self.chime_master = ChimeMasterClass()
        self.future = None
        #self.add_periodic_callback(self.print_iceboard_info_callback, period=60000)
        #self.metrics_queue = Queue.Queue(1000)
        self.metrics = Metrics()
        self.last_metrics_client = None
        #self.add_periodic_callback(self._get_metrics, 3000)
        #self.start_time = None
        self.process= psutil.Process(os.getpid())
        self.log.info('%r: Python kernel PROCESS ID is %s' % (self, self.process))
        # Start metric gathering loops
        self._tick_line()
        self._get_system_metrics()  #
        self._get_arm_metrics()  #
        self._get_fpga_metrics()  #
        self._auto_restart_raw_acq()

        # Create a cached gps time
        self._gps_time = {}
        self._gps_lock = tornado.locks.Lock()
    @coroutine
    def shutdown(self):
        print('Shutting down CHIME Master')
        yield self.chime_master.stop()
        yield sleep(3)
        print('CHIME Master is shut down')

    def print_iceboard_info_callback(self):
        if self.chime_master.fpgas:
            self.chime_master.fpgas.print_iceboard_info()
        else:
            self.log.info('FPGA array not yet initialized. No houskeeping info to show.')

    ##########################
    # Target endpoint methods
    ##########################


    @coroutine
    @endpoint('echo') # must be applied before coroutine because we lose the method signature
    def echo(self, handler, **args):
        coroutine_return(args)

    @coroutine
    @endpoint('set-state')
    def set_state(self, handler, state=None):
        self.chime_master.set_state(state)
        coroutine_return(args)

    @coroutine
    @endpoint('start')
    def start(self, handler, **config):
        # print('%r: Received start command' % self)
        self.log.info('%r: Received start command' % self)
        def encode_utf8(x):
            """Convert unicode strings to utf-8 strings for the target object and any objects in lists or dictionaries"""
            if type(x) is unicode:
                return x.encode('utf8')
            elif type(x) is dict:
                return {encode_utf8(k):encode_utf8(v) for k,v in x.items()}
            elif type(x) is list:
                return map(encode_utf8, x)
            else:
                return x
        config = encode_utf8(config)  # convert all strings in the config dict into utf8
        def done(future):
            # print('Done')
            try:
                logger = log.get_logger(self)
                #print('Got ne wlogger %r' % logger)
                #print(' Logger name=%s, level=%s, handlers=%s, disabled=%s' % (logger.name, logger.level, logger.handlers, logger.disabled))
            except Exception as e:
                print('oops. chimeMaster Server start().done() Exception: %s\n' % e)
                pass
            if future.exception():
                #self.start_time = None
                logger.error('START Done with exception: \n%s' % future.exception())
            else:
                logger.info('START Done. result is %r' % future.result())
            return True
        #self.start_time = time.time()
        #print('START config is %s' % config['logging'])
        self.future = self.chime_master.start(**config)
        IOLoop.current().add_future(self.future, done)
        self.log = log.get_logger(self)  # update the self.log pointer to the new logger
        self.log.debug('%r: future created. fpga_master initilization is in progress' % (self))
        coroutine_return('Initialization in progress. Check status for completion.')

    @coroutine
    @endpoint('methods')
    def methods(self, handler):
        coroutine_return(self.get_endpoint_info())

    @coroutine
    @endpoint('status')
    def status(self, handler):
        """ Return a dict describing the state of the server.

        If the ChimeMaster raised an exception during the background start() process, the exception is risen now.

        Returns:
            dict: containing the fields:
                :state: (str): current state string.
                :is_ready (bool): true when ChimeMaster has finished initializing successfully
                :start_result (str): Messsage returned by ChimeMaster.start() command.
                :config (dict): Current configuration
        """
        t0=time.time()
        self.log.info('%r: requesting fpga_master status' % self)
        r = self.chime_master.status() # {state:x and config: y}. chome_master always exists.
        result = dict(state=r['state'])
        self.log.info("%r: fpga_master status is currently '%s'. It took %f seconds to get it" % (self, r['state'], time.time()-t0))
        result['is_ready'] = r['state'] == 'on' # so we don't have to know the string to check
        if self.future and self.future.done():
            try:
                result['start_result'] = self.future.result() # raise an error if start failed
            except Exception as e:
                print('oops. ChimeMaster status() exception while reading chime master object future result. Exception:\n %s' % e)
                raise
        else:
            result['start_result'] = None
        coroutine_return(result)

    @coroutine
    @endpoint('stop')
    def stop(self, handler):
        #self.start_time = None
        results = yield self.chime_master.stop()
        coroutine_return(results)

    @coroutine
    @endpoint('switch-gains')
    def switch_gains(self, handler, gain_map):
        results = yield self.chime_master.switch_gains(gain_map)
        coroutine_return(results)

    @coroutine
    @endpoint('reset-gpu-links')
    def reset_gpu_links(self, handler, board_ids=None):
        """ REST endpoint to reset the GPU links on specified boards

        Parameters:

            board_ids (list of tuple/dict): List of boards whose GPU links should be resetted.

        Returns:

            List of board IDs that were actually reset.

        Example::

            curl -d '{"board_ids": [["*"]]}' -H "Content-Type: application/json" -X POST http://localhost:54321/reset-gpu-links   # Resets allGPU links
        """
        if not (self.chime_master and self.chime_master.state == 'on' and self.chime_master.fpgas):
            self.log.warning("%r: FPGA array is not ready to accept command" % (self))
            coroutine_return(message="FPGA not ready", board_ids=[])
            # raise RuntimeError('FPGA array is not ready to accept command')
        actual_board_ids = yield self.chime_master.fpgas.reset_gpu_links.async(board_ids)
        self.log.info("%r: The GPU links for the following boards were reset: %s" % (self, actual_board_ids))

        coroutine_return(message='Resetted %i boards' % len(actual_board_ids), board_ids=actual_board_ids)

    @coroutine
    @endpoint('compute-gains')
    def compute_gains(self, handler, **params):
        """ REST endpoint to compute gains for desired channels.

        Parameters:

            targets (list of tuple/dict): List of tuples describing the
                (crate, slot, channels) for which gains shall be recomputed.
                Missing tuple elements, ``"*"`` and `None` are considered to be a
                wildcard.

            **accepts all other parameters for `ChimeMaster.compute_gains`**

        Example::

            curl  -H "Content-Type: application/json" -X POST http://localhost:54321/compute-gains -d '{"targets": [[0, 0, "*"]]}'
        """
        if not (self.chime_master and self.chime_master.state == 'on' and self.chime_master.fpgas):
            self.log.warning("%r: FPGA array is not ready to accept command" % (self))
            coroutine_return(message="FPGA not ready")

        future = self.chime_master.compute_gains(**params)

        coroutine_return(message='Gains update in progress')

    @coroutine
    @endpoint('set-data-capture')
    def set_data_capture(self, handler, **params):
        """ REST endpoint to set the data capture for desired channels.

        Parameters:

            targets (list of tuple/dict): List of tuples describing the
                (crate, slot, channels) for which gains shall be recomputed.
                Missing tuple elements, ``"*"`` and None are considered to be a
                wildcard.

            **accepts all other parameters for `ChimeMaster.compute_gains`**

        Example::

            curl -H "Content-Type: application/json" -X POST http://localhost:54321/set-data-capture -d '{"chan_ids": [[0, 0, "*"]], "source": "scaler", "capture_rate": 16}'
        """
        if not (self.chime_master and self.chime_master.state == 'on' and self.chime_master.fpgas):
            self.log.warning("%r: FPGA array is not ready to accept command" % (self))
            coroutine_return(message="FPGA not ready")

        yield self.chime_master.set_fpga_data_capture(**params)
        coroutine_return(message='Data capture updated')

    @coroutine
    @endpoint('set-gains')
    def set_gains(self, handler, **params):
        """ REST endpoint to set the gains for desired channels.

        Parameters:

            targets (list of tuple/dict): List of tuples describing the
                (crate, slot, channels) for which gains shall be set.
                Missing tuple elements, ``"*"`` and None are considered to be a
                wildcard.

            **accepts all other parameters for `ChimeMaster.compute_gains`**

        Example::

            curl -H "Content-Type: application/json" -X POST http://localhost:54321/set-gains -d '{"gains": [ [["*"]], [1.0, 22]] ]}'
        """
        if not (self.chime_master and self.chime_master.state == 'on' and self.chime_master.fpgas):
            self.log.warning("%r: FPGA array is not ready to accept command" % (self))
            coroutine_return(message="FPGA not ready")

        yield self.chime_master.set_gains(**params)
        coroutine_return(message='Gains updated')


    @coroutine
    @endpoint('serial-compute-gains')
    def serial_compute_gains(self, handler, **params):
        """ REST endpoint to compute gains for desired channels in serial.

        Parameters:

            targets: list of tuples (or dict) describing the (crate, board,
                channel) (or {crate:c, board:b, channel:ch}) whose gains needs
                to be recomputed. Missing elements, `None` or ``"*"`` are treated
                as a wildcard.  List will be iterated over and the channels matching
                each element of the list will have their gains computed in parallel.
                If not provided, then will default to  a list of the crates.

            **accepts all other parameters for `ChimeMaster.compute_gains`**

        Example::

            # Compute gains for crate 0 and then crate 1
            curl  -H "Content-Type: application/json" -X POST http://localhost:54321/serial-compute-gains -d '{"targets": [[0, "*", "*"], [1, "*", "*]]}'
        """
        if not (self.chime_master and self.chime_master.state == 'on' and self.chime_master.fpgas):
            self.log.warning("%r: FPGA array is not ready to accept command" % (self))
            coroutine_return(message="FPGA not ready")

        # Set any `compute_gain` keyword arguments not provided in the endpoint call
        # to the value in the config file
        for key, val in self.chime_master.config.fpga.compute_gains.items():
            if (key not in params) and (key != 'targets'):
                params[key] = val

        # Compute gains
        future = self.chime_master.serial_compute_gains(**params)

        coroutine_return(message='Serial gain update in progress')

    # @coroutine
    # @endpoint('kotekan-start')
    # def kotekan_start(self, handler, **config):
    #     results = yield [k.start(**config) for k in self.kotekan_clients]
    #     coroutine_return(results)

    @coroutine
    @endpoint('get-frame-time')
    def get_frame_time(self, handler):
        if self.chime_master and self.chime_master.state == 'on' and self.chime_master.fpgas and self.chime_master.fpgas.ib:

            try:

                with (yield self._gps_lock.acquire(timeout=datetime.timedelta(seconds=60))):

                    start_time = time.time()

                    if self._gps_time:
                        self.log.info("GPS time is %0.1f seconds old." % (start_time - self._gps_time['server_ctime'], ))

                    if not self._gps_time or ((start_time - self._gps_time['server_ctime']) > 60.0):

                        self.log.info("Capturing GPS time from FPGA motherboard.")

                        frame_number, gps_ts = yield self.chime_master.fpgas.ib[0].capture_frame_time.async(format='raw')

                        frame0_ts = self.chime_master.fpgas.sync_timestamp

                        self._gps_time = dict(
                            frame_number=frame_number,  # 48-bit frame number
                            gps_time=gps_ts.time_struct, # time structure [year, month, day, hour, minute, second, microsecond (float, 10 ns resolution)]
                            gps_ctime=gps_ts.time, # GPS time, expressed in ctime format (float expressing seconds since UTC epoch)
                            gps_nano=gps_ts.nano,
                            gps_time2=gps_ts.time_struct2, # time structure [year, month, day, hour, minute, second, microsecond (float, 10 ns resolution)]
                            gps_ctime2=gps_ts.time2, # GPS time, expressed in ctime format (float expressing seconds since UTC epoch)
                            gps_nano2=gps_ts.nano2,
                            server_ctime=gps_ts.system_time, # system time, expressed in ctime format (float expressing seconds since UTC epoch)
                            server_ctime_before =gps_ts.system_time_before, # system time, expressed in ctime format (float expressing seconds since UTC epoch)
                            start_ctime=self.chime_master.start_time,
                            frame0_time=frame0_ts.time_struct,
                            frame0_ctime=frame0_ts.time,
                            frame0_nano=frame0_ts.nano)

                        self.log.info("Capture successful.  Took %0.1f seconds." % (time.time() - start_time, ))


            except Exception as ex:
                self.log.error("Failed to capture GPS time: %s" % ex)
                coroutine_return({})

            else:
                coroutine_return(self._gps_time)

        else:
            coroutine_return({})

    @coroutine
    @endpoint('get-frame0-time')
    def get_frame0_time(self, handler):
        if (self.chime_master and self.chime_master.state == 'on' and self.chime_master.fpgas and
            self.chime_master.fpgas.sync_timestamp):

            frame0_ts = self.chime_master.fpgas.sync_timestamp

            gps_time = dict(start_ctime=self.chime_master.start_time,
                            frame0_time=frame0_ts.time_struct,
                            frame0_ctime=frame0_ts.time,
                            frame0_nano=frame0_ts.nano)

            coroutine_return(gps_time)

        else:
            coroutine_return({})

    @coroutine
    @endpoint('get-frequency-map')
    def get_frequency_map(self, handler,format='s:b'):
        coroutine_return(sanitize_for_json(self.chime_master.get_frequency_map(format=format)))

    @coroutine
    @endpoint('get-channelizer-output')
    def get_channelizer_output(self, handler):
        coroutine_return(sanitize_for_json(self.chime_master.get_channelizer_output()))

    @coroutine
    @endpoint('reset-fpga-stats')
    def reset_fpga_stats(self, handler):
        if self.chime_master and self.chime_master.state == 'on' and self.chime_master.fpgas:
            self.chime_master.reset_fpga_stats()
            coroutine_return(results='FPGA STATS RESET')

        else:
            coroutine_return('FPGA array not yet initialized.')

    @coroutine
    @endpoint('reset-crossbar-stats')
    def reset_crossbar_stats(self, handler):
        if self.chime_master and self.chime_master.state == 'on' and self.chime_master.fpgas:
            self.chime_master.reset_crossbar_stats()
            coroutine_return(results='CROSSBAR STATS RESET')

        else:
            coroutine_return('FPGA array not yet initialized.')

    @coroutine
    @endpoint('reset-bp-shuffle-stats')
    def reset_bp_shuffle_stats(self, handler):
        if self.chime_master and self.chime_master.state == 'on' and self.chime_master.fpgas:
            self.chime_master.reset_bp_shuffle_stats()
            coroutine_return(results='BP SHUFFLE STATS RESET')

        else:
            coroutine_return('FPGA array not yet initialized.')

    @coroutine
    @endpoint('power-on')
    def power_on(self, handler):
        """ Power up only the power supplies used in this run"""
        result = yield self.chime_master.power_on()
        coroutine_return(result)

    @coroutine
    @endpoint('power-off')
    def power_off(self, handler):
        """ Power down only the power supplies used in this run"""
        result = yield self.chime_master.power_off()
        coroutine_return(result)

    @coroutine
    @endpoint('abort')
    def abort(self, handler):
        """ Savagely stop the server for debugging purposes."""
        tornado.ioloop.IOLoop.instance().stop()
        coroutine_return(results='ABORTING NOW!')
        # sys.exit(-1)

    @coroutine
    @endpoint('call-fpga-array-method')
    def call_fpga_array_method(self, handler, **args):
        """  For debuging: calls any fpga_array method. """
        if not hasattr(self.chime_master, 'fpgas') or not self.chime_master.fpgas:
            handler.write(dict(error='FPGA array is not created yet'))
            return
        r = getattr(self.chime_master.fpgas, args['method_name'])(**args)
        coroutine_return(sanitize_for_json(r))

    @coroutine
    def _tick_line(self):
        """ Regularly prints a line. Used to debug coroutine call timing.
        """
        tick_number = 0
        while True:
            print('--(%i)-------------------------------------------------------------------------------------------------' % tick_number)
            tick_number += 1
            yield sleep(1)

    @coroutine
    def _auto_restart_raw_acq(self):
        """ Regularly check if raw_acq server is running. If not, restart it.
        """
        # Don't monitor raw_acq servers at all if not is defined
        try:
            while True:
                self.log.info('%r: ============ Checking status of Raw_acq servers' % (self, ))
                if (self.chime_master and self.chime_master.raw_acq and
                    self.chime_master.state == 'on' and self.chime_master.fpgas):
                    for server_name, server in self.chime_master.raw_acq.items():
                        try:
                            self.log.info('%r: Checking status of Raw_acq server %s' % (self, server_name))
                            result = yield server.status()
                            if not result['started']:
                                self.log.info('%r: Raw_acq server %s seems to be stopped. Restarting.' % (self, server_name))
                                yield self.chime_master.start_raw_acq_servers()
                                yield self.chime_master.start_hdf5_capture()

                        except (HTTPError, RuntimeError, Exception) as e:
                            self.log.error('%r: Failed to get status info from raw_acq server %s (%r) due to the following exception: %r' % (self, server_name, server, e))
                yield sleep(3)
        except Exception as e:
            self.log.error('%r: auto_restart_raw_acq raised the following exception: %r' % (self, e))


    @coroutine
    def _get_system_metrics(self):
        """ Continuously gather system metrics.
        """
        while True:
            self.log.info('%r: Starting to gather a new set of system metrics' % (self, ))

            # Get memory-related metrics
            mem = psutil.virtual_memory()
            self.metrics.add('ch_master_node_mem_total', value=mem.total)
            self.metrics.add('ch_master_node_mem_available', value=mem.available)
            self.metrics.add('ch_master_node_mem_percent', value=mem.percent)
            self.metrics.add('ch_master_node_mem_used', value=mem.used)
            self.metrics.add('ch_master_node_mem_free', value=mem.free)
            proc_mem = self.process.memory_info().rss
            self.log.info('%r: Python kernel mem usage is %i bytes' % (self, proc_mem))
            self.metrics.add('ch_master_node_process_mem_used', value=proc_mem)  # in bytes

            # Get CPU-related metrics
            cpu = psutil.cpu_times()
            self.metrics.add('ch_master_node_cpu_percent', value=psutil.cpu_percent())
            self.metrics.add('ch_master_node_cpu_user', value=cpu.user)
            self.metrics.add('ch_master_node_cpu_system', value=cpu.system)
            self.metrics.add('ch_master_node_cpu_idle', value=cpu.idle)

            # Get fpga_master related metrics
            if self.chime_master.start_time is None:
                run_time = 0
            else:
                run_time = time.time() - self.chime_master.start_time
            self.metrics.add('ch_master_run_time', value=run_time)

            yield sleep(10)

    @coroutine
    def _get_arm_metrics(self):
        """ Continuously gather metrics from the ARM processor on the ICEBoards.
        """
        self.log.info('%r: Starting ARM metrics gathering loop' % (self, ))
        while True:
            t0 = time.time()
            self.log.info('%r: Starting to gather a new set of ARM metrics' % (self, ))
            # print('************ Getting ARM Metrics!')
            try:
                if self.chime_master and self.chime_master.fpgas:
                    self.metrics.add(self.chime_master.gain_calc_metrics.pop()) # add whatever gain calc metrics we have
                    yield self.chime_master.fpgas.get_arm_metrics.async(self.metrics)
                    self.log.info('%r: Successfully got ARM metrics' % self)
            except Exception as e:
                self.log.warning('%r: Error getting ARM metrics. error is: %r\n%s' % (self, e, traceback.format_exc()))

            self.log.info('%r: Finished gathering ARM metrics.  It took %.1f seconds to gather those. We now have %i metrics.' % (self, time.time() - t0, len(self.metrics)))
            # print('%r: Finished gathering ARM metrics.  It took %.1f seconds to gather those. We now have %i metrics.' % (self, time.time() - t0, len(self.metrics)))
            yield sleep(10)

    @coroutine
    def _get_fpga_metrics(self):
        """ Continuously gather metrics from the FPGAs in the ICEBoards.
        """
        while True:
            t0 = time.time()
            self.log.info('%r: Starting to gather a new set of FPGA metrics' % (self, ))

            if self.chime_master and self.chime_master.state == 'on' and self.chime_master.fpgas:
                try:
                    self.log.info('%r: Scraping metrics from FPGAs' % (self))
                    yield self.chime_master.fpgas.get_fpga_metrics.async(self.metrics, reset=self.chime_master.config.fpga.reset_stats)
                except Exception as e:
                    self.log.warning('%r: Error getting FPGA metrics. error is: %r\n%s' % (self, e, traceback.format_exc()))
            else:
                self.log.info('%r: We got a metrics requests but we are not yet ready to scrape metrics from the FPGAs. Ignoring.' % (self))

            self.log.info('%r: Finished gathering FPGA metrics.  It took %.1f seconds to gather those. We now have %i pending metrics.' % (self, time.time()-t0, len(self.metrics)))
            yield sleep(10)

    @coroutine
    @endpoint('get-monitoring-data')
    def get_monitoring_data(self, handler):
        try:
            t0 = time.time()
            # metrics = self.metrics #  Metrics()
            number_of_metrics = len(self.metrics)
            client_ip = handler.request.remote_ip
            self.log.info('%r: Received metrics request from %s' % (self, client_ip))
            if self.last_metrics_client and client_ip != self.last_metrics_client:
                self.log.warn('%r: A new client at %s is pulling metrics from '
                              'this server. Previous client was %s' %
                              (self, client_ip, self.last_metrics_client))
            self.last_metrics_client = client_ip

            handler.set_header('Content-Type', 'text/plain')
            handler.set_header('Content-Encoding', 'gzip')
            handler.write(self.metrics.pop().get_gzip())
            self.log.info('%r: Returning %i FPGA metrics (compression ratio %.0f%%)' %
                (self, number_of_metrics, self.metrics.last_compression_ratio * 100))

            # handler.write(self.metrics.pop().get_gzip())
            self.log.info('%r: Metrics request took %.3f seconds to execute' %
                (self, time.time()-t0))
        except Exception as e:
            self.log.error('%r: Exception in get-monitoring-data. Error is: %r' %
                (self, e))
            raise

    @coroutine
    @endpoint('get-hw-map')
    def get_hw_map(self, handler):
        if self.chime_master.fpgas:
            hwm = {}
            # Get icecrates
            for icecrate in self.chime_master.fpgas.ic:
                hwm['FCC%02d' %icecrate.crate_number] = 'K7BP16-0%s' %icecrate.serial
            # Get iceboards and mezzanines
            for iceboard in self.chime_master.fpgas.ib:
                hwm['FCC%02d%02d' %(iceboard.crate.crate_number, iceboard.slot-1)] = (
                    'MGK7MB-%s' %iceboard.serial.encode('utf-8'),
                    'MGMEZZ-%s' %iceboard.mezzanine.get(1, None).serial,
                    'MGMEZZ-%s' %iceboard.mezzanine.get(2, None).serial
                    )
            coroutine_return(hwm)
        else:
            self.log.info('FPGA array not yet initialized. No info to show.')

    @coroutine
    @endpoint('load-gains')
    def load_gains(self, handler, update_id=None, bank=0, when='now'):
        if self.chime_master and self.chime_master.state == 'on' and self.chime_master.fpgas:
            try:
                uid = yield self.chime_master.load_gains(update_id=update_id, bank=bank, when=when)

            except Exception as exception:
                msg = ('Failed to load digital gains from update_id = %s to bank %d.  Exception: %s' %
                       (update_id, bank, exception))
                self.log.error(msg)
                coroutine_return(msg)

            else:
                msg = 'Loaded digital gains with update_id = %s to bank %d.' % (uid, bank)
                self.log.info(msg)
                coroutine_return(msg)

    @coroutine
    @endpoint('switch-digital-gains')
    def switch_digital_gains(self, handler, delta_t_seconds=100):
        if self.chime_master and self.chime_master.state == 'on' and self.chime_master.fpgas:
            self.chime_master.switch_digital_gains(delta_t_seconds=delta_t_seconds)

    @coroutine
    @endpoint('sync')
    def sync(self, handler):
        if self.chime_master and self.chime_master.state == 'on' and self.chime_master.fpgas:
            self.log.info('%r: received sync() request' % self)
            self.chime_master.fpgas.sync()
            self.log.info('%r: sync() done' % self)

    @coroutine
    @endpoint('set_adc_delays')
    def set_adc_delays(self, handler):
        if self.chime_master and self.chime_master.state == 'on' and self.chime_master.fpgas:
            self.log.info('%r: received set_adc_delays() request' % self)
            yield self.chime_master.fpgas.set_adc_delays.async(**self.chime_master.config.fpga.adc_delay_params)
            self.log.info('%r: set_adc_delays() done' % self)

    @coroutine
    @endpoint('update-channelizers')
    def update_channelizers(self, handler, config='config.yaml:jfc.freq_test'):
        """
        Update channelizers using the parameters in the provided config.fpga.channelizer_params.
        """
        if self.chime_master and self.chime_master.state == 'on' and self.chime_master.fpgas:

            # Load configuration file
            try:
                conf = NameSpace(load_yaml_config(config))
                new_params = conf.fpga.channelizer_params

            except Exception as e:
                msg = 'Could not load fpga.channelizer_params from %s:  %s' % (config, e)
                self.log.error(msg)
                coroutine_return(msg)

            # Update channelizers
            yield self.chime_master.update_channelizers(**new_params)

            coroutine_return('Channelizers updated with configuration: %s' % config)

        else:
            coroutine_return('FPGA array not yet initialized.')

    @coroutine
    @endpoint('set-funcgen-function')
    def set_funcgen_function(self, handler, function='ab', **kwargs):
        """
        Set the function generated by the function generators.
        """
        if not (self.chime_master and self.chime_master.state == 'on' and self.chime_master.fpgas):
            coroutine_return('FPGA array not yet initialized.')

        # Convert unicode to native string using the tornado.escape module
        # to prevent problem writing buffer info
        function_name = native_str(function)
        function_kwargs = {}
        for key, val in kwargs.iteritems():
            function_kwargs[native_str(key)] = native_str(val) if isinstance(val, basestring) else val

        for ib in self.chime_master.fpgas.ib:
            ib.set_funcgen_function(function_name, **function_kwargs)
            yield moment

        coroutine_return('Function generator function set to %s(%r)' % (function_name, function_kwargs))

    @coroutine
    @endpoint('set-gtx-power')
    def set_gtx_power(self, handler, ib_serial=None, power=13, link_type=None):
        """
        Set gtx power of backplane links for a specific fpga motherboard.

        Parameters:
        -----------
        ib_serial (str):  serial number of iceboard
        power (int):  gtx power level. Integer in the range 0-15. The fpga_master default level is 13.
        """
        if self.chime_master and self.chime_master.state == 'on' and self.chime_master.fpgas:
            if ib_serial:
                ib = self.chime_master.fpgas.ib.get(serial=ib_serial)
                ib.BP_SHUFFLE.set_tx_power(int(power), link_type)
                self.log.info('%r: Set gtx power level of FCC%02i%02i (SN%s) to %s.' %
                             (self, ib.crate.crate_number, ib.slot - 1, ib.serial, power))

            else:
                self.chime_master.fpgas.ib.BP_SHUFFLE.set_tx_power(int(power), link_type)
                self.log.info('%r: Set gtx power level to %i of on all Iceboards.' %
                             (self, power))


class ChimeMasterAsyncRESTClient(AsyncRESTClient):

    DEFAULT_PORT = ChimeMasterAsyncRESTServer.DEFAULT_PORT

    def __init__(self, hostname='localhost', port=DEFAULT_PORT):
        super(ChimeMasterAsyncRESTClient, self).__init__(
            hostname=hostname,
            port=port,
            server_class=ChimeMasterAsyncRESTServer,
            heartbeat_string='Cc')

    def print_result(self, d):
        if d == {}:
            print('ok')
        elif 'error' in d:
            print('Error:', d['error'].rstrip())
        else:
            print(json.dumps(d, sort_keys=True, indent=2))

    @coroutine
    def nop(self):
        print('Doing nothing')


    @coroutine
    def raise_exception(self):
        raise RuntimeError('You asked for it') # for debugging

    @coroutine
    def get_methods(self):
        coroutine_return(self.get('methods'))


    @coroutine
    def ping(self):
        """
        Test connection to fpga_master.
        """
        import random
        nonce = random.getrandbits(32)
        r = yield self.post('echo', nonce=nonce)
        if 'nonce' in r and nonce == int(r['nonce']):
            print("ok")
            return
        self.print("internal error!. Server reply was: \n%s" % '\n'.join('%s:%s' % (k,v) for (k,v) in r.items()))

    @coroutine
    def set_state(self, state):
        r = yield self.post('set-state', state=state)
        self.print_result(r)

    @coroutine
    def start(self, config=None):
        """
        Start fpga_master with specified config file.
        """
        if not config:
            raise ValueError('A YAML configuration filename:object must be specified')
        if isinstance(config, str):
            config = load_yaml_config(config.encode('ascii'))
        print('Client start')
        self.log.info('%r: Sending start command to server' % self)
        reply = yield self.post('start', **config)
        self.log.info('%r: Reply to start command is: %r' % (self, reply))
        print('Client started')
        while True:
            try:
                status = yield self.status() # raise exception if start failed

                print('%r:  Current state is: %s, is_ready=%s' % (self, status['state'], status['is_ready']))
                if status['is_ready']:
                    self.log.info('%r: start process is completed' % self)
                    coroutine_return(status['start_result'])
            except RuntimeError as e:
                print('*** %r Client get_status got an exception:%r\n.' % (self, e))
                if 'timeout' not in e.message.lower():
                    status = dict(state='HTTP error')
                    raise e
                print("This is apparently a timout. We'll ignore it...\n")
            self.log.info('%r: Waiting for the START process to complete. Current state is: %s' % (self, status['state']))
            yield sleep(1)

    @coroutine
    def status(self):
        """
        Get fpga_master server status.

        Raises an exception if the start process failed.
        """
        result = yield self.get('status')
        coroutine_return(result)

    @coroutine
    def stop(self):
        """
        Stop fpga_master.
        """
        r = yield self.get('stop')
        self.print_result(r)

    @coroutine
    def switch_gains(self):
        """
        Change gains.
        """
        r = yield self.post('switchgains')
        self.print_result(r)

    @coroutine
    def kotekan_start(self, yaml):
        """
        Start kotekan with specified config file.
        """
        if not yaml:
            raise ValueError('A YAML configuration filename must be specified')
        config = load_yaml_config(yaml.encode('ascii'))
        r = yield self.post('kotekan-start', **config)
        self.print_result(r)

    @coroutine
    def get_frequency_map(self):
        """
        Print fpga_master status.
        """
        m = yield self.get('get-frequency-map')
        self.print_result(m)

    @coroutine
    def get_frame_time(self):
        """
        Get the current frame number and gps time.
        """
        gps_time = yield self.get('get-frame-time')
        coroutine_return(gps_time)

    @coroutine
    def get_frame0_time(self):
        """
        Get the gps time corresponding to frame 0.
        """
        gps_time = yield self.get('get-frame0-time')
        coroutine_return(gps_time)

    @coroutine
    def get_channelizer_output(self):
        """
        Get the channelizer output buffer.
        """
        channelizer_output_buffer = yield self.get('get-channelizer-output')
        coroutine_return(channelizer_output_buffer)

    @coroutine
    def get_hw_map(self):
        """
        Get hardware map
        """
        hwm = yield self.get('get-hw-map')
        # Save hardware map
        time_str = datetime.datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')
        pickle.dump(hwm, open( '/home/chime/ch_acq/%s_hardware_map.pkl' %time_str, 'wb'))
        # Print hardware map
        for key in np.sort(hwm.keys()):
            print('%s: %s' %(key, hwm[key]))
        #coroutine_return(result)

    @coroutine
    def load_gains(self, update_id=None, bank=0, when='now'):
        """
        Load digital gains.
        """
        res = yield self.post('load-gains', update_id=update_id, bank=bank, when=when)
        coroutine_return(res)

    @coroutine
    def switch_digital_gains(self, delta_t_seconds):
        """
        Switch digital gains.
        """
        self.post('switch-digital-gains', delta_t_seconds=float(delta_t_seconds))

def main():
    """ Command-line interface to operate the ChimeMaster server.

    ./fpga_master.py [config] [command {args}] [--host hostname] [--port port_number] [--no-run | --run] [--no-start]

    where:
        *config* : configuration in the format [[*filename*]:][*path_to_config_object*]
        *command* : the name of a ChimeMaster client method.
        --host: hostname of the server. Overrides the hostname found in the config. Default is 'localhost'.
        --port: port number of the server. Overrides the port number found in the config.  Default is 54321.
        --run: run the client/server until Ctrl-C is pressed. Default when no command is provided.
        --no-run: Do not run the client/server even if no comman dis provided.
        --no_start: do not attempt to initialize the server even if a configuration is provided.

    The `fpga_master` command is invoked from the command line with::

        ./fpga_master.py arguments...  # linux only
        python fpga_master.py arguments

    Or from an ipython interactive session::

        run -i fpga_master arguments

    Operations done:

        1. Create client:

            - Always starts a client that connects to server at address specified in config or as
              overriden by --host and --port.

        2. Create server if none already esists:

            - If there is no server, a server is created at localhost on the port specified in the
              config or as overriden by --port, unless -no-server is specified

        3. Initialize server with config file if requested:

            - If no config is present, or if --no-start option is specified, the server is not started
            - If there is a config file, the 'start' command is sent along with the specified
              config. If the server is already started with a different config, an error will be
              raised.

        4. Execute command or run server:

            - If a command and arguments are specified, the corresponding client methods commands
              are invoked. Those generally pass on the command to the corresponding server endpoint.
            - If no command is specified and a local server was started, the client (and locally
              started server if any) are run continually until stopped by Ctrl-C. Bypassed if --no-
              run is specified

    Examples:

    Create and initialize and run a new local server  or initialize an existing server::

        ./fpga_master.py jfc.erh

    Create an non-initialized server

        ./fpga_master.py  # starts server on localhost:54321
        ./fpga_master.py config --no-start # starts server at address specified in config

    Send a command to server:

        ./fpga_master stop # send stop command to server on localhost:54321
        ./fpga_master jfc.erh power_off # power off supplies used by server running at theaddress specified in the jfc.erh config
    """
    # Setup logging
    log.setup_basic_logging('INFO')

    client, server = run_client(sys.argv[1:], ChimeMasterAsyncRESTServer, ChimeMasterAsyncRESTClient, object_name ='ChimeMaster')
    cm = None
    if server and server.chime_master:
        cm = RunSyncWrapper(server.chime_master)
        print("   cm: ChimeMaster object")
    return client, server, cm

if __name__ == '__main__':
    client, server, cm = main()
