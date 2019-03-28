import socket
import logging
import pychfpga
import time
import numpy as np

def get_ps():
    ps = pychfpga.AgilentN5764AHandler('10.10.10.151')

def init_fpga():
    log = logging.getLogger()
    log.setLevel('INFO')
    ca = pychfpga.FPGAArray('bp16 025', prog=2 ,open=1, stderr_log_level='info')
    ca.ib.set_offset_binary_encoding(0)
    ca.ib.GPU.DEST_ADDR_MODE=1
    ca.ib.GPU.TEST_PACKET_LENGTH=0x0040
    ca.ib.GPU.TEST_PACKET_PERIOD=0xff00
    ca.ib.set_gains((800,12))  # assuming a -5 dBm signal through a resistive 1:2 splitter (6 dB loss)
    ca.set_sync_method('distributed_time')
    ca.set_operational_mode('shuffle512', frames_per_packet=1)
    return ca

def get_raw_socket():
    """
    Works only if python is run with root priviliges
    """
    s=socket.socket( socket.AF_PACKET , socket.SOCK_RAW , socket.ntohs(0x0003))
    return s

def wait_for_time(t):
    while time.time() % 1 < t:
        pass

class CornerTurnData(object):
    def __repr__(self):
        return 'CornerTurnData(timestamp=%08x, packet_length = %i)' % (self.timestamp, self.ethernet_packet_size)

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


SHUFFLE_TYPES = {
    0: 'raw',
    1: 'shuffle16',
    2: 'shuffle256',
    3: 'shuffle512' }

def decode_packets(self, packets):
    """ Decode the packets obtained at the output of the FPGA Corner Turn
    engine, which is also the data sent to the GPU.

    Parameters:

        packets

    """
 
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

def to_cplx(x):
    return np.int8(x) // 16 + (np.int8(x << 4) * 1j / 16.)

def find_edge(s, bin=-1, chan=0, shuffle_level=512):
    """
    Assumes frames_per_packet=1
    """
    frames_per_packet = 1

    if shuffle_level == 16:
        pkt_length = 2376 # Shuffle16
        chan_per_bin = 16
        bins_per_frame = 128
    elif shuffle_level == 512:
        pkt_length = 2496 #Shuffle512
        chan_per_bin = 512
        bins_per_frame = 4
    else:
        raise(ValueError('Invalid shuffle level'))
    data_bytes_total = frames_per_packet * bins_per_frame * chan_per_bin
    while(True):
        wait_for_time(0.99)
        for _ in xrange(100):
            s.recv(9000) 
        r = [s.recv(9000) for _ in xrange(100000)]
        d = [np.frombuffer(rr, dtype=np.int8) for rr in r]
        ddd = np.array([x[42: 42 + 16 + data_bytes_total] for x in d if len(x) == pkt_length])
        ts = ddd[:, 12:16].copy().view('<u4')[:, 0]
        dd = ddd[:, 16: 16 + data_bytes_total]
        stream_id = ddd[:, 2:4].copy().view('<u2')[:, 0]
        lane = (stream_id >> 0) & 0xF
        slot = (stream_id >> 4) & 0xF
        crate = (stream_id >> 8) & 0xF
        stream_type = (stream_id >> 12) & 0xF

        if bin < 0: # check every channels of all bins
            f = dd.any(axis=-1)
        else:
            f = dd[:, chan_per_bin * bin + chan]
        i = np.where(f)[0]
        if len(i):
            i0 = i[0]  # first event
            i1 = i[-1]  # last event
            tt = ts % 390625
            if ts[i0] - ts[i0-1] == 1:
                print('%s packet from (crate, slot, lane)=(%i,%i,%i): Rising Edge: %s@%i, %s@%i, %s@%i, Falling edge: %s@%i, %s@%i' % (
                    SHUFFLE_TYPES[stream_type[i0]],
                    crate[i0], slot[i0], lane[i0],
                    to_cplx(f[i0 - 1]), tt[i0 - 1],
                    to_cplx(f[i0]), tt[i0],
                    to_cplx(f[i0 + 1]), tt[i0 + 1],
                    to_cplx(f[i1]), tt[i1],
                    to_cplx(f[i1 + 1]), tt[i1 + 1] ))
            else:
                print('Non-consecutive frames captured at the trigger event. Retrying...')
    return ts, dd


def sync_timing_test(ca, Nsync=10, Nscan=1):
    """ Measure the timing of the SYNC event
    """
    sync_time = [] # [iter, slot]
    frame0_time = [] 
    delay_list = []
    for scan_number in range(Nsync):
        for delay in range(0,10*Nscan,10):
            for trial in range(3):
                try:
                    ca.sync(delay = 3 - delay/1e9)
                    sync_time.append(ca.sync_start_time.nano)
                    frame0_time.append(ca.sync_timestamps.nano)
                    delay_list.append(delay)
                    break
                except RuntimeError as e:
                    print 'Ignoring %r' % e
    return delay_list, np.array(sync_time), np.array(frame0_time)