#!/usr/bin/python

"""
ucap.py module
Interface to the FPGA UltraRAM-based frame capture module.
"""

import logging
import asyncio
import socket
import time
import __main__

import numpy as np

from pychfpga.common.udp_packet_receiver import UDPPacketReceiver

from ..mmi import MMI, BitField, CONTROL, STATUS



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
        slot = self.fpga.slot
        self.USER_STREAM_ID = slot-1 if slot else 0

    DATA_SOURCE_TABLE = {
        'funcgen': 0,
        'scaler': 255} # 255: set data source for all 8 channels (11111111 = 255 for unsigned 8-bit bin)

    def set_data_capture(self, source, mode=0, channels=None, periods=[195312,195312], select=False):
        """ Selects the source of the data to be captured (FUNCGEN or SCALER). Optionally enables streaming the captured data instead of the correlator data.

        Parameters:

            source (str): Source to use. The source string represents where the tata is tapped in
                the channelizer pipeline:

                - 'funcgen': The captured data is taken at the output of the function generator module
                  (which can be configured to pass the ADC data or internally-generated waveforms)
                - 'scaler': The captured data is taken from the capture output port (not the main
                  output) of the scaler. The scaler can be configured to output various signals on this
                  port (scaled/unscaled raw FFT data, overflow flags etc.)

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

    # def get_data_receiver(self, packet_receiver):
    #     return RawFrameReceiver(packet_receiver)

    def get_data_receiver(self, sock=None, buffer_length=256):
        return RawFrameReceiver(sock or self.fpga.get_data_socket(), buffer_length=buffer_length)

class RawDataProcessor(object):

    PACKET_COOKIE = 0xA0
    PACKET_COOKIE_MASK = 0xFE
    NBYTES_PER_HEADER = 10 # Number of bytes in the UDP payload header
    NBYTES_PER_PROD = 5  # Number of bytes used to store each complex correlator product ( (18+18) bits = 36 bits, 5 bytes = 40 bits)

    def __init__(self, buf, config=None, verbose=1):


        self.verbose = verbose
        # Define numpy data types that will be used to parse the correlator packets
        self.buf = buf
        # self.NCHAN = None
        # self.NCLK = None
        # self.bin_map = None
        # self.prod_map = None
        # self.firmware_integ_period = None

        self.BYTES_PER_SAMPLE = 2 # CRS
        self.SAMPLES_PER_FRAME = 16384
        self.BYTES_PER_FRAME = self.SAMPLES_PER_FRAME * self.BYTES_PER_SAMPLE # bytes
        self.NUMBER_OF_SUBFRAMES = 4
        self.BYTES_PER_SUBFRAME = self.BYTES_PER_FRAME // self.NUMBER_OF_SUBFRAMES # bytes
        # self.ignore_packet_size = ignore_packet_size
        # Define numpy data types that will be used to efficiently parse the data

        self.header_dtype = np.dtype(dict(
            names=['cookie', 'stream_id', 'source_crate', 'slot_chan', 'subframe', 'ts'],
            offsets=[0, 1, 1, 2, 3, 2],
            formats=['u1', '>u2', 'u1', 'u1', 'u1', '>u8']))

        self.packet_dtype = np.dtype([
            ('header', self.header_dtype, (1, )),
            ('data', np.int8, (self.BYTES_PER_SUBFRAME, ))])

        self.PACKET_SIZE = self.packet_dtype.itemsize

        # Pre-allocate buffers
        # Buffer in which recv_into() will put the data directly
        # self.buf = np.zeros((self.NPACKETS, self.PACKET_SIZE), dtype=np.int8)  # can use empty(), but the uninitialized data can be confusing for debugging
        self.n = 0  # buffer write index
        self.last_ts = None  # timestamp of the last packet written in the buffer

        # Various views of the buffer to allow quick and easy access to the packet contents

        self.buf_struct = self.pr.buf[:,:self.PACKET_SIZE].view(self.packet_dtype)[:,0]

        self.buf_cookie = self.buf_struct['header']['cookie'][:, 0]
        self.buf_frame_stream_id = self.buf_struct['header']['stream_id'][:, 0]
        # self.buf_source_crate = self.buf_struct['header']['source_crate'][:, 0]
        self.buf_slot_chan = self.buf_struct['header']['slot_chan'][:, 0]
        self.buf_subframe = self.buf_struct['header']['subframe'][:, 0]
        self.buf_ts = self.buf_struct['header']['ts'][:, 0]
        self.buf_ts_dirty = self.buf_struct['header']['ts'][:, 0]
        self.buf_data = self.buf_struct['data']
        self.ts_mask = np.uint64(0xFFFFFFFFFFFF)  # just keep the last 48 bits
        self.ts_map = {}  # Maps timestamp to storage slot
        NPACKETS = self.pr.buf.shape[0]
        self.buf_stream_id = np.empty((NPACKETS,), dtype=np.int32)
        self.buf_frame = np.empty((NPACKETS,), dtype=np.int32)
        self.TS_SLOTS = 3
        ts_dict = {} # {timestamp:n, ...): keeps track of the known timestamps and associated capture slots


        self.update_config(config)

    def update_config(self, config, force=False):
        """ Update the correlator receiver configuration


        `update_config` needs to be called at least once before we start processing packets. It will
        allocate processing buffers based on the correlator configuration. There is some logic to
        reallocate these buffers only if dependent parameters have changed.

        Parameters:

            config (dict): dict containing the configuration of the correlator. Any of the following
                keys can be provided in an update_config call, and multiple calls can be made, but all of them must have been
                provided before data is being processed.

                - NCHAN (int): Number of input channels that are beiing correlated together. Sets the number of products.
                - NCLK (int): Number of clock cycles used to compute one set of products. This sets how many packets are set by bin.
                - bin_map (numpy.ndarray or list): Array of dimensions [NBINSET, NCORR, NPROD_PER_CMAC, NCMAC] that
                  describe the bin numbers for a specific bin set, correlator, bin/clock (packet index for each correlator)
                  and CMAC (complex number index within a packet). The array can be provided as a numpy ndarray or a series of
                  nested lists that convert to the desired 4-dimensional array so it can be sent as a serialized object.
                - prod_map (numpy.ndarray or list): Array of dimensions [NCORR, NPROD_PER_CMAC, NCMAC] that
                  describe the normalized product index for data from a specific correlator, bin/clock (packet index for each correlator)
                  and CMAC (complex number index within a packet). The array can be provided as a numpy ndarray or a series of
                  nested lists that convert to the desired 4-dimensional array so it can be sent as a serialized object.
                - firmware_integration (int): Number of frames integrated in firmware.

        Notes:

            -

        """

        def set_param(name):
            if (v := config.get(name)) is not None:
                if force or (np.all(v != getattr(self, name)) if isinstance(v, np.ndarray) else v != getattr(self, name)):
                    setattr(self, name, v)
                    return True
            return False

        if config is None:
            return

        stream_ids_updated = set_param('stream_ids')
        self.format = '16'
        self.target_mode = 0
        self.frames_per_burst = 2 << self.target_mode  # for CRS


        if stream_ids_updated:
            self.stream_ids = stream_ids_updated
            self.nchan = len(self.stream_ids)  # number of stream IDs (channels) to capture
            self.six_map = np.full((1<<12, ), -1, dtype=np.int32) # maps a stream ID into a stream index, default of -1 means invalid stream ID
            self.six_map[self.stream_ids] = np.arange(self.nchan)
            self.ts_list = np.zeros((self.nchan,), dtype=np.uint64)  # timestamp for each storage slot

            self.raw_data = np.zeros((self.TS_SLOTS, self.nchan, self.BYTES_PER_FRAME * self.frames_per_burst), dtype=np.int8)
            self.data_count = np.zeros((self.TS_SLOTS, self.nchan), dtype=np.uint16)



    def get_data_iter(
            self,
            n,
            format='8',
            split = False,
            config = None,
            verbose=0
            ):
        """
        Returns a generator that yields the raw data resulting from processing `n` lines in the packet buffer.

        Parameters:

            n (int): Number of entries (packets) to process in the buffer


            skip_first_integ (bool): if True, the first integration will be dropped if it is missing any data.

            skip_partial_integs (bool): If True, any integration that is missing data will be dropped


            select (bool): If True, the firmware will configure to stream the correlator data if needed.

            out (tuple): (data, data_counts, data_sats)  tuple providing the ndarrays into which data will be stored.
                if None, the results will be yielded as they come.

            verbose (int): verbosity level. 0=Quiet, 1=basic info, 2=detailed info, 3 = debugging info
        """
        self.verbose = verbose

        if not self.config_ready():
            raise RuntimeError('Configuration parameters are missing')


        if config is not None:
            self.update_config(config)

        self.skip_partial_integs = skip_partial_integs

        if not n:
            return


        # self.expected_packets_per_ts = self.nchan * 16
        # self.skip_next_integ = skip_first_integ
        # self.n = 0 # number of processed packets

        # Select the buffer index that have the right cookie
        (bix, ) = np.where(self.buf_cookie[:npkts] & self.PACKET_COOKIE_MASK == self.PACKET_COOKIE)
        if not bix.size:
            return

        # Of those, select the buffer indices that have valid stream IDs and valid timestamps
        self.ts[bix] = self.buf_ts[bix] & self.ts_mask
        self.stream_id[bix] = self.buf_frame_stream_id[bix] & 0x0FFF
        self.mode[bix] = (self.buf_subframe[bix] >> 2) & 0x3
        min_ts = min(self.ts_list)
        bix = buf_ix[(self.six_map[self.buf_stream_id[bix]] >= 0) &
                     (self.ts[bix] >= min_ts) &
                     (self.mode == self.target_mode)
                     ] # just keep bin indices that have valid stream IDs and valid timestamps
        if not bix.size:
            return

        # Extract header information
        self.frame[bix] = (self.buf_frame_stream_id[bix] >> 12) & 0xF
        self.subframe[bix] = self.buf_subframe[bix] & 0x3
        self.six[bix] = self.six_map[self.stream_ids[bix]]  # get stream index from stream id


        # Process data by timestamp groups
        for ts in np.unique(ts):
            if ts not in self.ts_list: # if timestamp is not in the list, add it
                dest_slot = (self.write_slot + 1) % self.TS_SLOTS
                if dest_slot == self.read_slot: # FIFO is full, send data to make room
                    yield from self.yield_ts()
            else: # timestamp already exist, add new data to it.
                dest_slot = self.ts_list.index(ts)

            tix = bix[self.ts[bix] == ts] # select buf indices with matching timestamps
            x = self.frame[tix] * self.BYTES_PER_FRAME + self.subframe[tix] * self.BYTES_PER_SUBFRAME
            self.raw_data[dest_slot, self.six[tix], x:x + self.BYTES_PER_SUBFRAME] = self.buf_data[tix]
            self.ts_list[dest_slot] = ts
            self.raw_data_count[dest_slot] += len(tix)
            # self.raw_mode[dest_slot] = self.mode[tix[0]]
            self.raw_data_complete[dest_slot] = self.raw_data_count[dest_slot] == self.nchan * self.frames_per_burst * self.NUMBER_OF_SUBFRAMES

            # Immediately send data if we have full sets
            for i in range(self.TS_SLOTS):
                if any(self.raw_data_complete):
                    yield from self.yield_data()

    def yield_ts(self, bix, ts):
        """
        """
        if self.write_slot == self.read_slot:  # if ring buffer is empty
            return
        rs = self.read_slot
        self.read_slot = (self.read_slot + 1) % self.TS_SLOTS
        if verbose:
            for i in range(self.nchan):
                print(f'Chan {i}: {data_count[i].sum()} packets')

        # if ncap > 1 and verbose:
        #     print(f'Timestamp differences: {set(np.diff(ts))}')

        self.frames_per_channel = 2 << self.raw_mode[rs]

        ts = self.raw_ts[rs]
        data = self.raw_data[rs]
        data_count = self.raw_data_count[rs]

        data = self.convert_data(data)

        yield ts, data, data_count




    # def clear_integ_slot(self, slice_=slice(None)):
    #     """ Clear the integration slot(s)
    #     Parameters:
    #         slice_ (slice): Slice object indicating which slots to clear. If not specified, all slots are cleared.

    #     """
    #     self.total_count[slice_] = 0
    #     self.acc_re[slice_] = 0
    #     self.acc_im[slice_] = 0
    #     self.sat[slice_] = 0
    #     # self.count[slice_] = 0

    def convert_data_set(self, data):
        split = 0

        frames_per_channel = data.shape[-1]//self.BYTES_PER_FRAME
        ncap = data.shape[0] if len(data.shape) > 2 else 1

        if self.format == '8':
            if split:
                data = data.reshape((self.nchan, ncap*frames_per_channel,-1))
        elif self.format == "14":
            data = data.view('>i2') >> 2
            if split:
                data = data.reshape((self.nchan, ncap*frames_per_channel,-1))
        elif self.format == "16":
            data = self.raw_data.view('>i2')
            if split:
                data = data.reshape((self.nchan, ncap*frames_per_channel,-1))
        elif self.format == "32":
            data = data.view('>i4')
            if split:
                data = data.reshape((self.nchan, ncap*frames_per_channel,-1))

        elif self.format == "16+16":
            data = data.view('>i2')
            data = data[:, :, ::2] + 1j*data[:, :, 1::2]
            data = data.reshape((self.nchan, ncap*frames_per_channel,-1))
        elif self.format == "32+32":
            data = data.view('>i4')
            data = data[:, :, ::2] + 1j*data[:, :, 1::2]
            data = data.reshape((self.nchan, ncap*frames_per_channel,-1))
        elif self.format == "32+32d":
            data = data.view('>i4')
            data = data[:, :, ::2] + 1j*data[:, :, 1::2]
            data = data.reshape((self.nchan, ncap*frames_per_channel,-1)) # split
            s = data.shape
            data = data.reshape(s[0],-1,2,s[2]).reshape(s[0],-1,s[2]*2, order='F')

        elif self.format == "64+64":
            data = data.view('>i8')
            data = data[:, :, ::2] + 1j*data[:, :, 1::2]
            data = data.reshape((self.nchan, ncap*frames_per_channel,-1))
        else:
            raise ValueError(f'Invalid format "{self.format}"')

        return data

    def create_data_set(self, n):
            # Allocate memory to store reordered data
            data_sats = np.empty((n, self.NBINS, self.NPROD), dtype=np.complex64)
            data_counts = np.empty((n,), dtype=np.int32)
            nsamples = self.SAMPLES_PER_FRAME * self.frames_per_burst
            # data_counts = np.empty((n, self.NBINS, self.NPROD), dtype=np.int32)
            formats = {
                '8': (1, '>i1'),
                '14': (2, '>i2'),
                '16': (2, '>i2'),
                '32': (4, '>i4'),
                '64': (8, '>i8'),
                '16+16': (4, np.complex64),
                '32+32': (8, np.complex64),
                '64+64': (16, np.complex128),
            }

            nbytes,dtype = formats[self.format]
            nvalues = self.SAMPLES_PER_FRAME * self.frames_per_burst * self.BYTES_PER_SAMPLE // nbytes
            data = np.empty((self.nchan, n, nvalues), dtype=dtype)

            return (data, data_counts, data_sats)



