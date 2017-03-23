#!/usr/bin/python
# Disable pylint Line too long (=C0301)
# pylint: disable=C0301

"""
CROSSBAR.py module
 Implements interface to the crossbar.
 The crossbar gets data from all channelizers and provide a number of output streams, each of which contain selected frequency channels from those antennas

 History:
 2013-12-03 : JFC : Created
"""

import logging
import numpy as np

from Module import Module_base, BitField
import chan_bin_sel

class ChanCrossbar(Module_base):
    """ Object that allows access to a channelizer crossbar"""

    CONTROL = BitField.CONTROL
    STATUS = BitField.STATUS

    ALIGN_RESET        = BitField(CONTROL, 0, 6, doc='')
    LANE_MONITOR_RESET = BitField(CONTROL, 0, 4, doc='')
    LANE_MONITOR_SEL   = BitField(CONTROL, 0, 0, width=4, doc='')

    LANE_MONITOR_SOURCE   = BitField(CONTROL, 1, 0, width=4, doc='')
    # SOF_WINDOW_START   = BitField(CONTROL, 1, 0, width=8, doc='')
    # SOF_WINDOW_STOP    = BitField(CONTROL, 2, 0, width=8, doc='')

    RESET_MON           = BitField(STATUS, 0, 0, doc='')
    ALIGN_FIFO_OVERFLOW = BitField(STATUS, 0, 1, doc='')

    # BIN_CTR            = BitField(STATUS, 2, 0, width=8, doc='')
    INPUT_FRAME_CTR    = BitField(STATUS, 1, 0, width=8, doc='')
    ALIGN_FRAME_CTR    = BitField(STATUS, 2, 0, width=8, doc='')
    # ALIGN_GLOBAL_FRAME_CTR    = BitField(STATUS, 5, 0, width=8, doc='')
    DELAY_CAPTURE    = BitField(STATUS, 4, 0, width=16, doc="")

    def __init__(self, fpga_instance, base_address, address_increment, verbose=0):
        self.fpga = fpga_instance
        self.verbose = verbose
        self.logger = logging.getLogger(__name__)
        self.crossbar_level = 1
        super(ChanCrossbar, self).__init__(fpga_instance, base_address)
        self.BIN_SEL = []
        self.NUMBER_OF_CROSSBAR_INPUTS = self.fpga.NUMBER_OF_CROSSBAR_INPUTS
        self.NUMBER_OF_CROSSBAR_OUTPUTS = self.fpga.NUMBER_OF_CROSSBAR_OUTPUTS
        for i in range(self.fpga.NUMBER_OF_CROSSBAR_OUTPUTS):
            self.BIN_SEL.append(chan_bin_sel.ChanBinSel(fpga_instance, base_address + (i+1) * address_increment, i))

    def __getitem__(self, key):
        """    Returns the bin selector instance specified by the key"""
        return self.BIN_SEL[key]

    def init(self):
        """ Initializes all correlators"""
        for bs in self.BIN_SEL:
            bs.init()

        # Default bin selector configuration. To be overriden by system-level configuration method.
        # By default we send 64 bins for each of the 16 output lanes. Bins are interleaved.
        number_of_bins_per_crossbar_output = 64
        # Do not initialize the bin selection map now to same time. This will be done anyway when we initialize the shuffling system.
        # for (i, bs) in enumerate(self.BIN_SEL):
        #     bin_list = np.arange(number_of_bins_per_crossbar_output) * 16 + i
        #     bs.select_bins(bin_list)

    def set_data_width(self, width):
        """
        Sets the number of bits expected at the input of the crossbar.
        All crossbars are set to the new setting.
            width=4: data is 4 bits Real + 4 bits Imaginary
            width=8: data is 8 bits Real + 8 bits Imaginary
        """

        if width==4:
            is_four_bits = 1
        elif width == 8:
            is_four_bits = 0
        else:
            raise ValueError('Number of bits %i is invalid for the channelizers. Only 4 or 8 is allowed' % width)

        # Set the channelizer data width
        for bs in self.BIN_SEL:
            bs.FOUR_BITS = is_four_bits

    def get_data_width(self):
        """
        Returns number of bits used by the crossbar.
        If all the crossbar sub-units  are not set in the same mode, an error is raised.
        """
        if not self.BIN_SEL:
            return None

        four_bits = {bs.FOUR_BITS for bs in self.BIN_SEL}  # use a set to uniquely record all the possible encountered states

        if four_bits == {0}:
            return 8
        elif four_bits == {1}:
            return 4
        else:
            raise ValueError("The crossbars are not all set to the same data width.")

    def set_frames_per_packet(self, group_size):
        """
        Set the number of frame per packets.
        """
        for bs in self.BIN_SEL:
            bs.GROUP_FRAMES = group_size

    def get_frames_per_packet(self):
        """
        Return the number of frames per packets.
        """
        return self.BIN_SEL[0].GROUP_FRAMES


