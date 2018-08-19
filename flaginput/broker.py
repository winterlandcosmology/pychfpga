#!/usr/bin/env python
"""
REST server and client for flagging bad correlator inputs.
"""
import os
import sys
import glob
import shutil
import json
import Queue
import concurrent.futures
import datetime
import time

import tornado.ioloop
import tornado.escape

import numpy as np
import h5py
import log
import re

import flag_raw

from pychfpga import NameSpace, Metrics, load_yaml_config
from pychfpga import OrderedSetLifoQueue, OrderedSetFifoQueue
from rest import AsyncRESTClient, AsyncRESTServer
from rest import coroutine, coroutine_return, endpoint, run_client

from ch_util import tools, layout, ephemeris

from version import __version__
from containers import mkdir, FlagCorrInputArchive, FlagRawWriter, ControlFlag, RateFlag

LOG_FILE = os.environ.get('FLAGINPUT_LOG_FILE',
           os.path.join(os.path.dirname(os.path.realpath(__file__)), 'broker.log'))

DEFAULTS = NameSpace(load_yaml_config(os.path.join(os.path.dirname(
                                                   os.path.realpath(__file__)),
                                                   'defaults.yaml') + ':flaginput'))


###################################################
# ancillary functions
###################################################

def format_filename(filename):
    """ Extract the unique portion of the acquisition filename.
    """

    return os.path.join(os.path.basename(os.path.dirname(filename)),
                        os.path.basename(filename))

def filename_to_datetime(filename):
    """ Determines the datetime from the acquistion filename.
    """

    acq_date = ephemeris.timestr_to_datetime(os.path.basename(os.path.dirname(filename)))
    file_date = acq_date + datetime.timedelta(seconds=int(os.path.splitext(os.path.basename(filename))[0]))

    return file_date

def ensure_unix(val):
    """ Convert an input to unix time based on the input's datatype.
    """

    if isinstance(val, (float, int, long)):
        return val

    elif isinstance(val, basestring):
        return ephemeris.datetime_to_unix(ephemeris.timestr_to_datetime(val))

    elif isinstance(val, datetime.datetime):
        return ephemeris.datetime_to_unix(val)

    elif isinstance(val, dict):
        return ephemeris.datetime_to_unix(datetime.datetime(**val))

    elif isinstance(val, tuple) or isinstance(val, list):
        return ephemeris.datetime_to_unix(datetime.datetime(*val))

    else:
        ValueError("Do not recognize %.32r as time." % type(val))

def check_manual_flag(inp):

    return (inp.flag if hasattr(inp, 'flag') else True)

def wrapper_chime_input_labels(inputmap):

    def chime_input_labels(inputs):

        ninput = inputs.size

        # Initiate arrays to hold labels
        label_map = {}
        label_map['correlator_input'] = np.zeros(ninput, dtype='S32')
        label_map['bulkhead_position'] = np.zeros(ninput, dtype='S32')
        for key in ['chan_id', 'crate', 'slot', 'input']:
            label_map[key] = np.zeros(ninput, dtype=np.int)

        # Loop over inputs and extract labels
        for ii, inp in enumerate(inputs):

            mo = re.match('FCC(\d{2})(\d{2})(\d{2})', inp['correlator_input'])

            if mo is None:
                raise RuntimeError('Serial number %s does not match expected CHIME format.' % inp['correlator_input'])

            label_map['correlator_input'][ii] = inp['correlator_input']
            label_map['chan_id'][ii] = inp['chan_id']
            label_map['crate'][ii] = int(mo.group(1))
            label_map['slot'][ii]  = int(mo.group(2))
            label_map['input'][ii] = int(mo.group(3))

            label_map['bulkhead_position'][ii] = 'null'
            rf_thru = getattr(inputmap[inp['chan_id']], 'rf_thru', None)
            if rf_thru is not None:
                mo = re.match('BKR0(\w{1})C_(\w{3})', rf_thru)
                if mo is not None:
                    label_map['bulkhead_position'][ii] = '_'.join([mo.group(1), mo.group(2)])

        return label_map

    return chime_input_labels

def estimate_directional_scale(z, c=2.1):

    zmed = np.median(z)

    x = z - zmed

    xa = x[x <= 0.0]
    xb = x[x >= 0.0]

    def huber_rho(dx, c=2.1):

        num = float(dx.size)

        s0 = 1.4826 * np.median(np.abs(dx))

        dx_sig0 =  dx * tools.invert_no_zero(s0)

        rho = (dx_sig0 / c)**2
        rho[rho > 1.0] = 1.0

        return 1.54 * s0 * np.sqrt(2.0 * np.sum(rho) / num)

    sa = huber_rho(xa, c=c)
    sb = huber_rho(xb, c=c)

    return zmed, sa, sb

###################################################
# primary class
###################################################

