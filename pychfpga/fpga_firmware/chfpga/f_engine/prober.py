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
    _OVERRUN = BitField(STATUS, 0x00, 1, doc="High if a new burst was requested before the previous burst finished readout")

    TRIG_CTR = BitField(STATUS, 0x01, 0, width=8, doc="Number of frames")

    DATA_BUFFER_CAPACITY = 3 # Number of full frames that can fit in the FIFOs.

    def __init__(self,  *, router, router_port, instance_number, config):
        self.logger = logging.getLogger(__name__)
        super().__init__(router=router, router_port=router_port, instance_number=instance_number)
        self.config = config
        self._lock()  # Prevent accidental addition of attributes (if, for example, a value is assigned to a wrongly-spelled property)
    # Specialized functions

    def reset(self):
        """ Resets the module"""
        self.pulse_bit('RESET')

    def reset_fifo(self):
        """Deprecated. RTL has no separate FIFO reset; a module RESET would have side effects."""
        self.logger.warning(f'{self!r}: reset_fifo() is deprecated and has no effect (no separate FIFO reset in firmware)')

    DATA_SOURCE_TABLE = {
        'funcgen': 0,
        'scaler': 1}
    DATA_SOURCE_NAMES_TABLE = {v:k for k,v in DATA_SOURCE_TABLE.items()}  # maps source number to name

    @classmethod
    def normalize_source(cls, source):
        """Return ``(source_index, source_name)`` for a capture source specifier."""
        if isinstance(source, str):
            if source == 'adc':
                source = 'funcgen'
            if source not in cls.DATA_SOURCE_TABLE:
                valid_sources = ', '.join(f"'{key}'" for key in cls.DATA_SOURCE_TABLE)
                raise ValueError(f"Unknown data capture '{source}'. Valid sources are {valid_sources}.")
            return cls.DATA_SOURCE_TABLE[source], source
        source_index = int(source)
        if source_index not in cls.DATA_SOURCE_NAMES_TABLE:
            raise ValueError(
                f"Unknown data capture source index {source_index}. Valid indices are 0 (funcgen) or 1 (scaler)"
            )
        return source_index, cls.DATA_SOURCE_NAMES_TABLE[source_index]

    def set_data_source(self, source):
        source_index, source_name = self.normalize_source(source)
        self.SOURCE_SEL = source_index
        self.config.update(source=source_index, source_name=source_name)

    def get_data_source(self):
        return self.config.source_name

    def set_sub_period(self, sub_period):
        self.SUB_PERIOD = int(sub_period)
        self.config.sub_period = int(sub_period)

    def get_sub_period(self):
        return int(self.config.sub_period)

    def set_burst_period(self, burst_period0=390625,   burst_period1=390625):
        """ Sets the interval between data capture bursts. The period is specified in number of frames. This method is used because the property does not yet handle multi-byte values well."""
        self.PERIOD0 = burst_period0 - 1
        self.PERIOD1 = burst_period1 - 1

    def get_burst_period(self):
        """ Returns the interval between data capture bursts. The period is specified in number of frames. This method is used because the property does not yet handle multi-byte values well."""
        return [self.PERIOD0, self.PERIOD1]

    def config_capture(self, frames_per_burst=1, burst_period=100, offset=0, send_delay=0):
        """
        Configure the capture of data frames for transmisssion over the ethernet link.

        Parameters:

            frames_per_burst (int): number of continuous frames to send in a
                burst (default=1)

            burst_period (int): delay between bursts in seconds

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
        if not 1 <= int(frames_per_burst) <= 4:
            raise ValueError(
                f"frames_per_burst must be in range [1, 4] for PROBER hardware (2-bit BURST_LENGTH), "
                f"got {frames_per_burst}."
            )

        # frame_period=1.0/850e6*self.ant.frame_length
        # burst_period=int(period/frame_period)
        if burst_period < frames_per_burst:
            burst_period = frames_per_burst
        if burst_period >= 2**24:
            raise RuntimeError(f'Capture period of {int(burst_period)} frames is too long. Maximum value is {2 ** 24 - 1:.3f} s')

        delay_increment = 65536 / 125e6 # 0x10000 / 125 MHz
        delay_in_seconds = delay_increment * send_delay # Data transmission of new frames is held off by this amount
        period_in_seconds = self.fpga.FRAME_PERIOD * burst_period
        min_period = int(delay_in_seconds / self.fpga.FRAME_PERIOD / frames_per_burst)
        min_period_in_seconds = min_period * self.fpga.FRAME_PERIOD
        buffered_packets = frames_per_burst * np.ceil(delay_in_seconds / period_in_seconds)
        if buffered_packets > self.DATA_BUFFER_CAPACITY:
            raise RuntimeError(f'Capture send delay of {delay_in_seconds:.3f} s in combination with the capture period of {period_in_seconds:.3f} s will cause up to {int(buffered_packets)} packets to be buffered, wich exceed the capture buffer capability of {int(self.DATA_BUFFER_CAPACITY)} frames. Minimum period for the current send delay is {int(min_period)} frames ({min_period_in_seconds * 1000:.3f} ms)')

        self.BURST_LENGTH = frames_per_burst - 1 # BURST_LENGTH actually specifies the number of additional frames in the firmware
        self.config.frames_per_burst = frames_per_burst
        self.set_burst_period(burst_period, burst_period)
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

            stream_id = self.STREAM_ID = ((crate & 0xF) << 8) | ((slot & 0xF) << 4) | (channel & 0x0F)
        else:
            raise ValueError(f'Invalid stream id parameter {stream_id!r}')
        self.config.stream_id = stream_id

    def get_stream_id(self):
        """ Return the stream ID of the current channel instance.

        Returns:
            int: the stream id.

        """
        return self.config.stream_id


    def init(self, **kwargs):
        """ Initialize the data capture module"""
        source = int(self.SOURCE_SEL)
        source_name = self.DATA_SOURCE_NAMES_TABLE[source]
        self.config.update(
            source=source,
            source_name=source_name,
            sub_period=int(self.SUB_PERIOD),
            subframes_per_frame=1, # hardwired in PROBER
            frames_per_burst=1, # default for PROBER
        )
        channel = self.instance_number
        self.logger.debug(f'{self!r}: Initializing channel {int(channel)}')
        self.set_stream_id(self.fpga.get_id(channel)) # get crate/slot/channel tuple, it will be converted into a numeric stream id
        self.RESET = 1  # Make sure no data is being transmitted at reset

    def update_config(self):
        """Return ``self.config``."""
        return self.config

    def status(self):
        """ Displays the status of the data capture module"""
        print(f'-------------- CHAN[{int(self.instance_number)}] data capture --------------')
        print(f' Capture frame({self.BURST_LENGTH}) every {self.get_burst_period()} frames', end=' ')

