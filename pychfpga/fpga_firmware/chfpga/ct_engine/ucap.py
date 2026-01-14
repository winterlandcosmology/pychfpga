#!/usr/bin/python

"""
ucap.py module
Interface to the FPGA UltraRAM-based frame capture module.
"""

import logging
import asyncio
import socket
import time
import __main__

from ..mmi import MMI, BitField, CONTROL, STATUS
import numpy as np




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
        slot = self.fpga.slot
        self.USER_STREAM_ID = slot-1 if slot else 0

    DATA_SOURCE_TABLE = {
        'funcgen': 0,
        'scaler': 255} # 255: set data source for all 8 channels (11111111 = 255 for unsigned 8-bit bin)

    def set_data_capture(self, source, mode=0, channels=None, periods=[195312,195312], select=False):
        """ Selects the source of the data to be captured (FUNCGEN or SCALER). Optionally enables streaming the captured data instead of the correlator data.

        Parameters:

            source (str): Source to use. The source string represents where the tata is tapped in
                the channelizer pipeline:

                - 'funcgen': The captured data is taken at the output of the function generator module
                  (which can be configured to pass the ADC data or internally-generated waveforms)
                - 'scaler': The captured data is taken from the capture output port (not the main
                  output) of the scaler. The scaler can be configured to output various signals on this
                  port (scaled/unscaled raw FFT data, overflow flags etc.)

            mode (int): capture mode to use

            channels (list of int): list of channels do capture. Can be ``None`` for mode 0 since all channels are captured anyways.

            periods (list of int): Time between data captures for each of the sources, expressed in
                number of frames MINUS ONE. The number of consecutive frames stored by each capture
                is set by the `mode`.

            select (bool): If True, the output multiplexer of UCAP will be set to stream the
                captured data instead of the auxiliary (correlator) data.

        """
        # Set the data source (either 'adc' or 'scaler') in the control register
        if isinstance(source, str):
            if source in self.DATA_SOURCE_TABLE:
                source = self.DATA_SOURCE_TABLE[source]
            else:
                valid_sources = ', '.join(self.DATA_SOURCE_TABLE)
                raise ValueError(f"Unknown data capture source '{source}'. Valid sources are {valid_sources}")
        self.SOURCE_SEL = source

        if isinstance(channels, int):
            channels = [channels]

        if mode == 0:
            if channels is None:
                channels = list(range(8))
            if channels != list(range(8)):
                raise RuntimeError('UCAP mode 0 can only capture channels 0-7, in that order')
        elif mode == 1:
            if channels is None:
                channels = list(range(4))
            if len(channels) != 4:
                raise RuntimeError('UCAP mode 1 can only capture 4 channels')
            self.CH0 = channels[0]
            self.CH1 = channels[1]
            self.CH2 = channels[2]
            self.CH3 = channels[3]
        elif mode == 2 :
            if channels is None:
                channels = list(range(2))
            if len(channels) != 2:
                raise RuntimeError('UCAP mode 2 can only capture 2 channels')
            self.CH0 = channels[0]
            self.CH1 = channels[1]
        elif mode == 3:
            if channels is None:
                channels = list(range(1))
            if len(channels) != 1:
                raise RuntimeError('UCAP mode 3 can only capture 1 channel')
            self.CH0 = channels[0]
        else:
            raise RuntimeError(f'Invalid UCAP mode number {mode}')
        self.MODE = mode
        self.CAPTURE_PERIOD = periods[0]
        self.CAPTURE_PERIOD2 = periods[1]

        if select:
            self.select_output('ucap')

    OUTPUT_NAMES = {
        'cap':0,
        'ucap': 0,
        'aux': 1,
        'corr': 1}

    def select_output(self, out):
        """ Select whether UCAP streams capture or auxiliary (correlator) data

        Parameters:

            out (str or int): Output to stream: 0: Captured data; 1: auxiliary (correlator) data. If a string, the value will be looked up in OUTPUT_NAMES.

        """
        self.OUTPUT_SOURCE_SEL = out if isinstance(out, int) else self.OUTPUT_NAMES[out]


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