#     def configure(self, number_of_bins_per_crossbar_output= 8):
#         """
#         Configure the channel selection.
#         This should be done once the data width has been selected.
#         """
#         # data_width = self.get_data_width()
#         # # Compute the minimum word spacing to allow the channel_selector time to forwared the data.
#         # # In 8-bit mode, 2*N bins come every clock from the channelizers, and it takes N clocks to send them away (2 per output word). So the bin spacing is N.
#         # # In 4-bit mode, 2*N bins come every clock from the channelizers, and it takes N/2 clocks to send them away ( 4 per output word). So bin spacing is N/2.
#         # bin_step = self.fpga.NUMBER_OF_CROSSBAR_INPUTS * data_width / 8
#         # number_of_bins_per_frame = self.fpga.FRAME_LENGTH / 2 # The FFT generates 2048 bins, but half of them are discarded
#         # #number_of_bins_per_frame = 2 # The FFT generates 2048 bins, but half of them are discarded
#         # if number_of_bins_per_crossbar_output is None:
#         #     number_of_bins_per_crossbar_output =  int (number_of_bins_per_frame / bin_step) # Number of channels that one channel selector can handle

#         # if bin_step > self.fpga.NUMBER_OF_CROSSBAR_OUTPUTS:
#         #     self.logger.warning('   Only a fraction of the frequency bins can be mapped to the crossbar outputs because the total number of bits entering the crossbar exceeds the number of bits at its outputs.')

#         # # Check if the set-up is acceptable for the FPGA correlator (if present in the FPGA), and make corrections if needed
#         # if self.fpga.NUMBER_OF_CORRELATORS:
#         #     if data_width == 4:
#         #         self.logger.warning('The FPGA correlator will not operate properly in 4-bit mode ')

#         #     max_correlator_frame_length_in_words = 511 # maximum number of words that the correlator can handle in a frame. This is limited by the ACCumulator buffer depth
#         #     max_number_of_words_per_correlator = int( max_correlator_frame_length_in_words / self.fpga.NUMBER_OF_ANTENNAS_TO_CORRELATE ) # maximum number of words that can be selected
#         #     if number_of_bins_per_crossbar_output > 2*max_number_of_words_per_correlator:
#         #         self.logger.warning('   The number of frequency bins in each crossbar output was reduced from %i to %i due to the correlator accumulator memory limitation' % (number_of_bins_per_crossbar_output, max_number_of_words_per_correlator))
#         #         number_of_bins_per_crossbar_output = 2*max_number_of_words_per_correlator

#         # # Apply GPU Link limitations
#         # if self.fpga.NUMBER_OF_GPU_LINKS:
#         #     max_number_of_words_per_input_frame = 4095 // self.get_frame_grouping() * 8 / data_width / self.fpga.NUMBER_OF_CROSSBAR_INPUTS
#         #     if number_of_bins_per_crossbar_output > 2*max_number_of_words_per_input_frame:
#         #         self.logger.warning('   The number of frequency bins in each crossbar output was reduced from %i to %i due to the GPU link buffer size limitations' % (number_of_bins_per_crossbar_output, max_number_of_words_per_input_frame))
#         #         number_of_bins_per_crossbar_output = 2*max_number_of_words_per_input_frame

