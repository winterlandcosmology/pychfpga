#!/usr/bin/env python
""" Raw data acquisition REST Server and Client with Python UDP packet receiver
"""


# Python Standard Library packages
import os
import sys
import socket
import time
import asyncio
import __main__
# import itertools

import threading
import datetime
import select

# PyPi packages
import aiohttp.web
import netifaces  # non-standard Python library (pip install netifaces)
import numpy as np
import h5py
import psutil

# External private packages
from wtl import log
from wtl.rest import AsyncRESTServer, endpoint, AsyncRESTClient, run_client
from wtl.namespace import NameSpace
from wtl.metrics import Metrics

try:
    import comet
except ImportError:
    comet = None

# Local imports
from . import __version__
from .core import CORR


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

    # QUEUE_MAXSIZE = 10240 #: Maximum number of elements in a queue, just in
    # case we can't read the queue as fast as we fill it. Otherwise we can use
    # infinite memory.

    def __init__(self):
        self.log = log.get_logger(self)
        self.ports = None
        self.port_number = []  # actual port number associated with each socket
        self.name = None
        self.datawriter = None
        self.receivers = []
        self.data_queue = None
        self.ioloop_last_time = None
        self.ioloop_max_response_time = None
        self.ioloop_min_response_time = None
        self.start_time = None
        self.started = False
        self.stream_ids = []
        self.sockets = []  # Empty indicates that the receiver is not started
        self.lock = threading.RLock()  # Locks access to data while the
                                       # receiver thread is populating it
        self.is_locked=False #debug
        self.raw_packet_processor = None
        self.corr_packet_processor = None

    def __repr__(self):
        return '%s(%s)' % (self.__class__.__name__, self.name)

    async def start_async(
            self,
            name='RawAcq',
            ports=[],
            stream_ids=[],
            corr_name=None,
            run_name=None,
            data_folder=None,
            run_folder=None,
            start_thread=True,
            fft_offset_encoding=True,
            metrics_refresh_time=1,
            adc_rms_refresh_count=60,
            corr_firmware_integration_period=0,  #used to compute timestamps
            corr_software_integration_period=0,  # no corr processing until changed
            jump_thresholds=[],
            hostname=None, # not used, but may be passed by start_client()
            post=None # # not used, but may be passed by start_client()
            ):
        """ Start a raw data receiver for each specified port.

        For each port we monitor, create a data queue and start a
        multithreaded UDP receiver that will write data to that queue.

        Parameters:
            name (str): Name of the receiver array, used for logging


            ports (list of dict): describe the ports to be created. The list is in the format::

                [ {'port': port_id, 'sources': list_of_sources}, ...]


                Where :

                    port_id: the ID of the port to be created.

                       If the ``port_id`` is a string ID, a port number will
                       be selected automatically and *all* sources with that
                       same `port_id` will be assigned to that port.

                       If ``port_id`` is a non-zero integer, it will be
                       interpreted as a port number and all sources will be
                       assigned to that port number.

                       If ``port_id`` is Null or zero, *each* source in
                       `list_of_sources` will be assigned an individual random
                       port number.


                    list_of_sources:  (list of tuples): List of address:port
                        tuples [(addr, port)...] that describe the IceBoards
                        that will be sending data to this port. A TCP
                        connection will be attempted to those addresses to


                            1) confirm the presence of the source,

                            2) determine on which interface we should listen to,

                            3) and teach the switches routing table how to
                               route the UDP packets from the source to this
                               receiver (See Note below)


            stream_ids (list of int): List of STREAM iD that are expected to
                be received. This will be used to preallocate and classify the
                incoming data. Any packets with a STREAM ID that is not in
                this list will be rejected.


            jump_thresholds (list of int): Threshold values

            metrics_refresh_time (float): cadence in seconds at which metrics are updated

            adc_rms_refresh_count (float): number of frames to average for the rms cache

        Returns:
            A dict with the following keys:
                status:  Status of the receiver
                recv_addr: Receiver addresses to which each source should send its data. This is a dict in the format::

                        {(src_addr, src_port):(recv_addr, recv_port, recv_mac_addr),...}


        Notes:

            * Concerning item 3), the FPGAs will send UDP data to a specific
              MAC and IP address without possibly ever having received a
              directed packets from the server. This means that the switch
              might not know on which port to forward the packet towards the
              receiver, which will cause the switches to broadcast the data
              everywhere. If we **assume that the specified source addresses
              have interfaces on the same switch as the interface that sends
              the UDP packets***, establishing a bidirectional TCP connection
              to the source will tell all the switches between the source and
              the receiverhow to direct the data flow towards the server. This
              TCP connection needs to be redone periodically to prevent the
              cached entries in the switches MAC address tables from expiring.

            * In `ports`, ``port_id`` can appear in multiple entries; this is
              therefore why `ports` is not structured as a {port_id:sources}
              dict.


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
        self.listen_to_all_interfaces = True
        self.ports = ports
        self.jump_thresholds = jump_thresholds
        self.corr_name = corr_name
        self.run_name = run_name
        self.data_folder = data_folder
        self.run_folder = run_folder
        # self.frame0_irigb_time = frame0_irigb_time

        # Socket creation variables
        self.sockets = []  # Sockets that were opened
        self.port_number = []  # actual port number associated with each socket
        self.ping_error_count = {}

        self.start_time = time.time()
        # Number of packets that are received
        self.received_packets = 0
        self.packet_max_readout_time = 0
        self.current_packet_readout_time = 0
        self.packet_max_processing_time = 0
        self.current_processed_packets = 0
        self.packet_current_processing_time = 0

        #######################################
        # Raw buffer & buffer unpacking objetcs
        #######################################

        # Define numpy data types that will be used to efficiently parse the data

        self.DATA_SIZE = 2048  # number of bytes of data
        self.RAW_PACKET_LENGTH = 10 + self.DATA_SIZE  # header length + data length
        self.CORR_PACKET_LENGTH = CORR.NBYTES_PER_HEADER + CORR.NPROD_PER_CMAC * CORR.NBYTES_PER_PROD

        self.PACKET_SIZE = max(self.RAW_PACKET_LENGTH, self.CORR_PACKET_LENGTH)
        self.BUF_SIZE = 2048  # size of receive buffer. Big enough to accomodate all packets for one frame from the RAW or correlator subsystem.

        if not self.BUF_SIZE:
            self.log.warning('%r: Buffer size is zero! The list of expected STREAM IDs must have been empty!' % self)
        self.buf = np.empty((self.BUF_SIZE, self.PACKET_SIZE), dtype=np.uint8)
        self.buf_packet_length = np.empty(self.BUF_SIZE, dtype=np.uint16)
        self.n = 0  # number of packets currently stored in the buffer

        self.raw_packet_processor = RawPacketProcessor(
            self,
            stream_ids=stream_ids,
            metrics_refresh_time=metrics_refresh_time,
            adc_rms_refresh_count=adc_rms_refresh_count,
            fft_offset_encoding=fft_offset_encoding
            )

        self.corr_packet_processor = CorrPacketProcessor(
            self,
            firmware_integration_period=corr_firmware_integration_period,
            software_integration_period=corr_software_integration_period,
            # frame0_irigb_time = self.frame0_irigb_time
            )

        # Determine the interface from which data will be coming from each source by pinging them
        # returns a dictionary that maps each source to an interface IP and target port
        #   { (src_ip, src_port) : (if_ip, port) }
        src_if_addrs = await self.ping_sources_async()
        print('IF addr=', src_if_addrs)
        failed_src = [src_addr for src_addr, src_if_addr in src_if_addrs.items() if not src_if_addr]
        if failed_src:
            raise RuntimeError('Cannot ping %s, so cannot determine interface through which these data sources are reached.' %
                ','.join('%s:%s' (src_addr) for arc_addr in failed_src))


        # Determine the interface and port to which each receiver should listen to.
        #
        # If we want the UDP receiver to listen from all interfaces, we set the receiver address to
        # '0.0.0.0'.  Note that 'localhost' and 'some_ip' are separate interfaces: if
        # you specify one, you can't receive data from the other.
        #
        # If we want the UDP interface to listen to specific interface, we look all the interfaces
        # from the sources associated with a port must use the same interface.
        #
        # Expand the port info to identify the interface and sources associated with each individual socket that we will create
        #
        # target port number: a specific port number, a port name (assigned one random port to all sources), or 0 (assign a random port to each source)
        socket_if_ip = {} # interfaces accessed by each port. Should be only one for named and non-zero ports.
        socket_sources = {} # list of sources associated with each port
        for port_info in self.ports:
            # Create a list that associate a port to each source. If port==0,
            # a different port name is given to each source, otherwise all
            # ports have the specified port (number or name)
            ports = [(port_info['port'] if port_info['port'] else ('_random_port_%i' % i))
                     for i, _ in enumerate(port_info['sources'])]
            for port, src in zip(ports, port_info['sources']):
                socket_sources.setdefault(port, []).append(src)
                # get the set of IPs for this port, or create one if there is none yet
                if_ip = '0.0.0.0' if self.listen_to_all_interfaces else src_if_addrs[tuple(src)][0]
                # Check if we have multiple interfaces associated with specified or named ports
                if port in socket_if_ip and socket_if_ip[port] != if_ip:
                    raise RuntimeError('Data sources for port %s are accessed via different interfaces %s and %s.' % (port, socket_if_ip[port], if_ip))
                socket_if_ip[port] = if_ip
        # At this point, there is one socket per port_name, and no port_name is zero

        # Create the sockets
        actual_socket_if_ip = {}
        actual_socket_port = {}
        for port_name, if_ip in socket_if_ip.items():

            # if port name is a string, set the port to zero so the system
            # will assign a random port number. If port_name is a number, ask
            # the system to open the socket at that port.
            port = 0 if isinstance(port_name, str) else port_name
            self.log.info("%r: Creating socket for port ID '%s' on (%s:%s)" % (self, port_name, if_ip, port))
            sock = self.get_udp_socket((if_ip, port))
            self.sockets.append(sock)
            # Store actual port IP/port allocated by the system
            actual_socket_if_ip[port_name], actual_socket_port[port_name] = sock.getsockname()
            self.port_number.append(actual_socket_port[port_name])

            # Check if the port and IP that were given are what we expect. This should never happen.
            if ((actual_socket_if_ip[port_name] != socket_if_ip[port_name]) or
                (port and port != actual_socket_port[port_name])):
               self.log.warn(
                    'The socket for port ID %s was not created at the expected '
                    'address: got %s:%s instead of %s:%s' % (
                        port_name,
                        actual_socket_if_ip[port_name],
                        actual_socket_port[port_name],
                        socket_if_ip[port_name],
                        port))

            self.log.info("%r: receiver %s: UDP Socket created for port  '%s' at %s:%i" % (self, self.name, port_name, actual_socket_if_ip[port_name], actual_socket_port[port_name]))

        self.started = True
        if start_thread:
            self.start_processing_thread()
        # Build a mac address loopup table for all source interfaces
        if_ips = {if_addr[0] for if_addr in src_if_addrs.values()} # set of unique interface IPs used by all sources
        mac = {if_ip:self._get_mac_address(if_ip) for if_ip in if_ips} # map between ip and mac addresses
        self.log.info('%r: Available Interfaces are %s' % (self, mac))

        # Create the dict that provides the target ip address, port address and mac address for each source
        dest_ifs = []
        for port_name, sources in socket_sources.items():
            dest_port = actual_socket_port[port_name]
            for src in sources:
                dest_if_ip = src_if_addrs[tuple(src)][0]
                dest_mac = mac[dest_if_ip]
                dest = (dest_if_ip, dest_port, dest_mac)
                dest_ifs.append( (tuple(src), dest))


        result = dict(
            status='started',
            target_addr=dest_ifs # return as a list of tuples, json does not support tuple-indexed dicts
            )
        return result

    def get_udp_socket(self, addr):
        """
        Return a socket that is bound to the specified port/address.


        """
        # Make sure there is a list of opened sockets

        opened_sockets = __main__.__dict__.setdefault('__opened_sockets__', {})

        ip, port = addr
        # If we want to use a specific local port that was previously reserved, use its socket.
        if port and port in opened_sockets:
            sock = opened_sockets[port]
        else:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind((ip, port))
            # store the socket in the main module so it will live persistently until the Python session is closed.
            (ip, port) = sock.getsockname()
            opened_sockets[port] = sock

        return sock



    async def _ping_async(self, addr, timeout=0.3):
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
        loop = asyncio.get_event_loop()
        # stream = tornado.iostream.IOStream(s)

        try:
            await loop.sock_connect(s, addr)
            if_addr = s.getsockname()
            s.close()
        except (socket.timeout, Exception) as e:
            self.log.warn('Could not establish a TCP connection with %s:%s. Error is:\n %s' % (addr[0], addr[1], e))
            if_addr = None

        return if_addr


    async def ping_sources_async(self):
        if not self.ports:
            return None
        self.log.info('%r: Pinging all data sources' % (self))
        # Determine the interface from which data will be coming from each source by pinging them
        src_addrs = [tuple(src) for port_info in self.ports
                              for src in port_info['sources']]
        src_if_addrs = await asyncio.gather(*[self._ping_async(src) for src in src_addrs])
        for src_addr, src_if_addr in zip(src_addrs, src_if_addrs):
            old_count = self.ping_error_count.setdefault(src_addr, 0)
            if not src_if_addr:
                self.ping_error_count[src_addr] = old_count + 1
        return dict(zip(src_addrs, src_if_addrs))


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
        self.started = False
        if self.data_processing_thread:
            self.data_processing_thread.join()

        # Stop all packet processors
        for proc in [self.raw_packet_processor, self.corr_packet_processor]:
            if proc:
                proc.stop()
        self.sockets = []
        self.start_time = None

    def start_processing_thread(self):
        self.data_processing_thread = threading.Thread(target=self.receive_packets)
        self.data_processing_thread.setDaemon(True)
        self.data_processing_thread.start()
        # def process_thread(self):
        #     while self.started:
        #         self.process_packets()

    def receive_packets(self, reset_stats=True, timeout=0.001, stop_condition=None, max_time_between_processing=1):

        self.log.info("%r: Starting packet processing" % self)
        self.old_timestamp = None
        self.n_ant_rec = 0
        self.n = 0

        if not self.BUF_SIZE:
            return
        print('Starting receiver')
        last_processing_time = time.time()
        while self.started and (stop_condition is None or not stop_condition()):
            try:
                # Check which sockets have data and read it into the buffer
                # print(f'selecting sockets {self.sockets}')
                sockets_with_data, [], [] = select.select(self.sockets, [], [], timeout)
                # print(f' sockets with data:{sockets_with_data}')
                if sockets_with_data:
                    for sock in sockets_with_data:
                        t0 = time.time()
                        self.buf_packet_length[self.n] = sock.recv_into(self.buf[self.n])
                        self.n += 1
                        self.packet_max_readout_time = max(time.time() - t0, self.packet_max_readout_time)
                        # Process packets if the buffer is full or if we have
                        # been buffering packets for a sufficient period of
                        # time. This is useful when we have a long buffer but
                        # low packet rate.
                        if self.n == self.BUF_SIZE or t0 - last_processing_time > max_time_between_processing:
                            self.process_packets()
                            self.n = 0
                            last_processing_time = time.time()
                elif self.n:
                    # There was not data after the timeout. The pause might be
                    # much longer. Let's process whatever data we have so the user
                    # does not have to wait too long for it.
                    self.process_packets()
                    self.n = 0
                    last_processing_time = time.time()
            except KeyboardInterrupt:
                break



    def process_packets(self):
        """
        Process the data in the buffer by identifying the packet type (ADC,
        FFT, etc) and calling the appropriate processing method.

        inputs:
            self.n: number of packets in the buffer
            self.buf and its structured references: captured packets, in random order

        """
        # print('Processing %i packets' % self.n)

        # Just return if there are no packets to process
        if not self.n:
            return

        t0 = time.time()

        self.received_packets += self.n

        try:
            for p in [self.raw_packet_processor, self.corr_packet_processor]:
                if p:
                    p.process_packets()
        except Exception as e:
            self.log.error('process_packets: exception %r' % e)
            raise

        t3 = time.time()

        self.packet_max_processing_time = max(t3 - t0, self.packet_max_processing_time)
        self.packet_current_processing_time += t3 - t0


    async def get_metrics_async(self):
        """ Gather metrics from all packet processors"""
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

        await asyncio.sleep(0)


        # IOloop health stats
        metrics.add('raw_acq_ioloop_max_response_time', value=self.ioloop_max_response_time)
        metrics.add('raw_acq_ioloop_min_response_time', value=self.ioloop_min_response_time)
        self.ioloop_max_response_time = None
        self.ioloop_min_response_time = None

        metrics.add('raw_acq_run_time', value= 0 if self.start_time is None else time.time() - self.start_time )

        if self.started:
            # Number of packet received by this receiver
            metrics.add('raw_acq_received_packets', value=self.received_packets)

            # Get number of UDP packets dropped by the operating system
            dropped_packets = self.get_udp_dropped_packets(self.port_number)
            for port, dropped in dropped_packets.items():
                metrics.add('raw_acq_udp_dropped_packets', value=dropped, port=port)
            await asyncio.sleep(0)

            # Ping stats
            for (src_ip, src_port), count in self.ping_error_count.items():
                metrics.add('raw_acq_ping_errors', value=count, src_ip=src_ip, src_port=src_port)
            self.ping_error_count = {}

            # Port numbers
            for i, port in enumerate(self.port_number):
                metrics.add('raw_acq_port_number', value=port, index=i, name=self.ports[i])

            with self.lock:
                packet_max_readout_time = self.packet_max_readout_time
                packet_max_processing_time = self.packet_max_processing_time
                self.packet_max_readout_time = 0
                self.packet_max_processing_time = 0
            metrics.add('raw_acq_packet_max_readout_time', value=packet_max_readout_time)
            metrics.add('raw_acq_packet_max_processing_time', value=packet_max_processing_time)

            with self.lock:
                packet_avg_processing_time = (self.packet_current_processing_time * 1.0 * self.NCHAN / self.current_processed_packets) if self.current_processed_packets else 0
                self.packet_current_processing_time = 0
                self.current_processed_packets = 0
            metrics.add('raw_acq_packet_avg_processing_time', value=packet_avg_processing_time)


            # Get metrics from packet processors
            for proc in [self.raw_packet_processor, self.corr_packet_processor]:
                if proc:
                    await proc.get_metrics_async(metrics)

        return metrics

    def get_udp_dropped_packets(self, ports):
        """ Return the number of packet dropped by the operating system IP stack """

        results = {}
        try:
            with open('/proc/net/udp') as fh:
                for line in fh.readlines()[1:]:
                    cols = line.split()
                    try:
                        port = int(cols[1].rsplit(':', 1)[-1], 16)
                        dropped_packets = int(cols[-1])
                        # print('checking port %s against %s' % (port, self.port_number))
                        if port in ports:
                            results[port] = dropped_packets
                    except ValueError:
                        self.log.warning('%r: Bad value while reading system UDP statistics. Problematic line is %s' % (self, cols))
                    time.sleep(0) # relinquish some time to the thread? Not sure if it helps.
        except IOError:
            self.log.warning('%r: Could not read system UDP statistics' % self)

        return results

    def is_running(self):
        return bool(self.sockets)

    async def check_ioloop_response_time_async(self):
        t = time.time()
        if self.ioloop_last_time is not None:
            self.ioloop_max_response_time = max(self.ioloop_max_response_time or 0, t-self.ioloop_last_time)
            self.ioloop_min_response_time = min(self.ioloop_min_response_time or float('inf'), t-self.ioloop_last_time)
        self.ioloop_last_time = t

    def expand_path(self, path, extra_fields={}):
        """ Expand fields in a string.
        """
        fields = {
            'raw_acq_start_time': time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(self.start_time)),
            'data_folder': self.data_folder or '.',
            'run_name': self.run_name or 'Unknown',
            'run_folder': self.run_folder or '.',
            'corr_name': self.corr_name or 'Unknown'
            }
        fields.update(extra_fields)
        return os.path.expanduser(path % fields)


#######################################################
#######################################################
#######################################################
# RAw ADC and FFT packet processor
#######################################################
#######################################################
#######################################################


class RawPacketProcessor(object):
    """ Reads and process the raw ADC of FFT data packets.

    The data is captured and sent over the Gbit Ethernet link by the FPGA
    channelizer's PROBER module. Two types of packets are processed here,
    based on their first byte (cookie). Other packet types are ignored.


    Cookie 0xA0 (PROBER source 0):  is expected to be the FUNCGEN output, typically the raw ADC
        timestream data, which is always in 2's complement.

    Cookie 0xA1 (PROBER source 1) is expected to be FFT data from the SCALER output, typically from the ADC
        data passed through the FFT and SCALER (none of which are bypassed).
        The encoding is defined by `fft_offset_encoding`.
    """

    DATA_SIZE = 2048  # number of bytes of data in packets
    RAW_PACKET_LENGTH = 10 + DATA_SIZE  # total packet size, header + data

    def __init__(self, raw_acq_receiver, stream_ids, metrics_refresh_time=10, adc_rms_refresh_count=100, fft_offset_encoding=True):
        self.log = log.get_logger(self)
        self.recv = raw_acq_receiver

        # Raw data-specific parameters
        self.stream_ids = np.array(stream_ids, dtype=np.uint16) # list of stream ids that we expect to receive
        self.chan_ids = list(zip(*[v.tolist() for v in self.unpack_stream_id(self.stream_ids)])) # make sure all tuple elements are native int

        self.NCHAN = len(stream_ids)


        self.header_dtype = np.dtype(dict(
            names=['cookie', 'stream_id', 'source_crate', 'slot_chan', 'flags', 'ts'],
            offsets=[0, 1, 1, 2, 3, 2],
            formats=['u1', '>u2', 'u1', 'u1', 'u1', '>u8']))

        self.packet_dtype = np.dtype([
            ('header', self.header_dtype, 1),
            ('data', np.int8, self.DATA_SIZE),
            ('padding', np.int8, self.recv.buf.shape[-1] - self.RAW_PACKET_LENGTH)])


        # Useful views into the packet buffer for raw data
        self.buf = self.recv.buf
        self.buf_packet_length = self.recv.buf_packet_length
        self.BUF_SIZE = self.buf.shape[0]
        self.buf_struct = self.buf.view(self.packet_dtype)
        self.buf_cookie = self.buf_struct['header']['cookie'][:, 0]
        self.buf_stream_id = self.buf_struct['header']['stream_id'][:, 0]
        # self.buf_source_crate = self.buf_struct['header']['source_crate'][:, 0]
        self.buf_slot_chan = self.buf_struct['header']['slot_chan'][:, 0]
        self.buf_flags = self.buf_struct['header']['flags'][:, 0]
        self.buf_ts_dirty = self.buf_struct['header']['ts'][:, 0]
        self.buf_data = self.buf_struct['data'][:, 0]

        # Computed buffer parameters
        self.buf_ts = np.empty(self.BUF_SIZE, dtype=np.uint64) # maskeed buf_ts_dirty
        self.buf_source = np.empty(self.BUF_SIZE, dtype=np.uint8)
        self.buf_bank = np.empty(self.BUF_SIZE, dtype=np.uint8)

        # Storage for testing packet length
        self.buf_packet_length_ok = np.empty(self.BUF_SIZE, dtype=bool)

        # Compute the map that associates a stream id with a channel index
        self.sid_map = {sid:ix for ix, sid in enumerate(stream_ids)}
        # Channel index associated with each buffer entry. sid_map is used to update this array each time a block of packets is processed.
        self.buf_chan_ix = np.empty(self.BUF_SIZE, dtype=np.uint16)

        # port number / crate/slot mismatch counters
        self.chan_number_mismatch_count = 0
        self.crate_number_mismatch_count = 0
        self.slot_number_mismatch_count = 0


        # Statistics on how much time it takes to process valid ADC, FFT or
        # CORR packets. The packet count includes only valid packets (right
        # header and length), but length include time to select the valid
        # samples in addition to process them.

        self.processed_packets = 0

        self.lock = threading.RLock()  # Locks access to data while the receiver thread is populating it

        self.adc_processed_packets = 0
        self.adc_max_processing_time = 0
        self.adc_current_processing_time = 0
        self.adc_current_processed_packets = 0

        self.fft_processed_packets = 0
        self.fft_max_processing_time = 0
        self.fft_current_processing_time = 0
        self.fft_current_processed_packets = 0


        self.adc_hdf5_max_processing_time = 0
        self.adc_metrics_max_processing_time = 0
        self.adc_total_hdf5_processing_time = 0
        self.adc_rms_max_processing_time = 0

        # Channel-indexed arrays
        self.stream_id = np.array(stream_ids, dtype=np.uint16) # Stream ID associated with each channel
        self.adc_frames = np.zeros(self.NCHAN, dtype=np.uint32) # Number of packet received for each channel

        self.fixed_port_numbers = False # If True, checks if the crate/slot matches the port number. Assumes that the port numbers have been assigned using a predetermined scheme.

        # ADC Metrics
        self.start_time = time.time()
        self.metrics_refresh_time = metrics_refresh_time
        self.metrics_last_time = np.zeros(self.NCHAN, dtype=np.float64)
        self.metrics_raw_data = np.zeros((self.BUF_SIZE, self.DATA_SIZE), dtype=np.int8) # need to store repeated channels
        self.metrics_updated = np.zeros(self.NCHAN, dtype=np.int8)
        self.metrics_rms = np.zeros(self.NCHAN, dtype=np.float32)
        self.metrics_min = np.zeros(self.NCHAN, dtype=np.float32)
        self.metrics_max = np.zeros(self.NCHAN, dtype=np.float32)
        self.metrics_mean = np.zeros(self.NCHAN, dtype=np.float32)
        self.metrics_jumps = np.zeros(self.NCHAN, dtype=np.float32)
        self.metrics_maxdiff = np.zeros(self.NCHAN, dtype=np.float32)
        self.metrics_adc_packet_length_error = np.zeros(self.NCHAN, dtype=np.int32)
        self.metrics_fft_packet_length_error = np.zeros(self.NCHAN, dtype=np.int32)

        self.expected_ramp = np.arange(-128, self.DATA_SIZE - 128, dtype=np.int8) # Fixed. Used to test ramp errors
        self.metrics_ramp_error_count = np.zeros(self.NCHAN, dtype=np.float32)
        self.metrics_ramp_bit_error_count = np.zeros(self.NCHAN, dtype=np.float32)


        # ADC HDF5 file writer parameters
        self.hdf5_write_time = 0
        self.hdf5_start_time = None
        self.hdf5_run = False
        self.hdf5_base_dir = None
        self.hdf5_file = None
        self.hdf5_refresh_time = 30  # is updated when HDF5 capture is requested
        self.hdf5_last_time = np.zeros(self.NCHAN, dtype=np.float64)
        self.hdf5_block_writes = 0

        # ADC averaged RMS processing
        self.adc_rms_rlock = threading.RLock()
        self.adc_rms_refresh_count = adc_rms_refresh_count # Number of frames to average
        self.adc_rms = np.zeros(self.NCHAN, dtype=np.float32) # final averaged values
        self.adc_rms_timestamp = np.zeros(self.NCHAN, dtype=np.float64)
        self.adc_rms_buffer = np.zeros(self.NCHAN, dtype=np.float32) # used to accumulate square values
        self.adc_rms_mean_buffer = np.zeros(self.NCHAN, dtype=np.int32) # used to accumulate square values
        self.adc_rms_frame_count = np.zeros(self.NCHAN, dtype=np.int32)
        self.adc_rms_updated = np.zeros(self.NCHAN, dtype=bool)


        # FFT processing
        self.fft_offset_encoding_mask = -128 if fft_offset_encoding else 0  # xor'ed with the FFT data to convert into 2's complement
        self.fft_lock = threading.RLock()  # Locks access to data while the receiver thread is populating it
        self.fft_rms_started = np.zeros(self.NCHAN, dtype=bool)
        self.fft_rms_done = np.zeros(self.NCHAN, dtype=np.int8)
        self.fft_target_bank =  np.zeros(self.NCHAN, dtype=np.int8)
        self.fft_rms_buffer =  np.zeros((self.NCHAN, self.DATA_SIZE // 2), dtype=np.int32)
        self.fft_rms_current =  np.zeros((self.NCHAN, self.DATA_SIZE // 2), dtype=np.int32)
        self.fft_rms_old =  np.zeros((self.NCHAN, self.DATA_SIZE // 2), dtype=np.int32)
        self.fft_n_frames =  np.zeros(self.NCHAN, dtype=np.int32)
        self.fft_rms_average = np.zeros(self.NCHAN, dtype=np.int32) + 100
        self.fft_rms = np.zeros((self.NCHAN, self.DATA_SIZE // 2), dtype=np.float32)
        self.fft_overflow =  np.zeros((self.NCHAN, self.DATA_SIZE // 2), dtype=np.int32)
        self.fft_metrics_updated = np.zeros(self.NCHAN, dtype=np.int8)
        self.fft_mean_rms = np.zeros(self.NCHAN, dtype=np.int64)  # debug
        self.fft_mean_rms_old = np.zeros(self.NCHAN, dtype=np.int64) # debug

        # Full frame capture
        self.capture_start = False
        self.capture_done = False
        self.capture_timestamp = None
        self.capture_data = np.zeros((self.NCHAN, self.DATA_SIZE), dtype=np.int8)  # pre-allocate data (channels x bins) for all ports,  for a single timestamp
        # self.all_ts = np.zeros((self.NCHAN), dtype=np.int32) # pre-allocate timestamps storage for the current data for all ports (should all be the same)


    def stop(self):
        if self.hdf5_file:
            self.stop_adc_hdf5()

    def unpack_stream_id(self, stream_id):
        """
        """
        crate = (stream_id >> 8) & 0xF
        slot = (stream_id >> 4) & 0xF
        chan = stream_id & 0xF

        return crate, slot, chan

    def process_packets(self):
        """ Process the packets in the buffer.

        The receiver acquired a lock and the data is guaranteed to be stable during the processing calls.
        """

        t0 = time.time()
        self.process_adc_packets()
        t1 = time.time()
        self.process_fft_packets()
        t2 = time.time()

        self.adc_max_processing_time = max(t1 - t0, self.adc_max_processing_time)
        self.fft_max_processing_time = max(t2 - t1, self.fft_max_processing_time)
        self.adc_current_processing_time += t1 - t0
        self.fft_current_processing_time += t2 - t1

    def process_adc_packets(self):
        """ Process the ADC raw data packets (or more precisely, the data at the output of the function generator)

        The following is done:

            - packets with the right header and length are selected from the reception buffer
            - the channel and buffer index array for ADC data packets (source=0) is computed
            - crate/slot number are compated against port number  if self.fixed_port_numbers is True
            - Expired metrics are updated
            - Data is written to HDF5 file if the sampling period for each channel is reached
            - Full set of packet for one timestamp is captured
            - RMS values are averaged for each incoming packet

        Parameters:


            buf_ix (ndarray): index array that indicates the indices of the ADC data entries in the rx buffer.
        """


           #### Process raw ADC data packets (raw capture source 0) ###
        (buf_ix, ) = np.where(self.buf_cookie[:self.recv.n] == 0xA0)

        if not buf_ix.size:
            return


        self.buf_packet_length_ok[buf_ix] = self.buf_packet_length[buf_ix] == self.RAW_PACKET_LENGTH
        self.metrics_adc_packet_length_error += np.sum(self.buf_packet_length_ok[buf_ix]==False)
        buf_ix = buf_ix[self.buf_packet_length_ok[buf_ix]]

        if not buf_ix.size:
            return

        self.processed_packets += buf_ix.size
        # self.current_processed_packets += buf_ix.size
        self.adc_processed_packets += buf_ix.size
        self.adc_current_processed_packets += buf_ix.size

        self.buf_ts[buf_ix] = self.buf_ts_dirty[buf_ix] & 0xFFFFFFFFFFFF


        while buf_ix.size:

            same_ts = self.buf_ts[buf_ix] == self.buf_ts[buf_ix[0]]

            # Select the buffer index that have the same timestamp and have a valid stream ID
            bix = np.array([b for b in buf_ix[same_ts].tolist() if self.buf_stream_id[b] in self.sid_map], dtype=np.int16)  # make sure we have an integer array, even with an empty list
            # Find the channel index of each incoming packets by looking up their STREAM ID.
            ix = np.array([self.sid_map[sid] for sid in self.buf_stream_id[bix].tolist()], dtype=np.int16) # iterating over a list of int is much faster than over an array of int32

            # removed selected buffer indices for the next iteration
            buf_ix = buf_ix[same_ts == False]


            # if we used fixed port numbers, check that the crate and slot part of the stream ID matches the port number
            # if self.fixed_port_numbers:
            #     base_data_port = 42400
            #     crate, slot, port = self.unpack_stream_id(self.buf_port[buf_ix])
            #     bad_port = self.buf_port[buf_ix] < base_data_port
            #     bad_crate = crate != (self.buf_port[buf_ix] - base_data_port) // 100
            #     bad_slot = slot != (self.buf_port[buf_ix] - base_data_port) % 100
            #     bad = bad_port or bad_crate or bad_slot
            #     self.crate_number_mismatch_count += bad_crate.sum()
            #     self.slot_number_mismatch_count += bad_slot.sum()
            #     # remove bad channels from the channel & buffer indices
            #     ix = ix[not bad]
            #     buf_ix = buf_ix[not bad]


            # keep track of an average rms value for the flagging broker
            # self.rms_cache[ix] = np.std(data)


            # keep track of how many packets we received for each channel. Useful to detect packet loss.
            self.adc_frames[ix] += 1


            t0 = time.time()

            ### Process ADC metrics ###
            self.process_adc_metrics(bix, ix)

            t1 = time.time()

            ### Write ADC data to disk ###
            self.process_adc_hdf5(bix, ix)

            t2 = time.time()

            ### Compute averaged RMS values ###
            self.process_adc_rms(bix, ix)

            t3 = time.time()

            self.adc_metrics_max_processing_time = max(t1 - t0, self.adc_metrics_max_processing_time)
            self.adc_total_hdf5_processing_time += t2 - t1
            self.adc_hdf5_max_processing_time = max(t2 - t1, self.adc_hdf5_max_processing_time)
            self.adc_rms_max_processing_time = max(t3 - t2, self.adc_rms_max_processing_time)


    def process_adc_metrics(self, buf_ix, ix):
        """ Process the data to be used to produce raw-ADC-related metrics.

        Parameters:

            buf_ix (ndarray): index array containing the indices of the ADC packets in the rx buffer

            ix (ndarray): index array containing the indices of the corresponding packets in the channel buffer

        We process only channels whose metrics are older than
        `self.metric_refresh_time`. There is no point in wasting CPU cycles
        updating metrics data faster then the metrics refresh rate

        """


        # Create a boolean array that identifies the channel index of entries that have exprired metrics
        t0 = time.time()
        # print('ix=', ix)
        # print('last_time=', self.metrics_last_time[ix])
        is_expired = (t0 - self.metrics_last_time[ix]) >= self.metrics_refresh_time  # boolean ndarray
        # print('is_expired type', type(is_expired), 'is_expired=', is_expired, repr(ix))
        cix = ix[is_expired]
        if cix.size:
            # print('Updated expired metrics', np.sort(cix))
             # indices of buffer entries that correspond to expired metrics
            bix = buf_ix[is_expired]
            # move the data in a preallocated, contiguous memory block so
            # numpy does not have to do this each time we access it
            # (x[index_array] does NOT create a view, but a copy in newly
            # allocated memory)
            # print(buf_ix, is_expired, bix)
            self.metrics_raw_data[:bix.size] = self.buf_data[bix]
            # create a view into raw_data for convenience. We cannot do y=x[:n]= z[ix] : y is not a view of x
            data = self.metrics_raw_data[:bix.size]

            self.metrics_last_time[cix] = t0
            self.metrics_updated[cix] = True  # will be cleared when the metrics is read out
            self.metrics_mean[cix] = np.mean(data, axis=-1)
            self.metrics_rms[cix] = np.sqrt(np.mean((data - self.metrics_mean[cix, None])**2, axis=-1))  # faster than std()
            self.metrics_min[cix] = np.min(data, axis=-1)
            self.metrics_max[cix] = np.max(data, axis=-1)
            self.metrics_maxdiff[cix] = np.max(np.abs(np.diff(data, axis=-1)), axis=-1)

            # self.ramp_error_count[chan_id] = (
            #     self.ramp_error_count.get(chan_id, 0) +
            #     np.sum(adc_data != self.expected_ramp))
            # for bit in range(8):
            #     mask = 1 << bit
            #     chan_bit_id = (crate_number, slot_number, chan, bit)
            #     self.ramp_bit_error_count[chan_bit_id] = (
            #         self.ramp_bit_error_count.get(chan_bit_id, 0) +
            #         np.count_nonzero((adc_data ^ self.expected_ramp) & mask))
            # for threshold in self.jump_thresholds:
            #     jump_id = (crate_number, slot_number, chan, threshold)
            #     self.jumps[jump_id] = (
            #         self.jumps.get(jump_id, 0) +
            #         np.sum(np.abs(np.diff(adc_data)) > threshold))


    def process_adc_hdf5(self, buf_ix, ix):
        """ Write data to HDF file

        Parameters:

            buf_ix (ndarray): index array containing the indices of the ADC packets in the rx buffer

            ix (ndarray): index array containing the indices of the corresponding packets in the channel buffer


        We write data for channels that have not been written for at least self.hdf5_refresh_time
        """
        if self.hdf5_file:
            t0 = time.time()
            # find the channel index of channels that need to be written
            is_old = (t0 - self.hdf5_last_time[ix]) > self.hdf5_refresh_time  # boolean ndarray
            # find buffer index of entries that should be written
            bix = buf_ix[is_old]
            if bix.size:
                # update the last time of the channels . We use the boolean array directly, since we don't need to reuse an channel index array anymore
                self.hdf5_last_time[ix[is_old]] = t0
                # save the selected entries. Unfortunately, the array indexing
                # buf_x[bix] will cause copies to be created for each
                # argument. To avoid this extra copy, we would have to pass
                # bix separately, and let the copy happen only when we
                # transfer the data to the hdf5 internal buffers.
                self.hdf5_file.write(
                    self.buf_ts[bix],
                    self.buf_stream_id[bix],
                    self.buf_flags[bix],
                    self.buf_data[bix])

            # keep track of how many packets we write and how much time it
            # takes so we can get an average that informs us of the maximum
            # packet rate we can sustain
            dt = time.time() - t0
            # self.log.info('%r: it took %.3f ms to write %i packets to HDF5 file' % (self, dt*1000, len(bix)))
            self.hdf5_block_writes += 1

    def start_adc_hdf5(self,
                       base_dir,
                       base_filename,
                       capture_duration=60,
                       capture_refresh_time=0,
                       elements_per_file=2048*64):
        if self.hdf5_file:
            self.stop_adc_hdf5()
            # raise RuntimeError('HDF5 dataWriter is already running')
        self.log.info('%r: Starting HDF5 raw data data writer with base_dir=%s, base_filename=%s, capture_duration=%r (type=%s), elements_per_file=%r' %
            (self, base_dir, base_filename, capture_duration, type(capture_duration), elements_per_file))
        self.elements_per_file = elements_per_file

        self.hdf5_start_time = time.time() # used to keep track of how long the disk capture has been running

        self.hdf5_refresh_time = capture_refresh_time

        # Schedule for the acquisition to stop if capture_ducation is non-zero
        if capture_duration:
            self.log.info('%r: HDF5 raw data data writer will be stopped in %f seconds' % (self, capture_duration))
            asyncio.get_event_loop().call_later(capture_duration, self.stop_adc_hdf5)


        # Create the target folder
        extra_fields = dict(
            hdf5_start_time=datetime.datetime.utcnow().strftime('%Y%m%dT%H%M%SZ'))

        self.hdf5_base_dir = self.recv.expand_path(base_dir, extra_fields)
        try:
            os.makedirs(self.hdf5_base_dir)
        except:
            self.log.warning("%r: couldn't make directory '%s'. Using current directory." %
                (self, self.hdf5_base_dir))
            self.hdf5_base_dir = './'

        self.hdf5_file = HDF5RawWriter(base_dir=self.hdf5_base_dir,
                                       filename=base_filename,
                                       elements_per_file=self.elements_per_file)


    def stop_adc_hdf5(self):
        if not self.hdf5_file:
            raise RuntimeError('%r: HDF5 dataWriter is not running. Cannot stop it.' % self)
        self.log.info('%r: Stopping HDF5 data writer' % self)
        hdf5_file = self.hdf5_file
        self.hdf5_file = None # Stop the thread from using the file before we close it
        hdf5_file.close()
        self.hdf5_start_time = None
        self.log.info('%r: Write %i data blocks in %.3f s total (%.0f ms/write)' % (self, self.hdf5_block_writes, self.adc_hdf5_max_processing_time, (self.adc_hdf5_max_processing_time * 1000. / self.hdf5_block_writes)) if self.hdf5_block_writes else 0)


    def process_adc_rms(self, buf_ix, ix):
        """ Compute averaged RMS values

        Parameters:

            buf_ix (ndarray): index array containing the indices of the ADC packets in the rx buffer

            ix (ndarray): index array containing the indices of the corresponding packets in the channel buffer


        """

        self.adc_rms_buffer[ix] += np.var(self.buf_data[buf_ix], axis=-1)
        self.adc_rms_frame_count[ix] += 1
        complete_ix, = np.where(self.adc_rms_frame_count[ix] == self.adc_rms_refresh_count)
        if complete_ix.size:
            cix = ix[complete_ix]
            with self.adc_rms_rlock:
                self.adc_rms[cix] = np.sqrt(self.adc_rms_buffer[cix] / self.adc_rms_frame_count[cix])
                self.adc_rms_timestamp[cix] = time.time()
                self.adc_rms_frame_count[cix] = 0
                self.adc_rms_buffer[cix] = 0
                self.adc_rms_updated[cix] = 1



    async def get_adc_rms_async(self):
        """ Return the latest averaged ADC RMS values.


        Returns:

            A list of (chan_id, timestamp, rms) tuples, where:

                chan_id: a (crate, slot, channel) tuple

                timestamp: the ctime at which the rms value was refreshed for the last time. ``None`` if it was never refreshed.

                rms (float): the RMS value for that channel, averged over the number of frames that was specified at initialization
        """
        with self.adc_rms_rlock:
            result = list(zip(
                self.chan_ids,
                self.adc_rms_timestamp.tolist(),
                self.adc_rms.tolist()))
        return result


        #########################################
        # Update averaged RMS values
        #########################################

        # self.current_ts[buf][j][chan] = timestamp
        # self.current_data[buf][j][chan, :] = adc_data
        # self.current_crate[j][chan] = crate_number
        # self.current_slot[j][chan] = slot_number


    def process_adc_frame_capture(self, buf_ix, ix):
        """

        #########################################
        # Capture a full set of data with the same timestamp
        #########################################
        # Accumulate packets in a buffer. Settarget timestamp from the hihest timestamp of a packet that contains multiple timestamps
        """
        pass
        # if self.capture:
        #     self.buf_ts[buf_ix] = self.buf_ts[buf_ix] & 0xFFFFFFFFFFFF  # 48 bit timestamp. Mask extra bits.
        #     if self.capture_timestamp is None:
        #         max_timestamp = np.max(buf_ts[buf_ix])
        #         if self.capture_last_timestamp is None:
        #             self.capture_last_timestamp = max_timestamp
        #         elif self.capture_last_timestamp != max_timestamp:
        #             self.capture_timestamp = max_timestamp
        #         self.capture_last_timestamp = max_timestamp
        #     # We have a potentially updated self.capture_timestamp
        #     if self.capture_timestamp is not None:
        #         ts_match = self.buf_ts[buf_ix] == self.capture_timestamp
        #         bix = buf_ix[ts_match]
        #         if not bix.size: # no more packets with the target timestamp
        #             self.capture = False
        #             self.capture_done = True
        #         else:
        #             cix = ix[ts_match]
        #             self.capture_data[cix] = self.buf_data[bix]
        #             self.capture_valid[cix] = True

        # Capture a full timestamp set if self_capture = True
        # if self.capture_start:
        #     self.all_ts[port][chan] = timestamp
        #     self.all_data[port][chan, :] = adc_data
        #     if (timestamp == self.old_timestamp):
        #         self.n_ant_rec += 1
        #     else:
        #         self.old_timestamp = timestamp
        #         self.n_ant_rec = 1
        #     if self.n_ant_rec >= self.N_CHANNELS - 1:
        #         self.n_ant_rec = 0
        #         self.old_timestamp = None
        #         self.capture_start = False


    async def get_data_async(self):
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
            await asyncio.sleep(0)

        return self.all_ts, self.ports, self.all_data


    def process_fft_packets(self):
        """ Process the FFT data (or more precisely, the data at the output of the scaler) This corresponds to data tagged with source=1.

        - Compute an average per-bin RMS over self.fft_rms_average samples

        """


        (buf_ix, ) = np.where(self.buf_cookie[:self.recv.n] == 0xA1)

        if not buf_ix.size:
            return


        self.buf_packet_length_ok[buf_ix] = self.buf_packet_length[buf_ix] == self.RAW_PACKET_LENGTH
        self.metrics_fft_packet_length_error += np.sum(self.buf_packet_length_ok[buf_ix]==False)
        buf_ix = buf_ix[self.buf_packet_length_ok[buf_ix]]

        if not buf_ix.size:
            return


        self.processed_packets += buf_ix.size
        # self.current_processed_packets += buf_ix.size
        self.fft_processed_packets += buf_ix.size
        self.fft_current_processed_packets += buf_ix.size

        self.buf_ts[buf_ix] = self.buf_ts_dirty[buf_ix] & 0xFFFFFFFFFFFF


        while buf_ix.size:

            same_ts = self.buf_ts[buf_ix] == self.buf_ts[buf_ix[0]]

            # Select the buffer index that have the same timestamp and have a valid stream ID
            bix = np.array([b for b in buf_ix[same_ts].tolist() if self.buf_stream_id[b] in self.sid_map], dtype=np.int16)
            # Find the channel index of each incoming packets by looking up their STREAM ID.
            ix = np.array([self.sid_map[sid] for sid in self.buf_stream_id[bix].tolist()], dtype=np.int16) # iterating over a list of int is much faster than over an array of int32
            # Remove selected buffer indices for the next iteration
            buf_ix = buf_ix[same_ts == False]


            # print('Processing %i FFT frames (%i remains).************************ ' % (bix.size, buf_ix.size))


            with self.fft_lock:
                # Accumulate the square of the magnitude of the frequency samples. This corresponds to re**2 + im**2. We never actually use complex numbers, which saves CPU cycles.
                #
                # We xor with -128 to convert offect binary into two's complement (do not use +128, it is an int16)
                # We then right-shift by four, which preserves the sign
                #
                # Square of values from -8 to 7 fit in an int8, but not the sum of two. So we add the squares re and im values separately into the int32 buffer
                # todo: check if there is a more efficient way to do this
                # c = ((self.buf_data[buf_ix, ::2]^-128)>>4).astype(complex)+ 1j*((self.buf_data[buf_ix, 1::2]^-128)>>4).astype(complex)
                self.fft_rms_current[ix] = ((self.buf_data[bix, ::2] ^ self.fft_offset_encoding_mask) >> 4) ** 2
                self.fft_rms_current[ix] += ((self.buf_data[bix, 1::2] ^ self.fft_offset_encoding_mask) >> 4) ** 2
                self.fft_overflow[ix, ::2] += (self.buf_data[bix, ::4] & 0b0100) != 0
                self.fft_overflow[ix, 1::2] += (self.buf_data[bix, ::4] & 0b0010) != 0


                self.fft_metrics_updated[ix] = True


                # Extract the bank number for the incoming FFT packets
                self.buf_bank[bix] = (self.buf_flags[bix] >> 6) & 1

                # keep only those channels who are not done and who match the target bank
                is_valid = np.logical_and(self.fft_rms_done[ix] == False, self.fft_target_bank[ix] == self.buf_bank[bix])
                # print(is_valid, ix, bix, (self.buf_data[bix] ^ -128) >> 4)
                cix = ix[is_valid]
                if cix.size:
                    # print('streanm ids', self.buf_stream_id[bix])

                    self.fft_rms_buffer[cix] += self.fft_rms_current[cix]
                    self.fft_n_frames[cix] += 1
                    # find which frames have reached their total:
                    # print('Processing:  FFT n_frames are %s.************************ ' % self.fft_n_frames)

                    cix = cix[self.fft_n_frames[cix] == self.fft_rms_average[cix]]
                    if cix.size:
                        # print('Processing: %i FFT packets are done.************************ ' % cix.size)

                        self.fft_rms_done[cix] = True
                        self.fft_rms[cix] = np.sqrt(self.fft_rms_buffer[cix].astype(np.float32) / self.fft_n_frames[cix, None])
                        self.fft_rms_buffer[cix] = 0
                        # print('Completed channels', np.sort(cix))

    async def start_fft_rms_async(self, stream_ids, target_gain_bank, number_of_frames=100):
        """ Start the acquisition of averages RMS data from the FFT data using
        the specified target bank. `get_fft_rms()` should be polled to
        retreive the data products that are ready.


        This method can be called multiple times.

        """
        ix = [self.sid_map[sid] for sid in stream_ids]
        if not ix:
            return
        with self.fft_lock:
            self.fft_target_bank[ix] = target_gain_bank
            self.fft_rms_average[ix] = number_of_frames
            self.fft_n_frames[ix] = 0
            self.fft_rms_buffer[ix] = 0
            self.fft_overflow[ix] = 0
            self.fft_rms_done[ix] = False
            self.fft_rms_started[ix] = True



    async def get_fft_rms_async(self, all_done=True):
        """ Returns FFT RMS data products that are ready.

        Returns:

            (stream_ids, rms) tuple, where:

                stream_ids is a ndarray(N)  containing the stream ID of completed channels
                rms is an ndarray(N, 1024) containing the corresponding rms-averages FFT spetra
        """
        # t0 = time.time()
        # await self.start_fft_rms_async(stream_ids=stream_ids, target_gain_bank=target_gain_bank, number_of_frames=number_of_frames)

        # ix = np.array([self.sid_map[sid] for sid in stream_ids], dtype=np.int16)

        # t1 = time.time()
        # self.log.info('%r: get_fft_rms: done vector= %s' % (self, self.fft_rms_done))
        # if self.lock.locked():
        #     print('get_fft_rms: is locked!')
        # with threading.Lock():
        with self.fft_lock:
            # if all_done and not any(self.fft_rms_done[self.fft_rms_started]):
            #     return np.array([], dtype=np.int16),np.array([])))
            #     # while not all(self.fft_rms_done):
                #     # print(self.fft_rms_done[ix])
                #     time.sleep(0.001) # give some time to run the receiver thread
                #     await asyncio.sleep(0)
            # t2 = time.time()
            # print('FFT RMS acquisition done, setup=%.3f ms, acq=%.3f ms, total=%.3f' % ((t1-t0)*1000, (t2-t1)*1000, (t2-t0)*1000))
            ix = np.logical_and(self.fft_rms_started, self.fft_rms_done)
            sid = self.stream_ids[ix]
            rms = self.fft_rms[ix]
            self.fft_rms_started[ix] = False
        self.log.info('%r: get_fft_rms returned FFT RMS vectors from %i channels' % (self, sid.size))
        return ((sid, rms))



    async def get_metrics_async(self, metrics):
        """ Gathers the Raw acquisition related metrics (ADC and FFT)

        Parameters:

            metrics (object): Metrics object in which the metrics will be added
        """


        # Disk usage on the HDF5 file destination volume
        if hasattr(os, 'statvfs') and self.hdf5_base_dir:
            s = os.statvfs(self.hdf5_base_dir)
            metrics.add('raw_acq_disk_size', value=s.f_blocks * s.f_bsize)
            metrics.add('raw_acq_disk_used', value=(s.f_blocks - s.f_bfree) * s.f_bsize)
            metrics.add('raw_acq_disk_free', value=s.f_bfree * s.f_bsize)
            metrics.add('raw_acq_disk_percent_used', value=(s.f_blocks - s.f_bfree) / s.f_blocks)
            metrics.add('raw_acq_disk_percent_free', value=s.f_bfree / s.f_blocks)
            await asyncio.sleep(0)

        # HDF5 file writing stats
        metrics.add('raw_acq_hdf5_run_time', value= 0 if self.hdf5_start_time is None else time.time() - self.hdf5_start_time )

        metrics.add('raw_acq_hdf5_write_time', value=self.hdf5_write_time)
        self.hdf5_write_time = 0
        if self.hdf5_file:
            metrics.add('raw_acq_hdf5_n_elements', value=self.hdf5_file.nn + self.hdf5_file.n)
            metrics.add('raw_acq_hdf5_n_elements_max', value=self.hdf5_file.elements_per_file)
            metrics.add('raw_acq_hdf5_number_of_files', value=self.hdf5_file.file_number)

        await asyncio.sleep(0)



        metrics.add('raw_acq_processed_packets', value=self.processed_packets)
        metrics.add('raw_acq_processed_adc_packets', value=self.adc_processed_packets)
        metrics.add('raw_acq_processed_fft_packets', value=self.fft_processed_packets)
        metrics.add('raw_acq_hdf5_data_block_writes', value=self.hdf5_block_writes)



        # Generate metrics on the maximum processing time since the value was queried
        with self.lock:
            adc_max_processing_time = self.adc_max_processing_time
            fft_max_processing_time = self.fft_max_processing_time
            adc_metrics_max_processing_time = self.adc_metrics_max_processing_time
            adc_hdf5_max_processing_time = self.adc_hdf5_max_processing_time
            adc_rms_max_processing_time = self.adc_rms_max_processing_time
            self.adc_max_processing_time = 0
            self.fft_max_processing_time = 0
            self.adc_metrics_max_processing_time = 0
            self.adc_hdf5_max_processing_time = 0
            self.adc_rms_max_processing_time = 0

        metrics.add('raw_acq_adc_max_processing_time', value=adc_max_processing_time)
        metrics.add('raw_acq_fft_max_processing_time', value=fft_max_processing_time)
        metrics.add('raw_acq_adc_metrics_max_processing_time', value=adc_metrics_max_processing_time)
        metrics.add('raw_acq_adc_hdf5_max_processing_time', value=adc_hdf5_max_processing_time)
        metrics.add('raw_acq_adc_rms_max_processing_time', value=adc_rms_max_processing_time)


        # Generate metrics on the average processing time since last query
        with self.lock:
            adc_avg_processing_time = (self.adc_current_processing_time * 1.0 * self.NCHAN / self.adc_current_processed_packets) if self.adc_current_processed_packets else 0
            fft_avg_processing_time = (self.fft_current_processing_time * 1.0 * self.NCHAN / self.fft_current_processed_packets) if self.fft_current_processed_packets else 0
            self.adc_current_processing_time = 0
            self.fft_current_processing_time = 0
            self.adc_current_processed_packets = 0
            self.fft_current_processed_packets = 0

        metrics.add('raw_acq_adc_avg_processing_time', value=adc_avg_processing_time)
        metrics.add('raw_acq_fft_avg_processing_time', value=fft_avg_processing_time)



        await asyncio.sleep(0)

        cix, = np.where(self.metrics_updated)  # boolean ndarray

        for ix in cix:

            with self.lock:
                adc_frames = self.adc_frames[ix]
                metrics_rms = self.metrics_rms[ix]
                metrics_min = self.metrics_min[ix]
                metrics_max = self.metrics_max[ix]
                metrics_mean = self.metrics_mean[ix]
                metrics_maxdiff = self.metrics_maxdiff[ix]
                metrics_ramp_error_count = self.metrics_ramp_error_count[ix]
                metrics_adc_packet_length_error = self.metrics_adc_packet_length_error[ix]

            crate, slot, chan = self.unpack_stream_id(self.stream_id[ix])
            # print('Addingn rms metric for cix=%s : crate=%s, slot=%s, chan=%s, value = %f' % (ix, crate, slot, chan, self.metrics_rms[ix]))
            metrics.add('raw_acq_adc_frames', value=adc_frames, crate=crate, slot=slot, chan=chan)
            metrics.add('raw_acq_rms', value=metrics_rms, crate=crate, slot=slot, chan=chan)
            metrics.add('raw_acq_min', value=metrics_min, crate=crate, slot=slot, chan=chan)
            metrics.add('raw_acq_max', value=metrics_max, crate=crate, slot=slot, chan=chan)
            metrics.add('raw_acq_mean', value=metrics_mean, crate=crate, slot=slot, chan=chan)
            metrics.add('raw_acq_max_diff', value=metrics_maxdiff, crate=crate, slot=slot, chan=chan)
            metrics.add('raw_acq_ramp_errors', value=metrics_ramp_error_count, crate=crate, slot=slot, chan=chan)
            metrics.add('raw_acq_adc_packet_length_error', value=metrics_adc_packet_length_error, crate=crate, slot=slot, chan=chan)
            # for bit, count in enumerate(self.metrics_ramp_bit_error_count[ix]):
            #     metrics.add('raw_acq_ramp_bit_errors', value=count, crate=crate, slot=slot, chan=chan, bit=bit)
            # for i, count in enumerate(self.metrics_jumps[ix]):
            #     metrics.add('raw_acq_jumps', value= count, crate=crate, slot=slot, chan=chan, threshold=self.threshold[i])
            time.sleep(0.0001) # relinquish some time to the thread? Not sure if it helps.
            await asyncio.sleep(0)
        self.metrics_updated[cix] = False

        # ix, = np.where(self.fft_metrics_updated)  # boolean ndarray

        # if not np.array_equal(self.fft_mean_rms_old[ix], self.fft_mean_rms[ix]):
        #     print('fft rms differ!')
        # self.fft_mean_rms_old[ix] = self.fft_mean_rms[ix]


        # self.is_locked=True
        cix, = np.where(self.fft_metrics_updated)  # boolean ndarray
        # self.fft_mean_rms[cix] = np.sum(self.fft_rms_current[cix, 1:].astype(np.float32), axis=-1)
        for ix in cix:
            with self.fft_lock:
                stream_id = self.stream_id[ix]
                fft_rms_current = self.fft_rms_current[ix]
                metrics_fft_packet_length_error = self.metrics_fft_packet_length_error[ix]
                fft_scaler_overflows = self.fft_overflow[ix, 1:] # skip bin 0

            crate, slot, chan = self.unpack_stream_id(stream_id)
            metrics.add('raw_acq_fft_rms', value=np.sqrt(np.mean(fft_rms_current)), crate=crate, slot=slot, chan=chan)
            metrics.add('raw_acq_fft_packet_length_error', value=metrics_fft_packet_length_error, crate=crate, slot=slot, chan=chan)
            metrics.add('raw_acq_fft_scaler_overflows', value=np.sum(fft_scaler_overflows), crate=crate, slot=slot, chan=chan)
            time.sleep(0.0001) # relinquish some time to the thread? Not sure if it helps.
            await asyncio.sleep(0)
            self.fft_metrics_updated[cix] = False
            # self.is_locked=False

        # Add the averaged adc rms metrics
        with self.adc_rms_rlock:
            cix, = np.where(self.adc_rms_updated)
            if cix.size > 0:
                c_stream_id = self.stream_id[cix]
                c_adc_rms = self.adc_rms[cix]
                self.adc_rms_updated[cix] = False
            else:
                c_stream_id = []
                c_adc_rms = []

        for csid, crms in zip(c_stream_id, c_adc_rms):
            crate, slot, chan = self.unpack_stream_id(csid)
            metrics.add('raw_acq_adc_averaged_rms', value=crms, crate=crate, slot=slot, chan=chan)
            await asyncio.sleep(0)

        metrics.add('raw_acq_run_time', value=0 if self.start_time is None else time.time() - self.start_time)

        # Packet integrity stats

        metrics.add('raw_acq_chan_mismatch', value=self.chan_number_mismatch_count)
        metrics.add('raw_acq_crate_mismatch', value=self.crate_number_mismatch_count)
        metrics.add('raw_acq_slot_mismatch', value=self.slot_number_mismatch_count)

#######################################################
#######################################################
#######################################################
# Raw ADC HDF5 data writer
#######################################################
#######################################################
#######################################################



class HDF5RawWriter(object):
    """ Object representing a HDF5 file containing raw data
    """
    def __init__(self, base_dir='.', filename='%(file_number)06d.h5', elements_per_file=2048*64, crate_and_slot_from_port=False, chunk_size=1024):
        self.log = log.get_logger(self)
        self.N_SAMP = 2048 # data bytes per frame
        self.base_dir = base_dir
        self.filename = filename
        self.chunk_size = chunk_size
        #self.N_CHANNELS = 1
        self.crate_and_slot_from_port = crate_and_slot_from_port
        # self.filename = filestring
        self.file_number = 0
        self.nn = 0 # sample number of the first sample of the current file
        self.elements_per_file = elements_per_file
        self.f = None
        self.start_new_hdf5_file()

    def start_new_hdf5_file(self):
        self.close()
        fields = dict(
            file_number=self.file_number)

        filename = os.path.join(self.base_dir, self.filename % fields)
        self.open(filename)
        self.n = 0
        self.file_number += 1

    def open(self, filename):
        self.current_filename = filename
        self.lock_filename = self.current_filename + '.lock'

        # # create a lock file
        with open(self.lock_filename,'w') as h:
            h.write('locked\n')

        self.log.info('%r: Opening raw data HDF5 file %s' % (self, self.current_filename))
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
        self.f.attrs["timestamping_warning"] = "Done on file write, may be significantly different from snapshot acquistion time"

        # timestamp
        self.compound_dtype = np.dtype([('fpga_count', np.uint64), ('ctime', np.float64)])
        self.timestampDataset = self.f.create_dataset('timestamp',
            (1, 1), dtype=self.compound_dtype, maxshape=(None, 1), chunks=(self.chunk_size, 1))
        self.timestampDataset.attrs['axis'] = ['snapshot']

        # slot number
        self.slotDataset = self.f.create_dataset('slot', (1, 1),
            dtype=np.uint8, maxshape=(None, 1), chunks=(self.chunk_size, 1))
        self.slotDataset.attrs['axis'] = ['snapshot']

        # crate number
        self.crateDataset = self.f.create_dataset('crate', (1, 1),
            dtype=np.uint32, maxshape=(None, 1), chunks=(self.chunk_size, 1))
        self.crateDataset.attrs['axis'] = ['snapshot']

        # channel number
        self.chanDataset = self.f.create_dataset('adc_input', (1, 1),
            dtype=np.uint8, maxshape=(None, 1), chunks=(self.chunk_size, 1))
        self.chanDataset.attrs['axis'] = ['snapshot']

        # ADC data
        self.timestreamDataset = self.f.create_dataset('timestream',
            (1, self.N_SAMP), dtype=np.int8,
            maxshape=(None, self.N_SAMP), chunks=(self.chunk_size, self.N_SAMP))
        self.timestreamDataset.attrs['axis'] = ['snapshot', 'timestream']

        self.index_map = self.f.create_group("index_map")

        self.snapshot_index_map = self.index_map.create_dataset('snapshot',
            (1,), dtype=np.uint32, maxshape=(None,), chunks=(self.chunk_size, ))

        self.timestream_index_map = self.index_map.create_dataset("timestream",
            (2048,), dtype=np.uint16)
        self.timestream_index_map[:] = np.arange(2048)

        # self.n_times = 1
        self.n = 0 # number of samples fince start of file


    def write(self, timestamp, stream_id, flags, timestream):
        """
        """


        n1 = self.n
        self.n = n2 = n1 + timestamp.shape[0]

        # self.log.info('%r: Writing %i entries to HDF5 file %s' % (self, timestamp.size, self.current_filename))

        self.timestampDataset.resize((self.n, 1))
        self.crateDataset.resize((self.n, 1))
        self.slotDataset.resize((self.n, 1))
        self.chanDataset.resize((self.n, 1))
        self.timestreamDataset.resize((self.n, self.N_SAMP))

        current_time = time.time()
        slot_number = (stream_id >> 4) & 0xF
        crate_number = (stream_id >> 8) & 0xF
        chan_number = (stream_id ) & 0xF

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
        self.timestreamDataset[n1:n2] = timestream

        if n2 >= self.elements_per_file:
            self.start_new_hdf5_file()


    def close(self):
        if self.f:
            self.snapshot_index_map.resize((self.n,))
            self.snapshot_index_map[:] = np.arange(self.n) + self.nn
            self.nn += self.n

            self.log.info('%r: Closing HDF5 file %s' % (self, self.current_filename))
            self.f.close()
            try:
                os.remove(self.lock_filename)
                # os.rename(self.lock_filename, self.filename)
            except OSError:
                self.log.error('%r: Unable to rename HDF5 lock file from %s to %s' % (self, self.lock_filename, self.current_filename))


#######################################################
#######################################################
#######################################################
# Correlator packet processor
#######################################################
#######################################################
#######################################################


class CorrPacketProcessor(object):
    """ Reads the raw data in a socket-like interface"""

    DATA_SIZE = 2048 # number of bytes of data
    RAW_PACKET_LENGTH = 10 + DATA_SIZE  # header length + data length

    def __init__(self,
                 raw_acq_receiver,
                 firmware_integration_period=1,
                 software_integration_period=100, # Can be changed by hdf start
                 frame0_irigb_time=0,
                 ):
        self.log = log.get_logger(self)
        self.recv = raw_acq_receiver
        self.software_integration_period = software_integration_period
        self.firmware_integration_period = firmware_integration_period
        self.frame0_irigb_time = frame0_irigb_time

        # Correlator geometry

        self.NCHAN = CORR.NCHAN
        self.NCORR = CORR.NCORR
        self.NCMAC = CORR.NCMAC_PER_CORR
        self.NPROD = CORR.NPROD_PER_CMAC
        self.PACKET_SIZE = CORR.NBYTES_PER_HEADER + CORR.NPROD_PER_CMAC * CORR.NBYTES_PER_PROD

        # Define numpy data types that will be used to efficiently parse the data
        self.product_dtype = np.dtype(dict(
            names=['sat', 'h', 'l'],
            offsets=[4, 1, 0],
            formats=['u1', '<i4', '<i4']))

        self.packet_dtype = np.dtype([
            ('cookie', np.uint8, 1),
            ('proto', np.uint8, 1),
            ('corr', np.uint8, 1),
            ('cmac', np.uint8, 1),
            ('geometry', '<u4', 1),
            ('ts', '<u4', 1),
            ('data', self.product_dtype, self.NPROD)])



        self.corr_processed_packets = 0
        self.corr_max_processing_time = 0
        self.corr_current_processed_packets = 0
        self.corr_current_processing_time = 0
        self.metrics_corr_packet_length_error = 0

        # Header fields



        # Pre-allocate buffers
        # Buffer in which the correlator packets will be assembled
        self.BUF_SIZE = self.recv.buf.shape[0]
        self.buf = self.recv.buf
        self.buf_packet_length = self.recv.buf_packet_length

        # Storage for testing packet length
        self.buf_packet_length_ok = np.empty(self.BUF_SIZE, dtype=bool)

        # Various views of the buffer to allow quick and easy access to the packet contents
        self.buf_struct = self.buf.view(self.packet_dtype)
        # Header fields
        self.buf_cookie = self.buf_struct['cookie'][:,0]
        self.buf_corr = self.buf_struct['corr'][:,0]
        self.buf_cmac = self.buf_struct['cmac'][:,0]
        self.buf_ts = self.buf_struct['ts'][:,0] # watch out! Covers part of the stream id
        # data fields
        self.buf_data_h = self.buf_struct['data'][:,0]['h']
        self.buf_data_l = self.buf_struct['data'][:,0]['l']
        self.buf_data_sat = self.buf_struct['data'][:,0]['sat']

        # Temporary storage to extract the real/imaginary part from the 5-byte packed product
        self.temp32 = np.empty((self.BUF_SIZE, self.NPROD), dtype=np.int32)

        # Storage for the accumulated value
        self.acc_re = np.zeros((self.NCORR, self.NCMAC, self.NPROD), dtype=np.int64)
        self.acc_im = np.zeros((self.NCORR, self.NCMAC, self.NPROD), dtype=np.int64)
        # Number of saturations for the real and imaginary part of each product
        self.sat = np.zeros((self.NCORR, self.NCMAC, self.NPROD, 2), dtype=np.int32)
        self.sat_cplx = np.zeros((self.NCORR, self.NCMAC, self.NPROD), dtype=np.complex64)
        # Number of packets received for each NCMAC (and therefore each
        # product). Can be used to know how many packets were lost and to
        # normalize the data
        self.count = np.zeros((self.NCORR, self.NCMAC), dtype=np.uint32)
        # self.ts = np.zeros((number_of_results, self.NCORR, self.NCMAC), dtype=np.uint64)

        # Complex value data results
        #
        # Each correlator frame has a (18+18) bit resolution, which is then
        # integrated for some time. Assuming the worst case of a saturatet 1
        # Gb/s link sending the maximum value if 2**17, we would get 175.8
        # correlator frames/s,  with soft integrator values of increase by 2**24.46/s. If we integrate for
        # we
        #
        # A float32 can represent integers values exactly up to 2**24, which
        # leaves room for less than one second of integration in the worst
        # case. We cannot thereofre use a complex64 value (float32+float32),
        # and thereofre use a complex128 format, which can represent integers exactly up to 2**53.
        self.data = np.zeros((self.NCORR, self.NCMAC, self.NPROD), dtype=np.complex128)

        self.hdf5_file = None
        self.hdf5_start_time = None

        self.flushed_packets = 0
        self.state = 'align'
        self.last_ts = None
        self.current_integ = None
        self.n = 0  # number of packets currently stored in the buffer
        self.packets = 0

        print('Initialized correlator packet processor')

    def stop(self):
        if self.hdf5_file:
            self.hdf5_file.close()

    def process_packets(self):

        t0 = time.time()
        self.process_corr_packets()
        t1 = time.time()

       # self.buffer_preprocessing_time = max(t1 - t0, self.buffer_preprocessing_time)
        self.corr_max_processing_time = max(t1 - t0, self.corr_max_processing_time)
        self.corr_current_processing_time += t1 - t0


    def process_corr_packets(self):
        """ Process the firmware correlator data

        - Compute an average per-bin RMS over self.fft_rms_average samples

        """

        if not self.software_integration_period:
            return

        # find the buffer indices of the correlator packets by looking at their header
        (buf_ix, ) = np.where(self.buf_cookie[:self.recv.n] == 0xBF)

        if not buf_ix.size:
            return

        # Eliminate packets with the wrong length
        self.buf_packet_length_ok[buf_ix] = self.buf_packet_length[buf_ix] == self.PACKET_SIZE
        self.metrics_corr_packet_length_error += np.sum(self.buf_packet_length_ok[buf_ix] == False)
        buf_ix = buf_ix[self.buf_packet_length_ok[buf_ix]]

        if not buf_ix.size:
            return

        # self.processed_packets += buf_ix.size
        # self.current_processed_packets += buf_ix.size
        self.corr_processed_packets += buf_ix.size
        self.corr_current_processed_packets += buf_ix.size

        while buf_ix.size:

            if self.last_ts is None:
                self.last_ts = self.buf_ts[buf_ix[0]]
                buf_ix = buf_ix[1:]

            # Discard packets until we have a timestamp jump of at least 2 frames to flush the OS buffer
            elif self.state == 'flush1':
                # initialize last_ts. This will cause the side effect of the first packet to be always flushed.
                buf_ix = self.flush(buf_ix, 2)
                if buf_ix.size:
                    # the top ts has a jump of 2. We must force skipping it
                    self.last_ts = self.buf_ts[buf_ix[0]]
                    self.state = 'flush2'

            # Discard packets until we have a timestamp jump of at least 1 frames to flush the probably partial first frame
            elif self.state == 'flush2':
                buf_ix = self.flush(buf_ix, 1)
                if buf_ix.size:
                    print('Flushed %i UDP packets in total' % (self.flushed_packets))
                    self.state = 'align'

            # Wait until the frame is aligned to our integration period
            elif self.state == 'align':
                buf_ix = self.align(buf_ix)
                if buf_ix.size:
                    self.clear_data()
                    self.current_integ = None
                    self.state = 'integ'
            elif self.state == 'integ':
                self.integ(buf_ix)
                buf_ix = buf_ix[[]]
            else:
                raise RuntimeError('Unknown correlator state %s' % self.state)


    def flush(self, bix, jump=2):
        """ Flush the UDP buffer until the timout occurs or the packet timestamp jumps by more `threshold` or more.

        This function clears the local software buffer.

        If a timestamp jump is detected, we assume that we are now reading the
        part of a frame that could fit in the UDP buffer because we started
        flushing it. The frame is likely partial. For this reason, onece we
        detect a large jump, we continue flushing until the next timestamp
        arrives. This assumes that the packets will arrive grouped by timestamps number.

        The first packet with a new timestamp following a timestamp jump is left on top of the buffer.
        """

        for i, bi in enumerate(bix):
            ts = self.buf_ts[bi]
            if ts - self.last_ts >= jump:
                print('finished flushing')
                return bix[i:]
            self.last_ts = ts
            self.flushed_packets += 1
            print('FLushing packet until jump of %i. ts=%i' % (jump, ts))
        return bix[[]]


    def align(self, bix):
        """ Flush packets until we receive the packet that is part of the first frame of the specified integration period.

        This first packet is left in the buffer.
        """
        print('align: Waiting for first frame of the specified integration period')
        for i, bi in enumerate(bix):
            ts = self.buf_ts[bi]
            if (ts % self.software_integration_period) == 0: # if the frame is on an integration period
                print('   Found first frame of period at timestamp %i (integration index %i/%i)' % (ts, ts % self.software_integration_period, self.software_integration_period))
                return bix[i:]
            if ts != self.last_ts:
                print('   Discarding correlator timestamp %i (integration index %i/%i)' % (ts, ts % self.software_integration_period, self.software_integration_period))
            self.last_ts = ts
        return bix[[]]


    def integ(self, bix):
        """

        Parameters:

            number_of_results (int): Number of software-integrated frames to
                acquire and return. If a `filename` is specified, only the
                last frame is returned. Also only if `filename` is specified,
                a `number_of_results`=Non ewill result in indefinite data
                capture until the capture is stopped.

            software_integration_period (int): Number of correlator frames to
                accumulate in software. A software frame will always be
                aligned to a multiple of software_integration_period.




        The receiver can do software integration for unlimited time at a firmware integration period of 5000 frames (12.8 ms).


        """

        while bix.size:
            is_current = (self.buf_ts[bix] // self.software_integration_period) == self.current_integ
            ix = bix[is_current]
            bix = bix[is_current==False]
            if ix.size:
                self.packets += ix.size
                self.accumulate_data(ix)
            else:  # New integration period!
                if self.current_integ is not None:
                    self.save_data()
                    self.clear_data()
                    self.packets = 0
                self.current_integ = self.buf_ts[bix[0]] // self.software_integration_period


        # packets = 0
        # print('Accumulating software frame #%i, starting with correlator frame number %i (%i/%i)' % (current_integ, self.last_ts, self.last_ts % self.software_integration_period, self.software_integration_period))


    def save_data(self):
        packets = self.packets

        # Write data to HDF5 file
        if self.hdf5_file:

            print('Received %i packets' % (packets))
            print('Got %.1f%% of the packets, and between %.1f%% and %.1f%% of the correlator frames' % (
                    packets / (self.NCORR * self.NCMAC * self.software_integration_period) * 100,
                    np.min(self.count) / self.software_integration_period * 100,
                    np.max(self.count) / self.software_integration_period * 100))

            self.sat_cplx.real = self.sat[..., 0] / 32.0 # real sat flag is masked with 0x20
            self.sat_cplx.imag = self.sat[..., 1] / 16.0 # imag sat flag is masked with 0x10
            self.data.real = self.acc_re
            self.data.imag = self.acc_im
            print( self.current_integ, self.software_integration_period, self.firmware_integration_period)
            fpga_frame_number = self.current_integ * self.software_integration_period * self.firmware_integration_period
            irigb_time = (fpga_frame_number * 2560 + self.frame0_irigb_time)

            print('AutoCorr data for (0,0) is:', self.acc_re[:,0,0])
            self.hdf5_file.write(
                self.current_integ,
                fpga_frame_number,
                irigb_time,
                self.data,
                self.count,
                self.sat_cplx)
        else:
            print('Got an unused integrated correlator frame with %i packets' % packets)

        # Convert the products in the matrix format
        # m = self.raw_to_vector_map
        # vector = self.data[m[0], m[1], m[2]]
        # conjugate the lower triangle
        # (i, j) = np.triu_indices(self.NCHAN)
        # matrix[..., j, i] = matrix[..., i, j].conjugate()

        # count = self.count[m[0], m[1]]
        # sat_cplx = self.sat_cplx[m[0], m[1], m[2]]


    def clear_data(self):
        self.acc_re[:] = 0
        self.acc_im[:] = 0
        self.count[:] = 0
        self.sat[:] = 0
        print('Cleared accumulated data!')

    def accumulate_data(self, bix, verbose=0):
        """ Add the data from the packets 0 to `number_of_packets` in to the
        software accumulator array for software integration number
        `integ_number`.
        """
        t1 = time.time()

        n = bix.size

        if not n:
            return
        # v = a.view(t)[:n, 0]
        # hh = h[:n]

        corr = self.buf_corr[bix]
        cmac = self.buf_cmac[bix]
        temp32 = self.temp32[:n]
        # print 'corr=', corr
        # print 'cmac=', cmac

        # ts = self.buf_ts[:n]

        # Extract the real part (in bits 27:10 of the data_h). We shift
        # left the MSB to bit 31 and sift the lsb back down to 0 to sign
        # extend the 18-bit value result within the 32-bit word.
        np.copyto(temp32, self.buf_data_h[bix])
        np.left_shift(temp32, 4, temp32)
        np.right_shift(temp32, 14, temp32)
        # Add the sign-extended value to the 64-bit accumulator.
        self.acc_re[corr, cmac] += temp32

        # Extract the real part (in bits 17:0 of the data_l).
        np.copyto(temp32, self.buf_data_l[bix])
        np.left_shift(temp32, 14, temp32)
        np.right_shift(temp32, 14, temp32)
        # Add the sign-extended value to the 64-bit accumulator.
        self.acc_im[corr, cmac] += temp32

        t2 = time.time()

        # Keep track of how many packets were received for each correlator/cmac
        self.count[corr, cmac] += 1
        # self.ts[integ_number, corr, cmac]
        # Accumulate the flags for each product by or'ing them together
        # We'll mask those later to save time
        self.sat[corr, cmac, :, 0] += self.buf_data_sat[bix] & 0x20
        self.sat[corr, cmac, :, 1] += self.buf_data_sat[bix] & 0x10

        t3 = time.time()

        if verbose:
            # print 'count=', count[0,0]
            dt1 = t2 - t1
            dt2 = t3 - t2
            dt = t3 - t1
            timestamps = set(self.buf_ts[bix])
            print('Processing & accumulating %i packets from correlator frames %s;  took %.3f ms (%.3f ms/corr frame) (%.3f + %.3f ms)' % (
                n,
                ','.join('%i (%i/%i)' % (ts, ts % self.software_integration_period, self.software_integration_period) for ts in timestamps),
                dt * 1000,
                dt / (self.NCORR * self.NCMAC) * 1000,
                dt1 * 1000,
                dt2 * 1000))


    async def get_metrics_async(self, metrics):

        metrics.add('raw_acq_processed_corr_packets', value=self.corr_processed_packets)
        if self.hdf5_file:
            metrics.add('raw_acq_corr_hdf5_file_number', value=self.hdf5_file.file_number)
            metrics.add('raw_acq_corr_hdf5_current_sample_in_file', value=self.hdf5_file.n)
            metrics.add('raw_acq_corr_hdf5_current_total_samples', value=self.hdf5_file.n_total)

        return



    def start_corr_hdf5(self,
                        base_dir=None,
                        base_filename=None,
                        capture_duration=60,
                        capture_n_inputs=4,
                        elements_per_file=256,
                        software_integration_period=100,
                        firmware_integration_period=100,
                        frame0_irigb_time=0
                        ):
        if self.hdf5_file:
            self.stop_corr_adc_hdf5()
            # raise RuntimeError('HDF5 dataWriter is already running')

        self.log.info('%r: Starting correlator HDF5 data writer with base_dir=%s, base_filename=%s, capture_duration=%r (type=%s), n_inputs=%d, elements_per_file=%d, soft_integ=%s, firm_integ=%s, irigb_time=%s' %
            (self, base_dir, base_filename, capture_duration, type(capture_duration),
                capture_n_inputs, elements_per_file,
                software_integration_period,firmware_integration_period, frame0_irigb_time ))

        self.software_integration_period = software_integration_period
        self.firmware_integration_period = firmware_integration_period
        # Update time of frame 0 from last sync.
        self.frame0_irigb_time = frame0_irigb_time
        self.state = 'align' # restart correlation product receiver

        base_dir = base_dir or '%(run_folder)s/corr'
        base_filename = base_filename or '%(elapsed_seconds)08d_%(file_number)04d.h5'

        self.n_inputs = capture_n_inputs
        self.elements_per_file = elements_per_file

        self.hdf5_start_time = time.time() # used to keep track of how long the disk capture has been running

        # self.hdf5_refresh_time = capture_refresh_time

        # Schedule for the acquisition to stop if capture_ducation is non-zero
        if capture_duration:
            self.log.info('%r: HDF5 correlator data writer will be stopped in %f seconds' % (self, capture_duration))
            asyncio.get_event_loop().call_later(capture_duration, self.stop_corr_hdf5)


        # Create the target folder
        self.hdf5_start_time = time.time()
        hdf5_start_isotime = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(self.hdf5_start_time))
        extra_fields = dict(
            hdf5_start_time=hdf5_start_isotime)

        self.hdf5_base_dir = self.recv.expand_path(base_dir, extra_fields)

        try:
            os.makedirs(self.hdf5_base_dir)
        except:
            self.log.warning("%r: couldn't make directory '%s'. Using current directory." % (self, self.hdf5_base_dir))
            self.hdf5_base_dir = './'

        try:
            self.hdf5_file = HDF5CorrWriter(base_dir=self.hdf5_base_dir,
                                            filename=base_filename,
                                            elements_per_file=self.elements_per_file,
                                            n_inputs=self.n_inputs)
            self.log.info('%r: Correlator HDF5 data writer is started' % self)
        except Exception as e:
            self.log.error('%r: Could not open correlaor file %s/%s. Error is\n%r' % (self, self.hdf5_base_dir, base_filename, e))

    def stop_corr_hdf5(self):
        if not self.hdf5_file:
            raise RuntimeError('%r: Correlator HDF5 dataWriter is not running. Cannot stop it.' % self)
        self.log.info('%r: Stopping Correlator HDF5 data writer' % self)
        hdf5_file = self.hdf5_file
        self.hdf5_file = None # Stop the thread from using the file before we close it
        hdf5_file.close()
        self.hdf5_start_time = None







#######################################################
#######################################################
#######################################################
# Correlator HDF5 data writer
#######################################################
#######################################################
#######################################################

class HDF5CorrWriter(object):
    """ Object representing a HDF5 file containing N-squared correlation data
    """
    def __init__(self,
                 base_dir='.',
                 filename='%(file_number)04d',
                 n_inputs=16,
                 elements_per_file=256,
                 n_freq_bins=1024,
                 sample_freq=800.,
                 include_counts=True,
                 include_sat=True): #, crate_and_slot_from_port = False):

        self.log = log.get_logger(self)


        self.filename = filename
        self.base_dir = base_dir
        self.n_inputs = n_inputs
        self.elements_per_file = elements_per_file
        self.n_freq_bins = n_freq_bins
        self.sample_freq = sample_freq
        self.include_counts = include_counts
        self.include_sat = include_sat


        self.prod_dtype = np.dtype([('input_a', np.uint16), ('input_b', np.uint16)])
        # self.prod_axis = np.array([(i, j) for i, j in itertools.product(range(inputs_per_file), repeat=2) if i >= j],
        #                           dtype=self.prod_dtype)

        # Compute the index arrays that will build the product vector form the raw correlator data for the desired inputs
        (i, j) = np.triu_indices(self.n_inputs)
        self.prod_axis = np.array(list(zip(i,j)), dtype=self.prod_dtype)
        self.raw_to_vector_map = CORR.get_raw_to_matrix_map()[..., i, j]
        self.n_prod = len(i)

        self.start_time = time.time()

        df = self.sample_freq / (2 * self.n_freq_bins)
        self.freq_dtype = np.dtype([('centre', np.float64), ('width', np.float64)])
        self.freq_axis = np.array([(k, df) for k in (self.sample_freq - df * np.arange(self.n_freq_bins))],
                                  dtype=self.freq_dtype)
        self.f = None
        self.file_number = 0 # current file number
        self.n_total = 0 # total number of elements written so far in all files
        self.start_new_hdf5_file()

    def start_new_hdf5_file(self):
        self.close()
        fields=dict(
            file_number=self.file_number,
            elapsed_seconds=time.time() - self.start_time)

        filename = self.filename % fields
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

        #self.log.info('%r: Opening raw data HDF5 file %s' % (self, self.filename))
        print('%r: Opening HDF5 file %s' % (self, self.filename))
        self.f = h5py.File(self.current_filename, 'w', libver='earliest')
        self.f.attrs["instrument_name"] = "D3A"
        self.f.attrs["acquisition_name"] = "corr"
        self.f.attrs["file_name"] = self.base_dir + '/' + self.filename
        self.f.attrs["data_type"] = "correlation data"
        # We use complex128, which can store 53-bit integers exactly.
        # Complex64 offers 23-bit integers, which does not leave a lot of room
        # for integration of the incoming 18-bit data.
        self.vis = self.f.create_dataset('vis',(1, 1024, self.n_prod), dtype=np.complex128, maxshape=(None, 1024, self.n_prod))
        self.vis.attrs['axis'] = ['time', 'freq', 'prod']
        if self.include_counts:
            self.counts = self.f.create_dataset('counts',(1, 1024, self.n_prod), dtype=np.uint32, maxshape=(None, 1024, self.n_prod))
            self.counts.attrs['axis'] = ['time', 'freq', 'prod']
        if self.include_sat:
            self.sat = self.f.create_dataset('sat',(1, 1024, self.n_prod), dtype=np.complex64, maxshape=(None, 1024, self.n_prod))
            self.sat.attrs['axis'] = ['time', 'freq', 'prod']


        self.index_map = self.f.create_group("index_map")

        self.time_dtype = np.dtype([
            ('integ_number', np.uint32),
            ('fpga_count', np.uint64),
            ('irigb_time', np.uint64),
            ('ctime', np.float64)])
        self.time = self.index_map.create_dataset('time', (1, ), dtype=self.time_dtype, maxshape=(None,))
        self.prod = self.index_map.create_dataset('prod', data=self.prod_axis, dtype=self.prod_dtype)
        self.freq = self.index_map.create_dataset('freq', data=self.freq_axis, dtype=self.freq_dtype)
        self.n = 0

    def write(self, integ_number, fpga_frame_number, irigb_time, raw_data, counts, saturations):

        n1 = self.n
        self.n += 1
        self.n_total += 1

        self.time.resize((self.n, ))
        self.vis.resize((self.n, 1024, self.n_prod))
        if self.include_counts:
            self.counts.resize((self.n, 1024, self.n_prod))
        if self.include_sat:
            self.sat.resize((self.n, 1024, self.n_prod))

        # count = self.count[m[0], m[1]]
        # sat_cplx = self.sat_cplx[m[0], m[1], m[2]]
        m = self.raw_to_vector_map

        current_time = time.time()
        print('shapes are: raw_data %r, counts %r, sat %r' % (raw_data[m[0], m[1], m[2]].dtype, counts[m[0], m[1]].dtype,saturations[m[0], m[1], m[2]].dtype))
        self.time[n1] = (integ_number, fpga_frame_number, irigb_time, current_time)
        self.vis[n1] = raw_data[m[0], m[1], m[2]]
        if self.include_counts:
            self.counts[n1] = counts[m[0], m[1]]
        if self.include_sat:
            self.sat[n1] = saturations[m[0], m[1], m[2]]

        if self.n >= self.elements_per_file:
            self.start_new_hdf5_file()

    def close(self):
        if self.f:
            # self.snapshot_index_map.resize((self.n,))
            # self.snapshot_index_map[:] = np.arange(self.n) + self.n_total
            self.log.info('%r: Closing HDF5 file %s' % (self, self.current_filename))
            self.f.close()
            try:
                os.remove(self.lock_filename)
                # os.rename(self.lock_filename, self.filename)
            except OSError:
                self.log.error('%r: Unable to rename HDF5 lock file from %s to %s' % (self, self.lock_filename, self.current_filename))


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
        # self.add_periodic_callback(self.receiver.print_stats, 3000)
        self.add_periodic_callback(self.receiver.ping_sources_async, 20000) # ping the raw_acq data sources periodically to ensure the switches tables always know how to route the packets to here
        self.add_periodic_callback(self.receiver.check_ioloop_response_time_async, 3000)
        self.startup_time = datetime.datetime.utcnow()



    async def shutdown(self):
        self.receiver.stop()


    @endpoint('start')
    async def start(self, **config):
        self.log.info('%r: Received start command with %r' % (self, config))
        if self.receiver.is_running():
            self.log.info('%r: Receiver is already running. Stopping it and restarting a new one' % (self))
            self.receiver.stop()
            # raise RuntimeError('Server is already started')

        # Register config with comet broker
        comet_config = config.pop('comet_broker', {})
        enable_comet = comet_config.get('enabled', None)
        print(f'comet_broker={comet_config}, enabled={enable_comet}')
        if enable_comet is None: # if the comet_broker.enable parameter is not specified
            msg = "Missing config value 'comet_broker.enabled'."
            self.log.error(msg)
            raise RuntimeError('Cannot start comet broker: %s' % (msg))
        if enable_comet: # if comet parameters are present and comet is is enabled
            if comet is None:
                msg = "Failure importing comet for configuration tracking.  Please install the " \
                      "comet package or set 'comet_broker/enabled' to False in config."
                self.log.error(msg)
                return msg
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
                comet_manager.register_start(self.startup_time, __version__)
                comet_manager.register_config(config.copy())
            except comet.CometError as exc:
                msg = "Comet failed registering raw_acq start and initial config. " \
                      "The Comet client returned the following error: {}".format(exc)
                self.log.error(msg)
                raise RuntimeError('Cannot start comet broker: %s' % (msg))
        else:
            self.log.warning("Config registration DISABLED. This is only OK for testing.")
        # config.pop('hostname', None)
        # config.pop('port', None)
        result = await self.receiver.start_async(**config)
        self.log.info('%r: UDP receiver started. Returned %r' % (self, result))
        return result


    @endpoint('stop')
    async def stop(self):
        if not self.receiver.is_running():
            self.log.warning('%r: Server is not running' % self)
        self.receiver.stop()
        return "stopped receiver"


    @endpoint('start-raw-hdf5')
    async def start_raw_hdf5(self, base_dir='./', base_filename='RawAcq', capture_duration=0, capture_refresh_time=0, elements_per_file=2048*64):
        self.receiver.raw_packet_processor.start_adc_hdf5(
            base_dir=base_dir,
            base_filename=base_filename,
            capture_duration=capture_duration,
            capture_refresh_time=capture_refresh_time,
            elements_per_file=elements_per_file)
        return "started hdf5 writing to disk."

    @endpoint('stop-raw-hdf5')
    async def stop_raw_hdf5(self):
        self.receiver.raw_packet_processor.stop_adc_hdf5()
        return "stopped hdf5 writing to disk."

    @endpoint('status')
    async def status(self):
        self.log.info('%r: getting status request' % self)
        return dict(started=self.receiver.is_running() if self.receiver else False)

    @endpoint('get-packets')
    async def get_packets(self):
        self.log.info('%r: received get_packets command' % self)
        ts, ports, data = await self.receiver.raw_packet_processor.get_data()
        return dict(ts=ts.tolist(), ports=ports, data=data.tolist())

    @endpoint('start-fft-rms')
    async def start_fft_rms(self, stream_ids=[], target_gain_bank=0, number_of_frames=100):
        self.log.info('%r: received start_fft_rms command' % self)
        await self.receiver.raw_packet_processor.start_fft_rms_async(stream_ids=stream_ids, target_gain_bank=target_gain_bank, number_of_frames=number_of_frames)
        return

    @endpoint('get-fft-rms')
    async def get_fft_rms(self):
        self.log.info('%r: received get_fft_rms command' % self)
        ix, rms = await self.receiver.raw_packet_processor.get_fft_rms_async()
        return (ix.tolist(), rms.tolist())

    @endpoint('start-corr-hdf5')
    async def start_corr_hdf5(self,
                        base_dir=None,
                        base_filename=None,
                        capture_duration=0,
                        capture_n_inputs=4,
                        elements_per_file=256,
                        software_integration_period=100,
                        firmware_integration_period=1,
                        frame0_irigb_time=0,
                        ):
        self.receiver.corr_packet_processor.start_corr_hdf5(
            base_dir=base_dir,
            base_filename=base_filename,
            capture_duration=capture_duration,
            capture_n_inputs=capture_n_inputs,
            elements_per_file=elements_per_file,
            firmware_integration_period=firmware_integration_period,
            software_integration_period=software_integration_period,
            frame0_irigb_time=frame0_irigb_time
            )
        return "started correlator hdf5 writing to disk with parameters %r." % firmware_integration_period


    @endpoint('stop-corr-hdf5')
    async def stop_corr_hdf5(self):
        self.receiver.corr_packet_processor.stop_adc_hdf5()
        return "stopped corrlelator hdf5 writing to disk."

    @endpoint('get-rms')
    async def get_rms(self):
        if self.receiver and self.receiver.is_running() and self.receiver.raw_packet_processor is not None:
            rms = await self.receiver.raw_packet_processor.get_adc_rms_async()
        else:
            rms = []  # if the receiver is not ready, return an empty list. This won't cause the caller to crash.
            # return raw packet processor not yet running")
        return dict(rms=rms)

    @endpoint('get-monitoring-data')
    async def get_monitoring_data(self):
        t0 = time.time()
        self.log.info('%r: Received monitoring metrics request' % self)
        if self.receiver:
            metrics = await self.receiver.get_metrics_async()
        t1 = time.time()
        # handler.set_header('Content-Type', 'text/plain')
        # handler.set_header('Content-Encoding', 'gzip')
        # handler.write(metrics.get_gzip())

        # Return gzip-compressed response
        response = aiohttp.web.Response(
            body=metrics.get_gzip(),
            headers={'Content-Encoding': 'gzip'})
        t2 = time.time()
        self.log.info('%r: Returning raw_acq %i metrics. The request took %.3f seconds (%.3fs to format metrics, %.3fs to encode them)' % (self, len(metrics), t2 - t0, t1 - t0, t2 - t1))
        return response

################################################
# RawAcq REST Client
################################################

class RawAcqAsyncRESTClient(AsyncRESTClient):
    """
    Implements an asynchronous client that exposes the functions of the specified remote RawAcq server.

    This client is used by ch_master to start, configue and operate all the RawAcq servers in the array.

    The client is implemented using a Tornado AsyncHTTPClient. It exposes the RawAcq server methods
    (i.e REST endpoints) as local methods. The local methods are Tornados so requests to
    multiple clients can be made in parallel. This is especially beneficial since the data requests
    from the server are slow IO operations which benefit the mist from casync o-execution.

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
                 base_dir='~/data',
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



    async def ping(self):
        try:
            await self.get('status')
            self.log.info("Successfully pinged raw_acq server at %s:%i" % (self.hostname, self.port))
        except Exception as e:
            self.log.debug(repr(e))
            self.log.error("Can't ping raw_acq server at %s:%i" % (self.hostname, self.port))
            return False
        return True # return raises an exception: we don't want it in the try block


    async def status(self):
        result = await self.get('status')
        return result


    async def start(self, **config):
        """ Start the RaqAcq remote server with the keyword argument as configuration data"""
        self.log.info('%s: Starting remote RawAcq server at %s:%i with config: %r' % (self, self.hostname, self.port, config))
        result = await self.post('start', **config)
        return result


    async def stop(self):
        result = await self.get('stop')
        return result


    async def get_packets(self):
        data = await self.get('get-packets')
        return data


    async def start_fft_rms(self, stream_ids=[], target_gain_bank=0, number_of_frames=100):
        await self.post('start-fft-rms', stream_ids=stream_ids, target_gain_bank=target_gain_bank, number_of_frames=number_of_frames)


    async def get_fft_rms(self):
        ix, rms = await self.get('get-fft-rms')
        return (ix, rms)


    async def start_raw_hdf5(self, base_dir=None, base_filename=None, capture_duration=0, capture_refresh_time=0, elements_per_file=2048*64):
        result = await self.post('start-raw-hdf5',
            base_dir=base_dir or self.hdf5_base_dir,
            base_filename=base_filename or self.base_filename,
            capture_duration=capture_duration,
            capture_refresh_time=capture_refresh_time,
            elements_per_file=elements_per_file)
        return result


    async def stop_raw_hdf5(self):
        result = await self.get('stop-raw-hdf5')
        return result


    async def start_corr_hdf5(self,
        base_dir=None, base_filename=None,
        capture_duration=0,
        elements_per_file=2048*64,
        capture_n_inputs=4,
        software_integration_period=1,
        firmware_integration_period=1,
        frame0_irigb_time=0
        ):

        result = await self.post('start-corr-hdf5',
            base_dir=base_dir,
            base_filename=base_filename,
            capture_duration=capture_duration,
            elements_per_file=elements_per_file,
            capture_n_inputs=capture_n_inputs,
            software_integration_period=software_integration_period,
            firmware_integration_period=firmware_integration_period,
            frame0_irigb_time=frame0_irigb_time)
        return result


    async def stop_corr_hdf5(self):
        result = await self.get('stop-corr-hdf5')
        return result


    async def estimate_gains(self):
        return (await self.post('estimate_gains'))   # estimate-gains?

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
    log.setup_basic_logging('DEBUG')

    client, server = run_client(
        sys.argv[1:],
        RawAcqAsyncRESTServer,
        RawAcqAsyncRESTClient,
        object_name ='RawAcq',
        server_config_path='raw_acq.servers')
    return client, server

if __name__ == '__main__':
    client, server = main()


