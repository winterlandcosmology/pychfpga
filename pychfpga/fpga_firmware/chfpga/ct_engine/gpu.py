#!/usr/bin/python

"""
gpu.py module
    Implements interface to the GPU links

History:
    2013-10-29 : JFC : Created
"""
import logging
from ..mmi import BitField
from . import xglink


class GPU(xglink.XGLinkCore):
    """ Instantiates a container for all the GPU link ressources

    This class provides access to the memory-mapped registers and
    the high-level methods needed to operate the XGE_ARRAY.VHD module,
    which implements a array on transmit-only 10G Ethernet links.

    It implements a XGLinkCore (ensemble of QPLLs and GTXes and
    date encoding/synchonization) and adds 10G Ethernet packet framing
    (SOF, EOF) and checksum (CRC32). It provides an additional set of
    registers to the XGLinkCore.

    XGLinkArray is used to implement Tx-only communication links from the
    IceBoard's QSFP connector to the GPU farm.

    """

    CONTROL = BitField.CONTROL
    STATUS = BitField.STATUS

    # 10GE Link control and status registers
    RESET               = BitField(CONTROL, 4+0, 0, doc='Resets the MAC and the GTX core.')
    TEST_ENABLE         = BitField(CONTROL, 4+0, 1, doc='When 1, enables trsnamission of test packets over the link.')
    DEST_ADDR_MODE      = BitField(CONTROL, 4+0, 3, doc='When 1, the destination address is set by TEST_PACKET_LENGTH and the MSB of TEST_PACKET_PERIOD')
    OVERFLOW_RESET      = BitField(CONTROL, 4+0, 2, doc='When 1, The overflow bits are reset.')
    TEST_PACKET_LENGTH  = BitField(CONTROL, 4+2, 0, width=16, doc='test packet length in units of 32bit words')
    TEST_PACKET_PERIOD  = BitField(CONTROL, 4+4, 0, width=16, doc='period in 244.14MHz clocks')
    WORD_CTR            = BitField(STATUS, 2+0, 0, width=8, doc='Last 8 bits of the counter used to produce the test test pattern.')
    FRAME_CTR           = BitField(STATUS, 2+1, 0, width=8, doc='Counts the number of frames coming in on lane 0.')
    DATA_FIFO_OVERFLOW  = BitField(STATUS, 2+2, 0, width=8, doc='Indicates if the data FIFO has overflows on the last 8 GPU links. Bit 0 is for lane 0.')
    FRAME_FIFO_OVERFLOW = BitField(STATUS, 2+3, 0, width=8, doc='Indicates if the frame header FIFO has overflows on the last 8 GPU links. Bit 0 is for lane 0.')

    def __init__(self, *, router, router_port, verbose=0):
        self.verbose = verbose
        super().__init__(router=router, router_port=router_port)

    def set_enable(self, state):
        #  self.LINK_ENABLE = state
        pass

    def reset(self):
        """ Resets the UDP/MAC stack, the SGMII interface and the GTX """
        self.RESET = 1
        self.RESET = 0

    def get_lane_numbers(self):
        """ Returns a list of logical lane numbers for the GPU links.

        There is no distinction between the two QSFP connectors.

        Returns:

            List of integers.
        """
        return list(range(len(self.gtx)))

    def get_lane_ids(self):
        """ Return a list of all lane IDs in the form of [(crate_number, slot_number, lane_number), ...].

        Returns:
            List of all lane IDs in the form of [(crate_number, slot_number, lane_number), ...]
        """

        return [self.fpga.get_id(lane) for lane in self.get_lane_numbers()]

    def status(self):
        """ Displays the status of the GPU GTX hardware"""
        # for gtx in self.GTX_COMMON:
        #     gtx.status()
