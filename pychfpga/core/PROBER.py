#!/usr/bin/python

"""
PROBER.py module
 Implements interface to a data PROBER

 History:
 2012-06-21 JFC: Created
 2012-07-20 JFC: Added initialization of PROBE_ID with antenna number
"""

import logging

from Module import Module_base, BitField


class PROBER_base(Module_base):
    """ Implements the interface to the data PROBER within a channel processor
    """
    # Create local variables for page numbers tomake the table more readable
    CONTROL = BitField.CONTROL
    STATUS = BitField.STATUS

    # Memory-mapped control registers
    RESET = BitField(CONTROL, 0x00, 7, doc="Resets the module (including the FIFO)")
    FIFO_RESET = BitField(CONTROL, 0x00, 6, doc="When '1', resets the data FIFO")
    SOURCE_SEL = BitField(CONTROL, 0x00, 5, doc="0 = source selector output (timestream), 1 = scaler output (spectrum)")
    OFFSET = BitField(CONTROL, 0x00, 0, width=4, doc="Offset for sending data to avoid collisions")
    BURST_LENGTH = BitField(CONTROL, 0x01, 0, width=8, doc="Sets the number of frame to transmit in a burst. 0= Continuous transmission, 1-255 = Trigerred transmission.")
    BURST_PERIOD2 = BitField(CONTROL, 0x02, 0, width=8, doc="8 bit MSB of number of frames between bursts")
    BURST_PERIOD1 = BitField(CONTROL, 0x03, 0, width=8, doc="8 bit middle byte of Number of frames between bursts ")
    BURST_PERIOD0 = BitField(CONTROL, 0x04, 0, width=8, doc="8 bit LSB of number of frames between bursts")
    # BURST_PERIOD = BitField(CONTROL, 0x04, 0, width=32, doc="24 bit  number of frames between bursts. We read 32 bits but have to discard the MSbyte")
    BURST_NUMBER = BitField(CONTROL, 0x05, 0, width=8, doc="Sets the number of bursts to transmit. 0-255, 0= Continuous transmission.")
    PROBE_ID = BitField(CONTROL, 0x06, 0, width=8, doc="8-bit number that is the first byte of the raw data packet. Can be used as a cookie or to encode information from the source")
    STREAM_ID = BitField(CONTROL, 0x08, 0, width=12, doc="Arbitrary 12-bit number that that identifies the source of the data (typically crate/slot/channel numbers)")

    # Memory-mapped status registers
    _TRIG_CTR = BitField(STATUS, 0x01, 0, width=8, doc="Number of frames")
    _CAPTURE_FLAG = BitField(STATUS, 0x00, 7, doc="State of the CAPTURE flag in the incoming frame data (for debugging)")
    _CAPTURE_FRAME = BitField(STATUS, 0x00, 6, doc="State of the CAPTURE_FRAME signal (for debugging)")
    _IN_DAT_FIRST = BitField(STATUS, 0x00, 5, doc="State of the CAPTURE_FRAME signal (for debugging)")
    _DATA_FIFO_EMPTY = BitField(STATUS, 0x00, 4, doc="State of the DATA_FIFO_EMPTY signal (for debugging)")
    _DATA_FIFO_OVERFLOW = BitField(STATUS, 0x00, 3, doc="State of the DATA_FIFO_OVERFLOW signal (for debugging)")
    _DATA_FIFO_OVERFLOW_STICKY = BitField(STATUS, 0x00, 2, doc="State of the DATA_FIFO_OVERFLOW signal, stick to '1' when there us en aeeror until RESET=1 (for debugging)")
    CAPTURE_ACTIVE = BitField(STATUS, 0x00, 0, doc="Active high if data capture is in progress (cleared when BURST_NUMBER bursts have been sent)")

    def __init__(self, fpga_instance, base_address, instance_number):
        # self.ant = ant_instance
        self.logger = logging.getLogger(__name__)
        super(self.__class__, self).__init__(fpga_instance, base_address, instance_number)
        self._lock()  # Prevent accidental addition of attributes (if, for example, a value is assigned to a wrongly-spelled property)
    # Specialized functions

    def reset(self):
        """ Resets the module"""
        self.pulse_bit('RESET')

    def reset_fifo(self):
        """ Clears the data FIFO"""
        self.pulse_bit('FIFO_RESET')

    DATA_SOURCE_TABLE = {
        'adc': 0,
        'scaler': 1}

    def set_data_source(self, source):
        if isinstance(source, str):
            if source in self.DATA_SOURCE_TABLE:
                source = self.DATA_SOURCE_TABLE[source]
            else:
                ValueError("Unknown data capture source '%s'. Valid sources are %s." % (source, ','.join(self.DATA_SOURCE_TABLE.keys())))
        self.SOURCE_SEL = source


    def set_burst_period(self, burst_period):
        """ Sets the interval between data capture bursts. The period is specified in number of frames. This method is used because the property does not yet handle multi-byte values well."""
        self.BURST_PERIOD0 = burst_period & 0xff
        self.BURST_PERIOD1 = (burst_period >> 8) & 0xff
        self.BURST_PERIOD2 = (burst_period >> 16) & 0xff

    def get_burst_period(self):
        """ Returns the interval between data capture bursts. The period is specified in number of frames. This method is used because the property does not yet handle multi-byte values well."""
        return self.BURST_PERIOD0 + (self.BURST_PERIOD1 << 8) + (self.BURST_PERIOD2 << 16)

    def config_capture(self, frames_per_burst=1, burst_period=100, number_of_bursts=0, offset=0):
        """
        Configure the capture of data frames for transmisssion over the ethernet link.

        Parameters:

            frames_per_burst (int): number of continuous frames to send in a burst (default=1)
            burst_period (int): delay between bursts in seconds
            number_of_bursts (int): number of bursts to send. '0' means that bursts are sent continuously as long as frames are tagged for capture at the source . Default is '0'.
            offset (int): sets how many frames are skipped before the capture starts. The actual numbe of skipped frames is `offset` multiplied by a constant.

        """

        # frame_period=1.0/850e6*self.ant.frame_length
        # burst_period=int(period/frame_period)
        if burst_period < frames_per_burst:
            burst_period = frames_per_burst
        if burst_period >= 2**24:
            raise SystemError('Burst period of %i frames is too long. Maximum value is %.3f s' % (burst_period, 2**24-1))
        self.BURST_LENGTH = frames_per_burst
        self.set_burst_period(burst_period)
        self.BURST_NUMBER = number_of_bursts
        self.OFFSET = offset

    def init(self, **kwargs):
        """ Initialize the data capture module"""
        # self.config_capture(1, 100) # Capture 1 frame every 100 frames
        channel = self.instance_number
        slot = (self.fpga.slot or 1) - 1   # 0-based, 0 if no slot
        crate = self.fpga.crate.crate_number or 0 if self.fpga.crate else 0 # 0 if there is no backplane/crate, or the crate does not have an assigned crate number.
        self.logger.debug('PROBER crate=%r, slot=%r, channel=%r' % (crate, slot, channel))
        self.PROBE_ID = 0xA0 + self.instance_number  # For backwards compatibility
        self.STREAM_ID = ((crate & 0xF) << 8) | ((slot & 0xF) << 4) | (channel & 0x0F)
        self.RESET = 1  # Make sure no data is being transmitted at reset

    def status(self):
        """ Displays the status of the data capture module"""
        print '-------------- ANT[%i] data capture --------------' % self.instance_number
        print ' Capture %i frame(s) every %i frames' % (self.BURST_LENGTH, self.get_burst_period()),
        if self.BURST_NUMBER:
            print 'for %i bursts' % self.BURST_NUMBER
        else:
            print 'continuously while the frames are tagged for capture at the source'

