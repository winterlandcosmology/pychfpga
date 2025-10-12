"""
CORR.py module
 Implements interface to the correlator blocks

 History:
 2017-05-04 : JFC : Created
"""
import time
import logging
import numpy as np
import matplotlib.pyplot as plt
import socket
import select

from ..mmi import MMI, BitField, CONTROL, STATUS

#############################################
# Basic Geometry of the firmware correlator
#############################################

# Driving parameters
# NCHAN = 8  # Number of channels on which to generate N-squared products
NBINS_TOTAL = 8192  # Number of frequency bins to process
NBYTES_PER_PROD = 5


class UCORR(MMI):
    """ Implements interface the UCORR44_ARRAY correlator array"""

    ADDRESS_WIDTH = 16

    # Control registers
    SOFT_RESET         = BitField(CONTROL, 0x00, 7, doc="Resets the correlator array.")
    OVERRUN_RESET      = BitField(CONTROL, 0x00, 6, doc="Resets the overrun flag")
    CORR_ID            = BitField(CONTROL, 0x00, 2, width=4, doc="Correlator ID, placed in th e MSB of the stream ID")
    NO_ACCUM           = BitField(CONTROL, 0x00, 1, doc="Don't accumulate values - only last product of period is kept")
    AUTOCORR_ONLY      = BitField(CONTROL, 0x00, 0, doc="Force the correlator to output autocorrelations only")
    # NO_ACCUM           = BitField(CONTROL, 0x00, 5, doc="Disables accumulation - only the last result is saved")
    # USER_ID            = BitField(CONTROL, 0x00, 0, width=4, doc="USER ID used in the correlator packet header")
    INTEGRATION_PERIOD = BitField(CONTROL, 0x02, 0, width=16, doc="Duration of te integration period minus one")
    # BINS_PER_FRAME_OLD = BitField(CONTROL, 0x05, 0, width=7, doc="Number of frequency bins per frame minus one")
    # BINS_PER_FRAME     = BitField(CONTROL, 0x06, 0, width=9, doc="Number of frequency bins per frame minus one")

    # Status registers
    OVERRUN   = BitField(STATUS, 0, 0, doc="An overrun has occured in one of the correlator cores")
    RST_STATUS   = BitField(STATUS, 0, 1, doc="reset line status")
    NCHAN = BitField(STATUS, 1, 0, width=8, doc="NUmber of input channels to correlate")
    NCORR   = BitField(STATUS, 2, 4, width=4, doc="Number of correlator cores")
    BIN_DECIMATION_FACTOR   = BitField(STATUS, 2, 0, width=4, doc="Bin decimation factor")
    IN_FRAME_CTR  = BitField(STATUS, 3, 0, width=8, doc="Input frame counter")
    OUT_FRAME_CTR = BitField(STATUS, 4, 0, width=8, doc="Output frame counter")

    def __init__(self, *, router, router_port, verbose=0):
        super().__init__(router=router, router_port=router_port)
        self.verbose = verbose
        self.logger = logging.getLogger(__name__)

    def init(self):
        """ Inisializes all modules of a correlator block."""
        # self.INTEGRATION_PERIOD = 16384-1
        # self.USER_ID = self.fpga.slot - 1 if self.fpga.slot else 0
        self.CORR_ID = (self.fpga.slot-1) if self.fpga.slot else 0
        self.bins_per_frame = self.fpga.FRAME_LENGTH // 2 # total number of bins at the output of the channelizers

    def status(self):
        """Displays the status of the correlator array"""
        print('======= CORR.core[%i] =============' % self.instance_number)
        print(f' Don\'t accumulate: {self.NO_ACCUM}')
        print(f' Autocorrelation only: {self.AUTOCORR_ONLY}')
        print(f' Firmware integration period: {self.INTEGRATION_PERIOD} frames')
        print(f' Overrun in one of the cores: {self.OVERRUN}')

    def get_params(self):
            return Namespace(
                number_of_correlators=self.fpga.NUMBER_OF_CORRELATORS,  # hard coded in firmware
                number_of_correlated_inputs=self.fpga.NUMBER_OF_INPUTS_TO_CORRELATE,  # hard coded in firmware
                number_of_bins_per_frame=self.fpga.FRAME_LENGTH // 2  # CT engine always sends all bins in corr8
                )
    def get_input_bin_map(self):
        return self.fpga.chan[0].FFT.get_bin_map()

    def select_stream(self):
        """ Configures UCAP to stream the correlator data instead of raw data.
        """
        self.fpga.UCAP.OUTPUT_SOURCE_SEL = 1

    def start_correlator(self,
                         integration_period,
                         autocorr_only=False,
                         no_accum=False,
                         correlators=None,
                         bandwidth_limit=None,
                         verbose=0
                         ):
        self.logger.info(f' Starting correlator')
        self.INTEGRATION_PERIOD = integration_period-1
        self.AUTOCORR_ONLY = autocorr_only
        self.NO_ACCUM = no_accum
        if correlators is not None:
            raise RuntimeError("'correlators' argument is not supported by UCORR")
        if bandwidth_limit is not None:
            self.logger.warn(f"{self!r}: 'bandwidth_limit' argument is not yet supported by UCORR")
        self.verbose = verbose


    def get_data_receiver(self, sock, **kwargs):
        packet_receiver = UDPPacketReceiver(sock)
        return UCorrFrameReceiver(packet_receiver, correlators=(self,), **kwargs)

