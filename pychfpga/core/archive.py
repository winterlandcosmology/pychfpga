import os
import subprocess
import collections
import threading
import Queue
import numpy as np
import h5py
import re

from abc import ABCMeta, abstractmethod
from pychfpga import Metrics

MAX_FILE_SIZE = 4000000000          # in bytes

class abstract_attribute(object):
    """ Class that enables the specification of abstract attributes.
    Source: https://stackoverflow.com/questions/32536176/
    """
    def __get__(self, obj, type):
        # Now we will iterate over the names on the class,
        # and all its superclasses, and try to find the attribute
        # name for this descriptor
        # traverse the parents in the method resolution order
        for cls in type.__mro__:
            # for each cls thus, see what attributes they set
            for name, value in cls.__dict__.items():
                # we found ourselves here
                if value is self:
                    # if the property gets accessed as Child.variable,
                    # obj will be done. For this case
                    # If accessed as a_child.variable, the class Child is
                    # in the type, and a_child in the obj.
                    this_obj = obj if obj else type

                    raise NotImplementedError(
                         "%r does not have the attribute %r "
                         "(abstract from class %r)" %
                             (this_obj, name, cls.__name__))

        # we did not find a match, should be rare, but prepare for it
        raise NotImplementedError(
            "%s does not set the abstract attribute <unknown>", type.__name__)


def convert_camel_case(name):
    s1 = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', name)
    return re.sub('([a-z0-9])([A-Z])', r'\1_\2', s1).lower()


def rlock(func):
    """Decorator that acquires a re-entrant lock stored in self before
    running the function it wraps and releases the lock on completion.
    """
    def locked(self, *args, **kwargs):
        with self._rlock: return func(self, *args, **kwargs)
    return locked


