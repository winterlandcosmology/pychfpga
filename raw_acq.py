#!/usr/bin/env python
""" Raw data acquisition REST Server and Client with Python UDP packet receiver
"""
from __future__ import absolute_import, division, print_function

# Python Standard Library packages
import os
import sys
import socket
import time
import Queue
import SocketServer
import threading
import struct
import datetime

# PyPi packages
import netifaces  # non-standard Python library (pip install netifaces)
import numpy as np
import h5py
import tornado
import psutil

# External private packages
from wtl import log
from wtl.rest import AsyncRESTServer, endpoint, AsyncRESTClient, coroutine, coroutine_return, IOLoop, RunSyncWrapper, moment, run_client
from wtl.namespace import NameSpace
from wtl.metrics import Metrics
try:
    import comet
except ImportError:
    comet = None

# Local imports
from pychfpga import get_git_version

class hdf5TimestreamData(object):
    """ Object representing a HDF5 file containing raw data
    """
    def __init__(self, filestring, elements_per_file=2048*64, crate_and_slot_from_port = False):
        self.log = log.get_logger(self)
        self.N_SAMP = 2048
        #self.N_CHANNELS = 1
        self.crate_and_slot_from_port = crate_and_slot_from_port
        self.filename = filestring
        self.lock_filename = self.filename + '.lock'

        # # create a lock file
        with open(self.lock_filename,'w') as h:
            h.write('locked\n')

        self.log.info('%r: Opening raw data HDF5 file %s' % (self, self.filename))
        self.f = h5py.File(self.filename, 'w', libver='earliest')
        self.f.attrs["git_version_tag"] = "0.1"
        self.f.attrs["system_user"] = "root"
        self.f.attrs["collection_server"] = "hostname"
        self.f.attrs["instrument_name"] = "CHIME"
        self.f.attrs["acquisition_name"] = "rawadc"
        self.f.attrs["archive_version"] = "2.4.0"
        self.f.attrs["file_name"] = filestring
        self.f.attrs["data_type"] = "ADC snapshot data"
        self.f.attrs["rawadc_version"] = 0.1
        self.f.attrs["timestamping_warning"] = "Done on file write, may be significantly different from snapshot acquistion time"
        self.compound_dtype = np.dtype([('fpga_count', np.uint64), ('ctime', np.float64)])
        self.timestampDataset = self.f.create_dataset('timestamp',
                    (1, 1), dtype=self.compound_dtype, maxshape=(None, 1))
        self.timestampDataset.attrs['axis'] = ['snapshot']
        self.slotDataset = self.f.create_dataset('slot', (1, 1),
                                            dtype=np.uint8, maxshape=(None, 1))
        self.slotDataset.attrs['axis'] = ['snapshot']
        self.crateDataset = self.f.create_dataset('crate', (1, 1),
                                            dtype=np.uint32, maxshape=(None, 1))
        self.crateDataset.attrs['axis'] = ['snapshot']
        self.antDataset = self.f.create_dataset('adc_input', (1, 1),
                                            dtype=np.uint8, maxshape=(None, 1))
        self.antDataset.attrs['axis'] = ['snapshot']
        self.timestreamDataset = self.f.create_dataset('timestream',
                        (1, self.N_SAMP), dtype=np.int8,
                        maxshape=(None, self.N_SAMP))
        self.timestreamDataset.attrs['axis'] = ['snapshot', 'timestream']
        self.index_map = self.f.create_group("index_map")
        self.snapshot_index_map = self.index_map.create_dataset('snapshot',
                                            (elements_per_file,), dtype=np.uint32)
        self.start_index = int(filestring[-9:-6]) + 1
        self.snapshot_index_map[:] = np.arange(elements_per_file) + self.start_index
        self.timestream_index_map = self.index_map.create_dataset("timestream",
                                            (2048,), dtype=np.uint16)
        self.timestream_index_map[:] = np.arange(2048)
        self.n_times = 1
        self.n = 0

    def write(self, timestamp, port, chan, stream_id, flags, timestream):
        if self.n == self.n_times:
            self.n_times = self.n + 1
            self.timestampDataset.resize((self.n_times, 1))
            self.slotDataset.resize((self.n_times, 1))
            self.crateDataset.resize((self.n_times, 1))
            self.antDataset.resize((self.n_times, 1))
            self.timestreamDataset.resize((self.n_times, self.N_SAMP))
        elif self.n < self.n_times:
            pass
        else:
            print("ut oh...")
        # print(self.n_times)
        current_time = time.time()
        self.timestampDataset[self.n] = ( timestamp, current_time )
        self.antDataset[self.n] = chan
        if self.crate_and_slot_from_port:
            slot_number = port % 100  # assume port gives slot
            crate_number = ((port/100) % 10) - 1
        else:
            slot_number = (stream_id >> 4) & 0xF
            crate_number = (stream_id >> 8) & 0xF

        self.slotDataset[self.n] = slot_number
        self.crateDataset[self.n] = crate_number
        self.timestreamDataset[self.n] = timestream
        self.n += 1

    def close(self):
        self.log.info('%r: Closing HDF5 file %s' % (self, self.filename))
        self.f.close()
        try:
            os.remove(self.lock_filename)
            # os.rename(self.lock_filename, self.filename)
        except OSError:
            self.log.error('%r: Unable to rename HDF5 lock file from %s to %s' % (self, self.lock_filename, self.filename))