class UDPPacketReceiver:
    """ Receives and stores UDP packets until the buffer is full or a timout has occured.

        Parameters:

            sock (socket.socket): Socket to use for receiving UDP packets

            n_packets (int): Size of the packet buffer in number of packets

            max_packet_size (int): Maximum expected UDP payload length
    """

    def __init__(self, socket, n_packets=2048, max_packet_size=9000, verbose=1):

        self.socket = socket
        self.n_packets = n_packets
        self.max_packet_size = max_packet_size;
        self.verbose = verbose

        self.buf = np.zeros((n_packets, max_packet_size), dtype=np.uint8)
        self.pkt_len = np.zeros((n_packets,), dtype=np.uint32) # packet length. 0= unused slot.
        self.n = 0  # number of packets currently stored in the buffer

    def settimeout(self, timeout):
        """ Sets the timeout on the socket

        Parameter:

            timeout (float): timeout in seconds

        """
        self.socket.settimeout(timeout)

    def gettimeout(self):
        """ Returns the current timeout on the socket

        Returns:

            float: current timeout in seconds

        """
        return self.socket.gettimeout()

    def get_packets(self, verbose=0):
        """ Receive some packets into the buffer until there is a timeout or the buffer is full
        """
        verbose = max(verbose, self.verbose)
        # n = self.n # current packet number
        n = 0
        # last_ts = None
        while n < self.n_packets:
            try:
                s = self.pkt_len[n] = self.socket.recv_into(self.buf[n])
                # check if the packet has the right cookie
                if verbose >= 3:
                    print(f"get_packets: got raw packet {n:03d}, len={s}: {self.buf[n,:16].tobytes().hex(':')}")
                n += 1
            except socket.timeout:  # we have a timeout, so we probably have time to process data
                if not n:
                    continue
                if verbose >= 3:
                    print(f'get_packets: timeout')
                break
        # we get here if there is a timeout or if the buffer is full
        if verbose >= 2:
            print(f'get_packets: Returning {n} packets')
        self.n = n
        return n



    def flush(self, timeout=0.001, max_flush_time=2, verbose=0):
        """ Flush the UDP buffer (read packets until timeout).
        Note: Partial frame data may start to fill the UDP buffer while we start to flush it. There is still likely a partial frame in the buffer after this operation.

        """
        flushed_packets = 0
        t0 = time.time()
        # Read packets until timeout
        self.socket.settimeout(timeout)
        # self.socket.setblocking(1)
        if verbose >=2:
            print('Flushing UDP buffer')
        while time.time() - t0 < max_flush_time:  # give up after some time
            try:
                s = self.socket.recv_into(self.buf[0])
                flushed_packets += 1
                if verbose >=2:
                    print(f'flushing packet cookie=0x{self.buf_cookie[0]:02x} ts={self.buf_ts[0] & self.ts_mask}')

            except socket.timeout:
                break
        else:
            print(f'Stopped flushing after {max_flush_time} s: packets are arriving faster that the specified timeout period of {timeout} s')
        if verbose:
            print(f'Flushed {flushed_packets} packets')