class FlagCorrInput(object):
    """ Manages flagging of bad correlator inputs.  Specifically:

        - layout:  Queries layout database to flag inputs not connected to CHIME antennas.

        - power:  Queries bulkhead servers to flag inputs whose FLAs are not currently powered.

        - rms:  Queries raw acquisition server to flag inputs with low or high RMS.

        - raw:  Processes raw ADC acquisition files to flag inputs with anomalous histogram or spectrum.

    The user can specify which of these sources to use through the "sources" parameter.
    When flags from one of the user-requested sources changes, the current flags from all sources
    are appended to the end of an HDF5 file and also added to Grafana metrics.  The master flag
    obtained from the AND of all user-requested source flags is also passed on to ch_master for further
    distribution to relevant consumer processes.
    """

    _update_type = 'flaginput'
    _potential_sources = ['layout', 'manual', 'power', 'rms', 'rfi', 'raw']

    def __init__(self, **config):
        """ Instantiates a FlagCorrInput object.

        Parameters are passed through a config (hierarchical dictionary).
        See ch_acq/flaginput/defaults.yaml for their default values.

        Parameters
        ----------
        sources:  list of str
            List indicating which of the _potential_sources to collect flags from.

        output_dir:  str
            Directory where the output hdf5 files will be saved.

        output_suffix: str
            Suffix appended to the output hdf5 filenames.

        correlator: str
            Name of the correlator.  Used to query layout database and included in
            name of output hdf5 files.

        max_file_size:  int
            Maximum file size in bits.

        max_num_time: int
            Maximum number of time samples to include in single file.  The file is
            closed and a new one started when the file size is greater than max_file_size
            or the number of samples is greater than max_num_time, whichever comes first.

        always_return_metrics:  bool
            Return metrics every time they are requested.  Otherwise return metrics
            only when they have been updated.

        max_threadpool_workers:  str
            Maximum number of threads to use for asynchronous computations.

        sources_in_buffer: bool
            Update the file buffer when the flag for any source changes.
            Otherwise will update the file buffer only when the combined flag changes.

        consumers: list
            Any times the flags change they will be POSTed the consumers.
            Each element of the list should be a dictionary of the format:
                {hostname: 'csBfs', port: 54323}

        power: dict
            Config parameters related to the "power" source.

            - clients: list
                Parameters for connecting to the the FLA power REST servers.
                Each element of the list should be a dictionary of the format:
                    {hostname: 'hk-west', port: 5000, bulkhead: ['A', 'B']}

        rms: dict
            Config parameters related to the "rms" source.

            - clients:
                Parameters for connecting to the the raw acquisition REST servers.
                Each element of the list should be a dictionary of the format:
                    {hostname: 'carillon', port: 33221}

            - lower_limit: int
                Flag inputs as bad if their RMS power in LSB is below this number.

            - upper_limit: int
                Flag inputs as bad if their RMS power in LSB is above this number.

            - nsigma: float
                Flag inputs as bad if their RMS power is this number of sigma away
                from median of distribution of values over inputs.

            - group_by_cyl: bool
                Calculate median and sigma independently for each cylinder.

            - group_by_pol: bool
                Calculate median and sigma independently for each polarisation.

            - number_consecutive_good: int
                Number of consecutive samples that must be good in order for a
                RMS derived flag to change from bad to good.

            - number_consecutive_bad: int
                Number of consecutive samples that must be bad in order for a
                RMS derived flag to change from good to bad.

            - max_rate: float
                Maximum number of flag changes per hour beyond which an input
                is considered bad.

            - trial_period: float
                Do not apply max_rate test until after this many hours.

        rfi: dict
            Config parameters related to the "rfi" source.

            - clients:
                Parameters for connecting to the the raw acquisition REST servers.
                Each element of the list should be a dictionary of the format:
                    {hostname: 'carillon', port: 7342}

            - threshold: float
                Inputs that have a bad input likelihood determined by the rfi monitor
                to be greater than or equal to threshold (in percentage) will be
                flagged as bad.

        raw: dict
            Config parameters related to the "raw" source.

            - start_time:  unix time
                Process raw ADC acquisitions after this time.
                (None will use current time. 0 will process all files in archive.)

            - lifo:  bool
                Process new raw ADC acquisitions first.

            - acq_dir:  str
                Directory containing raw ADC acquisitions.

            - acq_suffix: str
                Suffix appended to the raw ADC acquisitions.

            - output_dir:  str
                Directory where the ancillary data products related to the
                raw aquisition analysis are saved.

            - output_suffix: str
                Suffix appended to the hdf5 filenames.

            - number_consecutive_good: int
                Number of consecutive samples that must be good in order for a
                raw derived flag to change from bad to good.

            - number_consecutive_bad: int
                Number of consecutive samples that must be bad in order for a
                raw derived flag to change from good to bad.
        """

        # Save configuration parameters
        self.config = DEFAULTS.deepcopy()
        self.config.merge(NameSpace(config))

        start_time = self.config.raw.start_time
        if start_time is None:
            start_time = time.time()
        self.search_time = ensure_unix(start_time)

        if self.config.sources is None:
            self.sources = self._potential_sources
        else:
            self.sources = [ss for ss in self._potential_sources if ss in self.config.sources]

        # Determine what sources we will combine for the master flag
        combine = self.config.combine if self.config.combine is not None else self.sources
        self.icombine = np.array([ii for ii, ss in enumerate(self.sources) if ss in combine])

        # Setup logger
        self.log = log.get_logger(self)

        # Set niceness (Unix only)
        try:
            sys.getwindowsversion()
        except AttributeError:
            niceness = os.nice(0)
            os.nice(self.config.niceness - niceness)
            self.log.info('Changing process niceness from %d to %d.' %
                          (niceness,  self.config.niceness))

        # Keep track of when each of the sources was last updated
        self.update_time = {ss:None for ss in self.sources + ['combined']}
        self.update_time['metrics'] = time.time()

        self.update_id = {ss:None for ss in self.sources + ['combined']}

        # Create pool of threads for asynchronous computations
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=self.config.max_threadpool_workers)
        self.futures = {ss:None for ss in self.sources}

        # Create hdf5 reader/writer for flags
        output_files = self.find_files(raw=False)
        if output_files is not None:
            self.log.info('Writing flags to existing file %s.' % format_filename(output_files[-1]))

        self.h5_flag = FlagCorrInputArchive(output_dir=self.config.output_dir, output_suffix=self.config.output_suffix,
                                            instrument=self.config.correlator, combine=np.array(self.sources)[self.icombine],
                                            archive_files=output_files, max_file_size=self.config.max_file_size,
                                            max_num=self.config.max_num_time, memory_size=self.config.memory_size)

        # Define buffer file
        self.buffer_file = os.path.join(self.config.output_dir, self.config.output_suffix + '_buffer.h5')

        # Query the layout database to obtain list of current correlator inputs
        self._query_layout_database(update=False)

        # Create internal variable for holding current flags.
        # If possible, pick up where we left off.
        if self.h5_flag:

            lastup = self.h5_flag.last_update
            lastupstr = datetime.datetime.utcfromtimestamp(lastup).strftime('%Y%m%dT%H%M%SZ')

            self.log.info('Using flags from %s.' % lastupstr)
            for ss in self.sources + ['combined']:
                self.update_time[ss] = lastup

            idd = self.h5_flag[lastup]
            self.source_flags = np.copy(self.h5_flag.read(idd, 'source_flags'))

            self.current_update_id = self.h5_flag.read(idd, 'update_id')
            for ss in self.sources + ['combined']:
                self.update_id[ss] = self.current_update_id

        else:
            self.log.info('No prior flags available.  Setting all correlator inputs to good.')
            self.source_flags = np.ones((len(self.sources), self.ninput), dtype=np.bool)
            self.current_update_id = None

        # Determine historically bad inputs
        self._update_historical_status()

        # Set up clients to communicate with FLA power server
        self.power_clients = []
        if 'power' in self.sources:

            for config in self.config.power.clients:
                self.power_clients.append(PowerAsyncRESTClient(**config))

        # Set up client to communicate with raw acquisition server
        self.control = {}
        self.rms_clients = []
        if 'rms' in self.sources:

            # Create control flags
            self.control['rms'] = RateFlag(num_consecutive_bad=self.config.rms.num_consecutive_bad,
                                           num_consecutive_good=self.config.rms.num_consecutive_good,
                                           max_rate=self.config.rms.max_rate,
                                           trial_period=self.config.rms.trial_period)
            self.control['rms'].update(self.source_flags[self.sources.index('rms')])

            # Define groups of inputs that should have
            # approximately the same rms at any time
            temp_id = ['%s - %s' % (self.config.rms.group_by_cyl and inp.cyl, self.config.rms.group_by_pol and inp.pol)
                        if tools.is_chime(inp) and (inp.cyl is not None) and (inp.pol is not None) else None
                        for ii, inp in enumerate(self._input)]

            rms_grp, rms_igrp = np.unique(temp_id, return_inverse=True)

            self._rms_ngroup = rms_grp.size
            self._rms_group = [np.flatnonzero(rms_igrp == gg) for gg in range(self._rms_ngroup)]
            self._rms_group_membership = rms_igrp

            rms_grp = list(rms_grp)
            if None in rms_grp:
                self._rms_group_null = [(rms_grp.index(None), [gg for gg, grp in enumerate(rms_grp) if grp is not None])]
            else:
                self._rms_group_null = []

            # Create clients
            for config in self.config.rms.clients:
                self.rms_clients.append(RmsAsyncRESTClient(**config))

            # Set the time ranges where we ignore anomalous rms
            self.update_rms_excludes()

        # Set up client to communicate with rfi server
        self.rfi_clients = []
        if 'rfi' in self.sources:

            if len(self.config.rfi.clients) > 1:
                ValueError("Only 1 rfi client allowed.")

            for config in self.config.rfi.clients:
                self.rfi_clients.append(RFIMonitorRESTClient(**config))

        # Set up processing of raw ADC files
        self.h5_raw = False
        if 'raw' in self.sources:

            # Create queue for raw acquisition files that need to be analyzed
            self.file_queue = OrderedSetLifoQueue(maxsize=0) if self.config.raw.lifo else OrderedSetFifoQueue(maxsize=0)

            # Check if any output files already exist
            output_files = self.find_files(raw=True)
            if output_files is not None:
                output_files = output_files[-1]
                self.log.info('Writing raw data products to existing file %s.' % format_filename(output_files))

            # Create separate entry in update_time dictionary for raw data products
            self.update_time['raw_data'] = None

            # Create hdf5 reader/writer for analysis of raw adc data
            self.h5_raw = FlagRawWriter(output_dir=self.config.raw.output_dir, output_suffix=self.config.raw.output_suffix,
                                         instrument=self.config.correlator, output_file=output_files,
                                         max_file_size=self.config.raw.max_file_size, max_num=self.config.raw.max_num_time,
                                         memory_size=self.config.raw.memory_size)

            # Create control flags
            self.control['raw'] = ControlFlag(num_consecutive_bad=self.config.raw.num_consecutive_bad,
                                              num_consecutive_good=self.config.raw.num_consecutive_good)
            self.control['raw'].update(self.source_flags[self.sources.index('raw')])

            # Start processing the raw adc files on a separate thread from the pool
            self.futures['raw'] = self.executor.submit(self.process_raw_adc_files)

        # Create clients to communicate with consumer of input flags
        self.consumers = []
        if self.config.consumers is not None:
            for config in self.config.consumers:
                self.consumers.append(KotekanRESTClient(**config))

    # -------------------------------
    # layout
    # -------------------------------

    def query_layout_database(self):
        """ Spawn a thread that queries the layout database.
        """

        if 'layout' in self.sources:
            if (self.futures['layout'] is not None) and not self.futures['layout'][1].done():
                self.futures['layout'][1].cancel()

            timestamp = time.time()
            self.futures['layout'] = (timestamp, self.executor.submit(self._query_layout_database, timestamp=timestamp))


    def _query_layout_database(self, timestamp=None, update=True):
        """ Query the layout database and save the list of CHIMEAntennas to self._input.

        Parameters
        ----------
        timestamp: unix time

        update: bool
            If True, use tools.is_chime_on to update the layout flags.
            Default is True.
        """

        if timestamp is None:
            timestamp = time.time()

        utc_time = datetime.datetime.utcfromtimestamp(timestamp)

        self.log.info('Querying layout database for time %s.' % utc_time.strftime("%Y-%m-%d %H:%M:%S"))

        layout.connect_database(read_write=False, reconnect=True)
        layout.set_user('Siegel')

        # Query layout database
        inp = tools.get_correlator_inputs(utc_time, correlator=self.config.correlator)

        # Make sure the database query did not take much longer than expected
        if (self.futures['layout'] is not None) and (self.futures['layout'][0] > timestamp):
            self.log.info('Layout database query timed out for time %s.' % utc_time.strftime("%Y-%m-%d %H:%M:%S"))
            return
        else:
            self.log.info('Layout database query successful for time %s.' % utc_time.strftime("%Y-%m-%d %H:%M:%S"))

        # Update class _input variable
        self._input = inp

        # Update flags
        if update:
            tests = {}
            tests['layout'] = np.array(tools.is_chime_on(self._input))

            if 'manual' in self.sources:
                tests['manual'] = np.array([check_manual_flag(inp) for inp in self._input])

            self.update_flags(**tests)


    # -------------------------------
    # power
    # -------------------------------

    @coroutine
    def query_fla_power_server(self):
        """ Query the FLA power servers and update the power flags.
        """

        if 'power' in self.sources:

            isource = self.sources.index('power')

            powered = []
            for client in self.power_clients:

                yield client.get_status()

                powered += client.powered.items()

            powered = dict(powered)

            flag = np.array([powered[inp.input_sn][1] if inp.input_sn in powered else
                            self.source_flags[isource, ii]
                            for ii, inp in enumerate(self._input)])

            self.update_flags(power=flag)


    # -------------------------------
    # rms
    # -------------------------------

    def rms_bounds(self, rms):

        lower = [self.config.rms.lower_limit] * self._rms_ngroup
        upper = [self.config.rms.upper_limit] * self._rms_ngroup

        current_flag = self.flag

        for gg, gindex in enumerate(self._rms_group):

            good_rms = [rms[self._input[ii].input_sn] for ii in gindex if current_flag[ii]]

            if good_rms:

                med, slow, shigh = estimate_directional_scale(good_rms)

                lower[gg] = max(lower[gg], med - self.config.rms.nsigma * slow)
                upper[gg] = min(upper[gg], med + self.config.rms.nsigma * shigh)

        for inull, igood in self._rms_group_null:
            lower[inull] = np.mean([lower[gg] for gg in igood])
            upper[inull] = np.mean([upper[gg] for gg in igood])

        return lower, upper

    @coroutine
    def query_rms_server(self):
        """ Query the raw acquisition servers and update the rms flags.
        """

        if 'rms' in self.sources:

            current_time = time.time()
            if any([np.abs(current_time - texc) < win for texc, win in self.rms_excludes]):
                self.log.info('Near transit.  Ignoring rms values.')
                return

            isource = self.sources.index('rms')

            rms = []
            for client in self.rms_clients:

                yield client.get_status()

                rms += client.rms.items()

            rms = dict(rms)

            lower, upper = self.rms_bounds(rms)

            flag = np.array([((rms[inp.input_sn] >= lower[self._rms_group_membership[ii]]) and
                              (rms[inp.input_sn] <= upper[self._rms_group_membership[ii]]))
                              if inp.input_sn in rms else self.source_flags[isource, ii]
                              for ii, inp in enumerate(self._input)])

            self.control['rms'].update(flag)

            self.update_flags(rms=self.control['rms'].flag)

            self.log.info('It took %s seconds to update rms.' % (time.time() - current_time,))


    def update_rms_excludes(self):

        self.rms_excludes = []
        if ('rms' in self.sources) and ('rms_excludes' in self.config.periodic_callbacks):

            now = time.time()
            cadence = self.config.rms_excludes.cadence
            excludes = self.config.rms_excludes.excludes

            if excludes:
                for src, win in excludes.iteritems():

                    if src.lower() == 'sun':
                        self.rms_excludes += [(tt, win) for tt in ephemeris.solar_transit(now - win, now + 2*cadence)]
                    else:
                        obj = ephemeris.source_dictionary[src]
                        self.rms_excludes += [(tt, win) for tt in ephemeris.transit_times(obj, now - win, now + 2*cadence)]


    # -------------------------------
    # rfi
    # -------------------------------

    @coroutine
    def query_rfi_server(self):
        """ Query the RFI servers and update the rfi flags.
        """

        if 'rfi' in self.sources:

            current_time = time.time()

            isource = self.sources.index('rfi')

            likelihood = []
            for client in self.rfi_clients:

                yield client.update_rfi_bad_input_mask()

                likelihood += client.bad_input_likelihood

            if len(likelihood) == self.ninput:

                flag = np.array([likelihood[inp.corr_order] < self.config.rfi.threshold for inp in self._input])

                self.update_flags(rfi=flag)

                self.log.info('It took %s seconds to update rfi.' % (time.time() - current_time,))


    # -------------------------------
    # raw
    # -------------------------------

    def get_raw_adc_files(self):
        """ Find new raw acquisition files in the archive and add them to self.file_queue.
        """

        if 'raw' in self.sources:

            # Find all raw acquisition files in raw_acq_dir
            all_files = sorted(glob.glob(os.path.join(self.config.raw.acq_dir, '*' + self.config.correlator + '_' +
                                                                                     self.config.raw.acq_suffix, '*.h5')))
            if not all_files:
                return

            # Remove files whose last modified time is before the time of the most recent update
            all_files = [ff for ff in all_files if (os.path.getmtime(ff) > self.search_time)]
            #all_files = [ff for ff in all_files if (ensure_unix(filename_to_datetime(ff)) > self.search_time)]
            if not all_files:
                return

            # Remove files that have already been analyzed
            if self.h5_raw:
                all_files = [ff for ff in all_files if format_filename(ff) not in self.h5_raw]
                if not all_files:
                    return

            # Remove files that are currently locked
            all_files = [ff for ff in all_files if not os.path.isfile(os.path.splitext(ff)[0] + '.lock')]
            if not all_files:
                return

            # Add new files in the queue
            self.log.info('Adding %d files to queue.' % len(all_files))
            for ff in all_files:
                self.file_queue.put(ff, block=False)

            # Update the time
            self.search_time = time.time()


    def process_raw_adc_files(self):
        """ Process raw acquisition files in self.file_queue.
        """

        while 'raw' in self.sources:

            try:
                my_file = self.file_queue.get(block=False)
            except Queue.Empty:
                time.sleep(self.config.raw.cadence)
                continue

            try:
                # Call the main routine to process data
                self.log.info('Processing file %s' % my_file)

                try:
                    outcls = flag_raw.main(my_file, output_csv=False, output_plot=False, output_tex=False, compute_template=True)
                except Exception as e:
                    self.log.error("flag_raw.main failed with error:  %s" % e)
                    raise

                self.log.info('Finished analysis of file %s' % my_file)

                # Make sure that the server was not stopped while processing file
                if not self.h5_raw.iam:
                    self.log.error("HDF5 file closed while processing file %s" % my_file)
                    raise RuntimeError

                # Extract the time associated with this file
                with h5py.File(my_file, 'r') as hf:
                    this_time = 0.5 * (hf['timestamp']['ctime'][0, 0] + hf['timestamp']['ctime'][-1, 0])

                # Grab correlator input ordering from most recent layout database query
                input_axis = np.array(self.input, dtype=[('chan_id', 'u2'), ('correlator_input', 'S32')])

                # Save results to dictionary.  First define axes.
                self.log.info('Reordering results from file %s' % my_file)
                res = dict()
                res['input'] = input_axis

                for axis in self.h5_raw._axes:
                    if (axis not in ['input', self.h5_raw._grow_ax]) and (axis in outcls.__dict__):
                        res[axis] = outcls.__dict__[axis]

                # Remove inputs with no data
                input_axis = np.array([(idd, sn) for idd, sn in input_axis if sn not in outcls.missing_channel],
                                        dtype=[('chan_id', 'u2'), ('correlator_input', 'S32')])

                # Save datasets to dictionary, ordered using the cylinder based scheme.
                for dset in self.h5_raw._dataset_spec:
                    if dset in outcls.__dict__:

                        # Extract the specifications for this dataset
                        dspec = self.h5_raw._dataset_spec[dset]
                        dtype = dspec['dtype']
                        axes = [ax for ax in dspec['axes'] if ax != self.h5_raw._grow_ax]

                        # Check if dataset has input axis to reorder
                        if 'input' in axes:
                            shp = tuple(res[axis].size for axis in axes)
                            default = True if dtype is np.bool else np.nan if np.issubdtype(dtype, np.floating) else 0

                            res[dset] = np.full(shp, default, dtype=dtype)
                            for ind, key in input_axis:
                                try:
                                    res[dset][ind] = outcls.__dict__[dset][key]
                                except KeyError:
                                    pass
                        else:
                            res[dset] = outcls.__dict__[dset]

                # Add filename to results
                res['filename'] = format_filename(my_file)

                # Write to output file
                self.log.info('Writing to disk results from file %s' % my_file)
                self.h5_raw.write(this_time, **res)

                # Update time
                self.update_time['raw_data'] = time.time()

                # Update flags
                self.log.info('Updating raw flags from file %s' % my_file)
                isource = self.sources.index('raw')
                flag = np.array([classification > self.config.raw.classification_threshold if not np.isnan(classification)
                                 else self.source_flags[isource, ii]
                                 for ii, classification in enumerate(res['classification'])])

                self.control['raw'].update(flag)
                self.update_flags(raw=self.control['raw'].flag)

            finally:
                self.file_queue.task_done()
                self.log.info('Finished processing file %s' % my_file)


    def get_raw_metrics(self):
        """ Spawn a thread that returns the current metrics
        from the raw analysis.
        """

        timestamp = time.time()

        if self.config.correlator.lower() in ['chime', 'fcc']:
            metric_labels = {'input':wrapper_chime_input_labels(self._input)}
        else:
            metric_labels = {}

        return self.executor.submit(self.h5_raw.get_metrics, timestamp, **metric_labels)


    # -------------------------------
    # ancillary methods
    # -------------------------------

    def find_files(self, raw=False):
        """ Find HDF5 archive files on disk.

        The directory and filename suffix are specified in
        the configuration file (self.config).

        Parameters
        ----------
        raw: bool
            If True, find files containing results from the
            analysis of raw acquisition data, using the parameters
            specificed in self.config.raw.  If False, find files
            containing correlator input flags, using the parameters
            specified in self.config.  Default is False.
        """

        if raw:
            output_dir = self.config.raw.output_dir
            output_suffix = self.config.raw.output_suffix
        else:
            output_dir = self.config.output_dir
            output_suffix = self.config.output_suffix

        candidate_files = sorted(glob.glob(os.path.join(output_dir, '*' + output_suffix, '*.h5')))

        output_files = []
        for cf in candidate_files:

            with h5py.File(cf, 'r') as hf:

                file_version = hf.attrs.get('version', None)
                instrument = hf.attrs.get('instrument_name', None)

                valid = (file_version == __version__) and (instrument == self.config.correlator)

                if not raw:
                    sources = hf['index_map']['source'][:]
                    nsources = len(sources)

                    valid = (valid and (nsources == len(self.sources)) and
                             all([sources[ii] == self.sources[ii] for ii in range(nsources)]))

                if valid:
                     output_files.append(cf)

        return output_files or None


    def update_flags(self, **kwargs):
        """ Update internal flags.

        Provide keyword arguments of the form source=flag.
        Can handle multiple sources.

        Parameters
        ----------
        source: np.array of bool
            ninput long boolean vector indicating whether
            each input is good (True) or bad (False) as
            determined by this particular source.
        """

        # Extract current combined flag to compare
        # with updated combined flag later
        combined_flag = np.array(self.flag)

        # Loop over sources in kwargs
        flags_were_updated = []
        for source, flag in kwargs.iteritems():

            isource = self.sources.index(source)

            # Only take action if flags are different from the current flags
            if np.any(self.source_flags[isource] != np.array(flag)):

                # Save to class variable
                self.source_flags[isource] = flag
                flags_were_updated.append(source)


        # If the flags changed, then make the relevant updates
        if flags_were_updated:

            this_time = time.time()

            update_id = '_'.join([self._update_type, datetime.datetime.utcfromtimestamp(this_time).strftime("%Y%m%dT%H%M%S.%fZ")] +
                            flags_were_updated)

            self.current_update_id = update_id

            new_combined_flag = self.flag
            # If the combined flags changed, then post to relevant consumer processes
            if np.any(combined_flag != np.array(new_combined_flag)):

                payload = self.create_payload(update_id, this_time + self.config.sync_delay, new_combined_flag)

                for client in self.consumers:
                    client.update_bad_inputs(**payload)

                # Save current time if the combined flags changed
                self.update_time['combined'] = this_time
                self.update_id['combined'] = update_id


            # Update time
            for ss in flags_were_updated:
                self.update_time[ss] = this_time
                self.update_id[ss] = update_id

            # Save to HDF5 file
            this_datetime = datetime.datetime.utcfromtimestamp(this_time).strftime("%Y%m%dT%H%M%SZ")
            self.log.info('Writing new %s flags at %s. (%s)' % (' and '.join(flags_were_updated), this_datetime,  update_id))

            input_axis = np.array(self.input, dtype=[('chan_id', 'u2'), ('correlator_input', 'S32')])

            res = {'input':input_axis,
                   'source':self.sources,
                   'datetime':this_datetime,
                   'update_id': update_id,
                   'source_flags':self.source_flags,
                   'flag':self.flag}

            previous_file = self.h5_flag.current_file

            self.h5_flag.write(this_time, **res)

            # If we changed files, then start a new log
            if (previous_file is not None) and (os.path.dirname(self.h5_flag.current_file) != os.path.dirname(previous_file)):
                previous_dir = os.path.dirname(previous_file)
                self.log.info('Starting new log file.  Moving old log file to %s.' % previous_dir)
                try:
                    shutil.move(LOG_FILE, previous_dir)
                except IOError as err:
                    self.log.info('Could not move log file:  %s.' % err)

            # Save most recent flags to HDF5 file that can be accessed by others.
            if self.config.sources_in_buffer or np.any(combined_flag != np.array(new_combined_flag)):
                self.h5_flag.dump(self.buffer_file, timestamp=this_time)

            # Check if we have flagged an abnormally large number of inputs
            # self.check_population()


    def check_population(self):
        """ Compares the number of bad inputs to the historical median.
        """

        passed = True

        if self.h5_flag:

            all_flags = self.h5_flag.read_all('flag')
            all_times = self.h5_flag.read_all('index_map/time')[0:all_flags.size]

            weight = np.diff(all_times)
            weight *= tools.invert_no_zero(np.sum(weight))

            nbad = np.sum(~all_flags, axis=1, dtype=np.int)[:-1]

            mu_nbad = np.sum(weight * nbad)

            mu_prop = mu_nbad / float(all_flags.shape[1])

            sigma = np.sqrt(mu_nbad * (1.0 - mu_prop))

            lower = mu_nbad - 3.0 * sigma
            upper = mu_nbad + 3.0 * sigma

            nbad = np.sum(~np.array(self.flag), dtype=np.int)

            if (nbad <= lower) or (nbad >= upper):

                self.log.warning("%d inputs were flagged bad.  Expect between %d and %d." %
                                 (nbad, lower, upper))

                # Provide alerts here

                # Failed test
                passed = False

        # Return status of population test
        return passed


    @coroutine
    def get_metrics(self):
        """ Return all metrics at the current time.
        """

        timestamp = time.time()

        if self.config.correlator.lower() in ['chime', 'fcc']:
            metric_labels = {'input':wrapper_chime_input_labels(self._input)}
        else:
            metric_labels = {}

        metrics = Metrics(default_type='gauge')

        if ('raw' in self.sources and self.h5_raw and
            (self.config.raw.always_return_metrics or (self.update_time['raw_data'] > self.update_time['metrics']))):
            metrics += yield self.executor.submit(self.h5_raw.get_metrics, timestamp, **metric_labels)

        if (self.h5_flag and (self.config.always_return_metrics or
            any([self.update_time[ss] > self.update_time['metrics'] for ss in self.sources]))):
            metrics += self._get_summary_metrics(timestamp)
            metrics += self._get_bad_input_metrics(timestamp)
            metrics += yield self.executor.submit(self.h5_flag.get_metrics, timestamp, **metric_labels)

        self.update_time['metrics'] = timestamp

        coroutine_return(metrics)


    def _get_bad_input_metrics(self, timestamp):
        """ Generate metrics that indicates which inputs were
        flagged as bad in the recent past.

        Parameters
        ----------
        timestamp : unix time
            Timestamp associated to the metrics.

        lookback : float
            Amount of time in seconds to look back in the past.
            Default is None, which uses the entirety of the flaginput history.
        """

        metrics = Metrics(default_type='gauge')

        if self.historical_status is not None:

            nbad = self.historical_status['bad'].size

            if nbad > 0:
                input_axis = np.array(self.input, dtype=[('chan_id', 'u2'), ('correlator_input', 'S32')])
                bad_inputs = input_axis[self.historical_status['bad']]
                func_input_labels = wrapper_chime_input_labels(self._input)
                labels = func_input_labels(bad_inputs)

                for ii in range(nbad):
                    lbls = {key:val[ii] for key, val in labels.iteritems()}
                    metrics.add('_'.join([self.h5_flag._metric_name, 'historically_bad_input']),
                                value=1, time=timestamp*1000, **lbls)

        return metrics


    def _get_summary_metrics(self, timestamp):

        metrics = Metrics(default_type='gauge')

        prefix = self.h5_flag._metric_name

        for src, dct in self.stats:
            kwargs = {'time':timestamp*1000, 'source':src}
            for key, val in dct.iteritems():
                if val is not None:
                    if key == 'update':
                        value = int(timestamp - val)
                        name = 'last_changed'
                    else:
                        name, value = key, val

                    metrics.add('_'.join([prefix, name]), value=value, **kwargs)

        return metrics


    @coroutine
    def update_historical_status(self):
        yield self.executor.submit(self._update_historical_status)


    def _update_historical_status(self):
        """ Find inputs that have always been flagged as good in the past.

        Parameters
        ----------
        lookback : float
            Amount of time in days to look back in the past.
            Default is the num_days_lookback parameter in config,
            set to None to use the entirety of the flaginput history.
        """

        self.log.info("Determining historically bad inputs.")

        self.historical_status = None
        if self.h5_flag and ('historical_status' in self.config.periodic_callbacks):

            lookback = self.config.historical_status.num_days_lookback

            all_flags = self.h5_flag.read_all('flag')
            all_times = self.h5_flag.read_all('index_map/time')[0:all_flags.size]

            if lookback is None:
                keep = slice(None)
            else:
                keep = np.flatnonzero(all_times >= (all_times[-1] - np.abs(24.0 * 3600.0 * lookback)))

            flg = np.all(all_flags[keep, :], axis=0)
            good_index = np.flatnonzero(flg)
            bad_index = np.flatnonzero(~flg)

            self.historical_status = {'good':good_index, 'bad':bad_index}

            self.log.info("Number historically bad: %d" % self.historical_status['bad'].size)


    def get_flag(self, search, dataset='flag'):
        """ Search the HDF5 flag archive for the
        source_flags or flag at some past time.

        Parameters
        ----------
        search : float or str
            Either unix time or unique update_id.

        dataset : str
            Either 'source_flag' or 'flag'.  Default 'flag'.
        """

        idd = self.h5_flag[search]

        return self.h5_flag.grow_axis(idd), self.h5_flag.read(idd, dataset)


    def stop(self):
        """ Shutdown routine:  Turns off flagging, shutdowns
        thread pool executor, closes hdf5 archives, and
        shutdown http clients.
        """

        self.sources = []
        self.executor.shutdown(wait=False)

        if self.h5_raw:
            self.log.info('Closing %s.' % self.h5_raw.current_file)
            self.h5_raw.close_all()

        if self.h5_flag:
            self.log.info('Closing %s.' % self.h5_flag.current_file)
            self.h5_flag.close_all()

        while self.rms_clients:
            client = self.rms_clients.pop()
            client.shutdown()

        while self.power_clients:
            client = self.power_clients.pop()
            client.shutdown()

        while self.rfi_clients:
            client = self.rfi_clients.pop()
            client.shutdown()

        self.log.info('Closed cleanly.')


    def create_payload(self, update_id, start_time, flag):

        cyl_bad_inputs = [ii for ii, flg in enumerate(flag) if not flg]
        corr_bad_inputs = sorted([self._input[ii].corr_order for ii in cyl_bad_inputs])

        return {'tag': update_id,
                'start_time': start_time,
                'cylinder_bad_inputs':cyl_bad_inputs,
                'correlator_bad_inputs':corr_bad_inputs}

    def check_memory(self):

        results = {}
        for container in [self.h5_flag, self.h5_raw]:
            if container:
                prefix = container._metric_name
                for key, val in container.check_memory().iteritems():
                    results['_'.join([prefix, key])] = val

        return results

    @property
    def stats(self):
        """ Returns a list of dictionaries where each
        element of the list contains basic statistics
        for a source flag:

            update:  str
                Time of last update.

            nbad:  int
                Number of correlator inputs that are
                flagged as bad by this source.

            nuniq: int
                Number of correlator inputs that are
                flagged as bad by this source and flagged
                as good by all other soruces.
        """
        stats = []
        for ind, ss in enumerate(self.sources):

            ialt = range(self.nsources)
            ialt.remove(ind)

            bad = ~self.source_flags[ind]

            nbad = np.sum(bad, dtype=np.int)
            nuniq = np.sum(bad & np.all(self.source_flags[ialt], axis=0), dtype=np.int)

            stats.append((ss, {'update': self.update_time[ss],
                               'number_bad': nbad,
                               'number_uniq_bad': nuniq}))

        nbad = np.sum(~np.array(self.flag))
        stats.append(('combined', {'update': self.update_time['combined'],
                                   'number_bad': nbad,
                                   'number_uniq_bad': nbad}))

        return stats


    @property
    def flag(self):
        """ Returns a list of correlator input flags
        obtained by taking the AND of the most recent
        source flags from the sources specified in
        self.config.combine.
        """
        return [bool(ff) for ff in np.all(self.source_flags[self.icombine], axis=0)]


    @property
    def input(self):
        """ Returns a list of tuples of the form:

        (Cylinder Based ID, Correlator Input SN [e.g., FCC000000])

        obtained from the most recent query to the layout database.
        """
        return [(inp.id, inp.input_sn) for inp in self._input]


    @property
    def ninput(self):
        """ Number of correlator inputs.  Obtained from
        the most recent query to the layout database.
        """
        return len(self._input)

    @property
    def nsources(self):
        """ Number of sources that are currently being
        polled for flags.
        """
        return len(self.sources)