class RawAcqUDPReceiver(SocketServer.UDPServer):
    class UDPHandler(SocketServer.BaseRequestHandler):
        '''
        Puts the data in the queue. Another process will pull the data from the queue and write it
        to a hdf5 file.
        '''
        def handle(self):
            try:
                t0 = time.time()
                self.server.delay_between_calls = t0 - self.server.last_call_time
                self.server.last_call_time = t0
                self.server.packet_counter += 1
                data, socket = self.request
                port = self.server.server_address[1]
                (probe_id, stream_id, ts_high, ts_low) = self.server.unpack_header(data[:9])
                chan = probe_id & 0x0F
                timestamp = (ts_high << 32) + ts_low
                flags = stream_id & 0xF
                stream_id = (stream_id >> 4) & 0xFFF
                adc_data = np.frombuffer(data[9:2057], dtype=np.int8)
                #print( "Data received on port {0}, channel#{1}, std(data)={2}".format(port, chan, adc_data.std()) )
                #print("0x%03x"% stream_id,end='')
                try:
                    self.server.data_queue.put((timestamp, port, chan, stream_id, flags, adc_data), True, 0.8)
                    self.server.queued_packets += 1# print(".", end='')
                except Queue.Full:
                    # print("o", end='')
                    self.server.queue_overflows += 1
                self.server.processing_time = time.time() - t0
            except Exception as e: # Added to track memory leaks
                print('Exception in receiver: %r' % e)

    def __init__(self, server_address, data_queue):
        self.dt = np.dtype([
            ('probe_id', 'u1', 1),
            ('stream_id', '<u2', 1),
            ('ts_low', '<u2', 1),
            ('ts_high', '<u4', 1),
            ('data', 'i1', 2048)])
        self.data_queue = data_queue
        self.queue_overflows = 0
        self.queued_packets = 0
        self.packet_counter = 0
        self.unpack_header = struct.Struct('>BHHL').unpack_from  # Precompile unpack string for performance
        self.delay_between_calls = 0
        self.processing_time = 0
        self.last_call_time = time.time()
        SocketServer.UDPServer.__init__(self, server_address, self.UDPHandler)  # cannot use super(...): this is an old-style class