class UCorrFrameReceiver:
    """
    Pure Python socket receiver to capture the data from the FPGA-based
    16-channel full N-square firmware correlator and integrates it in real
    time.

    Note that the packets are larger than 1500 bytes, which requires the
    networking equipment and the computer interface to be configured to
    receive Jumbo frames.

    The receiver is fast enough to capture data that is integrated in firmware
    down to a rate of about 10 ms/integrated frame, that is, exceeding 500
    Mbits/s, provided the system provides a sufficiently big UDP buffer to hold
    the data until the receiver method is called to process it (see below).

    The receiver can perform real-time software integration of the data. A
    specific number of software-integrated frames can be returned, or data can
    be saved to disk indefinitely until stopped.

    Parameters:

        socket (socket.socket): An opened and bound UDP socket to which the
            FPGA correlator data will be sent. The socket will not be closed
            when the call is completed.


    Data format
    -----------

    Complex value data results

    Each correlator frame has a (18+18) bit resolution, which is then
    integrated for some time. Assuming the worst case of a saturatet 1
    Gb/s link sending the maximum value if 2**17, we would get 175.8
    correlator frames/s,  with soft integrator values of increase by 2**24.46/s. If we integrate for
    we

    A float32 can represent integers values exactly up to 2**24, which
    leaves room for less than one second of integration in the worst
    case. We cannot thereofre use a complex64 value (float32+float32),
    and therefore use a complex128 format.


    System Requirements
    -------------------

    The transmit rate must be fast enough to accommodate the desired bandwidth
    by setting ib.GPIO.HOST_FRAME_READ_RATE = rate. rate=16 limits to about
    260 Mbps but is slow enough to allow python to process the data with a
    small standard UDP buffer. ``rate`` =15 is good for about 500 Mbps, and
    ``rate`` =16 is good for the full Gigabit bandwidth. The latetr two requireabs(vis[0, :, 0])
    bigger UDP buffers. See below::

        ib.GPIO.HOST_FRAME_READ_RATE = 14

    The Ethernet interface must be set to receive Jumbo frames::

        sudo ifconfig eno1 mtu 9000

    The UDP buffers shall be increased to reduce packet loss to a minimum::

        sudo sysctl -w net.core.rmem_max=262144000
        sudo sysctl -w net.core.rmem_default=262144000
        sudo sysctl -w net.ipv4.udp_mem='26214400 26214400 262144000'
        sudo sysctl -w net.ipv4.udp_rmem_min=262144000

    Check udp buffers::

        sysctl -a | grep mem

    Monitor UDP buffer::

        watch -cd -n .5 "cat  /proc/net/udp"
    """

    NBYTES_PER_HEADER = 10
    NBYTES_PER_PROD = 5


    def __init__(self, packet_receiver, correlators):

        if not isinstance(correlators, (tuple, list)):
            correlators = (correlators,)

        self.corrs = correlators

        # packets_per_chunk=10*NBINS_TOTAL,
        self.pr = packet_receiver
        self.NCHAN = NCHAN = self.get_corr_param('NCHAN')
        self.NDECIM = NDECIM = self.get_corr_param('BIN_DECIMATION_FACTOR')
        self.NBOARDS = NBOARDS = len(correlators)
        self.NBINS = Nbins = self.get_corr_param('bins_per_frame') # number of bins in the array
        # self.NPACKETS = packets_per_chunk
        self.NCORR_PER_BOARD = NCORR_PER_BOARD = self.get_corr_param('NCORR') # number of correlator per board, i.e. number of streams per board
        self.NPROD = NCHAN * (NCHAN + 1) // 2   # Total number of products per correlator frame
        self.NCLK = self.NDECIM  # number of clocks required to compute the products for one bin
        self.NCMAC = self.NPROD // self.NCLK # Number of complex multipliers/accumulators
        # self.NPROD = NCHAN   # Autocorrelation-only Total number of products per correlator frame
        self.NCORRS = self.NCORR_PER_BOARD * self.NBOARDS # total number of correlators in the array
        self.NBINS_PER_STREAM = self.NBINS // self.NCORRS
        self.NPROD_PER_CMAC = self.NBINS_PER_STREAM // self.NDECIM * self.NCLK

        self.PACKET_SIZE = self.NBYTES_PER_HEADER + self.NCMAC * self.NBYTES_PER_PROD # size of one packet (all products for a single bin plus header)

        # Define numpy data types that will be used to efficiently parse the data
        self.header_dtype = np.dtype(dict(
            names=['cookie', 'stream_id', 'flags', 'ts'],
            offsets=[0, 1, 3, 2],
            formats=['u1', '>u2', 'u1', '>u8']))

        self.product_dtype = np.dtype(dict(
            names=['sat', 'l', 'h'],
            offsets=[4, 0, 1],
            formats=['u1', '<i4', '<i4']))

        self.packet_dtype = np.dtype([
            ('header', self.header_dtype, (1, )),
            ('data', self.product_dtype, (self.NCMAC, ))])

        # Pre-allocate buffers in which recv_into() will put the received data
        # directly. We could use empty() to save some cycles, but the
        # uninitialized data can be confusing for debugging
        # self.buf = np.zeros((self.NPACKETS, self.PACKET_SIZE), dtype=np.uint8)

        # Various views of the buffer to allow quick and easy access to the
        # packet contents. This does not cause new memory allocations.
        self.buf_struct = self.pr.buf[:,:self.packet_dtype.itemsize].view(self.packet_dtype)[:, 0]  # (NPACKETS,)
        self.buf_cookie = self.buf_struct['header']['cookie'][:, 0]
        self.buf_flags = self.buf_struct['header']['flags'][:, 0]
        self.buf_stream_id = self.buf_struct['header']['stream_id'][:, 0]
        self.buf_ts = self.buf_struct['header']['ts'][:, 0]

        self.buf_data_h = self.buf_struct['data']['h']  # (NPACKETS, NPROD)
        self.buf_data_l = self.buf_struct['data']['l']  # (NPACKETS, NPROD)
        self.buf_data_sat = self.buf_struct['data']['sat']  # (NPACKETS, NPROD)
        self.ts_mask = np.uint64(0xFFFFFFFFFFFF)  # just keep the last 48 bits

        # Initialize variables used by the packet receiver
        # self.n = 0  # number of packets currently stored in the buffer
        self.last_ts = None  # timestamp of the last packet written in the buffer
        NPACKETS = self.pr.n_packets
        # Pre-allocate temporary storage to extract the real/imaginary part from the 5-byte packed product
        self.temp32 = np.empty((NPACKETS, self.NCMAC), dtype=np.int32)

        # Intermediate Storage slots for the accumulated value (a.k.a integration slots)
        self.N_SETS = 3 # Number of data sets (timestamps) to store
        self.current_set = None
        # self.ts = np.zeros((self.N_SETS,), dtype=np.uint64)
        # ts_dict = {} # {timestamp:n, ...): keeps track of the known timestamps and associated capture sets
        self.acc_re = np.zeros((self.N_SETS, self.NCORRS, self.NPROD_PER_CMAC, self.NCMAC), dtype=np.int64)
        self.acc_im = np.zeros((self.N_SETS, self.NCORRS, self.NPROD_PER_CMAC, self.NCMAC), dtype=np.int64)
        print(f'acc_re shape (N_results, Ncorr, Ncmac, Nprod) = {self.acc_re.shape}')
        # Number of saturations for the real and imaginary part of each product
        self.sat = np.zeros((self.N_SETS, self.NCORRS, self.NPROD_PER_CMAC, self.NCMAC, 2), dtype=np.int32)
        # Number of packets received for each NCMAC (and therefore each
        # product). Can be used to know how many packets were lost and to
        # normalize the data
        self.count = np.zeros((self.N_SETS, self.NCORRS, self.NPROD_PER_CMAC), dtype=np.uint32)
        self.total_count = np.zeros((self.N_SETS,), dtype=np.uint32)
        self.last_ts = None
        self.current_integ = None

        self.bin_map = self.corrs[0].get_input_bin_map()
        self.flush = True  # start first acquisition with UDP buffer flush

        # Storage for a single integrated result when streaming
        self.integ_sats = np.empty(( self.NBINS, self.NCMAC), dtype=np.complex64)
        self.integ_counts = 0
        self.integ_data = np.empty((self.NBINS, self.NCMAC), dtype=np.complex128)

    def get_corr_param(self,param):
        """ Return a correlator parameter, raising an arror if if is not the same for all correlators"""
        v = set(getattr(c,param) for c in self.corrs)
        if len(v) != 1:
            raise RuntimeError(f"Parameter'{param}' is not the same for all correlators")
        return v.pop()

    def get_freqs(self):
        return np.arange(self.NBINS)/self.NBINS/2*self.corrs[0].fpga._sampling_frequency

    def read_corr_gen(
            self,
            soft_integ_period=1,
            number_of_results=None,
            flush=None,
            align=False,
            data_timeout=0.1,
            flush_timeout=0.001,
            return_format='raw',
            skip_partial_integs=True,
            select = True,
            out = None,
            verbose=0):
        """

        Parameters:


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

            return_format (str): Only 'raw' is supported.


            select (bool): If True, the firmware will configure to stream the correlator data if needed.

            out (tuple): (data, data_counts, data_sats)  tuple providing the ndarrays into which data will be stored.
                if None, the results will be yielded as they come.

            verbose (int): verbosity level. 0=Quiet, 1=basic info, 2=detailed info, 3 = debugging info
        """

        self.verbose = verbose

        if return_format not in ('raw',):
            raise ValueError('Invalid return format "%s"' % return_format)

        if select:
            for c in self.corrs:
                c.select_stream()

        self.skip_partial_integs = skip_partial_integs
        self.soft_integ_period = soft_integ_period
        self.firmware_integ_period = self.get_corr_param('INTEGRATION_PERIOD') + 1
        # Compute the software integration period in number of frames. This will be used to determine the integration_number index in the destination matrix.
        # Note that we HAVE TO cast integ_period in an unsigned integer, otherwise any operations with other unsigned (e.g ts differences) will be promoted to a float64.
        integ_period = np.uint64(self.soft_integ_period * self.firmware_integ_period)

        if out: # if we provided destination arrays, use them
            self.stream = False
            self.out_data, self.out_counts, self.out_sats = out
            number_of_results = self.out_data.shape[0]
        else:
            self.stream = True

        self.overrun = 0

        self.pr.settimeout(data_timeout)

        if flush is not None:
            self.flush = flush

        # Flush the UDP buffer by reading data until we timeout. We assume
        # here that we can read the data fast enough to empty the buffer and
        # that no new will come  for the timeout period.
        if self.flush:
            self.pr.flush(flush_timeout)
            self.clear_integ_slot()
            align = True
            self.current_integ = None
            self.flush = False

        self.expected_packets_per_integ = self.NCORRS * self.NPROD_PER_CMAC * self.soft_integ_period

        self.skip_next_integ = align
        self.n = 0 # current result
        while True: # process packets until we have checked ncap+1 timestamps to make sure we have all the data for ncap timestamps
            # Get some packets until a new timestamp, timeout, or buffer full
            npkts = self.pr.get_packets(verbose=verbose)
            if not npkts:
                continue

            (buf_ix, ) = np.where(self.buf_cookie[:npkts] == 0xCF)

            if not buf_ix.size:
                continue

            ts = self.buf_ts[buf_ix] & self.ts_mask
            integ = ts // integ_period  # watch out! integ_period needs to be uint64 or the result will be "promoted" to float64 (signed OP unsigned=float)
            integ_min = int(integ.min()) # convert to int so we can do math with those and stay integer
            integ_max = int(integ.max())

            if self.current_integ is None:
                self.current_integ = integ_min

            if verbose >= 2:
                print(f'got {len(buf_ix)} corr packets. ts={set(ts)}/{integ_period}, integs={set(integ)} {ts.dtype}//{integ_period.dtype}')

            # slot = self.ts_dict.setdefault(integ, len(ts_dict))
            if integ_max - integ_min >= self.N_SETS-1:
                print(f'Received stale data from integration periods ({set(integ)}), but expected values between {self.current_integ}-{self.current_integ+self.N_SETS-1}. Ignoring data' )
                self.clear_integ_slot()
                self.skip_next_integ = True
                self.current_integ = None
                continue
            if integ_min < self.current_integ:
                raise RuntimeError('received timestamp before the current integration period.')

            if integ_max >= self.current_integ + self.N_SETS:
                # Flush integs that would come out of the integ window
                for i in range(self.N_SETS):
                    if integ_max >= self.current_integ + self.N_SETS:
                        yield from self.store_integ()
                # If we are still too far, reset the current_integ
                if integ_max  >= self.current_integ+ self.N_SETS:
                    self.current_integ = integ_max - self.N_SETS + 1;
            self.accumulate_data(buf_ix, integ % self.N_SETS)

            if verbose >= 2:
                print(f'Current_integ={self.current_integ}, Set counts: {self.total_count}. Expecting {self.expected_packets_per_integ} packets')
            # Flush integs as long as there are complete data sets
            while any(self.total_count >= self.expected_packets_per_integ) and not (number_of_results and self.n >= number_of_results):
                yield from self.store_integ()

            # exit the loop when we have all the results we need
            if number_of_results and self.n == number_of_results:
                break



        # print(f'Received {self.total_count} packets, {timeouts} timeouts')
        # packet_percent = packets/((self.NBINS-1) * self.soft_integ_period * number_of_results) * 100
        # print(f'Got {packet_percent:.1f}% of the packets')
        if self.overrun:
            print(f"*** Warning: the correlator experienced an overrun condition. All products could not be sent within the hardware integration period.")
        # print(f'stream={self.stream}. returning = {(self.out, self.out_counts, self.out_sats)}')
        yield (self.out_data, self.out_counts, self.out_sats) #, tt, self.bin_)

    def store_integ(self):
        """ Move the integrated results in the current integration slot into the destination array,
        clear the integration slot,  and increment both the destination and current integration
        counters.
        """
        cs = self.current_integ % self.N_SETS
        # print(f'{self.total_count[cs] == self.expected_packets_per_integ} {self.skip_partial_integs} {self.skip_next_integ}')
        yield_result = False
        if self.total_count[cs] == self.expected_packets_per_integ or not (self.skip_partial_integs or self.skip_next_integ):

            if self.verbose:
                print(f'Storing result {self.n}: integ={self.current_integ} ({type(self.current_integ)}), set={cs}, len={self.total_count[cs]}')
            # watch out: if you extract .real/.imag  *after* doing the advanced indexing, you will
            # store data to a copy
            if self.stream:
                self.integ_sats.real[self.bin_map] = self.sat[cs, ..., 0] / 32.
                self.integ_sats.imag[self.bin_map] = self.sat[cs, ..., 1] / 16.
                self.integ_data.real[self.bin_map] = self.acc_re[cs]
                self.integ_data.imag[self.bin_map] = self.acc_im[cs]
                self.integ_counts = self.total_count[cs]
                yield_result = True
            else:
                n = self.n
                self.out_sats.real[n,self.bin_map] = self.sat[cs, ..., 0] / 32.
                self.out_sats.imag[n,self.bin_map] = self.sat[cs, ..., 1] / 16.
                self.out_data.real[n, self.bin_map] = self.acc_re[cs]
                self.out_data.imag[n, self.bin_map] = self.acc_im[cs]
                self.out_counts[n] = self.total_count[cs]
            self.n += 1

        else:
            if self.verbose:
                print(f'Discarding result {self.n}: integ={self.current_integ} ({type(self.current_integ)}), set={cs}, len={self.total_count[cs]}')

        # Clear the integration slot
        self.clear_integ_slot(cs)
        self.current_integ += 1
        self.skip_next_integ = False
        if yield_result:
            yield (self.integ_data, self.integ_counts, self.integ_sats)

    def clear_integ_slot(self, slice_=slice(None)):
        self.total_count[slice_] = 0
        self.acc_re[slice_] = 0
        self.acc_im[slice_] = 0
        self.sat[slice_] = 0
        self.count[slice_] = 0

    def accumulate_data(self, ix, slots):
        """ Add the data from the packets at index `ix` into the corresponding integration slots `slots`.

        Parameters:

            ix (ndarray): indices of buffer entries containing valid data

            slots (ndarray): slot number to which each of the buffer entry should be added

        """
        t1 = time.time()

        # v = a.view(t)[:n, 0]
        # hh = h[:n]
        stream_id = self.buf_stream_id[ix] >> 11
        bin_ = self.buf_stream_id[ix] &  0b111_11111111
        integ = slots

        # ts = self.buf_ts[:n] & self.ts_mask

        if self.verbose >=2:
            print(f'Integ {set(integ)}')
            print(f'corr_id shape={stream_id.shape}, {stream_id.min()} - {stream_id.max()}')
            print(f'bin shape={bin_.shape}, {bin_.min()}-{bin_.max()}')
            print(f'{len(stream_id)} corr/bin IDs, Unique corr/bins IDs: {len(set(zip(stream_id, bin_)))}')


        # Extract the real part (in bits 27:10 of the data_h). We shift
        # left the MSB to bit 31 and sift the lsb back down to 0 to sign
        # extend the 18-bit value result within the 32-bit word.
        np.copyto(self.temp32, self.buf_data_h)
        np.left_shift(self.temp32, 4, self.temp32)
        np.right_shift(self.temp32, 14, self.temp32)
        # Add the sign-extended value to the 64-bit accumulator.
        # print(f'bin_ = {corr.shape}')
        # print(f'cmac shape = {cmac.shape}')

        self.acc_re[integ, stream_id, bin_] += self.temp32[ix]

        # Extract the imag part (in bits 17:0 of the data_l).
        np.copyto(self.temp32, self.buf_data_l)
        np.left_shift(self.temp32, 14, self.temp32)
        np.right_shift(self.temp32, 14, self.temp32)
        # Add the sign-extended value to the 64-bit accumulator.
        self.acc_im[integ, stream_id, bin_] += self.temp32[ix]

        t2 = time.time()

        # Keep track of how many packets were received for each correlator/cmac
        np.add.at(self.total_count, integ, 1)
        self.count[integ, stream_id, bin_] += 1
        # self.ts[integ_number, corr, cmac]
        # Accumulate the flags for each product by or'ing them together
        # We'll mask those later to save time
        self.sat[integ, stream_id, bin_, :, 0] += self.buf_data_sat[ix] & 0x20
        self.sat[integ, stream_id, bin_, :, 1] += self.buf_data_sat[ix] & 0x10

        t3 = time.time()
        self.overrun |= any(self.buf_flags[ix] & 1);
        if self.verbose >=2:
            # print 'count=', count[0,0]
            dt1 = t2 - t1
            dt2 = t3 - t2
            dt = t3 - t1
            print(f'Processing & accumulating {len(ix)} packets '
                  f'for software integ(s) # [{set(integ)}] '
                  # f'from correlator frame {ts} '
                  # f'({(ts - self.first_ts)% self.soft_integ_period}/{self.soft_integ_period};  '
                  # f'took {dt * 1000:.3f} ms ({(float(dt) / (self.NBINS) * 1000):.3f} ms/corr frame) '
                  # f'({dt1 * 1000:.3f} + {dt2 * 1000:.3f} ms) '
                  f' overrun={self.overrun}')


    def create_data_set(self, n):
            # Allocate memory to store reordered data
            data_sats = np.empty((n, self.NBINS, self.NCMAC), dtype=np.complex64)
            data_counts = np.empty((n,), dtype=np.int32)
            data = np.empty((n, self.NBINS, self.NCMAC), dtype=np.complex128)
            return (data, data_counts, data_sats)

    def read_corr_frames(self, number_of_results=1, **kwargs):
        """ Allocate a data set and capture correlator data into it.

        Parameters:


            soft_integ_period (int): Number of correlator frames to
                accumulate in software. A software frame will always be
                aligned to a multiple of soft_integ_period.


            number_of_results (int): Number of software-integrated frames to
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

            return_format (str): Only 'raw' is supported.


            select (bool): If True, the firmware will configure to stream the correlator data if needed.

            verbose (int): verbosity level. 0=Quiet, 1=basic info, 2=detailed info, 3 = debugging info
        """

        # Pre-Allocate memory to store processed data
        data = self.create_data_set(number_of_results)
        return next(self.read_corr_gen(out=data, **kwargs))

    def plot_corr_frames(
            self,
            soft_integ_period=1,
            flush=True,
            align=False,
            data_timeout=0.001,
            flush_timeout=0.001
            ):

        # fig =  plt.figure()
        fig = plt.gcf()
        d, c, sat = self.read_corr_frames(soft_integ_period=soft_integ_period, flush=flush, align=align)
        p = plt.plot(arange(1024)/1024*400, d[0,:4,1,:256].real[...,::-1].flatten(order='F'))[0]

        while True:
            d, c, sat = r.read_corr_frames(soft_integ_period=soft_integ_period, flush=False, align=False)
            p.set_ydata( d[0,:4,1,:256].real[...,::-1].flatten(order='F'))
            fig.canvas.draw()
            fig.canvas.flush_events()
