"""HDF5 writer for N-squared correlation data."""

import os
import time

import h5py
import numpy as np

from wtl import log


class HDF5CorrWriter(object):
    """ Object representing a HDF5 file containing N-squared correlation data

    Parameters:

    base_dir (str): the directory to save the HDF5 file to
    filename (str): the name of the HDF5 file
    capture_n_inputs (int or array of ints): can either be an int or an array of integers. If int, only the N first channels will be saved. 
        With an array you can specify arbitrary channels to be saved. Examples:

        - capture_n_inputs = 4 -- saves autocorrelations and crosscorrelations of channels A1, A2, A3 and A4
        -capture_n_inputs = [0, 7, 9] -- saves autocorrelations and crosscorrelations of channels A1, A8 and B2
    autocorr_only (bool): if True, only saves autocorrelation products
    elements_per_file (int): the number of elements to save per file
    n_freq_bins (int): the number of frequency bins to save per frame
    sample_freq (float): the sample frequency
    include_counts (bool): if True, include the counts in the HDF5 file
    include_sat (bool): if True, include the saturation information in the HDF5 file

    number_of_correlators (int): the number of correlators to save
    number_of_correlated_inputs (int): the number of inputs to correlate
    number_of_bins_per_frame (int): the number of bins per frame

    sample_freq (float): the sample frequency
    """
    def __init__(self,
                 base_dir='.',
                 filename='%(file_number)04d',
                 capture_n_inputs=16,
                 autocorr_only = False,
                 elements_per_file=256,
                 n_freq_bins=1024,

                 number_of_correlators = None, # has to be provided
                 number_of_correlated_inputs= None, # has to be provided
                 number_of_bins_per_frame = None, # has to be provided

                 sample_freq=800.,
                 include_counts=True,
                 include_sat=True):

        self.log = log.get_logger(self)

        self.filename = filename
        self.base_dir = base_dir
        self.capture_n_inputs = capture_n_inputs
        self.autocorr_only = autocorr_only
        self.elements_per_file = elements_per_file
        self.n_freq_bins = n_freq_bins
        self.sample_freq = sample_freq
        self.include_counts = include_counts
        self.include_sat = include_sat

        self.prod_dtype = np.dtype([('input_a', np.uint16), ('input_b', np.uint16)])
        # self.prod_axis = np.array([(i, j) for i, j in itertools.product(range(inputs_per_file), repeat=2) if i >= j],
        #                           dtype=self.prod_dtype)

        # Compute the index arrays that will build the product vector from the
        # raw correlator data for the desired inputs
        sel_cfg = self.capture_n_inputs
        self.log.debug(f'{self!r}: Starting HDF5CorrWriter. Capturing inputs/number of inpits: {self.capture_n_inputs}')

        if isinstance(sel_cfg, int):
            sel = np.arange(sel_cfg, dtype=int)
        elif isinstance(sel_cfg, (list, tuple, np.ndarray)):
            sel = np.asarray(sel_cfg, dtype=int)
        else:
            raise ValueError("capture_n_inputs must be int or a 1D list/array of ints")

        if autocorr_only:
            i0, j0 = np.diag_indices(sel.size)
        else:
            i0, j0 = np.triu_indices(sel.size)

        i = sel[i0]
        j = sel[j0]

        self.prod_axis = np.array(list(zip(i, j)), dtype=self.prod_dtype)
        self.log.debug(f'{self!r}: prod_axis: {self.prod_axis}')


        # Map to extract only subset of selected indices from all correlation products returned by firmware
        prod_idx_map = i * number_of_correlated_inputs - (i * (i - 1)) // 2 + (j - i)

        self.n_prod = len(i)

        # Pre-compute the matrix that will be used to re-index the raw data
        # vectors from the correlator into an easy-to-index matrix format.

        # self.raw_to_vector_map = CORR.get_raw_to_matrix_map()[..., i, j]
        from pychfpga.fpga_firmware.chfpga.x_engine.CORR import get_raw_corr_map

        print(f'HDF5: getting correlator mapping matrix for N={number_of_correlated_inputs}, Nbins={number_of_bins_per_frame}, Ncorr={number_of_correlators}')
        _, self.raw_to_vector_map = get_raw_corr_map(
            N=number_of_correlated_inputs,
            Nbins=number_of_bins_per_frame,
            Ncorr=number_of_correlators,
        )
        # self.raw_to_vector_map = self.raw_to_vector_map[..., i, j]  # keep only a subset of the inputs to save to disk
        self.raw_to_vector_map = self.raw_to_vector_map[..., prod_idx_map]  # keep only a subset of the inputs to save to disk

        print(f'raw_to-vector_map={self.raw_to_vector_map}')

        self.start_time = time.time()

        df = self.sample_freq / (2 * self.n_freq_bins)
        self.freq_dtype = np.dtype([('centre', np.float64), ('width', np.float64)])
        self.freq_axis = np.array([(k, df) for k in (self.sample_freq - df * np.arange(self.n_freq_bins))],
                                  dtype=self.freq_dtype)
        self.f = None
        self.file_number = 0  # current file number
        self.n_total = 0  # total number of elements written so far in all files
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
        # # create a lock file
        with open(self.lock_filename, 'w') as h:
            h.write('locked\n')

        # self.log.info(f'{self!r}: Opening raw data HDF5 file {self.filename}'
        print(f'{self!r}: Opening HDF5 file {self.filename}')
        self.f = h5py.File(self.current_filename, 'w', libver='earliest')
        self.f.attrs["instrument_name"] = "D3A"
        self.f.attrs["acquisition_name"] = "corr"
        self.f.attrs["file_name"] = self.base_dir + '/' + self.filename
        self.f.attrs["data_type"] = "correlation data"
        # We use complex128, which can store 53-bit integers exactly.
        # Complex64 offers 23-bit integers, which does not leave a lot of room
        # for integration of the incoming 18-bit data.
        self.vis = self.f.create_dataset(
            'vis', (1, 1024, self.n_prod),
            dtype=np.complex128,
            maxshape=(None, 1024, self.n_prod))
        self.vis.attrs['axis'] = ['time', 'freq', 'prod']
        if self.include_counts:
            self.counts = self.f.create_dataset(
                'counts', (1, 1024, self.n_prod),
                dtype=np.uint32,
                maxshape=(None, 1024, self.n_prod))
            self.counts.attrs['axis'] = ['time', 'freq', 'prod']
        if self.include_sat:
            self.sat = self.f.create_dataset(
                'sat', (1, 1024, self.n_prod),
                dtype=np.complex64,
                maxshape=(None, 1024, self.n_prod))
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

        # resize arrays to make room for new data
        self.uid.resize((self.n, ))
        self.time.resize((self.n, ))
        self.vis.resize((self.n, 1024, self.n_prod))
        if self.include_counts:
            self.counts.resize((self.n, 1024, self.n_prod))
        if self.include_sat:
            self.sat.resize((self.n, 1024, self.n_prod))


        current_time = time.time()
        # print(f'shapes are: raw_data {raw_data}, '
        #       f'counts {counts}, '
        #       f'sat {saturations}')

        # Store the uid, time, data, counts and saturation info
        self.uid[n1] = uid
        self.time[n1] = (integ_number, fpga_frame_number, irigb_time, current_time)
        m = self.raw_to_vector_map  # get remapping matrix
        self.vis[n1] = raw_data[m[0], m[1], m[2]]

        print(f'remapped vis[{n1},:100,0] = {self.vis[n1,:100,0].real}')
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
            # self.snapshot_index_map.resize((self.n,))
            # self.snapshot_index_map[:] = np.arange(self.n) + self.n_total
            self.log.info(f'{self!r}: Closing HDF5 file {self.current_filename}')
            self.f.close()
            self.log.debug(f'{self!r}: Closed the HDF5 file')
            try:
                os.remove(self.lock_filename)
                self.log.debug(f'{self!r}: Removed file lock')
                # os.rename(self.lock_filename, self.filename)
            except OSError:
                self.log.error(
                    f'{self!r}: Unable to rename HDF5 lock file '
                    f'from {self.lock_filename} to {self.current_filename}')


################################################
# RawAcq REST Server
################################################

