#!/usr/bin/python

"""
SHUFFLE_BIN_SEL.py module

Implements an interface to the shuffle bin selector (used in the shuffle
crossbars, not to be confused with the channel bin selector used in the
channel crossbar)

History:

    2014-09-01 JFC : Created from CH_DIST.py
"""

import numpy as np
from collections import OrderedDict

from ..mmi import MMI, BitField
import logging


class SHUFFLE_BIN_SEL_base(MMI):
    """ Implements an interface to the SHUFFLE_BIN_SEL FPGA module"""

    ADDRESS_WIDTH = 9 # 19-3-2-5

    # Create local variables for page numbers to make the bitfield table more readable
    CONTROL = BitField.CONTROL
    STATUS = BitField.STATUS

    # Control bitfields
    RESET                       = BitField(CONTROL, 0, 7, doc="Reset the CH_DIST. Clears FIFO.")
    BYPASS                      = BitField(CONTROL, 0, 6, doc="When high, routes input lane 'x' directly to the output, where x in the index of this bin selector.")
    NUMBER_OF_DATA_FLAGS_WORDS_PER_BIN = BitField(CONTROL, 0, 4, width=2, doc="Number of data flags words in each incoming bin and frame")
    FIRST_LANE                  = BitField(CONTROL, 0, 0, width=4, doc="Index of the first lane to be sent out")
    # HEADER_CAPTURE_DATA_SEL     = BitField(CONTROL, 0, 5, doc=" Select whether we capture Stream ID or timestamps.")

    STREAM_ID                   = BitField(CONTROL, 2, 4, width=12, doc="Stream ID to be used for tagging the output frames")
    ZERO_PACKET_FLAGS           = BitField(CONTROL, 2, 3, doc="When high, packets plags are forced to zero.")
    NUMBER_OF_FRAME_FLAGS_WORDS_PER_FRAME = BitField(CONTROL, 2, 0, width=3, doc="Number of frame flags words in each incoming frame")
    NUMBER_OF_BINS_PER_FRAME    = BitField(CONTROL, 3, 0, width=8, doc="Number of bins expected in each incoming frame")
    FIFO_OVERFLOW_RESET         = BitField(CONTROL, 4, 7, doc="When high, resets the FIFO OVERFLOW flag.")
    NUMBER_OF_WORDS_PER_BIN     = BitField(CONTROL, 4, 0, width=7, doc="Number of words expected in each bin of the incoming frames. In 4-bit mode, 1 Word = 4 analog channels")
    NUMBER_OF_FRAMES_PER_PACKET = BitField(CONTROL, 5, 5, width=3, doc="Number of expected frames per packet. ")
    LAST_LANE                   = BitField(CONTROL, 5, 0, width=4, doc="Index of the last lane to be transmitted")

    NUMBER_OF_OUTPUT_WORDS_PER_BIN = BitField(CONTROL, 6, 0, width=8, doc="Number of words generated for each bin of the transmitted frames. Is used only to populate the outgoing packet header and does not affect the actual data.")

    FOUR_BITS                   = BitField(CONTROL, 7, 7, doc="1=four bit mode, 0= 8 bit mode. Is used only to populate the outgoing packet header and does not affect the actual data. ")
    USE_OFFSET_BINARY           = BitField(CONTROL, 7, 6, doc="Is used only to populate the outgoing packet header and does not affect the actual data. ")
    SEND_FLAGS                  = BitField(CONTROL, 7, 5, doc="Is used only to populate the outgoing packet header and does not affect the actual data. ")
    NUMBER_OF_OUTPUT_BINS_PER_FRAME = BitField(CONTROL, 7, 0, width=5, doc="Number of bins in each outgoing frame. Is used only to populate the outgoing packet header and does not affect the actual data.")


    # Status bitfields
    FIFO_EMPTY               = BitField(STATUS, 0, 7, doc="Active high when the data FIFO is empty")
    FLAGS_FIFO_OVERFLOW      = BitField(STATUS, 0, 6, doc="Active high if the flags FIFO has overflowed since the last time the flag was cleared with FIFO_OVERFLOW_RESET")
    IS_RESET                 = BitField(STATUS, 0, 5, doc="High when the module reset line is active")
    COMBINE_DATA_FLAGS       = BitField(STATUS, 0, 4, doc="Active high if this crossbar is configured to pack the data flags two by two. This is used for the 2nd crossbar, where the incoming data flags occupy only 16 bits of the words.")
    NUMBER_OF_OUTPUTS        = BitField(STATUS, 0, 0, width=4, doc="")

    FIFO_OVERFLOW             = BitField(STATUS, 2, 0, width=16, doc="Active high if any fo the data FIFO has overflowed since the last time the flag was cleared with FIFO_OVERFLOW_RESET")
    # TIMESTAMP_CAPTURE        = BitField(STATUS, 4, 0, width=16, doc="")
    # IN_FRAME_CTR             = BitField(STATUS, 0x02, 0, width=8, doc="Number of frames received on lane 0 before the alignment FIFOs. Rolls over.")
    # LANE_CTR                = BitField(STATUS, 0x04, 0, width=4, doc="Debug")

    def __init__(self, fpga_instance, base_address, instance_number, crossbar_level):
        # self.parent = parent
        # self.fpga = fpga_instance
        self.crossbar_level = crossbar_level
        super().__init__(fpga_instance, base_address, instance_number)
        self.logger = logging.getLogger(__name__)
        self.NUMBER_OF_INPUTS = None
        self.cached_bin_select_table = None
        self._lock()

    def init(self, number_of_inputs):
        """ Initializes SHUFFLE_BIN_SEL"""
        self.NUMBER_OF_INPUTS = number_of_inputs
        self.NUMBER_OF_FRAMES_PER_PACKET = 4
        self.NUMBER_OF_WORDS_PER_BIN = 4
        self.NUMBER_OF_BINS_PER_FRAME = 8
        self.FIRST_LANE = 0
        self.LAST_LANE = 15
        self.ZERO_PACKET_FLAGS = 0  # set to 1 to force the packets flags to zero

    def reset(self):
        """Performs the soft reset of the CH_DIST module."""
        self.RESET = 1
        self.RESET = 0

    def select_bins(self, bins_to_enable):
        """
        Selects which frequency bins are going to be passed to the output.


        Parameters:

            bins_to_enable (int or ndarray):

                If 'bins_to_enable' is an integer, words 0 to
                (bins_to_enable-1) are transmitted. (i.e channels 0 to
                2*bins_to_enable-1 are selected )

                select_words(4) selects words 0,1,2 and 3. and freq channels [0,1,2,3,4,5,6,7]

                If 'bins_to_enable' is an array, the word numbers indicated in
                the arrays are selected.

                select_words([0,1,2,3]) selects words 0,1,2 and 3. and freq channels [0,1,2,3,4,5,6,7]

        If the FFT is bypassed, each word contains 4 8-bit ADC samples instead of a pair of frequency channels.
        """

        # Initialize bin selection mask
        mask = np.zeros(128, np.uint8)  # 128*8 = up to 1024 bins / frame

        if isinstance(bins_to_enable, int):
            bins_to_enable = list(range(bins_to_enable))

        # Set the bits in mask
        for j in bins_to_enable:
            # print 'setting bit %i of byte %i' % ((j % 8), j//8)
            mask[j // 8] |= (1 << (j % 8))
        # verbose = False
        # if verbose: print (bins_to_enable)
        self.logger.debug('%r: CROSSBAR%i.BIN_SEL[%i] configured to capture %i frequency bins: %s...' % (
            self.fpga,
            self.crossbar_level,
            self.instance_number,
            len(bins_to_enable),
            repr(bins_to_enable[:10])))
        # self.logger.debug('%r: Mask pattern is: %s' % (self.fpga, ' '.join('%02X'% byte for byte in mask)))

        self.cached_bin_select_table = mask
        self.write_ram(0x00, mask)  # Write the bin selection mask array

    def get_selected_bins(self, use_cache=True):

        if use_cache and self.cached_bin_select_table is not None:
            mask = self.cached_bin_select_table
        else:
            mask = self.read_ram(0x00, length=128)  # 128*8 = up to 1024 bins / frame

        bin_map = np.unpackbits(mask[::-1])[::-1]
        return np.where(bin_map)[0]

    def map(self, input_data, header=False):
        """ Return a bin selector frequency map, which describes the structure and contents of the
        bin selector output stream.
        """

        N = self.NUMBER_OF_INPUTS // self.NUMBER_OF_OUTPUTS  # number of input lanes per output
        ch_per_bin = self.NUMBER_OF_WORDS_PER_BIN * 4  # fixme - only 4-bit mode
        bs_out = OrderedDict()
        bins = self.get_selected_bins()
        bypass = self.BYPASS
        # if self.BYPASS:
        #     raise RuntimeError('%r: CHAN_BIN_SEL cannot yet provide maps in BYPASS mode', self)

        # Prepare the header info that does not change as a function of lane
        # to minimize FPGA access
        if header:
            static_header = dict(
                cookie=0xcf,
                protocol_version=1,
                header_length=4,
                stream_id=None,  # to be populated
                four_bits=self.FOUR_BITS,
                use_offset_binary=self.USE_OFFSET_BINARY,
                send_flags=self.SEND_FLAGS,
                bypass=self.BYPASS,
                frames_per_packet=self.NUMBER_OF_FRAMES_PER_PACKET,
                bins_per_frame=self.NUMBER_OF_OUTPUT_BINS_PER_FRAME,
                words_per_bin=self.NUMBER_OF_OUTPUT_WORDS_PER_BIN,
                ancillary=None,
                timestamp=0,
                )
            static_stream_id = (self.STREAM_ID << 4) | self.instance_number * self.NUMBER_OF_OUTPUTS
        else:
            header = None

        for sublane in range(self.NUMBER_OF_OUTPUTS):
            if bypass:
                data = input_data[self.instance_number * self.NUMBER_OF_OUTPUTS + sublane]['data']
            else:
                channels = list(range(N * sublane + self.FIRST_LANE, N * sublane + self.LAST_LANE + 1))
                d = [input_data[ch]['data'][ch_per_bin * bin_number: ch_per_bin * (bin_number + 1)]
                     for bin_number in bins for ch in channels]
                data = [i for di in d for i in di]  # flatten the list of lists,
            if header:
                header = static_header.copy()
                header['stream_id'] = static_stream_id + sublane

            bs_out[sublane] = dict(
                header=header,
                data=data,
                data_flags=None,
                frame_flags=None,
                packet_flags=None
                )
        return bs_out

    def status(self):
        """Displays the status of SHUFFLE_BIN_SEL."""
        self.logger.debug('%r: --- SHUFFLE_BIN_SEL[%i] STATUS' % (self.fpga, self.instance_number))
        self.logger.debug('%r:    RESET: %i' % (self.fpga, self.RESET))
        self.logger.debug('%r:    FIFO EMPTY: %i' % (self.fpga, self.FIFO_EMPTY))
        # self.logger.debug('   FIFO OVERFLOW: %i' % self.FIFO_OVERFLOW)
