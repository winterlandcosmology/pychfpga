#!/usr/bin/env python
""" REST Client to configure and operate kotekan nodes and dummy kotekan REST Servers"""

from __future__ import absolute_import, division, print_function

import logging
import argparse
import sys

import numpy as np

import tornado
import tornado.web
import tornado.httpclient

from pychfpga import Ccoll, NameSpace, load_yaml_config

from rest import AsyncRESTClient, AsyncRESTServer, coroutine, coroutine_return, endpoint, RunSyncWrapper, IOLoop

################################################
# Dummy kotekan REST Server
################################################

class KotekanAsyncRESTServer(AsyncRESTServer):
    """
    Asynchronous dummy kotekan REST server.

    """

    DEFAULT_PORT = 12048 # 54323

    def __init__(self, address='', port=DEFAULT_PORT, logging_params={}):
        super(KotekanAsyncRESTServer, self).__init__(address=address, port=port, heartbeat_string='Ks')

    @coroutine
    def shutdown(self):
        pass

    @coroutine
    @endpoint('start')
    def start(self, handler, **config):
        self.log.info('%.32r: Received start command with %r' % (self, config))
        coroutine_return('Started kotekan')

    @coroutine
    @endpoint('stop')
    def stop(self, handler):
        self.log.info('%.32r: Received stop command' % (self))
        coroutine_return("Stopped kotekan")

    @coroutine
    @endpoint('update')
    def update(self, handler, **config):
        self.log.info('%.32r: Received update command with %r' % (self, config))
        coroutine_return("Updated kotekan")

    @coroutine
    @endpoint('status')
    def status(self, handler):
        self.log.info('%.32r: Received status command' % (self))
        coroutine_return("Status")

    @coroutine
    @endpoint('packet_grab')
    def packet_grab(self, handler, number_of_packets=5):
        self.log.info('%.32r: Received status command' % (self))
        coroutine_return("Status")


################################################
# kotekan REST Client
################################################


