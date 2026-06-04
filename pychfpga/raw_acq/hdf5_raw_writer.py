"""HDF5 writer for raw ADC timestream snapshots."""

import os
import time

import h5py
import numpy as np

from wtl import log


class HDF5RawWriter(object):
    """ Object representing a HDF5 file containing raw data
    """
    def __init__(self,
                 base_dir='.',
                 filename='%(file_number)06d.h5',
                 elements_per_file=2048 * 64,
                 crate_and_slot_from_port=False,
                 chunk_size=1024,
                 storage_dtype='i1',
                 samples_per_frame=None,
                 nbytes_per_frame=None):
        self.log = log.get_logger(self)
        if samples_per_frame is None:
            if nbytes_per_frame is None:
                raise ValueError("samples_per_frame is required")
            samples_per_frame = int(nbytes_per_frame)
            storage_dtype = 'i1'
        self.storage_dtype = np.dtype(storage_dtype)
        self.N_SAMP = int(samples_per_frame)
        self.base_dir = base_dir
        self.filename = filename
        self.chunk_size = chunk_size
        # self.N_CHANNELS = 1
        self.crate_and_slot_from_port = crate_and_slot_from_port
        # self.filename = filestring
        self.file_number = 0
        self.nn = 0  # sample number of the first sample of the current file
        self.elements_per_file = elements_per_file
        self.f = None
        self.start_new_hdf5_file()

    def start_new_hdf5_file(self):
        self.close()
        fields = dict(
            file_number=self.file_number)

        filename = os.path.join(self.base_dir, self.filename.format(**fields) % fields)
        self.open(filename)
        self.n = 0
        self.file_number += 1

    def open(self, filename):
        self.current_filename = filename
        self.lock_filename = self.current_filename + '.lock'

        # # create a lock file
        with open(self.lock_filename, 'w') as h:
            h.write('locked\n')

        self.log.info(f'{self!r}: Opening raw data HDF5 file {self.current_filename}')
        self.f = h5py.File(self.current_filename, 'w', libver='earliest')
        self.f.attrs["git_version_tag"] = "0.1"
        self.f.attrs["system_user"] = "root"
        self.f.attrs["collection_server"] = "hostname"
        self.f.attrs["instrument_name"] = "CHIME"
        self.f.attrs["acquisition_name"] = "rawadc"
        self.f.attrs["archive_version"] = "2.4.0"
        self.f.attrs["file_name"] = self.current_filename  # was filestring
        self.f.attrs["data_type"] = "ADC snapshot data"
        self.f.attrs["rawadc_version"] = 0.1
        self.f.attrs["storage_dtype"] = self.storage_dtype.str
        self.f.attrs["samples_per_frame"] = self.N_SAMP
        self.f.attrs["timestamping_warning"] = (
            "Done on file write, may be significantly "
            "different from snapshot acquistion time")

        # timestamp
        self.compound_dtype = np.dtype([('fpga_count', np.uint64), ('ctime', np.float64)])
        self.timestampDataset = self.f.create_dataset(
            'timestamp', (1, 1), dtype=self.compound_dtype,
            maxshape=(None, 1), chunks=(self.chunk_size, 1))
        self.timestampDataset.attrs['axis'] = ['snapshot']

        # slot number
        self.slotDataset = self.f.create_dataset(
            'slot', (1, 1), dtype=np.uint8,
            maxshape=(None, 1), chunks=(self.chunk_size, 1))
        self.slotDataset.attrs['axis'] = ['snapshot']

        # crate number
        self.crateDataset = self.f.create_dataset(
            'crate', (1, 1), dtype=np.uint32,
            maxshape=(None, 1), chunks=(self.chunk_size, 1))
        self.crateDataset.attrs['axis'] = ['snapshot']

        # channel number
        self.chanDataset = self.f.create_dataset(
            'adc_input', (1, 1), dtype=np.uint8,
            maxshape=(None, 1), chunks=(self.chunk_size, 1))
        self.chanDataset.attrs['axis'] = ['snapshot']

        # ADC data
        self.timestreamDataset = self.f.create_dataset(
            'timestream', (1, self.N_SAMP), dtype=self.storage_dtype,
            maxshape=(None, self.N_SAMP), chunks=(self.chunk_size, self.N_SAMP))
        self.timestreamDataset.attrs['axis'] = ['snapshot', 'timestream']

        self.index_map = self.f.create_group("index_map")

        self.snapshot_index_map = self.index_map.create_dataset(
            'snapshot', (1,), dtype=np.uint32,
            maxshape=(None,), chunks=(self.chunk_size,))

        self.timestream_index_map = self.index_map.create_dataset(
            "timestream", (self.N_SAMP,), dtype=np.uint16)
        self.timestream_index_map[:] = np.arange(self.N_SAMP)

        # self.n_times = 1
        self.n = 0  # number of samples fince start of file

    def write(self, timestamp, stream_id, flags, timestream):
        """
        """
        n1 = self.n
        self.n = n2 = n1 + timestamp.shape[0]

        # self.log.info(f'{self!r}: Writing {timestamp.size} entries to HDF5 file {self.current_filename}')

        self.timestampDataset.resize((self.n, 1))
        self.crateDataset.resize((self.n, 1))
        self.slotDataset.resize((self.n, 1))
        self.chanDataset.resize((self.n, 1))
        self.timestreamDataset.resize((self.n, self.N_SAMP))

        current_time = time.time()
        slot_number = (stream_id >> 4) & 0xF
        crate_number = (stream_id >> 8) & 0xF
        chan_number = (stream_id) & 0xF

        # we have to build a compound array to assign elements to it using the
        # field names. Doing that directly on the dataset does nothing.

        ts = np.empty(timestamp.shape, dtype=self.compound_dtype)  # memory allocation! might not be efficient!
        ts['fpga_count'] = timestamp
        ts['ctime'] = current_time
        self.timestampDataset[n1:n2, 0] = ts
        # print('ts=', ts)

        self.chanDataset[n1:n2, 0] = chan_number
        self.slotDataset[n1:n2, 0] = slot_number
        self.crateDataset[n1:n2, 0] = crate_number
        self.timestreamDataset[n1:n2] = np.asarray(timestream, dtype=self.storage_dtype)

        if n2 >= self.elements_per_file:
            self.start_new_hdf5_file()

    def close(self):
        if self.f:
            self.snapshot_index_map.resize((self.n,))
            self.snapshot_index_map[:] = np.arange(self.n) + self.nn
            self.nn += self.n

            self.log.info(f'{self!r}: Closing HDF5 file {self.current_filename}')
            self.f.close()
            try:
                os.remove(self.lock_filename)
                # os.rename(self.lock_filename, self.filename)
            except OSError:
                self.log.error(
                    f'{self!r}: Unable to rename HDF5 lock file '
                    f'from {self.lock_filename} to {self.current_filename}')


#######################################################
#######################################################
#######################################################
# Correlator packet processor
#######################################################
#######################################################
#######################################################


