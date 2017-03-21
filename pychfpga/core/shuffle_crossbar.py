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
from collections import OrderedDict

import numpy as np

from Module import Module_base, BitField
import SHUFFLE_BIN_SEL


class ShuffleCrossbar(Module_base):
    """ Instantiates a container for all correlators blocks"""

    CONTROL = BitField.CONTROL
    STATUS = BitField.STATUS

    HEADER_CAPTURE_EN  = BitField(CONTROL, 0, 7, doc="Enables capture of header info on all lanes simultaneously.")
    # FRAME_CLK_SEL      = BitField(CONTROL, 0, 7, doc='')
    ALIGN_RESET        = BitField(CONTROL, 0, 6, doc='')
    REMAP_RESET        = BitField(CONTROL, 0, 5, doc='')
    LANE_MONITOR_RESET = BitField(CONTROL, 0, 4, doc='')
    LANE_MONITOR_SEL   = BitField(CONTROL, 0, 0, width=4, doc='')

    USE_TIMEOUT         = BitField(CONTROL, 1, 7, doc='')
    LANE_MONITOR_SOURCE = BitField(CONTROL, 1, 3, width=4, doc='')
    TIMEOUT_PERIOD      = BitField(CONTROL, 1, 0, width=3, doc='')

    SOF_WINDOW_STOP    = BitField(CONTROL, 2, 0, width=8, doc='')
    LANE_MAP_BYTE0     = BitField(CONTROL, 3, 0, width=8, doc='Lane map')
    # LANE_MAP_BYTE7     = BitField(CONTROL, 10, 0, width=8, doc='Lane map')
    # IGNORE_LANE        = BitField(CONTROL, 12, 0, width=16, doc='')


    FIFO_TFIRST_MON         = BitField(STATUS, 0, 1, doc='')
    BAD_EOF_MON             = BitField(STATUS, 0, 2, doc='')
    BAD_LANE_MON            = BitField(STATUS, 0, 3, doc='')
    BAD_FRAME_LENGTH        = BitField(STATUS, 0, 4, doc='')
    MISSING_FRAME_MON       = BitField(STATUS, 0, 5, doc='')
    ALIGN_FIFO_OVERFLOW_MON = BitField(STATUS, 0, 6, doc='')
    DATA_TIMEOUT_MON        = BitField(STATUS, 0, 7, doc='')

    NUMBER_OF_OUTPUT_LANES    = BitField(STATUS, 1, 0, width=4, doc='')
    NUMBER_OF_BIN_SEL         = BitField(STATUS, 1, 4, width=4, doc='')


    INPUT_FRAME_CTR    = BitField(STATUS, 2, 0, width=8, doc='')
    ALIGN_FRAME_CTR    = BitField(STATUS, 3, 0, width=8, doc='')
    OUTPUT_FRAME_CTR   = BitField(STATUS, 4, 0, width=8, doc='')
    CLK_CTR            = BitField(STATUS, 5, 0, width=8, doc='')

    HAD_TIMEOUT    = BitField(STATUS, 6, 7, doc='')
    CAPTURE_DONE      = BitField(STATUS, 6, 5, doc='')
    NUMBER_OF_INPUT_LANES    = BitField(STATUS, 6, 0, width=5, doc='')
    # CAPTURE_TVALID    = BitField(STATUS, 6, 1, doc='')
    # CAPTURE_TLAST    = BitField(STATUS, 6, 2, doc='')

    # CAPTURE_TDATA    = BitField(STATUS, 10, 0, width=32, doc='')

    FRAME_NUMBER_CAPTURE_DATA = BitField(STATUS, 7, 0, width=8, doc="")
    STREAM_ID_CAPTURE_DATA    = BitField(STATUS, 8, 0, width=8, doc="")
    DELAY_CAPTURE    = BitField(STATUS, 10, 0, width=16, doc="")
    FIFO_COUNT    = BitField(STATUS, 12, 0, width=16, doc="")

    def __init__(self, fpga_instance, base_address, address_increment, crossbar_level=1, verbose=0, number_of_bin_sel=2):
        self.fpga = fpga_instance
        self.verbose = verbose
        self.logger = logging.getLogger(__name__)
        self.crossbar_level = crossbar_level
        super(ShuffleCrossbar, self).__init__(fpga_instance, base_address)
        self.BIN_SEL = []
        for i in range(number_of_bin_sel):
            self.BIN_SEL.append(SHUFFLE_BIN_SEL.SHUFFLE_BIN_SEL_base(fpga_instance, base_address+ (i+1) * address_increment, i, crossbar_level=crossbar_level))

    def __getitem__(self, key):
        """    Returns the bin selector instance specified by the key"""
        return self.BIN_SEL[key]

    def init(self):
        """ Initializes all correlators"""
        self.NUMBER_OF_CROSSBAR_INPUTS = self.NUMBER_OF_INPUT_LANES
        self.NUMBER_OF_CROSSBAR_OUTPUTS = self.NUMBER_OF_OUTPUT_LANES
        self.NUMBER_OF_OUTPUTS_PER_BIN_SEL = self.NUMBER_OF_CROSSBAR_OUTPUTS/self.NUMBER_OF_BIN_SEL
        self.SOF_WINDOW_STOP = 50
        for bs in self.BIN_SEL:
            bs.init(number_of_inputs=self.NUMBER_OF_INPUT_LANES)

        # self.configure() # apply default configuration for now.
    # def select_words(self, words):
    #     """ Initializes all correlators"""
    #     for CROSSBAR in self.CROSSBAR:
    #         CROSSBAR.CH_DIST.select_words(words)

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

    def set_lane_map(self, lane_map):
        """ Sets the lane remapping.

        Lanes are numbered from 0 to 15. lane_map[x] indicates which lane the
        bin selectors will see in their input lane x. In other words, the
        position in the lane map is the bin_selector input lane, and the value
        in the lane map is the backplane shuffle output lane number. A value
        of 0 refers to the direct internal (non-backplane shuffled) lane.

        This repamming affects all bin selectors.
        """

        if len(lane_map) != self.NUMBER_OF_CROSSBAR_INPUTS:
            raise TypeError('Lane map must be a list of %i values' % self.NUMBER_OF_CROSSBAR_INPUTS)

        lane_map_bytes = np.zeros(self.NUMBER_OF_CROSSBAR_INPUTS/2, dtype=np.uint8)
        for i,lane in enumerate(lane_map):
            byte = i//2
            bit = (i%2)*4
            lane_map_bytes[byte] |= (lane & 0x0F) << bit

        self.write(self.get_addr('LANE_MAP_BYTE0'), lane_map_bytes)

    def get_lane_map(self):
        """ Get the lane remapping vector that indicates from which shuffle
        output lanes each bin selected or taing its data, i.e.
        shuffle_output_lane = lane_map[bin_sel_input_lane]
        """
        map_bytes = self.read(self.get_addr('LANE_MAP_BYTE0'), length=self.NUMBER_OF_CROSSBAR_INPUTS/2)

        lane_map = []
        for i, byte in enumerate(map_bytes):
            lane_map.append(byte & 0x0F)
            lane_map.append((byte >> 4) & 0x0F)
        return lane_map

    def get_reverse_lane_map(self):
        """ Gets the reverse of the lane remapping vector, where bin_sel_input_lane = lane_map[shuffle_output_lane].

        This method will fail if the mappings are not unique and do not cover all available lanes.
        """

        lane_map = self.get_lane_map()
        if set(lane_map) != set(range(self.NUMBER_OF_CROSSBAR_INPUTS)):
            raise ValueError('Invalid lane map: values are not unique.')

        return [lane_map.index(lane) for lane in range(len(lane_map))]

    def compute_bp_shuffle_lane_map(self):
        """ Computes a lane mapping vector that will compensate for the
        backplane connectivity on the specified IceBoard to obtain data
        from slot 1 in lane 0, slot 2 in lane 1 etc.

        The IceBoard must be connected to an identified backplane in order to
        obtain the slot number and backplane connectivity information.
        """
        ib = self.fpga
        lane_map = np.zeros(self.NUMBER_OF_CROSSBAR_INPUTS, dtype=np.int8)
        for i in range(self.NUMBER_OF_CROSSBAR_INPUTS):
            rx = (ib.slot, i)
            tx = ib.crate.get_matching_tx(rx)
            # print '%s is receiving from %s' % (rx, tx)
            lane_map[tx[0]-1] = i
        return lane_map

    def map(self, data):
        """
        Returns a crossbar map that describes the contents of each bin selector.
        input: {lane:[elements ...], ...}
        returns: {lane: [elements], ...}
        format:
            {lane_number: {channels:[channel numbers...], bins:[bin numbers ...], stream_id:x, ...}, ...}
        """

        remap_out = {output_lane: data[input_lane] for output_lane, input_lane in enumerate(self.get_lane_map())}

        cb_out = OrderedDict()
        N = self.NUMBER_OF_OUTPUT_LANES / self.NUMBER_OF_BIN_SEL
        for bs_number, bs in enumerate(self.BIN_SEL):
            for sublane, data in bs.map(remap_out).items():
                cb_out[N * bs_number + sublane] = data
        return cb_out

    def configure(self, number_of_bins_per_crossbar_output= 8):
        """
        Configure the channel selection.
        This should be done once the data width has been selected.
        """
        # data_width = self.get_data_width()
        # # Compute the minimum word spacing to allow the channel_selector time to forwared the data.
        # # In 8-bit mode, 2*N bins come every clock from the channelizers, and it takes N clocks to send them away (2 per output word). So the bin spacing is N.
        # # In 4-bit mode, 2*N bins come every clock from the channelizers, and it takes N/2 clocks to send them away ( 4 per output word). So bin spacing is N/2.
        # bin_step = self.fpga.NUMBER_OF_CROSSBAR_INPUTS * data_width / 8
        # number_of_bins_per_frame = self.fpga.FRAME_LENGTH / 2 # The FFT generates 2048 bins, but half of them are discarded
        # #number_of_bins_per_frame = 2 # The FFT generates 2048 bins, but half of them are discarded
        # if number_of_bins_per_crossbar_output is None:
        #     number_of_bins_per_crossbar_output =  int (number_of_bins_per_frame / bin_step) # Number of channels that one channel selector can handle

        # if bin_step > self.fpga.NUMBER_OF_CROSSBAR_OUTPUTS:
        #     self.logger.warning('   Only a fraction of the frequency bins can be mapped to the crossbar outputs because the total number of bits entering the crossbar exceeds the number of bits at its outputs.')

        # # Check if the set-up is acceptable for the FPGA correlator (if present in the FPGA), and make corrections if needed
        # if self.fpga.NUMBER_OF_CORRELATORS:
        #     if data_width == 4:
        #         self.logger.warning('The FPGA correlator will not operate properly in 4-bit mode ')

        #     max_correlator_frame_length_in_words = 511 # maximum number of words that the correlator can handle in a frame. This is limited by the ACCumulator buffer depth
        #     max_number_of_words_per_correlator = int( max_correlator_frame_length_in_words / self.fpga.NUMBER_OF_ANTENNAS_TO_CORRELATE ) # maximum number of words that can be selected
        #     if number_of_bins_per_crossbar_output > 2*max_number_of_words_per_correlator:
        #         self.logger.warning('   The number of frequency bins in each crossbar output was reduced from %i to %i due to the correlator accumulator memory limitation' % (number_of_bins_per_crossbar_output, max_number_of_words_per_correlator))
        #         number_of_bins_per_crossbar_output = 2*max_number_of_words_per_correlator

        # # Apply GPU Link limitations
        # if self.fpga.NUMBER_OF_GPU_LINKS:
        #     max_number_of_words_per_input_frame = 4095 // self.get_frame_grouping() * 8 / data_width / self.fpga.NUMBER_OF_CROSSBAR_INPUTS
        #     if number_of_bins_per_crossbar_output > 2*max_number_of_words_per_input_frame:
        #         self.logger.warning('   The number of frequency bins in each crossbar output was reduced from %i to %i due to the GPU link buffer size limitations' % (number_of_bins_per_crossbar_output, max_number_of_words_per_input_frame))
        #         number_of_bins_per_crossbar_output = 2*max_number_of_words_per_input_frame

