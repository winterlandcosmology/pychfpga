import numpy as np
import struct
import time
import logging
from subprocess import Popen, PIPE
import shlex
import os
import socket
import json
import requests

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
    """
    You can start the program from:

    /home/chime-user/ch_gpu/build/kotekan

    Run either:

    sudo ./kotekan -c ../../kotekan.conf
    or
    sudo ./kotekan

    In that later case you'll need to use the /start endpoint, in the first case it just starts automatically.

    The config file needs to have the "mode" in the "system" section set to "packet_cap"
    """

    __handler_for__ = GpuNode

    hostname = handler.HandlerParentAttribute(lambda ib: ib.hostname)

    def __init__(self, hostname=None, **kwargs):
        super(GpuNodeHandler, self).__init__(**kwargs)
        if hostname is not None:
            self.hostname = hostname  # Overrides the HandlerParentAttributes object

    def open(self):
        pass


    def send_command(self, command, args):
        """
        Sends a command to the kotekan REST server

        All endpoints return failure status codes if something goes wrong, along with a (sometimes
        helpful) error message in the "Error: <message>" field of the HTML header.  They don't
        return any json data on failure at the moment.

        """
        port = 12048  # hard coded
        # command = {"port": port, "num_packets": number_of_packets}
        resp = requests.post('http://%s:%i/%s' % (self.hostname, port, command), data=json.dumps(args))
        if resp.reason != 'OK' or resp.status_code != 200:
            raise RuntimeError('The kotekan returned the following error: %i:%s' % (resp.status_code, resp.reason))
        return resp.content

    def start(self, config):
        """
        Requires a json config file. Just returns an html status code on success or failure, and
        error message if a failure happens (see comment on errors near the end).
        """
        data = self.send_command('start', config)

    def stop(self):
        """
        Just give it an empty json string {} and it returns a status code for now.  This one takes a
        little while to return while it clears memory.
        """
        return self.send_command('stop', {})

    def status(self):
        """
        Give it an empty {}, and returns a json {"running": true/false}.   This will do more
        interesting things later.
        """
        return json.loads(self.send_command('status', {}))

    def packet_grab(self, port=0, number_of_packets=1):
        """
        Send it {"num_packets": [1,100]}, returns "Content-Type: application/octet-stream"

        Packet size is html content length divided by num_packets.  Note returns "not found" error
        code if the system isn't running.
        """

        data = self.send_command('packet_grab/%i' % port, {'num_packets': number_of_packets})
        return np.fromstring(data, np.uint8).reshape((number_of_packets, -1))

    def vis(self, freq):
        """
        Send it {"freq":[0,64]} - range depends on mode.
        Sends a binary "Content-Type: application/octet-stream" with size "num_elements * (num_elements + 1) / 2"  i.e. the upper triangle matrix in row major order.
        """
        data = self.send_command('vis', {'freq': freq})
        # return np.fromstring(data, np.uint8).reshape((number_of_packets, -1))
        return data

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

    def capture_raw_packets(self, port, number_of_packets=5):
        """ Parses the inspect_packet output and return the captured packets as a list of strings.
        """
        return self.packet_grab(port, number_of_packets)

    def capture_packets(self, port=0, number_of_packets=5, print_packet_info=True):
        """ Obtain packets from the node and decode them.
        """
        # Get raw packets
        raw_packets = self.packet_grab(port, number_of_packets)

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
            d.raw_data_bytes = udp_payload[4:].astype('>u4').view(np.uint8) # UDP payload, without header (but includes data, scaler flags, frame flags, packet flags
            d.shuffle_data_bytes = udp_payload[4:].view(np.uint8)
            d.data_length = len(d.raw_data_bytes) # length of all the packet without the header
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