class RawAcqReceiver(object):
    ''' Implement an array of multi-threaded UDP Raw data receiver.

    The object offers `start` and `stop` methods to start and stop the receivers, a method to grab a
    snapshot of the current data, and a method to start a thred that continuously writes the data to
    disk if HDF5 format.

    This reciever uses Threads to implement concurrency, not Tornado.


    Notes:
        The performance ofthis receiver is limited by Python. It is meant to be used mostly for debugging.

        Should probably fix the 'serve forever bits'
    '''

    QUEUE_MAXSIZE = 10240 #: Maximum number of elements in a queue, just in case we can't read the queue as fast as we fill it. Otherwise we can use infinite memory.

    def __init__(self):
        self.log = log.get_logger(self)
        self.ports = None
        self.name = None
        self.datawriter = None
        self.receivers = []
        self.data_queue = None
        self.ioloop_last_time = None
        self.ioloop_max_response_time = None
        self.ioloop_min_response_time = None
        self.hdf5_write_time = 0
        self.start_time = None
        self.hdf5_start_time = None
        self.hdf5_run = False
        self.run = False

    def __repr__(self):
        return '%s(%s)' % (self.__class__.__name__, self.name)


    @coroutine
    def start(self, name='RawAcq', ports=[], jump_thresholds = []):
        """ Start a raw data receiver for each specified port.

        For each port we monitor, create a data queue and start a
        multithreaded UDP receiver that will write data to that queue.

        Parameters:
            name (str): Name of the receiver array, used for logging


            ports (list of dict): describe the ports to be created. An
                indeqpendent, multi- threaded receiver will be created for each port. Each entry is a dict in the format::

                    {port: port_number, sources: [(addr, port)...]}

                port: the desired port number sources:  (list of tuples): List of (src_addr,
                src_port)  tuples to *ping* the data source that will be sending data to that port.
                This is used to:
                    1) confirm that the data source is there,
                    2) to make sure that the switches know how to route the packets from the source to
                       the receiver, and
                    3) to determine the IP and MAC address that route to/from that data source so the
                       information can be provided back to the source.

                Concerning item 2), the FPGAs will send data to a specific MAC and IP address
                without ever having received a directed packets from the server. This means that the
                switch might not know on which port to forward the packet towards the server, which
                will cause the switches to broadcast the data everywhere. If we **assumes that the
                pinged interface is connected on the same switch as the data source interface**, all
                the switches between the source and the receiver will learn on which port to direct
                the data flow towards the server.

        Returns:
            A dict with the following keys:
                status:  Status of the receiver
                recv_addr: Receiver addresses to which each source should send its data. This is a dict in the format::

                        {(src_addr, src_port):(recv_addr, recv_port, recv_mac_addr),...}


            Notes:
            -

        Todo:
            - Might need to ping the source periodically as the switches may clear their routing
              tables periodically for stale entries.
            - Port numbers could in fact just be IDs or zero, and the server could assign its own port for each ID.
            - There is currently no way to assign port numbers to specific interfaces. The system
              will work only if 1) all the ports are in the same interface which connect to all
              FPGAs ping addresses), or 2) ports listen to all interfaces. Not clear if the later
              can be related to a performance issue. Unless all ports listen to all interfaces, each
              port shall be associated with a ping address to we know on which interface it should
              connect.
        """
        self.name = name
        self.listen_to_all_ports = True
        self.ports = ports
        self.data_queue = None
        self.receivers = []
        self.server_threads = []
        self.N_CHANNELS = 16
        self.all_data = {}
        self.all_ts = {}
        self.hdf5_file = None
        self.hdf5_run = False
        self.capture_start = False
        self.jump_thresholds = jump_thresholds
        self.start_time = time.time()
        self.rms_cache = {}

        # Metrics
        self.rms = {}
        self.min = {}
        self.max = {}
        self.mean = {}
        self.jumps = {}
        self.maxdiff = {}
        self.expected_ramp = np.arange(-128, 2048 - 128, dtype=np.int8)
        self.chan_number_mismatch_count = 0
        self.crate_number_mismatch_count = 0
        self.slot_number_mismatch_count = 0
        self.ramp_error_count = {}
        self.ramp_bit_error_count = {}
        self.ping_error_count = {}
        self.start_time = time.time()

        # Determine the interface from which data will be coming from each source by pinging them
        src_if_addrs = yield self.ping_sources()
        failed_src = [src_addr for src_addr, src_if_addr in src_if_addrs.items() if not src_if_addr]
        if failed_src:
            raise RuntimeError('Cannot ping %s, so cannot determine interface through which these data sources are reached.' %
                ','.join('%s:%s' (src_addr) for arc_addr in failed_src))


        # Determine the interface and port to which each receiver should listen to.
        #
        # If we want the UDP receiver to listen from all ports, we set the recever address to
        # '0.0.0.0'.  Note that 'localhost' and 'some_ip' are separate interfaces: if
        # you specify one, you can't receive data from the other.
        #
        # If we want the UDP interface to listen to specific interface, we look all the interfaces
        # from the sources associated with a port must use the same interface.
        receiver_ip = {}
        receiver_port = {}
        for port_info in self.ports:
            port = port_info['port']
            receiver_port[port] = 0 if isinstance(port, (str, unicode)) else port
            if self.listen_to_all_ports:
                receiver_ip[port] = '0.0.0.0'
            else:
                if_ips = {src_if_addrs[tuple(src)][0] for src in port_info['sources']}
                if len(if_ips) != 1:
                    raise RuntimeError('Data sources for port %s are accessed via different interfaces.' % port)
                receiver_ip[port] = if_ips.pop()

        # Create the data receivers
        actual_receiver_ip = {}
        actual_receiver_port = {}
        self.data_queue = Queue.Queue(self.QUEUE_MAXSIZE)
        for port in receiver_port.keys():
            addr = (receiver_ip[port], receiver_port[port])
            self.log.info('%.32r: Creating RawAcqUDPreceiver receiver for port %s on (%s:%s)' % (self, port, addr[0], addr[1]))
            receiver = RawAcqUDPReceiver(addr, self.data_queue)
            self.receivers.append(receiver)
            actual_receiver_ip[port], actual_receiver_port[port] = receiver.socket.getsockname()
            if actual_receiver_ip[port] != receiver_ip[port]: # just checking, should not happen
                raise RuntimeError('The receiver for port %s was not created on the correct interface (%s instead of %s)' % (port, actual_receiver_ip[port], receiver_ip[port]))
            thread = threading.Thread(target=receiver.serve_forever)
            thread.setDaemon(True)
            thread.start()
            self.server_threads.append(thread)
            self.log.info('UDP Receiver thread %s[port id=%s] started on %s:%i' % (self.name, port, actual_receiver_ip[port], actual_receiver_port[port]))

            self.all_data[receiver_port[port]] = np.zeros((self.N_CHANNELS, 2048), dtype=np.int8)  # pre-allocate data (channels x bins) for all ports,  for a single timestamp
            self.all_ts[receiver_port[port]] = np.zeros((self.N_CHANNELS), dtype=np.int32) # pre-allocate timestamps storage for the current data for all ports (should all be the same)

        self.run = True
        self.data_processing_thread = threading.Thread(target=self.process_data)
        self.data_processing_thread.setDaemon(True)
        self.data_processing_thread.start()

        # Build a mac address loopup table for all source interfaces
        if_ips = {if_addr[0] for if_addr in src_if_addrs.values()} # set of unique interface IPs used by all sources
        mac = {if_ip:self._get_mac_address(if_ip) for if_ip in if_ips} # map between ip and mac addresses
        self.log.info('%.32r: Available Interfaces are %s' % (self, mac))
        # Create the dict that provides the target ip address, port address and mac address for each source
        dest_ifs = {}
        for port_info in self.ports:
            port = port_info['port']
            for src in port_info['sources']:
                src_if_ip, src_if_port = src_if_addrs[tuple(src)]
                dest_ifs[tuple(src)] = (src_if_ip, actual_receiver_port[port], mac[src_if_ip])

        result = dict(
            status='started',
            target_addr=dest_ifs.items() # return as a list of tuples, json does not support tuple-indexed dicts
            )
        coroutine_return(result)


    @coroutine
    def _ping(self, addr, timeout=0.3):
        """
        Establish a TCP connection with `addr`  at and return the interface and local port used for the connection.

        Parameters:
            addr ((str, int) tuple): Address and port to which a TCP connection is made
            timeout (fload): Time to wait before giving up on the connection

        Return:
            An (interface_address, local_port) if the connection is successful, None otherwise.

        """
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        stream = tornado.iostream.IOStream(s)

        try:
            yield stream.connect(addr)
            if_addr = s.getsockname()
            s.close()
        except (socket.timeout, Exception) as e:
            self.log.warn('Could not establish a TCP connection with %s:%s. Error is:\n %s' % (addr[0], addr[1], e))
            if_addr = None

        coroutine_return(if_addr)

    @coroutine
    def ping_sources(self):
        if not self.ports:
            coroutine_return()
        self.log.info('%r: Pinging all data sources' % (self))
        # Determine the interface from which data will be coming from each source by pinging them
        src_if_addrs = yield {tuple(src):self._ping(tuple(src))
                              for port_info in self.ports
                              for src in port_info['sources']} # can be parallelized
        for src_addr, src_if_addr in src_if_addrs.items():
            old_count = self.ping_error_count.setdefault(src_addr, 0)
            if not src_if_addr:
                self.ping_error_count[src_addr] = old_count + 1
        coroutine_return(src_if_addrs)


    def _get_mac_address(self, if_addr):
        """ Return the MAC address of the interface with address `if_addr`.

        Parameters:

            if_addr (str): address of the interface (not any target)

        Returns:
            a string describing the mac address of the interface in the format 'xx:xx:xx:xx:xx:xx'. *None* if no match was found.
        """
        interfaces = netifaces.interfaces()
        mac_list = []
        for interface in interfaces:
            afs = netifaces.ifaddresses(interface)
            if netifaces.AF_INET not in afs or netifaces.AF_LINK not in afs:
                continue
            self.log.debug('checking interface %s with AF %s' % (interface, afs))
            ips = [af for af in afs[netifaces.AF_INET] if af['addr'] == if_addr]
            if ips:
                for eth_if in afs[netifaces.AF_LINK]:
                    mac_list.append(eth_if['addr'])
        if len(mac_list) > 1:
            raise RuntimeError('Multiple MAC addresses were found to be associated with the same IP address')
        if mac_list:
            return mac_list[0]
        else:
            return None


    def stop(self):
        if self.datawriter:
            self.stopHdf5Disk()
        while self.receivers:
            receiver = self.receivers.pop()
            receiver.shutdown()
            receiver.server_close()
            receiver.socket.close()  # free the socket so we can restart the receiver later
            print("shutdown servers")
            self.data_queue.queue.clear()
            self.start_time = None
        self.start_time = None

    def process_data(self):
        self.old_timestamp = None
        self.n_ant_rec = 0
        while self.run:
            # for j, out_q in enumerate(self.data_queues):
                # if out_q.empty():
                    # continue

                # get the packet from the queue
                try:
                    (timestamp, port, chan, stream_id, flags, adc_data) = self.data_queue.get() # block for minimum cpu usage
                except Queue.Empty:
                    #print('process_data: Queue Empty')
                    continue
                #print('.')

                # continue  # JFC debug mem leak
                base_data_port = 42500
                if port < base_data_port:
                    crate_number_from_port = None
                    slot_number_from_port = None
                crate_number_from_port = (port - base_data_port) // 100
                slot_number_from_port = ((port - base_data_port) % 100) - 1 # zero-based

                stream_id &= 0xFFF
                chan_number = stream_id & 0xF
                slot_number = (stream_id >> 4) & 0xF  # zero-based
                crate_number = (stream_id >> 8) & 0xF

                discard = False
                if chan != chan_number:
                    self.chan_number_mismatch_count += 1
                    discard = True
                if crate_number_from_port != crate_number:
                    self.crate_number_mismatch_count += 1
                    discard = True
                if slot_number_from_port != slot_number:
                    self.slot_number_mismatch_count += 1
                    discard = True

                if discard:
                    self.log.warning('%r:Crate/slot/channel mismatch: (%i, %i, %i) from port, (%i, %i, %i) from streamID' %
                        (self, crate_number_from_port, slot_number_from_port, chan, crate_number, slot_number, chan_number))
                    continue
                # Write data to HDF file
                t0 = time.time()
                if self.hdf5_run:
                    self.hdf5_file.write(timestamp, port, chan, stream_id, flags, adc_data)
                    self.n_elements += 1
                    if self.n_elements >= self.elements_per_file:
                        self.hdf5_file_number += 1
                        self.hdf5_file = self.start_new_hdf5_file()
                elif self.hdf5_file: # if we are no longer capturing to file, but a file is open, then close it.
                    self.hdf5_file.close()
                    self.hdf5_file = None # This will tell us we are finished capturing
                self.hdf5_write_time = max(self.hdf5_write_time, time.time() - t0)


                # if timestamp not in self.buffers:
                #     self.buffers.pop()  # remove last element
                #     self.buffers.insert(0, timestamp) # insert as first element
                # buf = self.buffers.index(timestamp)

                # self.current_ts[buf][j][chan] = timestamp
                # self.current_data[buf][j][chan, :] = adc_data
                # self.current_crate[j][chan] = crate_number
                # self.current_slot[j][chan] = slot_number

                # Capture a full timestamp set if self_capture = True
                if self.capture_start:
                    self.all_ts[port][chan] = timestamp
                    self.all_data[port][chan, :] = adc_data
                    if (timestamp == self.old_timestamp):
                        self.n_ant_rec += 1
                    else:
                        self.old_timestamp = timestamp
                        self.n_ant_rec = 1
                    if self.n_ant_rec >= self.N_CHANNELS - 1:
                        self.n_ant_rec = 0
                        self.old_timestamp = None
                        self.capture_start = False

                # Store some stats
                chan_id = (crate_number, slot_number, chan)
                self.rms_cache[chan_id] = np.std(adc_data)
                self.rms[chan_id] = np.std(adc_data)
                self.min[chan_id] = np.min(adc_data)
                self.max[chan_id] = np.max(adc_data)
                self.mean[chan_id] = np.mean(adc_data)
                self.maxdiff[chan_id] = np.max(np.abs(np.diff(adc_data)))
                if True:  # JFC debug mem leak
                    #if (adc_data != self.expected_ramp).any() and crate_number==0 and slot_number==0:
                    # print('%r: Ramp mismatch. Expected %s, got %s' % (self, self.expected_ramp[:8], adc_data[:8]))
                    self.ramp_error_count[chan_id] = (
                        self.ramp_error_count.get(chan_id, 0) +
                        np.sum(adc_data != self.expected_ramp))
                    for bit in range(8):
                        mask = 1 << bit
                        chan_bit_id = (crate_number, slot_number, chan, bit)
                        self.ramp_bit_error_count[chan_bit_id] = (
                            self.ramp_bit_error_count.get(chan_bit_id, 0) +
                            np.count_nonzero((adc_data ^ self.expected_ramp) & mask))
                    for threshold in self.jump_thresholds:
                        jump_id = (crate_number, slot_number, chan, threshold)
                        self.jumps[jump_id] = (
                            self.jumps.get(jump_id, 0) +
                            np.sum(np.abs(np.diff(adc_data)) > threshold))
                    # print('jumps thresholds=', self.jump_thresholds)

    def print_stats(self):
        for i, r in enumerate(self.receivers):
            self.log.debug('Recv %i, pkts=%i, queued= %i, overflows=%i, qsize=%i' %
                           (i, r.packet_counter, r.queued_packets,
                            r.queue_overflows, self.data_queue.qsize()))
        print

    def startHdf5Disk(self, base_dir, base_filename, capture_duration=60, elements_per_file=2048*64):
        if self.hdf5_file:
            raise RuntimeError('HDF5 dataWriter is already running')

        self.hdf5_start_time = time.time()
        # self.datawriter = dataWriter(self.data_queues, base_dir, base_filename, elements_per_file)
        # self.data_writer_thread = threading.Thread(target=self.datawriter.write)
        # self.data_writer_thread.setDaemon(True)
        # self.data_writer_thread.start()
        if capture_duration:
            capture_duration += 0,  # stop HDF5 capture 1 min after the desired time in case ch_master does not do it.
            self.log.info('%.32r: HDF5 data writer will be stopped in %f seconds' % (self, capture_duration))
            IOLoop.current().call_later(capture_duration, self.stopHdf5Disk)

        self.elements_per_file = elements_per_file

        # Create the target folder
        time_str = datetime.datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')
        self.hdf5_base_dir = os.path.join(os.path.expanduser(base_dir),'%s_%s/' % (time_str, base_filename))
        try:
            os.makedirs(self.hdf5_base_dir)
        except:
            self.log.warning("%.32r: couldn't make directory '%s'. Using current directory." % (self, self.hdf5_base_dir))
            self.hdf5_base_dir = './'

        self.hdf5_file_number = 0
        self.hdf5_file = self.start_new_hdf5_file()
        self.hdf5_run = True


    def stopHdf5Disk(self):
        if not self.hdf5_file:
            raise RuntimeError('%.32r: HDF5 dataWriter is not running' % self)
        self.log.info('%.32r: Stopping HDF5 data writer' % self)
        self.hdf5_run = False
        self.hdf5_start_time = None
        # self.data_writer_thread.join()
        # self.datawriter.close()
        # self.datawriter = None


    def start_new_hdf5_file(self):
        if self.hdf5_file:
            self.hdf5_file.close()
        self.n_elements = 0
        filename = "{0:06d}.h5".format(self.hdf5_file_number)
        filename =  os.path.join(self.hdf5_base_dir, filename)
        h5file = hdf5TimestreamData(filename, elements_per_file=self.elements_per_file)  # start a new empty file
        return h5file

    # def hdf5_write(self, timestamp, port, chan, stream_id, flags, adc_data):
    #     """
    #     Aggregate a number of data sets and write them into the current HDF5 file, then start a new
    #     file. Runs forever until self.hdf5_run is False.
    #     """
    #     if self.hdf5_run:
    #         self.hdf5_file.write(timestamp, port, chan, stream_id, flags, adc_data)
    #         self.n_elements += 1
    #         if n_elements >= self.elements_per_file:
    #             self.hdf5_file_number += 1
    #             self.hdf5_file = self.start_new_hdf5_file()
    #     elif self.hdf5_file:
    #         self.hdf5_file.close()
    #         self.hdf5_file = None

    @coroutine
    def get_data(self):
        """
        Grab data from the queue until we have a frame for all channels for a single timestamp.
        """
        # for j, out_q in enumerate(self.data_queues):
        #     trying_to_receive = True
        #     while trying_to_receive:
        #         (timestamp, port, chan, stream_id, flags, adc_data) = out_q.get()
        #         if (timestamp == self.old_timestamp) and (self.n_ant_rec < self.N_CHANNELS - 1):
        #             self.all_ts[j][chan] = timestamp
        #             self.all_data[j][chan, :] = adc_data
        #             self.n_ant_rec += 1
        #         elif (timestamp == self.old_timestamp) and (self.n_ant_rec == self.N_CHANNELS - 1):
        #             self.all_ts[j][chan] = timestamp
        #             self.all_data[j][chan, :] = adc_data
        #             self.n_ant_rec = 0
        #             self.old_timestamp = 0
        #             trying_to_receive = False
        #         elif (timestamp != self.old_timestamp) and (self.n_ant_rec < self.N_CHANNELS):
        #             # Start over, would be new set start as well.
        #             #print "didn't get full set, only received {0} ant. restarting.".format(self.n_ant_rec)
        #             self.old_timestamp = timestamp
        #             self.all_ts[j][chan] = timestamp
        #             self.all_data[j][chan, :] = adc_data
        #             self.n_ant_rec = 1
        #Should use the returned port.  cheating here.
        if self.start_capture:
            raise RuntimeError('Data set capture is already in progress')
        self.start_capture = True
        while not self.start_capture:
            yield moment

        coroutine_return(self.all_ts, self.ports, self.all_data)


    def is_running(self):
        return bool(self.receivers)

    def check_ioloop_response_time(self):
        t = time.time()
        if self.ioloop_last_time is not None:
            self.ioloop_max_response_time = max(self.ioloop_max_response_time or 0, t-self.ioloop_last_time)
            self.ioloop_min_response_time = min(self.ioloop_min_response_time or float('inf'), t-self.ioloop_last_time)
        self.ioloop_last_time = t

    @coroutine
    def get_metrics(self):
        metrics = Metrics(default_type='gauge')

        # Node stats

        mem = psutil.virtual_memory()

        metrics.add('raw_acq_node_mem_total', value=mem.total)
        metrics.add('raw_acq_node_mem_available', value=mem.available)
        metrics.add('raw_acq_node_mem_percent', value=mem.percent)
        metrics.add('raw_acq_node_mem_used', value=mem.used)
        metrics.add('raw_acq_node_mem_free', value=mem.free)

        cpu = psutil.cpu_times()

        metrics.add('raw_acq_node_cpu_percent', value=psutil.cpu_percent())
        metrics.add('raw_acq_node_cpu_user', value=cpu.user)
        metrics.add('raw_acq_node_cpu_system', value=cpu.system)
        metrics.add('raw_acq_node_cpu_idle', value=cpu.idle)

        # Disk usage on the hdf5 file destination volume
        if hasattr(os,'statvfs') and self.hdf5_run:
            s = os.statvfs(self.hdf5_base_dir)
            metrics.add('raw_acq_disk_size', value=s.f_blocks * s.f_bsize)
            metrics.add('raw_acq_disk_used', value=(s.f_blocks - s.f_bfree) * s.f_bsize)
            metrics.add('raw_acq_disk_free', value=s.f_bfree * s.f_bsize)
            metrics.add('raw_acq_disk_percent_used', value=float(s.f_blocks - s.f_bfree)/s.f_blocks)
            metrics.add('raw_acq_disk_percent_free', value=float(s.f_bfree)/s.f_blocks)

        metrics.add('raw_acq_run_time', value= 0 if self.start_time is None else time.time() - self.start_time )
        metrics.add('raw_acq_hdf5_run_time', value= 0 if self.hdf5_start_time is None else time.time() - self.hdf5_start_time )

        # IOloop health stats
        metrics.add('raw_acq_ioloop_max_response_time', value=self.ioloop_max_response_time)
        metrics.add('raw_acq_ioloop_min_response_time', value=self.ioloop_min_response_time)
        self.ioloop_max_response_time = None
        self.ioloop_min_response_time = None

        # HDF5 file writing stats

        metrics.add('raw_acq_hdf5_write_time', value=self.hdf5_write_time)
        self.hdf5_write_time = 0
        if self.hdf5_run:
            metrics.add('raw_acq_hdf5_n_elements', value=self.n_elements)
            metrics.add('raw_acq_hdf5_n_elements_max', value=self.elements_per_file)
            metrics.add('raw_acq_hdf5_number_of_files', value=self.hdf5_file_number)



        # receiver data queue stats

        if self.run:
            metrics.add('raw_acq_queue_size', value=self.data_queue.qsize())
            metrics.add('raw_acq_queue_maxsize', value=self.data_queue.maxsize)

            # ADC signal stats
            for (crate, slot, chan), rms in self.rms.items():
                metrics.add('raw_acq_rms', value= rms, crate=crate, slot=slot, chan=chan)
            self.rms = {}
            for (crate, slot, chan), min_ in self.min.items():
                metrics.add('raw_acq_min', value= min_, crate=crate, slot=slot, chan=chan)
            self.min = {}
            for (crate, slot, chan), max_ in self.max.items():
                metrics.add('raw_acq_max', value= max_, crate=crate, slot=slot, chan=chan)
            self.max = {}
            for (crate, slot, chan), mean in self.mean.items():
                metrics.add('raw_acq_mean', value= mean, crate=crate, slot=slot, chan=chan)
            self.mean = {}
            for (crate, slot, chan), maxdiff in self.maxdiff.items():
                metrics.add('raw_acq_max_diff', value= maxdiff, crate=crate, slot=slot, chan=chan)
            self.maxdiff = {}
            for (crate, slot, chan), count in self.ramp_error_count.items():
                metrics.add('raw_acq_ramp_errors', value= count, crate=crate, slot=slot, chan=chan)
            #self.ramp_error_count = {}
            for (crate, slot, chan, bit), count in self.ramp_bit_error_count.items():
                metrics.add('raw_acq_ramp_bit_errors', value=count, crate=crate, slot=slot, chan=chan, bit=bit)
            #self.ramp_bit_error_count = {}
            for (crate, slot, chan, threshold), count in self.jumps.items():
                metrics.add('raw_acq_jumps', value=count, crate=crate, slot=slot, chan=chan, threshold=threshold)
            #self.jumps = {}

            # Receiver-specific stats
            for i, r in enumerate(self.receivers):
                metrics.add('raw_acq_received_packets', value=r.packet_counter, receiver=i)
                metrics.add('raw_acq_queued_packets', value=r.queued_packets, receiver=i)
                metrics.add('raw_acq_overflow_packets', value=r.queue_overflows, receiver=i)
                metrics.add('raw_acq_packet_receiver_delay_between_calls', value=r.delay_between_calls, receiver=i)
                metrics.add('raw_acq_packets_receiver_processing_time', value=r.processing_time, receiver=i)


            metrics.add('raw_acq_run_time', value=0 if self.start_time is None else time.time() - self.start_time)

            # Packet integrity stats

            metrics.add('raw_acq_chan_mismatch', value=self.chan_number_mismatch_count)
            metrics.add('raw_acq_crate_mismatch', value=self.crate_number_mismatch_count)
            metrics.add('raw_acq_slot_mismatch', value=self.slot_number_mismatch_count)

            # Ping stats
            for (src_ip, src_port), count in self.ping_error_count.items():
                metrics.add('raw_acq_ping_errors', value=count, src_ip=src_ip, src_port=src_port)
            self.ping_error_count = {}

        return metrics



