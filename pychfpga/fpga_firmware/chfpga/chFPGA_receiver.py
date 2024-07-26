#!/usr/bin/python

"""
chFPGA_receiver.py module
Implements the classes that read the data capture streams coming from chFPGA.


Notes:
    - For a modern correlator packet receiver, see CORR.py module.

    - A faster data capture engine is used in raw_acq.py.

History:
    2012-07-19 JFC: Created
    2012-10-17 JFC: Modified behavior of receiver for:

    a) If send_every_frame=False, discard the first block of frames after flush() avoid sending partial frame blocks
    b) if the Queue is full, automatically pop an element before pushing a new
       one. Old data will be automatically flushed over time.
"""

# Standard library packages

import queue
import threading
import struct
import time
import select

# PyPi packages

import numpy as np

# Local packages

from . import SocketIO


class ReceiverThread(threading.Thread):
    BUF_SIZE = 65536
    data = bytearray(BUF_SIZE)
    data_buf = memoryview(data)
    data_block = np.zeros((16, 2048 + 10), dtype=np.uint8)
    MAX_CORR_FRAME_LENGTH = 128 * 4 * 5 + 12  # 128 bins x 4 product/bin x 5 bytes/product + 12 header bytes

#        frame_block = {'timestamp' :0, 'data':frame_data}
    queue_overflow = 0
    queue_corr_overflow = 0
    n_frames = 0
    store_data = 0  # do not store frame blocks if False
    store_corr_data = 0  # do not store frame blocks if False

    def __init__(
            self,
            sock, queue,
            queue_corr,
            NUMBER_OF_ANTENNAS_TO_CORRELATE,
            NUMBER_OF_CORRELATORS,
            verbose=1):
        self.sock = sock
        self.queue = queue
        self.queue_corr = queue_corr
        self.Nch = NUMBER_OF_ANTENNAS_TO_CORRELATE  # Number of analog channels
        self.Ncorr = NUMBER_OF_CORRELATORS  # Number of correlator cores
        self.Ncmac = 2 * (self.Nch + 1)
        # Pre-allocate the frame assembly array
        self.corr_data_block = np.zeros((self.Ncmac * self.Ncorr, self.MAX_CORR_FRAME_LENGTH), dtype=np.uint8)
        self._stopping = threading.Event()  # changed from _stop which collided with Threading internals
        self._flushing = threading.Event()
        self.verbose = verbose
        self.print_delay = 1
        self._send_every_frame = threading.Event()
        super().__init__()

    def stop(self):
        self._stopping.set()

    # Currently the flush is unused...  Remove?
    def flush(self, state):
        if state:
            self._flushing.set()
        else:
            self._flushing.clear()

    def send_every_frame(self, state):
        if state:
            self._send_every_frame.set()
        else:
            self._send_every_frame.clear()

    def is_stopped(self):
        return self._stopping.is_set()

    def run(self):
        #    timeout=1
        #    frame_array=[]
        last_timestamp = 0
        last_corr_timestamp = 0
        last_corr_time = time.time()
        #    last_delta = 0
        n = 0
        nc = 0  # current number of correlator frames stored
        total_queue_entries = 0
        # t0 = time.time()
        # last_display_time = t0
        #            expected_delta=self.chan[0].PROBER.get_burst_period()
        #            missing_frames = 0
        #            bad_delta = 0
        #    data2 = bytearray(buf_size)
        self.sock.settimeout(0.1)
        # self.sock.setblocking(0)
        if self.verbose:
            print('Frame acquisition thread is running')
        while not self._stopping.is_set():
            # data = self.sock.read_data(timeout_delay=timeout)
            # Read data from the UDP listening port
            if self._flushing.is_set():
                try:
                    r1, w1, e1 = select.select([self.sock], [], [])
                    for e in r1:
                        if e == self.sock:
                            nbytes = self.sock.recv_into(self.data)
                except SocketIO.timeout:
                    pass
                self.store_data = 0  # do not store data
                self.store_corr_data = 0  # do not store data
                self.queue.queue.clear()
                self.queue_corr.queue.clear()
                continue
            try:
                r1, w1, e1 = select.select([self.sock], [], [])
                for e in r1:
                    if e == self.sock:
                        nbytes = self.sock.recv_into(self.data)
            except SocketIO.timeout:
                nbytes = 0
            if not nbytes:
                continue
            # print 'Received a frame!!!'
            self.n_frames += 1

            # probe_id = struct.unpack_from('>B', self.data_buf)
            frame_id = self.data[0]  # get the frame ID
            # Correlator input
            # ##### CORRELATOR DATA HANDLER ###########
            if (frame_id == 0xBF):  # If correlator data
                # print 'Got corr frame with frame id', frame_id
                # (_, _, corr_number, cmac_number, _,  timestamp) = struct.unpack_from('>BBBBLL', self.data_buf)
                corr_number = self.data[2] & 0x0F
                cmac_number = self.data[3] % 0xFF
                timestamp = (self.data[11] << 24) + (self.data[10] << 16) + (self.data[9] << 8) + self.data[8]
                corr_time = time.time()
                # Correlator unpack first try very simple.
                # corr_number &= 0x0F  # mask the FRAME ID bits
                if nbytes <= 12 or nbytes > self.MAX_CORR_FRAME_LENGTH:
                    if self.verbose:
                        print("Corr Receiver: Bad frame length of %i bytes" % nbytes)
                elif (cmac_number >= self.Ncmac):
                    if self.verbose:
                        print("Corr Receiver: Bad multiplier number")
                else:
                    # if this is the beginning of a new correlator data block
                    if ((timestamp != last_corr_timestamp) or (corr_time - last_corr_time > 2.9)) and (nc > 0):
                        # If the queue is full, make room by poping the oldest element
                        # if nc != self.Ncmac * self.Ncorr:
                        #     print 'Got only %i packets before a new timestanmp came in' % nc

                        # store_corr_data is False if this is the first block
                        # to be stored. In this case, do not store the data in
                        # case we got partial block after a flush()
                        if self.store_corr_data:
                            if self.queue_corr.full():
                                self.queue_corr.get()
                            # Now try to write the data into the Queue.
                            try:
                                self.queue_corr.put_nowait(self.corr_data_block[0:nc, :nbytes].copy())
                                # print 'Corr receiver: Pushing data to Queue with timestamp #%i (delta=%i), '
                                #      'dt=%0.3f, # frames = %i' % (
                                #           timestamp, timestamp - last_corr_timestamp, corr_time - last_corr_time, nc)
                            except queue.Full:
                                self.queue_corr_overflow += 1
                                print('Corr Receiver Queue overflow... Should not happen...')
                        else:
                            self.store_corr_data = 1  # next time store the block
                        nc = 0
                        last_corr_timestamp = timestamp
                        last_corr_time = corr_time
                    if nc >= self.Ncmac * self.Ncorr:
                        if self.verbose:
                            print('Corr Receiver: Received extra correlator frames for Corr#%i Mult#%i' % (
                                corr_number, cmac_number))
                    else:
                        # print 'Corr#%i Mult#%i ts=%i, time=%0.3f, dt=%0.3fs' % (
                        #       corr_number, cmac_number, timestamp, corr_time, corr_time-last_corr_time)
                        self.corr_data_block[nc, :nbytes] = self.data[:nbytes]
                        nc += 1

            # Timestream data handler
            elif (frame_id == 0xA0): # if timestream or spectrum data
                (probe_id,  stream_id, flags, ts_high, timestamp) = struct.unpack_from('>BHBHL', self.data_buf)
                timestamp += ts_high << 32
                # If we don't want to wait for all frames with the same timestanp to be grouped, put the frame immediately on the queue
                if self._send_every_frame.is_set():
                    self.data_block[0, :] = self.data[:2048 + 10]
                    # If the queue is full, make room by popping the oldest element
                    if self.queue.full():
                        self.queue.get()
                    # Now try to write the data into the Queue.
                    try:
                        self.queue.put_nowait((timestamp, self.data_block[0:1, :].copy()))
                        # print 'Stored a frame!!!'
                    except queue.Full:
                        self.queue_overflow += 1
                # otherwise store the data only when a new timestamp is received and the number of frames is not zero
                else:
                    if (timestamp != last_timestamp) and (n != 0):
                        # print 'trying to store a frame!!!'

                        # store_data is False if this is the first block to be
                        # stored. In this case, do not store the data in case
                        # we got partial block after a flush()
                        if self.store_data:
                            # If the queue is full, make room by popping the oldest element
                            if self.queue.full():
                                self.queue.get()
                            # Now write the block of frames to the queue
                            try:
                                # print 'Storing a frame!!!'
                                self.queue.put_nowait((timestamp, self.data_block[0:n, :].copy()))
                                total_queue_entries += 1
                                # if not (total_queue_entries % 10):
                                #   print '.',
                            except queue.Full:
                                self.queue_overflow += 1
                                if self.verbose:
                                    print('Timestream Receiver Queue overflow... Should not happen...')
                        else:
                            self.store_data = 1  # next time store the block
                        last_timestamp = timestamp
                        n = 0
                    # Copy the new vector into the block memory buffer
                    if n < 0 or n >= 16:
                        if self.verbose:
                            print('Timestream Receiver: received %i Timestrem/Spectrum frames '
                                  'with the same timestamp.' % n)
                    elif nbytes != 2048 + 10:
                        if self.verbose:
                            print('Timestream Receiver: Timestrem/Spectrum frame has %i bytes instead of '
                                  '2048+9=2057 bytes. First bytes are: 0x%s' % (
                                        nbytes,
                                        ' '.join('%02X' % c for c in self.data[:32])))
                    else:
                        self.data_block[n, :] = self.data[: 2048 + 10]
                        n += 1
            # Unknown frame type
            else:  # unknown frame format
                if self.verbose:
                    print('Receiver: Frame of %i bytes with unknown identifier 0x%2X has been received. '
                          'It was discarded. First bytes are 0x%s' % (
                                nbytes,
                                frame_id,
                                ' '.join('%02X' % c for c in self.data[:32])))

        # self.queue.task_done() # JFC: Must be used by queue consumer, not the producer (this thread)
        # self.queue_corr.task_done()
        if self.verbose:
            print('Frame acquisition thread is stopped')

    def status(self, print_delay=1):
        last_display_time = 0
        try:
            while True:
                t = time.time()
                dt = t-last_display_time
                if dt > print_delay:
                    print('Received %i frames at %f frames/s (%f Mb/s), buffer size = %i, overflows= %i' % (
                        self.n_frames,
                        self.n_frames / dt,
                        self.n_frames / dt * (2048 + 10) * 8 / 1e6,
                        self.queue.qsize(),
                        self.queue_overflow))
                    last_display_time = t
                    self.n_frames = 0
        except KeyboardInterrupt:
            pass


