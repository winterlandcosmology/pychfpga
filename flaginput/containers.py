import os
import datetime

import numpy as np
import h5py

from ch_util.ephemeris import datetime_to_unix

from pychfpga import NameSpace, load_yaml_config
from pychfpga import Hdf5Archive, Hdf5Writer

from version import __version__

DEFAULTS = NameSpace(load_yaml_config(os.path.join(os.path.dirname(os.path.realpath(__file__)),
                                                   'defaults.yaml') + ':flaginput'))

class FlagCorrInputArchive(Hdf5Archive):
    """ Interface to an Hdf5Archive containing correlator input flags.
    """

    _uniq_id = 'datetime'
    _grow_ax = 'time'

    _axes = {
        'time': {'dtype': np.float64},
        'source': {'dtype': str},
        'input': {'dtype': str},
    }

    _dataset_spec = {
        'datetime': {
            'axes': ['time', ],
            'dtype': h5py.special_dtype(vlen=bytes),
            'metric': False,
        },
        'source_flags': {
            'axes': ['time', 'source', 'input'],
            'dtype': np.bool,
            'metric': True,
        },
        'flag': {
            'axes': ['time', 'input'],
            'dtype': np.bool,
            'metric': True,
        }
    }

    def __init__(self, output_dir=DEFAULTS.output_dir, output_suffix=DEFAULTS.output_suffix,
                       instrument=DEFAULTS.correlator, combine=None, *args, **kwargs):
        """ Instantiates a FlagCorrInputArchive object.

        Parameters
        ----------
        output_dir:  str
            Directory where the hdf5 archive files will be saved.

        output_suffix: str
            Suffix appended to the hdf5 archive filenames.

        instrument:  str
            Name of the instrument/correlator.  Included in hdf5 archive filenames,
            and also saved to file attributes.

        combine: list
            List of sources that are combined to produce the master flag.
        """

        # Call superclass
        super(FlagCorrInputArchive, self).__init__(*args, **kwargs)

        # Set parameters that specify output file format
        self.output_dir = output_dir
        self.output_suffix = output_suffix

        # Set attributes
        attrs = {'instrument':instrument, 'version':__version__}
        if combine is not None:
            attrs['combine'] = np.array(combine)

        self.set_attrs(**attrs)

        # Set metric name
        self._metric_name = 'flaginput'


    def get_output_file(self, smp, **kwargs):
        """ Defines the filenaming conventions for the archive files:

            {output_dir}/{YYYYMMDD}T{HHMMSS}Z_{instrument}_{output_suffix}/{SSSSSSS}.h5

        Parameters
        ----------
        smp: unix time
            Time at which the datasets in kwargs were collected.

        datetime: str
            Datetime string {YYYYMMDD}T{HHMMSS}Z indicating the time the flags
            were updated.  Used in the archive file directory name.
        """

        # Determine directory
        if not 'datetime' in kwargs:
            RuntimeError("Must include datetime in call to write.")

        output_dir = os.path.join(self.output_dir, '_'.join([kwargs['datetime'], self.attrs['instrument'], self.output_suffix]))
        mkdir(output_dir)

        # Determine filename
        start_time = datetime_to_unix(datetime.datetime.strptime(kwargs['datetime'], "%Y%m%dT%H%M%SZ"))
        seconds_elapsed = smp - start_time

        output_file = os.path.join(output_dir, "%07d.h5" % seconds_elapsed)

        return output_file


