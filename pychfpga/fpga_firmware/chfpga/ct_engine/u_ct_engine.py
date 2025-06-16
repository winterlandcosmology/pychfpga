"""
cge.py module
    Implements interface to the 100G Ethernet FPGA module
"""
import logging
import numpy as np

from ..mmi import MMI, BitField

from . import xxvglink


class CT1Regs(MMI):
    CONTROL = BitField.CONTROL
    STATUS = BitField.STATUS

    CT_LEVEL = BitField(STATUS, 0, 0, width=2, doc='Corner-turning level')
    CT1_RST_STATUS = BitField(STATUS, 0, 2, doc='Reset line state')
    RST_STATUS = BitField(STATUS, 0, 3, doc='Reset line state')
    ARST_STATUS = BitField(STATUS, 0, 4, doc='Reset line state')

    IN_FRAME_CTR = BitField(STATUS, 1, 0, width=8, doc='Counts frames coming into the CT engine')
    OUT_FRAME_CTR = BitField(STATUS, 2, 0, width=8, doc='Counts frames coming out of the CT engine')

class CT2Regs(MMI):
    CONTROL = BitField.CONTROL
    STATUS = BitField.STATUS

    ALIGN_MON_RESET = BitField(CONTROL, 0, 0, doc='Resets the ALIGN monitoring statistics')
    ALIGN_MON_SOURCE = BitField(CONTROL, 1, 4, width=4, doc='Select the information shown on the ALIGN_WORD')
    ALIGN_MON_LANE = BitField(CONTROL, 1, 0, width=4, doc='Select the lane from which info is shown on ALIGN_WORD')
    ALIGN_SOF_WINDOW = BitField(CONTROL, 2, 0, width=8, doc='Maximum allowable clock delays between the start of lane 0 and the other lanes before a lane is tagged as invalid')
    ALIGN_IGNORE_LANE = BitField(CONTROL, 4, 0, width=16, doc='A 1 indicates that a lane should not be waited for')
    LANE_MAP_BYTE0 = BitField(CONTROL, 5, 0, width=8,  doc='Remap the lanes before sending them to the other boards ')
    LANE_POSTMAP_BYTE0 = BitField(CONTROL, 7, 0, width=8,  doc='Remap the lanes after receiving them from the other boards ')
    # CMAC_SYS_RESET      = BitField(CONTROL, 0, 2, doc='When 1, The CMAC is reset.')
    # CORE_TX_RESET      = BitField(CONTROL, 0, 3, doc='When 1, The 100G Cor elogic is reset.')
    # TEST_PACKET_ENABLE  = BitField(CONTROL, 0, 7, doc='When 1, the test packet generator is enabled')
    # TEST_PACKET_WORDS  = BitField(CONTROL, 1, 0, width=8, doc='Number of 32-byte words in the UDP packets in addition to the 22 bytes payload header')
    # TEST_PACKET_PERIOD  = BitField(CONTROL, 3, 0, width=16, doc='Time between packets in  322MHz clocks periods')
    # CAPTURE_BYTE_NUMBER = BitField(CONTROL, 5, 0, width=16, doc='Index of byte to capture')
    # STATUS0            = BitField(STATUS, 0, 0, width=8, doc='various status bits')
    ALIGN_MON_WORD           = BitField(STATUS, 2, 0, width=16, doc='ALIGN monitoring word, selected by ALIGN_MON_SOURCE and ALIGN_MON_LANE')
    # OUT_FRAME_CTR           = BitField(STATUS, 2, 0, width=8, doc='Counts the number of framesgoing out to the CMAC.')
    # PACKET_LENGTH           = BitField(STATUS, 4, 0, width=16, doc='length of incoming packets')
    # CAPTURE_BYTE           = BitField(STATUS, 5, 0, width=8, doc='Captured byte')
    # DATA_FIFO_OVERFLOW  = BitField(STATUS, 2+2, 0, width=8, doc='Indicates if the data FIFO has overflows on the last 8 GPU links. Bit 0 is for lane 0.')
    # FRAME_FIFO_OVERFLOW = BitField(STATUS, 2+3, 0, width=8, doc='Indicates if the frame header FIFO has overflows on the last 8 GPU links. Bit 0 is for lane 0.')

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

        print(f'{self!r}: CT2 Lane pre-shuffle map is {premap} and post-shuffle map is {postmap}')

    def init(self):
        # Compute a lane map so input lane x goes to slot x
        rx_slot = tx_slot = (self.fpga.slot-1) % 4
        lane_premap = [self.fpga.mb.TX_TO_RX_LANE_MAP[(tx_slot, bp_tx_lane)][0] for bp_tx_lane in range(self.NUMBER_OF_CT2_INPUTS)]
        lane_postmap = [self.fpga.mb.RX_TO_TX_LANE_MAP[(rx_slot, bp_rx_lane)][0] for bp_rx_lane in range(self.NUMBER_OF_CT2_OUTPUTS)]

        self.set_lane_map(lane_premap, lane_postmap);

class CT3Regs(MMI):
    pass

class UCTEngine(MMI):
    """

    """

    CONTROL = BitField.CONTROL
    STATUS = BitField.STATUS

    BSB_ROUTING_ADDRESS_WIDTH = 2
    BSB_ROUTER_CT1_PORT = 0
    BSB_ROUTER_CT2_PORT = 1
    BSB_ROUTER_CT3_PORT = 2
    BSB_ROUTER_BPLINKS_PORT = 3

    def __init__(self, fpga_instance, base_address,  address_width, router_port, verbose=1):
        self.logger = logging.getLogger(__name__)
        self.verbose = verbose

        super().__init__(fpga_instance, base_address=base_address, address_width=address_width, router_port=router_port)

        submodule_address_width = address_width - self.BSB_ROUTING_ADDRESS_WIDTH
        self.CT_LEVEL = self.fpga.CT_LEVEL

        assert self.CT_LEVEL >=1, "CT_LEVEL cannot be < 1"

        # Instantiate Level-1 CT registers
        self.CT1 = CT1Regs(
            fpga_instance,
            base_address=self.base_address,
            address_width=submodule_address_width,
            router_port=self.BSB_ROUTER_CT1_PORT)

        if self.CT_LEVEL >=2:
            lane_groups = (('pcb', 1, 3),) # (name, # of bypass lanes, # of links)
            self.CT2 = CT2Regs(
                fpga_instance,
                base_address=self.base_address,
                address_width=submodule_address_width,
                router_port=self.BSB_ROUTER_CT2_PORT)
            self.BPLINKS = xxvglink.XXVGLinkArray(
                fpga_instance=fpga_instance,
                base_address = self.base_address,
                address_width = submodule_address_width,
                router_port =  self.BSB_ROUTER_BPLINKS_PORT,
                lane_groups=lane_groups,
                verbose=1)
        else:
            self.CT2 = None
            self.GTLINKS = 0


        if self.CT_LEVEL >=3:
            self.CT3 = CT3Regs(
                fpga_instance,
                base_address=self.base_address,
                address_width=submodule_address_width,
                router_port=self.BSB_ROUTER_CT3_PORT)
        else:
            self.CT3 = None



    def init(self):
        self.TEST_PACKET_ENABLE = 0

        if self.CT_LEVEL >=2:
            self.CT2.init()

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
        self.logger.warn('{self!r}: set_enable()  is not implemented on UltraCT. Command is ignored. ')

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

    # def status(self):
    #     """ Displays the status of the GPU GTX hardware"""
    #     # for gtx in self.GTX_COMMON:
    #     #     gtx.status()