class Hdf5Writer(object):
    """Class that interfaces to an hdf5 file and enables safe read/write access.
    Writes must append to datasets along a single axis.

    Abstract Attributes
    -------------------
    Any subclass of Hdf5Writer must define these attributes:

    _axes: {axis name: {'dtype': dtype}, ...}
        Recursive dictionary that specifies the axes that the hdf5 file will contain
        and their properties.  Properties must include 'dtype'.

    _dataset_spec:  {dataset name: {'axes': [axis1, axis2, ...], 'dtype': dtype}, ...}
        Recursive dictionary that specifies the datasets that the hdf5 file will contain
        and their properties.  Properties must include 'axes' and 'dtype'.

    _grow_ax: str
        Name of the axis of the h5py file to which new samples will be appended.
        This will most likely be 'time'.

    _uniq_id: str
        Name of the dataset in the h5py file that acts as a unique identifier for each sample.

    Attributes
    ----------
    grow : {'axis':[smp1, smp2, ... ], 'index':[(rindex, sindex), ...]}
        Map between grow axis value and the corresponding (reader index, sample index).

    search : {uniq id: (reader index, grow index), ...}
        Map betweeen unique identifier and the corresponding (reader index, sample index).

    Properties
    ----------
    datasets
    index_map
    archive_files
    writer_index

    Methods
    -------
    create_writer
    write
    flush_writer
    close_writer
    read
    read_all
    get
    close_all
    set_attrs
    get_metrics
    dump

    Abstract Methods
    ----------------
    Any subclass of Hdf5Writer must define these methods:

    get_output_file

    """

    __metaclass__ = ABCMeta

    _uniq_id = abstract_attribute()
    _grow_ax = abstract_attribute()

    _axes = abstract_attribute()
    _dataset_spec = abstract_attribute()

    _with_lock_file = True

    def __init__(self, output_file=None, max_file_size=MAX_FILE_SIZE, memory_size=0, max_num=None):
        """ Instantiates an Hdf5Writer.

        Parameters
        ----------
        output_file: str
            Name of HDF5 file to append to.
        """

        # Initialize variables
        self.iam = True
        self.ind = 0
        self.num = 0
        self.writer = None

        self.in_memory = []
        self.memory = {}

        self.reader = []
        self.search = {}
        self.grow = {'axis':[], 'index':[]}

        self.attrs = {}
        self.index_map = {}

        self._rlock = threading.RLock()

        self._max_file_size = max_file_size
        self._max_num = max_num if max_num is not None else float('inf')

        self._with_memory = memory_size

        self._metric_name = convert_camel_case(self.__class__.__name__)

        # If output_file provided, then add writer
        if output_file is not None:
            self.create_writer(output_file)


    def create_writer(self, output_file, **kwargs):

        # Open output file
        if not os.path.isfile(output_file):

            # If requested, acquire lock file.
            if self._with_lock_file:
                self.acquire_lock_file(output_file)

            # File does not exist, so create it.
            self.writer = h5py.File(output_file, 'w', libver='latest')

            # Add attributes
            self.attrs['acquisition_name'] = os.path.basename(os.path.dirname(output_file))

            for key, value in self.attrs.iteritems():
                self.writer.attrs[key] = value

            # Create index map
            self.writer.create_group('index_map')
            for name, dct in self._axes.iteritems():
                if name == self._grow_ax:
                    self._index_map.create_dataset(name, (1, ), maxshape=(None, ), dtype=dct['dtype'])

                elif name in kwargs:
                    self._index_map.create_dataset(name, data=kwargs[name])

                else:
                    RuntimeError("Must supply all non-growing axis at file creation, please specify %s." % name)

            # Set index map
            self._set_index_map()

            # Create datasets
            for name in self._dataset_spec.keys():
                self._add_dataset(name)

            # Set dimensions
            self.num = 1
            self.ind = 0

        else:

            # If requested, acquire lock file.
            if self._with_lock_file:
                self.acquire_lock_file(output_file)

            # File already exists, so open it.
            self.writer = h5py.File(output_file, 'r+', libver='latest')

            # Check version number
            if ('version' in self.attrs) and (self.writer.attrs['version'] != self.attrs['version']):
                ValueError("Code is version %s, file is version %s." % (self.writer.attrs['version'], self.attrs['version']))

            # Set index map
            self._set_index_map()

            # Set dimensions
            self.num = self.index_map[self._grow_ax].size
            self.ind = self.num

            # Update searchable axes
            rr = len(self.reader)

            for kk, key in enumerate(self.writer[self._uniq_id][:]):
                self.search[key] = (rr, kk)

            # Set growing axis
            tmp = self.index_map[self._grow_ax][:]
            self.grow['axis'] += list(tmp)
            self.grow['index'] += zip(np.repeat(rr, tmp.size), np.arange(tmp.size, dtype=np.int))

        # Add to readers
        self.reader.append(self.writer)

        # Initialize memory
        self._initialize_memory()
        self._load_into_memory()


    def _add_dataset(self, name):

        # Extract specifications for this dataset from class attribute
        dspec = self._dataset_spec[name]

        axes = dspec['axes']
        dtype = dspec['dtype']

        # Check that all the specified axes are defined, and fetch their lengths
        shape, maxshape = (), ()
        for axis in axes:
            if axis == self._grow_ax:
                l = 1
                m = None
            else:
                l = len(self.index_map[axis])
                m = l

            shape += (l,)
            maxshape += (m, )

        # Create dataset
        dset = self.writer.create_dataset(name, shape, maxshape=maxshape, dtype=dtype)

        # Add axis attribute
        dset.attrs['axis'] = np.array(axes)

        # Return link to dataset
        return dset


    def write(self, smp, **kwargs):

        # Check if file does not exist or has reached maximum size
        if (self.writer is None) or (self.writer.id.get_filesize() >= self._max_file_size) or (self.ind >= self._max_num):

            # Determine new filename using an abstracted method
            output_file = self.get_output_file(smp, **kwargs)

            # Extract now-grow axes from kwargs or current file
            index_map = {}
            for axis in self._axes:
                if axis != self._grow_ax:
                    if axis in kwargs:
                        index_map[axis] = kwargs[axis]
                    else:
                        if axis in self.index_map:
                            index_map[axis] = self.index_map[axis][:]
                        else:
                            RuntimeError("Must include non-grow axes in initial call to write.")

            # Take the existing file out of write mode
            self.close_writer()

            # Create new file
            self.create_writer(output_file, **index_map)

        # Expand grow axis of all datasets by 1 sample
        if self.ind == self.num:

            # Lock access to the file during resize
            with self._rlock:

                self.num = self.ind + 1

                self._index_map[self._grow_ax].resize((self.num, ))
                for name in self.datasets:
                    is_grow = np.flatnonzero(self.writer[name].attrs['axis'] == self._grow_ax)
                    if is_grow.size > 0:
                        shp = np.array(self.writer[name].shape)
                        shp[is_grow] = self.num
                        self.writer[name].resize(tuple(shp))

        elif self.ind < self.num:
            pass

        else:
            ValueError("Samples out of sync.")

        # Lock access to the file during write
        with self._rlock:

            index = (self.writer_index, self.ind)

            # Append this sample to the end of the datasets
            self._index_map[self._grow_ax][self.ind] = smp
            self._set_index_map()

            for key in self.datasets:
                value = kwargs.get(key, None)
                if value is not None:

                    self.writer[key][self.ind] = value

                    if key in self.memory:
                        self.memory[key] = np.roll(self.memory[key], -1, axis=0)
                        self.memory[key][-1] = value

            # Update searchable axes
            uniq_id = self.writer[self._uniq_id][self.ind]
            self.search[uniq_id] = index

            # Update grow axis
            self.grow['axis'].append(smp)
            self.grow['index'].append(index)

            # Update memory contents
            if self.in_memory:
                self.in_memory = self.in_memory[1:] + [index]

            # Flush to disk
            self.flush_writer()

            # Increment counter
            self.ind += 1


    @rlock
    def flush_writer(self):

        if self.writer:

            filename = self.writer.id.name
            rr = self.writer_index

            # Close the file to ensure data is flushed to disk
            self.writer = self.writer.close()

            # Reopen the file in write mode
            self.writer = h5py.File(filename, 'r+', libver='latest')
            self.reader[rr] = self.writer


    @rlock
    def close_writer(self):

        if self.writer:

            # Save filename and reader index
            filename = self.writer.id.name
            rr = self.writer_index

            # Reset writer counters
            self.ind = 0
            self.num = 0

            # Close the file
            self.writer = self.writer.close()

            # Release the lock file
            if self._with_lock_file:
                self.release_lock_file(filename)

            # Reset utilities for reading
            self.search = {key:val for key, val in self.search.iteritems() if val[0] != rr}

            grow = {'axis':[], 'index':[]}
            for smp, ind in zip(self.grow['axis'], self.grow['index']):
                if ind[0] != rr:
                    grow['axis'].append(smp)
                    grow['index'].append(ind)
            self.grow = grow

            self.reader.pop(rr)

            # Reset memory
            self._initialize_memory()
            self._load_into_memory()


    def read(self, key, dataset):

        if isinstance(key, tuple):
            index = key
        else:
            index = self[key]

        if index is not None:
            if index in self.in_memory:
                return self.memory[dataset][self.in_memory.index(index)]

            else:
                with self._rlock:
                    return self.reader[index[0]][dataset][index[1]]

        else:
            return None


    @rlock
    def read_all(self, dataset):

        return np.concatenate(tuple([rd[dataset][:] for rd in self.reader]), axis=0)


    @rlock
    def close_all(self):

        self.iam = False

        # Close out h5py files
        while self.reader:
            reader = self.reader.pop()
            reader.close()

        if self.writer:
            self.writer = self.writer.close()

        # Empty memory
        self.memory = {}
        self.in_memory = []

        # Empty index map and attributes
        self.index_map = {}
        self.attrs = {}

        # Reset search and grow axis
        self.search = {}
        self.grow = {'axis':[], 'index':[]}

        self.ind = 0
        self.num = 0


    def _initialize_memory(self):

        if self._with_memory:

            # Create empty buffer for this dataset
            for name, dspec in self._dataset_spec.iteritems():

                axes = dspec['axes']
                dtype = dspec['dtype']

                memshape = ()
                for axis in axes:
                    b = self._with_memory if axis == self._grow_ax else len(self._index_map[axis])
                    memshape += (b, )

                self.memory[name] = np.zeros(memshape, dtype=dtype)

            # Create list for lookup
            self.in_memory = [None] * self._with_memory


    def _load_into_memory(self):

        for kk in range(self._with_memory):

            rr = -(kk + 1)
            try:
                index = self.grow['index'][rr]
            except IndexError:
                break

            for dset in self._dataset_spec.keys():
                self.memory[dset][rr] = self.reader[index[0]][dset][index[1]]

            self.in_memory[rr] = index

    @rlock
    def check_memory(self):

        check = {}

        for dset, mem in self.memory.iteritems():

            check[dset] = {'match':0, 'total':0}

            for ii, index in enumerate(self.in_memory):

                if index is not None:

                    match = int(np.all(self.reader[index[0]][dset][index[1]] == mem[ii]))

                    check[dset]['match'] += match
                    check[dset]['total'] += 1

        return check

    def set_attrs(self, **kwargs):

        # Include important attributes
        # that we want all archive files to have
        if not self.attrs:
            self.attrs['type'] = str(type(self))
            self.attrs['git_version_tag'] = subprocess.check_output(["git", "-C", os.path.dirname(__file__), "describe", "--always"]).strip()
            self.attrs['collection_server'] = subprocess.check_output(["hostname"]).strip()
            self.attrs['system_user'] = subprocess.check_output(["id", "-u", "-n"]).strip()

        # Save input attributes
        for key, value in kwargs.iteritems():
            self.attrs[key] = value


    def dump(self, output_file, uniq_id=None, timestamp=None, datasets=None):
        """ Dump a single timestamp to a separate HDF5 file.
        """

        if uniq_id is not None:
            timestamp = self.grow_axis(uniq_id)

        if timestamp is None:
            with self._rlock:
                timestamp = self.grow['axis'][-1]

        if datasets is not None:
            datasets = [dset for dset in datasets if dset in self.datasets]
        else:
            datasets = self.datasets

        axes = []
        for name in datasets:
            axes += self._dataset_spec[name]['axes']
        axes = [ax for ax in set(axes) if ax != self._grow_ax]

        with h5py.File(output_file, 'w', libver='latest') as fdump:

            # Copy attributes
            for key, value in self.attrs.iteritems():
                fdump.attrs[key] = value

            # Add timestamp to attributes
            fdump.attrs[self._grow_ax] = timestamp

            # Copy index map
            index_map = fdump.create_group('index_map')
            for name in axes:
                index_map.create_dataset(name, data=self.index_map[name])

            # Copy datasets for this timesample
            for name in datasets:
                data = self.read(timestamp, name)
                if np.isscalar(data):
                    fdump.attrs[name] = data
                else:
                    fdump.create_dataset(name, data=data)


    def get_metrics(self, timestamp=None, **kwargs):

        if self._grow_ax != 'time':
            ValueError('Function get_metrics is only compatible with time growing archives.')

        if timestamp is None:
            timestamp = self.grow['axis'][-1]

        metrics = Metrics(default_type='gauge')
        if self.writer:
            # Read in the index_map
            index_map = {}
            index_lbl = {}
            for name, value in self.index_map.iteritems():
                if name != self._grow_ax:
                    if name in kwargs:
                        new_labels = kwargs[name](value)

                        index_lbl[name] = sorted(new_labels.keys())
                        for lbl in index_lbl[name]:
                            index_map[lbl] = new_labels[lbl]

                    elif value.dtype.fields is None:
                        index_lbl[name] = [name]
                        index_map[name] = value[:]

                    else:
                        fields = value.dtype.fields.keys()
                        index_lbl[name] = fields
                        for field in fields:
                            index_map[field] = value[field][:]

            # Loop over datasets
            for name, dspec in self._dataset_spec.iteritems():

                # Check to see if we are saving this metric
                if dspec['metric']:

                    # Read in the data for latest timesample
                    results = self.read(timestamp, name)
                    axes = [ax for ax in dspec['axes'] if ax != self._grow_ax]

                    # Multidimensional loop over array and add element to metrics container
                    for index, res in np.ndenumerate(results):

                        # Construct labels
                        labels = {}
                        for dim, ind in enumerate(index):
                            for lbl in index_lbl[axes[dim]]:
                                labels[lbl] = index_map[lbl][ind]

                        # Add to metrics
                        metrics.add('_'.join([self._metric_name, name]), value=res, time=timestamp*1000, **labels)

        return metrics


    def acquire_lock_file(self, output_file):

        lock_file = output_file + '.lock'

        if not os.path.isfile(lock_file):
            with open(lock_file,  'w') as lofi:
                lofi.write('locked\n')

        # Switch to the lock file format below, but leave the
        # original version above in place until we fix
        # downstream code (theremin)
        lock_file = os.path.join(os.path.dirname(output_file),
                           '.' + os.path.basename(output_file) + '.lock')

        if os.access(lock_file, os.F_OK):
            with open(lock_file, 'r') as lofi:
                lofi.seek(0)
                old_pid = int(lofi.readline())

            if old_pid == os.getpid():
                return
            elif os.path.isdir('/proc/%d' % old_pid):
                RuntimeError("%s is already locked by process %d." % (output_file, old_pid))
            else:
                try:
                    os.remove(lock_file)
                except OSError:
                    pass

        with open(lock_file, 'w') as lofi:
            lofi.write('%d' % os.getpid())


    def release_lock_file(self, output_file):

        lock_file = output_file + '.lock'

        try:
            os.remove(lock_file)
        except OSError:
            pass

        # Switch to the lock file format below, but leave the
        # original version above in place until we fix
        # downstream code (theremin)
        lock_file = os.path.join(os.path.dirname(output_file),
                           '.' + os.path.basename(output_file) + '.lock')
        try:
            os.remove(lock_file)
        except OSError:
            pass


    def __nonzero__(self):

        return bool(self.reader)


    def __len__(self):

        return self.grow['axis'].size


    def __getitem__(self, key):

        if isinstance(key, basestring):

            idd = self.search.get(key, None)

        elif isinstance(key, (float, int, long)):

            delta = key - np.array(self.grow['axis'])
            ipos = np.flatnonzero(delta >= 0.0)

            idd = None if ipos.size == 0 else self.grow['index'][ipos[np.argmin(delta[ipos])]]

        else:

            idd = None

        return idd


    def __contains__(self, item):

        return (item in self.search)


    def grow_axis(self, key):

        if isinstance(key, tuple):
            idd = key
        else:
            idd = self[key]

        return self.grow['axis'][self.grow['index'].index(idd)] if idd is not None else None


    @property
    def _index_map(self):
        """ index_map for writing (not thread-safe).
        """
        return self.writer['index_map']

    @rlock
    def _set_index_map(self):
        """ set the index_map for reading (thread-safe).
        """
        self.index_map = {key:value[:] for key, value in self._index_map.iteritems()}

    @property
    def last_update(self):
        return self.grow['axis'][-1]

    @abstractmethod
    def get_output_file(self, *args, **kwargs):
        return

    @property
    @rlock
    def datasets(self):
        return [name for name, item in self.writer.iteritems() if isinstance(item, h5py.Dataset)]

    @property
    @rlock
    def archive_files(self):
        return [rd.id.name for rd in self.reader]

    @property
    @rlock
    def current_file(self):
        return self.writer.id.name if self.writer else None

    @property
    @rlock
    def writer_index(self):
        return [rd.id.name for rd in self.reader].index(self.writer.id.name)