#########################################
# FlagCorrInput REST server
#########################################

class FlagCorrInputAsyncRESTServer(AsyncRESTServer):
    """
    REST interface for FlagCorrInput
    """

    DEFAULT_PORT = DEFAULTS.port

    def __init__(self,  address='', port=DEFAULT_PORT, logging_params={}):

        # Initialize attributes
        self.flg = None
        self._periodic_callbacks = {}

        # Define map between sources and periodic update methods
        self._periodic_methods = {'layout': self._query_layout_database, 'power': self._query_fla_power_server,
                                  'rms': self._query_rms_server, 'rfi': self._query_rfi_server,
                                  'raw': self._get_raw_adc_files}

        # Call AsyncRESTServer
        super(FlagCorrInputAsyncRESTServer, self).__init__(address=address, port=port,
                                                           heartbeat_string='Is', heartbeat_period=60000)


    @coroutine
    def shutdown(self):

        self.log.info('%r: Shutting down.' % self)

        # Stop all periodic callbacks
        while True:
            try:
                source, callback = self._periodic_callbacks.popitem()
                callback.stop()
                self.log.info('%r: Ended %s periodic callback.' % (self, source))
            except KeyError:
                break

        # Close open files, shutdown thread executor
        self.flg.stop()

        # Set flg to False
        self.flg = None


    ###########################
    # Periodic callback methods
    ###########################

    @coroutine
    def _get_raw_adc_files(self):
        """ Process new raw acquisition files.
        """
        if self.flg is None: return

        self.log.info('%r: Searching for new raw ADC acquisition files.' % self)
        try:
            # Add new files to the queue
            self.flg.get_raw_adc_files()

            # Make sure the file processor has not died
            if 'raw' in self.flg.sources:
                try:
                    exc = self.flg.futures['raw'].exception(timeout=0.25)

                except concurrent.futures.TimeoutError:
                    pass

                else:
                    self.log.error('%r: Raw ADC file processor failed with error:  %s' % (self, exc))
                    self.log.info('%r: Restarting raw ADC file processor.' % self)
                    self.flg.futures['raw'] = self.flg.executor.submit(self.flg.process_raw_adc_files)


        except Exception as e:
            self.log.error(e)
            raise

    @coroutine
    def _query_layout_database(self):
        """ Query the layout database and find feeds not connected to an antenna.
        """
        if self.flg is None: return

        self.log.info('%r: Querying layout database.' % self)
        try:
            self.flg.query_layout_database()

        except Exception as e:
            self.log.error(e)
            raise

    @coroutine
    def _query_fla_power_server(self):
        """ Query the FLA power server and find feeds currently powered off.
        """
        if self.flg is None: return

        self.log.info('%r: Querying FLA power server.' % self)
        try:
            yield self.flg.query_fla_power_server()

        except Exception as e:
            self.log.error(e)
            raise

    @coroutine
    def _query_rms_server(self):
        """ Query the raw acquisition server and find feed with low or high RMS.
        """
        if self.flg is None: return

        self.log.info('%r: Querying raw adc server for rms.' % self)
        try:
            yield self.flg.query_rms_server()

        except Exception as e:
            self.log.error(e)
            raise

    @coroutine
    def _query_rfi_server(self):
        """ Query the rfi server and find feed with high bad input likelihood.
        """
        if self.flg is None: return

        self.log.info('%r: Querying rfi server.' % self)
        try:
            yield self.flg.query_rfi_server()

        except Exception as e:
            self.log.error(e)
            raise

    @coroutine
    def _update_rms_excludes(self):
        """ Update the time ranges to exclude from rms calculation.
        """
        if self.flg is None: return

        self.log.info('%r: Updating time ranges to exclude from rms test.' % self)
        try:
            self.flg.update_rms_excludes()

        except Exception as e:
            self.log.error(e)
            raise

    @coroutine
    def _update_historical_status(self):
        """ Update the inputs that have been historically bad.
        """
        if self.flg is None: return

        self.log.info('%r: Updating historically bad inputs.' % self)
        try:
            yield self.flg.update_historical_status()

        except Exception as e:
            self.log.error(e)
            raise

    ##################
    # Server commands
    ##################

    @coroutine
    @endpoint('start')
    def start(self, handler, **config):
        """ Start FlagCorrInput analysis with provided config.
        """
        self.log.info('%r: Received start command' % self)
        if self.flg:
            self.log.info('%r: FlagCorrInput server already running.  Restarting with new config.' % self)
            self.shutdown()

        # Save configuration file
        # Save configuration parameters
        self.config = DEFAULTS.deepcopy()
        self.config.merge(NameSpace(config))

        self.log.debug('%r: Creating FlagCorrInput handler' % self)
        self.flg = FlagCorrInput(**self.config)

        #Add periodic callbacks for sources
        for path, method in self._periodic_methods.iteritems():

            if path in self.flg.config.sources:
                cfg = self.flg.config[path]
                self._periodic_callbacks[path] = tornado.ioloop.PeriodicCallback(method, 1000 * cfg.cadence)
                self._periodic_callbacks[path].start()

        # Add additional periodic callbacks
        for path in self.flg.config.periodic_callbacks:
            cfg = self.flg.config[path]
            self._periodic_callbacks[path] = tornado.ioloop.PeriodicCallback(getattr(self, cfg.method), 1000 * cfg.cadence)
            self._periodic_callbacks[path].start()

        # Server started
        coroutine_return('FlagCorrInput server started.')

    @coroutine
    @endpoint('stop')
    def stop(self, handler):
        """ Stop FlagCorrInput analysis.
        """
        self.log.info('%r: Received stop command' % self)
        if self.flg:
            self.shutdown()
            coroutine_return('FlagCorrInput server stopped.')

        else:
            coroutine_return('FlagCorrInput server is not started.')

    @coroutine
    @endpoint('configuration')
    def configuration(self, handler):
        self.log.info('%r: Received request for configuration.' % self)
        if self.flg:
            coroutine_return( {'flaginput': self.flg.config.as_dict()} )

        else:
            coroutine_return( "FlagCorrInput server is not started" )

    @coroutine
    @endpoint('correlator-inputs')
    def correlator_inputs(self, handler):
        self.log.info('%r: Received request for correlator inputs.' % self)
        if self.flg:
            coroutine_return( self.flg.input )

        else:
            coroutine_return( "FlagCorrInput server is not started" )

    @coroutine
    @endpoint('good-correlator-inputs')
    def good_correlator_inputs(self, handler):
        self.log.info('%r: Received request for good correlator inputs.' % self)
        if self.flg:
            coroutine_return( [inp for inp, good in zip(self.flg.input, self.flg.flag) if good] )

        else:
            coroutine_return( "FlagCorrInput server is not started" )

    @coroutine
    @endpoint('bad-correlator-inputs')
    def bad_correlator_inputs(self, handler):
        self.log.info('%r: Received request for bad correlator inputs.' % self)
        if self.flg:
            coroutine_return( [inp for inp, good in zip(self.flg.input, self.flg.flag) if not good] )

        else:
            coroutine_return( "FlagCorrInput server is not started" )

    @coroutine
    @endpoint('bad-correlator-inputs-by-source')
    def bad_correlator_inputs_by_source(self, handler):
        self.log.info('%r: Received request for bad correlator inputs for each source.' % self)
        if self.flg:
            coroutine_return( {src:[inp for inp, good in zip(self.flg.input, self.flg.source_flags[ii]) if not good]
                                for ii, src in enumerate(self.flg.sources)} )
        else:
            coroutine_return( "FlagCorrInput server is not started" )

    @coroutine
    @endpoint('correlator-input-flags')
    def correlator_input_flags(self, handler):
        self.log.info('%r: Received request for correlator input flag.' % self)
        if self.flg:
            coroutine_return( [[idd, sn, int(flag)] for (idd, sn), flag in zip(self.flg.input, self.flg.flag)] )

        else:
            coroutine_return( "FlagCorrInput server is not started" )

    @coroutine
    @endpoint('correlator-input-reorder')
    def correlator_input_reorder(self, handler):
        self.log.info('%r: Received request for correlator input reordering.' % self)
        if self.flg:
            coroutine_return( [[inp.corr_order, inp.id, inp.input_sn] for inp in self.flg._input] )

        else:
            coroutine_return( "FlagCorrInput server is not started" )

    @coroutine
    @endpoint('noise-injection-inputs')
    def noise_injection_inputs(self, handler):
        self.log.info('%r: Received request for noise injection inputs.' % self)
        if self.flg:
            coroutine_return( [idx for idx, inp in zip(self.flg.input, self.flg._input)
                               if isinstance(inp, tools.NoiseSource)] )

        else:
            coroutine_return( "FlagCorrInput server is not started" )

    @coroutine
    @endpoint('noise-injection-products')
    def noise_injection_products(self, handler):
        self.log.info('%r: Received request for noise injection products.' % self)
        if self.flg:
            inoise = [ix for ix, inp in enumerate(self.flg._input) if isinstance(inp, tools.NoiseSource)]

            prod = sorted([tools.cmap(ii, jj, self.flg.ninput) for ii in inoise for jj in inoise])

            coroutine_return( prod )

        else:
            coroutine_return( "FlagCorrInput server is not started" )

    @coroutine
    @endpoint('holography-inputs')
    def holography_inputs(self, handler):
        self.log.info('%r: Received request for noise injection inputs.' % self)
        if self.flg:
            coroutine_return( [idx for idx, inp in zip(self.flg.input, self.flg._input)
                               if isinstance(inp, tools.HolographyAntenna)] )

        else:
            coroutine_return( "FlagCorrInput server is not started" )

    @coroutine
    @endpoint('summary')
    def summary(self, handler):
        self.log.info('%r: Received request for summary.' % self)
        if self.flg:

            def format_time(unix_time):
                return datetime.datetime.utcfromtimestamp(unix_time).strftime('%Y-%m-%d %H:%M:%S') if unix_time else ''

            fmt = "%-10s %-30s %-10s %-10s"
            summary  = fmt % ("SOURCE", "LAST CHANGE (UTC)", "N BAD", "N UNIQ BAD") + '\n'
            summary += '\n'.join([fmt % (ss, format_time(dct['update']), dct['number_bad'], dct['number_uniq_bad'])
                                    for ss, dct in self.flg.stats])

            coroutine_return( summary )

        else:
            coroutine_return( "FlagCorrInput server is not started" )

    @coroutine
    @endpoint('stats')
    def stats(self, handler):
        self.log.info('%r: Received request for summary.' % self)
        if self.flg:
            coroutine_return( self.flg.stats )

        else:
            coroutine_return( "FlagCorrInput server is not started" )

    @coroutine
    @endpoint('monitoring-data')
    def monitoring_data(self, handler):
        self.log.info('%r: Received monitoring metrics request.' % self)
        t0 = time.time()
        try:
            metrics = yield self.flg.get_metrics()

        except Exception as e:
            handler.set_header('Content-Type', 'text/plain')
            handler.write('')
            self.log.error(e)

        else:
            encoding = self.flg.config.get('metric_encoding', 'text/plain')
            handler.set_header('Content-Type', 'text/plain')
            if encoding == 'gzip':
                handler.set_header('Content-Encoding', 'gzip')
                handler.write(metrics.get_gzip())
            else:
                handler.write(str(metrics))
            self.log.info('%r: Returning %i flaginput metrics. The request took %.3f seconds' % (self, len(metrics), time.time()-t0))

    @coroutine
    @endpoint('raw-metrics')
    def metrics(self, handler):
        self.log.info('%r: Received raw metrics request.' % self)
        t0 = time.time()
        try:
            metrics = yield self.flg.get_raw_metrics()

        except Exception as e:
            handler.set_header('Content-Type', 'text/plain')
            handler.write('')
            self.log.error(e)

        else:
            encoding = self.flg.config.raw.get('metric_encoding', 'gzip')
            handler.set_header('Content-Type', 'text/plain')
            if encoding == 'gzip':
                handler.set_header('Content-Encoding', 'gzip')
                handler.write(metrics.get_gzip())
            else:
                handler.write(str(metrics))
            self.log.info('%r: Returning %i flag_raw metrics. The request took %.3f seconds' % (self, len(metrics), time.time()-t0))

    @coroutine
    @endpoint('check-memory')
    def stats(self, handler):
        self.log.info('%r: Checking memory fidelity.' % self)
        if self.flg:

            coroutine_return( self.flg.check_memory() )

        else:
            coroutine_return( "FlagCorrInput server is not started" )

    @coroutine
    @endpoint('past-correlator-input-flags')
    def past_correlator_input_flags(self, handler, timestamp):
        self.log.info('%r: Received request for past correlator input flags.' % self)

        timestamp_decoded = ensure_unix(json.loads(timestamp))

        if self.flg:
            timestamp_returned, arr = self.flg.get_flag(timestamp_decoded, dataset='flag')

            if timestamp_returned is not None:
                coroutine_return( {'time': timestamp_returned,
                                   'payload':[[idd, sn, int(flag)] for (idd, sn), flag in zip(self.flg.input, arr)]} )

            else:
                coroutine_return( "No flags available for time %s" % timestamp_decoded )

        else:
            coroutine_return( "FlagCorrInput server is not started" )

    @coroutine
    @endpoint('past-good-correlator-inputs')
    def past_good_correlator_inputs(self, handler, timestamp):
        self.log.info('%r: Received request for past good correlator inputs.' % self)

        timestamp_decoded = ensure_unix(json.loads(timestamp))

        if self.flg:
            timestamp_returned, arr = self.flg.get_flag(timestamp_decoded, dataset='flag')

            if timestamp_returned is not None:
                coroutine_return( {'time': timestamp_returned,
                                   'payload': [inp for inp, good in zip(self.flg.input, arr) if good]} )

            else:
                coroutine_return( "No flags available for time %s" % timestamp_decoded )

        else:
            coroutine_return( "FlagCorrInput server is not started" )

    @coroutine
    @endpoint('past-bad-correlator-inputs')
    def past_bad_correlator_inputs(self, handler, timestamp):
        self.log.info('%r: Received request for past bad correlator inputs.' % self)

        timestamp_decoded = ensure_unix(json.loads(timestamp))

        if self.flg:
            timestamp_returned, arr = self.flg.get_flag(timestamp_decoded, dataset='flag')

            if timestamp_returned is not None:
                coroutine_return( {'time': timestamp_returned,
                                   'payload': [inp for inp, good in zip(self.flg.input, arr) if not good]} )

            else:
                coroutine_return( "No flags available for time %s" % timestamp_decoded )

        else:
            coroutine_return( "FlagCorrInput server is not started" )

    @coroutine
    @endpoint('past-source-flags')
    def past_source_flags(self, handler, timestamp):
        self.log.info('%r: Received request for past flags.' % self)

        timestamp_decoded = ensure_unix(json.loads(timestamp))

        if self.flg:
            timestamp_returned, arr = self.flg.get_flag(timestamp_decoded, dataset='source_flags')

            if timestamp_returned is not None:
                source_flags = {'time': timestamp_returned,
                                'payload': {src:[[idd, sn, int(flag)] for (idd, sn), flag in zip(self.flg.input, arr[ii])]
                                                          for ii, src in enumerate(self.flg.sources)}}
                coroutine_return( source_flags )

            else:
                coroutine_return( "No flags available for time %s" % timestamp_decoded )

        else:
            coroutine_return( "FlagCorrInput server is not started" )

    @coroutine
    @endpoint('last-update')
    def last_update(self, handler):
        self.log.info('%r: Received request for last update: %s')

        if self.flg:
            update_id = self.flg.update_id['combined']
            timestamp_returned, arr = self.flg.get_flag(update_id, dataset='flag')

            if timestamp_returned is not None:

                payload = self.flg.create_payload(update_id, timestamp_returned, arr)

                coroutine_return( payload )

            else:
                coroutine_return( "No flags available." )

        else:
            coroutine_return( "FlagCorrInput server is not started" )

    @coroutine
    @endpoint('past-update')
    def past_update(self, handler, tag):
        self.log.info('%r: Received request for past tag: %s' % (self, tag))

        if self.flg:
            timestamp_returned, arr = self.flg.get_flag(tag, dataset='flag')

            if timestamp_returned is not None:

                payload = self.flg.create_payload(tag, timestamp_returned, arr)

                coroutine_return( payload )

            else:
                coroutine_return( "No flags available for tag %s" % tag )

        else:
            coroutine_return( "FlagCorrInput server is not started" )


