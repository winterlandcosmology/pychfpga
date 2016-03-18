#!/usr/bin/python
# Disable pylint TAB warnings (W0312) and Line too long (=C0301)
# pylint: disable=W0312,C0301

"""
chFPGA_reader.py module
 Implements the classes that read the data streams coming from chFPGA.

 History:
    2012-07-19 JFC: Created
    2012-10-17 JFC: Modified behavior of receiver for:
        a) If send_every_frame=False, discard the first block of frames after flush() avoid sending partial frame blocks
        b) if the Queue is full, automatically pop an element before pushing a new one. Old data will be automatically flushed over time.
"""


import Queue
import threading
import struct
import time
import select

import numpy as np

import SocketIO



class ReceiverThread(threading.Thread):
    BUF_SIZE=65536
    data = bytearray(BUF_SIZE)
    data_buf = buffer(data)
    data_block = np.zeros((16,2048+9), dtype=np.uint8)
    #Number of frequency bin pairs, Number of antennas, Number of bytes per word, header
    #NUMBER_OF_CORRELATORS = 5
    #NUMBER_OF_ANTENNAS_TO_CORRELATE = 5 #8
    #NUMBER_OF_MULTIPLIERS = NUMBER_OF_ANTENNAS_TO_CORRELATE + 1
    #MAX_NUMBER_OF_CHANNELS_PER_CORRELATOR = 128
    MAX_CORR_FRAME_LENGTH = 512*13+11 #in bytes. The accumulator size is always 512 words, each word being 13 bytes long. A 11 byte header is added.

