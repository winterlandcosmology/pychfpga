#!/usr/bin/python
# Disable pylint Line too long (=C0301)
# pylint: disable=C0301

"""
CH_DIST.py module
 Implements interface to the channel filter
#
# History:
# 2011-07-12 JFC : Created from test code in chFPGA.py
# 2012-05-29 JFC: Extracted frm ANT.py
# 2012-07-23 JFC: Adapted to new firmware version now part of the correlator block
"""
#import time
import numpy as np
from Module import Module_base, BitField
import logging


class ChanBinSel(Module_base):
    """ Implements interface to the FR_DIST within a procecessor pipeline"""
    # Create local variables for page numbers to make the bitfield table more readable
    CONTROL = BitField.CONTROL
    STATUS = BitField.STATUS

    # Control bitfields
    RESET                    = BitField(CONTROL, 0x00, 7, doc="Reset the CH_DIST. Clears FIFO.")
    FOUR_BITS                = BitField(CONTROL, 0x00, 6, doc="When '1', input data is assumed to be four bits only and the output words are repacked accordingly (4 complex numbers per word).")
    USE_OFFSET_BINARY        = BitField(CONTROL, 0x00, 5, doc="When '1', indicates that the data uses offset binary encoding instead of 2's complement. Does not affect any processing here, but the flag is passed in the frame header.")
    SEND_FLAGS               = BitField(CONTROL, 0x00, 4, doc="When '1', the scaler and ADC/FFT flags are appended to the end of the data packet")
    OVERFLOW_RESET           = BitField(CONTROL, 0x00, 3, doc="When '1', both bins coming out of the FFT are always selected simultaneously, allowing all the data from a channelizer to be packed into a single GPU lane. '0' is the default. ")
    GROUP_FRAMES             = BitField(CONTROL, 0x00, 0, width=3, doc="Number of input frames to pack into an output frames. ")

    STREAM_ID                = BitField(CONTROL, 0x02, 4, width=12, doc="Stream ID to be used for tagging the output frames")
    NUMBER_OF_SELECTED_WORDS = BitField(CONTROL, 0x03, 0, width=11, doc="Number of words(frequency pairs) selected by this correlator.  Must match length of selected words")
    BYPASS                   = BitField(CONTROL, 0x04, 5, doc="If '1', sends the raw data from the channel with the same number, with header but no flags.")
    COMBINE_DATA_FLAGS       = BitField(CONTROL, 0x04, 4, doc="If '1', The data flags from two adjacent bins are combined into a single word.")
    FIRST_FIFO_NUMBER        = BitField(CONTROL, 0x04, 2, width=2, doc="First FIFO (i.e. group of 4 inputs) to transmit data from. Ranges from 0 to 3.")
    LAST_FIFO_NUMBER         = BitField(CONTROL, 0x04, 0, width=2, doc="Last FIFO (i.e. group of 4 inputs) to transmit data from. Ranges from 0 to 3.")
    # NUMBER_OF_LANES          = BitField(CONTROL, 0x04, 0, width=5, doc="Number of lanes to include in output")
    # LAST_INPUT               = BitField(CONTROL, 0x05, 4, width=0, doc="Index of the last channelizer to get data from ")

    # Status bitfields
    FIFO_EMPTY               = BitField(STATUS, 0x00, 7, doc="Active high when the data FIFO is empty")
    FIFO_OVERFLOW            = BitField(STATUS, 0x00, 6, doc="'1' if the data FIFO is overflowing (sticky, cleared by OVERFLOW_RESET=1)")
    IS_RESET                 = BitField(STATUS, 0x00, 5, doc="High when the module reset line is active")
    EIGHT_BIT_SUPPORT        = BitField(STATUS, 0x00, 4, doc="'1' when the module supports (8+8) bit operation")
    FRAME_PER_PACKET_CTR     = BitField(STATUS, 0x00, 2, width=2, doc="Currently processed frame in the packet")
    DATA_FLAGS_OVERFLOW      = BitField(STATUS, 0x00, 1, doc="'1' if the data flag FIFO (scaler flags) is overflowing (sticky, cleared by OVERFLOW_RESET=1)")
    FRAME_FIFO_OVERFLOW      = BitField(STATUS, 0x00, 0, doc="'1' if the frame flag FIFO (ADC flags) is overflowing (sticky, cleared by OVERFLOW_RESET=1)")

    TIMESTAMP_CTR            = BitField(STATUS, 0x01, 0, width=8, doc="Last 8 bits of the current timestamp.")
    IN_FRAME_CTR             = BitField(STATUS, 0x02, 0, width=8, doc="Number of frames received. Rolls over.")

    # INPUT_CTR                = BitField(STATUS, 0x04, 0, width=4, doc="Debug")
    # SCALER_FLAG_FIFO_OVERFLOW= BitField(STATUS, 0x04, 4, doc="Debug")
    # ADC_FLAG_FIFO_OVERFLOW   = BitField(STATUS, 0x04, 5, doc="Debug")
    # DATA_FIFO_RD_EN          = BitField(STATUS, 0x04, 6, doc="Debug")


    def __init__(self, fpga_instance, base_address, instance_number):
        # self.parent = parent
        # self.fpga = fpga_instance
        super(ChanBinSel, self).__init__(fpga_instance, base_address, instance_number)
        self.logger = logging.getLogger(__name__)
        self.NUMBER_OF_CROSSBAR_INPUTS = self.fpga.NUMBER_OF_CROSSBAR_INPUTS
        self.NUMBER_OF_CROSSBAR_OUTPUTS = self.fpga.NUMBER_OF_CROSSBAR_OUTPUTS
        self.cached_bin_select_table = None
        self._lock()
    def reset(self):
        """Performs the soft reset of the CH_DIST module."""
        self.RESET = 1
        self.RESET = 0

    # def select_words(self, words_to_enable):
    #     """
    #     Selects which frequency channels are going to be passed to this correlator.
    #     A correlator normally process only a subset of the frequency channels because it receives those channels from all antennas but it has a limited computational bandwidth.
    #     The correlation of all frequency channels is therefore usually spread over many correlator blocks, each processing a different range of frequency channels.

    #     Due to the internal architecture of the system, the frequency channels are selected in pairs: an even and odd bin.
    #     Each pair is contained in a 32-bit word. This function selects which word to transmit.

    #     In order to deal with a decreased buffer size, the number of contiguous words has been decreased to 16.  Default behavior
    #     should be to have every Nth word selected where N is the number of antennas to be correlated.

    #     If 'words_to_enable' is an integer, words 0 to (words_to_enable-1) are transmitted. (i.e channels 0 to 2*words_to_enable-1 are selected )
    #         select_words(4) selects words 0,1,2 and 3. and freq channels [0,1,2,3,4,5,6,7]
    #     If 'words_to_enable' is an array, the word numbers indicated in the arrays are selected.
    #         select_words([0,1,2,3]) selects words 0,1,2 and 3. and freq channels [0,1,2,3,4,5,6,7]

    #     If the FFT is bypassed, each word contains 4 8-bit ADC samples instead of a pair of frequency channels.
    #     """
    #     # Initialize filter mask (8 flags per word)
    #     mask = np.zeros(self.fpga.FRAME_LENGTH/4/8, np.uint8) # frequency_bins_per_frame (FRAME_LENGTH/2) * words_per_frequency_bins (1/2) * mask_byte_per_word (1/8)

    #     if isinstance(words_to_enable, int):
    #         words_to_enable = range(words_to_enable)

    #     # Set the bits in mask
    #     for j in words_to_enable:
    #         #print 'setting bit %i of byte %i' % ((j % 8), j//8)
    #         mask[j//8] |= (1<<(j % 8))
    #     # verbose = False
    #     # if verbose: print (words_to_enable)
    #     self.logger.debug('Configuring lane %i of the crossbar to capture the following frequency bin pairs: %s' %(self.instance_number, repr(words_to_enable)))
    #     self.NUMBER_OF_SELECTED_WORDS = len(words_to_enable)

    #     self.write_ram(0x00, mask) # Enable transmission of selected bytes

    def select_bins(self, bins_to_enable):
        """
        Selects which frequency bins are going to be passed to this laner.

        Default behavior is to have every Nth bin selected where N is the number of crossbar inputs.

        If 'bins_to_enable' is an integer, words 0 to (bins_to_enable-1) are transmitted. (i.e channels 0 to 2*bins_to_enable-1 are selected )
            select_words(4) selects words 0,1,2 and 3. and freq channels [0,1,2,3,4,5,6,7]
        If 'bins_to_enable' is an array, the word numbers indicated in the arrays are selected.
            select_words([0,1,2,3]) selects words 0,1,2 and 3. and freq channels [0,1,2,3,4,5,6,7]

        If the FFT is bypassed, each word contains 4 8-bit ADC samples instead of a pair of frequency channels.
        """

        if isinstance(bins_to_enable, int):
            bins_to_enable = range(bins_to_enable)

        # try this:
        # bin_map = zeros(1024, np.uint8)
        # bin_map[bins_to_enable] = 1
        # mask = np.packbits(bin_map[::-1])[::-1]

        # Initialize filter mask (8 flags per word)
        mask = np.zeros(self.fpga.FRAME_LENGTH/2/8, np.uint8) # frequency_bins_per_frame (FRAME_LENGTH/2) *  mask_byte_per_word (1/8)
        # Set the bits in mask
        for j in bins_to_enable:
            #print 'setting bit %i of byte %i' % ((j % 8), j//8)
            mask[j // 8] |= (1 << (j % 8))
        # verbose = False
        # if verbose: print (bins_to_enable)
        self.logger.info('%.32r: BIN_SEL: Configuring lane %i of the crossbar to capture %i frequency bins: %s' % (self.fpga, self.instance_number, len(bins_to_enable), repr(bins_to_enable)))
        # self.logger.debug('Mask pattern is: %s' % ( ' '.join('%02X'% byte for byte in mask)))
        self.NUMBER_OF_SELECTED_WORDS = len(bins_to_enable)

        self.cached_bin_select_table = mask
        self.write_ram(0x00, mask) # Enable transmission of selected bytes

    def set_selected_bins(self, bins_to_enable):
        self.select_bits(bins_to_enable)

    def get_selected_bins(self, use_cache=True):

        if use_cache and self.cached_bin_select_table is not None:
            mask = self.cached_bin_select_table
        else:
            mask = self.read_ram(0x00, length =self.fpga.FRAME_LENGTH/2/8)

        bin_map = np.unpackbits(mask[::-1])[::-1]
        return np.where(bin_map)

    def init(self):
        """ Initializes CH_DIST."""
        #self.select_words(self.fpga.FRAME_LENGTH//4) # enable tranmission of all words by default
        #array doesn't seem to work here....
#        frequency_bins_per_correlator = 124 # 202-5chan correlator # must be even, max 1010 / number of correlated antennas 124-8 channel.  Should get this from config
        #self.select_words(range(words_per_correlator)) # enable tranmission 8 words, 16 freq channels by default
        self.COMBINE_DATA_FLAGS = 0

    def status(self):
        """Displays the status of CH_DIST."""
        self.logger.debug('--- CROSSBAR.CH_DIST[%i] STATUS' % (self.instance_number))
        self.logger.debug('   RESET: %i' % self.RESET)
        self.logger.debug('   FIFO EMPTY: %i' % self.FIFO_EMPTY)
        self.logger.debug('   FIFO OVERFLOW: %i' % self.FIFO_OVERFLOW)

    def get_sim_output(self, input_lanes):
        """ Compute the channelizer bin selector output packets.

        ``chan_outputs`` is an array of frame arrays (one frame array per input lane). A frame array must have at least ``frames_per_packet`` frames.
        """

        number_of_lanes = len(input_lanes)
        if number_of_lanes != self.NUMBER_OF_CROSSBAR_INPUTS:
            raise ValueError('The number of input lanes does not match the crossbar configuration')

        input_lane_shapes = set(fa.shape for fa in input_lanes)
        if len(input_lane_shapes) == 1:
            (number_of_frames, words_per_frame) = input_lane_shapes.pop()
        else:
            raise ValueError('All input lanes must have the same number of frames and the same frame size')

        frames_per_packet = self.GROUP_FRAMES
        if number_of_frames % frames_per_packet:
            raise ValueError('The input lanes must have a multiple of frames_per_packet')

        bypass = self.BYPASS
        lane_number = self.instance_number
        # Build the header words
        for frame_number in range(number_of_frames/4):
            header_words = np.zeros(4, int)
            header_words[0] = 0x000014CF | (self.STREAM_ID << 20) | (lane_number << 16)
            header_words[1] = (self.FOUR_BITS << 31) | (self.USE_OFFSET_BINARY << 30) | (self.SEND_FLAGS << 29) | (self.SYPASS << 28) | (frames_per_packet << 24) | (self.NUMBER_OF_SELECTED_WORDS << 12) | (self.NUMBER_OF_LANES <<0 )
            header_words[2] = 0
            header_words[3] = frame_number

            if bypass:
                data = np.concat(input_lanes[lane_number][frame_number: frame_number+4])
                return np.concat((header_words, data))

            selected_bins = self.get_selected_bins()
            RuntimeError('Channelizer Bin selector non-bypass mode is not supported yet')