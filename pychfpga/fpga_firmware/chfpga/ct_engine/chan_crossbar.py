#!/usr/bin/python

"""
CHAN_CROSSBAR.py module
 Implements the interface to the channelizer crossbar firmware in the FPGA.

The crossbar is the first stage of the corner turn engine. It aligns the packets from all
channelizers and feeds them to an array of bin selectors. Each bin selector generates a stream of
data that contains some selected frequency bins from all input channels.
"""

import logging
import asyncio


from wtl.metrics import Metrics
from ..mmi import MMI, MMIRouter, BitField, CONTROL, STATUS

from . import chan_bin_sel

class CCBRouter(MMIRouter):
    ROUTER_PORT_NUMBER_WIDTH = 5
    ROUTER_PORT_MAP = {
        'COMMON': 0
        # port numbers for BIN_SELS are computed
        }

class ChanCrossbar(MMI):
    """ Object that allows access to a channelizer crossbar"""

    ALIGN_RESET        = BitField(CONTROL, 0, 6, doc='')
    LANE_MONITOR_RESET = BitField(CONTROL, 0, 4, doc='')
    LANE_MONITOR_SEL   = BitField(CONTROL, 0, 0, width=4, doc='')

    LANE_MONITOR_SOURCE= BitField(CONTROL, 1, 0, width=4, doc='')
    # SOF_WINDOW_START   = BitField(CONTROL, 1, 0, width=8, doc='')
    # SOF_WINDOW_STOP    = BitField(CONTROL, 2, 0, width=8, doc='')

    RESET_MON           = BitField(STATUS, 0, 0, doc='')
    ALIGN_FIFO_OVERFLOW = BitField(STATUS, 0, 1, doc='')
    LANE_MONITOR_BIT    = BitField(STATUS, 0, 2, doc='ALIGN Monitoring bit. LANE_MONITOR_SOURCE selects the type of monitoring. LANE_MONITOR_SEL selects the bit within the work (which typically corresponds to the lane) ')
    LANE_MONITOR_CTR    = BitField(STATUS, 0, 3, width=5, doc='Counts up (and rollback) when LANE_MONITR_BIT is high')

    # BIN_CTR            = BitField(STATUS, 2, 0, width=8, doc='')
    INPUT_FRAME_CTR    = BitField(STATUS, 1, 0, width=8, doc='')
    ALIGN_FRAME_CTR    = BitField(STATUS, 2, 0, width=8, doc='')
    # ALIGN_GLOBAL_FRAME_CTR    = BitField(STATUS, 5, 0, width=8, doc='')
    DELAY_CAPTURE    = BitField(STATUS, 4, 0, width=16, doc="")

    def __init__(self, *, router, router_port, verbose=0):
        self.verbose = verbose
        self.logger = logging.getLogger(__name__)
        ccb_router = CCBRouter(router=router, router_port=router_port)
        super().__init__(router=ccb_router, router_port='COMMON')
        print(f'Reset Mon = {self.RESET_MON}')
        self.crossbar_level = 1
        self.BIN_SEL = []
        self.NUMBER_OF_CROSSBAR_INPUTS = self.fpga.NUMBER_OF_CROSSBAR_INPUTS
        self.NUMBER_OF_CROSSBAR_OUTPUTS = self.fpga.NUMBER_OF_CROSSBAR1_OUTPUTS
        for i in range(self.fpga.NUMBER_OF_CROSSBAR1_OUTPUTS):
            self.BIN_SEL.append(chan_bin_sel.ChanBinSel(router=ccb_router, router_port=i + 1, instance_number=i))

    def __getitem__(self, key):
        """    Returns the bin selector instance specified by the key"""
        return self.BIN_SEL[key]

    def init(self):
        """ Initializes all correlators"""
        for bs in self.BIN_SEL:
            bs.init()

        # Default bin selector configuration. To be overriden by system-level configuration method.
        # By default we send 64 bins for each of the 16 output lanes. Bins are interleaved.
        # number_of_bins_per_crossbar_output = 64
        # Do not initialize the bin selection map now to same time. This will be done anyway when we initialize the shuffling system.
        # for (i, bs) in enumerate(self.BIN_SEL):
        #     bin_list = np.arange(number_of_bins_per_crossbar_output) * 16 + i
        #     bs.select_bins(bin_list)

    def set_data_width(self, width):
        """ Sets the number of bits expected at the input of the crossbar.

        All crossbars are set to the new setting.

        Parameters:

            width (int): data widths:

                - width=4: data is 4 bits Real + 4 bits Imaginary
                - width=8: data is 8 bits Real + 8 bits Imaginary
        """

        if width == 4:
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

        # use a set to uniquely record all the possible encountered states
        four_bits = {bs.FOUR_BITS for bs in self.BIN_SEL}

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

    def status(self):
        """ Displays the status of all bin selectors"""
        for bs in self.BIN_SEL:
            bs.status()

    CB1_LANE_MONITOR_TABLE = {
        'RESET': 0,
        'MISSING_FRAME': 4,
        'ALIGN_FIFO_OVERFLOW': 6,
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

    def reset_stats(self):
        self.LANE_MONITOR_RESET = 1
        self.LANE_MONITOR_RESET = 0

    def print_lane_monitor(self, reset=True):

        if reset:
            self.reset_stats()

        lane_range = list(range(self.NUMBER_OF_CROSSBAR_INPUTS))

        print('%20s: %s' % ('Monitor point', ' '.join('  L%2i ' % v for v in lane_range)))
        print('%20s: %s' % ('--------------------', ' '+' '.join('------' for v in lane_range)))
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

        print('%20s: %s' % ('RESET', ' '.join('%6s' % ('-', 'ERR!')[bool(v)] for v in reset_mon)))
        print('%20s: %s' % ('ALIGN_FIFO_OVERFLOW',
                            ' '.join('%6s' % ('-', 'ERR!')[bool(v)] for v in align_fifo_overflow)))
        print('%20s: %s' % ('INPUT FRAME CTR', ' '.join('%6i' % v for v in input_frame_ctr)))
        print('%20s: %s' % ('ALIGN FRAME CTR', ' '.join('%6i' % v for v in align_frame_ctr)))
        # print '%20s: %s' % ('ALIGN GLOBAL FRAME CTR', '(common to all lanes) %6i' % self.ALIGN_GLOBAL_FRAME_CTR)

    def print_align_monitor(self, reset=True, N=100):
        """ Debug method to monitor the CHAN_ALIGN module monitoring bits. We print the number of times each bit was read as '1'
        """
        if reset:
            self.reset_stats()

        for source in range(16):
            self.LANE_MONITOR_SOURCE=source;
            print(f'Source {source:2}: ', end='')
            for i in range(16):
                self.LANE_MONITOR_SEL=i;
                self.reset_stats()  # reset the bit counter now that we have selected the bit
                s = sum(self.LANE_MONITOR_BIT for _ in range(N))
                c = self.LANE_MONITOR_CTR
                print(f"{s:3}({c:2})", end=" ", flush=True)
            print()

    async def get_metrics(self, reset=True):
        """ Return the monitoring metrics for the 1st crossbar.
        """
        metrics = Metrics(
            type='GAUGE',
            crate_id=self.fpga.crate.get_string_id() if self.fpga.crate else None,
            crate_number=self.fpga.crate.crate_number if self.fpga.crate else None,
            slot=(self.fpga.slot or 0) - 1,
            id=self.fpga.get_string_id())

        for lane in range(self.NUMBER_OF_CROSSBAR_INPUTS):
            await asyncio.sleep(0)
            self.LANE_MONITOR_SEL = lane
            await asyncio.sleep(0)
            metrics.add('fpga_crossbar1_reset_state', value=self.RESET_MON, lane=lane)
            await asyncio.sleep(0)
            metrics.add('fpga_crossbar1_align_fifo_overflow_flag', value=self.ALIGN_FIFO_OVERFLOW, lane=lane)
            await asyncio.sleep(0)
            metrics.add('fpga_crossbar1_input_frame_counter', value=self.INPUT_FRAME_CTR, lane=lane)
            await asyncio.sleep(0)
            metrics.add('fpga_crossbar1_align_output_frame_counter', value=self.ALIGN_FRAME_CTR, lane=lane)

        if reset:
            self.reset_stats()

        return metrics

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

    def get_sim_output(self, chan_outputs):
        """ Compute the channelizer crossbar output packets.
        """
        return [bs.get_sim_output(chan_outputs) for bs in self.BIN_SEL]