#########################################
# FlagCorrInput REST client
#########################################

class FlagCorrInputAsyncRESTClient(AsyncRESTClient):
    """
    Implements an asynchronous client that exposes the functions of the specified remote
    FlagCorrInput server.  This client is used by ch_master to start, configure, and
    operate FlagCorrInput.

    The client is implemented using a Tornado AsyncHTTPClient. It exposes the FlagCorrInput
    server methods (i.e REST endpoints) as local methods. The local methods are Tornado coroutines
    so requests to multiple clients can be made in parallel. This is especially beneficial since
    the data requests from the server are slow IO operations which benefit the most from co-execution.

    The client will operate only if the IOloop in which it was created is running.

    Parameters:

        hostname (str): The hostname of the FlagCorrInput REST server.

        port (int): The port number to which the FlagCorrInput REST server is listening.
                    Default is port 54327.

    """

    DEFAULT_PORT = FlagCorrInputAsyncRESTServer.DEFAULT_PORT

    def __init__(self, hostname='localhost', port=DEFAULT_PORT):

        super(FlagCorrInputAsyncRESTClient, self).__init__(hostname=hostname, port=port,
                                                           heartbeat_string='Ic', heartbeat_period=60000,
                                                           server_class=FlagCorrInputAsyncRESTServer)

    def print_result(self, d):
        if d == {}:
            print('ok')
        elif 'error' in d:
            print('Error:', d['error'].rstrip())
        else:
            print(json.dumps(d, sort_keys=True, indent=2))

    @coroutine
    def start(self, config):
        """ If the FlagCorrInput remote server is not started, start it with the specified configuration

        Parameters:

            config (str or dict): If a string, the configuration is loaded from the specified
                configuration file and name. if a dict, it is passed directly to the server.

        """

        self.log.info('%s: Starting remote FlagCorrInput server at %s:%i with config: %r' % (self, self.hostname, self.port, config))

        if isinstance(config, str):
            config = load_yaml_config(config)

        result = self.post('start', **config)
        coroutine_return('FlagCorrInput server started')

    @coroutine
    def stop(self):
        result = yield self.get('stop')
        coroutine_return(result)

    @coroutine
    def get_configuration(self):
        res = yield self.get('configuration')
        coroutine_return(res)

    @coroutine
    def get_correlator_inputs(self):
        res = yield self.get('correlator-inputs')
        coroutine_return(res)

    @coroutine
    def get_good_correlator_inputs(self):
        res = yield self.get('good-correlator-inputs')
        coroutine_return(res)

    @coroutine
    def get_bad_correlator_inputs(self):
        res = yield self.get('bad-correlator-inputs')
        coroutine_return(res)

    @coroutine
    def get_bad_correlator_inputs_by_source(self):
        res = yield self.get('bad-correlator-inputs-by-source')
        coroutine_return(res)

    @coroutine
    def get_correlator_input_flags(self):
        res = yield self.get('correlator-input-flags')
        coroutine_return(res)

    @coroutine
    def get_correlator_input_reorder(self):
        res = yield self.get('correlator-input-reorder')
        coroutine_return(res)

    @coroutine
    def get_noise_injection_inputs(self):
        res = yield self.get('noise-injection-inputs')
        coroutine_return(res)

    @coroutine
    def get_noise_injection_products(self):
        res = yield self.get('noise-injection-products')
        coroutine_return(res)

    @coroutine
    def get_holography_inputs(self):
        res = yield self.get('holography-inputs')
        coroutine_return(res)

    @coroutine
    def get_stats(self):
        res = yield self.get('stats')
        coroutine_return(res)

    @coroutine
    def get_summary(self):
        res = yield self.get('summary')
        self.print_result(res)

    @coroutine
    def get_past_correlator_input_flags(self, timestamp):
        res = yield self.post('past-correlator-input-flags', timestamp)
        coroutine_return(res)

    @coroutine
    def get_past_bad_correlator_inputs(self, timestamp):
        res = yield self.post('past-bad-correlator-inputs', timestamp)
        coroutine_return(res)

    @coroutine
    def get_past_good_correlator_inputs(self, timestamp):
        res = yield self.post('past-good-correlator-inputs', timestamp)
        coroutine_return(res)

    @coroutine
    def get_past_source_flags(self, timestamp):
        res = yield self.post('past-source-flags', timestamp)
        coroutine_return(res)

    @coroutine
    def get_last_update(self):
        res = yield self.get('last-update')
        coroutine_return(res)

    @coroutine
    def get_past_update(self, tag):
        res = yield self.post('past-update', tag)
        coroutine_return(res)