class Hdf5Archive(Hdf5Writer):
    """Subclass of Hdf5Writer that interfaces to an archive consisting of
    multiple (identically structured) hdf5 files.  Enables safe read/write
    access to the archive.

    The primary distinction between Hdf5Archive and Hdf5Writer is that
    Hdf5Archive maintains read-only access to all past files.

    Methods
    -------
    close_writer
    create_reader
    """

    _with_lock_file = False

    def __init__(self, archive_files=None, *args, **kwargs):
        """ Instantiates an Hdf5Archive.

        Parameters
        ----------
        archive_files: str, list of str
            List of hdf5 files that contain the archive.
            Last element of list will be opened in write mode.
            Other elements will be opened in read-mode.
        """

        # Call superclass
        super(Hdf5Archive, self).__init__(*args, **kwargs)

        # If archive_files provided, then add readers/writers
        if archive_files is not None:
            if isinstance(archive_files, basestring):
                output_file = archive_files
                archive_files = []
            else:
                output_file = archive_files.pop()

            if len(archive_files) > 0:
                self.create_reader(archive_files)

            self.create_writer(output_file)

    @rlock
    def close_writer(self):

        if self.writer:

            # Save filename
            filename = self.writer.id.name
            rr = self.writer_index

            # Reset writer counters
            self.ind = 0
            self.num = 0

            # Close the file
            self.writer = self.writer.close()

            # Release the lock file
            if self._with_lock_file:
                self.release_lock_file(filename)

            # Open the file in read mode
            self.reader[rr] = h5py.File(filename, 'r', libver='latest')


    def create_reader(self, archive_files):

        list_af = archive_files if hasattr(archive_files, '__iter__') else [archive_files]

        for af in list_af:

            if af not in self.archive_files:

                rr = len(self.reader)

                # Open file in single-writer-multiple-reader mode
                reader = h5py.File(af, 'r', libver='latest')

                # Update searchable axis
                for kk, key in enumerate(reader[self._uniq_id][:]):
                    self.search[key] = (rr, kk)

                # Update growing axis
                tmp = reader['index_map'][self._grow_ax][:]
                self.grow['axis'] += list(tmp)
                self.grow['index'] += zip(np.repeat(rr, tmp.size), np.arange(tmp.size, dtype=np.int))

                # Add file to internal list
                self.reader.append(reader)