class FlagRawWriter(Hdf5Writer):
    """ Interface to an Hdf5Writer containing flags derived
    from raw adc data (and associated data products).
    """

    _uniq_id = 'filename'
    _grow_ax = 'time'

    _axes = {
        'time': {'dtype': np.float64},
        'input': {'dtype': str},
        'lsb': {'dtype': np.int8},
        'freq': {'dtype': np.float32, 'binsize':4}
    }

    _dataset_spec = {
        'filename': {
            'axes': ['time', ],
            'dtype': h5py.special_dtype(vlen=bytes),
            'metric': False,
        },
        'histogram_threshold': {
            'axes': ['time', ],
            'dtype': np.float32,
            'metric': True,
        },
        'histogram_template': {
            'axes': ['time', 'lsb'],
            'dtype': np.float32,
            'metric': False,
        },
        'spectrum_threshold': {
            'axes': ['time', ],
            'dtype': np.float32,
            'metric': True,
        },
        'spectrum_template': {
            'axes': ['time', 'freq'],
            'dtype': np.float32,
            'metric': True,
        },
        'nframe': {
            'axes': ['time', 'input'],
            'dtype': np.int16,
            'metric': True,
        },
        'mean': {
            'axes': ['time', 'input'],
            'dtype': np.float32,
            'metric': False,
        },
        'rms': {
            'axes': ['time', 'input'],
            'dtype': np.float32,
            'metric': False,
        },
        'skew': {
            'axes': ['time', 'input'],
            'dtype': np.float32,
            'metric': True,
        },
        'kurtosis': {
            'axes': ['time', 'input'],
            'dtype': np.float32,
            'metric': True,
        },
        'snr': {
            'axes': ['time', 'input'],
            'dtype': np.float32,
            'metric': True,
        },
        'histogram': {
            'axes': ['time', 'input', 'lsb'],
            'dtype': np.float32,
            'metric': False,
        },
        'histogram_corr_coeff': {
            'axes': ['time', 'input'],
            'dtype': np.float32,
            'metric': True,
        },
        'histogram_fail': {
            'axes': ['time', 'input'],
            'dtype': np.bool,
            'metric': True,
        },
        'spectrum': {
            'axes': ['time', 'input', 'freq'],
            'dtype': np.float32,
            'metric': True,
        },
        'spectrum_corr_coeff': {
            'axes': ['time', 'input'],
            'dtype': np.float32,
            'metric': True,
        },
        'spectrum_fail': {
            'axes': ['time', 'input'],
            'dtype': np.bool,
            'metric': True,
        },
        'weight': {
            'axes': ['time', 'input'],
            'dtype': np.float32,
            'metric': True,
        },
        'classification': {
            'axes': ['time', 'input'],
            'dtype': np.float32,
            'metric': False,
        }
    }

    def __init__(self, output_dir=DEFAULTS.raw.output_dir, output_suffix=DEFAULTS.raw.output_suffix,
                       instrument=DEFAULTS.correlator, *args, **kwargs):
        """ Instantiates a FlagRawWriter object.

        Parameters
        ----------
        output_dir:  str
            Directory where the hdf5 archive files will be saved.

        output_suffix: str
            Suffix appended to the hdf5 archive filenames.

        instrument:  str
            Name of the instrument/correlator.  Included in hdf5 archive filenames,
            and also saved to file attributes. (Default 'chime')
        """

        # Call superclass
        super(FlagRawWriter, self).__init__(*args, **kwargs)

        # Set parameters that specify output file format
        self.output_dir = output_dir
        self.output_suffix = output_suffix

        # Set attributes
        self.set_attrs(**{'instrument':instrument, 'version':__version__})

        # Set metric name
        self._metric_name = 'rawflag'


    def get_output_file(self, smp, **kwargs):
        """ Defines the filenaming conventions for the archive files:

            {output_dir}/{YYYYMMDD}T{HHMMSS}Z_{instrument}_{output_suffix}/{SSSSSSS}.h5

        Parameters
        ----------
        smp: unix time
            Time at which the datasets in kwargs were collected.

        filename: str
            Full path to the current raw acquisition file.  The timestamp from the
            raw acquisition directory name is used in the archive file directory name.
        """

        # Determine directory
        if not 'filename' in kwargs:
            RuntimeError("Must include raw acquisition filename in call to write.")

        base_prefix = os.path.basename(os.path.dirname(kwargs['filename']))[0:16]
        output_dir = os.path.join(self.output_dir, '_'.join([base_prefix, self.attrs['instrument'], self.output_suffix]))
        mkdir(output_dir)

        # Determine filename
        start_time = datetime_to_unix(datetime.datetime.strptime(base_prefix, "%Y%m%dT%H%M%SZ"))
        seconds_elapsed = smp - start_time

        output_file = os.path.join(output_dir, "%07d.h5" % seconds_elapsed)

        return output_file


class ControlFlag(object):
    """ Container for flags that requires consecutive good or bad
    values in order for the flag to change.
    """

    def __init__(self, num_consecutive_bad=1, num_consecutive_good=8):
        """
        Parameters
        ----------
        num_consecutive_bad : int
            If a flag is True, then there must be this number of
            consecutive Falses for the flag to change to False.
            Default is 1.

        num_consecutive_good : int
            If a flag is False, then there must be this number of
            consecutive Trues for the flag to change to True.
            Default is 8.
        """

        self._flag = None
        self._count = None

        self.num_consecutive_bad = num_consecutive_bad
        self.num_consecutive_good = num_consecutive_good

    def update(self, val):

        # Make sure input is a boolean numpy array
        flag = np.array(val).astype(np.bool)

        # If this is the first update, then create internal
        # flag and counter
        if self._flag is None:

            self._flag = flag
            self._count = np.zeros(flag.size, dtype=np.int)
            return

        # Check if any of the flags have changed
        good_to_bad = np.flatnonzero(self._flag & ~flag)
        bad_to_good = np.flatnonzero(~self._flag & flag)

        # Increase or decrease the counter for the flags that have changed
        if good_to_bad.size > 0:
            self._count[good_to_bad] -= 1

        if bad_to_good.size > 0:
            self._count[bad_to_good] += 1

        # Set the counter to zero for flags that have not changed
        no_change = np.flatnonzero(self._flag == flag)

        if no_change.size > 0:
            self._count[no_change] = 0

        # Compare the count to the user specified limits
        now_bad = np.flatnonzero(self._count <= -self.num_consecutive_bad)
        if now_bad.size > 0:
            self._flag[now_bad] = False

        now_good = np.flatnonzero(self._count >= self.num_consecutive_good)
        if now_good.size > 0:
            self._flag[now_good] = True

    @property
    def flag(self):
        return self._flag