#########################################
# Power REST client
#########################################

FLA_COLUMN = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'J', 'K', 'L', 'M', 'N', 'P', 'Q', 'R']

def bulkhead_position_to_correlator_input(bulkhead, column, row):

    blk = ord(bulkhead.upper()) - 65 if isinstance(bulkhead, basestring) else bulkhead

    crate = blk*2 + int(row < 16)
    slot = FLA_COLUMN.index(column)
    inp = row % 16

    correlator_input = 'FCC%02d%02d%02d' % (crate, slot, inp)

    return correlator_input


class PowerAsyncRESTClient(AsyncRESTClient):
    """
    Implements an asynchronous client that exposes the functions of the specified remote
    FLA power server.  This client is used by FlagCorrInput to query the FLA Power REST server.

    Parameters
    ----------
    hostname: str
        The hostname of the FLA power REST server.  Default is 'hk-west'.

    port: int
        The port number to which the FLA power REST server is listening.
        Default is port 5000.

    bullkheads: list of str
        The bulkheads that the FLA power REST server is monitoring.
        Default is ['A', 'B'].

    """

    def __init__(self, hostname='hk-west', port=5000, bulkheads=['A', 'B'], *args, **kwargs):

        super(PowerAsyncRESTClient, self).__init__(hostname=hostname, port=port)

        self.bulkheads = bulkheads
        self.powered = {}

    @coroutine
    def get_status(self):

        for bulkhead in self.bulkheads:
            endpoint = '/'.join(['fla_power', bulkhead, 'metrics'])
            resp = yield self.get(endpoint, raw=True)
            self._update_status(resp)

    def _update_status(self, results):

        res = results.splitlines()
        type_prefix = '# TYPE'

        for rr in res:
            if rr.startswith(type_prefix):
                metric_name = rr[len(type_prefix):].split()[0]
                break

        for rr in res:
            if rr.startswith(metric_name):
                labels, powered, time = rr[len(metric_name):].split()

                mo = re.match('{bulkhead="(\d{1})",row="(\d{1,2})",column="([A-R])"}', labels)
                if mo is not None:
                    bulkhead, row, column = 3 - int(mo.group(1)), int(mo.group(2)), mo.group(3)
                    corr_input = bulkhead_position_to_correlator_input(bulkhead, column, row)
                    self.powered[corr_input] = (float(time) / 1000.0, bool(int(powered)))


