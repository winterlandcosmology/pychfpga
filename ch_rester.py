#!/usr/bin/env python

from __future__ import absolute_import, division, print_function

import argparse
import collections
import getpass
import logging
import numpy
import os
import signal
import socket
import struct
import subprocess
import sys
import pickle
import time
import traceback
import yaml
import inspect

import tornado
import tornado.tcpclient
import tornado.web

try:
    import chrx
except ImportError:
    chrx = None
    print('chrx could not be found. Ignoring.')

import pychfpga  # used to access .calculate_gain.
from pychfpga.fpga_array import FPGAArray
from pychfpga.core.icecore import NameSpace

from rest import RESTServer
from kotekan import KotekanRESTClient

# Should put somewhere else. Flatten arbitrarily deep nested lists
# from stack overflow
def flatten(x):
    result = []
    for el in x:
        if hasattr(el, "__iter__") and not isinstance(el, basestring):
            result.extend(flatten(el))
        else:
            result.append(el)
    return result

def convert_types(val):
    # Do the annoying conversion of numpy types to native Python types. Sigh.
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
        if not isinstance(val, str):
            try:
                if val.dtype.kind in ('i', 'u', 'f', 'b'):
                    val = numpy.asscalar(val)
            except:
                    # Hopefully already a int/float
                    pass
    return val


## Setup logging ##

LOG_FORMATTER = logging.Formatter(
    "%(asctime)s %(levelname)s %(filename)s:%(lineno)d >> %(message)s",
    "%b %d %H:%M:%S")
log = logging.getLogger()
log.handlers = []  # clear all existing handlers
log.setLevel(logging.DEBUG) # pass all messages to the handlers
h = logging.StreamHandler(sys.stdout)
h.setFormatter(LOG_FORMATTER)
log.addHandler(h)


## Constants ##

# Current archive format version. Prefixed by "NT_" to signify that
# these data do not have the time-transpose completed.
ARCHIVE_VERSION = "NT_2.2.0"


# Full path to this file.
PROGRAM = os.path.realpath(__file__)

# Git version.
try:
    GIT_VERSION = subprocess.check_output(
        'git describe --all --dirty --long'.split(),
        cwd=os.path.dirname(PROGRAM)).strip()
except WindowsError:
    print('GIT was not found')
    GIT_VERSION = 'unknown' # JFC: To allow tests in windows

SECONDS_PER_FRAME = 2.56e-6

# JFC: moved load_gains as an FPGAArray method





def pass_gains_to_chrx(acq, fpga_gains):
    remap_adc_sma = [12, 13, 14, 15,  8, 9, 10, 11,  4,  5,  6,  7, 0, 1, 2, 3]
    remap_slot    = [ 5,  1,  4,  0, 13, 9, 12,  8, 15, 11, 14, 10, 7, 3, 6, 2]
    for fpga_slot, slot_gain in fpga_gains.items():
        for val in slot_gain:
            v = convert_types(val)
            inp = remap_slot[fpga_slot-1] * 16 + remap_adc_sma[int(val[0])]
            acq.pass_fpga_gain(inp, v)


def reap_cached_sockets():
    import __main__
    if hasattr(__main__, '__opened_sockets__'):
        for port,socket in __main__.__opened_sockets__.items():
            log.debug("closing cached socket on port %d" % port)
            socket.close()
        del __main__.__opened_sockets__