#        frame_block = {'timestamp' :0, 'data':frame_data}
    queue_overflow = 0
    queue_corr_overflow = 0
    n_frames = 0
    store_data = 0 # do not store frame blocks if False
    store_corr_data = 0 # do not store frame blocks if False

    def __init__(self, sock, queue, queue_corr, NUMBER_OF_ANTENNAS_TO_CORRELATE, NUMBER_OF_CORRELATORS, verbose = 1):
        self.sock = sock
        self.queue = queue
        self.queue_corr = queue_corr
        self.NUMBER_OF_CORRELATORS = NUMBER_OF_CORRELATORS
        self.NUMBER_OF_ANTENNAS_TO_CORRELATE = NUMBER_OF_ANTENNAS_TO_CORRELATE
        self.NUMBER_OF_MULTIPLIERS = NUMBER_OF_ANTENNAS_TO_CORRELATE + 1
        self.corr_data_block = np.zeros((self.NUMBER_OF_MULTIPLIERS * self.NUMBER_OF_CORRELATORS, self.MAX_CORR_FRAME_LENGTH), dtype=np.int8)
        self._stop = threading.Event()
        self._flush = threading.Event()
        self.verbose = verbose
        self.print_delay = 1
        self._send_every_frame = threading.Event()
        super(type(self), self).__init__()

    def stop(self):
        self._stop.set()

    # Currently the flush is unused...  Remove?
    def flush(self,state):
        if state:
            self._flush.set()
        else:
            self._flush.clear()

    def send_every_frame(self,state):
        if state:
            self._send_every_frame.set()
        else:
            self._send_every_frame.clear()

    def is_stopped(self):
        return self._stop.is_set()

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
        #            expected_delta=self.ANT[0].PROBER.get_burst_period()
        #            missing_frames = 0
        #            bad_delta = 0
        #    data2 = bytearray(buf_size)
        self.sock.settimeout(0.1)
        # self.sock.setblocking(0)
        print 'Frame acquisition thread is running'
        while not self._stop.is_set():
            # data = self.sock.read_data(timeout_delay=timeout)
            # Read data from the UDP listening port
            if self._flush.is_set():
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
            else:
                try:
                    r1, w1, e1 = select.select([self.sock], [], [])
                    for e in r1:
                        if e == self.sock:
                            nbytes = self.sock.recv_into(self.data)
                except SocketIO.timeout:
                    nbytes = 0
                if nbytes:
                    # print 'Received a frame!!!'
                    self.n_frames += 1

                    # probe_id = struct.unpack_from('>B', self.data_buf)
                    frame_id = self.data[0] & 0xF0  # get the frame ID
                    # Correlator input
                    # ##### CORRELATOR DATA HANDLER ###########
                    if (frame_id == 0xF0):  # If correlator data
                        (corr_number, mult_number, stream_id, word_length, timestamp) = struct.unpack_from('>BHHHL', self.data_buf)
                        corr_time = time.time()
                        # Correlator unpack first try very simple.
                        corr_number &= 0x0F  # mask the FRAME ID bits
                        if (mult_number < self.NUMBER_OF_MULTIPLIERS ) :
                            if ((timestamp != last_corr_timestamp) or (corr_time-last_corr_time > 2.9) ) and (nc>0): #if this is the beginning of a new correlator data block
                                # If the queue is full, make room by poping the oldest element
                                if self.store_corr_data:  # False if this is the first block to be stored. In this case, do not store the data in case we got partial block after a flush()
                                    if self.queue_corr.full():
                                        self.queue_corr.get()
                                   # Now try to write the data into the Queue.
                                    try:
                                        self.queue_corr.put_nowait(self.corr_data_block[0:nc,:nbytes].copy())
                                        #print 'Corr receiver: Pushing data to Queue with timestamp #%i (delta=%i), dt=%0.3f, # frames = %i' % (timestamp, timestamp - last_corr_timestamp, corr_time - last_corr_time, nc)
                                    except Queue.Full:
                                        self.queue_corr_overflow += 1
                                        print 'Corr Receiver Queue overflow... Should not happen...'
                                else:
                                    self.store_corr_data = 1 # next time store the block
                                nc = 0
                                last_corr_timestamp = timestamp
                                last_corr_time = corr_time
                            if nc >= self.NUMBER_OF_MULTIPLIERS * self.NUMBER_OF_CORRELATORS:
                                print 'Corr Receiver: Received extra correlator frames for Corr#%i Mult#%i' % (corr_number, mult_number)
                            else:
                                #print 'Corr#%i Mult#%i ts=%i, time=%0.3f, dt=%0.3fs' % (corr_number, mult_number, timestamp, corr_time, corr_time-last_corr_time)
                                self.corr_data_block[nc,:nbytes] = self.data[:nbytes]
                                nc += 1
                        else:
                            print "Corr Receiver: Bad multiplier number"
                            #Clear stuff? ERROR HANDLE

                    ###### TIMESTREAM DATA HANDLER ###########
                    elif (frame_id == 0xA0): # if timestream or spectrum data
                        (probe_id, stream_id, word_length, timestamp) = struct.unpack_from('>BHHL', self.data_buf)
                        # If we don't want to wait for all frames with the same timestanp to be grouped, put the frame immediately on the queue
                        if self._send_every_frame.is_set():
                            self.data_block[0,:] = self.data[:2048+9]
                            # If the queue is full, make room by poping the oldest element
                            if self.queue.full():
                                self.queue.get()
                            # Now try to write the data into the Queue.
                            try:
                                self.queue.put_nowait((timestamp, self.data_block[0:1,:].copy()))
                                #print 'Stored a frame!!!'
                            except Queue.Full:
                                self.queue_overflow += 1
                        else: # otherwise store the data only when a new timestanp is received and the numbe of frames is not zero
                            if (timestamp != last_timestamp) and (n != 0):
                                #print 'trying to store a frame!!!'
                                if self.store_data: # False if this is the first block to be stored. In this case, do not store the data in case we got partial block after a flush()
                                # If the queue is full, make room by poping the oldest element
                                    if self.queue.full():
                                        self.queue.get()
                                    # Now write the block of frames to the queue
                                    try:
                                        #print 'Storing a frame!!!'
                                        self.queue.put_nowait((timestamp, self.data_block[0:n,:].copy()))
                                        total_queue_entries += 1
                                        #if not (total_queue_entries % 10):
                                            #print '.',
                                    except Queue.Full:
                                        self.queue_overflow += 1
                                        print 'Timestream Receiver Queue overflow... Should not happen...'
                                else:
                                    self.store_data = 1  # next time store the block
                                last_timestamp = timestamp
                                n = 0
                            # Copy the new vector into the block memory buffer
                            if n < 0 or n >= 16:
                                print 'Timestream Receiver: received %i Timestrem/Spectrum frames with the same timestamp.' % n
                            elif nbytes != 2048 + 9:
                                print 'Timestream Receiver: Timestrem/Spectrum frame has %i bytes instead of 2048+9=2057 bytes. First bytes are: 0x%s' % (nbytes, ' '.join('%02X' % c for c in self.data[:32]))
                            else:
                                self.data_block[n, :] = self.data[: 2048 + 9]
                                n += 1
                    ###### UNKNOWN FRAME TYPE###########
                    else:  # unknown frame format
                        print 'Receiver: Frame of %i bytes with unknown identifier 0x%Xx has been received. It was discarded. First bytes are 0x%s' % (nbytes, (self.data[0] & 0xF0) >> 4, ' '.join('%02X' % c for c in self.data[:32]))

        # self.queue.task_done() # JFC: Must be used by queue consumer, not the producer (this thread)
        # self.queue_corr.task_done()
        print 'Frame acquisition thread is stopped'

    def status(self, print_delay=1):
        last_display_time = 0
        try:
            while True:
                t = time.time()
                dt = t-last_display_time
                if dt > print_delay:
                    print 'Received %i frames at %f frames/s (%f Mb/s), buffer size = %i, overflows= %i' % (self.n_frames, self.n_frames/dt, self.n_frames/dt*(2048+9)*8/1e6,  self.queue.qsize(), self.queue_overflow)
                    last_display_time = t
                    self.n_frames = 0
        except KeyboardInterrupt:
            pass