################################################
# RawAcq REST Server
################################################

class RawAcqAsyncRESTServer(AsyncRESTServer):
    """
    Asynchronous RawAcq REST server that operates Python-based multi-threaded UDP data receivers.

    Todo:
        - Setup logging.
    """

    DEFAULT_PORT = 54322

    def __init__(self, address='', port=DEFAULT_PORT, logging_params={}):
        self.receiver = RawAcqReceiver()
        super(RawAcqAsyncRESTServer, self).__init__(address=address, port=port,  heartbeat_string='Rs')
        self.add_periodic_callback(self.receiver.print_stats, 3000)
        self.add_periodic_callback(self.receiver.ping_sources, 3000) # ping the raw_acq data sources periodically to ensure the switches tables always know how to route the packets to here
        self.add_periodic_callback(self.receiver.check_ioloop_response_time, 300)
        self.startup_time = datetime.datetime.utcnow()
        self.GIT_VERSION = get_git_version()


    @coroutine
    def shutdown(self):
        self.receiver.stop()

    @coroutine
    @endpoint
    def start(self, handler, **config):
        self.log.info('%.32r: Received start command with %r' % (self, config))
        if self.receiver.is_running():
            self.log.info('%.32r: Receiver is already running. Stopping it and restarting a new one' % (self))
            yield self.receiver.stop()
            # raise RuntimeError('Server is already started')

        # Register config with comet broker
        comet_config = config.pop('comet_broker', {})
        try:
            enable_comet = comet_config['enabled']
        except KeyError:
            msg = "Missing config value 'comet_broker/enabled'."
            self.log.error(msg)
            raise RuntimeError('Cannot start comet broker: %s' % (msg))
        if enable_comet:
            if comet is None:
                msg = "Failure importing comet for configuration tracking.  Please install the " \
                      "comet package or set 'comet_broker/enabled' to False in config."
                self.log.error(msg)
                coroutine_return(msg)
            try:
                comet_host = comet_config['host']
                comet_port = comet_config['port']
            except KeyError as exc:
                msg = "Failure registering initial config with comet broker: 'comet_broker/{}' " \
                      "not defined in config.".format(exc[0])
                self.log.error(msg)
                raise RuntimeError('Cannot start comet broker: %s' % (msg))
            comet_manager = comet.Manager(comet_host, comet_port)
            try:
                comet_manager.register_start(self.startup_time, self.GIT_VERSION)
                comet_manager.register_config(config.copy())
            except comet.CometError as exc:
                msg = "Comet failed registering raw_acq start and initial config. " \
                      "The Comet client returned the following error: {}".format(exc)
                self.log.error(msg)
                raise RuntimeError('Cannot start comet broker: %s' % (msg))
        else:
            self.log.warning("Config registration DISABLED. This is only OK for testing.")

        result = yield self.receiver.start(**config)
        self.log.info('%.32r: UDP receiver started. Returned %r' % (self, result))
        coroutine_return(result)

    @coroutine
    @endpoint
    def stop(self, handler):
        if not self.receiver.is_running():
            self.log.warning('%.32r: Server is not running' % self)
        self.receiver.stop()
        coroutine_return("stopped receiver")

    @coroutine
    @endpoint('start-hdf5')
    def start_hdf5(self, handler, base_dir='./', base_filename='RawAcq', capture_duration=0, elements_per_file=2048*64):
        self.receiver.startHdf5Disk(base_dir, base_filename, capture_duration=capture_duration, elements_per_file=elements_per_file)
        coroutine_return("started hdf5 writing to disk.")

    @coroutine
    @endpoint('stop-hdf5')
    def stop_hdf5(self, handler):
        self.receiver.stopHdf5Disk()
        coroutine_return("stopped hdf5 writing to disk.")

    @coroutine
    @endpoint('status')
    def status(self, handler):
        coroutine_return(dict(started=self.receiver.is_running() if self.receiver else False))


    @coroutine
    @endpoint
    def get_packets(self, handler):
        self.log.info('%.32r: received get_packets command' % self)
        ts, ports, data = yield self.receiver.get_data()
        print(ts)
        print(ports)
        print(data)
        coroutine_return(ts=ts.tolist(), ports=ports, data=data.tolist())

    @coroutine
    @endpoint('get-rms')
    def get_rms(self, handler):
        if self.receiver.is_running():
            coroutine_return(rms=self.receiver.rms_cache.items())
        else:
            coroutine_return(rms=[])

    @coroutine
    @endpoint('get-monitoring-data')
    def get_monitoring_data(self, handler):
        t0 = time.time()
        self.log.info('%.32r: Received monitoring metrics request' % self)
        metrics = yield self.receiver.get_metrics()
        handler.set_header('Content-Type', 'text/plain')
        handler.set_header('Content-Encoding', 'gzip')
        handler.write(metrics.get_gzip())
        self.log.info('%.32r: Returning raw_acq %i metrics. The request took %.3f seconds' % (self, len(metrics), time.time()-t0))


