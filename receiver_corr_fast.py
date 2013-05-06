#!/usr/bin/python

'''
Fast python corr reciever.  try to save faster.
'''

import Queue
import threading
import struct
import time

import numpy as np

import core.SocketIO as SocketIO

class ReceiverThread(threading.Thread):        
    BUF_SIZE=65536
    data = bytearray(BUF_SIZE)
    data_buf = buffer(data)
    #Number of frequency bin pairs, Number of antennas, Number of bytes per word, header
    NUMBER_OF_CORRELATORS = 5
    NUMBERS_OF_ANTENNAS_TO_CORRELATE = 5
    NUMBER_OF_MULTIPLIERS = NUMBERS_OF_ANTENNAS_TO_CORRELATE + 1
    #MAX_NUMBER_OF_CHANNELS_PER_CORRELATOR = 128 
    MAX_CORR_FRAME_LENGTH = 512*13+11 #in bytes. The accumulator size is always 512 words, each word being 13 bytes long. A 11 byte header is added. 
    corr_data_block = np.zeros((NUMBER_OF_MULTIPLIERS * NUMBER_OF_CORRELATORS, MAX_CORR_FRAME_LENGTH), dtype=np.int8)      
    queue_overflow = 0
    queue_corr_overflow = 0
    n_frames = 0
    
    def __init__(self, sock, queue_corr):
        self.sock = sock
        self.queue_corr = queue_corr
        self._stop = threading.Event()
        self._flush = threading.Event()
        self.print_delay = 1
        self._send_every_frame = threading.Event()
        super(type(self), self).__init__()
    
    def stop(self):
        self._stop.set()

    def send_every_frame(self,state):
        if state:
            self._send_every_frame.set()
        else:
            self._send_every_frame.clear()
            
    def is_stopped(self):
        return self._stop.is_set()

    def run(self):
        last_corr_timestamp = 0
        last_corr_time = time.time()
    #    last_delta = 0
        nc=0 # current number of correlator frames stored
        total_queue_entries = 0       
        self.sock.settimeout(0.1)
        print 'Frame acquisition thread is running'
        while not self._stop.is_set():
                try:
                    nbytes = self.sock.recv_into(self.data)
                except SocketIO.timeout:
                    nbytes = 0
                if nbytes:
                    #print 'Received a frame!!!'
                    self.n_frames += 1
                    frame_id=self.data[0] & 0xF0 # get the frame ID 
                    #Correlator input                    
                    ###### CORRELATOR DATA HANDLER ###########
                    if (frame_id == 0xF0): # If correlator data
                        (corr_number, mult_number, stream_id, word_length, timestamp) = struct.unpack_from('>BHHHL', self.data_buf)
                        corr_time = time.time()
                        #Correlator unpack first try very simple. 
                        corr_number &= 0x0F # mask the FRAME ID bits
                        if (mult_number < self.NUMBER_OF_MULTIPLIERS ) :
                            if ((timestamp != last_corr_timestamp) or (corr_time-last_corr_time > 0.5) ) and (nc>0): #if this is the beginning of a new correlator data block
                                # If the queue is full, make room by poping the oldest element
                                if self.queue_corr.full():
                                    self.queue_corr.get()
                               # Now try to write the data into the Queue. 
                                try:
                                    self.queue_corr.put_nowait(self.corr_data_block[0:nc,:nbytes].copy())
                                    #print 'Corr receiver: Pushing data to Queue with timestamp #%i (delta=%i), dt=%0.3f, # frames = %i' % (timestamp, timestamp - last_corr_timestamp, corr_time - last_corr_time, nc)
                                except Queue.Full:
                                    self.queue_corr_overflow += 1
                                    print 'Corr Receiver Queue overflow... Should not happen...'
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
                    
                    ###### UNKNOWN FRAME TYPE###########
                    else: # unknown frame format
                        print 'Receiver: Frame of %i bytes with unknown identifier 0x%Xx has been received. It was discarded. First bytes are 0x%s' % (nbytes, (probe_id & 0xF0) >> 4, ' '.join('%02X' % c for c in self.data[:32]))                              
                        
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
    NUMBER_OF_ANTENNAS_TO_CORRELATE = 5
    CHANNELS_PER_CORR_MAX = 512 // NUMBER_OF_ANTENNAS_TO_CORRELATE
    #NUMBER_OF_CORRELATORS = NUMBER_OF_ANTENNAS_TO_CORRELATE
    FREQ_CHANNELS_MAX = 1024

    def __init__(self, chFPGA_config, ip_address='10.10.10.11', port=41001):

        print '*** Opening receiver sockets ***'
        # Create socket handled and open socket communications to the chFPGA board
        self.sock=SocketIO.DataSocket_base(ip_address, port)
        #self.sock.open()
        self.chFPGA_config = chFPGA_config
        self.NUMBER_OF_ANTENNAS_TO_CORRELATE = chFPGA_config.number_of_antennas_to_correlate
        self.NUMBER_OF_CORRELATORS = chFPGA_config.number_of_correlators
        self.CHANNELS_PER_CORR_MAX = 512 // self.NUMBER_OF_ANTENNAS_TO_CORRELATE
        # Create a frame a queue and a thread that will fill it
        self.frame_queue_corr = Queue.Queue(maxsize=self.FRAME_BUFFER_LENGTH)
        self.frame_receiver = ReceiverThread(self.sock.sock, self.frame_queue_corr)
        self.frame_receiver.start()
        X, Y = np.mgrid[0:self.NUMBER_OF_ANTENNAS_TO_CORRELATE,0:self.NUMBER_OF_ANTENNAS_TO_CORRELATE]
        self.K = X*self.NUMBER_OF_ANTENNAS_TO_CORRELATE - X*(X+1)/2 + Y
        
    def __del__(self):

        self.close()
        print '__del__: Closed FPGA at IP address'# %s' % self.SocketIO.OUT_IP

    def close(self):
        """ 
        Close object, which releases the socket bindings
        """
        self.frame_receiver.stop()
        self.frame_receiver.join()
        self.sock.close()


    def send_every_frame(self, state):
        """ 
        If state=True, tells the receiver Thread to put in the FIFO every data frame as it comes in.
        If state=False, the receiver will put all the data with the same timestamp in the FIFO. This means this is not done until another timestanp is received.
        """        
        self.frame_receiver.send_every_frame(state)
        
    def length(self):
        """ Returns the number of entries in the receiver FIFO """
        
        return self.frame_queue.qsize()
      
            
    def read_corr_frames(self, timeout=3):
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
            130131 KMB: Removed all processing to be fast.
        """

            
        in_frames = self.frame_queue_corr.get(timeout=timeout)
   
        return in_frames



if __name__=='__main__':        
    r = chFPGA_receiver()
    fb = open('data_testing.bin', 'a+b')
    numbers = 0
    while numbers < 2560:
        data = r.frame_queue_corr.get(timeout=3)
        #data = r.read_corr_frames()
        fb.write(data)
        numbers +=1
    r.close()
