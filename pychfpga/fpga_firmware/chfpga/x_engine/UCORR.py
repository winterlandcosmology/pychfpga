#!/usr/bin/python

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

from ..mmi import MMI, BitField

#############################################
# Basic Geometry of the firmware correlator
#############################################

# Driving parameters
NCHAN = 8  # Number of channels on which to generate N-squared products
NBINS_TOTAL = 8192  # Number of frequency bins to process
NBYTES_PER_PROD = 5


class UCORR(MMI):
    """ Implements interface to one of the correlator"""

    CONTROL = BitField.CONTROL
    STATUS = BitField.STATUS

    # Control registers
    SOFT_RESET         = BitField(CONTROL, 0x00, 7, doc="Resets the correlator array.")
    NO_ACCUM           = BitField(CONTROL, 0x00, 1, doc="Don't accumulate values - only last product of period is kept")
    AUTOCORR_ONLY      = BitField(CONTROL, 0x00, 0, doc="Force the correlator to output autocorrelations only")
    # NO_ACCUM           = BitField(CONTROL, 0x00, 5, doc="Disables accumulation - only the last result is saved")
    # USER_ID            = BitField(CONTROL, 0x00, 0, width=4, doc="USER ID used in the correlator packet header")
    INTEGRATION_PERIOD = BitField(CONTROL, 0x02, 0, width=16, doc="Duration of te integration period minus one")
    # BINS_PER_FRAME_OLD = BitField(CONTROL, 0x05, 0, width=7, doc="Number of frequency bins per frame minus one")
    # BINS_PER_FRAME     = BitField(CONTROL, 0x06, 0, width=9, doc="Number of frequency bins per frame minus one")

    # Status registers
    OVERRUN   = BitField(STATUS, 0x00, 0, doc="An overrun has occured in one of the correlator cores")
    # IN_FRAME_CTR  = BitField(STATUS, 0x01, 0, width=8, doc="Input frame counter")
    # OUT_FRAME_CTR = BitField(STATUS, 0x02, 0, width=8, doc="Output frame counter")

    def __init__(self, fpga_instance, base_address, instance_number, verbose=0):
        super().__init__(fpga_instance, base_address, instance_number)
        self.verbose = verbose
        self.logger = logging.getLogger(__name__)

    def init(self):
        """ Inisializes all modules of a correlator block."""
        # self.INTEGRATION_PERIOD = 16384-1
        # self.USER_ID = self.fpga.slot - 1 if self.fpga.slot else 0


    def status(self):
        """Displays the status of al the correlator blocks"""
        print('======= CORR.core[%i] =============' % self.instance_number)
        print(f' Don\'t accumulate: {self.NO_ACCUM}')
        print(f' Autocorrelation only: {self.AUTOCORR_ONLY}')
        print(f' Firmware integration period: {self.INTEGRATION_PERIOD} frames')
        print(f' Overrun in one of the register cores: {self.OVERRUN}')


    def start_correlator(self, *args, **kwargs):
        self.logger.info(f' Starting correlator')

    def get_data_receiver(self, sock, **kwargs):
        return UCorrFrameReceiver(sock, **kwargs)

