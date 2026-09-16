"""HDF5 writer for N-squared correlation data."""

import os
import time

import h5py
import numpy as np

from wtl import log


class HDF5CorrWriter(object):
    """Object representing a HDF5 file containing N-squared correlation data.

    UCORR integrations arrive pre-formatted as ``(NBINS, NPROD)`` arrays
    (``capture_engine='UCORR'``).  Legacy ICE correlators use raw
    ``(CORR, CMAC, PROD)`` arrays remapped through ``CORR.get_raw_corr_map()``.
    """
    def __init__(self,
                 base_dir='.',
                 filename='%(file_number)04d',
                 capture_n_inputs=16,
                 autocorr_only=False,
                 elements_per_file=256,
                 n_freq_bins=1024,
                 capture_engine='ICE',
                 number_of_correlators=None,
                 number_of_correlated_inputs=None,
                 number_of_bins_per_frame=None,
                 sample_freq=800.,
                 include_counts=True,
                 include_sat=True):

        self.log = log.get_logger(self)

        self.filename = filename
        self.base_dir = base_dir
        self.capture_n_inputs = capture_n_inputs
        self.autocorr_only = autocorr_only
        self.elements_per_file = elements_per_file
        self.n_freq_bins = int(n_freq_bins)
        self.capture_engine = capture_engine
        self.preformatted = capture_engine == 'UCORR'
        self.sample_freq = sample_freq
        self.include_counts = include_counts
        self.include_sat = include_sat

        if number_of_correlated_inputs is None:
            raise ValueError('number_of_correlated_inputs (NCHAN) is required')

        nchan = int(number_of_correlated_inputs)
        self.prod_dtype = np.dtype([('input_a', np.uint16), ('input_b', np.uint16)])

        sel_cfg = self.capture_n_inputs
        self.log.debug(
            f'{self!r}: Starting HDF5CorrWriter ({capture_engine}). '
            f'Capturing inputs: {self.capture_n_inputs}')

        if isinstance(sel_cfg, int):
            sel = np.arange(sel_cfg, dtype=int)
        elif isinstance(sel_cfg, (list, tuple, np.ndarray)):
            sel = np.asarray(sel_cfg, dtype=int)
        else:
            raise ValueError("capture_n_inputs must be int or a 1D list/array of ints")

        # UCORR integrations are (NBINS, NPROD) with NPROD = nchan*(nchan+1)/2; channel
        # indices in capture_n_inputs must stay within 0..nchan-1.
        if self.preformatted:
            out_of_range = sel[sel >= nchan]
            if out_of_range.size:
                self.log.warning(
                    f'{self!r}: capture_n_inputs includes channels '
                    f'{out_of_range.tolist()} but UCORR has NCHAN={nchan}; '
                    f'clipping to valid channel indices.')
                sel = sel[sel < nchan]
            if sel.size == 0:
                sel = np.arange(nchan, dtype=int)

        if autocorr_only:
            i0, j0 = np.diag_indices(sel.size)
        else:
            i0, j0 = np.triu_indices(sel.size)

        i = sel[i0]
        j = sel[j0]

        self.prod_axis = np.array(list(zip(i, j)), dtype=self.prod_dtype)
        self.log.debug(f'{self!r}: prod_axis: {self.prod_axis}')

        self.prod_idx_map = i * nchan - (i * (i - 1)) // 2 + (j - i)
        self.n_prod = len(i)

        self.raw_to_vector_map = None
        if not self.preformatted:
            if number_of_correlators is None or number_of_bins_per_frame is None:
                raise ValueError(
                    'ICE correlator HDF5 requires number_of_correlators and '
                    'number_of_bins_per_frame')
            from pychfpga.fpga_firmware.chfpga.x_engine.CORR import get_raw_corr_map

            self.log.debug(
                f'{self!r}: ICE correlator map N={nchan}, '
                f'Nbins={number_of_bins_per_frame}, Ncorr={number_of_correlators}')
            _, raw_to_vector_map = get_raw_corr_map(
                N=nchan,
                Nbins=number_of_bins_per_frame,
                Ncorr=number_of_correlators,
            )
            self.raw_to_vector_map = raw_to_vector_map[..., self.prod_idx_map]

        self.start_time = time.time()

        df = self.sample_freq / (2 * self.n_freq_bins)
        self.freq_dtype = np.dtype([('centre', np.float64), ('width', np.float64)])
        self.freq_axis = np.array(
            [(k, df) for k in (self.sample_freq - df * np.arange(self.n_freq_bins))],
            dtype=self.freq_dtype)
        self.f = None
        self.file_number = 0
        self.n_total = 0
        self.start_new_hdf5_file()

    def start_new_hdf5_file(self):
        self.close()
        self.log.debug(f'{self!r}: Starting new HDF5 File')
        fields = dict(
            file_number=self.file_number,
            elapsed_seconds=time.time() - self.start_time)

        filename = self.filename.format(**fields) % fields
        filename = os.path.join(self.base_dir, filename)
        self.open(filename)
        self.n = 0
        self.file_number += 1

    def open(self, filename):

        self.current_filename = filename
        self.lock_filename = self.current_filename + '.lock'
        with open(self.lock_filename, 'w') as h:
            h.write('locked\n')

        self.log.info(f'{self!r}: Opening HDF5 file {self.current_filename}')
        self.f = h5py.File(self.current_filename, 'w', libver='earliest')
        self.f.attrs["instrument_name"] = "D3A"
        self.f.attrs["acquisition_name"] = "corr"
        self.f.attrs["file_name"] = self.base_dir + '/' + self.filename
        self.f.attrs["data_type"] = "correlation data"
        nb = self.n_freq_bins
        self.vis = self.f.create_dataset(
            'vis', (1, nb, self.n_prod),
            dtype=np.complex128,
            maxshape=(None, nb, self.n_prod))
        self.vis.attrs['axis'] = ['time', 'freq', 'prod']
        if self.include_counts:
            if self.preformatted:
                self.counts = self.f.create_dataset(
                    'counts', (1,),
                    dtype=np.uint32,
                    maxshape=(None,))
                self.counts.attrs['axis'] = ['time']
            else:
                self.counts = self.f.create_dataset(
                    'counts', (1, nb, self.n_prod),
                    dtype=np.uint32,
                    maxshape=(None, nb, self.n_prod))
                self.counts.attrs['axis'] = ['time', 'freq', 'prod']
        if self.include_sat:
            self.sat = self.f.create_dataset(
                'sat', (1, nb, self.n_prod),
                dtype=np.complex64,
                maxshape=(None, nb, self.n_prod))
            self.sat.attrs['axis'] = ['time', 'freq', 'prod']

        self.index_map = self.f.create_group("index_map")

        self.time_dtype = np.dtype([
            ('integ_number', np.uint32),
            ('fpga_count', np.uint64),
            ('irigb_time', np.uint64),
            ('ctime', np.float64)])
        self.uid = self.index_map.create_dataset('uid', (1, ), dtype=np.uint8, maxshape=(None,))
        self.time = self.index_map.create_dataset('time', (1, ), dtype=self.time_dtype, maxshape=(None,))
        self.prod = self.index_map.create_dataset('prod', data=self.prod_axis, dtype=self.prod_dtype)
        self.freq = self.index_map.create_dataset('freq', data=self.freq_axis, dtype=self.freq_dtype)
        self.n = 0

    def write(self, uid, integ_number, fpga_frame_number, irigb_time, raw_data, counts, saturations):
        self.log.debug(f'{self!r}: Writing data to the file')
        n1 = self.n
        self.n += 1
        self.n_total += 1

        nb = self.n_freq_bins
        self.uid.resize((self.n, ))
        self.time.resize((self.n, ))
        self.vis.resize((self.n, nb, self.n_prod))
        if self.include_counts:
            if self.preformatted:
                self.counts.resize((self.n, ))
            else:
                self.counts.resize((self.n, nb, self.n_prod))
        if self.include_sat:
            self.sat.resize((self.n, nb, self.n_prod))

        current_time = time.time()
        self.uid[n1] = uid
        self.time[n1] = (integ_number, fpga_frame_number, irigb_time, current_time)
        if self.preformatted:
            self.vis[n1] = raw_data[:, self.prod_idx_map]
            if self.include_counts:
                self.counts[n1] = counts
            if self.include_sat:
                self.sat[n1] = saturations[:, self.prod_idx_map]
        else:
            m = self.raw_to_vector_map
            self.vis[n1] = raw_data[m[0], m[1], m[2]]
            if self.include_counts:
                self.counts[n1] = counts[m[0], m[1]]
            if self.include_sat:
                self.sat[n1] = saturations[m[0], m[1], m[2]]

        if self.n >= self.elements_per_file:
            self.start_new_hdf5_file()
        self.log.debug(f'{self!r}: Data write succeded')

    def close(self):
        self.log.debug(f'{self!r}: Closing file')
        if self.f:
            self.log.info(f'{self!r}: Closing HDF5 file {self.current_filename}')
            self.f.close()
            self.log.debug(f'{self!r}: Closed the HDF5 file')
            try:
                os.remove(self.lock_filename)
                self.log.debug(f'{self!r}: Removed file lock')
            except OSError:
                self.log.error(
                    f'{self!r}: Unable to rename HDF5 lock file '
                    f'from {self.lock_filename} to {self.current_filename}')
