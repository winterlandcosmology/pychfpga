"""UDP raw acquisition receiver and packet processing loop."""

import asyncio
import datetime
import os
import select
import socket
import sys
import threading
import time

import netifaces
import numpy as np
import psutil

from wtl import log
from wtl.metrics import Metrics

from pychfpga.common.state import State

from .corr_data_processor import CorrDataProcessor
from .raw_data_processor import RawDataProcessor

FIRMWARE_CONFIG_KEYS = ("raw", "corr")
CAPTURE_CONFIG_KEYS = FIRMWARE_CONFIG_KEYS  # legacy alias


def _firmware_config_sections(config):
    """Split a firmware State or partial processor patch dict into per-receiver sections."""
    if config is None:
        return {}
    if isinstance(config, State):
        payload = config.as_dict()
    elif isinstance(config, dict):
        payload = config
    else:
        raise TypeError(f"Expected firmware State or dict, got {type(config).__name__!r}")
    if set(payload.keys()) >= set(FIRMWARE_CONFIG_KEYS):
        return {key: payload[key] for key in FIRMWARE_CONFIG_KEYS}
    return payload

# TCP port used to reach CRS/ICE boards for switch MAC learning (echo service).
DEFAULT_PING_PORT = 7

# Persist UDP sockets across receiver restarts within one Python process.
_opened_udp_sockets = {}


