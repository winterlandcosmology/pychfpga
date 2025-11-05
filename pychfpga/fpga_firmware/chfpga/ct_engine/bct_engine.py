"""
UCT.py module
    Implements the Ultrascale Corner-Turn Engine
"""
import logging
import numpy as np

from ..mmi import MMI, MMIRouter, BitField, CONTROL, STATUS

from . import chan_crossbar
from . import shuffle_crossbar
from . import shuffle

class WiredCrossbar(MMI):
    """ Trivial hardwired crossbar, typically used with the internal correlator engine. Does not have any MMI register.

    Outputs 2 lanes (2 bins)  of 16 channels each. Both lanes/bins channels are in linear order.

    """

    def init(self):
        pass

    def set_data_width(self, width):
        if width != 4:
            raise RuntimeError(f'CT engine only supports a data width of 4+4 bits. {width}+{width} bits is not supported')
    def get_data_width(self):
        return 4

    def set_frames_per_packet(self, frames):
        if frames != 1:
            self.logger.warning(f'{self!r}: CT engine only supports packaging 1 frame per packet. {frames} frames are not supported')

    def get_frames_per_packet(self):
        return 1


class BCTEngine(MMIRouter):
    """ BRAM-Based corner-turn (CT) engine.

    Implements a 1st crossbar, intra-crate (16x16) data shuffle, 2nd crossbar, inter-crate (2x2) data shiffle, and a 3rd crossbar.

    """

    ROUTER_PORT_NUMBER_WIDTH = 2
    ROUTER_PORT_MAP = {
        'CB1': 0,
        'CB2': 1,
        'CB3': 2,
        'SHUFFLE': 3
        }

    def __init__(self, *, router, router_port, verbose=1):
        self.logger = logging.getLogger(__name__)
        self.verbose = verbose

        super().__init__(router=router, router_port=router_port)

        self.BP_SHUFFLE = None
        self.CROSSBAR = None
        self.CROSSBAR2 = None
        self.CROSSBAR3 = None

        CT_LEVEL = self.CT_LEVEL = self.fpga.CT_LEVEL

        self.logger.debug(f'{self!r}: === Instantiating Corner-turn engine Type BCT Level {CT_LEVEL}')

        if CT_LEVEL == 1:

            self.logger.debug(f'{self!r}: ===     Instantiating 1st CROSSBAR (Wired)')
            self.CROSSBAR = WiredCrossbar(router=self, router_port='CB1')

        elif CT_LEVEL == 3:
            self.logger.debug(f'{self!r}: ===     Instantiating 1st CROSSBAR (Programmable)')
            self.CROSSBAR = chan_crossbar.ChanCrossbar(router=self, router_port='CB1')

            self.logger.debug(f'{self!r}: ===     Instantiating Backplane shuffle subsystem')
            self.BP_SHUFFLE = shuffle.Shuffle(router=self, router_port='SHUFFLE')

            self.logger.debug(f'{self!r}: ===     Instantiating 2nd CROSSBAR')
            self.CROSSBAR2 = shuffle_crossbar.ShuffleCrossbar(
                router=self,
                router_port='CB2',
                crossbar_level=2,
                number_of_bin_sel=2)  # CROSSBAR block

            self.logger.debug(f'{self!r}: ===     Instantiating 3rd CROSSBAR')
            self.CROSSBAR3 = shuffle_crossbar.ShuffleCrossbar(
                router = self,
                router_port='CB3',
                crossbar_level=3,
                number_of_bin_sel=8)  # CROSSBAR block
        else:
            raise RuntimeError("BCT-type Corner turn engine only supports CT_LEVEL of 1 or 3. Received CT_LEVEL={CT_LEVEL}")

    def init(self):
        self.logger.debug(f'{self!r}: === Initializing 1st Crossbar')

        # if self.NUMBER_OF_CROSSBAR1_OUTPUTS > 0:
        if self.CROSSBAR:
            self.logger.debug(f'{self!r}:  - BCT 1st CROSSBAR')
            self.CROSSBAR.init()
            # self.CROSSBAR.status()
        else:
            self.logger.warning(f"{self!r}: There is no 1st CROSSBAR module in this firmware build. "
                                 "The Channelizer outputs will be discarded.")

        if self.BP_SHUFFLE:
            self.logger.debug(f'{self!r}: === Initializing Backplane Shuffle')
            self.BP_SHUFFLE.init()

        self.logger.debug(f'{self!r}: === Initializing BCT 2nd Crossbar')
        if self.CROSSBAR2:
            # await asyncio.sleep(0)
            self.CROSSBAR2.init()
        # else:
        #     self.logger.warning(f"{self!r}: There is no 2nd CROSSBAR module in this firmware build")

        self.logger.debug(f'{self!r}: === Initializing BCT 3rd Crossbar')
        if self.CROSSBAR3:
            # await asyncio.sleep(0)
            self.CROSSBAR3.init()
        # else:
        #     self.logger.warning(f"{self!r}: There is no BCT 3rd CROSSBAR module in this firmware build")

    def set_data_width(self, width):
        if self.CROSSBAR:
            self.CROSSBAR.set_data_width(width)

    def get_data_width(self):
        return self.CROSSBAR.get_data_width()

    def set_frames_per_packet(self, frames):
        if self.CROSSBAR:
            self.CROSSBAR.set_frames_per_packet(frames)
            self.logger.debug(f'{self!r}: The 1st crossbar will pack {frames} frames per packet')

    # def capture_bytes(self, N=64, fmt='hex'):
    #     """ Captures N bytes
    #     """
    #     b = bytearray(N)
    #     for i in range(N):
    #         self.CAPTURE_BYTE_NUMBER = i
    #         b[i] = self.CAPTURE_BYTE

    #     if fmt == 'hex':
    #         print('\n'.join(f'{b[i: i+16].hex()} {b[i+16: i+32].hex()}' for i in range(0,len(b),32)))
    #         return None

    #     return b

    # def set_enable(self, enable):
    #     """
    #     Enable link.
    #     """
    #     self.logger.warn('{self!r}: set_enable()  is not implemented on UltraCT. Command is ignored. ')

    # # def reset(self):
    # #     """ Resets the UDP/MAC stack, the SGMII interface and the GTX """
    # #     self.RESET = 1
    # #     self.RESET = 0

    # def get_lane_numbers(self):
    #     """ Returns a list of logical lane numbers for the GPU links.

    #     There is no distinction between the two QSFP connectors.

    #     Returns:

    #         List of integers.
    #     """
    #     return list(range(1))

    # def get_lane_ids(self):
    #     """ Return a list of all lane IDs in the form of [(crate_number, slot_number, lane_number), ...].

    #     Returns:
    #         List of all lane IDs in the form of [(crate_number, slot_number, lane_number), ...]
    #     """

    #     return [self.fpga.get_id(lane) for lane in self.get_lane_numbers()]

    # # def status(self):
    # #     """ Displays the status of the GPU GTX hardware"""
    # #     # for gtx in self.GTX_COMMON:
    # #     #     gtx.status()



    def init_crossbars(
            self,
            mode=None,
            frames_per_packet=2,
            bin_map=None,

            cb1_lanes=16,
            cb1_bins=64,
            dsmap=list(range(16)),
            cb1_bypass=False,
            cb1_combine_data_flags=0,

            bp_shuffle_bypass=1,

            cb2_lanes=None,
            cb2_bins=1,
            cb2_bypass=False,

            crate_shuffle_bypass=1,

            remap=True,
            chan8_channel_map=list(range(8)),
            send_flags=True):
        """ Initializes the Corner Turn engine in the specified operation mode.

        This method configured the 1st, 2nd and 3rd crossbars (which each can include a remap, frame
        aligner and an array of bin selectors), as well as the PCB and QSFP data shuffle links.


        Parameters:

            mode (str): String describing a predefined corner-turn engine operation mode. If None,
                the crossbars and shuffle enginecan be set manually with the other provided
                parameters. The valid modes are:

                    - 'chan8': Corner-turn engine is mostly bypassed and raw
                      8-bit data from 8 channelizers is sent directly to the 8
                      CT-Engine outputs.

                    - 'chan4': Corner-turn engine is mostly bypassed and raw
                      8-bit data from 16 channelizers is sent directly to the
                      8 CT-Engine outputs.

                    - 'shuffle16 and 'chord16': A corner-turn operation is applied only
                      within the 16 channelizer outputs of this board.

                    - 'shuffle256': The corner-turn operation is applies
                      between 16 channelizers within a board and between the
                      16 boards within a crate using the backplane PCB links.

                    - 'shuffle512': The corner-turn operation is applies
                      between 16 channelizers within a board, between the 16
                      boards within a crate using the backplane PCB links, and
                      between 2 crates using the backplane QSFP links.

                    - 'corr4', 'corr8', 'corr16': The corner-turn engine is configured to feed
                      the internal firmware correlator (only if the firmware
                      was compiled with it).


            frames_per_packet (int): Number of frames to be packaged in a packet. Defaults to 2. Is
                limited by the amount of memory used by the FPGA to store data and flags.

            bin_map (dict): Describes the bin indices to be assigned to the
                outputs of each crossbar.

            cb1_lanes (int): Number of input lanes considered in the
                CROSSBAR1. Defaults to 16. Used only if `mode` = `None`.

            cb1_bins (int): Number of frequency bins to be included in the first
                crossbar. Defaults to 64. Used only if `mode` = `None`.


            dsmap (list) : Destination slot for each of the bin selectors of
               the first crossbar. Defaults to range(16). Used only in modes
               that use the backplane PCB links (``shuffle256``,
               ``shuffle512``).


            cb1_bypass (bool): If True, the first crossbar will be bypassed.
                Used only if `mode` = `None`.

            cb1_combine_data_flags (bool): if True, the data flags at the output of CROSSBAR1 will
                be packed in 32-bit words. Defaults to False, where each data word is associated
                with a data flag word filled with only 16 flag bits, which is needed to allow the
                next crossbar to further split the flags and data as part of the 2nd stage corner
                turn operation.

            cb2_lanes (list of tuple): list of (lower_lane, upper_lane), one for each of the 2
                CROSSBAR2 bin selector, that describe the range of input lanes that shall be read
                out by the bin selectors. Only partial number of lanes is used if the data is to be
                further recombined by the 3rd stage corner turn operation. If None, both bin
                selectros will combine data from all 16 input lanes.


            cb2_bins (int): Number of bins to be included in the bin selectors
                of the second crossbar.



            cb2_bypass (bool): If True the 2nd stage cirner-turn operation (CROSSBAR2) will be
                bypassed. Defaults to `False`


            bp_shuffle_bypass (bool): If `False`, the 16 data lanes form the CROSSBAR1 will be sent
                to other boards through the backplane PCB links, and the CROSSBAR2 will receiver 16
                lanes of data from the other boards (note than lane 0 is in fact always internal).
                If `True`, the 16 data lanes from the CROSSBAR1 will be forwrded directly to the the
                input of CROSSBAR2.


            crate_shuffle_bypass (bool): If False, half of the 8 outputs lanes of CROSSBAR2 will be
                sent to other boards in the other crate through the backplane QSFP links, and half
                will be sent to the CROSSBAR3. CROSSBAR3 will receive half its data from the other
                crate the corresponding board.If True, the 8 output lanes of CROSSBAR2 go directly
                to the 8 input lanes of CROSSBAR3.



            remap (bool). Defaults to True

            chan8_channel_map (list) : channel remapping to be used in `chan8` mode. Defaults to the
                identity map (range(8))

            send_flags (bool): If False, the Scaler and Frame flags will not be sent.

        Returns:

            list of the stream ids at each output lane of the corner turn engine

        Note:
            bin_map is a dict in the format

                {'cb1':cb1_bin_indices, 'cb2':cb2_bin_indices, 'cb3':cb3_bin_indices}

            where

            -   cb1_bin_indices (list of list): List of bin indices that will be
                selected for each bin selector (output lane) of the first
                crossbar.

                There are 16 bin selectors in the first crossbar.
                `cb1_bin_indices` should therefore consist of 16 lists, even
                in modes where only the first 8 bin selectors are used. The
                list is in the order of the destination slot. The list will be
                reordered to account for the backplane connectivity in the
                modes where those links are used..

                If `None`, the default bin selection is used: bins are
                interleaved between each bin selector. `shuffle16` and `corr16` select 128
                bins per bin selector (only the first 8 bin selector will be
                used);and `shuffle256`/`shuffle512` select 64 bins.

                The Bin selectors require 4 clocks to process a selected bin.
                A bin pair (even,odd) is processed on each clock. This means
                that if a bin from a bin pair is selected, no bin can be
                selected from the following 3 bin pairs.

            -  cb2_bin_indices (list of list): List of bin indices that will be
                selected for each bin selector of the second
                crossbar.

                There are 2 bin selectors in the second crossbar.

                If `None`, the default assignments are used. In `shuffle256`,
                all 64 incoming  bins are selected (from half the input lanes,
                to be reassembled by the following crossbar). In `shuffle512`,
                the bins are interleaved between the 2 bin_selectors, with the
                even bins sent to the even crate number. `cb2_bin_indices` is
                not used in other modes.

                The first list is always for the even crate and the second
                list is for the odd crate. In `shuffle512`, The list is reordered automatically
                to account for crate connectivity (i.e the lists for an odd
                crate are swapped).

            -   cb3_bin_indices (list of list): List of bin indices that will be
                selected for each bin selector of the third (and final)
                crossbar.

                There are 8 bin selectors in the third crossbar.

                If `None`, the default assignments are used.  In `shuffle512`,
                the bins are interleaved between the 8 bin_selectors.
                `cb3_bin_indices` is not used in other modes.

        Returns:

            list of stream IDs that will be present at each output (i.e GPU link) of the corner turn
            engine

        """

        cb1 = self.CROSSBAR
        cb2 = self.CROSSBAR2
        cb3 = self.CROSSBAR3

        number_of_cb1_bin_sel = 16
        number_of_cb2_bin_sel = 2
        number_of_cb3_bin_sel = 8

        if bin_map is None:
            bin_map = {}
        cb1_bin_indices = bin_map.get('cb1', None)
        cb2_bin_indices = bin_map.get('cb2', None)
        cb3_bin_indices = bin_map.get('cb3', None)

        cb2_ignore_lane = 0
        cb3_ignore_lane = 0

        if not (cb1_bin_indices is None or len(cb1_bin_indices) == number_of_cb1_bin_sel):
            raise ValueError('1st crossbar bin map should have %i lists of bins. Ir currently has %i' % (
                number_of_cb1_bin_sel, len(cb1_bin_indices)))
        if not (cb2_bin_indices is None or len(cb2_bin_indices) == number_of_cb2_bin_sel):
            raise ValueError('2nd crossbar bin map should have %i lists of bins. Ir currently has %i' % (
                number_of_cb2_bin_sel, len(cb2_bin_indices)))
        if not (cb3_bin_indices is None or len(cb3_bin_indices) == number_of_cb3_bin_sel):
            raise ValueError('3rd crossbar bin map should have %i lists of bins. Ir currently has %i' % (
                number_of_cb3_bin_sel, len(cb3_bin_indices)))

        def get_dest_slot_for_src_lane(src_lane):
            tx = (self.slot, src_lane)  # unique transmitter id (slot, lane)
            dest_slot = self.crate.get_matching_rx(tx)[0]
            return dest_slot

        # def get_src_slot_for_dest_lane(dest_lane):
        #     rx = (self.slot, dest_lane)
        #     src_slot = self.crate.get_matching_tx(rx)[0]
        #     return src_slot

        if frames_per_packet < 1 or frames_per_packet > 4:
            raise ValueError('Number of frames per packet must be between 1 and 4')
        if cb1_lanes in (4, 8, 12, 16):
            cb1_lanes = [(0, cb1_lanes // 4 - 1)] * number_of_cb1_bin_sel
        else:
            raise ValueError('Crossbar 1 number of input lanes must be 4,8,12 or 16')

        if cb2_lanes is None:
            cb2_lanes = ((0, 15), (0, 15))  # Both bin selectors

        # if cb2_lanes % 2:
        #     raise ValueError('Crossbar 2 number of input lanes must be a multiple of 2')

        cb2_timeout_period = None
        cb2_sof_window_stop = None

        if mode == 'chan8':

            if self.CROSSBAR1_TYPE=="URAM":
                self.set_corr_reset(0)
                self.set_chan_reset(0)
                return (0,)

            # Get raw data from the channelizer (all 32-bit sent as is). Only 8 lanes are available to the GPU.
            #############################
            # 1st Crossbar
            #############################
            # In bypass mode. Bin sel 0-15 get every word out of channelizers 0-15
            cb1_bypass = True
            cb1_four_bit = False
            cb1_combine_data_flags = 0
            cb1_bin_select_map = [[]] * number_of_cb1_bin_sel

            #################################
            # Backplane PCB (intra-crate) shuffle
            #################################
            bp_shuffle_bypass = True

            #############################
            # 2nd Crossbar
            #############################
            # Bypassed. Lanes are reordered to select which of the 8 channelizers we want to forward.
            cb2_lane_map = np.hstack((chan8_channel_map, chan8_channel_map))  # Here we could select which 8 inputs we want to stream to the GPU
            cb2_bypass = True
            cb2_bin_select_map = [[]] * number_of_cb2_bin_sel

            #################################
            # Backplane QSFP (crate) shuffle
            #################################
            crate_shuffle_bypass = True

            #############################
            # 3rd Crossbar
            #############################
            # Bypassed. No channel reordering.
            cb3_lane_map = list(range(8))
            cb3_bypass = True
            crate_number = self.crate.crate_number or 0 if self.crate else 0
            cb3_bin_select_map = [[]] * number_of_cb3_bin_sel

            cb3_output_bins = 0  # debug
            cb3_output_words_per_bin = 0  # debug
            cb3_output_data_flags_words_per_bin = 0  # debug
            cb3_output_frame_flags_words_per_frame = 0  # debug

            stream_type = 0
            cb2_ignore_lane = 0
            cb3_ignore_lane = 0


        elif mode == 'chan4':
            """
            In 'chan4' mode, we combine the high nibble of each pair of channelizers, allowing us to
            stream data from ALL channelizers. depending on the configuration of the channelizer, the data can be:

              - 4-bit ADC bit data from all channelizers
              - (4+4) bit FFT value from all channelizers

            """
            #############################
            # 1st Crossbar
            #############################
            # In bypass mode. Bin sel 0 selects 4+4 bit data for all bins of
            # channelizer 0 and 1, etc.  Bin selectors cover all 16
            # channelizers. Bin selectors 8-15 has the same info as bin sel
            # 0-7.
            cb1_bypass = True
            cb1_four_bit = True  # combines high nibbles of pair of input lanes
            cb1_combine_data_flags = 0
            cb1_bin_select_map = []

            #################################
            # Backplane PCB (intra-crate) shuffle
            #################################
            bp_shuffle_bypass = True

            #############################
            # 2nd Crossbar
            #############################
            # Bypassed. No channel reordering. Data from input lanes 8-15 is redundant and is not forwarded.
            cb2_lane_map = list(range(16))  # All information
            cb2_bypass = True
            cb2_bin_select_map = []

            #################################
            # Backplane QSFP (crate) shuffle
            #################################
            crate_shuffle_bypass = True

            #############################
            # 3rd Crossbar
            #############################
            # Bypassed. No channel reordering.
            cb3_lane_map = list(range(8))
            cb3_bin_select_map = []
            cb3_bypass = True
            crate_number = self.crate.crate_number if self.crate else 0
            stream_type = 0

        elif mode in ('shuffle16', 'chord16'):
            """
            In shuffle16 mode, each of the GPU links output data for 128 bins,
            each bins containing the data from 16 channels. The data for each
            channel is a byte, representing the FFT output as a (4+4) bit
            compler number.

            In this mode, each of the 16 bin selector select data from lane
            groups 0-3 (i.e. lanes 0-15). They each select 128 bins, i.e. 1/64
            of the 1024 bins incoming from the channelizer. The first 8 bin
            selectors contain all the data; the last 8 will not be passed by
            the following crossbar.

            There are 4 words per bins (16 channels). The data flags of two
            consecutive bins are combined to build a single data flag word.

            The packet format is as follows (assume one frame per packet):
                - Header: 4 words (16 bytes) for the header
                - Data block: 128 bins x 16 channels = 2056 bytes
                - Data flags block: We combine the data flags, so scaler flags
                  from two consecutive bins are combined into a single 32 bit
                  word ( one bit per channel for each of the 2 bins). This
                  represents 128*16/32=64 words = 256 bytes
                - Frame flags: 16 bit FFT overflow + 16 bit ADC overflow flags
                  per frame. This is 1 word = 4 bytes.
                - Packet flags: 32 bit word (4 bytes)
                - Total: 2328 bytes

            """

            #############################
            # 1st Crossbar
            #############################
            # Selects data from all channelizers, and spread the bins betweeen
            # the first 8 bin selectors so the data can go through the
            # following bypassed crossbars.
            cb1_bypass = False
            cb1_four_bit = True
            # BS0 grabs data from FIFO 0-1 (lanes 0-7), BS1 from FIFO 2-3
            # (lanes 8-15), repeat... We capture 2 words per bin in 2 clocks,
            # bins are separated by 2 clocks, so we have time to empty the
            # FIFO
            # set the first and last inlut lane group
            cb1_lanes = [(0, 3)] * number_of_cb1_bin_sel
            # Set the bins selected by each bin selector
            if cb1_bin_indices:
                cb1_bin_select_map = cb1_bin_indices
                cb1_bins = len(cb1_bin_indices[0])
            else:  # Use default
                cb1_bins = 128
                cb1_bin_spacing = 1024 // cb1_bins  # = 8 bins, or 4 clocks
                cb1_bin_select_map = [
                    np.arange(cb1_bins) * cb1_bin_spacing + (i % cb1_bin_spacing)
                    for i in range(number_of_cb1_bin_sel)]
            cb1_combine_data_flags = 1
            cb1_output_words_per_bin = 4
            cb1_output_bins = cb1_bins
            cb1_input_lanes_per_output_lane = 16
            cb1_output_data_flags_words_per_bin = (cb1_output_words_per_bin * 4.0) / (32 if cb1_combine_data_flags else 16)  # This is 0.5 if we combine_flags
            cb1_output_frame_flags_words_per_frame = 1


            #################################
            # Backplane PCB (intra-crate) shuffle
            #################################
            bp_shuffle_bypass = True

            #############################
            # 2nd Crossbar
            #############################
            # Bypassed. No channel reordering.

            cb2_lane_map = list(range(16))
            cb2_bypass = True
            cb2_input_words_per_bin = cb1_output_words_per_bin
            cb2_input_data_flags_words_per_bin = cb1_output_data_flags_words_per_bin
            cb2_input_frame_flags_words_per_frame = cb1_output_frame_flags_words_per_frame
            cb2_input_bins = cb1_bins
            # cb2_lanes : Not applicable because of bypass
            # cb2_bins : Not applicable because of bypass
            # cb2_bin_spacing : Not applicable because of bypass
            # cb2_bin_select_map : Not applicable because of bypass
            cb2_output_words_per_bin = cb2_input_words_per_bin
            cb2_output_bins = cb2_bins
            cb2_input_lanes_per_output_lane = cb1_input_lanes_per_output_lane
            cb2_output_data_flags_words_per_bin = cb2_input_data_flags_words_per_bin
            cb2_output_frame_flags_words_per_frame = cb2_input_frame_flags_words_per_frame

            crate_number = self.crate.crate_number or 0 if self.crate else 0
            stream_type = 1

            #################################
            # Backplane QSFP (crate) shuffle
            #################################
            crate_shuffle_bypass = True


            #############################
            # 3rd Crossbar
            #############################
            # Bypassed. No channel reordering.

            cb3_lane_map = list(range(8))
            cb3_bypass = True
            cb3_output_words_per_bin = cb2_input_words_per_bin
            cb3_output_bins = cb2_input_bins
            cb3_output_data_flags_words_per_bin = cb2_output_data_flags_words_per_bin
            cb3_output_frame_flags_words_per_frame = cb2_output_frame_flags_words_per_frame

        elif mode == 'shuffle128':
            """ Configures the corner-turn engine for a 8-board shuffle.

            Boards are assumed to be in slots 1-8 of the crates (slots 0-7).

            In this mode, crossbar1 merges 16 channelizer outputs into 8
            output lanes, with 128 bins per lane, like 'shuffle16'. However,
            we do not bypass the backplane PCB shuffle. The lanes that end up
            in one of the 8 populated slots are used. The backplane data rate is close to the limit (7.5 Gbps). We cannot send the flags due to bandwidth limitations.

            Crossbar2 remaps the 8 received lanes only. Crossbar 2 bin selectors doubles the data rate, so we can't use them and they are bypassed.

            Crossbar3 operates as in shuffle512: data from 8 input lanes is sent to the 8 output GPU lanes. We have 16 bins per lane.
            """
            if not self.slot:
                raise RuntimeError('The slot number is unknown. Cannot route the appropriate bins to the target boards in the same crate')

            #############################
            # 1st Crossbar
            #############################
            # Selects data from all channelizers, and spread the bins betweeen
            # the first 8 bin selectors so the data can go through the
            # following bypassed crossbars.
            cb1_bypass = False
            cb1_four_bit = True
            # Bin selectors grab data from all lanes
            cb1_lanes = [(0, 3)] * number_of_cb1_bin_sel

            # Set the bins selected by each bin selector
            if cb1_bin_indices:
                cb1_bin_select_map = cb1_bin_indices
                cb1_bins = len(cb1_bin_indices[0])
            else:  # Use default
                cb1_bins = 128
                cb1_bin_spacing = 1024 // cb1_bins  # = 8 bins, or 4 clocks

                # *** JFC debug
                cb1_bin_spacing = 8  # Normally  8 bins, which is the minimum
                cb1_bins = 1024 // cb1_bin_spacing


                cb1_bin_select_map = [
                    np.arange(cb1_bins) * cb1_bin_spacing + (i % cb1_bin_spacing)
                    for i in range(number_of_cb1_bin_sel)]

            # Reorder cb1_bin_select_map with the knowledge of the backplane
            # PCB links connectivity so slot 0 gets cb1_bin_select_map[0],
            # slot 1 gets cb1_bin_select_map[1] etc.
            cb1_bin_select_map = [
                cb1_bin_select_map[dsmap[get_dest_slot_for_src_lane(i) - 1]]
                for i in range(16)]

            cb1_combine_data_flags = 0 # we cannot combine the flags of two bins because we further bin-select them in crossbar 3
            send_flags = False  # There is not enough bandwidth on the backplane to send uncombined flags
            cb1_output_words_per_bin = 4
            cb1_output_bins = cb1_bins
            cb1_input_lanes_per_output_lane = 16
            cb1_output_data_flags_words_per_bin = send_flags * (cb1_output_words_per_bin * 4.0) / (32 if cb1_combine_data_flags else 16)  # This is 0.5 if we combine_flags
            cb1_output_frame_flags_words_per_frame = 1 * send_flags


            #################################
            # Backplane PCB (intra-crate) shuffle
            #################################
            bp_shuffle_bypass = False

            #############################
            # 2nd Crossbar
            #############################
            # Bypassed. No channel reordering.

            # CB2 packet aligner
            cb2_timeout_period = 0
            cb2_sof_window_stop = 55
            # CB2 REMAP
            # get a list that indicates which input lanes to get data from so we get it in increasing order of origin slot number
            # eg, if we are in slot 2 (zero-based)
            # (tx_slot => rx_lane) = (0=>1), (1->3), 2->0, 3->2, we have a map of [1, 3, 0, 2]
            # If slot 3 is not sending data,  we should discard  data from lanes lane_map[3] = 2
            cb2_lane_map = self.CROSSBAR2.compute_bp_shuffle_lane_map()
            print(f'slot {self.slot-1}/15: CB2 lane map: {cb2_lane_map}') #[ 0  8  6 14  5  4 15  7  3  2 11  1 13 12  9 10]
            cb2_ignore_lane = sum((1<<lane) for lane in cb2_lane_map[8:16])
            cb2_lane_map[8:16] = [0,0,0,0,0,0,0,0]  # hack to make sure all the bin sel input lanes have valid data
            print(f'slot {self.slot-1}/15: CB2 lane map: {cb2_lane_map}') #[ 0  8  6 14  5  4 15  7  3  2 11  1 13 12  9 10]
            cb2_bypass = True

            cb2_input_words_per_bin = cb1_output_words_per_bin
            cb2_input_data_flags_words_per_bin = cb1_output_data_flags_words_per_bin
            cb2_input_frame_flags_words_per_frame = cb1_output_frame_flags_words_per_frame
            cb2_input_bins = cb1_output_bins
            # cb2_lanes : Not applicable because of bypass
            # cb2_bins : Not applicable because of bypass
            # cb2_bin_spacing : Not applicable because of bypass
            # cb2_bin_select_map : Not applicable because of bypass
            cb2_output_words_per_bin = cb2_input_words_per_bin
            cb2_output_bins = cb2_input_bins
            cb2_input_lanes_per_output_lane = cb1_input_lanes_per_output_lane
            cb2_output_data_flags_words_per_bin = cb2_input_data_flags_words_per_bin
            cb2_output_frame_flags_words_per_frame = cb2_input_frame_flags_words_per_frame

            crate_number = self.crate.crate_number or 0 if self.crate else 0
            stream_type = 1

            #################################
            # Backplane QSFP (crate) shuffle
            #################################
            crate_shuffle_bypass = True


            #############################
            # 3rd Crossbar
            #############################
            # Crossbar 2 just passed the 8 lanes from the 8 boards, reordered by slot number origin
            # Crossbar 3 can finish the job, i.e. channels are spread over its
            # 8 input links.

            # Remap input lanes so data is selected in proper channel order
            cb3_lane_map = [0, 1, 2, 3, 4, 5, 6, 7]
            cb3_ignore_lane = 0
            cb3_bypass = False
            cb3_input_words_per_bin = cb2_output_words_per_bin
            cb3_input_data_flags_words_per_bin = cb2_output_data_flags_words_per_bin
            cb3_input_frame_flags_words_per_frame = cb2_output_frame_flags_words_per_frame
            cb3_input_bins = cb2_output_bins
            cb3_lanes = [(0, 7)] * number_of_cb3_bin_sel
            cb3_input_lanes_per_output_lane = cb3_lanes[0][1] - cb3_lanes[0][0] + 1  # 8 input lanes per bin sel output
            cb3_combine_data_flags = False  # hardwired to False in crossbar 3

            # Select the bins to be assigned to each bin selector output.
            if cb3_bin_indices is not None:
                cb3_bins = len(cb3_bin_indices[0])
                cb3_bin_select_map = cb3_bin_indices
            else:
                # Default: we select 1/8th of the incoming bins from all the input lanes
                # We merge data from 8 full bandwidth input lanes, so we select 1/8th of the bins on each output lane
                cb3_bins = cb3_input_bins // 8
                cb3_bin_spacing = 8  # use maximum possible number so we minimize FIFO usage
                cb3_bin_select_map = [
                    np.arange(cb3_bins) * cb3_bin_spacing + i
                    for i in range(number_of_cb3_bin_sel)]

            cb3_output_words_per_bin = cb3_input_words_per_bin * 8
            cb3_output_bins = cb3_bins
            cb3_output_data_flags_words_per_bin = (
                cb3_input_data_flags_words_per_bin * cb3_input_lanes_per_output_lane
                // (2 if cb3_combine_data_flags else 1))
            cb3_output_frame_flags_words_per_frame = (
                cb3_input_frame_flags_words_per_frame * cb3_input_lanes_per_output_lane)

        elif mode == 'shuffle256':
            if not self.slot:
                raise RuntimeError('The slot number is unknown. Cannot route the appropriate bins to the target boards in the same crate')

            #############################
            # 1st Crossbar
            #############################
            # Selects data from all channelizers, and spread the bins betweeen
            # all 16 bin selectors to spread the data across 16 boards in a crate.
            cb1_bypass = False
            cb1_four_bit = True
            cb1_combine_data_flags = 0
            cb1_lanes = [(0, 3)] * number_of_cb1_bin_sel
            # Select the bins to be assigned to each bin selector output.
            # Here, we assume that the  list is in the order of the
            # destination slot.
            if cb1_bin_indices is not None:
                cb1_bin_select_map = cb1_bin_indices
                cb1_bins = len(cb1_bin_indices[0])
            else:
                cb1_bins = 64
                cb1_bin_spacing = 1024 // cb1_bins
                cb1_bin_select_map = [
                    np.arange(cb1_bins) * cb1_bin_spacing + i
                    for i in range(number_of_cb1_bin_sel)]
            # Reorder cb1_bin_select_map with the knowledge of the backplane
            # PCB links connectivity so slot 0 gets cb1_bin_select_map[0],
            # slot 1 gets cb1_bin_select_map[1] etc.
            cb1_bin_select_map = [
                cb1_bin_select_map[dsmap[get_dest_slot_for_src_lane(i) - 1]]
                for i in range(16)]
            cb1_output_words_per_bin = 16 // 4
            cb1_output_bins = cb1_bins

            #################################
            # Backplane PCB (intra-crate) shuffle
            #################################
            bp_shuffle_bypass = False

            #############################
            # 2nd Crossbar
            #############################
            #
            # CB2 has 2 BIN_SEL
            # Each BS captures data from 16 input lanes and has 4 outputs.
            # Each output covers gathers data from 4 input lanes (sublanes 0-3).
            #   Output 0: Sublanes 0-3 = Input Lanes 0-3
            #   Output 1: Sublanes 0-3 = Input Lanes 4-7
            #   Output 2: Sublanes 0-3 = Input Lanes 8-11
            #   Output 3: Sublanes 0-3 = Input Lanes 12-15
            #
            # In this config, we will bypass the crate_shuffle. We therefore want all outputs to
            # output the same bins. So BS0 gets data from half of its sublanes, and BS1 gets data
            # from the other half.
            #
            # CB2 Output Lane 0: BS0.0: all 64 bins from sublanes 0-1 (Input lanes 0-1   = CH0-31)
            # CB2 Output Lane 1: BS0.1: all 64 bins from sublanes 0-1 (Input lanes 4-5   = CH64-95)
            # CB2 Output Lane 2: BS0.2: all 64 bins from sublanes 0-1 (Input lanes 8-9   = CH128-159)
            # CB2 Output Lane 3: BS0.3: all 64 bins from sublanes 0-1 (Input lanes 12-13 = CH192-223)
            # CB2 Output Lane 4: BS1.0: all 64 bins from sublanes 2-3 (Input lanes 2-3   = CH32-63)
            # CB2 Output Lane 5: BS1.1: all 64 bins from sublanes 2-3 (Input lanes 6-7   = CH96-127)
            # CB2 Output Lane 6: BS1.2: all 64 bins from sublanes 2-3 (Input lanes 10-11 = CH160-191)
            # CB2 Output Lane 7: BS1.3: all 64 bins from sublanes 2-3 (Input lanes 14-15 = CH224-255)
            # Crate shuffle is bypassed.
            # CB3 inputs are therefore identical to CB2 output
            # CB3 lane map selects data in the order: [BS0.0, BS1.0, BS0.1, BS1.1 ...]
            # CB3 remapped BIN_SEL inputs are
            #    CB3 Input Lane 0: 64 bins CH0-31
            #    CB3 Input Lane 1: 64 bins CH32-63
            #    CB3 Input Lane 2: 64 bins CH64-95
            #    CB3 Input Lane 3: 64 bins CH96-127
            #    CB3 Input Lane 4: 64 bins CH128-159
            #    CB3 Input Lane 5: 64 bins CH160-191
            #    CB3 Input Lane 6: 64 bins CH192-223
            #    CB3 Input Lane 7: 64 bins CH224-255
            # CB3 outputs are:
            #    CB3 Output Lane 0: BS0.0: 8 bins (0,8...)  from sublanes 0-7 (Input lanes 0-7 =CH0-511)
            #    CB3 Output Lane 1: BS0.1: 8 bins (1,9...)  from sublanes 0-7 (Input lanes 0-7 =CH0-511)
            #    CB3 Output Lane 2: BS0.2: 8 bins (2,10...) from sublanes 0-7 (Input lanes 0-7 =CH0-511)
            #    CB3 Output Lane 3: BS0.3: 8 bins (3,11...) from sublanes 0-7 (Input lanes 0-7 =CH0-511)
            #    CB3 Output Lane 4: BS1.0: 8 bins (4,12...) from sublanes 0-7 (Input lanes 0-7 =CH0-511)
            #    CB3 Output Lane 5: BS1.1: 8 bins (5,13...) from sublanes 0-7 (Input lanes 0-7 =CH0-511)
            #    CB3 Output Lane 6: BS1.2: 8 bins (6,14...) from sublanes 0-7 (Input lanes 0-7 =CH0-511)
            #    CB3 Output Lane 7: BS1.3: 8 bins (7,15...) from sublanes 0-7 (Input lanes 0-7 =CH0-511)

            # CB2 ALIGN (JM changed cb2_sof_window_stop from 70 to 50 which the default value of CROSSBAR.SOF_WINDOW_STOP in shuffle_crossbar module. When 70, get errors when establishing connection with gpu nodes, reporting wrong packet size). JFC: CHanged to 55 during backplane shuffle debugging
            cb2_timeout_period = 0
            cb2_sof_window_stop = 55
            # CB2 REMAP
            cb2_lane_map = self.CROSSBAR2.compute_bp_shuffle_lane_map()
            cb2_bypass = False
            # CB2 BIN_SEL
            cb2_input_words_per_bin = cb1_output_words_per_bin
            cb2_input_bins = cb1_output_bins
            cb2_input_data_flags_words_per_bin = 1
            cb2_input_frame_flags_words_per_frame = 1

            # BS0 selects sublanes 0-1, i.e. its 4 outputs gather data from lanes 0-1, 4-5, 8-9, and 12-13
            # BS1 selects sublanes 2-3 (lanes 2-3, 6-7, 10-12 and 14-15 )
            cb2_lanes = ((0, 1), (2, 3))
            cb2_combine_data_flags = True  # hardwired to True in crossbar 2
            cb2_input_lanes_per_output_lane = cb2_lanes[0][1] - cb2_lanes[0][0] + 1  # 2 input lanes per output

            # Select the bins to be assigned to each bin selector output.
            if cb2_bin_indices is not None:
                cb2_bins = len(cb2_bin_indices[0])
                cb2_bin_select_map = cb2_bin_indices
            else:
                # Default: we select all 64 incoming bins, but from half the input lanes
                cb2_bins = 64
                cb2_bin_spacing = 1
                cb2_bin_select_map = [np.arange(cb2_bins) * cb2_bin_spacing
                                      for i in range(number_of_cb2_bin_sel)]

            cb2_output_words_per_bin = cb2_input_words_per_bin * cb2_input_lanes_per_output_lane

            cb2_output_data_flags_words_per_bin = (
                cb2_input_data_flags_words_per_bin * cb2_input_lanes_per_output_lane
                // (2 if cb2_combine_data_flags else 1))

            cb2_output_frame_flags_words_per_frame = (
                cb2_input_frame_flags_words_per_frame * cb2_input_lanes_per_output_lane)

            cb2_output_bins = cb2_bins
            crate_number = (self.crate.crate_number or 0) if self.crate else 0
            stream_type = 2

            #################################
            # Backplane QSFP (crate) shuffle
            #################################
            crate_shuffle_bypass = True

            #############################
            # 3rd Crossbar
            #############################
            # Crossbar 2 partially combined the channels in a way that
            # Crossbar 3 can finish the job, i.e. channels are spread over its
            # 8 input links.

            # Remap input lanes so data is selected in proper channel order In
            # shuffle256, input lanes 0-3 are from BS2 input anes 0-1, 4-5,
            # 8-9, and 12-13, and input lanes 4=7 are from lanes 2-3, 6-7,
            # 10-12 and 14-15.
            cb3_lane_map = [0, 4, 1, 5, 2, 6, 3, 7]
            cb3_bypass = False
            cb3_input_words_per_bin = cb2_output_words_per_bin
            cb3_input_data_flags_words_per_bin = cb2_output_data_flags_words_per_bin
            cb3_input_frame_flags_words_per_frame = cb2_output_frame_flags_words_per_frame
            cb3_input_bins = cb2_output_bins
            cb3_lanes = [(0, 7)] * number_of_cb3_bin_sel
            cb3_input_lanes_per_output_lane = cb3_lanes[0][1] - cb3_lanes[0][0] + 1  # 8 input lanes per bin sel output
            cb3_combine_data_flags = False  # hardwired to False in crossbar 3

            # Select the bins to be assigned to each bin selector output.
            if cb3_bin_indices is not None:
                cb3_bins = len(cb3_bin_indices[0])
                cb3_bin_select_map = cb3_bin_indices
            else:
                # Default: we select 1/8th of the incoming bins from all the input lanes
                # We merge data from 8 full bandwidth input lanes, so we select 1/8th of the bins on each output lane
                cb3_bins = 8
                cb3_bin_spacing = 8  # use maximum possible number so we minimize FIFO usage
                cb3_bin_select_map = [
                    np.arange(cb3_bins) * cb3_bin_spacing + i
                    for i in range(number_of_cb3_bin_sel)]

            cb3_output_words_per_bin = cb3_input_words_per_bin * 8
            cb3_output_bins = cb3_bins
            cb3_output_data_flags_words_per_bin = (
                cb3_input_data_flags_words_per_bin * cb3_input_lanes_per_output_lane
                // (2 if cb3_combine_data_flags else 1))
            cb3_output_frame_flags_words_per_frame = (
                cb3_input_frame_flags_words_per_frame * cb3_input_lanes_per_output_lane)

        elif mode == 'shuffle512':
            """
            Like shuffle256, but adds a corner-turn operation between pair of crates using the
            backplane QSFP connections between them.
            """
            if not self.slot:
                raise RuntimeError('The slot number is unknown. Cannot route the appropriate bins '
                                   'to the target boards in the same crate')

            #############################
            # 1st Crossbar
            #############################
            # Selects data from all channelizers, and spread the bins between
            # all 16 bin selectors to spread the data across 16 boards in a crate.
            cb1_bypass = False
            cb1_four_bit = True
            cb1_combine_data_flags = 0
            # get data from channel group 0 - 3. Each channel group combines data from 4 channelizers (in 4 bit mode)
            cb1_lanes = [(0, 3)] * number_of_cb1_bin_sel
            # Select the bins to be assigned to each bin selector output.
            # Here, we assume that the  list is in the order of the
            # destination slot.
            if cb1_bin_indices is not None:
                cb1_bin_select_map = cb1_bin_indices
                cb1_bins = len(cb1_bin_indices[0])
            else:
                cb1_bins = 64
                cb1_bin_spacing = 1024 // cb1_bins
                cb1_bin_select_map = [
                    np.arange(cb1_bins) * cb1_bin_spacing + i
                    for i in range(number_of_cb1_bin_sel)]
            # Reorder cb1_bin_select_map with the knowledge of the backplane
            # PCB links connectivity so slot 0 gets cb1_bin_select_map[0],
            # slot 1 gets cb1_bin_select_map[1] etc.
            cb1_bin_select_map = [
                cb1_bin_select_map[dsmap[get_dest_slot_for_src_lane(i) - 1]]
                for i in range(16)]

            # Output packet geometry
            cb1_output_words_per_bin = 16 // 4
            cb1_output_bins = cb1_bins

            #################################
            # Backplane PCB (intra-crate) shuffle
            #################################
            bp_shuffle_bypass = False

            # In this config, we do not bypass the crate_shuffle. Half the bins are sent out, and we
            # receive bins that are the same as those of the direct lanes.
            #
            # We therefore want BS0 to get half the bins from all input lanes, and BS1 gets the
            # other half of the bins also from all input lanes.
            #
            # CB2 Output lanes are:
            #    CB2 Output Lane 0: BS0.0: 32 even bins from sublanes 0-3 (Input lanes 0-3   = CH0-63)
            #    CB2 Output Lane 1: BS0.1: 32 even bins from sublanes 0-3 (Input lanes 4-7   = CH64-127)
            #    CB2 Output Lane 2: BS0.2: 32 even bins from sublanes 0-3 (Input lanes 8-11  = CH128-191)
            #    CB2 Output Lane 3: BS0.3: 32 even bins from sublanes 0-3 (Input lanes 12-15 = CH192-255)
            #    CB2 Output Lane 4: BS1.0: 32 odd  bins from sublanes 0-3 (Input lanes 0-3   = CH0-63)
            #    CB2 Output Lane 5: BS1.1: 32 odd  bins from sublanes 0-3 (Input lanes 4-7   = CH64-127)
            #    CB2 Output Lane 6: BS1.2: 32 odd  bins from sublanes 0-3 (Input lanes 8-11  = CH128-191)
            #    CB2 Output Lane 7: BS1.3: 32 odd  bins from sublanes 0-3 (Input lanes 12-15 = CH192-255)
            # Crate shuffle is *not* bypassed
            # CB3 inputs are therefore:
            #    CB3 Input Lane 0: 32 even bins CH0-63
            #    CB3 Input Lane 1: 32 even bins CH64-127
            #    CB3 Input Lane 2: 32 even bins CH128-191
            #    CB3 Input Lane 3: 32 even bins CH192-255
            #    CB3 Input Lane 4: 32 even bins CH256-319
            #    CB3 Input Lane 5: 32 even bins CH320-383
            #    CB3 Input Lane 6: 32 even bins CH384-447
            #    CB3 Input Lane 7: 32 even bins CH448-511
            # CB3 lane map selects data in the input lane order: [0, 1, 2, 3 ... 7]
            # CB3 BIN sel inputs are therefore identical to CB3 inputs
            # CB3 outputs are:
            #    CB3 Output Lane 0: BS0.0: 4 bins (0,8...)  from sublanes 0-7 (Input lanes 0-7 =CH0-511)
            #    CB3 Output Lane 1: BS0.1: 4 bins (1,9...)  from sublanes 0-7 (Input lanes 0-7 =CH0-511)
            #    CB3 Output Lane 2: BS0.2: 4 bins (2,10...) from sublanes 0-7 (Input lanes 0-7 =CH0-511)
            #    CB3 Output Lane 3: BS0.3: 4 bins (3,11...) from sublanes 0-7 (Input lanes 0-7 =CH0-511)
            #    CB3 Output Lane 4: BS1.0: 4 bins (4,12...) from sublanes 0-7 (Input lanes 0-7 =CH0-511)
            #    CB3 Output Lane 5: BS1.1: 4 bins (5,13...) from sublanes 0-7 (Input lanes 0-7 =CH0-511)
            #    CB3 Output Lane 6: BS1.2: 4 bins (6,14...) from sublanes 0-7 (Input lanes 0-7 =CH0-511)
            #    CB3 Output Lane 7: BS1.3: 4 bins (7,15...) from sublanes 0-7 (Input lanes 0-7 =CH0-511)

            #############################
            # 2nd Crossbar
            #############################

            # CB2 ALIGN
            # cb2_timeout_period = 0

            crate_number = (self.crate.crate_number or 0) if self.crate else 0
            stream_type = 3  # crossbar 3-level data

            # Input packet geometry

            cb2_input_data_flags_words_per_bin = 1
            cb2_input_words_per_bin = cb1_output_words_per_bin
            cb2_input_bins = cb1_output_bins
            cb2_input_frame_flags_words_per_frame = 1

            # Configuration
            cb2_sof_window_stop = 55
            cb2_bypass = False
            # CB2 REMAP
            cb2_lane_map = self.CROSSBAR2.compute_bp_shuffle_lane_map()
            # CB@ BIN_SEL
            cb2_lanes = [(0, 3), (0, 3)]  # Every output of both bin sels get data from all the 4 sublanes they get.
            cb2_input_lanes_per_output_lane = cb2_lanes[0][1] - cb2_lanes[0][0] + 1  # 4 input lanes per output
            # Select the bins to be assigned to each bin selector output.
            if cb2_bin_indices is not None:
                cb2_bin_select_map = cb2_bin_indices
                cb2_bins = len(cb2_bin_indices[0])
            else:
                # Default: we select half the bins (from all input lanes)
                cb2_bins = cb1_output_bins // number_of_cb2_bin_sel  # 64/2 = 32
                cb2_bin_spacing = number_of_cb2_bin_sel  # 2
                cb2_bin_select_map = [
                    np.arange(cb2_bins) * cb2_bin_spacing + i
                    for i in range(number_of_cb2_bin_sel)]
            # swap bin selection list on odd crates so the bins on the first
            # list are sent to the other (even) crate
            if crate_number & 1:
                cb2_bin_select_map = cb2_bin_select_map[::-1]

            cb2_combine_data_flags = True  # hardwired to True in crossbar 2

            # Output packet geometry

            cb2_output_words_per_bin = cb2_input_words_per_bin * cb2_input_lanes_per_output_lane
            cb2_output_bins = cb2_bins
            cb2_output_data_flags_words_per_bin = (
                cb2_input_data_flags_words_per_bin * cb2_input_lanes_per_output_lane //
                (2 if cb2_combine_data_flags else 1))
            cb2_output_frame_flags_words_per_frame = (
                cb2_input_frame_flags_words_per_frame * cb2_input_lanes_per_output_lane)
            # print("cb2_output frame flags words=%i, input frame flags words=%i, input_lanes=%i" % (
            #   cb2_output_frame_flags_words_per_frame,
            #   cb2_input_frame_flags_words_per_frame,
            #   cb2_input_lanes_per_output_lane))

            #################################
            # Backplane QSFP (crate) shuffle
            #################################
            crate_shuffle_bypass = False
            # crate_shuffle_bypass = True #***debug

            #############################
            # 3rd Crossbar
            #############################
            # Crossbar gathers the data from the 2 crates.

            # Input packet geometry
            cb3_input_words_per_bin = cb2_output_words_per_bin
            cb3_input_data_flags_words_per_bin = cb2_output_data_flags_words_per_bin
            cb3_input_frame_flags_words_per_frame = cb2_output_frame_flags_words_per_frame
            cb3_input_bins = cb2_output_bins

            # Configuration

            # Input lane remapping to keep the channel number consistent
            # whether we are on an odd or even crate number.
            #
            # For the even crate, input lane 0-3 contains the local even bins
            # for the low channels, and lane 4-7 get the even bins from the
            # other crate (high channels). For the odd crate, input lane  0-3
            # are the odd bins from the local high channels, and lanes 3-7 are
            # the odd bins from the other crate low channels.
            #
            # JM: modified this line to have consistent input ordering between
            # pairs of crates. Before this change this line was just range(8)
            cb3_lane_map = [4, 5, 6, 7, 0, 1, 2, 3] if (crate_number & 1) \
                else [0, 1, 2, 3, 4, 5, 6, 7]
            cb3_bypass = False
            cb3_lanes = [(0, 7)] * number_of_cb3_bin_sel
            cb3_input_lanes_per_output_lane = cb3_lanes[0][1] - cb3_lanes[0][0] + 1  # 8 input lanes per bin sel output

            # Select the bins to be assigned to each bin selector output.
            if cb3_bin_indices is not None:
                cb3_bins = len(cb3_bin_indices[0])
                cb3_bin_select_map = cb3_bin_indices
            else:
                # Default: we select 1/8th of the incoming bins from all the input lanes
                cb3_bins = cb2_output_bins // number_of_cb3_bin_sel  # 32/8 = 4
                cb3_bin_spacing = number_of_cb3_bin_sel  # = 8
                cb3_bin_select_map = [
                    np.arange(cb3_bins) * cb3_bin_spacing + i
                    for i in range(number_of_cb3_bin_sel)]
            cb3_combine_data_flags = False  # hardwired to False in crossbar 3

            # Output packet geometry
            cb3_output_words_per_bin = cb3_input_words_per_bin * cb3_input_lanes_per_output_lane
            cb3_output_bins = cb3_bins
            cb3_output_data_flags_words_per_bin = (
                cb3_input_data_flags_words_per_bin * cb3_input_lanes_per_output_lane
                // (2 if cb3_combine_data_flags else 1))
            cb3_output_frame_flags_words_per_frame = (
                cb3_input_frame_flags_words_per_frame * cb3_input_lanes_per_output_lane)

        elif mode in ('corr16', ):
            """
            Implement the corner-turn operation for the 16-channel firmware correlator embedded in
            the same FPGA. in this mode, we simply enable the 1st crossbar. The 2nd and 3rd
            crossbars are not present in the firmware.
            """

            # corr16 mode uses the hardwired corner-turn, so there is nothing to do here

            if self.CT_LEVEL == 1:
                return range(16)

            #############################
            # 1st Crossbar
            #############################
            # Reorders the data from the 16 local channelizers

            number_of_cb1_bin_sel = 8
            cb1_bypass = False
            cb1_four_bit = True
            # BS0 grabs data from FIFO 0-1 (lanes 0-7), BS1 from FIFO 2-3
            # (lanes 8-15), repeat... We capture 2 words per bin in 2 clocks,
            # bins are separated by 2 clocks, so we have time to empty the
            # FIFO
            cb1_lanes = [(0, 3)] * number_of_cb1_bin_sel
            cb1_combine_data_flags = 1
            # Select the bins to be assigned to each bin selector output.
            if cb1_bin_indices:
                cb1_bin_select_map = cb1_bin_indices
                cb1_bins = len(cb1_bin_indices[0])
            else:
                cb1_bins = 128
                cb1_bin_spacing = 1024 // cb1_bins  # = 8 = 4 clocks
                cb1_bin_select_map = [
                    np.arange(cb1_bins) * cb1_bin_spacing + (i % cb1_bin_spacing)
                    for i in range(number_of_cb1_bin_sel)]
            cb1_output_words_per_bin = 4
            cb1_output_bins = cb1_bins

            #################################
            # Backplane PCB (intra-crate) shuffle
            #################################
            # Not implemented in the firmware correlator

            #################################
            # Backplane QSFP (crate) shuffle
            #################################
            # Not implemented in the firmware correlator

            #############################
            # 2nd and 3rd Crossbar
            #############################
            # Not implemented in the firmware correlator
            # We define default values to prevent the packet size computation to fail.
            cb2_bypass = True
            cb3_bypass = True
            cb3_output_bins = cb1_bins
            cb3_output_words_per_bin = 0  # To be updated
            cb3_output_data_flags_words_per_bin = 0  # To be updated
            cb3_output_frame_flags_words_per_frame = 0  # To be updated
            stream_type = 0  # not used, as the shuffled packets are correlated never get out of the FPGA
            crate_number = 0  # idem

        elif mode == 'corr4':
            """
            Implement the corner-turn operation for the 16-channel firmware correlator embedded in
            the same FPGA. in this mode, we simply enable the 1st crossbar. The 2nd and 3rd
            crossbars are not present in the firmware.
            """
            #############################
            # 1st Crossbar
            #############################
            # Reorders the data from the 16 local channelizers

            number_of_cb1_bin_sel = 8
            cb1_bypass = False
            cb1_four_bit = True
            send_flags = False
            # BS0 grabs data from FIFO 0-1 (lanes 0-7), BS1 from FIFO 2-3
            # (lanes 8-15), repeat... We capture 2 words per bin in 2 clocks,
            # bins are separated by 2 clocks, so we have time to empty the
            # FIFO
            cb1_lanes = [(0, 0)] * number_of_cb1_bin_sel
            cb1_combine_data_flags = 1
            # Select the bins to be assigned to each bin selector output.
            if cb1_bin_indices:
                cb1_bin_select_map = cb1_bin_indices
                cb1_bins = len(cb1_bin_indices[0])
            else:
                cb1_bins = 256
                cb1_bin_spacing = 1024 // cb1_bins  # = 8 = 4 clocks
                cb1_bin_select_map = [
                    np.arange(cb1_bins) * cb1_bin_spacing + (i % cb1_bin_spacing)
                    for i in range(number_of_cb1_bin_sel)]
            cb1_output_words_per_bin = 1
            cb1_output_bins = cb1_bins

            #################################
            # Backplane PCB (intra-crate) shuffle
            #################################
            # Not implemented in the firmware correlator

            #################################
            # Backplane QSFP (crate) shuffle
            #################################
            # Not implemented in the firmware correlator

            #############################
            # 2nd and 3rd Crossbar
            #############################
            # Not implemented in the firmware correlator
            # We define default values to prevent the packet size computation to fail.
            cb2_bypass = True
            cb3_bypass = True
            cb3_output_bins = cb1_bins
            cb3_output_words_per_bin = 0  # To be updated
            cb3_output_data_flags_words_per_bin = 0  # To be updated
            cb3_output_frame_flags_words_per_frame = 0  # To be updated
            stream_type = 0  # not used, as the shuffled packets are correlated never get out of the FPGA
            crate_number = 0  # idem

        elif mode in ('corr8', 'corr32'):
            """
            Implement the corner-turn operation for the 16-channel firmware correlator embedded in
            the same FPGA. in this mode, we simply enable the 1st crossbar. The 2nd and 3rd
            crossbars are not present in the firmware.
            """
            if self.CT_TYPE == "UCT": # hack
                self.set_corr_reset(0)
                self.set_chan_reset(0)
                return 0

            raise RuntimeError('Unsupported mode corr8 with current firmware configuration')

        elif mode is None:  # Manual config
            cb1_four_bit = True

            # cb1_bypass = False

            # Select the bins to be assigned to each bin selector output.
            if cb1_bin_indices:
                cb1_bin_select_map = cb1_bin_indices
            else:
                cb1_bin_spacing = 1024 // cb1_bins
                cb1_bin_select_map = [
                    (np.arange(cb1_bins) * cb1_bin_spacing + i) % 1024
                    for i in range(number_of_cb1_bin_sel)]

            #################################
            # Backplane QSFP (crate) shuffle
            #################################
            crate_shuffle_bypass = 1

            crate_number = self.crate.crate_number if self.crate else 0
            stream_type = 4

            cb2_input_bins = cb1_bins
            if bp_shuffle_bypass and self.slot is not None:
                cb2_lane_map = self.CROSSBAR2.compute_bp_shuffle_lane_map()
            else:
                cb2_lane_map = list(range(16))

            cb2_input_frame_flags_words_per_frame = 1
            cb2_input_data_flags_words_per_bin = 1
            # Select the bins to be assigned to each bin selector output.
            if cb2_bin_indices:
                cb2_bins = len(cb2_bin_indices[0])
                cb2_bin_select_map = cb2_bin_indices
            else:
                # Default: we select all 64 incoming bins, but from half the input lanes
                cb2_bins = cb2_input_bins
                cb2_bin_spacing = 1
                cb2_bin_select_map = [np.arange(cb2_bins) * cb2_bin_spacing
                                      for i in range(number_of_cb2_bin_sel)]

            #################################
            # Backplane QSFP (crate) shuffle
            #################################
            # Not supported in this mode
            crate_shuffle_bypass = True

            #############################
            # 3rd Crossbar
            #############################
            # Not supported in this mode
            cb3_bypass = True
            cb3_lane_map = list(range(8))

            cb1_output_words_per_bin = cb1_lanes[0][1] - cb1_lanes[0][0] + 1
            cb1_output_bins = cb1_bins

        else:
            raise ValueError('Unknown mode')

        # cb2_payload_size = header_size + packet_flags_size + frames_per_packet \
        #   * (cb2_input_words_per_bin * cb2_bins* cb2_lanes + 1*cb2_bins*cb2_lanes/2 + cb2_lanes) * 4

        self.logger.debug(
            '%r: Configuring crossbars 1 & 2 with frames_per_packet=%i, cb1_lanes=%s, cb1_bins=%i, '
            'cb2_lanes=%s, cb2_bins=%i, cb2_bypass=%s, bp_shuffle_bypass=%s' % (
                self,
                frames_per_packet,
                cb1_lanes,
                cb1_bins,
                cb2_lanes,
                cb2_bins,
                bool(cb2_bypass),
                bool(bp_shuffle_bypass)))

        # Put everything in reset
        self.fpga.set_chan_reset(1)
        self.fpga.set_corr_reset(1)

        # slot_number = self.slot - 1 if self.slot is not None else 0
        slot_number = self.slot - 1 if self.slot else 0  # SC 03/17/2019 changed for individual iceboard

        ###########################
        #  Configure CROSSBAR 1
        ###########################

        stream_id = [((stream_type << 12) | (crate_number << 8) | (slot_number << 4) | lane)
                     for lane in range(cb1.NUMBER_OF_CROSSBAR_OUTPUTS)]
        for (cb1_output_lane, bs) in enumerate(cb1):
            bs.BYPASS = cb1_bypass
            bs.STREAM_ID = stream_id[cb1_output_lane] >> 4  # lane is already hardwared in the last 4 bits
            bs.COMBINE_DATA_FLAGS = cb1_combine_data_flags
            bs.SEND_FLAGS = send_flags
            bs.GROUP_FRAMES = frames_per_packet
            # print(stream_type, crate_number, slot_number)
            bs.FOUR_BITS = cb1_four_bit
            bs.FIRST_FIFO_NUMBER = cb1_lanes[cb1_output_lane][0]
            bs.LAST_FIFO_NUMBER = cb1_lanes[cb1_output_lane][1]
            # bs.NUMBER_OF_LANES = cb1_lanes
            bs.select_bins(cb1_bin_select_map[cb1_output_lane])

        ###########################
        # Configure BP_SHUFFLE
        ###########################
        if self.BP_SHUFFLE:
            # Configure PCB (intra-crate) shuffle
            self.BP_SHUFFLE.BYPASS_PCB_SHUFFLE = bp_shuffle_bypass
            # Configure CRATE (inter-crate) shuffle
            self.BP_SHUFFLE.BYPASS_QSFP_SHUFFLE = crate_shuffle_bypass
        elif not bp_shuffle_bypass:
            raise RuntimeError("The FPGA firmware implement BP_SHUFFLE in the '%s' operational mode "
                               "(shuffle bypass flag is not set in that mode)", mode)
        ###########################
        # Configure CROSSBAR 2
        ###########################
        if cb2:
            if cb2_bypass:  # if we bypass, just remap the stream ids from the previous crossbar
                stream_id = [stream_id[i] for i in cb2_lane_map]
            else:  # otherwise, the bin selector overrides
                stream_id = [((stream_type << 12) | (crate_number << 8) | (slot_number << 4) | lane)
                             for lane in range(cb2.NUMBER_OF_CROSSBAR_OUTPUTS)]
            cb2.IGNORE_LANE = cb2_ignore_lane
            cb2.set_lane_map(cb2_lane_map)
            if cb2_timeout_period is not None:
                cb2.TIMEOUT_PERIOD = cb2_timeout_period
            if cb2_sof_window_stop is not None:
                cb2.SOF_WINDOW_STOP = cb2_sof_window_stop
            for (cb2_bin_sel, bs) in enumerate(cb2):
                bs.BYPASS = bool(cb2_bypass)
                if not cb2_bypass:
                    bs.STREAM_ID = stream_id[cb2_bin_sel * cb2.NUMBER_OF_OUTPUTS_PER_BIN_SEL] >> 4
                    bs.SEND_FLAGS = send_flags
                    bs.NUMBER_OF_FRAMES_PER_PACKET = frames_per_packet
                    bs.NUMBER_OF_BINS_PER_FRAME = cb2_input_bins
                    bs.NUMBER_OF_WORDS_PER_BIN = cb2_input_words_per_bin
                    bs.NUMBER_OF_DATA_FLAGS_WORDS_PER_BIN = cb2_input_data_flags_words_per_bin
                    bs.NUMBER_OF_FRAME_FLAGS_WORDS_PER_FRAME = cb2_input_frame_flags_words_per_frame
                    bs.FIRST_LANE = cb2_lanes[cb2_bin_sel][0]
                    bs.LAST_LANE = cb2_lanes[cb2_bin_sel][1]
                    bs.select_bins(cb2_bin_select_map[cb2_bin_sel])
        elif not cb2_bypass:
            raise RuntimeError("The FPGA firmware must have a CROSSBAR2 in the '%s' operational mode", mode)

        ###########################
        # Configure CROSSBAR 3
        ###########################
        if cb3:
            cb3.IGNORE_LANE = cb3_ignore_lane
            cb3.set_lane_map(cb3_lane_map)
            if cb3_bypass:  # if we bypass, just remap the stream ids from the previous crossbar
                stream_id = [stream_id[i] for i in cb3_lane_map]
            else:  # otherwise, the bin selector overrides
                stream_id = [((stream_type << 12) | (crate_number << 8) | (slot_number << 4) | lane)
                             for lane in range(cb3.NUMBER_OF_CROSSBAR_OUTPUTS)]
            for (cb3_bin_sel, bs) in enumerate(cb3):
                bs.BYPASS = bool(cb3_bypass)
                if not cb3_bypass:
                    bs.STREAM_ID = stream_id[cb3_bin_sel * cb3.NUMBER_OF_OUTPUTS_PER_BIN_SEL] >> 4
                    bs.SEND_FLAGS = send_flags
                    bs.NUMBER_OF_FRAMES_PER_PACKET = frames_per_packet
                    assert cb3_input_data_flags_words_per_bin == int(cb3_input_data_flags_words_per_bin), f'CB3 number of flag words is not an integer ({cb3_input_data_flags_words_per_bin})'
                    bs.NUMBER_OF_DATA_FLAGS_WORDS_PER_BIN = int(cb3_input_data_flags_words_per_bin)
                    bs.NUMBER_OF_FRAME_FLAGS_WORDS_PER_FRAME = cb3_input_frame_flags_words_per_frame
                    # print('CB3: FFWPF=%i' % bs.NUMBER_OF_FRAME_FLAGS_WORDS_PER_FRAME)
                    bs.FIRST_LANE = cb3_lanes[cb3_bin_sel][0]
                    bs.LAST_LANE = cb3_lanes[cb3_bin_sel][1]
                    # print('CB3: FFWPF=%i' % bs.NUMBER_OF_FRAME_FLAGS_WORDS_PER_FRAME)
                    bs.NUMBER_OF_BINS_PER_FRAME = cb3_input_bins
                    bs.NUMBER_OF_WORDS_PER_BIN = cb3_input_words_per_bin
                    bs.select_bins(cb3_bin_select_map[cb3_bin_sel])

                    # bs.SEND_FLAGS = 0  # JFC debug. Does not affect data.
            # print("cb3 input frame flags words=%i" %cb3_input_frame_flags_words_per_frame)
        elif not cb3_bypass:
            raise RuntimeError("The FPGA firmware must implement CROSSBAR3 in the '%s' operational mode", mode)

        def print_packet_size(
                crossbar_name,
                frames_per_packet,
                bins,
                data_words_per_bin,
                data_flags_words_per_bin,
                frame_flags_words_per_frame):
            header_words_per_packet = 4
            packet_flags_words_per_packet = 1
            if not send_flags:
                data_flags_words_per_bin = 0
                frame_flags_words_per_frame = 0
            payload_size = 4 * (
                header_words_per_packet +
                frames_per_packet * (
                    (data_words_per_bin + data_flags_words_per_bin) * bins +
                    frame_flags_words_per_frame) +
                packet_flags_words_per_packet)
            ethernet_packet_overhead_bytes = 42
            ethernet_packet_size = (ethernet_packet_overhead_bytes + payload_size + 7) // 8 * 8
            # eth_data_rate = 156.25e6 * 66 * 32/33
            # bp_data_rate = 156.25e6* 50 * 32/33
            packet_rate = 800e6 / 2048 / frames_per_packet
            ethernet_data_rate = (packet_rate * ethernet_packet_size) * 8
            self.logger.info(
                f'{self!r}: {crossbar_name}, Eth packet size {ethernet_packet_size} bytes, Eth data rate {ethernet_data_rate/1e9:0.1f} Gbit/s')
            self.logger.debug(
                f'{self!r}: {crossbar_name}:'
                f'   UDP payload size: {payload_size} bytes\n'
                f'   Ethernet packet size: {ethernet_packet_size} bytes\n'
                f'   Ethernet data rate: {ethernet_data_rate/1e9:0.1f} Gbit/s\n'
                f'   Packet geometry: {frames_per_packet} frames_per_packet\n'
                f'                    {bins} bins\n'
                f'                    {data_words_per_bin} data words/bin\n'
                f',                   {data_flags_words_per_bin} data flags_words/bin\n'
                f'                    {frame_flags_words_per_frame} frame_flags_words/frame)'
                )
            # self.logger.info('%r: %s config: frames_per_packet=%i, cb1_lanes=%s, cb1_bypass=%s, '
            #                   'cb1_combine=%s, cb1_bins=%i, cb1_words_per_bin=%i' % (
            #                   self, frames_per_packet, cb1_lanes, bool(cb1_bypass), bool(cb1_combine_data_flags),
            #                   cb1_bins, cb1_output_words_per_bin ))
            # self.logger.debug(
            #   '%r: CROSSBAR1 output packets payload = %i bytes (%i words)' % (
            #       self, cb1_payload_size, (cb1_payload_size+3)//4))

        print_packet_size('CROSSBAR3',
                          frames_per_packet=frames_per_packet,
                          bins=cb3_output_bins,
                          data_words_per_bin=cb3_output_words_per_bin,
                          data_flags_words_per_bin=cb3_output_data_flags_words_per_bin,
                          frame_flags_words_per_frame=cb3_output_frame_flags_words_per_frame)

        self.set_corr_reset(0)
        self.set_chan_reset(0)
        return stream_id