class KotekanAsyncRESTClient(AsyncRESTClient):
    """
    Provides access to the remote GPU node kotekan processes through its REST interface.

    Uses Tornado AsyncHTTPClient. All methods are Tornado coroutines so that operations can be
    performed concurrently on multiple nodes.
    """
    DEFAULT_PORT = KotekanAsyncRESTServer.DEFAULT_PORT

    def __init__(self, hostname=None, port=DEFAULT_PORT, **config):
        super(KotekanAsyncRESTClient, self).__init__(
            hostname=hostname,
            port=port,
            # server_class=KotekanAsyncRESTServer,
            heartbeat_string='Kc')
        # self.name = name
        self.config = config
        #self.ping_cb = tornado.ioloop.PeriodicCallback(self.ping, 60e3)
        #self.ping_cb.start()

    @coroutine
    def ping(self):
        try:
            yield self.post('status')
            self.log.info("%.32r: Pinged kotekan at %s:%s" % (self, self.hostname, self.port))
        except Exception as e:
            self.log.debug(repr(e))
            self.log.warning("%.32r: Cannot ping kotekan at %s:%s" % (self, self.hostname, self.port))
            coroutine_return(False)
        coroutine_return(True)  # we dont want this in the try block, as by design it raises an exception

    @coroutine
    def status(self):
        result = yield self.post('status')
        coroutine_return(result)

    @coroutine
    def start(self, config):
        newconfig = self.config.copy()
        newconfig.update(config)
        result = yield self.post('start', **newconfig)
        coroutine_return(result)

    @coroutine
    def stop(self):
        result = yield self.post('stop')
        coroutine_return(result)

    @coroutine
    def update(self, config):
        result = yield self.post('update', **config)
        coroutine_return(result)

    # def send_command(self, command, args):
    #     """
    #     Sends a command to the kotekan REST server

    #     All endpoints return failure status codes if something goes wrong, along with a (sometimes
    #     helpful) error message in the "Error: <message>" field of the HTML header.  They don't
    #     return any json data on failure at the moment.

    #     """
    #     port = 12048  # hard coded
    #     # command = {"port": port, "num_packets": number_of_packets}
    #     resp = requests.post('http://%s:%i/%s' % (self.hostname, port, command), data=json.dumps(args))
    #     if resp.reason != 'OK' or resp.status_code != 200:
    #         raise RuntimeError('The kotekan returned the following error: %i:%s' % (resp.status_code, resp.reason))
    #     return resp.content


    @coroutine
    def packet_grab(self, port=0, number_of_packets=1):
        """
        Send it {"num_packets": [1,100]}, returns "Content-Type: application/octet-stream"

        Packet size is html content length divided by num_packets.  Note returns "not found" error
        code if the system isn't running.
        """
        port = int(port)
        number_of_packets = int(number_of_packets)

        data = yield self.post('packet_grab/%i' % port, raw=True, num_packets=number_of_packets)
        coroutine_return(np.fromstring(data, np.uint8).reshape((number_of_packets, -1)))

    @coroutine
    def vis(self, freq):
        """
        Send it {"freq":[0,64]} - range depends on mode.
        Sends a binary "Content-Type: application/octet-stream" with size "num_elements * (num_elements + 1) / 2"  i.e. the upper triangle matrix in row major order.
        """
        freq = int(freq)
        data = yield self.post('vis', freq=freq)
        # return np.fromstring(data, np.uint8).reshape((number_of_packets, -1))
        coroutine_return(data)

    def parse_hexdump(self, hexdump):
        """
        Parses a string as a series of hexdumps. Each packet is seperated by a single line containing 'Packet'.
        Returns an list containing a uint8 array for each packet.
        """
        packets = []
        for line in hexdump.splitlines():
            if line.startswith('Packet'):
                packets.append([])
            else:
                split_line = line.split(None, 17) # Split at most 17 items, remove empty splits
                packets[-1] += [int(c, 16) for c in split_line[1:17] if c]
        return [np.array(p, np.uint8) for p in packets]

    @coroutine
    def capture_raw_packets(self, port, number_of_packets=5):
        """ Parses the inspect_packet output and return the captured packets as a list of strings.
        """
        data = yield self.packet_grab(port, number_of_packets)
        coroutine_return(data)

    @coroutine
    def capture_packets(self, port=0, number_of_packets=5, print_packet_info=True):
        """ Obtain packets from the node and decode them.
        """
        # Get raw packets
        raw_packets = yield self.packet_grab(port, number_of_packets)

        # Process the packets
        result = []
        for pkt in raw_packets:
            d = NameSpace()
            d.hostname = self.hostname
            d.interface_name = 'dna%i' % port
            d.port_number = port
            d.ethernet_packet_size = len(pkt)
            d.mac_dst = ':'.join(['%02X' % c for c in pkt[0:6]])
            d.mac_src = ':'.join(['%02X' % c for c in pkt[6:12]])
            d.ethertype = '%04X' % (pkt[12]*256 + pkt[13])
            d.ip_length = pkt[16]*256 + pkt[17]
            d.ip_protocol = pkt[23]
            d.ip_src = pkt[26:30]
            d.ip_dst = pkt[30:34]
            d.udp_src_port = pkt[34]*256 + pkt[35]
            d.udp_dst_port = pkt[36]*256 + pkt[37]
            d.udp_length = pkt[38]*256 + pkt[39] # includes 8 bytes of the UDP header
            d.udp_payload_length = d.udp_length - 8

            udp_payload = pkt[42:42 + d.udp_payload_length].view('<u4')  #  word array
            d.header_words = udp_payload[0:4]
            # Header word 0
            d.cookie = np.uint8(d.header_words[0] & 0xFF)
            d.header_length_in_words = int((d.header_words[0] >> 8) & 0xF)
            d.protocol = int((d.header_words[0] >> 12) & 0xF)
            d.stream_id = np.uint16((d.header_words[0] >> 16) & 0xFFFF)
            d.source_lane_number = int(d.stream_id & 0xF)
            d.source_slot_number = int((d.stream_id >> 4) & 0xF) + 1
            # Header word 1
            d.encoding_flags = int((d.header_words[1] >> 28) & 0xF)
            d.four_bit_encoding = bool(d.encoding_flags & 0b0001)
            d.offset_binary_encoding = bool(d.encoding_flags & 0b0010)
            d.crossbar2_bypass = bool(d.encoding_flags & 0b0100)
            d.number_of_frames_per_packet = int((d.header_words[1] >> 24) & 0xF)
            d.number_of_bins_per_frame = np.uint16((d.header_words[1] >> 12) & 0xFFF)
            d.number_of_adc_channels_per_bin = np.uint16((d.header_words[1] >> 0) & 0xFFF)
            # Header word 2
            d.ancillary_data = d.header_words[2]
            # Header word 3
            d.timestamp = d.header_words[3]
            d.data_words = udp_payload[4:]
            d.raw_data_bytes = udp_payload[4:].astype('>u4').view(np.uint8) # UDP payload, without header (but includes data, scaler flags, frame flags, packet flags
            d.shuffle_data_bytes = udp_payload[4:].view(np.uint8)
            d.data_length = len(d.raw_data_bytes) # length of all the packet without the header
            result.append(d)
            if print_packet_info:
                print('Timestamp %08X, Ethernet packet= %i bytes' % (d.timestamp, d.ethernet_packet_size))
        coroutine_return(Ccoll(result))  # Ccoll allows attributes of the list elements to be accessed directly in parallel

    # def get_raw_data(self, port=0, number_of_packets=5):
    #     """ Capture and return the raw data bytes from specified ``port``. Data is concatenated into a single vector. """
    #     if isinstance(port, (list, tuple)):
    #         return np.array([np.concatenate(self.capture_packets(p, number_of_packets).raw_data_bytes) for p in port])
    #     else:
    #         return np.concatenate(self.capture_packets(port, number_of_packets).raw_data_bytes)


