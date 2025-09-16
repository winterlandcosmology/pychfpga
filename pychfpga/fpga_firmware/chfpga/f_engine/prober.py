#!/usr/bin/python

"""
PROBER.py module
 Implements interface to a data PROBER

 History:
 2012-06-21 JFC: Created
 2012-07-20 JFC: Added initialization of PROBE_ID with antenna number
"""

# Standard library packages
import logging
import numpy as np
import socket

from ..mmi import MMI, BitField, CONTROL, STATUS


class PROBER(MMI):
    """ Implements the interface to the data PROBER within a channel processor
    """

    ADDRESS_WIDTH = 9

    # Memory-mapped control registers
    RESET = BitField(CONTROL, 0, 7, doc="Resets the module (including the FIFO)")
    FIFO_RESET = BitField(CONTROL, 0, 6, doc="When '1', resets the data FIFO")
    SOURCE_SEL = BitField(CONTROL, 0, 5, doc="0 = source selector output (timestream), 1 = scaler output (spectrum)")
    PROBER_USER_FLAGS = BitField(CONTROL, 0, 2,
                                 doc="If True, returns user flags in last 4 bits captured from scaler")
    BURST_LENGTH = BitField(CONTROL, 0, 0, width=2, doc="Sets the number of consecutive frames capture and transmit")


    OFFSET = BitField(CONTROL, 3, 0, width=24, doc="Number of frames to wait before starting the capture")

    PERIOD0 = BitField(CONTROL, 6, 0, width=24, doc="Number of frames between capture triggers")

    STREAM_ID = BitField(CONTROL, 8, 0, width=16, doc="Arbitrary 12-bit number that that identifies the source of the data (typically crate/slot/channel numbers)")
    SEND_DELAY = BitField(CONTROL, 10, 0, width=16, doc="Amount of time to wait before sending captured data once the data fifo has been emptied. The actual delay is (send_delay*65536)/125 MHz.")

    PERIOD1 = BitField(CONTROL, 13, 0, width=24, doc="Number of frames between capture triggers")
    SUB_PERIOD = BitField(CONTROL, 14, 0, width=5, doc="Capture additional frames every 2**(N+1) frames")

    # Memory-mapped status registers
    _CAPTURE_FLAG = BitField(STATUS, 0x00, 7, doc="State of the CAPTURE flag in the incoming frame data (for debugging)")
    _INHIBIT_DATA = BitField(STATUS, 0x00, 6, doc="Indicates if data capture in inhibited by a source transition")
    _CURRENT_SOURCE = BitField(STATUS, 0x00, 5, doc="Currently selected source")
    _DATA_FIFO_EMPTY = BitField(STATUS, 0x00, 4, doc="State of the DATA_FIFO_EMPTY signal (for debugging)")
    _DATA_FIFO_OVERFLOW = BitField(STATUS, 0x00, 3, doc="State of the DATA_FIFO_OVERFLOW signal (for debugging)")
    _DATA_FIFO_OVERFLOW_STICKY = BitField(STATUS, 0x00, 2, doc="State of the DATA_FIFO_OVERFLOW signal, stick to '1' when there us en aeeror until RESET=1 (for debugging)")
    _DATA_FIFO_OVERFLOW = BitField(STATUS, 0x00, 1, doc="State of the header_FIFO_OVERFLOW signal (for debugging)")

    TRIG_CTR = BitField(STATUS, 0x01, 0, width=8, doc="Number of frames")

    DATA_BUFFER_CAPACITY = 3 # Number of full frames that can fit in the FIFOs.

    def __init__(self,  *, router, router_port, instance_number):
        self.logger = logging.getLogger(__name__)
        super().__init__(router=router, router_port=router_port, instance_number=instance_number)
        self._lock()  # Prevent accidental addition of attributes (if, for example, a value is assigned to a wrongly-spelled property)
    # Specialized functions

    def reset(self):
        """ Resets the module"""
        self.pulse_bit('RESET')

    def reset_fifo(self):
        """ Clears the data FIFO"""
        self.pulse_bit('FIFO_RESET')

    DATA_SOURCE_TABLE = {
        'adc': 0,  # for old code. technically capature is done after funcgen
        'funcgen': 0,
        'scaler': 1}

    def set_data_source(self, source):
        if isinstance(source, str):
            if source in self.DATA_SOURCE_TABLE:
                source = self.DATA_SOURCE_TABLE[source]
            else:
                valid_sources = ', '.join(f"'{key}'" for key in self.DATA_SOURCE_TABLE)
                raise ValueError(f"Unknown data capture '{source}'. Valid sources are {valid_sources}.")
        self.SOURCE_SEL = source


    def set_burst_period(self, burst_period0=390625,   burst_period1=390625):
        """ Sets the interval between data capture bursts. The period is specified in number of frames. This method is used because the property does not yet handle multi-byte values well."""
        self.PERIOD0 = burst_period0 - 1
        self.PERIOD1 = burst_period1 - 1

    def get_burst_period(self):
        """ Returns the interval between data capture bursts. The period is specified in number of frames. This method is used because the property does not yet handle multi-byte values well."""
        return [self.PERIOD0, self.PERIOD1]

    def config_capture(self, frames_per_burst=1, burst_period=100, number_of_bursts=0, offset=0, send_delay=0):
        """
        Configure the capture of data frames for transmisssion over the ethernet link.

        Parameters:

            frames_per_burst (int): number of continuous frames to send in a
                burst (default=1)

            burst_period (int): delay between bursts in seconds

            number_of_bursts (int): number of bursts to send. '0' means that
                bursts are sent continuously as long as frames are tagged for
                capture at the source . Default is '0'.

            offset (int): sets how many frames are skipped before the capture
                starts. The actual number of skipped frames is `offset` x 256.
                The range is 0 to 8191. Assuming 2.56us frames, the offset is
                adjustable up to 5.36 s in increments io 655.36 us.

            send_delay (int): Sets the delay to start sending a block of data
                after it has been captured. The delay applies to tranmission
                that start after the local data buffer has been emptied (i.e the delay
                is not applied between contiguously captured framed). The delay is
                a 16 bit value that correspond to increments of 65536/125
                MHz=524.288 us. The maximum delay is therefore 524.288 us * 65535 =
                34.36 s.
        """

        # frame_period=1.0/850e6*self.ant.frame_length
        # burst_period=int(period/frame_period)
        if burst_period < frames_per_burst:
            burst_period = frames_per_burst
        if burst_period >= 2**24:
            raise RuntimeError('Capture period of %i frames is too long. Maximum value is %.3f s' % (burst_period, 2**24 - 1))

        delay_increment = 65536 / 125e6 # 0x10000 / 125 MHz
        delay_in_seconds = delay_increment * send_delay # Data transmission of new frames is held off by this amount
        period_in_seconds = self.fpga.FRAME_PERIOD * burst_period
        min_period = int(delay_in_seconds / self.fpga.FRAME_PERIOD / frames_per_burst)
        min_period_in_seconds = min_period * self.fpga.FRAME_PERIOD
        buffered_packets = frames_per_burst * np.ceil(delay_in_seconds / period_in_seconds)
        if buffered_packets > self.DATA_BUFFER_CAPACITY:
            raise RuntimeError('Capture send delay of %.3f s in combination with the capture period of %.3f s '
                'will cause up to %i packets to be buffered, wich exceed the capture buffer capability of %i frames. '
                'Minimum period for the current send delay is %i frames (%.3f ms)' %
                (delay_in_seconds, period_in_seconds, buffered_packets, self.DATA_BUFFER_CAPACITY, min_period, min_period_in_seconds * 1000))

        self.BURST_LENGTH = frames_per_burst - 1 # BURST_LENGTH actually specifies the number of additional frames in the firmware
        self.set_burst_period(burst_period, burst_period)
        # self.BURST_NUMBER = number_of_bursts
        self.OFFSET = offset
        self.SEND_DELAY = send_delay

    def set_stream_id(self, stream_id=None):
        """ Sets the STREAM ID of the raw data capture packets.

        Parameters:

            stream_id (int, tuple or None): desired Stream id.
                - If `stream_id` is an ``int``, the specified value is used.

                - If `stream_id` is a (crate, slot, channel) tuple, the
                  numeric stream_id will be built from the tuple elements.

                  The crate number '0' will be assumed if the crate has no
                  numeric crate_number information (even if the crate exists).
                  The same is true for the slot field if the board has no valid
                  slot information.


        Notes:

            The user must ensure that the stream IDs are unique if they are to
            be used with a stream-id-aware data receiver. The same stream-id
            could be used if crate numbers have not been assigned, or if
            multiple stand-alone boards are part of the array.

        """
        if isinstance(stream_id, int):
            self.STREAM_ID = stream_id

        elif isinstance(stream_id, (tuple, list)) and len(stream_id) == 3:
            crate, slot, channel = stream_id

            if not isinstance(slot, int):
                slot = 0
            if not isinstance(crate, int):
                crate = 0  # 0 if there is no backplane/crate, or the crate does not have an assigned crate number.

            self.STREAM_ID = ((crate & 0xF) << 8) | ((slot & 0xF) << 4) | (channel & 0x0F)
        else:
            raise ValueError('Invalid stream id parameter %r' % (stream_id, ))

    def get_stream_id(self):
        """ Return the stream ID of the current channel instance.

        Returns:
            int: the stream id.

        """
        return self.STREAM_ID


    def init(self, **kwargs):
        """ Initialize the data capture module"""
        # self.config_capture(1, 100) # Capture 1 frame every 100 frames
        channel = self.instance_number
        self.logger.debug('%r: Initializing channel %i' % (self, channel))
        # self.PROBE_ID = 0xA0 + channel  # For backwards compatibility
        self.set_stream_id(self.fpga.get_id(channel))
        self.RESET = 1  # Make sure no data is being transmitted at reset

    def status(self):
        """ Displays the status of the data capture module"""
        print('-------------- CHAN[%i] data capture --------------' % self.instance_number)
        print(' Capture frame(%s) every %s frames' % (self.BURST_LENGTH, self.get_burst_period()), end=' ')
        # if self.BURST_NUMBER:
        #     print 'for %i bursts' % self.BURST_NUMBER
        # else:
        #     print 'continuously while the frames are tagged for capture at the source'



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

    The transmit rate must be fast enough to accommodate the desired bandwidth
    by setting ib.GPIO.HOST_FRAME_READ_RATE = rate. rate=16 limits to about
    260 Mbps but is slow enough to allow python to process the data with a
    small standard UDP buffer. ``rate`` =15 is good for about 500 Mbps, and
    ``rate`` =16 is good for the full Gigabit bandwidth. The latetr two require
    bigger UDP buffers. See below::

        ib.GPIO.HOST_FRAME_READ_RATE = 14

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

        watch -cd -n .5 "grep :A6  /proc/net/udp"
    """

    def __init__(self, socket, buffer_length=256):

        self.socket = socket
        self.NPACKETS = buffer_length
        self.DATA_SIZE = 2048
        # self.ignore_packet_size = ignore_packet_size
        # Define numpy data types that will be used to efficiently parse the data

        self.header_dtype = np.dtype(dict(
            names=['cookie', 'stream_id', 'source_crate', 'slot_chan', 'flags', 'ts'],
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
        self.buf_flags = self.buf_struct['header']['flags'][:, 0]
        self.buf_ts = self.buf_struct['header']['ts'][:, 0]
        self.buf_ts_dirty = self.buf_struct['header']['ts'][:, 0]
        self.buf_data = self.buf_struct['data']
        self.ts_mask = np.uint64(0xFFFFFFFFFFFF)  # just keep the last 48 bits

    def flush(self, timeout, verbose):
            # Read packets until timeout to flush the OS UDP buffer
            self.socket.settimeout(timeout)
            while True:
                try:
                    s = self.socket.recv_into(self.buf[0])
                    if verbose:
                        print(f'flushing packet len={s} cookie=0x{self.buf_cookie[0]:02x} ts={self.buf_ts[0] & self.ts_mask}')

                except socket.timeout:
                    break
            # We assume the buffer might have overflowed and contains partial
            # timestamp. We flush until we gen a new timestamp.

    def wait_for_new_timestamp(self, cookie, timeout, verbose=0):
            self.socket.settimeout(timeout)
            while True:
                try:
                    s = self.socket.recv_into(self.buf[0])
                    if self.buf_cookie[0] & 0xfe != cookie:
                        continue
                    ts = self.buf_ts[0] & self.ts_mask
                    if self.last_ts is None:
                        self.last_ts = ts
                        continue
                    if self.last_ts == ts:
                        if verbose:
                            print(f'skipping packet len={s} cookie=0x{self.buf_cookie[0]:02x} ts={ts}')
                        continue
                    self.last_ts = ts
                    self.n = 1
                    break
                except socket.timeout:
                    continue

    def receive_packets(self, cookie, verbose=0):
        """ Receive some packets into the buffer until there is a timeout, there is a new timestamp, or the buffer is full
        """
        while True:
            if self.n == self.NPACKETS:
                return
            try:
                s = self.socket.recv_into(self.buf[self.n])
                # check if the packet has the right cookie
                if verbose:
                    print(f'got packet len={s} cookie=0x{self.buf_cookie[self.n]:02x} ts={self.buf_ts[self.n] & self.ts_mask}')
                if self.buf_cookie[self.n] & 0xfe != cookie:
                    continue

                # Ignore packets that don't have the right length
                if s != self.PACKET_SIZE:
                    continue
                ts = self.buf_ts[self.n] & self.ts_mask
                self.n += 1
                if verbose:
                    print(f'ts={ts}, last_ts={self.last_ts}, sid={self.buf_stream_id[self.n]:04x}')
                if self.last_ts is None:
                    self.last_ts = ts
                    continue
                if ts != self.last_ts:
                    break
            except socket.timeout:  # we have a timeout, so we probably have time to process data
                if self.n:  # continue if we don't have any data to process
                    if verbose:
                        print('timeout')
                    break
        # we get here if there is a timeout, a timestamp change, or if the buffer is full
        if verbose:
            print(f'got {self.n} packets')

    def read_raw_frames(
            self,
            stream_ids=range(16),
            flush=True,
            data_timeout=0.01,
            flush_timeout=0.001,
            number_of_frames=None,
            verbose=0
            ):
        """
        Reads raw data from PROBER.

        We read until we have stored up to  number_of_frames+1 frames to make sure we dont stop
        prematurely, but return only number_of_frames frames. The extra frames are currently lost,
        so we will lose data even if we call `read_raw_frames()` repetitively.


        Parameters:

            stream_ids (list of int): List of the stream IDs of the ADC channels to capture. Stream
                IDs consist are values encoded as ``0b0000ccccssssnnnn`` where ``cccc`` is the crate
                number (default to 0), ``ssss`` is the slot number (defaults to 0), and ``nnnn`` is
                the channel number. Specify ``range(16)`` to capture all 16 channels of a single stand-alone board.

            flush (bool): Indicates if the UDP system buffers should be flushed before capture. Set
                to False if the on subsequent calls to this function if you want to continuously
                capture packets bursts, assuming you call it often enough to prevent the system buffers to overflow.

            data_timeout (float): Amout of time we wait for packets until we decide it's time to
                process what we have acumulated in the buffer. This should be set to a delay smaller
                that the dely between bursts if we want to have each burst processed immediately. If
                the value is too big, we'll process data only when the buffer is full, which will
                still work but might cause the user to wait unnecessarily.

            flush_timeout (float): Amount of time we wait for packets before we decide there is no
                more data in the system's UDP buffers. This should be much shorter than the delay
                between packet transmission, otherwise we'll flush forever.

            number_of_frames (int): number of frames to capture for each channel. Of 0 or None, only
                a single frame will be captured, and the "frame number" dimension will be removed
                from the returned arrays.

            verbose (int): verbosity level.

        Returns:
                (times_stamp, data, data_count) where:

                if ``number_of_frames`` is 0 or `None`:

                - time_stamp is an integer  indicating frame number tat was received.
                - data is a numpy array of dimention (NS, 2048) where NS is the number of stream IDs
                - data count is a numpy vector indicating the number of frames received for each stream ID.

                otherwise:

                - time_stamp is a numpy vector of length `number of frames` indicating the value of each timestamp.
                - data is a numpy array of dimention (NT, NS, 2048) where NT=`number of frames` and NS is the number of stream IDs
                - data count is a numpy array indicating the number of frames received for each stream ID.

        """
        sid_map = {sid:ix for ix, sid in enumerate(stream_ids)} # maps stream ID to a stream id slot
        ts_map = {}  # map of (timestamp:ix) that keep track in which timestamp slot the data with a give timestamp should go

        NF = number_of_frames or 1
        NS = len(sid_map)

        self.data = np.zeros((NS, NF + 1, self.DATA_SIZE), dtype=np.int8)
        self.data_count = np.zeros((NS, NF + 1), dtype=np.int32)
        self.ts = np.zeros((NF + 1,), dtype=np.int64)
        self.cookie = cookie = 0xa0

        if flush:
            self.flush(flush_timeout, verbose=verbose)
            self.wait_for_new_timestamp(cookie, data_timeout, verbose=verbose)
            if verbose:
                print(f'finished flushing')

        # self.last_ts = None
        self.socket.settimeout(data_timeout)
        tix = 0 # timestamp index
        while tix < NF + 1: # capture packets until we have all the timestamps we need
            self.receive_packets(cookie=cookie, verbose=verbose)  # receive packets until timeout of buffer is full
            # Now process the packets
            for i in range(self.n):
                bix = sid_map.get(self.buf_stream_id[i], None)
                # print(f'Stream id={self.buf_stream_id[i]:04x} => index {bix}')
                if bix is None:
                    continue
                ts = self.buf_ts[i] & self.ts_mask
                tix = ts_map.setdefault(ts, len(ts_map))
                if tix >= NF + 1:
                    break
                if verbose > 1:
                    print(f'Storing {bix=} {tix=} {i=} {ts=}')
                self.ts[tix] = ts
                self.data[bix, tix] = self.buf_data[i]
                self.data_count[bix, tix] += 1
            self.n = 0
        if number_of_frames:
            return self.ts[:NF], self.data[:,:NF,:], self.data_count[:, :NF]
        else:
            return self.ts[0], self.data[:,0,:], self.data_count[:,0]

    def read_raw_frames_double_resolution(self, *args, **kwargs):
        '''
        Thin wrapper around read_raw_frames which then combines adjacent bytes of data into a single 16-bit number
        '''
        ts, data, flags = self.read_raw_frames(*args, **kwargs)
        combined = data[:, ::2].astype(np.int16) * 2**8 + data[:, 1::2].astype(np.uint8)
        return ts, combined, flags