#         for (i, xbar) in enumerate(self.CROSSBAR):
#             bin_list = np.arange(number_of_bins_per_crossbar_output)* bin_step + i
#             # xbar.CH_DIST.select_words(word_list) # enable tranmission 8 words, 16 freq channels by default
# #            bin_list = [0,8]
#             xbar.CH_DIST.select_bins(bin_list) # enable tranmission 8 words, 16 freq channels by default
        for (i, bs) in enumerate(self.BIN_SEL):
            if self.crossbar_level==1:
                bin_list = np.arange(number_of_bins_per_crossbar_output)* 2 + i
            else:
                bin_list = np.arange(number_of_bins_per_crossbar_output) * 8 + i
            # xbar.CH_DIST.select_words(word_list) # enable tranmission 8 words, 16 freq channels by default
#            bin_list = [0,8]
            bs.select_bins(bin_list) # enable tranmission 8 words, 16 freq channels by default

    def status(self):
        """ Displays the status of all correlators"""
        for bs in self.BIN_SEL:
            bs.status()


    def capture_stream_id(self):
        sid = []

        # get 8 bits of stream ID
        self.HEADER_CAPTURE_EN = 0
        for i in range(self.NUMBER_OF_CROSSBAR_INPUTS):
            self.LANE_MONITOR_SEL = i
            sid.append(self.STREAM_ID_CAPTURE_DATA)
        self.HEADER_CAPTURE_EN = 1
        return sid

    def capture_frame_number(self):
        frame = []

        # get 8 bits of stream ID
        self.HEADER_CAPTURE_EN = 0
        for i in range(self.NUMBER_OF_CROSSBAR_INPUTS):
            self.LANE_MONITOR_SEL = i
            frame.append(self.FRAME_NUMBER_CAPTURE_DATA)
        self.HEADER_CAPTURE_EN = 1
        return frame


    LANE_MONITOR_TABLE = {
        'FIFO_TFIRST': 'FIFO_TFIRST_MON',
        'BAD_TLAST': 'BAD_EOF_MON',
        'BAD_TVALID': 'BAD_LANE_MON',
        'BAD_FRAME_LENGTH': 'BAD_FRAME_LENGTH',
        'MISSING_FRAME': 'MISSING_FRAME_MON',
        'ALIGN_FIFO_OVERFLOW': 'ALIGN_FIFO_OVERFLOW_MON',
        'DATA_TIMEOUT': 'DATA_TIMEOUT_MON',
        }

    def get_lane_monitor(self, names):
        """
        Return a list describing the status of the specified flag for each
        lane.
        """
        if isinstance(names, str):
            names = [names]
            is_list = False
        else:
            is_list = True

        bitfields = []
        for name in names:
            if name not in self.LANE_MONITOR_TABLE:
                raise ValueError('Invalid lane monitor name. valid names are %s' % ','.join(self.LANE_MONITOR_TABLE.keys()))
            bitfields.append(self.get_bitfield(self.LANE_MONITOR_TABLE[name]))

        mon = [[] for _ in bitfields]
        for lane in range(self.NUMBER_OF_CROSSBAR_INPUTS):
            self.LANE_MONITOR_SEL = lane
            for i, bf in enumerate(bitfields):
                mon[i].append(self.read_bitfield(bf))

        return mon if is_list else mon[0]

    def get_align_status(self):
        status = []
        err_names = ['TLAST', 'TVALID', 'DISCARD', 'MISSING','FIFO','TIMEOUT']
        errors = self.get_lane_monitor(['BAD_TLAST', 'BAD_TVALID', 'BAD_FRAME_LENGTH', 'MISSING_FRAME', 'ALIGN_FIFO_OVERFLOW', 'DATA_TIMEOUT'])
        for lane in range(len(errors[0])):
            status.append({err_names[errno]:err[lane] for (errno, err) in enumerate(errors) if err[lane]})
        return status

    def get_frame_alignment_status(self):
        frame_numbers = self.capture_frame_number()
        # is_aligned = len(set(frame_numbers)) == 1
        status = [{'DELTA':f-frame_numbers[0]} if f-frame_numbers[0] else {} for (lane, f) in enumerate(frame_numbers)]
        return status

    def get_bin_sel_status(self):
        status = []
        for bs in self.BIN_SEL:
            number_of_sublanes_per_output = self.NUMBER_OF_INPUT_LANES/bs.NUMBER_OF_OUTPUTS
            sublane_mask = (1 << (bs.LAST_LANE + 1)) - (1 << bs.FIRST_LANE)
            mask = sum(sublane_mask << (number_of_sublanes_per_output * i) for i in range(bs.NUMBER_OF_OUTPUTS))

            err = {}
            if bs.FIFO_OVERFLOW & mask:
                err['DFIFO'] = 1
            if bs.FLAGS_FIFO_OVERFLOW & mask:
                err['FFIFO'] = 1
            status.append(err)
        return status

    def reset_stats(self):
        self.LANE_MONITOR_RESET = 1
        self.LANE_MONITOR_RESET = 0
        for bs in self.BIN_SEL:
            bs.FIFO_OVERFLOW_RESET = 1
            bs.FIFO_OVERFLOW_RESET = 0


    def print_crossbar_monitor(self, reset=True):

        if reset:
            self.reset_stats()
            self.fpga.BP_SHUFFLE.reset_stats()

        lane_range = range(self.NUMBER_OF_CROSSBAR_INPUTS)
        lane_map = self.get_lane_map()
        active_slots = set(self.fpga.crate.slot.keys())
        rx_errors = self.fpga.BP_SHUFFLE.get_rx_lane_monitor('ERROR_CTR')
        rx_max_frame = self.fpga.BP_SHUFFLE.get_rx_lane_monitor('MAX_FRAME_LENGTH')
        rx_min_frame = self.fpga.BP_SHUFFLE.get_rx_lane_monitor('MIN_FRAME_LENGTH')

        stream_id = self.capture_stream_id()
        stream_id = [stream_id[lane] for lane in lane_map]
        frame_number = self.capture_frame_number()
        frame_ref = frame_number[0]
        frame_number = [frame_number[lane] for lane in lane_map]

        print '%25s: %s' % ('Monitor point', ' '.join('  L%2i ' % v for v in lane_range))
        print '%25s: %s' % ('--------------------', ' '+' '.join('------' for v in lane_range))
        print '%25s: %s' % ('Pre-map lane #', ' '.join(('%6i' % lane_map[lane] for lane in lane_range)))
        if self.fpga.slot is not None:
            gtx_ids = [(self.fpga.slot, lane_map[lane]) for lane in lane_range]
            print '%25s: %s' % ('Rx Node ID', ''.join('%7s' % ('(%i,%i)' % id_) for id_ in gtx_ids))
            matching_gtx_ids = [self.fpga.crate.get_matching_tx(gtx_id) for gtx_id in gtx_ids]
            print '%25s: %s' % ('Matching GTX present', ' '.join(('%6s' % ('-N/A-', 'ok ')[matching_id[0] in active_slots]) for matching_id in matching_gtx_ids))
            print '%25s: %s' % ('Matching TX Node ID', ''.join('%7s' % ('(%i,%i)' % matching_id) for matching_id in matching_gtx_ids))
        print '%25s: %s' % ('Detected Stream ID', ''.join('%7s' % ('(%i,%i)' % (((id_ >> 4) & 15)+1, id_& 15)) for id_ in stream_id))
        print '%25s: %s' % ('RX Errors', ' '.join('%6i' % rx_errors[lane_map[lane]] for lane in lane_range))
        print '%25s: %s' % ('RX max frame len (words)', ' '.join('%6i' % (rx_max_frame[lane_map[lane]] + 1) for lane in lane_range))
        print '%25s: %s' % ('RX min frame len (words)', ' '.join('%6i' % (rx_min_frame[lane_map[lane]] + 1) for lane in lane_range))
        # input_detect = self.get_lane_monitor('INPUT_DETECT')
        # input_detect = [input_detect[lane] for lane in lane_map]
        # align_detect = self.get_lane_monitor('ALIGN_DETECT')
        # align_detect = [align_detect[lane] for lane in lane_map]
        # remap_detect = self.get_lane_monitor('REMAP_DETECT')
        # print '%25s: %s' % ('IN/ALGN/REMAP DETECT', ' '.join(' %i/%i/%i' % (input_detect[lane], align_detect[lane], remap_detect[lane]) for lane in lane_range))
        for name in [ 'MISSING_FRAME', 'BAD_FRAME_LENGTH', 'ALIGN_FIFO_OVERFLOW', 'DATA_TIMEOUT', 'BAD_TVALID', 'BAD_TLAST']:
            value = self.get_lane_monitor(name)
            print '%25s: %s' % (name, ' '.join('%6s' % ('-', 'ERR!')[bool(value[lane])] for lane in lane_map))
        input_frame_ctr = []
        align_frame_ctr = []
        delay = []
        fifo_tfirst = self.get_lane_monitor('FIFO_TFIRST')
        fifo_count = []
        for lane in lane_range:
            self.LANE_MONITOR_SEL = lane
            input_frame_ctr.append(self.INPUT_FRAME_CTR)
            align_frame_ctr.append(self.ALIGN_FRAME_CTR)
            delay.append(self.DELAY_CAPTURE if lane < 8 else '-')
            fifo_count.append(self.FIFO_COUNT)

        print '%25s: %s' % ('INPUT DELAY', ' '.join('%6s' % delay[lane] for lane in lane_map))
        print '%25s: %s' % ('INPUT_FRAME_CTR', ' '.join('%6i' % input_frame_ctr[lane] for lane in lane_map))
        print '%25s: %s' % ('ALIGN_FRAME_CTR', ' '.join('%6i' % align_frame_ctr[lane] for lane in lane_map))
        print '%25s: %s' % ('FIFO_TFIRST', ' '.join('%6i' % fifo_tfirst[lane] for lane in lane_map))
        print '%25s: %s' % ('FIFO_COUNT', ' '.join('%6i' % fifo_count[lane] for lane in lane_map))
        output_frame_ctr = []
        for lane in range(self.NUMBER_OF_CROSSBAR_OUTPUTS):
            self.LANE_MONITOR_SEL = lane
            output_frame_ctr.append(self.OUTPUT_FRAME_CTR)
        print '%25s: %s' % ('OUTPUT_FRAME_CTR', ' '.join('%6i' % v for v in output_frame_ctr))

        print '%25s: %s' % ('Frame #', ' '.join('%6i' % f for f in frame_number))
        print '%25s: %s' % ('Delta Frame #', ' '.join('%6i' % (f - frame_ref) for f in frame_number))


    # def print_capture_word(self):

    #     print 'Lane %02i, Word %i: Done=%i, tvalid=%i, tlast=%i, tdata=0x%08X' % (self.LANE_MONITOR_SEL, self.CAPTURE_WORD_NUMBER, self.CAPTURE_DONE, self.CAPTURE_TVALID, self.CAPTURE_TLAST, self.CAPTURE_TDATA)