class chFPGA_receiver(object):
    # define constants
    FRAME_BUFFER_LENGTH = 2#10
    FRAME_HEADER_LENGTH = 9
    CORR_FRAME_HEADER_LENGTH = 11
    LOG2_FRAME_LENGTH = 11
    FRAME_LENGTH = 2**LOG2_FRAME_LENGTH

    #CHANNELS_PER_CORR = 204
    #NUMBER_OF_ANTENNAS_TO_CORRELATE = 5 #8
    #NUMBER_OF_CORRELATORS = NUMBER_OF_ANTENNAS_TO_CORRELATE
    FREQ_CHANNELS_MAX = 1024

    def __init__(self, chFPGA_config):

        print '*** Opening receiver sockets ***'
        # Create socket handled and open socket communications to the chFPGA board
        self.ip_address = chFPGA_config.system_fpga_ip_address
        self.host_ip = chFPGA_config.system_interface_ip_address
        self.sock = SocketIO.DataSocket_base(self.ip_address, host_ip=self.host_ip)  # removed , port_number=chFPGA_config.system_local_data_port_number
        self.port_number = self.sock.port_number
        #self.sock.open()
        #Add configuration
        self.chFPGA_config = chFPGA_config
        self.NUMBER_OF_ANTENNAS_TO_CORRELATE = chFPGA_config.number_of_antennas_to_correlate
        self.NUMBER_OF_CORRELATORS = chFPGA_config.number_of_correlators
        self.CHANNELS_PER_CORR_MAX = 512 // max(1, self.NUMBER_OF_ANTENNAS_TO_CORRELATE)
        # Create a frame a queue and a thread that will fill it
        self.frame_queue = Queue.Queue(maxsize=self.FRAME_BUFFER_LENGTH)
        self.frame_queue_corr = Queue.Queue(maxsize=self.FRAME_BUFFER_LENGTH)
        #self.frame_queue = multiprocessing.Queue(maxsize=1000)
        self.frame_receiver = ReceiverThread(self.sock.sock, self.frame_queue, self.frame_queue_corr, self.NUMBER_OF_ANTENNAS_TO_CORRELATE, self.NUMBER_OF_CORRELATORS, verbose=0)
        self.frame_receiver.start()
        X, Y = np.mgrid[0:self.NUMBER_OF_ANTENNAS_TO_CORRELATE,0:self.NUMBER_OF_ANTENNAS_TO_CORRELATE]
        self.K = X * self.NUMBER_OF_ANTENNAS_TO_CORRELATE - X*(X+1)/2 + Y
        self.define_sort_array()


    def __del__(self):

        self.close()
        print '__del__: Closed FPGA at IP address'# %s' % self.SocketIO.OUT_IP

    def close(self):
        """
        Close object, which releases the socket bindings
        """
        self.frame_receiver.stop()
        self.frame_receiver.join(1)  # Wait up to the specified amount of time for the thread to complete
        if self.frame_receiver.isAlive():
            raise RuntimeError('Could not terminate Frame Receiver thread')
        #self.frame_queue_corr.join()
        #self.frame_queue.join()
        self.sock.close()


    def flush(self):
        """ Empties the frame buffer.
        It is suggested to call this function when no data is being transmitted if the first frame to be received is generated by a specific user-controlled event (frame injection, single trigger etc.)
        """
        #self.sock.flush_data_socket() # This cause conflict with the background socket operations
        self.frame_receiver.flush(1)
        while (not self.frame_queue.empty()) or (not self.frame_queue_corr.empty()):
            pass
        self.frame_receiver.flush(0)
        #with self.frame_queue.mutex:
        #    self.frame_queue.queue.clear()

    def send_every_frame(self, state):
        """
        If state=True, tells the receiver Thread to put in the FIFO every data frame as it comes in.
        If state=False, the receiver will put all the data with the same timestamp in the FIFO. This means this is not done until another timestanp is received.
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
                Changed output format to dictionnary of arrays instead of bidimentional array.
                Now use global trigger to support multi-channel
            120713 JFC: Changed name to from read_ADC_frames to get_frames. Rewritten for new frame acquisition architecture
        """

        # Acquire the data
        data={}
        if flush:
            self.flush()

        for j in range(frames):
        #j=0
        #while 1:
            #j+=1
            if verbose>1 or (verbose==1 and (j % 100 ==99 or j==frames-1)):
                print 'Acquiring Frame %i (%.0f%%)' % ((j+1),(100*(j+1)/frames))
            #try:
            data_block = self.frame_queue.get(timeout=timeout)
            #except Queue.`:
            #    return None

            block_timestamp = data_block[0]
            in_frames =  data_block[1]

            data['timestamp'] = block_timestamp

            for in_frame in in_frames[:]:


                if(len(in_frame) < self.FRAME_HEADER_LENGTH):
                    print 'Bad header'
                    break
                else:
                    (probe_id, stream_id, word_length, timestamp) = struct.unpack_from('>BHHL', in_frame)
                    channel = probe_id & 0x0F
                    flags = word_length >> 12
                    word_length &= (2**12 - 1)

                if(len(in_frame) != self.FRAME_LENGTH+self.FRAME_HEADER_LENGTH):
                    print 'Frame too short'
                    break

                # Process the frame data

                raw_data=in_frame[self.FRAME_HEADER_LENGTH:]
                raw_data.dtype=np.int8 # ADC output are signed values

                if raw:
                    raw_data.dtype=np.uint8
                    raw_data^=0x80

                if verbose >=2:
                    print 'Packet received from port %i. Frame header information:  probe_id #=%i, stream_id #=%i, Word length=%i words, timestamp=%i, flags=%i' % (channel, probe_id, stream_id, word_length, timestamp, flags)
                    print data
                    #pass
                # Make sure there is an empty vector on the first storage so we can concatenate to it the new data

                if channel not in data:
                    data[channel]=raw_data
                else:
                    data[channel]=np.hstack((data[channel],raw_data));
        return data

    def read_corr_frames(self, flush=0, timeout=3, verbose=2):
        """
        Get correlator frames that were captured by the capture thread, combine them, and return a processed complex correlation array.

        Parameters:
            flush: when True, flushes the receive buffer before getting new data

        Returns:
            An complex array of integrated, cross-correlated spectrums  C(product_number, freq_bin_number) where
            product_number identifies the desired cross-corrleation, and freq_bin_index is the frrequency index.
            The cross-correlations are ordered as follows: A(0)xA(0)*, A(0)xA(1)* ... A(0)xA(n-1)*, A(1)xA(1)*, ... A(1)xA(n-1)*, ... A(n-1)xA(n-1)* where n is the numbe rof correlated antennas. There are N*(N+1)/2 products.
            For n=5 antennas, C(0), C(5), C(9), C(12), C(14) are the 5 auto-correlation spectrums of antennas 0 to 4.
        NOTES:
            - The function assumes that the number of frequency channels processed by each correlator is the same for all correlators. The number is derived from the length of the frames.
        History:
            120913 KMB: Created from read_frames to read corr buffer
            121021 JFC: Updated for multi-correlator data processing.
            121126 JM: initialized corr_data as a matrix of nan (before it was started as matrix of zeros).
        """
        Nant = self.NUMBER_OF_ANTENNAS_TO_CORRELATE # Number of correlated antennas c.GPIO.
        Nproducts_max = (Nant*(Nant+1))/2 # Total number of correlation products
        #Nchannels_max = self.CHANNELS_PER_CORR_MAX # Maximum number of frequency channels that can be contained in a frame CHANNELS_PER_CORR_MAX*NUMBER_OF_CORRELATORS
        ##linear_map = lambda i, j : (Nant * (Nant + 1) - (Nant - i) * (Nant - i + 1)) / 2 + (j - i) # Maps (i,j) (for j>=i) matrix coordinates into a linear array indexed from 0 to Nant*(Nant-1)/2-1: x0x0, x0x1, x0x2, x0x3, x1x1, x1x2, x1x3, x2x2, x2x3, x3x3
        ### Replace linear map with a Matrix

        corr_data=np.zeros((Nproducts_max, self.FREQ_CHANNELS_MAX), dtype=complex)*np.nan  # Dimensions are: (Number_of_products, number_of_frequency_channels)

        # Acquire the data
        #data={}
        #need to change flush to take a queue object
        if flush:
            self.flush()

        #for j in range(frames):
        #j=0
        #while 1:
            #j+=1
        #    if verbose>1 or (verbose==1 and (j % 100 ==99 or j==frames-1)):
        #        print 'Acquiring Frame %i (%.0f%%)' % ((j+1),(100*(j+1)/frames))
            #try:
        in_frames = self.frame_queue_corr.get(timeout=timeout)
        #except Queue.`:
        #    return None
        if verbose >= 1:
            print 'Got a data block of shape ', np.shape(in_frames)
        #in_frames =  data_block
        #block_timestamp = 0
        #data['timestamp'] = block_timestamp
        for in_frame in in_frames[:]:
            if(len(in_frame) < self.CORR_FRAME_HEADER_LENGTH):
                print 'Bad header'
                break
            else:
                (frame_id, mult_id, stream_id, word_length, timestamp) = struct.unpack_from('>BHHHL', in_frame)
                #data['mult_id'] = mult_id

            #Return numpy complex128's  Check if this shifting is correct
            #not shifting through correctly yet.
            #not sure if the word thing will work, might need indexes or something
            #raw_data = []
            #in_frame[11+13*i:24+13*i] i from 0 to 512
            if verbose >= 4:
                print 'Frame data: %s' % (''.join('%02X' % np.uint8(c) for c in in_frame))
            corr_number = frame_id & 0x0F
            product_number = 0  #counts the products until the end of a correlator frame.  Most basic product counter
            if len(in_frame[11:])%13:
                print 'Error: number of product bytes (%i) not a multiple of 13' %  (in_frame[11:])

            num_products = len(in_frame[11:])/13 # Total number of products in the frame (for all channels)
            if num_products % Nant:
                print 'Error: number of products (%i)  not a multiple of the number of antennas (%i)' % (num_products, Nant)
            num_channels_per_correlator = num_products//Nant*2
            if verbose >=2:
                print 'Frame header information:  corr#=%i, mult#=%i, num_channels in this corr=%i, Word length=0x%X words, timestamp=0x%X ' % ( corr_number, mult_id, num_channels_per_correlator, word_length, timestamp )
                #pass
            for word in in_frame[11:].reshape(num_products,13):
                (flags, r1, r2, i1, i2) = struct.unpack_from('>BhLhL',word)
                product = ((r1 << 32) | r2 ) + 1.0j * ((i1 << 32) | i2)
                corr_data[self.corr2sorted[corr_number,mult_id,product_number,0], self.corr2sorted[corr_number,mult_id,product_number,1]] = product
                product_number += 1
            #raw_data = np.array(raw_data)





            # Process the frame data Need to use Mult_ID to sort out what is what.
            # include in data flags etc?

            #Format data from frame

            # Make sure there is an empty vector on the first storage so we can concatenate to it the new data

            #if mult_id in data:
            #    print 'Warning: correlator data is received multiple times from the same multiplier'

            #data[mult_id]=raw_data

            #if mult_id not in data:
            #    data[mult_id]=raw_data
            #else:
            #    data[mult_id]=np.hstack((data[mult_id],raw_data));
        #if raw:
        #    return data
        #else:
        return corr_data

    def define_sort_array(self):
        '''
        Create Array of indicies that goes from corr_number, mult_id, and product_number to K and frequency
        '''
        Nant = self.NUMBER_OF_ANTENNAS_TO_CORRELATE
        corr2sorted = np.empty((Nant,Nant+1,512, 2 ), dtype=int) #corr_number, mult_id, product_number to K, freq
        mult_ids = np.arange(Nant+1)
        corr_numbers = np.arange(Nant)
        product_numbers = np.arange(512)  #Need a better way to get this...
        for corr_number in corr_numbers:
            for mult_id in mult_ids:
                for product_number in product_numbers:
                    freq_bin_product_number = product_number %  Nant  #Product index within a frequency bin pair 0-Nantenna
                    freq_channel = (product_number//Nant)*2*Nant + corr_number*2
                    # Compute the (i,j) index of each product
                    if mult_id == 0:
                        i_index = Nant - 1 - freq_bin_product_number
                        j_index = Nant - 1 - freq_bin_product_number
                        freq_channel_offset = 1
                    elif mult_id == Nant:
                        i_index = freq_bin_product_number
                        j_index = freq_bin_product_number
                        freq_channel_offset = 0
                    elif freq_bin_product_number < mult_id: # if we have the 'A' peoducts
                        i_index = Nant - 1 - mult_id
                        j_index = Nant - mult_id + freq_bin_product_number
                        freq_channel_offset = 0
                    else:
                        i_index = mult_id - 1
                        j_index = mult_id + Nant - freq_bin_product_number - 1
                        freq_channel_offset = 1
                    linear_index = self.K[i_index, j_index]
                    #print corr_number, mult_id, product_number, linear_index, freq_channel+freq_channel_offset
                    corr2sorted[corr_number,mult_id,product_number] = [linear_index, freq_channel+freq_channel_offset]
        self.corr2sorted = corr2sorted





