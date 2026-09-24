"""
cge.py module
    Implements interface to the 100G Ethernet FPGA module
"""
import logging
import time

import numpy as np

from ..mmi import MMI, MMIRouter, BitField, CONTROL, STATUS

from . import xxvglink
from .uct_array_methods import UCTArrayMethods


class CT1Regs(MMI):

    CT_LEVEL = BitField(STATUS, 0, 0, width=2, doc='Corner-turning level')
    CT1_RST_STATUS = BitField(STATUS, 0, 2, doc='Reset line state')
    RST_STATUS = BitField(STATUS, 0, 3, doc='Reset line state')
    ARST_STATUS = BitField(STATUS, 0, 4, doc='Reset line state')

    IN_FRAME_CTR = BitField(STATUS, 1, 0, width=8, doc='Counts frames coming into the CT engine')
    OUT_FRAME_CTR = BitField(STATUS, 2, 0, width=8, doc='Counts frames coming out of the CT engine')

    def __init__(self, *, router, router_port, config):
        super().__init__(router=router, router_port=router_port)
        self.config = config

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

    def __init__(self, *, router, router_port, config):
        super().__init__(router=router, router_port=router_port)
        self.config = config

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

    def __init__(self, *, router, router_port, config):
        super().__init__(router=router, router_port=router_port)
        self.config = config

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

    def __init__(self,*, router, router_port, config, verbose=1):
        self.logger = logging.getLogger(__name__)
        self.verbose = verbose

        super().__init__(router=router, router_port=router_port)
        self.config = config
        self.fpga.register_array_methods(UCTArrayMethods, key='CT')

        self.CT_LEVEL = self.fpga.CT_LEVEL

        assert self.CT_LEVEL >=1, "CT_LEVEL cannot be < 1"

        self.CT1 = self.CT2 = self.CT3 = self.GTLINKS = None

        # Instantiate Level-1 CT registers
        self.CT1 = CT1Regs(router=self, router_port='CT1', config=self.config.add("ct1"))

        if self.CT_LEVEL >=2:
            lane_groups = (('pcb', 1, 3),) # (name, # of bypass lanes, # of links)
            self.CT2 = CT2Regs(router=self, router_port='CT2', config=self.config.add("ct2"))
            self.BPLINKS = xxvglink.XXVGLinkArray(
                router = self,
                router_port='BPLINKS',
                lane_groups=lane_groups,
                config=self.config.add("bplinks"),
                verbose=1)

        if self.CT_LEVEL >=3:
            self.CT3 = CT3Regs(router=self, router_port='CT3', config=self.config.add("ct3"))



    def init(self):
        self.TEST_PACKET_ENABLE = 0
        ct_level = int(self.CT_LEVEL)
        if ct_level >=2:
            self.CT2.init()

        if ct_level >=3:
            self.CT3.init()
        self.config.CT_LEVEL = ct_level

    def update_config(self):
        """Refresh CT config subtree; returns ``self.config``."""
        return self.config

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



    @classmethod
    async def init_corner_turn_array(
            cls,
            fpga_array,
            mode,
            frames_per_packet=1,
            sync=True,
            **kwargs):
        """ Setup corner-turning across  the array.

        """
        non_none_kwargs = {k: v for k, v in kwargs.items() if v is not None}
        if non_none_kwargs:
            fpga_array.logger.debug(f'{fpga_array!r}: init_corner_turn_array(): the following arguments will be ignored: {",".join(non_none_kwargs.keys())}')

        (CT_LEVEL,) = {(mb.CT.CT_LEVEL if mb.CT else None) for mb in fpga_array.ib}  # get a set of all different CT engine types

        fpga_array.logger.info(f'{fpga_array!r}: Configuring array-wide corner-turning')

        # Initialize the bin map for every board
        # bin_map = {mb.get_id(): {'cb1': None, 'cb2': None, 'cb3': None}
        #            for mb in fpga_array.ib}


        fpga_array.corner_turn_stream_ids = None # UCT stream IDs are handled by UPACK
        fpga_array.corner_turn_frequency_bins = None # same

        for mb in fpga_array.ib:
            mb.set_corr_reset(0)  # Put the corner_turn engine out of reset
            stream_ids = mb.CT.init_crossbars(mode=mode, frames_per_packet=frames_per_packet)



        # Reset UPACK and CGE transmitters
        # todo. CGE and UPACK init() settings are currently ok.

        fpga_array.logger.info(f'{fpga_array!r}: Corner-turning initialization completed.')

        if sync:
            await fpga_array.sync_async()

    def init_crossbars(
            self,
            mode=None,
            frames_per_packet=1):

        if frames_per_packet not in (1, None):
            raise RuntimeError(f'UCT engine only supports 1 frame per packet. {frames_per_packet} frames/packet are not supported.')

        # For now, just do some basic checks. There isn't much to do for UCT.
        if mode in ('corr8', 'corr4'):
            if  self.CT_LEVEL != 1:
                raise RuntimeError(f'CT_LEVEL must be 1 for {mode} mode')
            # Corner-turn is internal and hard-wired. Nothing to configure.
        elif mode in ('shuffle8',):
            if  self.CT_LEVEL != 1:
                raise RuntimeError(f'CT_LEVEL must be 1 for {mode} mode')
            # Corner-turn is internal and hard-wired. Nothing to configure.
            # Might want to include configuring UPACK here in the future. That's where the actual corner-turn is done.
        elif mode in ('corr32',):
            if  self.CT_LEVEL != 2:
                raise RuntimeError(f'CT_LEVEL must be 2 for {mode} mode')
            # Corr32 uses fixed bin routing through backplane PCB links (4-way corner-turn)
        elif mode in ('corr64',):
            if  self.CT_LEVEL != 3:
                raise RuntimeError(f'CT_LEVEL must be 3 for {mode} mode')
            # Corr64 uses fixed bin routing through backplane PCB links (4-way corner-turn) and SFP links (2-way corner-turn)
        else:
            raise ValueError(f'Unknown UCT operational mode: {mode}')



        return None

    # def status(self):
    #     """ Displays the status of the GPU GTX hardware"""
    #     # for gtx in self.GTX_COMMON:
    #     #     gtx.status()