################################################
# RawAcq REST Client
################################################

class RawAcqAsyncRESTClient(AsyncRESTClient):
    """
    Implements an asynchronous client that exposes the functions of the specified remote RawAcq server.

    This client is used by ch_master to start, configue and operate all the RawAcq servers in the array.

    The client is implemented using a Tornado AsyncHTTPClient. It exposes the RawAcq server methods
    (i.e REST endpoints) as local methods. The local methods are Tornado coroutines so requests to
    multiple clients can be made in parallel. This is especially beneficial since the data requests
    from the server are slow IO operations which benefit the mist from co-execution.

    The client will operate only if the IOloop in which is was created is running.

    Parameters:

        name (str): Name of the client, to be used in logging etc.

        hostname (str): The hostname of the RawAcq REST server. If `host` is None, an (experimental,
             Python-based) RawAcq REST server will be created locally.

        port (int): The port number to which the RawAcq REST server is listening. Default is port 80.

        kwargs: All remaining aruments will be stored as configuration data.
    """

    def __init__(self,
                 name='RawAcq',
                 hostname='localhost',
                 port=RawAcqAsyncRESTServer.DEFAULT_PORT,
                 base_dir = '~/data',
                 base_filename= None,
                 create_server = True,
                 **config):
        super(RawAcqAsyncRESTClient, self).__init__(
            hostname=hostname,
            port=port,
            server_class=RawAcqAsyncRESTServer if create_server else None,
            heartbeat_string='Rc')

        self.name = name
        self.hdf5_base_dir = base_dir
        self.base_filename = base_filename or name
        self.config = config


    @coroutine
    def ping(self):
        try:
            yield self.get('status')
            self.log.info("Successfully pinged raw_acq server at %s:%i" % (self.hostname, self.port))
        except Exception as e:
            self.log.debug(repr(e))
            self.log.error("Can't ping raw_acq server at %s:%i" % (self.hostname, self.port))
            coroutine_return(False)
        coroutine_return(True) # coroutine_return raises an exception: we don't want it in the try block

    @coroutine
    def status(self):
        result = yield self.get('status')
        coroutine_return(result)

    @coroutine
    def start(self, **config):
        """ Start the RaqAcq remote server with the keyword argument as configuration data"""
        self.log.info('%s: Starting remote RawAcq server at %s:%i with config: %r' % (self, self.hostname, self.port, config))
        result = yield self.post('start', **config)
        coroutine_return(result)

    @coroutine
    def stop(self):
        result = yield self.get('stop')
        coroutine_return(result)

    @coroutine
    def get_packets(self):
        data = yield self.get('get-packets')
        coroutine_return(data)

    @coroutine
    def start_hdf5(self, base_dir=None, base_filename=None, capture_duration=0, elements_per_file=2048*64):
        result = yield self.post('start-hdf5', base_dir=base_dir or self.hdf5_base_dir, base_filename=base_filename or self.base_filename, capture_duration=capture_duration, elements_per_file=elements_per_file)
        coroutine_return(result)

    @coroutine
    def stop_hdf5(self, base_dir=None, base_filename=None):
        result = yield self.get('stop-hdf5')
        coroutine_return(result)

    @coroutine
    def estimate_gains(self):
        coroutine_return((yield self.post('estimate_gains')))   # estimate-gains?



