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


def combine_data_flag_words(word_lists, bins):
    """ Pack input-lane data-flags words pairwise into combined output words.

    Pure layout model of the shuffle bin selector's COMBINE_DATA_FLAGS=1
    readout (crossbar 2): for each selected incoming bin, consecutive pairs
    of forwarded input lanes are packed into single words - low half from
    the even lane of the pair, high half from the odd lane.

    Parameters:
        word_lists: one sequence per forwarded input lane (in lane order) of
            uint32 data-flags words indexed by incoming bin position.

        bins: selected incoming bin positions in transmission order.

    Returns:
        np.uint32 array of len(bins) * len(word_lists)//2 words, bins outer.
    """
    if len(word_lists) % 2:
        raise ValueError('combined data flags need an even number of forwarded lanes')
    out = []
    for bin_number in bins:
        for i in range(0, len(word_lists), 2):
            low = int(word_lists[i][bin_number]) & 0xFFFF
            high = int(word_lists[i + 1][bin_number]) & 0xFFFF
            out.append(low | (high << 16))
    return np.asarray(out, np.uint32)


def passthrough_data_flag_words(word_lists, bins, words_per_bin=1):
    """ Concatenate input-lane data-flags words without combining.

    Pure layout model of the COMBINE_DATA_FLAGS=0 readout (crossbar 3):
    bins outer, forwarded lanes inner, each lane contributing its
    `words_per_bin` flag words for the selected bin.

    Returns:
        np.uint32 array of len(bins) * len(word_lists) * words_per_bin words.
    """
    out = []
    for bin_number in bins:
        for lane_words in word_lists:
            for w in range(words_per_bin):
                out.append(int(lane_words[bin_number * words_per_bin + w]) & 0xFFFFFFFF)
    return np.asarray(out, np.uint32)


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

    def __init__(self, *, router, router_port, instance_number, crossbar_level, config):
        # self.parent = parent
        # self.fpga = fpga_instance
        self.crossbar_level = crossbar_level
        super().__init__(router=router, router_port=router_port, instance_number=instance_number)
        self.config = config
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
        self.logger.debug(f'{self.fpga!r}: CROSSBAR{int(self.crossbar_level)}.BIN_SEL[{int(self.instance_number)}] configured to capture {len(bins_to_enable)} frequency bins: {bins_to_enable[:10]!r}...')
        # self.logger.debug('%r: Mask pattern is: %s' % (self.fpga, ' '.join('%02X'% byte for byte in mask)))

        self.cached_bin_select_table = mask
        self.write_ram(0x00, mask)  # Write the bin selection mask array

    def get_selected_bins(self, use_cache=True):

        if use_cache and self.cached_bin_select_table is not None:
            mask = self.cached_bin_select_table
        else:
            mask = self.read_ram(0x00, type=np.dtype('u1'), length=128)  # 128*8 = up to 1024 bins / frame

        bin_map = np.unpackbits(mask[::-1])[::-1]
        return np.where(bin_map)[0]

    def map_flags(self, input_flags):
        """ Model the layout of the flags blocks this bin selector re-emits.

        Pure permutation/packing model of the flag path, mirroring what
        map() does for the data section: the caller supplies the incoming
        per-lane flag words and this method re-slices them exactly as the
        firmware does (combined pair-packing on crossbar 2, pass-through
        concatenation on crossbar 3; frame-flags words are concatenated in
        forwarded-lane order on both).

        Parameters:
            input_flags: {input_lane: dict-like with 'data_flags' (sequence
                of uint32 words indexed by incoming bin position x
                words-per-bin) and 'frame_flags' (a single word or a list of
                words per frame)}. Missing lanes/entries contribute zeros.

        Returns:
            {sublane: dict(data_flags=<uint32 array>,
                           frame_flags=<list of int words>,
                           packet_flags=0)}
        """
        if self.BYPASS:
            raise RuntimeError(f'{self!r}: SHUFFLE_BIN_SEL cannot provide flag maps in BYPASS mode')

        N = self.NUMBER_OF_INPUTS // self.NUMBER_OF_OUTPUTS
        bins = self.get_selected_bins()
        combine = bool(self.COMBINE_DATA_FLAGS)
        words_per_bin = max(int(self.NUMBER_OF_DATA_FLAGS_WORDS_PER_BIN), 1)
        incoming_bins = int(self.NUMBER_OF_BINS_PER_FRAME)
        frame_flag_words = max(int(self.NUMBER_OF_FRAME_FLAGS_WORDS_PER_FRAME), 1)

        def lane_entry(lane, key, default):
            entry = input_flags.get(lane)
            value = entry.get(key) if entry is not None else None
            if value is None:
                return default
            if key == 'frame_flags' and isinstance(value, (int, np.integer)):
                return [int(value)]  # single-word producers (chan_bin_sel) emit an int
            return value

        out = OrderedDict()
        for sublane in range(self.NUMBER_OF_OUTPUTS):
            lanes = list(range(N * sublane + self.FIRST_LANE, N * sublane + self.LAST_LANE + 1))
            zero_df = [0] * (incoming_bins * words_per_bin)
            word_lists = [lane_entry(lane, 'data_flags', zero_df) for lane in lanes]
            if combine:
                data_flags = combine_data_flag_words(word_lists, bins)
            else:
                data_flags = passthrough_data_flag_words(word_lists, bins, words_per_bin)
            frame_flags = []
            for lane in lanes:
                frame_flags.extend(int(v) & 0xFFFFFFFF
                                   for v in lane_entry(lane, 'frame_flags', [0] * frame_flag_words))
            out[sublane] = dict(data_flags=data_flags, frame_flags=frame_flags, packet_flags=0)
        return out

    def map(self, input_data, header=False, flags=False):
        """ Return a bin selector frequency map, which describes the structure and contents of the
        bin selector output stream.

        Parameters:
            flags (bool): when True, the incoming lane dicts' 'data_flags'
                and 'frame_flags' entries are re-sliced through map_flags()
                and returned in place of the None placeholders.
        """

        N = self.NUMBER_OF_INPUTS // self.NUMBER_OF_OUTPUTS  # number of input lanes per output
        ch_per_bin = self.NUMBER_OF_WORDS_PER_BIN * 4  # fixme - only 4-bit mode
        bs_out = OrderedDict()
        bins = self.get_selected_bins()
        bypass = self.BYPASS
        mapped_flags = self.map_flags(input_data) if flags else None
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

            if mapped_flags is not None:
                bs_out[sublane] = dict(
                    header=header,
                    data=data,
                    data_flags=mapped_flags[sublane]['data_flags'],
                    frame_flags=mapped_flags[sublane]['frame_flags'],
                    packet_flags=mapped_flags[sublane]['packet_flags']
                    )
            else:
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
        self.logger.debug(f'{self.fpga!r}: --- SHUFFLE_BIN_SEL[{int(self.instance_number)}] STATUS')
        self.logger.debug(f'{self.fpga!r}:    RESET: {int(self.RESET)}')
        self.logger.debug(f'{self.fpga!r}:    FIFO EMPTY: {int(self.FIFO_EMPTY)}')
        # self.logger.debug('   FIFO OVERFLOW: %i' % self.FIFO_OVERFLOW)
