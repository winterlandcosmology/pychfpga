#!/usr/bin/python
# Disable pylint Line too long (=C0301)
# pylint: disable=C0301

"""
GPU.py module
    Implements interface to the GPU links

History:
    2013-10-29 : JFC : Created
"""
import logging
from Module import BitField
import xglink


class GPU_base(xglink.XGLink):
    """ Instantiates a container for all the GPU link ressources """

    CONTROL = BitField.CONTROL
    STATUS = BitField.STATUS

    # 10GE Link control and status registers
    RESET               = BitField(CONTROL, 4+0, 0, doc='Resets the MAC and the GTX core.')
    TEST_ENABLE         = BitField(CONTROL, 4+0, 1, doc='When 1, enables trsnamission of test packets over the link.')
    TEST_PACKET_LENGTH  = BitField(CONTROL, 4+2, 0, width=16, doc='test packet length in units of 32bit words')
    TEST_PACKET_PERIOD  = BitField(CONTROL, 4+4, 0, width=16, doc='period in 244.14MHz clocks')
    WORD_CTR            = BitField(STATUS, 2+0, 0, width=8, doc='Last 8 bits of the counter used to produce the test test pattern.')
    FRAME_CTR           = BitField(STATUS, 2+1, 0, width=8, doc='Counts the number of frames coming in on lane 0.')
    DATA_FIFO_OVERFLOW  = BitField(STATUS, 2+2, 0, width=8, doc='Indicates if the data FIFO has overflows on the last 8 GPU links. Bit 0 is for lane 0.')
    FRAME_FIFO_OVERFLOW = BitField(STATUS, 2+3, 0, width=8, doc='Indicates if the frame header FIFO has overflows on the last 8 GPU links. Bit 0 is for lane 0.')

    def __init__(self, fpga_instance, base_address, address_increment, verbose=1):
        self.logger = logging.getLogger(__name__)
        self.verbose = verbose
        super(GPU_base, self).__init__(fpga_instance, base_address, address_increment)

    def set_enable(self, state):
        #  self.LINK_ENABLE = state
        pass

    def status(self):
        """ Displays the status of the GPU GTX hardware"""
        # for gtx in self.GTX_COMMON:
        #     gtx.status()