# #         for (i, xbar) in enumerate(self.CROSSBAR):
# #             bin_list = np.arange(number_of_bins_per_crossbar_output)* bin_step + i
# #             # xbar.CH_DIST.select_words(word_list) # enable tranmission 8 words, 16 freq channels by default
# # #            bin_list = [0,8]
# #             xbar.CH_DIST.select_bins(bin_list) # enable tranmission 8 words, 16 freq channels by default
#         for (i, bs) in enumerate(self.BIN_SEL):
#             if self.crossbar_level==1:
#                 bin_list = np.arange(number_of_bins_per_crossbar_output)* 2 + i
#             else:
#                 bin_list = np.arange(number_of_bins_per_crossbar_output) * 8 + i
#             # xbar.CH_DIST.select_words(word_list) # enable tranmission 8 words, 16 freq channels by default
# #            bin_list = [0,8]
#             bs.select_bins(bin_list) # enable tranmission 8 words, 16 freq channels by default

    def status(self):
        """ Displays the status of all bin selectors"""
        for bs in self.BIN_SEL:
            bs.status()

    CB1_LANE_MONITOR_TABLE = {
        'RESET': 0,
        'MISSING_FRAME': 4,
        'ALIGN_FIFO_OVERFLOW' : 6,
        }

    def get_lane_monitor(self, name):
        """
        Return a list describing the status of the specified flag for each
        lane.
        """
        table = self.CB1_LANE_MONITOR_TABLE

        if name not in table:
            raise ValueError('Invalid lane monitor name. valid names are %s' % ','.join(table.keys()))
        ix = table[name]
        self.LANE_MONITOR_SEL = ix
        value = self.LANE_MONITOR
        return [bool(value & (1 << bit)) for bit in range(16)]

    def print_lane_monitor(self, reset=True):

        if reset:
            self.LANE_MONITOR_RESET = 1
            self.LANE_MONITOR_RESET = 0

        lane_range = range(self.NUMBER_OF_CROSSBAR_INPUTS)

        print '%20s: %s' % ('Monitor point', ' '.join('  L%2i ' % v for v in lane_range))
        print '%20s: %s' % ('--------------------', ' '+' '.join('------' for v in lane_range))
        input_frame_ctr = []
        align_frame_ctr = []
        reset_mon = []
        align_fifo_overflow = []
        for lane in lane_range:
            self.LANE_MONITOR_SEL = lane
            reset_mon.append(self.RESET_MON)
            align_fifo_overflow.append(self.ALIGN_FIFO_OVERFLOW)
            input_frame_ctr.append(self.INPUT_FRAME_CTR)
            align_frame_ctr.append(self.ALIGN_FRAME_CTR)

        print '%20s: %s' % ('RESET', ' '.join('%6s' % ('-', 'ERR!')[bool(v)] for v in reset_mon))
        print '%20s: %s' % ('ALIGN_FIFO_OVERFLOW', ' '.join('%6s' % ('-', 'ERR!')[bool(v)] for v in align_fifo_overflow))
        print '%20s: %s' % ('INPUT FRAME CTR', ' '.join('%6i' % v for v in input_frame_ctr))
        print '%20s: %s' % ('ALIGN FRAME CTR', ' '.join('%6i' % v for v in align_frame_ctr))
        # print '%20s: %s' % ('ALIGN GLOBAL FRAME CTR', '(common to all lanes) %6i' % self.ALIGN_GLOBAL_FRAME_CTR)

    def map(self, input_data):
        """
        Reorders the input data based on the configuration of the bin selectors.
        input data: {channel_number: [data, ...]}
        output_data: {lane_number: [data, ...]}
        """
        cb_out = {output_lane: bs.map(input_data) for output_lane, bs in enumerate(self.BIN_SEL)}
        return cb_out
        # fmap = {output_lane:bs.get_map() for output_lane, bs in enumerate(self.BIN_SEL)}
        # return fmap

    def get_sim_output(chan_outputs):
        """ Compute the channelizer crossbar output packets.
        """
        return [bs.get_sim_output(chan_outputs) for bs in self.BIN_SEL]