class RawFrameReceiver(RawDataProcessor):
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

    def __init__(self, packet_receiver, config=None):

        self.pr = packet_receiver
        super().__init__(buf=self.pr.buf, config=config)

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

    def read_corr_data_iter(
            self,
            number_of_results = None,
            soft_integ_period=1,
            flush=None,
            align=False,
            data_timeout=0.1,
            flush_timeout=0.001,
            skip_partial_integs=True,
            config = None,
            verbose=0):
        """ Generator that continuously retreives UDP packets and yields processed correlator data when they are ready.

        Parameters:


            number_of_results (int): Number of results to yields back. If None or zero, the generator never ends.

            soft_integ_period (int): Number of correlator frames to
                accumulate in software. A software frame will always be
                aligned to a multiple of soft_integ_period.


            flush (bool): if True, the UDP buffers will be emptied before data is captured. For
                multiple acquisitions, set to True on the first call and False on the subsequent
                ones to allow lossless  acquisition.

            align (bool): if True, a partial first integration will be dropped. Align is automatically set if Flush is set.

            data_timeout (float): Maximum amount of time to wait for data before starting
                processing. Should be matched to a dead time between data set transmission to be
                effective. If too large, the data will be processed only when the buffer is full.

            flush_timeout (float): Maximum amount of time to wait for packets before deciding there
                is no more data in the buffer. Must be smaller than the gap between data packets,
                otherwise it will never stop flushing.



            out (tuple): (data, data_counts, data_sats)  tuple providing the ndarrays into which data will be stored.
                if None, the results will be yielded as they come.

            verbose (int): verbosity level. 0=Quiet, 1=basic info, 2=detailed info, 3 = debugging info
        """
        # self.verbose = verbose
        # self.soft_integ_period = soft_integ_period,
        # self.skip_partial_integs = skip_partial_integs,

        if self.pre_func:
            self.pre_func(self)

        # Flush the UDP buffer by reading data until we timeout. We assume
        # here that we can read the data fast enough to empty the buffer and
        # that no new will come  for the timeout period.
        if flush:
            self.pr.flush(flush_timeout)
            self.clear_integ_slot() # clear ALL integration slots
            align = True
            self.current_integ = None

        self.pr.settimeout(data_timeout)
        n = 0  # number of results sent
        while True:
            # Get some packets until a new timestamp, timeout, or buffer full
            if not (npkts := self.pr.get_packets(verbose=verbose)):
                continue
            # Extract and yield all possible data from the buffer
            for d,c,s in self.get_data_iter(n=npkts, skip_first_integ=align, soft_integ_period = soft_integ_period, skip_partial_integs = skip_partial_integs, verbose=verbose):
                yield d,c,s
                n += 1
                if n >= number_of_results:
                    return
            align = False

    def read_raw_frames(self, ncap=1, **kwargs):
        """ Allocate a data set and capture correlator data into it.

        Parameters:


            soft_integ_period (int): Number of correlator frames to
                accumulate in software. A software frame will always be
                aligned to a multiple of soft_integ_period.


            ncap (int): Number of software-integrated frames to
                acquire and return.


            flush (bool): if True, the UDP buffers will be emptied before data is captured. For
                multiple acquisitions, set to True on the first call and False on the subsequent
                ones to allow lossless  acquisition.

            data_timeout (float): Maximum amount of time to wait for data before starting
                processing. Should be matched to a dead time between data set transmission to be
                effective. If too large, the data will be processed only when the buffer is full.

            flush_timeout (float): Maximum amount of time to wait for packets before deciding there
                is no more data in the buffer. Must be smaller than the gap between data packets,
                otherwise it will never stop flushing.

            verbose (int): verbosity level. 0=Quiet, 1=basic info, 2=detailed info, 3 = debugging info
        """

        # Pre-Allocate memory to store processed data
        data, counts, sats = self.create_data_set(ncap)
        for i, (d,c,s) in enumerate(self.read_corr_data_iter(ncap=ncap, **kwargs)):
            data[i], counts[i], sats[i] = d, c, s
        return (data, counts, sats)