class ChimeMaster(object):
    """ Object that provide methods to initialize, control, monitor and shutdown a CHIME telescope
    array (or subarray)
    """
    def __init__(self):
        self.state = 'off'
        self.config = None
        self.chrx = None  # CHRX REST clients
        self.raw_acq = None # Raw FPGA data acquisitoin REST clients
        self.kotekan = None # Kotekan REST clients
        self.fpgas = None # fpga_array object

    def set_config(self, config):
        self.config = config

    def create_chrx_clients(self):
        # Create CHRX REST clients: These receive the data processed from the GPUs
        conf = NameSpace(self.config)
        self.chrx = []
        for node_name, node_params in conf.acq.nodes.items():
            self.chrx.append(ChrxRESTClient(**node_params))

    def create_kotekan_clients(self):
        # Create Kotekan REST clients
        conf = NameSpace(self.config)
        self.kotekan = []
        for node_name, node_params in conf.gpu.nodes.items():
            self.kotekan.append(KotekanRESTClient(name = node_name, **node_params))

    def create_raw_acq_clients(self):
        # Create CHRX REST clients: These receive the data processed from the GPUs
        conf = NameSpace(self.config)
        self.raw_acq = []
        for node_name, node_params in conf.raw_acq.nodes.items():
            self.raw_acq.append(RawAcqRESTClient(**node_params))


    def set_state(self, new_state):
        """ Sets the state to a specified value. Used for debugging. """
        self.state = new_state

    def start(self, **kvs):
        """ Start the FPGA F-Engine and correlator output acquisition process """
        if self.state != 'off':
            return dict(error='already started')

        self.state = 'starting'
        self.config = kvs
        conf = NameSpace(self.config) # Make the code below cleaner by accessing dict entries as attributes


        # Create output directories
        time_str = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        self.acq_name = "%s_%s_corr" % (time_str, conf.corr_name)
        self.acq_base_dir = os.path.join(conf.acq.base_path, self.acq_name)

        try:
            os.makedirs(self.acq_base_dir)
        except:
            errmsg = "Could not create directory '%s'!" % self.acq_base_dir
            log.critical(errmsg)
            return {'error':errmsg}

        # Create a symbolic link to the output directory.
        if conf.acq.curfile:
            if os.path.islink(conf.acq.curfile):
                os.unlink(conf.acq.curfile)
            os.symlink(self.acq_base_dir, conf.acq.curfile)

        # Start writing to a log file in this directory.
        acq_log_path = "%s/ch_master.log" % self.acq_base_dir
        self.logfile = logging.FileHandler(acq_log_path)
        self.logfile.setFormatter(LOG_FORMATTER)
        log.addHandler(self.logfile)
        log.info("now logging to \"%s\"." % acq_log_path)

        # Now that the housekeeping is done, let's start the real work

        # Create objects to communicates to the remote processes needed to run the array
        self.create_chrx_clients() # CHRX nodes receive data processed by the GPU nodes
        self.create_kotekan_clients() # Kotekan processes run on the GPU nodes; they receive the data from the FPGAs over dedicated point-to-point FPGA-GPU 10G Ethernet links, perform the correlation on the data, and forward the processed data to the CHRX nodes
        self.create_raw_acq_clients() # Raw acq clients receive raw ADC data sent by the FPGA over the control network

        # Initialize FPGAs
        log.info("initializing FPGAs...")
        self.configure_fpgas()
        log.info("finished initializing FPGAs")

        # Read the FPGA setting back from the FPGA
        log.info("getting configuration data from all FPGAs")
        self.fpga_conf = {ib.slot:vars(ib.get_config()) for ib in self.fpgas.ib}


        # Create local CHRX instance
        # In Full CHIME, there will be multiple CHRX instances running on
        # multiple nodes and will be started and configured through a REST interface
        if chrx:
            CRATE_SN = self.fpga.ic[0].get_string_id() # Hack. Works with pathfinder only. Have to rewrite for full CHIME.
            # FPGA housekeeping.
            FPGA_HK_FIELDS = { "core_temp": "deg C" } # To be rewritten with new chrx
            log.info("starting CHRX...")
            self.acq = chrx.acq(conf, log, 16, FPGA_HK_FIELDS)
            headers = self.make_chrx_headers()
            for k,v in headers.items():
                self.acq.add_header_item(k, v)

            self.acq.start(self.acq_base_dir, CRATE_SN, int(conf.fpga.subarray))
            log.info("finished starting CHRX")
        else:
            self.acq = None

        self.configure_fpgas_post_acq()
        self.current_bank = 0

        self.state = 'on'
        return {}
    def configure_fpgas(self):

        # shortcuts
        conf = NameSpace(self.config)
        fpga_array_params = conf.fpga.fpga_array_params

        log.info("Sampling frequency is %0.3f MHz." %
            float(fpga_array_params.samp_freq))

        # Create the FPGAArray object. This object will create a database of all FPGA boards, crates and
        # mezzanines as described by the ``fpga_array_params`` parameters.fpga_array_params If specified
        # in the parameters, the FPGAs will be loaded with their bitstream, communication with the FPGAs
        # will be established and all the Python objects needed to operate the FPGA firmware will be
        # created and initialized.and
        self.fpgas = ca = FPGAArray(**fpga_array_params)

        if not ca.ib:
            raise RuntimeError('No IceBoard could be found. Are the boards powered up? Is the networking functional?')

        # # if this needed?
        # ca.ib.set_adc_mask(0) # null the ADC data before it gets to the channelizers to reduce power consumption

        # Set ADC delays from delay files. Recompute and save new delays if the files do not exist or if
        # the delays loaded from them do not work.
        ca.set_adc_delays(**conf.fpga.adc_delay_params)


        # Reset the correlator. Not sure if this is necesssary?
        ca.ib.set_corr_reset(1)
        time.sleep(0.1)
        ca.ib.set_corr_reset(0)


        # Compute gains if requested
        if conf.fpga.compute_gains.enable:
            self.compute_gains()


        # Set-up channelizers to process data normally
        log.info("Setting-up channelizers")
        ca.set_channelizers(**conf.fpga.channelizer_params)

        # Set-up initial gains in gain bank #0
        if conf.fpga.load_initial_gains:
            log.info("Loading initial scaler gains in bank #0")
            ca.set_synchronized_gain_switching_mode(enable=0)  # Disable synchronized gain switching
            ca.set_next_gain_bank(bank=0)  # immediately select bank zero to load initial gains
            ca.load_gains(bank=0) # load gains from gain files

        # for bankset in ca.ib.get_current_gain_bank():
        #     log.info('Using gain banks %s' % (', '.join([str(i) for i in bankset])))

        if conf.acq.enable_gain_switching:
            # set frame number to switch gains at.
            ca.set_gain_switch_frame_number(frame=0) # XXX:???
            # set to only change when at configured frame number
            ca.set_synchronized_gain_switching_mode(enable=1)
            # set to use bank 1 next, change in loop below.
            # have to do this after config to wait for frame number


        # Is the logging below useful? We just set them...

        # for bankset in ca.ib.get_current_gain_bank():
        #     log.info('Using gain banks %s' % (', '.join([str(i) for i in bankset])))

        # for enabled_sync in ca.ib.get_synchronized_gain_switching():
        #     log.info('Gain sync status is %s' % (', '.join([str(i) for i in enabled_sync])))

        # for frames_set in ca.ib.get_gain_switch_frame_number():
        #     log.info('Gain sync frame is %s' % (', '.join([str(i) for i in frames_set])))

        # log.info("Sending local sync to each board")
        # ca.ib.sync()

        # Setup raw data capture transmission
        self.setup_raw_data_capture()

        # Setup noise injection for normal operation
        self.setup_noise_injection(conf.fpga.noise_injection)


        # Initialize data shufling and transmission to the GPU
        # log.info("Setting FPGA operational mode")
        # ca.set_operational_mode(conf.fpga.operational_mode, frames_per_packet=fpga_array_params.group_frames)

        log.info("Synchronizing the array...")
        ca.sync()  # synchronize all the boards in the array

        # log.info("Unmasking the ADC data")
        # ca.ib.set_adc_mask(0xFF) # restore normal ADC data, necessary anymore?

        log.info("Waiting for 2 seconds")
        time.sleep(2)

    def configure_fpgas_post_acq(self):
        # shortcuts
        ca = self.fpgas
        conf = NameSpace(self.config)

        ca.ib.CROSSBAR.LANE_MONITOR_RESET = 1
        ca.ib.CROSSBAR.LANE_MONITOR_RESET = 0
        ca.ib.CROSSBAR2.LANE_MONITOR_RESET = 1
        ca.ib.CROSSBAR2.LANE_MONITOR_RESET = 0
        ca.ib.CROSSBAR.LANE_MONITOR_SEL = 6
        ca.ib.CROSSBAR2.LANE_MONITOR_SEL = 6

        if conf.acq.enable_gain_switching:
            ca.set_next_gain_bank(bank=1)
        for bankset in ca.ib.get_next_gain_bank():
            log.info('Set next gain bank to %s' % ', '.join([str(i) for i in bankset]))
        for bankset in ca.ib.get_current_gain_bank():
            log.info('Currently using gain banks %s' % ', '.join([str(i) for i in bankset]))


    def setup_noise_injection(self, ni_params):
        """
        Setup the noise gating PWM signals for all the boards specified in `ni_params`.

        `ni_params` is a dictionary containing the parameters passed to the fpga_array's setup_noise_injection() method.

        If no board is specified for an entry (.board evaluates to False), the parameters are ignored.
        """
        for source_name, source_params in ni_params.items():
            log.info("Setting noise injection for source '%s' with parameters %s" % (source_name, source_params))
            if source_params.board:
                ca.setup_noise_injection(local_sync=True, **source_params)

    def setup_raw_data_capture(self):
        # Raw Data capture should be coordinated with the corresponding raw data receiver.
        # Destination IP and port number should be set-up appropriately
        # So for now we'll disable data capture below
        #
        # for ib in ca.ib:
        #     ib.set_local_data_port_number((ib.slot or 1) + 41100)
        #     ib.start_data_capture(period=30, source='adc', offset=(ib.slot or 1) - 1)

        return # bypass code below
        conf = NameSpace(self.config)
        rdc = conf.raw_data_receivers
        for receiver_name, params in rdc:
            (crate, slot) = params.source
            if slot=='*':
                ibs=self.ic.get(crate_number=crate).slot.values()
            else:
                ibs=self.ic.get(crate_number=crate).slot[slot]
            for ib in ibs:
                # should we get the port from the receiver?
                ib.set_data_capture_target(target_ip=params.ip, target_port = params.port) # and MAC address?
                ib.start_data_capture(period=params.period, source=params.source, offset=params.offset)  # offset was (ib.slot or 1) - 1

    def compute_gains(self):
        """
        Compute the gains of the SCALER module so that the conversion of the FFT output to (4+4) bit complex values syays within range for the current signal conditions.

        This method will have to be rewritten to use data obtained over REST-based raw data receivers.
        """

        # shortcuts
        conf = NameSpace(self.config)
        cg = conf.fpga.compute_gains
        if not cg.enable:
            return
        # setup noise injection using noise injection parameters that are specific to the gain calculation operation.
        self.setup_noise_injection(cg.noise_injection)
        for ib in self.fpgas.ib:
            if ib.slot in cg.slots:
                pychfpga.calculate_gains.calculate_gains(ib, str(ib.fpga_port_number + 1)) # use of fixed port numbers is obsolete

    def make_chrx_headers(self):
        # Add some acquisition information to the header, for kicks.
        conf = NameSpace(self.config)
        headers = {
            'acquisition_name': self.acq_name,
            'acquisition_type': 'corr',
            'archive_version': ARCHIVE_VERSION,
            'collection_server': socket.gethostname(),
            'instrument_name': conf.corr_name,
            'git_version_tag': GIT_VERSION,
            'system_user': getpass.getuser(),
        }

        for k in ['notes']:
            if k in conf:
                headers[k] = conf[k]

        # Pass FPGA configuration variables to header.
        for fpga_slot, slot_conf in self.fpga_conf.items():
            for name in slot_conf:
                if name != 'antenna_scaler_gain':
                    val = convert_types(slot_conf[name])
                    name = 'Slot_'+ str(fpga_slot) + '_' + name
                    headers[name] = val
        return headers

    def status(self):
        """ Get the operational status of the telescope as a dictionary"""
        status = dict(state=self.state)
        if self.state == 'on':
            status['config'] = self.config
        return status

    def stop(self):
        """ Stop the F-engine and the correlator data acquisition processes"""
        if self.state == 'on':
            self.state = 'stopping'
            log.info("stopping acquisition")
            self.iceboard_cb.stop()
            if self.acq:
                self.acq.stop()
            log.removeHandler(self.logfile)
            reap_cached_sockets()
            self.state = 'off'
        return {}

    def load_gains(self):
        """ Reload a new set of FPGA F-Engine complex gains from the gain files in the currently unused gain bank"""

        current_bank = self.current_bank
        next_bank = (current_bank + 1) % 2
        iceboards = self.fpgas.ib

        # log current gains
        for bankset in iceboards.get_current_gain_bank():
            log.info('Using gain banks ' + ', '.join(map(str,bankset)))

        # load gains into next bank
        fpga_gains = self.fpga.load_gains(bank=next_bank)
        log.info("Loaded gains into bank %d" % next_bank)

        return fpga_gains

    def set_gain_switch_frame(self):

        iceboards = self.fpgas.ib
        gain_switch_delay = self.config['fpga']['gain_switch_delay']
        gpu_integration_period = self.config['gpu']['gpu_integration_period']

        # set gain switch time
        frame_number = iceboards[0].get_frame_number()
        new_gain_switch_frame = (1 + (frame_number + gain_switch_delay)//gpu_integration_period)*gpu_integration_period
        iceboards.set_gain_switch_frame_number(frame=new_gain_switch_frame)

        sleep = (new_gain_switch_frame - frame_number)*SECONDS_PER_FRAME
        return sleep

    def switch_gain_banks(self):
        current_bank = self.current_bank
        next_bank = (current_bank + 1) % 2
        iceboards = self.fpgas.ib

        self.fpgas.set_next_gain_bank(bank=current_bank)
        self.current_bank = next_bank
        log.debug("changed which gain bank will be written to over to %d"
            % current_bank)

        # log current gains
        for bankset in iceboards.get_current_gain_bank():
            log.info('Using gain banks ' + ', '.join(map(str,bankset)))

    def get_frequency_map(self):
        return self.fpgas.get_frequency_map()


class DummyChimeMaster(ChimeMaster):
    """
    A variant of ChimeMaster that doesn't do anything hardware related.
    """

    def start(self, **kvs):
        self.config = kvs
        return kvs

    def status(self):
        return self.config

    def stop(self):
        return {}


class ChimeMasterApp(RESTServer):
    """ Wraps the ChimeMaster into a REST server which receives HTTP GET or POST requests and calls
    the correspnding ChimeMaster methods.
    """

    def __init__(self, port, dummy=False, gpu_config_file=None):

        self.port = port # port on which the web server will be run
        self.dummy = dummy

        # Create a kotekan client for each node specified in the gpu_config_file
        # We may want to make this part of ChimeMaster initialization
        gpu_config = yaml.load(open(gpu_config_file)) if gpu_config_file else {}
        self.kotekan_clients = [KotekanClient(k, **v) for k,v in gpu_config.items()]

        # Use a dummy CHIME Master object if dummy is True
        ChimeMasterClass = DummyChimeMaster if self.dummy else ChimeMaster

        self.chime_master = ChimeMasterClass()
        super(ChimeMasterApp, self).__init__(port=port)

    def shutdown(self):
        self.chime_master.stop()

    def add_periodic_callbacks(self, period=60):
        # print board info every 60s
        def print_iceboard_info():
            if hasattr(self.chime_master, 'fpgas'):
                self.chime_master.fpgas.print_iceboard_info()
        self.iceboard_cb = tornado.ioloop.PeriodicCallback(print_iceboard_info, period*1000)
        self.iceboard_cb.start()
        return [self.iceboard_cb]

    ##########################
    # Target endpoint methods
    ##########################


    @tornado.gen.coroutine
    @RESTServer.endpoint # must be applied before tornado.gen.coroutine because we lose the method signature
    def echo(self, handler, args):
        handler.write(args)

    @tornado.gen.coroutine
    @RESTServer.endpoint
    def set_state(self, handler, args):
        self.chime_master.set_state(args['state'])
        handler.write(args)

    @tornado.gen.coroutine
    @RESTServer.endpoint
    def start(self, handler, args):
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
        config = encode_utf8(args)  # convert all strings in the config dict into utf8
        handler.write(self.chime_master.start(**config))

    @tornado.gen.coroutine
    @RESTServer.endpoint
    def methods(self, handler):
        handler.write(dict(results=self._endpoint_info))

    @tornado.gen.coroutine
    @RESTServer.endpoint
    def status(self, handler):
        handler.write(self.chime_master.status())

    @tornado.gen.coroutine
    @RESTServer.endpoint
    def stop(self, handler):
        handler.write(self.chime_master.stop())

    @tornado.gen.coroutine
    @RESTServer.endpoint
    def switch_gains(self, handler):
        if self.chime_master.state != 'on':
            handler.write(dict(error='not started'))
            return

        fpga_gains = self.chime_master.load_gains()
        sleep = self.chime_master.set_gain_switch_frame()

        # wait for switch
        yield tornado.gen.sleep(sleep)
        pass_gains_to_chrx(self.chime_master.acq, fpga_gains)

        # wait for 10 secs, then switch banks
        yield tornado.gen.sleep(10)
        self.chime_master.switch_gain_banks()
        self.write({})

    @tornado.gen.coroutine
    @RESTServer.endpoint
    def kotekan_start(self, handler, args):
        results = yield [k.start(args) for k in self.kotekan_clients]
        handler.write(dict(results=results))

    @tornado.gen.coroutine
    @RESTServer.endpoint
    def get_frequency_map(self, handler):
        handler.write(dict(results=self.chime_master.get_frequency_map()))

    @tornado.gen.coroutine
    @RESTServer.endpoint
    def abort(self, handler):
        """ Savagely stop the server for debugging purposes."""
        handler.write(dict(results='ABORTING NOW!'))
        tornado.ioloop.IOLoop.instance().stop()
        # sys.exit(-1)

    @tornado.gen.coroutine
    @RESTServer.endpoint
    def call_fpga_array_method(self, handler, args):
        """  For debuging: calls any fpga_array method. """
        if not hasattr(self.chime_master, 'fpgas') or not self.chime_master.fpgas:
            handler.write(dict(error='FPGA array is not created yet'))
            return
        r=getattr(self.chime_master.fpgas, args['method_name'])(**args['kwargs'])
        handler.write(dict(results=self.sanitize_for_json(r)))

    def sanitize_for_json(self, obj):
        """
        Modify an object to make it JSON-compatible. Contents of dicts and
        lists contained in the object are recursively converted.

            - dict-like object with string keys are converted to Python dict
            - dict-like objects witn non-string keys are converted into a Python list of (key,value) tuple.
            - list objects are converted into Python list
            - other objects stay the same.

        """
        if isinstance(obj, collections.Mapping) or hasattr(obj, 'items'):
            if not all(isinstance(k,str) for k in obj.keys()):
                return [(k, self.sanitize_for_json(v)) for k, v in obj.items()]
            else:
                return {str(k): self.sanitize_for_json(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [self.sanitize_for_json(v) for v in obj]
        else:
            return obj




def parse_cmdline_args(argv):
    parser = argparse.ArgumentParser(description="Chime Master")
    parser.add_argument('-d', '--debug', action='store_true',
                        help="debug mode")
    parser.add_argument('-g', '--gpus', default=None, type=str)
    parser.add_argument('-p', '--port', default=54321, type=int)
    return parser.parse_args(argv)


def main(args):
    """
    Create the Tornado IOLoop and run the ChimeMasterApp web server that will answer the HTTP
    commands and operate the ChimeMaster instance.
    """

    log.info("program %s" % PROGRAM)
    log.info("version %s" % GIT_VERSION)

    # create event loop
    loop = tornado.ioloop.IOLoop.instance()

    # Create a web server that will provide REST endpoints that connect to the ChimeMaster methods
    app = ChimeMasterApp(port=args.port, dummy=args.debug, gpu_config_file=args.gpus)

    app.add_periodic_callbacks()

    # Start heartbeat. Shows then the IO loop is running and allows keyboard events (Ctrl-C) to be captured (for some reason).
    def heartbeat():
        print('.', end='')
    tornado.ioloop.PeriodicCallback(heartbeat, 1000).start()


    def shutdown():
        print("Received SHUTDOWN signal")
        app.shutdown()
        loop.stop()
        sys.exit(-1)

    handler = lambda sig,frame: loop.add_callback_from_signal(shutdown)
    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)



    # start event loop
    log.info("ready")
    loop.start()


if __name__ == '__main__':
    args = parse_cmdline_args(sys.argv[1:])
    main(args)