class RawAcqReceiver(object):
    """ Implement an array of multi-threaded UDP Raw data receiver and the data processor that will handle the received data.

    The object offers `start` and `stop` methods to start and stop the receivers, a method to grab a
    snapshot of the current data, and a method to start a thred that continuously writes the data to
    disk if HDF5 format.

    This reciever uses Threads to implement concurrency, not Tornado.

    Packet buffer contract (aligned with :class:`~pychfpga.common.udp_packet_receiver.UDPPacketReceiver`
    so :class:`~pychfpga.common.raw_data_packet_processor.RawDataPacketProcessor` can use either as ``packet_receiver``):

    ``buf``, ``pkt_len``, ``n_packets``, ``n_stored``, ``max_packet_size``, and :meth:`flush`.

    Notes:
        The performance ofthis receiver is limited by Python. It is meant to be used mostly for debugging.

        Should probably fix the 'serve forever bits'
    """

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

        # Define a lock to control access to data while the receiver thread is populating it
        self.lock = threading.RLock()
        self.is_locked = False  # debug
        self.processors = {}

    def __repr__(self):
        return f'{self.__class__.__name__}({self.name})'

    def update_config(self, config=None, **kwargs):
        """Apply firmware or per-processor configuration patches without merging subtrees."""
        if kwargs:
            config = {**(config or {}), **kwargs}
        if not config:
            return
        sections = _firmware_config_sections(config)

        raw_proc = self.processors.get("raw")
        if raw_proc is not None:
            if "raw" in sections:
                raw_proc.update_config(
                    config=sections["raw"],
                    adc_config=sections["raw"],
                    fft_config=sections["raw"],
                )
            elif any(k in sections for k in ("adc", "fft")):
                raw_proc.update_config(
                    config=None,
                    adc_config=sections.get("adc"),
                    fft_config=sections.get("fft"),
                )
            else:
                raw_proc.update_config(config=sections)

        corr_proc = self.processors.get("corr")
        if corr_proc is not None:
            if "corr" in sections:
                corr_proc.update_config(sections["corr"])
            elif (
                "firmware_integ_period" in sections  # *** TODO: looks like an exception to he habdles some other way
                or "soft_integ_period" in sections
            ):
                corr_proc.update_config(sections)

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
            metrics_refresh_time=1,
            adc_rms_refresh_count=60,
            jump_thresholds=[],
            config=None,
            raw_capture_state=None,
            ping_port=DEFAULT_PING_PORT,
            no_ping=False,
            hostname=None,  # not used, but may be passed by start_client()
            post=None  # not used, but may be passed by start_client()
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
        self.no_ping = bool(no_ping)
        self.listen_to_all_interfaces = True
        self.ping_port = int(ping_port) if ping_port is not None else DEFAULT_PING_PORT
        self.ports = ports
        self.jump_thresholds = jump_thresholds
        self.corr_name = corr_name
        self.run_name = run_name
        self.data_folder = data_folder
        self.run_folder = run_folder
        firmware_config = config if config is not None else raw_capture_state
        if firmware_config is None:
            firmware_config = {}
        self.firmware_config = (
            firmware_config.deepcopy()
            if isinstance(firmware_config, State)
            else State(firmware_config).deepcopy()
        )

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

        # We create a pre-allocated receiver buffer `buf` of BUF_SIZE packets
        # of size PACKET_SIZE. PACKET_SIZE is big enough to contain a raw data
        # packet or a correlator packet.
        # Big enough for UCAP subframes and UCORR jumbo packets on one shared buffer.
        self.MAX_RAW_PACKET_SIZE = 9000
        self.BUF_SIZE = 2048  # Number of packets that can be held in the buffer (alias of n_packets).
        self.n_packets = self.BUF_SIZE

        # if not self.BUF_SIZE:
        #     self.log.warning('%r: Buffer size is zero! The list of expected STREAM IDs must have been empty!' % self)
        # Define the BUF_SIZE x MAX_RAW_PACKET_SIZE buffer
        self.buf = np.empty((self.n_packets, self.MAX_RAW_PACKET_SIZE), dtype=np.uint8)
        # Per-slot byte counts (UDPPacketReceiver names this pkt_len)
        self.pkt_len = np.empty(self.n_packets, dtype=np.uint16)
        self.buf_packet_length = self.pkt_len  # legacy alias for packet processors
        self.source_socket = np.empty(self.n_packets, dtype=np.uint16)
        self.n_stored = 0  # number of packets currently stored in the buffer
        self.max_packet_size = int(self.MAX_RAW_PACKET_SIZE)


        sections = _firmware_config_sections(self.firmware_config)
        if set(sections.keys()) != set(FIRMWARE_CONFIG_KEYS):
            raise ValueError(
                f'{self!r}: firmware config must include {FIRMWARE_CONFIG_KEYS!r}'
            )

        raw_proc = RawDataProcessor(
            self,
            config=sections["raw"],
            adc_config=sections["raw"],
            fft_config=sections["raw"],
            stream_ids=stream_ids,
            metrics_refresh_time=metrics_refresh_time,
            adc_rms_refresh_count=adc_rms_refresh_count,
        )
        corr_proc = CorrDataProcessor(self, config=sections["corr"])

        self.processors = {
            "raw": raw_proc,
            "corr": corr_proc,
        }

        # Determine the interface from which data will be coming from each source by pinging them
        # returns a dictionary that maps each source to an interface IP and target port
        #   { (src_ip, src_port) : (if_ip, port) }
        if self.no_ping:
            self.log.info(f'{self!r}: no_ping=True; skipping TCP ping (listen on all interfaces)')
            src_if_addrs = {}
            for port_info in self.ports:
                for src in port_info["sources"]:
                    source_key, _ping_addr = self._parse_source(src, self.ping_port)
                    src_if_addrs[source_key] = ('0.0.0.0', 0)
        else:
            src_if_addrs = await self.ping_sources_async()
        print('IF addr=', src_if_addrs)
        failed_src = [src_addr for src_addr, src_if_addr in src_if_addrs.items() if not src_if_addr]
        if failed_src:
            failed_addrs = ','.join(f'{a}:{p}' for a, p in failed_src)
            if self.listen_to_all_interfaces:
                self.log.warning(
                    f'Cannot ping {failed_addrs} (TCP); listening on all interfaces anyway.')
                for src_addr in failed_src:
                    src_if_addrs[src_addr] = ('0.0.0.0', 0)
            else:
                raise RuntimeError(
                    f'Cannot ping {failed_addrs}, so cannot determine interface '
                    f'through which these data sources are reached.')

        # Determine the interface and port to which each receiver should listen to.
        #
        # If we want the UDP receiver to listen from all interfaces, we set the receiver address to
        # '0.0.0.0'.  Note that 'localhost' and 'some_ip' are separate interfaces: if
        # you specify one, you can't receive data from the other.
        #
        # If we want the UDP interface to listen to specific interface, we look all the interfaces
        # from the sources associated with a port must use the same interface.
        #
        # Expand the port info to identify the interface and sources
        # associated with each individual socket that we will create
        #
        # target port number: a specific port number, a port name (assigned
        # one random port to all sources), or 0 (assign a random port to each
        # source)

        # interfaces accessed by each port. Should be only one for named and non-zero ports.
        socket_if_ip = {}
        socket_sources = {}  # list of sources associated with each port
        for port_info in self.ports:
            # Create a list that associate a port to each source. If port==0,
            # a different port name is given to each source, otherwise all
            # ports have the specified port (number or name)
            ports = [(port_info['port'] if port_info['port'] else (f'_random_port_{i}'))
                     for i, _ in enumerate(port_info['sources'])]
            for port, src in zip(ports, port_info['sources']):
                socket_sources.setdefault(port, []).append(src)
                # get the set of IPs for this port, or create one if there is none yet
                if_ip = '0.0.0.0' if self.listen_to_all_interfaces else src_if_addrs[tuple(src)][0]
                # Check if we have multiple interfaces associated with specified or named ports
                if port in socket_if_ip and socket_if_ip[port] != if_ip:
                    raise RuntimeError(
                        f'Data sources for port {port} are accessed via '
                        f'different interfaces {socket_if_ip[port]} and {if_ip}.')
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
            self.log.info(f"{self!r}: Creating socket for port ID '{port_name}' on ({if_ip}:{port})")
            sock = self.get_udp_socket((if_ip, port))
            self.sockets.append(sock)
            # Store actual port IP/port allocated by the system
            actual_socket_if_ip[port_name], actual_socket_port[port_name] = sock.getsockname()
            self.port_number.append(actual_socket_port[port_name])

            # Check if the port and IP that were given are what we expect. This should never happen.
            if ((actual_socket_if_ip[port_name] != socket_if_ip[port_name])
                    or (port and port != actual_socket_port[port_name])):
                self.log.warn(
                    f'The socket for port ID {port_name} was not created at the expected '
                    f'address: got {actual_socket_if_ip[port_name]}:{actual_socket_port[port_name]} '
                    f'instead of {socket_if_ip[port_name]}:{port}')

            self.log.info(
                f"{self!r}: receiver {self.name}: UDP Socket created for "
                f"port '{port_name}' at {actual_socket_if_ip[port_name]}:{actual_socket_port[port_name]}")

        self.started = True
        if start_thread:
            self.start_processing_thread()
        # Build a mac address loopup table for all source interfaces
        if_ips = {if_addr[0] for if_addr in src_if_addrs.values()}  # set of unique interface IPs used by all sources
        mac = {if_ip: self._get_mac_address(if_ip) for if_ip in if_ips}  # map between ip and mac addresses
        self.log.info(f'{self!r}: Available Interfaces are {mac}')

        # Create the dict that provides the target ip address, port address and mac address for each source
        dest_ifs = []
        for port_name, sources in socket_sources.items():
            dest_port = actual_socket_port[port_name]
            for src in sources:
                dest_if_ip = src_if_addrs[tuple(src)][0]
                dest_mac = mac[dest_if_ip]
                dest = (dest_if_ip, dest_port, dest_mac)
                dest_ifs.append((tuple(src), dest))

        result = dict(
            status='started',
            target_addr=dest_ifs  # return as a list of tuples, json does not support tuple-indexed dicts
        )
        return result

    def get_udp_socket(self, addr):
        """
        Return a socket that is bound to the specified port/address.


        """
        # Make sure there is a list of opened sockets

        opened_sockets = _opened_udp_sockets

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
            self.log.warn(f'Could not establish a TCP connection with {addr[0]}:{addr[1]}. Error is:\n {e}')
            if_addr = None

        return if_addr

    @staticmethod
    def _parse_source(src, default_ping_port=DEFAULT_PING_PORT):
        """Return ``(source_key, ping_addr)`` for a port source entry.

        ``source_key`` is ``(host, port)`` used in ``target_addr`` and must match the
        FPGA/iceboard identity expected by ``fpga_master`` (often the board UDP source port).

        ``ping_addr`` is ``(host, tcp_port)`` for switch MAC learning. CRS boards use TCP
        echo on port 7; do not use the ephemeral UDP source port (e.g. 62510) for ping.

        Accepted forms: ``[host, key_port]``, ``[host, key_port, ping_port]``, or a dict
        with ``host``, ``port``/``key_port``, and optional ``ping_port``.
        """
        if isinstance(src, dict):
            host = src["host"]
            key_port = int(src.get("port", src.get("key_port", 0)))
            ping_port = int(src.get("ping_port", default_ping_port))
        else:
            src = list(src)
            if len(src) < 2:
                raise ValueError(f"source entry must be [host, key_port] or [host, key_port, ping_port]: {src!r}")
            host = src[0]
            key_port = int(src[1])
            ping_port = int(src[2]) if len(src) > 2 else int(default_ping_port)
        return (host, key_port), (host, ping_port)

    async def ping_sources_async(self):
        if not self.ports:
            return None
        if getattr(self, 'no_ping', False):
            return None
        self.log.info(f'{self!r}: Pinging all data sources (TCP; default ping port {self.ping_port})')
        source_keys = []
        ping_addrs = []
        for port_info in self.ports:
            for src in port_info["sources"]:
                source_key, ping_addr = self._parse_source(src, self.ping_port)
                source_keys.append(source_key)
                ping_addrs.append(ping_addr)
        ping_results = await asyncio.gather(*[self._ping_async(addr) for addr in ping_addrs])
        src_if_addrs = {}
        for source_key, ping_addr, src_if_addr in zip(source_keys, ping_addrs, ping_results):
            if not src_if_addr:
                old_count = self.ping_error_count.setdefault(source_key, 0)
                self.ping_error_count[source_key] = old_count + 1
                self.log.warning(
                    f'{self!r}: TCP ping to {ping_addr[0]}:{ping_addr[1]} failed '
                    f'(source key {source_key[0]}:{source_key[1]})')
            src_if_addrs[source_key] = src_if_addr
        return src_if_addrs

    def _get_mac_address(self, if_addr):
        """ Return the MAC address of the interface with address `if_addr`.

        Parameters:

            if_addr (str): address of the interface (not any target)

        Returns:

            a string describing the mac address of the interface in the format
            'xx:xx:xx:xx:xx:xx'. *None* if no match was found.
        """
        interfaces = netifaces.interfaces()
        mac_list = []
        for interface in interfaces:
            afs = netifaces.ifaddresses(interface)
            if netifaces.AF_INET not in afs or netifaces.AF_LINK not in afs:
                continue
            self.log.debug(f'checking interface {interface} with AF {afs}')
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
        thread = getattr(self, "data_processing_thread", None)
        if thread:
            thread.join()

        # Stop all packet processors
        for proc in self.processors.values():
            if proc:
                proc.stop()
        self.sockets = []
        self.start_time = None

    def start_processing_thread(self):
        self.data_processing_thread = threading.Thread(target=self.receive_packets, daemon=True)
        self.data_processing_thread.start()
        # def process_thread(self):
        #     while self.started:
        #         self.process_packets()

    def receive_packets(self, reset_stats=True, timeout=0.001, stop_condition=None, max_time_between_processing=1):
        """ This method is run in a thread to continuously receive packets in
        the buffer form all sockets and call process_packets when the buffer
        is sufficiently full or there is no more data immediately available.

        Parameters:

            reset_stats (bool): Unused. To be removed.

            timeout (float): Maximum time (in seconds) we will wait for packet. Sets how fast we figure out no more dta is coming.

            stop_condition (func or None): Function that returns True to stop the receive loop and the thread. If None, the function is not called.

            max_time_between_processing (float): Maximum time (in seconds) the system will
                wait until processing packets. This is to ensure that data will be
                processed even if the buffer is not full. This is useful when data
                is coming slowly, but we still want the processing (and resulting
                metrics refreshes) will happen  at regular intervals.

        """

        self.log.info(f"{self!r}: Starting packet processing")
        self.old_timestamp = None
        self.n_ant_rec = 0
        self.n_stored = 0

        if not self.BUF_SIZE:
            return
        print('Starting receiver')
        last_processing_time = time.time()
        while self.started and (stop_condition is None or not stop_condition()):
            try:
                # Check which sockets have data and read it into the buffer. Continue adding data to the buffer as long as some socket has something.
                # print(f'selecting sockets {self.sockets}')
                sockets_with_data, [], [] = select.select(self.sockets, [], [], timeout)
                # print(f' sockets with data:{sockets_with_data}')
                if sockets_with_data:
                    for sock in sockets_with_data:
                        t0 = time.time()
                        self.pkt_len[self.n_stored] = sock.recv_into(self.buf[self.n_stored])  # data is transferred directly into the preallocated buffer, which is fast!
                        self.source_socket[self.n_stored] = self.sockets.index(sock) # store indice of current socket
                        self.n_stored += 1
                        self.packet_max_readout_time = max(time.time() - t0, self.packet_max_readout_time)
                        # Process packets if the buffer is full or if we have
                        # been buffering packets for a sufficient period of
                        # time. This is useful when we have a long buffer but
                        # low packet rate.
                        if self.n_stored == self.BUF_SIZE or (t0 - last_processing_time) > max_time_between_processing:
                            self.process_packets()
                            self.n_stored = 0
                            last_processing_time = time.time()
                elif self.n_stored:
                    # There was not data after the timeout. Its probable the pause might be
                    # much longer since data comes in burst, so we don't want to wait for `max_time_between_processing`.
                    # Let's process whatever data we have so the user
                    # does not have to wait too long for it.
                    self.process_packets()
                    self.n_stored = 0
                    last_processing_time = time.time()
            except KeyboardInterrupt:
                break

    def process_packets(self):
        """
        Process the data in the buffer by identifying the packet type (ADC,
        FFT, etc) and calling the appropriate processing method.

        inputs:
            self.n_stored: number of packets in the buffer
            self.buf and its structured references: captured packets, in random order

        """
        # print('Processing %i packets' % self.n_stored)

        # Just return if there are no packets to process
        if not self.n_stored:
            return

        t0 = time.time()

        self.received_packets += self.n_stored

        try:
            raw = self.processors.get("raw")
            if raw:
                raw.process_udp_packets()
            corr = self.processors.get("corr")
            if corr:
                t_corr = time.time()
                corr.process_udp_packets()
                corr.after_process_udp_packets(time.time() - t_corr)
                corr.consume(corr.get_formatted_data())
        except Exception as e:
            self.log.error(f'process_packets: exception {e!r}')
            raise

        t3 = time.time()

        self.packet_max_processing_time = max(t3 - t0, self.packet_max_processing_time)
        self.packet_current_processing_time += t3 - t0

    def flush(self, timeout=0.001, max_flush_time=2, verbose=0, **kwargs):
        """Clear packet buffer bookkeeping (UDPPacketReceiver-compatible signature).

        Unlike :meth:`UDPPacketReceiver.flush`, there is no UDP socket to drain;
        this only resets the in-memory ring used with :class:`RawDataPacketProcessor`.
        """
        self.n_stored = 0
        self.pkt_len[:] = 0
        for key, proc in self.processors.items():
            if proc is not None:
                proc.flush()
        return 0

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

        disk_dir = self._disk_metrics_path()
        if hasattr(os, 'statvfs') and disk_dir:
            s = os.statvfs(disk_dir)
            metrics.add('raw_acq_disk_size', value=s.f_blocks * s.f_bsize)
            metrics.add('raw_acq_disk_used', value=(s.f_blocks - s.f_bfree) * s.f_bsize)
            metrics.add('raw_acq_disk_free', value=s.f_bfree * s.f_bsize)
            metrics.add('raw_acq_disk_percent_used', value=(s.f_blocks - s.f_bfree) / s.f_blocks)
            metrics.add('raw_acq_disk_percent_free', value=s.f_bfree / s.f_blocks)
            await asyncio.sleep(0)

        # IOloop health stats
        metrics.add('raw_acq_ioloop_max_response_time', value=self.ioloop_max_response_time)
        metrics.add('raw_acq_ioloop_min_response_time', value=self.ioloop_min_response_time)
        self.ioloop_max_response_time = None
        self.ioloop_min_response_time = None

        metrics.add('raw_acq_run_time', value=0 if self.start_time is None else time.time() - self.start_time)

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
                packet_avg_processing_time = (
                    (self.packet_current_processing_time * 1.0 * self.NCHAN / self.current_processed_packets)
                    if self.current_processed_packets else 0)
                self.packet_current_processing_time = 0
                self.current_processed_packets = 0
            metrics.add('raw_acq_packet_avg_processing_time', value=packet_avg_processing_time)

            # Get metrics from packet processors
            for proc in self.processors.values():
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
                        self.log.warning(
                            f'{self!r}: Bad value while reading system UDP statistics. '
                            f'Problematic line is {cols}')
                    time.sleep(0)  # relinquish some time to the thread? Not sure if it helps.
        except IOError:
            self.log.warning(f'{self!r}: Could not read system UDP statistics')

        return results

    def is_running(self):
        return bool(self.sockets)

    async def check_ioloop_response_time_async(self):
        t = time.time()
        if self.ioloop_last_time is not None:
            self.ioloop_max_response_time = max(self.ioloop_max_response_time or 0, t - self.ioloop_last_time)
            self.ioloop_min_response_time = min(
                self.ioloop_min_response_time or float('inf'),
                t - self.ioloop_last_time)
        self.ioloop_last_time = t

    def _disk_metrics_path(self):
        """Filesystem path for global raw_acq_disk_* metrics."""
        data_folder = getattr(self, 'data_folder', None)
        if data_folder:
            return data_folder
        raw = self.processors.get("raw")
        if raw is not None:
            base_dir = getattr(raw.adc, "hdf5_base_dir", None)
            if base_dir:
                return base_dir
        corr = self.processors.get("corr")
        if corr is not None:
            base_dir = getattr(corr, "hdf5_base_dir", None)
            if base_dir:
                return base_dir
        return None

    def expand_path(self, path, extra_fields={}):
        """ Expand fields in a string.
        """
        fields = {
            'raw_acq_start_time': time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(self.start_time)),
            'data_folder': getattr(self, 'data_folder', None) or '.',
            'run_name': getattr(self, 'run_name', None) or 'Unknown',
            'run_folder': getattr(self, 'run_folder', None) or '.',
            'corr_name': getattr(self, 'corr_name', None) or 'Unknown'
        }
        fields.update(extra_fields)
        return os.path.expanduser(path.format(**fields) % fields)

