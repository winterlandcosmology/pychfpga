import numpy as np
import struct
import time
import logging
from subprocess import Popen, PIPE
import shlex
import os
import socket
import json
#import core.icecore.icebox

from sqlalchemy import Column, Integer, String, ForeignKey
from sqlalchemy import UniqueConstraint, CheckConstraint
from sqlalchemy.orm import relationship, backref
from sqlalchemy.orm.collections import attribute_mapped_collection

from pychfpga.core.icecore import hardware_map
from pychfpga.core.icecore import handler
from pychfpga.core.icecore import session
from pychfpga.core.icecore import Ccoll


class GpuData(object):
    def __repr__(self):
        return 'GpuData(timestamp=%08x, packet_length = %i)' % (self.timestamp, self.ethernet_packet_size)

    def __str__(self):
        name_width = max(len(name) for name in vars(self).keys())
        name_fmt = '%%%is' % name_width
        s = []
        for (name, value) in  sorted(vars(self).items()):
            if name.startswith('_') or name=='data':
                continue
            if isinstance(value, int):
                hex_value = '(0x%X)' % value
            elif np.isscalar(value) and hasattr(value, 'nbytes'):  # this is a numpy number
                hex_value = ('(0x%%0%iX)' % (value.nbytes * 2)) % value
            else:
                hex_value = ''
            s.append((name_fmt + ': %r %s') % (name, value, hex_value))
        return '\n'.join(s)

    def get_timestream_data(self):
        return self.data.astype('<u4').view(np.int8)  # Make the words be stored LSB first in memory, and convert to int8



@session.register_yaml_object()
class GpuNode(hardware_map.HWMResource, handler.HandlerObject):
    handler_name = 'GpuNodeHandler'
    __tablename__ = 'GpuNode'
    # __table_args__ = (
    #     UniqueConstraint('serial'),
    # )
    __mapper_args__ = {'polymorphic_identity': 'GpuNode',
                       'polymorphic_on':'_polymorphic_key'}
    __ipmi_part_number__ = None  # Must match part number in IPMI data

    _pk = Column(Integer, primary_key=True)
    _polymorphic_key = Column(String)  # Needed to allow multiple types of power supplies

    hostname = Column(String,
                    doc="The hostname (ip address or name of the GPU node")

    # serial = Column(String,
    #                 doc="The serial number written on the board (e.g. '001')")

    def __repr__(self):
        return "%s(%s)" % (self.__class__.__name__, self.hostname)


class GpuNodeHandler(handler.Handler):
    __handler_for__ = GpuNode

    hostname = handler.HandlerParentAttribute(lambda ib: ib.hostname)

    def __init__(self, hostname=None, node_type='packet_server', **kwargs):
        super(GpuNodeHandler, self).__init__(**kwargs)
        if node_type not in self.NODE_TYPES:
            raise ValueError("Node type can only be one of the following: %s" % ', '.join(self.NODE_TYPES.keys()))
        self.node_type = node_type
        (self.number_of_ports, self.inspect_method) = self.NODE_TYPES[node_type]
        if hostname is not None:
            self.hostname = hostname

    def open(self):
        pass

    def _inspect_gamma_win(self, port=0,  number_of_packets=5):
        """
        Calls inspect_packet on gamma from a Windows host.

        We use the ssh -tt option to spawn a teletype, because the sudoers list is configured to require a TTY to allow sudo.
        """
        if number_of_packets != 5:
            raise ValueError('with inspect_pkt_dna_select, number of packets must be 5')
        command = 'ssh -i %%HOMEPATH%%/.ssh/gamma-user gamma-user@%s -tt "sudo ~/inspect_pkt_dna_select dna0"' % (self.hostname)
        (data, stderr) = Popen(shlex.split(command), stdout=PIPE, shell=True).communicate()
        return self.parse_hexdump(data)

    def _inspect_chi(self, port=0, number_of_packets=5):
        if number_of_packets != 5:
            raise ValueError('with inspect_pkt_dna_select, number of packets must be 5')
        command = 'sudo ssh -i /root/.ssh/id_rsa root@%s "/root/inspect_pkt_dna_select dna%i"'  % (self.hostname, port)
        (data, stderr) = Popen(shlex.split(command), stdout=PIPE).communicate()
        return self.parse_hexdump(data)

    def _inspect_server(self, port=0, number_of_packets=1):
        """
        Obtain data from an inspect server running on the node.

        The server listens to TCP port 5001 and responds with JSON headers followed by binary data.
        """
        command = 'dna%i, n=%i\n' % (port, number_of_packets)

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2)
        sock.connect((self.hostname, 5001))
        sock.send(command)
        fh = sock.makefile()
        packets = []
        try:
            for i in range(number_of_packets):
                header = fh.readline()
                # print header,
                h = json.loads(header)
                # print h
                err = h['error']
                if err:
                    raise RuntimeError('inspect_pkt_server returned the folloring error: %s' % err)
                n = h['packet_length']
                packets.append(np.fromstring(fh.read(n), np.uint8))
        except:
            raise
        finally:
            # print 'closing connection'
            sock.shutdown(socket.SHUT_RDWR)
            sock.close()
        return packets

    NODE_TYPES = {
        'packet_server':  (16, _inspect_server),  # ssh, Logs in as gamma-user, requires a private key in ~/.ssh. Works on windows if ssh (or git) is installed
        'gamma-win':  (8, _inspect_gamma_win),  # ssh, Logs in as gamma-user, requires a private key in ~/.ssh. Works on windows if ssh (or git) is installed
        'chi': (16, _inspect_chi)  #
        }

    def parse_hexdump(self, hexdump):
        """
        Parses a string as a series of hexdumps. Each packet i sseperated by a single line containing 'Packet'.
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

    def capture_raw_packets(self, port, number_of_packets=5):
        """ Parses the inspect_packet output and return the captured packets as a list of strings.
        """
        if port >= self.number_of_ports:
            raise ValueError('Invalid dna port number')
        return self.inspect_method(self, port, number_of_packets)

    def capture_packets(self, port=0, number_of_packets=5, print_packet_info=True):
        """ Obtain packets from the node and decode them.
        """
        # Get raw packets
        raw_packets = self.capture_raw_packets(port, number_of_packets)

        # Process the packets
        result = []
        for pkt in raw_packets:
            d = GpuData()
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
            d.raw_data_bytes = udp_payload[4:].astype('>u4').view(np.uint8)
            d.shuffle_data_bytes = udp_payload[4:].view(np.uint8)
            d.data_length = len(d.raw_data_bytes)
            result.append(d)
            if print_packet_info:
                print 'Timestamp %08X, Ethernet packet= %i bytes' % (d.timestamp, d.ethernet_packet_size)
        return Ccoll(result)  # Ccoll allows attributes of the list elements to be accessed directly in parallel

    def get_raw_data(self, port=0, number_of_packets=5):
        """ Capture and return the raw data bytes from specified ``port``. Data is concatenated into a single vector. """
        if isinstance(port, (list, tuple)):
            return np.array([np.concatenate(self.capture_packets(p, number_of_packets).raw_data_bytes) for p in port])
        else:
            return np.concatenate(self.capture_packets(port, number_of_packets).raw_data_bytes)

if __name__ == '__main__':
    if os.name == 'nt':
        n = GpuNode('10.10.10.200')
