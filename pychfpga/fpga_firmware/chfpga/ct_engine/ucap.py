#!/usr/bin/python

"""
ucap.py module
Interface to the FPGA UltraRAM-based frame capture module.
"""

import logging
import asyncio
import socket
import time

from ..mmi import MMI, BitField, CONTROL, STATUS
import numpy as np
import __main__



class UCAP(MMI):
    """ Object that allows access to an UltraRAM-based frame capture module"""
    ADDRESS_WIDTH = 16

    MODE             = BitField(CONTROL, 0, 6, width=2, doc='Capture mode: 0: 8 channel, 1: 4 channel, 2: 2 channel, 3: 1 channel')
    CH0              = BitField(CONTROL, 0, 0, width=3, doc='Channel #0 in 1, 2, or 4-channel mode')
    CH1              = BitField(CONTROL, 0, 3, width=3, doc='Channel #1 in 2 or 4-channel mode')
    USER_RESET       = BitField(CONTROL, 1, 7, doc='User reset')
    OUTPUT_SOURCE_SEL = BitField(CONTROL, 1, 6, doc='Selects what data is streamed out: 0: UCAP, 1: CORR44')
    CH2              = BitField(CONTROL, 1, 0, width=3, doc='Channel #2 in 4-channel mode')
    CH3              = BitField(CONTROL, 1, 3, width=3, doc='Channel #3 in 4-channel mode')
    CAPTURE_PERIOD   = BitField(CONTROL, 4, 0, width=24, doc='Number of frames between captures for source 0')
    CAPTURE_PERIOD2  = BitField(CONTROL, 7, 0, width=24, doc='Number of frames between captures for source 1')
    SUB_PERIOD       = BitField(CONTROL, 8, 0, width=5, doc='Dynamic capture rate, 2**(N+1) frames')
    SOURCE_SEL       = BitField(CONTROL, 9, 0, width=8, doc='0 = source selector output (timestream), 255 = scaler output (spectrum)')
    USER_STREAM_ID   = BitField(CONTROL, 10, 0, width=8, doc='Bits 11:4 of the Stream ID found in the raw packet header')

    FIFO_OVERFLOW    = BitField(STATUS, 0, 0, doc='1 when dat FIFO has overflowed. Sticky flag.')
    OVERRUN          = BitField(STATUS, 0, 1, doc='1 when data transmission request was performed before the previous transmission was completed. Sticky flag.')
    RST_MON          = BitField(STATUS, 0, 2, doc='debug')
    USER_RST_MON     = BitField(STATUS, 0, 3, doc='debug')
    PERIOD_CTR       = BitField(STATUS, 1, 0, width=8, doc='debug')
    SOURCE_SEL_IN    = BitField(STATUS, 2, 0, width=8, doc='debug')

    def __init__(self, *, router, router_port, verbose=0):
        self.verbose = verbose
        self.logger = logging.getLogger(__name__)
        super().__init__(router=router, router_port=router_port)
        self.sock = None

    def init(self):
        """ Initializes UCAP module"""
        self.USER_STREAM_ID = self.fpga.slot or 0

    DATA_SOURCE_TABLE = {
        'funcgen': 0,
        'scaler': 255} # 255: set data source for all 8 channels (11111111 = 255 for unsigned 8-bit bin)

    def set_data_capture(self, source, mode=0, channels=None, periods=[195312,195312], select=False):
        """ Selects the source of the data to be captured (FUNCGEN or SCALER). Optionally enables streaming the captured data instead of the correlator data.

        Parameters:

            source (str): Source to use

            mode (int): capture mode to use

            channels (list of int): list of channels do capture. Can be ``None`` for mode 0 since all channels are captured anyways.

            periods (list of int): Time between data captures for each of the sources, expressed in
                number of frames MINUS ONE. The number of consecutive frames stored by each capture
                is set by the `mode`.

            select (bool): If True, the output multiplexer of UCAP will be set to stream the
                captured data instead of the auxiliary (correlator) data.

        """
        # Set the data source (either 'adc' or 'scaler') in the control register
        if isinstance(source, str):
            if source in self.DATA_SOURCE_TABLE:
                source = self.DATA_SOURCE_TABLE[source]
            else:
                valid_sources = ', '.join(self.DATA_SOURCE_TABLE)
                raise ValueError(f"Unknown data capture source '{source}'. Valid sources are {valid_sources}")
        self.SOURCE_SEL = source

        if isinstance(channels, int):
            channels = [channels]

        if mode == 0:
            if channels is None:
                channels = list(range(8))
            if channels != list(range(8)):
                raise RuntimeError('UCAP mode 0 can only capture channels 0-7, in that order')
        elif mode == 1:
            if channels is None:
                channels = list(range(4))
            if len(channels) != 4:
                raise RuntimeError('UCAP mode 1 can only capture 4 channels')
            self.CH0 = channels[0]
            self.CH1 = channels[1]
            self.CH2 = channels[2]
            self.CH3 = channels[3]
        elif mode == 2 :
            if channels is None:
                channels = list(range(2))
            if len(channels) != 2:
                raise RuntimeError('UCAP mode 2 can only capture 2 channels')
            self.CH0 = channels[0]
            self.CH1 = channels[1]
        elif mode == 3:
            if channels is None:
                channels = list(range(1))
            if len(channels) != 1:
                raise RuntimeError('UCAP mode 3 can only capture 1 channel')
            self.CH0 = channels[0]
        else:
            raise RuntimeError(f'Invalid UCAP mode number {mode}')
        self.MODE = mode
        self.CAPTURE_PERIOD = periods[0]
        self.CAPTURE_PERIOD2 = periods[1]

        if select:
            self.select_output('ucap')

    OUTPUT_NAMES = {
        'cap':0,
        'ucap': 0,
        'aux': 1,
        'corr': 1}

    def select_output(self, out):
        """ Select whether UCAP streams capture or auxiliary (correlator) data

        Parameters:

            out (str or int): Output to stream: 0: Captured data; 1: auxiliary (correlator) data. If a string, the value will be looked up in OUTPUT_NAMES.

        """
        self.OUTPUT_SOURCE_SEL = out if isinstance(out, int) else self.OUTPUT_NAMES[out]


    def get_data(self, flush_timeout=0.01):

        self.sock = self.fpga.get_data_socket()
        # s = getattr(__main__,"ucap_sock", None)
        # if s:
        #     self.sock = s
        # if self.sock is None:
        #     self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM);
        #     self.sock.bind(("0.0.0.0",41000))
        #     __main__.ucap_sock = self.sock
        # Flush the UDP  buffers by reading and discarding data until we timeout
        self.sock.settimeout(flush_timeout)
        b = np.zeros((128, 8192+5), '>i2')
        print('Flushing UDP buffers')
        while True:
            try:
                self.sock.recv_into(b[0])
            except socket.timeout:
                break
        self.sock.settimeout(2)
        print('Capturing data')
        for bb in b:
            self.sock.recv_into(bb)
        print(f'got stream_IDs: {b[:, 1] >> 8}')
        for bb in b:
            print(bb[:5].tobytes().hex(':'))
        # sid =
        # sid_ok = all(b[:,1]>>8 == np.arange(b.shape[0], dtype=np.uint8) & 63)
        # if not sid_ok:
        #     raise RuntimeError('Missing packets')

        return b[:64, 5:]

    def get_data_receiver(self, sock=None, buffer_length=256):
        return RawFrameReceiver(sock or self.fpga.get_data_socket(), buffer_length=buffer_length)