#########################################
# RMS REST client
#########################################

SMA_TO_INP = [12, 13, 14, 15, 8, 9, 10, 11, 4, 5, 6, 7, 0, 1, 2, 3]

class RmsAsyncRESTClient(AsyncRESTClient):
    """
    Implements an asynchronous client that exposes (some of) the functions of the specified remote
    raw acquisition server.  This client is used by FlagCorrInput to query the raw acquisition server
    for the most recent RMS values.

    Parameters
    ----------
    hostname: str
        The hostname of the raw acquisition REST server.  Default is 'carillon'.

    port: int
        The port number to which the raw acquisition REST server is listening.
        Default is port 54322.

    """

    def __init__(self, hostname='carillon', port=54322, *args, **kwargs):

        super(RmsAsyncRESTClient, self).__init__(hostname=hostname, port=port)

        self.rms = {}

    @coroutine
    def get_status(self):

        resp = yield self.get('get-rms', raw=False)
        self._update_status(resp['rms'])

    def _update_status(self, results):

        for (crate, slot, sma), rms in results:

            corr_input = 'FCC%02d%02d%02d' % (crate, slot, SMA_TO_INP[sma])

            self.rms[corr_input] = rms


#########################################
# RFI Monitor REST client
#########################################

class RFIMonitorRESTClient(AsyncRESTClient):
    """
    Implements an asynchronous client that exposes (some of) the functions of the
    rfi monitor server.   This client is used by FlagCorrInput to query the
    rfi monitor server for the most recent bad input liklihood values.

    Parameters
    ----------
    hostname: str
        The hostname of the RFI REST server.  Default is 'carillon'.

    port: int
        The port number to which the RFI REST server is listening.
        Default is port 7342.

    """

    def __init__(self, hostname='carillon', port=7342, *args, **kwargs):

        super(RFIMonitorRESTClient, self).__init__(hostname=hostname, port=port)

        self.bad_input_likelihood = []

    @coroutine
    def update_rfi_bad_input_mask(self):

        resp = yield self.get('rfi_bad_input_mask', raw=True)
        self._update_rfi_bad_input_mask(resp)

    def _update_rfi_bad_input_mask(self, results):

        match_string = 'rfi_bad_input_mask = '

        for rr in results.splitlines():
            if rr.startswith(match_string):
                likelihood = tornado.escape.json_decode(rr[len(match_string):])
                self.bad_input_likelihood = likelihood
                break