def parse_cmdline_args(argv):
    parser = argparse.ArgumentParser(description="kotekan: kotekan client/server command line interface", epilog="""
        """)
    parser.add_argument('args', type=str, nargs='*', default='',  help='config name and/or command')
    parser.add_argument('-p', '--port', default=KotekanAsyncRESTServer.DEFAULT_PORT, type=int, help="Server port")
    parser.add_argument('-n', '--host', default='localhost', type=str, help="Server hostname")
    parser.add_argument('-s', '--server', action='store_true', help='Start a server')
    return parser.parse_args(argv)



if __name__ == '__main__':
    # Create our own IOLoop so we don't interfere with ipython's own ioloop.
    ioloop = IOLoop()
    ioloop.make_current()

    # Setup logging
    #log.setup_logger(__name__, stderr_log_level='warning', syslog_level='debug')
    logging.getLogger().setLevel('INFO')

    args = parse_cmdline_args(sys.argv[1:])
    port = args.port
    host = args.host
    is_server = args.server
    args = args.args
    first_arg = args[0].lower() if args else None
    node_config = None
    server = None  # PowerSupply server object
    client = None  # PowerSupply client object
    # print(args.args[1:], first_arg)

    if args and not hasattr(KotekanAsyncRESTClient, args[0]):
        if len(args) >= 2:
            print('Loading %s from config %s ' % (args[1], args[0]))
            config = NameSpace(load_yaml_config(args[0]))
            node_name = args[1]
            node_config = config.kotekan.nodes[node_name]
            args = args[2:]
        else:
            raise RuntimeError('Please specify both a config root name and power supply name')

    if is_server:
        server_port = node_config.port if node_config else port
        server = RunSyncWrapper(KotekanAsyncRESTServer(port=server_port))
        if node_config:
            server.start(None, name=node_name, **node_config)
        print("Kotekan REST Server started. Waiting for REST commands.")
        server.run()
        print("\nI'm done. Bye!")

    else:
        client_port = node_config.port if node_config else port
        client_host = node_config.hostname if node_config else host
        client = RunSyncWrapper(KotekanAsyncRESTClient(hostname=client_host, port=client_port))
        if node_config:
            client.start(node_config)
        # If the client started a server, get it for the interactive session
        if hasattr(client,'server'):
            server = RunSyncWrapper(client.server)
        # If there are further arguments, assume they are commands
        if args:
            cmd = args[0]
            if cmd and hasattr(client, cmd):
                print('Sending command %s(%s) to CHIME Master server %s:%s' % (cmd, ', '.join(args[1:]), client_host, client_port))
                print(getattr(client, cmd)(*args[1:]))


    print()
    print("If this was run in an interactive session (ipython -i), the following variables are now accessible:")
    if server: print("   server: Kotekan REST server")
    if client: print("   client: Kotekan REST client")