def h5py_dataset_iterator(group):

    for name, item in group.iteritems():

        if isinstance(item, h5py.Dataset): # test for dataset
            yield (name, item)

        elif isinstance(item, h5py.Group): # test for group (go down)
            for subname, subitem in h5py_dataset_iterator(item):
                yield (subname, subitem)


class OrderedSet(collections.MutableSet):
    """ Set that remembers original insertion order.
    Implementation based on a doubly linked link and an internal dictionary.
    This design gives OrderedSet the same order running times as regular sets
    including O(1) adds, removes, and lookups as well as O(n) iteration.
    Source:  http://code.activestate.com/recipes/576694/
    """

    def __init__(self, iterable=None):
        self.end = end = []
        end += [None, end, end]         # sentinel node for doubly linked list
        self.map = {}                   # key --> [key, prev, next]
        if iterable is not None:
            self |= iterable

    def __len__(self):
        return len(self.map)

    def __contains__(self, key):
        return key in self.map

    def add(self, key):
        if key not in self.map:
            end = self.end
            curr = end[1]
            curr[2] = end[1] = self.map[key] = [key, curr, end]

    def discard(self, key):
        if key in self.map:
            key, prev, next = self.map.pop(key)
            prev[2] = next
            next[1] = prev

    def __iter__(self):
        end = self.end
        curr = end[2]
        while curr is not end:
            yield curr[0]
            curr = curr[2]

    def __reversed__(self):
        end = self.end
        curr = end[1]
        while curr is not end:
            yield curr[0]
            curr = curr[1]

    def pop(self, last=True):
        if not self:
            raise KeyError('set is empty')
        key = self.end[1][0] if last else self.end[2][0]
        self.discard(key)
        return key

    def __repr__(self):
        if not self:
            return '%s()' % (self.__class__.__name__,)
        return '%s(%r)' % (self.__class__.__name__, list(self))

    def __eq__(self, other):
        if isinstance(other, OrderedSet):
            return len(self) == len(other) and list(self) == list(other)
        return set(self) == set(other)


class OrderedSetQueue(Queue.Queue):
    """ Queue with unique elements.
    """

    last = False    # Default is FIFO queue

    def _init(self, maxsize):
        self.queue = OrderedSet()

    def _put(self, item):
        self.queue.add(item)

    def _get(self):
        return self.queue.pop(last=self.last)


class OrderedSetFifoQueue(OrderedSetQueue):
    last = False
    pass

class OrderedSetLifoQueue(OrderedSetQueue):
    last = True
    pass