class RawFrameReceiver(object):
    """
    Pure Python socket receiver to capture the raw ADC or FFT data from the FPGA.

    Note that the packets are larger than 1500 bytes, which requires the
    networking equipment and the computer interface to be configured to
    receive Jumbo frames.

    The receiver is fast enough to capture data exceeding 500
    Mbits/s, provided the system provides a sufficiently big UDP buffer to hold
    the data until the receiver method is called to process it (see below).

    Parameters:

        socket (socket.socket): An opened and bound UDP socket to which the
            FPGA correlator data will be sent. The socket will not be closed
            when the call is completed.

    System Requirements:

    The Ethernet interface must be set to receive Jumbo frames::

        sudo ifconfig eno1 mtu 9000

    The UDP buffers shall be increased to reduce packet loss to a minimum::

        sudo sysctl -w net.core.rmem_max=26214400
        sudo sysctl -w net.core.rmem_default=26214400
        sudo sysctl -w net.ipv4.udp_mem='26214400 26214400 26214400'
        sudo sysctl -w net.ipv4.udp_rmem_min=26214400

    Check udp buffers::

        sysctl -a | grep mem

    Monitor UDP buffer::

        watch -cd -n .5 "cat /proc/net/udp"
    """

    def __init__(self, socket, buffer_length=256):

        self.socket = socket
        self.NPACKETS = buffer_length
        self.FRAME_SIZE = 16384*2 # bytes
        self.DATA_SIZE = self.FRAME_SIZE // 4
        # self.ignore_packet_size = ignore_packet_size
        # Define numpy data types that will be used to efficiently parse the data

        self.header_dtype = np.dtype(dict(
            names=['cookie', 'stream_id', 'source_crate', 'slot_chan', 'subframe', 'ts'],
            offsets=[0, 1, 1, 2, 3, 2],
            formats=['u1', '>u2', 'u1', 'u1', 'u1', '>u8']))

        self.packet_dtype = np.dtype([
            ('header', self.header_dtype, (1, )),
            ('data', np.int8, (self.DATA_SIZE, ))])

        self.PACKET_SIZE = self.packet_dtype.itemsize

        # Pre-allocate buffers
        # Buffer in which recv_into() will put the data directly
        self.buf = np.zeros((self.NPACKETS, self.PACKET_SIZE), dtype=np.int8)  # can use empty(), but the uninitialized data can be confusing for debugging
        self.n = 0  # buffer write index
        self.last_ts = None  # timestamp of the last packet written in the buffer

        # Various views of the buffer to allow quick and easy access to the packet contents

        self.buf_struct = self.buf.view(self.packet_dtype)[:,0]

        self.buf_cookie = self.buf_struct['header']['cookie'][:, 0]
        self.buf_stream_id = self.buf_struct['header']['stream_id'][:, 0]
        # self.buf_source_crate = self.buf_struct['header']['source_crate'][:, 0]
        self.buf_slot_chan = self.buf_struct['header']['slot_chan'][:, 0]
        self.buf_subframe = self.buf_struct['header']['subframe'][:, 0]
        self.buf_ts = self.buf_struct['header']['ts'][:, 0]
        self.buf_ts_dirty = self.buf_struct['header']['ts'][:, 0]
        self.buf_data = self.buf_struct['data']
        self.ts_mask = np.uint64(0xFFFFFFFFFFFF)  # just keep the last 48 bits
        NCHAN_MAX = 8
        FRAMES_PER_CHANNEL_MAX = 16
        self.cookie = cookie = 0xa0

    def flush(self, timeout, verbose=0):
            # Read packets until timeout
            flushed = 0
            self.socket.settimeout(timeout)
            while True:
                try:
                    s = self.socket.recv_into(self.buf[0])
                    flushed += 1
                    if verbose >=2:
                        print(f'flushing packet len={s} cookie=0x{self.buf_cookie[0]:02x} ts={self.buf_ts[0] & self.ts_mask}')

                except socket.timeout:
                    break
            # We assume the buffer might have overflowed and contains partial
            # timestamp. We flush until we gen a new timestamp.
            return flushed
    def wait_for_new_timestamp(self, cookie, timeout, verbose=0):
            self.socket.settimeout(timeout)
            flushed = 0
            while True:
                try:
                    s = self.socket.recv_into(self.buf[0])
                    flushed += 1
                    if self.buf_cookie[0] & 0xfe != cookie:
                        continue
                    ts = self.buf_ts[0] & self.ts_mask
                    if self.last_ts is None:
                        self.last_ts = ts
                        continue
                    if self.last_ts == ts:
                        if verbose >=2:
                            print(f'skipping packet len={s} cookie=0x{self.buf_cookie[0]:02x} ts={ts}')
                        continue
                    self.last_ts = ts
                    self.n = 1
                    break
                except socket.timeout:
                    continue
            return flushed

    def receive_packets(self, cookie, verbose=0):
        """ Receive some packets into the buffer until there is a timeout or the buffer is full
        """
        n = self.n # current packet number
        last_ts = None
        while n < self.NPACKETS:
            try:
                s = self.socket.recv_into(self.buf[n])
                # check if the packet has the right cookie
                if verbose >= 2:
                    print(f'got packet len={s} cookie=0x{self.buf_cookie[n]:02x} ts={self.buf_ts[n] & self.ts_mask}')
                if self.buf_cookie[n] & 0xfe != cookie: # ignore packets with wrong cookie
                    continue

                # Ignore packets that don't have the right length
                if s != self.PACKET_SIZE:
                    continue
                ts = self.buf_ts[n] & self.ts_mask
                if verbose >=2:
                    print(f'ts={ts}, last_ts={self.last_ts}, sid={self.buf_stream_id[n]:04x}')
                n += 1
                if last_ts is None: # if we don't have a last timestamp, initialize it and continue
                    self.last_ts = ts
                elif ts != last_ts: # if we have a new timestamp, exit
                    break
            except socket.timeout:  # we have a timeout, so we probably have time to process data
                print('timeout')
                continue
        # we get here if there is a timeout, a timestamp change, or if the buffer is full
        if verbose:
            print(f'got {n} packets, delta_ts={ts-self.last_ts}')
        self.n = n
        return n

    def read_raw_frames(
            self,
            stream_ids=range(8),
            flush=True,
            data_timeout=1,
            flush_timeout=0.001,
            format='8',
            ncap = None,
            split = False,
            verbose=0):
        """ Capture raw data frames sent by UCAP.

        Parameters:

            stream_ids (list of int): List of channels to capture

            flush (bool): If True, the UDP buffer will be emptied and capture will be realigned to the next full new timestamp.

            data_timeout (float): Amount of time (in seconds) to wait for data. Does not affect the data capture,
                but a value larger that the max amount of time we expect to reasonably wait for data
                packets will be slightly more efficient.

            flush_timeout (float): Amount of time (in seconds) to wait until we decide there are no
                longer packets in the UDP buffers. Must be smaller than the time between packets,
                otherwise the buffer will never be emptied. number_of_frames (int): number of frames
                to capture for each channel.

            format (str): output data format:

                '8': return the data as array of bytes (int8)
                '16': return the data as array of 16-bit signed integers. Use when capturing the output of the FUNCGEN.
                '16+16': return the data as an array of (16+16) bit complex numbers. Use for data at the output of the SCALER (unless the FFT is bypassed)

            ncap (int): Number of bursts to capture

            verbose (int): verbosity level. 0: no messages, 1: basic messages, 2: detailed messages

        Returns:
         (timestamp, data, count) tuple where:

            timestamp (int): timestamp of the captured packet

            data (ndarray): Nc*Ns array, where Nc is the number of channels, and Ns is the number of
                samples. Ns depends on the capture mode used by UCAP (it is determined from the
                packet contents)

            count (ndarray): number of packets received for each channel


        use flush=true is we expect the UDP buffer to contain old data tht needs to be discarded.
        """
        sid_map = {sid:ix for ix, sid in enumerate(stream_ids)}

        if flush:
            flushed = self.flush(flush_timeout, verbose=verbose)
            if verbose >=1:
                print(f'Flushed {flushed} packets while emptying UDP buffers')
            self.last_ts = None
            flushed = self.wait_for_new_timestamp(self.cookie, data_timeout, verbose=verbose)
            if verbose:
                print(f'Skipped {flushed} packets while waiting for a fresh timestamp')
        self.socket.settimeout(data_timeout)

        if not self.n:
            raise RuntimeError('There is no initial data in the buffer. Run with Flush=True first')
        return_multiple_captures = ncap is not None
        ncap = ncap or 1

        # Determine the capture mode based in the first packet in the buffer
        mode = (self.buf_subframe[0] >> 2) & 0x3
        frames_per_channel = 2 * 2**(mode)
        nchan = len(sid_map)  # number of captured channels
        if verbose:
            print(f"mode={mode}, {nchan} channel(s), {frames_per_channel} frames per channel, {ncap} captures")

        # Allocate destination buffer

        data = np.zeros((nchan, ncap, self.FRAME_SIZE*frames_per_channel), dtype=np.int8)
        data_count = np.zeros((nchan, ncap), dtype=np.uint16)
        ts = np.zeros((ncap,), dtype=np.uint64)
        ts_dict = {} # {timestamp:n, ...): keeps track of the known timestamps and associated capture slots
        n = 0 # current capture slot
        sid = []
        last_timestamp = None
        while n < ncap + 1: # process packets until we have checked ncap+1 timestamps to make sure we have all the data for ncap timestamps
            # Get some packets until a new timestamp, timeout, or buffer full
            npkts = self.receive_packets(cookie=self.cookie, verbose=verbose)

            if verbose >=2:
                print(f'sf={self.buf_subframe[:self.n]}')

            t0 = time.time()
            for i in range(npkts):
                timestamp = self.buf_ts[i] & self.ts_mask
                if timestamp != last_timestamp:  # update n only when the timestamp changes to minimize slow lookups
                    last_timestamp = timestamp
                    n = ts_dict.setdefault(timestamp, len(ts_dict)) # get existing slot if timestamp exists, otherwise create next slot
                    if verbose:
                        print(f'Switching to slot {n} at timestamp {timestamp}')
                    if n < ncap:
                        ts[n] = timestamp
                if n < ncap:
                    sid.append(self.buf_stream_id[i])
                    bix = sid_map.get(self.buf_stream_id[i] & 0xFFF, None)
                    subframe = self.buf_subframe[i] & 0x3
                    frame = (self.buf_stream_id[i] >> 12) & 0x0F
                    if verbose >= 3:
                        print(f'sid = {self.buf_stream_id[i] & 0xFFF:03x}, Ch={bix}, frame={frame}, subframe={subframe}')
                    if bix is not None:
                        x = frame * self.FRAME_SIZE + subframe*self.DATA_SIZE
                        data[bix, n, x:x+self.DATA_SIZE] = self.buf_data[i]
                        data_count[bix, n] += 1
            self.n = 0
        if verbose:
            for i in range(nchan):
                print(f'Chan {i}: {data_count[i].sum()-ncap*16*4 or "no"} missing packets')

        if ncap > 1 and verbose:
            print(f'Timestamp differences: {set(np.diff(ts))}')

        if format == "16":
            data = data.view('>i2')
        elif format == "14":
            data = data.view('>i2') >> 2
        elif format == "16+16":
            data = data.view('>i2')
            data = data[:, :, ::2] + 1j*data[:, :, 1::2]
        elif format == "32":
            data = data.view('>i4')
        elif format == "32+32":
            data = data.view('>i4')
            data = data[:, :, ::2] + 1j*data[:, :, 1::2]
        elif format == "64+64":
            data = data.view('>i8')
            data = data[:, :, ::2] + 1j*data[:, :, 1::2]
        elif format == '8':
            pass
        else:
            raise ValueError(f'Invalid format "{format}"')
            
        if split:
            data = data.reshape((nchan, ncap*frames_per_channel,-1))

        if return_multiple_captures:
            return ts, data, data_count
        else:
            return ts, data[:,0], data_count[:,0]
