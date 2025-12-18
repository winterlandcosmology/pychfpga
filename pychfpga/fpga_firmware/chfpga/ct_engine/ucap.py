#!/usr/bin/python

"""
ucap.py module
Interface to the FPGA UltraRAM-based frame capture module.
"""

import logging
import asyncio
import socket
import time

from ..mmi import MMI, BitField, CONTROL, STATUS
from ..raw_frame_receiver import RawFrameReceiver
import numpy as np
import __main__



class UCAP(MMI):
    """ Object that allows access to an UltraRAM-based frame capture module"""
    ADDRESS_WIDTH = 16

    MODE             = BitField(CONTROL, 0, 6, width=2, doc='Capture mode: 0: 8 channel, 1: 4 channel, 2: 2 channel, 3: 1 channel')
    CH0              = BitField(CONTROL, 0, 0, width=3, doc='Channel #0 in 1, 2, or 4-channel mode')
    CH1              = BitField(CONTROL, 0, 3, width=3, doc='Channel #1 in 2 or 4-channel mode')
    USER_RESET       = BitField(CONTROL, 1, 7, doc='User reset')
    OUTPUT_SOURCE_SEL = BitField(CONTROL, 1, 6, doc='Selects what data is streamed out: 0: UCAP, 1: CORR44')
    CH2              = BitField(CONTROL, 1, 0, width=3, doc='Channel #2 in 4-channel mode')
    CH3              = BitField(CONTROL, 1, 3, width=3, doc='Channel #3 in 4-channel mode')
    CAPTURE_PERIOD   = BitField(CONTROL, 4, 0, width=24, doc='Number of frames between captures for source 0')
    CAPTURE_PERIOD2  = BitField(CONTROL, 7, 0, width=24, doc='Number of frames between captures for source 1')
    SUB_PERIOD       = BitField(CONTROL, 8, 0, width=5, doc='Dynamic capture rate, 2**(N+1) frames')
    SOURCE_SEL       = BitField(CONTROL, 9, 0, width=8, doc='0 = source selector output (timestream), 255 = scaler output (spectrum)')
    USER_STREAM_ID   = BitField(CONTROL, 10, 0, width=8, doc='Bits 11:4 of the Stream ID found in the raw packet header')

    FIFO_OVERFLOW    = BitField(STATUS, 0, 0, doc='1 when dat FIFO has overflowed. Sticky flag.')
    OVERRUN          = BitField(STATUS, 0, 1, doc='1 when data transmission request was performed before the previous transmission was completed. Sticky flag.')
    RST_MON          = BitField(STATUS, 0, 2, doc='debug')
    USER_RST_MON     = BitField(STATUS, 0, 3, doc='debug')
    PERIOD_CTR       = BitField(STATUS, 1, 0, width=8, doc='debug')
    SOURCE_SEL_IN    = BitField(STATUS, 2, 0, width=8, doc='debug')

    def __init__(self, *, router, router_port, verbose=0):
        self.verbose = verbose
        self.logger = logging.getLogger(__name__)
        super().__init__(router=router, router_port=router_port)
        self.sock = None

    def init(self):
        """ Initializes UCAP module"""
        self.USER_STREAM_ID = self.fpga.slot or 0

    DATA_SOURCE_TABLE = {
        'adc': 0,
        'scaler': 255} # 255: set data source for all 8 channels (11111111 = 255 for unsigned 8-bit bin)

    def set_data_source(self, source):
        # Set the data source (either 'adc' or 'scaler') in the control register
        if isinstance(source, str):
            if source in self.DATA_SOURCE_TABLE:
                source = self.DATA_SOURCE_TABLE[source]
            else:
                ValueError(f'Unknown data capture source \'{source}\'. Valid sources are {self.DATA_SOURCE_TABLE.keys()}')
        self.SOURCE_SEL = source

    def get_data(self, flush_timeout=0.01):

        self.sock = self.fpga.get_data_socket()
        # s = getattr(__main__,"ucap_sock", None)
        # if s:
        #     self.sock = s
        # if self.sock is None:
        #     self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM);
        #     self.sock.bind(("0.0.0.0",41000))
        #     __main__.ucap_sock = self.sock
        # Flush the UDP  buffers by reading and discarding data until we timeout
        self.sock.settimeout(flush_timeout)
        b = np.zeros((128, 8192+5), '>i2')
        print('Flushing UDP buffers')
        while True:
            try:
                self.sock.recv_into(b[0])
            except socket.timeout:
                break
        self.sock.settimeout(2)
        print('Capturing data')
        for bb in b:
            self.sock.recv_into(bb)
        print(f'got stream_IDs: {b[:, 1] >> 8}')
        for bb in b:
            print(bb[:5].tobytes().hex(':'))
        # sid =
        # sid_ok = all(b[:,1]>>8 == np.arange(b.shape[0], dtype=np.uint8) & 63)
        # if not sid_ok:
        #     raise RuntimeError('Missing packets')

        return b[:64, 5:]