def main():
    """ Command-line interface to operate the RawAcq server.

    ./raw_acq.py [config] [command {args}] [--host hostname] [--port port_number] [--no-run | --run] [--no-start]

    where:
        *config* : configuration in the format [[*filename*]:][*path_to_config_object*]
        *command* : the name of a ChimeMaster client method.
        --host: hostname of the server. Overrides the hostname found in the config. Default is 'localhost'.
        --port: port number of the server. Overrides the port number found in the config.  Default is 54322.
        --run: run the client/server until Ctrl-C is pressed. Default when no command is provided.
        --no-run: Do not run the client/server even if no comman dis provided.
        --no_start: do not attempt to initialize the server even if a configuration is provided.

    The `raw_acq` command is invoked from the command line with::

        ./raw_acq.py arguments...  # linux only
        python raw_acq.py arguments

    Or from an ipython interactive session::

        run -i raw_acq arguments

    Operations done:

        1. Create client:

            - Always starts a client that connects to server at address specified in config or as
              overriden by --host and --port.

        2. Create server if none already exists:

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

    Create and initialize and run a new local server or initialize an existing server::

        ./raw_acq.py jfc.erh

    Create an non-initialized server

        ./raw_acq.py  # starts server on localhost:54322
        ./raw_acq.py config --no-start # starts server at address specified in config

    Send a command to server:

        ./raw_acq stop # send stop command to server on localhost:54322
        ./raw_acq jfc.erh power_off # power off supplies used by server running at theaddress specified in the jfc.erh config
    """
    # Setup logging
    log.setup_basic_logging('INFO')

    client, server = run_client(sys.argv[1:], RawAcqAsyncRESTServer, RawAcqAsyncRESTClient, object_name ='RawAcq', server_config_path='raw_acq.servers')
    return client, server

if __name__ == '__main__':
    client, server = main()