class chFPGA_receiver(object):
    # define constants
    FRAME_BUFFER_LENGTH = 2 # 10
    FRAME_HEADER_LENGTH = 10
    CORR_FRAME_HEADER_LENGTH = 11
    LOG2_FRAME_LENGTH = 11
    FRAME_LENGTH = 2 ** LOG2_FRAME_LENGTH

    # CHANNELS_PER_CORR = 204
    # NUMBER_OF_ANTENNAS_TO_CORRELATE = 5 #8
    # NUMBER_OF_CORRELATORS = NUMBER_OF_ANTENNAS_TO_CORRELATE
    FREQ_CHANNELS_MAX = 1024

    def __init__(self, chFPGA_config, verbose=1):

        print('*** Opening receiver sockets ***')
        # Create socket handled and open socket communications to the chFPGA board
        self.ip_address = chFPGA_config.system_fpga_ip_address
        self.host_ip = chFPGA_config.system_interface_ip_address
        self.sock = SocketIO.DataSocket_base(
            self.ip_address,
            host_ip=self.host_ip,
            port_number=chFPGA_config.system_local_data_port_number)
        self.port_number = self.sock.port_number
        # self.sock.open()
        # Add configuration
        self.chFPGA_config = chFPGA_config
        self.Nch = chFPGA_config.number_of_antennas_to_correlate
        self.Ncorr = chFPGA_config.number_of_correlators
        self.Ncmac = 2 * (self.Nch + 1)
        # self.CHANNELS_PER_CORR_MAX = 512 // max(1, self.Nch)
        # Create a frame a queue and a thread that will fill it
        self.frame_queue = queue.Queue(maxsize=self.FRAME_BUFFER_LENGTH)
        self.frame_queue_corr = queue.Queue(maxsize=self.FRAME_BUFFER_LENGTH)
        # self.frame_queue = multiprocessing.Queue(maxsize=1000)
        self.frame_receiver = ReceiverThread(
            self.sock.sock,
            self.frame_queue,
            self.frame_queue_corr,
            self.Nch,
            self.Ncorr,
            verbose=verbose)
        self.frame_receiver.start()
        X, Y = np.mgrid[0:self.Nch, 0:self.Nch]
        self.K = X * self.Nch - X * (X + 1) // 2 + Y
        self.define_sort_array()

        if self.Ncorr:
            self.raw_corr_map = self.raw_corr_map()
            self.rm = self.reverse_map(self.raw_corr_map)

    def __del__(self):

        self.close()
        print('__del__: Closed FPGA at IP address')  # %s' % self.SocketIO.OUT_IP

    def close(self):
        """
        Close object, which releases the socket bindings
        """
        self.frame_receiver.stop()
        self.frame_receiver.join(1)  # Wait up to the specified amount of time for the thread to complete
        if not self.frame_receiver.is_stopped():
            raise RuntimeError('Could not terminate Frame Receiver thread')
        # self.frame_queue_corr.join()
        # self.frame_queue.join()
        self.sock.close()

    def flush(self):
        """
        Empties the frame buffer. It is suggested to call this function when
        no data is being transmitted if the first frame to be received is
        generated by a specific user-controlled event (frame injection, single
        trigger etc.)
        """
        # self.sock.flush_data_socket() # This cause conflict with the background socket operations
        self.frame_receiver.flush(1)
        while (not self.frame_queue.empty()) or (not self.frame_queue_corr.empty()):
            print('data_queue_empty=%s, corr_queue_empty=%s' % (
                self.frame_queue.empty(),
                self.frame_queue_corr.empty()))
            time.sleep(0.1)
        self.frame_receiver.flush(0)
        # with self.frame_queue.mutex:
        #    self.frame_queue.queue.clear()

    def send_every_frame(self, state):
        """
        If state=True, tells the receiver Thread to put in the FIFO every data
        frame as it comes in.

        If state=False, the receiver will put all the data with the same
        timestamp in the FIFO. This means this is not done until another
        timestanp is received.
        """
        self.frame_receiver.send_every_frame(state)

    def length(self):
        """ Returns the number of entries in the receiver FIFO """

        return self.frame_queue.qsize()

    def read_frames(self, frames=1, verbose=0, raw=0, flush=0, timeout=3):
        """
        Get frames that were captured by the capture thread.

        Parameters:
            frames: Number of frames to acquire per channel. Limited by the buffer lengths in the FPGA
            raw: when true, returns the unsigned raw data from the ADC (bit 7 is not inverted)
        History:
            110916 JFC: Added comments.
                Changed output format to dictionary of arrays instead of bidimensional array.
                Now use global trigger to support multi-channel

            120713 JFC: Changed name to from read_ADC_frames to get_frames.
                Rewritten for new frame acquisition architecture
        """

        # Acquire the data
        data = {}
        if flush:
            self.flush()

        for j in range(frames):
            if verbose > 1 or (verbose == 1 and (j % 100 == 99 or j == frames - 1)):
                print('Acquiring Frame %i (%.0f%%)' % ((j + 1), (100 * (j + 1) / frames)))
            data_block = self.frame_queue.get(timeout=timeout)

            block_timestamp = data_block[0]
            in_frames = data_block[1]

            data['timestamp'] = block_timestamp

            for in_frame in in_frames[:]:

                if(len(in_frame) < self.FRAME_HEADER_LENGTH):
                    print('Bad header')
                    break
                else:
                    (probe_id, stream_id, flags, ts_high, timestamp) = struct.unpack_from('>BHBHL', in_frame)
                    timestamp += ts_high << 32
                    channel = stream_id & 0x0F
                    
                if(len(in_frame) != self.FRAME_LENGTH+self.FRAME_HEADER_LENGTH):
                    print('Frame too short')
                    break

                # Process the frame data

                raw_data = in_frame[self.FRAME_HEADER_LENGTH:]
                raw_data.dtype = np.int8  # ADC output are signed values

                if raw:
                    raw_data.dtype=np.uint8
                    raw_data^=0x80

                if verbose >= 2:
                    print('Packet received from port %i. Frame header information:  probe_id #=%i, stream_id #=%i, flags=%i words, timestamp=%i, flags=%i' % (channel, probe_id, stream_id, flags, timestamp, flags))
                    print(data)
                # Make sure there is an empty vector on the first storage so we can concatenate to it the new data

                if channel not in data:
                    data[channel] = raw_data
                else:
                    data[channel] = np.hstack((data[channel], raw_data))
        return data

    def read_corr_frames(
            self,
            flush=0,
            timeout=3,
            verbose=2,
            raw=False,
            complete_set=True,
            max_trials=15):
        """
        Get correlator frames that were captured by the capture thread,
        combine them, and return a processed complex correlation array.

        Parameters:
            flush: when True, flushes the receive buffer before getting new data

        Returns:

            A complex array of integrated, cross-correlated spectra
            C(product_number, freq_bin_number) where product_number identifies
            the desired cross-correlation, and freq_bin_index is the
            frequency index.

            The cross-correlations are ordered as follows: A(0)xA(0)*,
            A(0)xA(1)* ... A(0)xA(n-1)*, A(1)xA(1)*, ... A(1)xA(n-1)*, ...
            A(n-1)xA(n-1)* where n is the number of correlated antennas. There
            are N*(N+1)/2 products.

            For n=5 antennas, C(0), C(5), C(9), C(12), C(14) are the 5
            auto-correlation spectra of antennas 0 to 4.

        NOTES:
            - The function assumes that the number of frequency channels
              processed by each correlator is the same for all correlators.
              The number is derived from the length of the frames.

        History:
            120913 KMB: Created from read_frames to read corr buffer
            121021 JFC: Updated for multi-correlator data processing.
            121126 JM: initialized corr_data as a matrix of NaN (before it was started as matrix of zeros).
        """
        # Create an empty array of correlation products. Dimensions are: (corr_number, cmac_number,  product_number)
        raw_corr_data = np.zeros((self.Ncorr, self.Ncmac, 512), dtype=complex) * np.nan

        # Flush the queue. Need to change flush to take a queue object
        if flush:
            self.flush()

        # Acquire the data

        trial = 0
        while True:
            frames = self.frame_queue_corr.get(timeout=timeout)

            if verbose >= 1:
                print('Got a data block of shape ', np.shape(frames))

            if not complete_set or len(frames) == self.Ncorr * self.Ncmac:
                for frame in frames[:]:
                    if(len(frame) < self.CORR_FRAME_HEADER_LENGTH):
                        print('Bad header')
                        break
                    else:
                        (_, _, corr_id, cmac_id, _, timestamp) = struct.unpack_from('<BBBBLL', frame)

                    if verbose >= 4:
                        print('Frame data: %s' % (''.join('%02X' % np.uint8(c) for c in frame)))

                    if len(frame[12:]) % 5:
                        print('Error: number of product bytes (%i) not a multiple of 5' % (frame[12:]))

                    num_products = len(frame[12:]) // 5  # Total number of products in the frame (for all channels)
                    if num_products % self.Nch:
                        print('Error: number of products (%i)  not a multiple of the number of channelizers (%i)' % (
                            num_products,
                            self.Nch))
                    if verbose >= 2:
                        print('Frame header information:  corr#=%i, cmac#=%i, timestamp=0x%X ' % (
                            corr_id, cmac_id, timestamp))

                    # chop the data in 5-byte chunks and compute ``num_products`` 40-bit words
                    # print frame[12:].reshape(num_products, 5)                                    #nice spaghetti :) it confuses the linter
                    w = np.flipud((frame[12:].reshape(num_products, 5).view(np.uint8) *
                                  [1, 1 << 8, 1 << 16, 1 << 24, 1 << 32]).sum(-1))
                    re = np.int32((w >> 18) & 0x3FFFF)
                    re[(re & (1 << 17)) != 0] -= 1 << 18
                    im = np.int32(w & 0x3FFFF)
                    im[(im & (1 << 17)) != 0] -= 1 << 18
                    v = re + 1j*im
                    raw_corr_data[corr_id, cmac_id, 0:len(v)] = v
                if not complete_set or not np.any(np.isnan(raw_corr_data)):
                    break
                print('There are missing frames')
            else:
                if verbose:
                    print('Got %i frames instead of %i' % (len(frames), self.Ncorr * self.Ncmac))
            trial += 1
            if trial >= max_trials:
                raise RuntimeError('Could not get a complete correlator frame set after %i trials' % trial)
            if verbose:
                print('Retrying')

        if raw:
            data = raw_corr_data
        else:
            rm = self.rm
            data = raw_corr_data[rm[..., 0], rm[..., 1], rm[..., 2]]
            i, j = np.tril_indices_from(data[0], -1)
            data[:, j, i] = data[:, i, j].conj()  # fill the upper triangle with the conjugate of the lower

        return data

    def pp(self, data):
        for i in data.shape[0]:
            for j in data.shape[1]:
                pass

    def define_sort_array(self):
        '''
        Create Array of indices that goes from corr_number, mult_id, and product_number to K and frequency
        '''
        Nant = self.Nch
        corr2sorted = np.empty((Nant, Nant+1, 512, 2), dtype=int)  # corr_number, mult_id, product_number to K, freq
        mult_ids = np.arange(Nant + 1)
        corr_numbers = np.arange(Nant)
        product_numbers = np.arange(512)  # Need a better way to get this...
        for corr_number in corr_numbers:
            for mult_id in mult_ids:
                for product_number in product_numbers:
                    # Compute the product index within a frequency bin pair 0-Nantenna
                    freq_bin_product_number = product_number % Nant
                    freq_channel = (product_number // Nant) * 2 * Nant + corr_number * 2
                    # Compute the (i,j) index of each product
                    if mult_id == 0:
                        i_index = Nant - 1 - freq_bin_product_number
                        j_index = Nant - 1 - freq_bin_product_number
                        freq_channel_offset = 1
                    elif mult_id == Nant:
                        i_index = freq_bin_product_number
                        j_index = freq_bin_product_number
                        freq_channel_offset = 0
                    elif freq_bin_product_number < mult_id:  # if we have the 'A' products
                        i_index = Nant - 1 - mult_id
                        j_index = Nant - mult_id + freq_bin_product_number
                        freq_channel_offset = 0
                    else:
                        i_index = mult_id - 1
                        j_index = mult_id + Nant - freq_bin_product_number - 1
                        freq_channel_offset = 1
                    linear_index = self.K[i_index, j_index]
                    # print corr_number, mult_id, product_number, linear_index, freq_channel+freq_channel_offset
                    corr2sorted[corr_number, mult_id, product_number] = [
                        linear_index, freq_channel + freq_channel_offset]
        self.corr2sorted = corr2sorted