#########################################
# Kotekan Master REST client
#########################################

class KotekanRESTClient(AsyncRESTClient):
    """
    Implements an asynchronous client that exposes (some of) the functions of the
    kotekan master client.   This client is used by FlagCorrInput to POST the
    bad input list to kotekan master for distribution to relevant kotekan processes.

    Parameters
    ----------
    hostname: str
        The hostname of the kotekan master REST server.  Default is 'csBfs'.

    port: int
        The port number to which the kotekan master REST server is listening.
        Default is port 54323.

    """

    def __init__(self, hostname='csBfs', port=54323, *args, **kwargs):

        super(KotekanRESTClient, self).__init__(hostname=hostname, port=port)

    @coroutine
    def update_bad_inputs(self, tag, start_time, correlator_bad_inputs, cylinder_bad_inputs):

        result = yield self.post('update-bad-inputs', tag=tag, start_time=start_time,
                                                      correlator_bad_inputs=correlator_bad_inputs,
                                                      cylinder_bad_inputs=cylinder_bad_inputs)
        coroutine_return(result)


#########################################
# main
#########################################

DEFAULT_LOGGING = {
    'formatters': {
         'std': {
             'format': "%(asctime)s %(levelname)s %(name)s: %(message)s",
             'datefmt': "%m/%d %H:%M:%S"},
          },
    'handlers': {
        'stderr': {'class': 'logging.StreamHandler', 'formatter': 'std', 'level': 'DEBUG'}
        },
    'loggers': {
        '': {'handlers': ['stderr'], 'level': 'DEBUG'}  # root logger

        }
    }

def main(logging_params=DEFAULT_LOGGING):

    # Setup logging
    log.setup_logging(logging_params)

    # Create client and server
    client, server = run_client(sys.argv[1:], FlagCorrInputAsyncRESTServer, FlagCorrInputAsyncRESTClient,
                                              object_name='FlagCorrInput', server_config_path='flaginput.servers')
    return client, server


if __name__ == '__main__':
    """ Command-line interface to launch and operate the FlagCorrInput server.
    """

    # If calling from the command line, then send logging to log file instead of screen
    mkdir(os.path.dirname(LOG_FILE))
    logging_params = DEFAULT_LOGGING
    logging_params['handlers'] = {'stderr': {'class': 'logging.handlers.WatchedFileHandler',
                                            'filename': LOG_FILE, 'formatter': 'std', 'level': 'INFO'}}

    # Create client and server with modified logging parameters
    client, server = main(logging_params=logging_params)
