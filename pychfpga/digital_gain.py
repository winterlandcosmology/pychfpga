import logging
import os
import glob
import datetime
import time
from calendar import timegm

import h5py
import numpy as np

from wtl.archive import Hdf5Archive

__version__ = '0.5'
__archive_version__ = '3.2.0'


class DigitalGainArchive(Hdf5Archive):
    """Interface to an Hdf5Archive containing digital gains.
    """

    _uniq_id = 'update_id'
    _grow_ax = 'update_time'

    _axes = {
        'update_time': {'dtype': np.float64},
        'freq':  {'dtype': np.dtype([('centre', '<f8'), ('width', '<f8')])},
        'input': {'dtype': np.dtype([('chan_id', 'u2'), ('correlator_input', 'U32')])},
    }

    _dataset_spec = {
        'update_id': {
            'axes': ['update_time', ],
            'dtype': h5py.special_dtype(vlen=str),
            'metric': False,
        },
        'compute_time': {
            'axes': ['update_time', 'input'],
            'dtype': np.float64,
            'metric': False,
        },
        'gain_coeff': {
            'axes': ['update_time', 'freq', 'input'],
            'dtype': np.complex64,
            'metric': False,
        },
        'gain_exp': {
            'axes': ['update_time', 'input'],
            'dtype': np.int32,
            'metric': False,
        }
    }

    _with_lock_file = True
    _resume_writing = False
    _check_size_after_write = True

    def __init__(self, output_dir=None, output_suffix="digitalgain", instrument_name="chime",
                 notes="", search=True, max_num=1, max_file_size=1e9,
                 *args, **kwargs):
        """Instantiates a DigitalGainArchive object.

        If not appending to an existing file, then this will create a new file on disk
        with the first call to write.

        Parameters
        ----------
        output_dir :  str
            Directory where the digital gain acquisitions will be saved. There is no "~" or named-field expansion.
        output_suffix : str
            Suffix appended to the acquisition name.
        instrument_name :  str
            Name of the instrument/correlator.  Included in acquisition name,
            and also saved to file attributes.
        notes : str
            User notes that are saved to file attributes.
        search : bool
            Search the output_dir for compatible files and provide read access to them.
        max_num : int
            Maximum number of time samples to save to a single file.
        max_file_size : int
            Maximum size in bytes of a single file.
        """

        self.log = logging.getLogger(__name__)

        # Save axes
        self.axes = {}
        for ax in self._axes.keys():
            if ax != self._grow_ax:
                if ax in kwargs:
                    dtype = self._axes[ax]['dtype']
                    self.axes[ax] = kwargs.pop(ax).astype(dtype)
                else:
                    ValueError(f"Must pass the axis {ax} as a keyword arg when initializing {self}.")

        # Set parameters that specify output file format
        self.output_dir = output_dir or '.'
        self.output_suffix = output_suffix
        self.log.info(f'{self!r}: Digital gain output directory is {self.output_dir}')

        # Search for previous files
        if search:
            search_pathname = os.path.join(
                self.output_dir,
                '*' + instrument_name + '_' + self.output_suffix, '*.h5')
            candidate_files = sorted(glob.glob(search_pathname))

            # Only use files with the same axes
            output_files = []
            for cf in candidate_files:

                with h5py.File(cf, 'r') as hf:

                    valid = True
                    for key, val in self.axes.items():
                        dtype = self._axes[key]['dtype']
                        valid = (valid and (key in hf['index_map']) and
                                 (hf['index_map'][key].size == val.size) and
                                 np.all(hf['index_map'][key][:].astype(dtype) == val))

                    if valid:
                        output_files.append(cf)

            output_files = output_files or None
            self.log.info(f'{self!r}: Searched for previous gain files.  Candidates are: {output_files}')

        else:
            output_files = None

        # Set the file attributes
        attrs = {'instrument_name': instrument_name,
                 'version': __version__,
                 'archive_version': __archive_version__,
                 'notes': notes}

        if 'attrs' not in kwargs:
            kwargs['attrs'] = {}

        for key, val in attrs.items():
            if key not in kwargs['attrs']:
                kwargs['attrs'][key] = val

        # Call superclass
        super().__init__(archive_files=output_files,
                         max_num=max_num, max_file_size=max_file_size,
                         *args, **kwargs)

        # Initialize the gain buffer
        self.buffer = {}
        datasets = [dset for dset in self._dataset_spec.keys() if dset != self._uniq_id]
        for dset in datasets:
            dspec = self._dataset_spec[dset]
            axes = [ax for ax in dspec['axes'] if ax != self._grow_ax]
            if axes:
                shp = [self.axes[ax].size for ax in axes]
                self.buffer[dset] = np.zeros(shp, dtype=dspec['dtype'])

        # Save the last update to the buffer
        if self.current_file is not None:
            lastup = self.last_update_id
            for dset in datasets:
                self.buffer[dset] = self.read(lastup, dset)

    def get_output_file(self, smp, **kwargs):
        """Defines the filenaming conventions for the archive files:

            {output_dir}/{YYYYMMDD}T{HHMMSS}Z_{instrument_name}_{output_suffix}/{SSSSSSS}.h5
            
        Parameters
        ----------
        smp : unix time
            Time at which the datasets in kwargs were collected.

        run_name : str
            Name of the fpga_master run. The timestamp from the run is used
            in the digital gain acquisition directory name.
        """

        if 'run_name' in kwargs:
            base_prefix = kwargs['run_name'][0:16]
        else:
            base_prefix = datetime.datetime.utcfromtimestamp(smp).strftime("%Y%m%dT%H%M%SZ")

        start_time = timegm(datetime.datetime.strptime(base_prefix, "%Y%m%dT%H%M%SZ").timetuple())

        # Determine directory
        output_dir = os.path.join(self.output_dir, '_'.join(
            [base_prefix, self.attrs['instrument_name'], self.output_suffix]))
        try:
            os.makedirs(output_dir)
        except OSError:
            if not os.path.isdir(output_dir):
                raise

        # Determine filename
        seconds_elapsed = smp - start_time

        output_file = os.path.join(output_dir, f"{int(seconds_elapsed):08}.h5")

        return output_file

    def get_current_gain_file(self):
        """Get the full file path of the current digital gains file"""
        
        digital_gains_parent_path = f'{self.output_dir}/{self.attrs['acquisition_name']}'

        files = os.listdir(f'{gain_hdf5.output_dir}/{gain_hdf5.attrs['acquisition_name']}')

        if len(files) == 1:
            digital_gains_path = f'{digital_gains_parent_path}/{files[0]}'
        else:
            raise NotImplementedError('Recovering the current ') # FIXME

        return

    def change_file(self, smp, **kwargs):
        """Boolean indicating if the current gains should be written to new file.

        Changes the file if the run name changes.

        Parameters
        ----------
        smp: unix time
            Time at which the datasets in kwargs were computed.

        run_name : str
            Name of the run in which the datasets in kwargs were computed.
        """
        current_acq = self.writer.attrs.get('acquisition_name', None)
        current_run = kwargs.get('run_name', None)

        if (current_acq is not None) and (current_run is not None):
            return current_run[0:16] != current_acq[0:16]
        else:
            return False

    def write(self, smp=None, **kwargs):
        """Write a gain update to the file.

        Writes the contents of the buffer to the file.  Use `set_gain` to update the buffer.

        Parameters
        ----------
        smp: unix time
            Time of the gain update.  Defaults to current time.
        """

        if smp is None:
            smp = time.time()

        for key, value in self.axes.items():
            kwargs[key] = value

        for key, value in self.buffer.items():
            kwargs[key] = value

        if 'update_id' not in kwargs:
            kwargs['update_id'] = '_'.join(
                [self.output_suffix, datetime.datetime.utcfromtimestamp(smp).strftime("%Y%m%dT%H%M%S.%fZ")])

        # Call superclass
        super().write(smp, **kwargs)

    def set_gain(self, gain, compute_time=None):
        """ Update the buffer with new gains.

        Parameters
        ----------
        gain : dict
            Dictionary of format {'input_serial_number': [gain_coeff, gain_exp], ...}
            containing the linear (nfreq array) and logarithmic (scalar) digital gain.
        compute_time : dict
            Either a dictionary of format {'input_serial_number': unix_timestamp, ...} or
            a single unix timestamp that is applied to all inputs.  Defaults to the current time.
        """

        if compute_time is None:
            compute_time = time.time()

        for sn, (gcoeff, gexp) in gain.items():

            gain_timestamp = compute_time[sn] if isinstance(compute_time, dict) else compute_time

            chan_id = self.chan_id[sn]

            self.buffer['gain_coeff'][:, chan_id] = gcoeff
            self.buffer['gain_exp'][chan_id] = gexp
            self.buffer['compute_time'][chan_id] = gain_timestamp

    def read_gain(self, update_id=None):
        """Read gains from the archive of files.

        Parameters
        ----------
        update_id : str or float
            This can be either a unique update_id or a unix timestamp.  If unix timestamp
            then the most recent update occuring before that timestamp will be returned.
            Defaults to the last update_id.

        Returns
        -------
        gain : dict
            Dictionary of format {'input_serial_number': [gain_coeff, gain_exp], ...}.
        compute_time : dict
            Dictionary of format {'input_serial_number': gain_timestamp, ...}

        The ``input_serial_number`` values are unicode strings (not bytes, as in the archive)
        """

        if update_id is None:
            update_id = self.last_update_id

        gain_coeff = self.read(update_id, 'gain_coeff')
        gain_exp = self.read(update_id, 'gain_exp')
        gain_timestamp = self.read(update_id, 'compute_time')

        gain, compute_time = {}, {}
        for ii, inp in enumerate(self.axes['input']['correlator_input']):
            # str_inp = inp.decode()
            gain[inp] = [gain_coeff[:, ii], gain_exp[ii]]
            compute_time[inp] = gain_timestamp[ii]

        return gain, compute_time

    @property
    def chan_id(self):
        """Mapping between correlator input serial number and index into the input axis.

        Format is

            {correlator_input_serial_number: numeric_channel_id}

        where ``correlator_input_serial_number`` is a bytearray , and ``numeric_channel_id`` is an integer.

        """
        try:
            return self._chan_id

        except AttributeError:
            self._chan_id = {inp['correlator_input']: inp['chan_id']
                             for inp in self.axes['input']}
            return self._chan_id
