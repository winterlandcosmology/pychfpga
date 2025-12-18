"""
cge.py module
    Implements interface to the 100G Ethernet FPGA module
"""
import logging
import numpy as np

from ..mmi import MMI, MMIRouter, BitField, CONTROL, STATUS

from . import xxvglink


class CT1Regs(MMI):

    CT_LEVEL = BitField(STATUS, 0, 0, width=2, doc='Corner-turning level')
    CT1_RST_STATUS = BitField(STATUS, 0, 2, doc='Reset line state')
    RST_STATUS = BitField(STATUS, 0, 3, doc='Reset line state')
    ARST_STATUS = BitField(STATUS, 0, 4, doc='Reset line state')

    IN_FRAME_CTR = BitField(STATUS, 1, 0, width=8, doc='Counts frames coming into the CT engine')
    OUT_FRAME_CTR = BitField(STATUS, 2, 0, width=8, doc='Counts frames coming out of the CT engine')

class CT2Regs(MMI):

    ALIGN_MON_RESET = BitField(CONTROL, 0, 0, doc='Resets the ALIGN monitoring statistics')
    ALIGN_MON_SOURCE = BitField(CONTROL, 1, 4, width=4, doc='Select the information shown on the ALIGN_WORD')
    ALIGN_MON_LANE = BitField(CONTROL, 1, 0, width=4, doc='Select the lane from which info is shown on ALIGN_WORD')
    ALIGN_SOF_WINDOW = BitField(CONTROL, 2, 0, width=8, doc='Maximum allowable clock delays between the start of lane 0 and the other lanes before a lane is tagged as invalid')
    LANE_MAP_BYTE0 = BitField(CONTROL, 5, 0, width=8,  doc='Remap the lanes before sending them to the other boards ')
    LANE_POSTMAP_BYTE0 = BitField(CONTROL, 7, 0, width=8,  doc='Remap the lanes after receiving them from the other boards ')
    ALIGN_MON_WORD           = BitField(STATUS, 2, 0, width=16, doc='ALIGN monitoring word, selected by ALIGN_MON_SOURCE and ALIGN_MON_LANE')

    NUMBER_OF_CT2_INPUTS = 4
    NUMBER_OF_CT2_OUTPUTS = 4

    def set_lane_map(self, premap, postmap):
        """ Sets the lane remapping.

        Lanes are numbered from 0 to 3. lane_map[x] indicates which CT1 lane is routed to CT2 backplane lane.
        ``lane_map[0]`` should always be 0
        """

        # if len(lane_map) != self.NUMBER_OF_CROSSBAR_INPUTS:
        #     raise TypeError('Lane map must be a list of %i values' % self.NUMBER_OF_CROSSBAR_INPUTS)

        lane_map_bytes = np.zeros(self.NUMBER_OF_CT2_INPUTS // 2, dtype=np.uint8)
        for i, lane in enumerate(premap):
            lane_map_bytes[i//2] |= (lane & 0x0F) << (((i+1) % 2) * 4)
        self.write(self.get_addr('LANE_MAP_BYTE0'), lane_map_bytes)

        lane_map_bytes = np.zeros(self.NUMBER_OF_CT2_INPUTS // 2, dtype=np.uint8)
        for i, lane in enumerate(postmap):
            lane_map_bytes[i//2] |= (lane & 0x0F) << (((i+1) % 2) * 4)
        self.write(self.get_addr('LANE_POSTMAP_BYTE0'), lane_map_bytes)

        self.logger.debug(f'{self!r}: CT2 Lane pre-shuffle map is {premap} and post-shuffle map is {postmap}')

    def init(self):
        # Compute a lane map so input lane x goes to slot x
        rx_slot = tx_slot = (self.fpga.slot - 1) % 4
        sub_bp = (self.fpga.slot - 1) // 4
        # Compute pre-shuffle lane map to ensure that bins go to the correct slots
        lane_premap = [self.fpga.mb.TX_TO_RX_LANE_MAP[(tx_slot, bp_tx_lane)][0] for bp_tx_lane in range(self.NUMBER_OF_CT2_INPUTS)]
        # Compute post-shuffle lane map to ensure that channels are stacked together in the right order
        lane_postmap = [self.fpga.mb.SLOT_TO_LANE_MAP[(ss, rx_slot)][1] for ss in range(self.NUMBER_OF_CT2_OUTPUTS)]

        self.set_lane_map(lane_premap, lane_postmap);

class CT3Regs(MMI):

    ALIGN_MON_RESET   = BitField(CONTROL, 0, 7, doc='Resets the ALIGN monitoring statistics')
    SLOT_GROUP        = BitField(CONTROL, 0, 6, doc='The slot group (i.e. sub-backplane) to which the board belongs. Is used by CT_LEVEL=3 to direct bins to the right destination ')
    ZERO_DATA_ON_LANE_ERROR = BitField(CONTROL, 0, 5, doc='When 1, data is zeroed on invalid lanes')
    ALIGN_MON_SOURCE  = BitField(CONTROL, 1, 4, width=4, doc='Select the information shown on the ALIGN_WORD')
    ALIGN_MON_LANE    = BitField(CONTROL, 1, 0, width=4, doc='Select the lane from which info is shown on ALIGN_WORD')
    RST_STATUS        = BitField(STATUS, 0, 0, doc='Reset line state')
    ALIGN_MON_WORD    = BitField(STATUS, 2, 0, width=16, doc='ALIGN monitoring word, selected by ALIGN_MON_SOURCE and ALIGN_MON_LANE')

    def init(self):
        self.SLOT_GROUP =  (self.fpga.slot - 1) // 4
        self.ZERO_DATA_ON_LANE_ERROR = 1

class UCTEngine(MMIRouter):
    """

    """

    ROUTER_PORT_NUMBER_WIDTH = 2
    ROUTER_PORT_MAP = {
        'CT1': 0,
        'CT2': 1,
        'CT3': 2,
        'BPLINKS': 3
        }

    def __init__(self,*, router, router_port, verbose=1):
        self.logger = logging.getLogger(__name__)
        self.verbose = verbose

        super().__init__(router=router, router_port=router_port)

        self.CT_LEVEL = self.fpga.CT_LEVEL

        assert self.CT_LEVEL >=1, "CT_LEVEL cannot be < 1"

        self.CT1 = self.CT2 = self.CT3 = self.GTLINKS = None

        # Instantiate Level-1 CT registers
        self.CT1 = CT1Regs(router=self, router_port='CT1')

        if self.CT_LEVEL >=2:
            lane_groups = (('pcb', 1, 3),) # (name, # of bypass lanes, # of links)
            self.CT2 = CT2Regs(router=self, router_port='CT2')
            self.BPLINKS = xxvglink.XXVGLinkArray(
                router = self,
                router_port='BPLINKS',
                lane_groups=lane_groups,
                verbose=1)

        if self.CT_LEVEL >=3:
            self.CT3 = CT3Regs(router=self, router_port='CT3')



    def init(self):
        self.TEST_PACKET_ENABLE = 0

        if self.CT_LEVEL >=2:
            self.CT2.init()

        if self.CT_LEVEL >=3:
            self.CT3.init()

    def capture_bytes(self, N=64, fmt='hex'):
        """ Captures N bytes
        """
        b = bytearray(N)
        for i in range(N):
            self.CAPTURE_BYTE_NUMBER = i
            b[i] = self.CAPTURE_BYTE

        if fmt == 'hex':
            print('\n'.join(f'{b[i: i+16].hex()} {b[i+16: i+32].hex()}' for i in range(0,len(b),32)))
            return None

        return b

    def set_enable(self, enable):
        """
        Enable link.
        """
        self.logger.warn(f'{self!r}: set_enable()  is not implemented on UltraCT. Command is ignored. ')

    # def reset(self):
    #     """ Resets the UDP/MAC stack, the SGMII interface and the GTX """
    #     self.RESET = 1
    #     self.RESET = 0

    def get_lane_numbers(self):
        """ Returns a list of logical lane numbers for the GPU links.

        There is no distinction between the two QSFP connectors.

        Returns:

            List of integers.
        """
        return list(range(1))

    def get_lane_ids(self):
        """ Return a list of all lane IDs in the form of [(crate_number, slot_number, lane_number), ...].

        Returns:
            List of all lane IDs in the form of [(crate_number, slot_number, lane_number), ...]
        """

        return [self.fpga.get_id(lane) for lane in self.get_lane_numbers()]

    def set_data_width(self, width):
        if width != 4:
            raise RuntimeError(f'CT engine only supports a data width of 4+4 bits. {width}+{width} bits is not supported')
    def get_data_width(self):
        return 4

    def set_frames_per_packet(self, frames):
        if frames != 1:
            self.logger.warn(f'{self!r}: CT engine only outputs 1 frame per packet. {frames} frames/packet are not supported.')

    def get_frames_per_packet(self):
        return 1


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

        if mode == 'corr8':
            if  self.CT_LEVEL != 1:
                raise RuntimeError(f'CT_LEVEL must be 1 for {mode} mode')
        return np.arange(8)

    # def status(self):
    #     """ Displays the status of the GPU GTX hardware"""
    #     # for gtx in self.GTX_COMMON:
    #     #     gtx.status()