class UCorrFrameReceiver(object):
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

    System Requirements:

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

    def __init__(self, socket, N=NCHAN, Nbins=NBINS_TOTAL, Ncorr=4, packets_per_chunk=10*NBINS_TOTAL, ignore_packet_size=False):

        NCORR = Ncorr

        self.NCHAN = N
        self.socket = socket
        self.NPACKETS = packets_per_chunk
        self.NCORR = NCORR
        self.NPROD = NCHAN * (NCHAN + 1) // 2  # Total number of products per correlator frame
        # self.NPROD = NCHAN   # Autocorrelation-only Total number of products per correlator frame
        self.NBINS = Nbins
        self.NBYTES_PER_HEADER = 10
        self.NBYTES_PER_PROD = 5
        self.PACKET_SIZE = self.NBYTES_PER_HEADER + self.NPROD * self.NBYTES_PER_PROD # size of one packet (all products for a single bin plus header)
        # self.NPACKETS = NBINS # Number of packets per frame: This correlator sends one packet per bin
        self.ignore_packet_size = ignore_packet_size
        # Define numpy data types that will be used to efficiently parse the data
        # self.product_dtype = np.dtype(dict(
        #     names=['sat', 'l', 'h'],
        #     offsets=[0, 0, 1],
        #     formats=['u1', '>i4', '>i4']))
        self.product_dtype = np.dtype(dict(
            names=['sat', 'l', 'h'],
            offsets=[4, 0, 1],
            formats=['u1', '<i4', '<i4']))

        self.header_dtype = np.dtype(dict(
            names=['cookie', 'stream_id', 'flags', 'ts'],
            offsets=[0, 1, 3, 2],
            formats=['u1', '>u2', 'u1', '>u8']))

        self.packet_dtype = np.dtype([
            ('header', self.header_dtype, (1, )),
            ('data', self.product_dtype, (self.NPROD, ))])

        # Pre-allocate buffers in which recv_into() will put the received data
        # directly. We could use empty() to save some cycles, but the
        # uninitialized data can be confusing for debugging
        self.buf = np.zeros((self.NPACKETS, self.PACKET_SIZE), dtype=np.uint8)

        # Various views of the buffer to allow quick and easy access to the
        # packet contents. This does not cause new memory allocations.
        self.buf_struct = self.buf.view(self.packet_dtype)[:, 0]  # (NPACKETS,)
        self.buf_cookie = self.buf_struct['header']['cookie'][:, 0]
        self.buf_flags = self.buf_struct['header']['flags'][:, 0]
        self.buf_stream_id = self.buf_struct['header']['stream_id'][:, 0]
        self.buf_ts = self.buf_struct['header']['ts'][:, 0]

        self.buf_data_h = self.buf_struct['data']['h']  # (NPACKETS, NPROD)
        self.buf_data_l = self.buf_struct['data']['l']  # (NPACKETS, NPROD)
        self.buf_data_sat = self.buf_struct['data']['sat']  # (NPACKETS, NPROD)
        self.ts_mask = np.uint64(0xFFFFFFFFFFFF)  # just keep the last 48 bits

        # Initialize variables used by the packet receiver
        self.n = 0  # number of packets currently stored in the buffer
        self.last_ts = None  # timestamp of the last packet written in the buffer

        # Pre-allocate temporary storage to extract the real/imaginary part from the 5-byte packed product
        self.temp32 = np.empty((self.NPACKETS, self.NPROD), dtype=np.int32)

        # Pre-compute the remapping vectors that will be used to convert the
        # raw integrated results (Nresults, NCORR, NPROD) into more palatable
        # arrays
        # self.raw_to_matrix_map, self.raw_to_vector_map = get_raw_corr_map(N=N, Nbins=Nbins, Ncorr=Ncorr)
        self.firmware_integ_period = 16384  # ToDo: fetch value automatically or get it via param

    def flush(self, timeout=0.001, max_flush_time=2, verbose=0):
        """ Flush the UDP buffer (read packets until timeout).
        Note: Partial frame data may start to fill the UDP buffer while we start to flush it. There is still likely a partial frame in the buffer after this operation.

        """
        flushed_packets = 0
        t0 = time.time()
        # Read packets until timeout
        self.socket.settimeout(timeout)
        # self.socket.setblocking(1)
        print('Flushing UDP buffer')
        while time.time()-t0 < max_flush_time:  # give up after some time
            try:
                s = self.socket.recv_into(self.buf[0])
                flushed_packets += 1
                if verbose:
                    print(f'flushing packet cookie=0x{self.buf_cookie[0]:02x} ts={self.buf_ts[0] & self.ts_mask}')

            except socket.timeout:
                break
        else:
            print(f'Stopped flushing after {max_flush_time} s: packets are arriving faster that the specified timeout period of {timeout} s')
        print(f'Flushed {flushed_packets} packets')

    def align(self, integ_period):
        """
        Flush packets until we receive the packet that is part of the first
        frame of the specified integration period.

        This first packet is left in the buffer.
        """
        # If the first packet in the buffer is already on an integration
        # boundary, we don't need to drop packets to align
        if self.n and not ((self.buf_ts[0] & self.ts_mask) % self.soft_integ_period):
            print("align: We're already aligned, no need to flush packets!")
            return

        print('Waiting for first frame of the specified integration period')
        while True:
            try:
                self.socket.recv_into(self.buf[0])
                if self.buf_cookie[0] != 0xcf:
                    continue
                ts = self.buf_ts[0] & self.ts_mask
                if ts != self.last_ts:  # we have a new timestamp
                    self.last_ts = ts
                    integ_index = ts % integ_period
                    if integ_index == 0:  # if the new frame is on an integration period
                        self.n = 1
                        break
                    print(f'   Discarding correlator timestamp {ts:012X} (integration index {integ_index}/{integ_period})')
            except socket.timeout:
                continue


    def read_corr_frames(
            self,
            soft_integ_period=1,
            number_of_results=1,
            filename=None,
            flush=True,
            align=True,
            data_timeout=0.1,
            flush_timeout=0.001,
            return_format='raw',
            verbose=0):
        """

        Parameters:

            number_of_results (int): Number of software-integrated frames to
                acquire and return. If a `filename` is specified, only the
                last frame is returned. Also only if `filename` is specified,
                a `number_of_results` =None will result in indefinite data
                capture until the capture is stopped.

            soft_integ_period (int): Number of correlator frames to
                accumulate in software. A software frame will always be
                aligned to a multiple of soft_integ_period.




        The receiver can do software integration for unlimited time at a firmware integration period of 5000 frames (12.8 ms).


        """
        # integration_period = self.CORR[0].INTEGRATION_PERIOD + 1
        # integration_time = 2.56e-6 * integration_period

        # average_data_rate = (NCORR * NCMAC * (42 + PACKET_SIZE) * 8) / integration_time
        # min_transmit_time = (NCORR * NCMAC * (42 + PACKET_SIZE) * 8) / 1e9
        # integ_time = max(integ_time, integration_time)
        # expected_chunks = int(integ_time * NCORR * NCMAC / integration_time / self.NPACKETS)
        # expected_packets = expected_chunks * self.NPACKETS
        # expected_corr_frames = expected_packets / (NCORR * NCMAC)

        # print 'Correlator is sending data at %.3f Gb/s, correlator frame period= %i channelizer frames = %.3f ms, minimum transmit time = %.3f' % (average_data_rate/1e9, integration_period, integration_time*1000, min_transmit_time*1000)
        # print 'We expect around %.1f packets and %.1f correlator frames in the requested integration period of %.3fs' % (expected_packets, expected_corr_frames, integ_time)

        if return_format not in ('raw', 'matrix', 'vector'):
            raise ValueError('Invalid return format "%s"' % return_format)

        self.soft_integ_period = soft_integ_period

        # Storage for the accumulated value
        self.acc_re = np.zeros((number_of_results, self.NBINS, self.NPROD), dtype=np.int64)
        self.acc_im = np.zeros((number_of_results, self.NBINS, self.NPROD), dtype=np.int64)
        print(f'acc_re shape (N_results, Ncorr, Ncmac, Nprod) = {self.acc_re.shape}')
        # Number of saturations for the real and imaginary part of each product
        self.sat = np.zeros((number_of_results, self.NBINS, self.NPROD, 2), dtype=np.int32)
        self.sat_cplx = np.zeros((number_of_results, self.NBINS, self.NPROD), dtype=np.complex64)
        # Number of packets received for each NCMAC (and therefore each
        # product). Can be used to know how many packets were lost and to
        # normalize the data
        self.count = np.zeros((number_of_results, self.NBINS), dtype=np.uint32)
        # self.ts = np.zeros((number_of_results, self.NBINS), dtype=np.uint64)
        self.overrun = 0
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
        # and thereofre use a complex128 format.
        self.data = np.zeros((number_of_results, self.NBINS, self.NPROD), dtype=np.complex128)

        # lists for debugging
        self.bin_ = [] # used for debugging missing bins
        tt = []

        # packets_per_chunk = corr_frames_per_chunk * NCORR * NCMAC

        # sock = self.get_data_socket()
        self.socket.settimeout(data_timeout)
        # chunks = 0
        timeouts = 0

        integ_period = self.soft_integ_period * self.firmware_integ_period

        # Flush the UDP buffer by reading data until we timeout. We assume
        # here that we can read the data fast enough to empty the buffer and
        # that no new will come  for the timeout period.
        if flush:
            self.flush(flush_timeout)

        # Make sure we have at least one packet in the buffer so we have a reference timestamp
        self.last_ts = None
        self.socket.settimeout(data_timeout)
        # if not self.n:

        # wait for a new timestamp to make sure we start at the beginning of a new firmware integration
        while True:
            try:
                s = self.socket.recv_into(self.buf[0])
                if self.buf_cookie[0] != 0xcf:
                    continue
                ts = self.buf_ts[0] & self.ts_mask

                if  ts != self.last_ts:
                    if self.last_ts is None:
                        self.last_ts = ts
                    else:
                        self.last_ts = ts
                        self.n = 1
                        break
                # print(f'Waiting for new frame')
            except socket.timeout:
                continue
        # get the timestamp
        # self.last_ts = self.buf_ts[self.n-1] & self.ts_mask

        self.first_ts = 0
        # Wait for a new timestamp that is the first of an integ period
        if align:
            self.align(integ_period)
        else:
            self.first_ts = self.last_ts

        current_integ = (self.last_ts - self.first_ts) // integ_period
        integ_number = 0
        packets = 0
        timeouts = 0
        bad_packets = 0
        print(f'Accumulating software frame #{current_integ}, '
              f'starting with correlator frame number {self.last_ts} ')
        while True:
            try:
                s = self.socket.recv_into(self.buf[self.n])
            except socket.timeout:
                timeouts += 1
                continue
            if self.buf_cookie[self.n] != 0xcf:
                continue

            # Ignore packets that don't have the right length
            if s != self.PACKET_SIZE and not self.ignore_packet_size:
                bad_packets += 1
                continue
            # size += s
            packets += 1
            ts = self.buf_ts[self.n] & self.ts_mask
            # If we start a new timestamp, process what was in the buffer (if
            # any) and make sure that the new sample is at the top of the
            # buffer.
            if ts != self.last_ts:
                if verbose > 1:
                    print(f'new timestamp {self.last_ts} => {ts}, n={self.n}')
                if self.n:
                    self.accumulate_data(self.n, integ_number, self.last_ts, verbose=verbose)
                    self.buf[0, :] = self.buf[self.n, :]
                self.n = 1
                self.last_ts = ts
                # If the timestamp change imply and integration period change,
                # increase the counter, and exit if we have all the
                # integration periods we wanted.
                integ = (ts - self.first_ts) // integ_period
                if verbose > 1:
                    print(f'integ number = {integ}, current_integ={current_integ}')
                # if the packet belongs to another integration period, update the integration ts and count
                if integ != current_integ:
                    integ_number += 1
                    current_integ = integ
                    if integ_number == number_of_results:
                        break
            # If this is the last entry in the buffer, process the data
            elif self.n == self.NPACKETS - 1:
                if verbose > 1:
                    print(f'Packet buffer full {self.n}')
                self.accumulate_data(self.n + 1, integ_number, self.last_ts, verbose=verbose)
                self.n = 0
            else:
                self.n += 1

            tt.append(ts)

            # packets += n
            # chunks += 1
            # packets_per_chunk += n
            # # z=zeros(h.shape,dtype=int64)
            # l=empty((1*8*34,512), dtype=np.int32)
            # h=a.view(t)[:,0]['data']['h'].copy();np.left_shift(h,4,h);np.right_shift(h,14,h);np.add(z,h,out=z)
        # print 'Got %i packets in %i chunks with %i timeouts total and %i data timeouts. Processing took on average %i packets/chunk at %.3f ms/chunk, %.1f bytes/packet' % (packets, chunks, timeouts, data_timeouts, float(self.NPACKETS)/chunks, (float(dt) / chunks) * 1000, float(size)/packets)
        print(f'Received {packets} packets, {timeouts} timeouts, {bad_packets} bad packets')
        packet_percent = packets/((self.NBINS-1) * self.soft_integ_period * number_of_results) * 100
        print(f'Got {packet_percent:.1f}% of the packets')
        if self.overrun:
            print(f"*** Warning: the correlator experienced an overrun condition. All products could not be sent within the hardware integration period.")
        # self.sat.real /= 32.
        # self.sat.imag /= 16.
        self.sat_cplx.real = self.sat[..., 0] / 32.
        self.sat_cplx.imag = self.sat[..., 1] / 16.
        self.data.real = self.acc_re
        self.data.imag = self.acc_im
        if return_format == 'raw':
            return (self.data, self.count, self.sat_cplx, tt, self.bin_)
        elif return_format == 'matrix':
            m = self.raw_to_matrix_map
            matrix = self.data[:, m[0], m[1], m[2]]
            # conjugate the lower triangle
            (i, j) = np.triu_indices(self.NCHAN)
            matrix[..., j, i] = matrix[..., i, j].conjugate()
            return (matrix,
                    self.count[:, m[0], m[1]],
                    self.sat_cplx[:, m[0], m[1], m[2]])
        elif return_format == 'vector':
            print('Returning vector format')
            m = self.raw_to_vector_map
            return (self.data[:, m[0], m[1], m[2]],
                    self.count[:, m[0], m[1]],
                    self.sat_cplx[:, m[0], m[1], m[2]])

    def accumulate_data(self, number_of_packets, integ_number, ts, verbose=1):
        """ Add the data from the packets 0 to `number_of_packets` in to the
        software accumulator array for software integration number
        `integ_number`.
        """
        t1 = time.time()

        # v = a.view(t)[:n, 0]
        # hh = h[:n]

        bin_ = self.buf_stream_id[:number_of_packets]
        # if len(bin_) != len(np.unique(bin_)):
            # print(bin_)
            # print(np.unique(bin_))
            # raise ValueError(f'Received {len(bin_)} frequency bins, but there are only {len(np.unique(bin_))} unique bins. Some bins are repeated.')
        self.bin_.append(bin_)
        # cmac = self.buf_cmac[:number_of_packets]
        # print 'corr=', corr
        # print 'cmac=', cmac

        # ts = self.buf_ts[:n]

        # Extract the real part (in bits 27:10 of the data_h). We shift
        # left the MSB to bit 31 and sift the lsb back down to 0 to sign
        # extend the 18-bit value result within the 32-bit word.
        np.copyto(self.temp32, self.buf_data_h)
        np.left_shift(self.temp32, 4, self.temp32)
        np.right_shift(self.temp32, 14, self.temp32)
        # Add the sign-extended value to the 64-bit accumulator.
        # print(f'bin = {corr.shape}')
        # print(f'cmac shape = {cmac.shape}')

        self.acc_re[integ_number, bin_] += self.temp32[:number_of_packets]

        # Extract the imag part (in bits 17:0 of the data_l).
        np.copyto(self.temp32, self.buf_data_l)
        np.left_shift(self.temp32, 14, self.temp32)
        np.right_shift(self.temp32, 14, self.temp32)
        # Add the sign-extended value to the 64-bit accumulator.
        self.acc_im[integ_number, bin_] += self.temp32[:number_of_packets]

        t2 = time.time()

        # Keep track of how many packets were received for each correlator/cmac
        self.count[integ_number, bin_] += 1
        # self.ts[integ_number, corr, cmac]
        # Accumulate the flags for each product by or'ing them together
        # We'll mask those later to save time
        self.sat[integ_number, bin_, :, 0] += self.buf_data_sat[:number_of_packets] & 0x20
        self.sat[integ_number, bin_, :, 1] += self.buf_data_sat[:number_of_packets] & 0x10

        t3 = time.time()
        self.overrun |= any(self.buf_flags & 1);
        if verbose:
            # print 'count=', count[0,0]
            dt1 = t2 - t1
            dt2 = t3 - t2
            dt = t3 - t1
            print(f'Processing & accumulating {number_of_packets} packets '
                  f'for software frame {integ_number} '
                  f'from correlator frame {ts} '
                  f'({(ts - self.first_ts)% self.soft_integ_period}/{self.soft_integ_period};  '
                  f'took {dt * 1000:.3f} ms ({(float(dt) / (self.NBINS) * 1000):.3f} ms/corr frame) '
                  f'({dt1 * 1000:.3f} + {dt2 * 1000:.3f} ms) '
                  f' overrun={self.overrun}')


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
