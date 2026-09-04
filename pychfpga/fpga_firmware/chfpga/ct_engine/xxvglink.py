"""
xxvglink.py module
    Implements the interface to the generic 25 Gbps packet transmitter/receiver array.

History:
    2025-01-27: JFC : Created
"""
import logging
import time
import collections
import numpy as np
import matplotlib.pyplot as plt
import asyncio

from ..mmi import MMI, MMIRouter, BitField, CONTROL, STATUS, DRP
from wtl.metrics import Metrics

class QPLL(MMI):
    """ Implements interface to one of the COMMON """

    ADDRESS_WIDTH = 10

    QPLL_LOCK                = BitField(STATUS, 0, 0, doc='Indicates if the QPLL is locked')
    QPLL_PD                  = BitField(CONTROL, 0,0, doc="power down qpll.  needs 500ns after reset.")

    QPLL_INIT_CFG            = BitField(DRP, 0x0030, 0, width=16, doc="0-65535")
    QPLL_LPF                 = BitField(DRP, 0x0031, 11, width=4, doc="0-15")
    QPLL_INIT_CFG            = BitField(DRP, 0x0031, 0, width=8,   doc="0-255")
    QPLL_CFG                 = BitField(DRP, 0x0032, 0, width=16, doc="0-65535")
    QPLL_REFCLK_DIV          = BitField(DRP, 0x0033, 11, width=5, doc="1: 16, 2: 0, 3: 1, 4: 2, 5: 3, 6: 5, 8: 6, 10: 7, 12: 13, 16: 14, 20: 15")
    QPLL_CFG                 = BitField(DRP, 0x0033, 0, width=11, doc="0-2047")
    QPLL_LOCK_CFG            = BitField(DRP, 0x0034, 0, width=16, doc="0-65535")
    QPLL_COARSE_FREQ_OVRD    = BitField(DRP, 0x0035, 10, width=6, doc="0-63")
    QPLL_CP                  = BitField(DRP, 0x0035, 0, width=10,  doc="0-1023")
    QPLL_DMONITOR_SEL        = BitField(DRP, 0x0036, 15,             doc="0-1")
    QPLL_FBDIV_MONITOR_EN    = BitField(DRP, 0x0036, 14,             doc="0-1")
    QPLL_CP_MONITOR_EN       = BitField(DRP, 0x0036, 13,             doc="0-1")
    QPLL_COARSE_FREQ_OVRD_EN = BitField(DRP, 0x0036, 11,             doc="0-1")
    QPLL_FBDIV               = BitField(DRP, 0x0036, 0, width=10,  doc="0-1023")
    QPLL_FBDIV_RATIO         = BitField(DRP, 0x0037, 6,              doc="0-1")
    QPLL_CLKOUT_CFG          = BitField(DRP, 0x0037, 2, width=4,   doc="0-15")
    BIAS_CFG0                = BitField(DRP, 0x003E, 0, width=16, doc="BIAS_CFG[15:0 ] 0-65535")
    BIAS_CFG1                = BitField(DRP, 0x003F, 0, width=16, doc="BIAS_CFG[31:16] 0-65535")
    BIAS_CFG2                = BitField(DRP, 0x0040, 0, width=16, doc="BIAS_CFG[47:32] 0-65535")
    BIAS_CFG3                = BitField(DRP, 0x0041, 0, width=16, doc="BIAS_CFG[63:48] 0-65535")
    COMMON_CFG0              = BitField(DRP, 0x0043, 0, width=16, doc="COMMON_CFG[15:0 ] 0-65535")
    COMMON_CFG1              = BitField(DRP, 0x0044, 0, width=16, doc="COMMON_CFG[31:16] 0-65535")

    def __init__(self, *, router, router_port, instance_number, config):
        self.logger = logging.getLogger(__name__)
        super().__init__(router=router, router_port=router_port, instance_number=instance_number)
        self.config = config

    def select(self):
        """No-op: this block has a dedicated DRP port (API parity with :class:`cge.CGECommon`)."""
        return self

    def unselect(self):
        """No-op."""
        return self

    def init(self):
        """ Initializes the antenna modules"""
        # self.logger.info('Initializing BP Shuffle QPLL #%i' % self.instance_number)

    def status(self):
        """ Displays the status of the antenna modules"""
        return self.read_all_fields()


class GTY(MMI):
    """ Implements interface to a GTY_CHANNEL block """

    ADDRESS_WIDTH = 10

    # USER_RESET     = BitField(CONTROL, 0, 7, doc='')
    # USER_GTTXRESET = BitField(CONTROL, 0, 6, doc='')
    SCRAMBLE_EN     = BitField(CONTROL, 0, 7, doc='')
    RXGEARBOXFORCESLIP = BitField(CONTROL, 0, 6, doc='')
    TXINHIBIT      = BitField(CONTROL, 0, 5, doc='Debug')
    TXPOSTCURSOR   = BitField(CONTROL, 0, 0, width=5, doc='Debug')

    TXDIFFCTRL     = BitField(CONTROL, 1, 4, width=4, doc='Debug')
    UNSCRAMBLER_RESET = BitField(CONTROL, 1, 3, doc='Debug')
    LOOPBACK       = BitField(CONTROL, 1, 0, width=3, doc='Debug') #-- '000' = normal operation

    SOURCE_SEL     = BitField(CONTROL, 2, 7, doc='')
    TXPRECURSOR    = BitField(CONTROL, 2, 2, width=5, doc='Debug')
    TXHEADER       = BitField(CONTROL, 2, 0, width=2, doc='Debug')

    TX_DATA_LSB    = BitField(CONTROL, 3, 0, width=8, doc='Debug')

    RXDFELPMRESET    = BitField(CONTROL, 4, 7, doc='') #gt_control_bytes(i)(10)(5);
    # USER_GTRXRESET   = BitField(CONTROL, 4, 6, doc='')
    RXLPMEN          = BitField(CONTROL, 4, 5, doc='') #gt_control_bytes(i)(10)(6);
    RXMONITORSEL     = BitField(CONTROL, 4, 3, width=2, doc='') #gt_control_bytes(i)(10)(4 downto 3);
    CAPTURE_ENABLE   = BitField(CONTROL, 4, 2, doc='')
    BLOCK_LOCK_RESET = BitField(CONTROL, 4, 1, doc='')
    RXDFEAGCHOLD = BitField(CONTROL, 4, 0, doc='')

    # SCRAMBLER_RESET    = BitField(CONTROL, 0, 5, doc='')
    # DESCRAMBLER_RESET  = BitField(CONTROL, 0, 4, doc='')
    # SCRAMBLER_ENABLE   = BitField(CONTROL, 0, 3, doc='')
    # DESCRAMBLER_ENABLE = BitField(CONTROL, 0, 2, doc='')

    RXPRBSCNTRESET = BitField(CONTROL, 5, 7, doc='Debug')
    TXPRBSFORCEERR = BitField(CONTROL, 5, 6, doc='Debug')
    TXPRBSSEL      = BitField(CONTROL, 5, 3, width=3, doc='Debug')
    RXPRBSSEL      = BitField(CONTROL, 5, 0, width=3, doc='Debug')


    # RXCDRHOLD      = BitField(CONTROL, 0, 3, doc='Debug')
    # TXPOLARITY     = BitField(CONTROL, 1, 1, doc='Debug')
    # RXPOLARITY     = BitField(CONTROL, 1, 0, doc='Debug')

    # BLOCK_LOCK_RESET  = BitField(CONTROL, 8, 5, doc='')
    # SCRAMBLER_RESET   = BitField(CONTROL, 8, 6, doc='')
    # UNSCRAMBLER_RESET = BitField(CONTROL, 8, 7, doc='')

    # SCRAMBLE_EN    = BitField(CONTROL, 9, 0, doc='')
    # DESCRAMBLE_EN  = BitField(CONTROL, 9, 1, doc='')
    # CAPTURE_EN     = BitField(CONTROL, 9, 2, doc='')
    # STOP_BITSLIP   = BitField(CONTROL, 9, 3, doc='')
    # DRP_BANK       = BitField(CONTROL, 9, 7, doc='')

    # RXLPMHOLD     = BitField(CONTROL, 10, 1, width=2, doc='') #gt_control_bytes(i)(10)(2 downto 1);
    # RXDFEHOLD     = BitField(CONTROL, 11, 0, width=9, doc='') # Bit 0: AGC, 1: LF, 2-5: TAP2-5, 6: UT, 7: VP, 8: OS
    # RXLPMOVRD     = BitField(CONTROL, 12, 1, width=2, doc='') #gt_control_bytes(i)(10)(2 downto 1);
    # RXDFEOVRD     = BitField(CONTROL, 13, 0, width=9, doc='') # Bit 0: AGC, 1: LF, 2-5: TAP2-5, 6: UT, 7: VP, 8: OS

    TX_RESETDONE  = BitField(STATUS, 0, 7, doc='Debug')
    RX_RESETDONE  = BitField(STATUS, 0, 6, doc='Debug')  # From the GTY
    TXBUFSTATUS   = BitField(STATUS, 0, 4, width=2)
    TXRESETDONE   = BitField(STATUS, 0, 3, doc='Debug')
    RXRESETDONE   = BitField(STATUS, 0, 2, doc='Debug')  # From the RX_FSM
    BLOCK_LOCK    = BitField(STATUS, 0, 1, doc='Debug')
    RX_PRESENT    = BitField(STATUS, 0, 0, doc='Indicates if the RX logic is implemented')

    ERR_CTR       = BitField(STATUS, 4, 0, width=32)

    RXHEADER      = BitField(STATUS, 5, 6, width=2, doc='Debug')
    RXGEARBOXSLIP = BitField(STATUS, 5, 5, doc='Debug')
    RXPRBSLOCKED  = BitField(STATUS, 5, 3, doc='GTY Output to indicate that the RX PRBS checker has been error free. Is reset by RXPRBSCNTRESET.')
    RXBUFSTATUS   = BitField(STATUS, 5, 0, width=3)

    RXMONITOR     = BitField(STATUS, 6, 0, width=8, doc='Reserved port according to the GTY manuals. Not clear what this does.')
    RXDATA        = BitField(STATUS, 10, 0, width=32)

    # ERR_CTR       = BitField(STATUS, 10, 0, width=32)
    # RXHEADER      = BitField(STATUS, 1, 6, width=2, doc='Debug')
    # RXBUFSTATUS   = BitField(STATUS, 1, 0, width=3)
    # RXMONITOR     = BitField(STATUS, 2, 0, width=7, doc='Debug')
    # RXDATA        = BitField(STATUS, 6, 0, width=32)


    DMONITOROUT   = BitField(STATUS, 11, 0, width=8, doc='Debug')

    # RXPRBSERR     = BitField(STATUS, 5, 0, doc='Debug')
    # TXUSERRDY     = BitField(STATUS, 6, 0, doc='Debug')
    # RX_CDRLOCKED  = BitField(STATUS, 6, 3, doc='Debug')
    # GTTXRESET     = BitField(STATUS, 6, 4, doc='Debug')
    # RXUSERRDY     = BitField(STATUS, 6, 5, doc='Debug')

    # RXGEARBOXSLIP = BitField(STATUS, 7, 4, doc='Debug')
    # RXHEADERVALID = BitField(STATUS, 7, 5, doc='Debug')
    # GTRXRESET     = BitField(STATUS, 7, 6, doc='Debug')


    RX_PRBS_ERR_CNT_LO = BitField(DRP, 0x25E, 0, width=16, doc='RX_PRBS_ERR_CNT[15:0]; read before 0x25F')
    RX_PRBS_ERR_CNT_HI = BitField(DRP, 0x25F, 0, width=16, doc='RX_PRBS_ERR_CNT[31:16]; saturates at 0xFFFFFFFF')
    GEARBOX_MODE      = BitField(DRP, 0x099, 11, width=5, doc='GEARBOX_MODE[4:0]')
    RXGEARBOX_EN      = BitField(DRP, 0x064, 0, doc='RXGEARBOX_EN')
    RXBUF_EN          = BitField(DRP, 0x064, 3, doc='RXBUF_EN')
    TXGEARBOX_EN      = BitField(DRP, 0x07C, 13, doc='TXGEARBOX_EN')
    TXBUF_EN          = BitField(DRP, 0x07C, 7, doc='TXBUF_EN')

    RX_DEBUG_CFG      = BitField(DRP, 0x0A5, 0, width=16, doc='DRP 0x0A5 is RXCDR_CFG3_GEN3 on GTY (legacy name)')
    DMONITOR_CFG0     = BitField(DRP, 0x065, 0, width=10, doc='DMONITOR_CFG0[9:0]')
    DMONITOR_CFG1     = BitField(DRP, 0x03A, 8, width=8, doc='DMONITOR_CFG1[7:0] at DRP 0x03A[15:8]; bit 0 enables monitor')
    RXDFE_GC_CFG0     = BitField(DRP, 0x09F, 0, width=16, doc='RXDFE_GC_CFG0')
    RXDFE_GC_CFG1     = BitField(DRP, 0x0A0, 0, width=16, doc='RXDFE_GC_CFG1')
    RXDFE_GC_CFG2     = BitField(DRP, 0x0A1, 0, width=16, doc='RXDFE_GC_CFG2')
    ES_PMA_CFG        = BitField(DRP, 0x051, 0, width=10, doc='ES_PMA_CFG[9:0]')
    ES_ERRDET_EN      = BitField(DRP, 0x03C, 9, doc='ES_ERRDET_EN')
    ES_EYE_SCAN_EN    = BitField(DRP, 0x03C, 8, doc='ES_EYE_SCAN_EN')
    ES_CONTROL        = BitField(DRP, 0x03C, 10, width=6, doc='ES_CONTROL[5:0]; RUN=bit0, ARM=bit1')
    PMA_RSV2_5        = BitField(DRP, 0x03C, 8, doc='7-series PMA_RSV2[5]; on GTY same bit as ES_EYE_SCAN_EN')

    ES_QUALIFIER0     = BitField(DRP, 0x03F, 0, width=16, doc='ES_QUALIFIER0[15:0]')
    ES_QUALIFIER1     = BitField(DRP, 0x040, 0, width=16, doc='ES_QUALIFIER1[15:0]')
    ES_QUALIFIER2     = BitField(DRP, 0x041, 0, width=16, doc='ES_QUALIFIER2[15:0]')
    ES_QUALIFIER3     = BitField(DRP, 0x042, 0, width=16, doc='ES_QUALIFIER3[15:0]')
    ES_QUALIFIER4     = BitField(DRP, 0x043, 0, width=16, doc='ES_QUALIFIER4[15:0]')
    ES_QUAL_MASK0     = BitField(DRP, 0x044, 0, width=16, doc='ES_QUAL_MASK0[15:0]')
    ES_QUAL_MASK1     = BitField(DRP, 0x045, 0, width=16, doc='ES_QUAL_MASK1[15:0]')
    ES_QUAL_MASK2     = BitField(DRP, 0x046, 0, width=16, doc='ES_QUAL_MASK2[15:0]')
    ES_QUAL_MASK3     = BitField(DRP, 0x047, 0, width=16, doc='ES_QUAL_MASK3[15:0]')
    ES_QUAL_MASK4     = BitField(DRP, 0x048, 0, width=16, doc='ES_QUAL_MASK4[15:0]')
    ES_SDATA_MASK0    = BitField(DRP, 0x049, 0, width=16, doc='ES_SDATA_MASK0[15:0]')
    ES_SDATA_MASK1    = BitField(DRP, 0x04A, 0, width=16, doc='ES_SDATA_MASK1[15:0]')
    ES_SDATA_MASK2    = BitField(DRP, 0x04B, 0, width=16, doc='ES_SDATA_MASK2[15:0]')
    ES_SDATA_MASK3    = BitField(DRP, 0x04C, 0, width=16, doc='ES_SDATA_MASK3[15:0]')
    ES_SDATA_MASK4    = BitField(DRP, 0x04D, 0, width=16, doc='ES_SDATA_MASK4[15:0]')
    ES_PRESCALE       = BitField(DRP, 0x03C, 0, width=5, doc='ES_PRESCALE[4:0]')
    RX_EYESCAN_VS_UT_SIGN = BitField(DRP, 0x097, 9, doc='RX_EYESCAN_VS_UT_SIGN (was ES_VERT_OFFSET[8])')
    RX_EYESCAN_VS_NEG_DIR = BitField(DRP, 0x097, 10, doc='RX_EYESCAN_VS_NEG_DIR (was ES_VERT_OFFSET[7])')
    RX_EYESCAN_VS_CODE    = BitField(DRP, 0x097, 2, width=7, doc='RX_EYESCAN_VS_CODE[6:0] (was ES_VERT_OFFSET[6:0])')
    RX_EYESCAN_VS_RANGE   = BitField(DRP, 0x097, 0, width=2, doc='RX_EYESCAN_VS_RANGE[1:0]')
    ES_HORZ_OFFSET    = BitField(DRP, 0x04F, 4, width=12, doc='ES_HORZ_OFFSET[11:0] at DRP 0x04F[15:4]')

    RX_CM_SEL         = BitField(DRP, 0x061, 0, width=2, doc='RX_CM_SEL[1:0]')
    RX_CM_TRIM        = BitField(DRP, 0x061, 2, width=4, doc='RX_CM_TRIM[3:0]')

    ES_ERROR_COUNT    = BitField(DRP, 0x251, 0, width=16, doc='es_error_count; valid in END/WAIT')
    ES_SAMPLE_COUNT   = BitField(DRP, 0x252, 0, width=16, doc='es_sample_count; valid in END/WAIT')
    ES_CONTROL_STATUS = BitField(DRP, 0x253, 0, width=4, doc='es_control_status: [0]=DONE, [3:1]=state')

# Add DRP registers here...

    def __init__(self, *, router, router_port, instance_number, config):
        self.logger = logging.getLogger(__name__)
        super().__init__(router=router, router_port=router_port, instance_number=instance_number)
        self.config = config
        self.node_id = (self.fpga.slot, instance_number + 1)
        self._lock()

    def select(self):
        """No-op: this lane has a dedicated DRP port (API parity with :class:`cge.CGEChannel`)."""
        return self

    def unselect(self):
        """No-op."""
        return self

    def init(self):
        """ Initializes the GTY CHANNEL block"""
        # self.logger.info('Initializing GTY_CHANNEL  #%i' % self.instance_number)
        self.configure()
        if self.RX_PRESENT:  # Call only if there is a RX link, otherwise it will kill the GPU links
            self.reset_rx_equalizer()

    def status(self):
        """ Displays the status of the GTY_CHANNEL"""
        self.logger.info(f'--- GPU GTY CHANNEL {int(self.instance_number)} ')

    def get_rxdata(self):
        self.CAPTURE_ENABLE = 1
        self.CAPTURE_ENABLE = 0
        return self.RXDATA

    def get_prbs_error_ctr(self):
        """UG578 RX_PRBS_ERR_CNT: read low half (0x25E) then high (0x25F)."""
        lo = int(self.RX_PRBS_ERR_CNT_LO)
        hi = int(self.RX_PRBS_ERR_CNT_HI)
        return lo | (hi << 16)

    def _set_es_vert_offset(self, v_offset, ut_sign=0):
        """Map legacy 9-bit ES_VERT_OFFSET packing to RX_EYESCAN_VS_* (DRP 0x097)."""
        self.RX_EYESCAN_VS_CODE = abs(v_offset) & 0x7F
        self.RX_EYESCAN_VS_NEG_DIR = int(v_offset < 0)
        self.RX_EYESCAN_VS_UT_SIGN = int(bool(ut_sign))

    def configure(self):
        """ Execute only when there is a clock """
        self.SOURCE_SEL = 0  # 0:Send user packets, 1: send TXDATA word
        self.LOOPBACK = 0
        # self.TXPOLARITY=0
        # self.RXPOLARITY=0
        self.TXPRBSSEL = 0
        self.RXPRBSSEL = 0
        self.RXLPMEN = 0  # Use low power mode, not the DFE
        self.TXDIFFCTRL = 13
        self.TXPRECURSOR = 4  # DFE cannot compensate pre-cursor (but that seems to give the best result anyway!)
        self.TXPOSTCURSOR = 0b00000
        self.RXMONITORSEL = 1 # 1=AGC, 2=UL, 3=VP loop
        self.RX_DEBUG_CFG = 0x14  # 0x14= Vpeak, 0x2C=AGC (legacy; DRP 0x0A5 is RXCDR_CFG3_GEN3 on GTY)
        self.DMONITOR_CFG0 = 0x101  # DMONITOR_CFG0[9:0]: enable output (bit 0)
        self.DMONITOR_CFG1 = 1  # DMONITOR_CFG1[0] at 0x03A[15:8]

    def reset_rx_equalizer(self):
        """ Execute only when there is a clock """
        self.RXDFELPMRESET = 1
        self.RXDFELPMRESET = 0

    def capture_rx_words(self, number_of_words=1000):
        """ Return a unique set of words seen on the rx link.
        'number_of_words' words are sampled randomly, so not all words in a packet may appear in the set.
        The values are resturned as a 8-digit hex value string.
        """
        return set([f'{self.get_rxdata():08x}' for x in range(number_of_words)])



    def get_ber(self, tx_init_power=11, tx_power=15, period=0.3, verbose=1):
        # (link_type, (sc, ss, sl), (dc, ds, dl)), (self, self) = link
        DATA_RATE = 25e9
        self.RXPRBSSEL = 4
        self.TXPRBSSEL = 4
        self.TXDIFFCTRL = tx_init_power  # Setting initial power level

        self.RXDFELPMRESET = 1  # Retting the Reciever DFE
        time.sleep(0.5)
        self.RXDFELPMRESET = 0

        self.TXDIFFCTRL = tx_power

        # if print_:
        #    print 'Measuring BER for link %s' % (link[0],),
        #    print self.TXDIFFCTRL
        self.RXPRBSCNTRESET = 1  # Resetting errors
        time.sleep(0.5)
        self.RXPRBSCNTRESET = 0

        time.sleep(period)

        cnt = self.ERR_CTR
        err = (float(cnt) * 16) / (period * DATA_RATE)
        err_max = (float(cnt) * 16 + 1) / (period * DATA_RATE)

        # self.TXPRBSSEL = 0  #Setting PRBS to 0 (data path)
        # self.TXPRBSSEL = 0  #Setting PRBS to 0 (data path)
        if verbose:
            print(f'{self!r} BER = {err:1.1e} ({cnt} errors, BER<{err_max:1.1e})')
        # self.print_flush()
        return err


    def get_eye_diagram(
            self,
            horiz_offset=list(range(-32, 32, 4)),
            vert_offset=list(range(-127, 127, 16)),
            max_scaler = 12,
            ut_sign=0,
            prescale_step=6):
        """
        Return a (M x N) matrix of BER values for M horizontal and N vertical offsets.
        Horiz_offset : -32 to 32
        Vert offset: : -127 to 127
        """
        prescale_step = 4
        self.ES_EYE_SCAN_EN = 1
        self.ES_ERRDET_EN = 1
        self.ES_SDATA_MASK0 = 0x00ff
        self.ES_SDATA_MASK1 = 0x0000
        self.ES_SDATA_MASK2 = 0xFF00
        self.ES_SDATA_MASK3 = 0xFFFF
        self.ES_SDATA_MASK4 = 0xFFFF

        self.ES_QUAL_MASK0 = 0xFFFF
        self.ES_QUAL_MASK1 = 0xFFFF
        self.ES_QUAL_MASK2 = 0xFFFF
        self.ES_QUAL_MASK3 = 0xFFFF
        self.ES_QUAL_MASK4 = 0xFFFF

        if np.isscalar(horiz_offset):
            horiz_offset = [horiz_offset]

        if np.isscalar(vert_offset):
            vert_offset = [vert_offset]

        ber = np.zeros((len(horiz_offset), len(vert_offset)))
        prescale = 0

#        sample_list = [(ih,iv, h,v, h**2+v**2) for iv,v in enumerate(vert_offset) for ih,h in enumerate(horiz_offset)]
#
#        sample_list.sort(key=lambda x: x[4])
#        sample_list.reverse()
        nh = len(horiz_offset)
        nv = len(vert_offset)

        for (iv, v_offset) in enumerate(vert_offset):
            ih = 0
            processed_ih = []
            dir = 1
            while True:
                # print ih, len(horiz_offset)
                h_offset = horiz_offset[ih]
                self._set_es_vert_offset(v_offset, ut_sign)
                self.ES_HORZ_OFFSET = h_offset & 0xFFF
                progress = (iv * nh + len(set(processed_ih))) / float(nh * nv - 1) * 100
                print(f'{progress:3.0f}% Vert offset {int(iv):2}/{int(nv - 1):2}= {int(v_offset):3}, Horiz offset {int(ih):3}/{int(nh - 1):3}= {int(h_offset):3}: ', end=' ')

                while True:
                    self.ES_PRESCALE = prescale
                    self.ES_CONTROL = 0
                    self.ES_CONTROL = 1
                    while (self.ES_CONTROL_STATUS & 1) == 0:
                        time.sleep(.2)
                    error_count = self.ES_ERROR_COUNT
                    sample_count = self.ES_SAMPLE_COUNT

                    print(f'(Prescale={int(prescale)} => {int(error_count)} err / {int(sample_count)} samples) ', end=' ')
                    if sample_count < 100:
                        if prescale == 0:
                            break
                        else:
                            prescale = max(0, prescale-prescale_step)
                    elif error_count < 100:
                        if prescale == max_scaler:
                            break
                        else:
                            prescale = min(max_scaler, prescale + prescale_step)
                    else:
                        break
                if sample_count == 0:
                    sample_count = 1

                sample_count *= 2 ** (1 + prescale)
                e = float(error_count) / float(sample_count)
                print(f' --- Got {int(sample_count)} samples, {int(error_count)} errors, BER={e:1.1e}')
                ber[ih, iv] = e
                processed_ih.append(ih)
                if dir == -1 and (ih == old_ih or ih == 0):
                    break
                elif dir == 1 and ih == len(horiz_offset)-1:
                    break

                if error_count == 0:
                    if dir == 1:
                        print('Swapping direction!')
                        old_ih = ih
                        dir = -1
                        ih = len(horiz_offset)-1
                    else:
                        break
                else:
                    ih = ih+dir

        EyeDiag = collections.namedtuple('EyeDiag', ['gty', 'horiz_offsets', 'vert_offsets', 'ber_map'])

        return EyeDiag(gty=self,
                       horiz_offsets=horiz_offset,
                       vert_offsets=vert_offset,
                       ber_map=ber)

    def plot_eye_diagram(self, eye_diag=None, **kwargs):
        if eye_diag is None:
            eye_diag = self.get_eye_diagram(**kwargs)
        extent = (
            min(eye_diag.horiz_offsets),
            max(eye_diag.horiz_offsets),
            min(eye_diag.vert_offsets),
            max(eye_diag.vert_offsets))
        plt.imshow(np.log10(eye_diag.ber_map+1e-12), origin='lower', extent=extent, aspect=0.1, vmin=-12, vmax=1)
        plt.xlabel('Horizontal sampling offset')
        plt.ylabel('Vertical sampling offset')
        iceboard = eye_diag.gty.fpga
        plt.title(f'Eye diagram for IceBoard SN{iceboard.serial} (Icecrate {iceboard.crate.__class__.__name__} SN{iceboard.crate.serial} Slot {int(iceboard.slot)}) Lane {int(eye_diag.gty.instance_number + 1)}')

class XXVGLRouter(MMIRouter):
    ROUTER_PORT_NUMBER_WIDTH = 4
    ROUTER_PORT_MAP = {
        'COMMON': 0
        # port numbers for QPLL and GTY are computed
        }


class XXVGLinkCore(MMI):
    """ Instantiates a container for all the xXVglink core module

    The XXVGLink core implements an array of QPLLs and GTYes with a primitive
    data/control word interface, 64/66 bit encoding, scrambling and
    synchronization. The core is used to implement both arrays of 10G Ethernet
    transmitters as well as custom backplane 10G links.

    The core contains a number of common control registers, which are extended
    by registers added by the protocol-specific logic.
    """

    # XXVGLINK common control and status registers. These registers are technically on port 0 with
    # an address width of 10 bits, but we place them on the base address for convenience.
    CORE_RESET            = BitField(CONTROL, 0, 7, doc='The GTY cores are reset when this signal goes from 1 to 0')
    TX_RESET              = BitField(CONTROL, 0, 3, doc='')
    RX_PLL_DATAPATH_RESET = BitField(CONTROL, 0, 2, doc='')
    RX_DATAPATH_RESET     = BitField(CONTROL, 0, 1, doc='')
    RX_RESET              = BitField(CONTROL, 0, 0, doc='')

    TX_DATA_MSB     = BitField(CONTROL, 3, 0, width=24, doc='24 most significant bits of the data word that can be sent manually. This is common to all lanes.')

    NUMBER_OF_QUADS = BitField(STATUS, 0, 5, width=3, doc='Number of QUADS (QPLLs)')
    NUMBER_OF_LINKS = BitField(STATUS, 0, 0, width=5, doc='Number of links')
    RESET_PULSE     = BitField(STATUS, 1, 5, doc='debug')
    RESET_DONE      = BitField(STATUS, 1, 4, doc='debug')
    QPLL_RESET_MON  = BitField(STATUS, 1, 3, doc='debug')
    RX_CDR_STABLE   = BitField(STATUS, 1, 2, doc='debug')

    def __init__(self, *, router, router_port, config, verbose=1):
        self.logger = logging.getLogger(__name__)
        self.verbose = verbose
        self.config = config
        xxvgl_router = XXVGLRouter(router=router, router_port=router_port)

        super().__init__(router=xxvgl_router, router_port='COMMON')

        router_port = 1

        # Instantiate QUAD objects
        self.logger.info(f'{self!r}: Instantiating {self.NUMBER_OF_QUADS} QPLLs and {self.NUMBER_OF_LINKS} GTYs, XXVGLINK base addr={self.base_address:06x}, width={self.address_width}')
        self.logger.debug(f'{self!r}: Instantiating {self.NUMBER_OF_QUADS} QPLLs ')
        self.qpll = []
        qpll_state = self.config.add("qpll")
        for j in range(self.NUMBER_OF_QUADS):
            self.qpll.append(QPLL(router=xxvgl_router, router_port=router_port, instance_number=j, config=qpll_state.add(j)))
            router_port += 1

        self.logger.info(f'{self!r}: Instantiating {self.NUMBER_OF_LINKS} GTYs')
        self.gty = []
        gty_state = self.config.add("gty")
        for j in range(self.NUMBER_OF_LINKS):
            self.gty.append(GTY(router=xxvgl_router, router_port=router_port, instance_number=j, config=gty_state.add(j)))
            router_port += 1

    def init(self):
        """ Initializes the links"""

        self.logger.debug(f'{self!r}: Initializing GTY links ({len(self.qpll)} QUADs & {len(self.gty)} GTYs)')
        for (i, qpll) in enumerate(self.qpll):
            qpll.init()

        for (i, gty) in enumerate(self.gty):
            gty.init()

    def reset_rx_equalizers(self):
        for g in self.gty:
            g.reset_rx_equalizer()

    def set_tx_power(self, power):
        for g in self.gty:
            g.TXDIFFCTRL = power

    def status(self):
        """ Displays the status of the QPLLs and GTYes"""

        print('Common Bitfields')
        for (name, value) in self.read_all_fields():
            print(f'    {name} = {int(value)}, 0x{value:X}, {bin(value)}')

        for (i, qpll) in enumerate(self.qpll):
            print(f'QPLL[{int(i)}] Bitfields')
            for (name, value) in qpll.read_all_fields():
                print(f'    {name} = {int(value)}, 0x{value:X}, {bin(value)}')

        for (i, gty) in enumerate(self.gty):
            print(f'GTY[{int(i)}] Bitfields')
            for (name, value) in gty.read_all_fields():
                print(f'    {name} = {int(value)}, 0x{value:X}, {bin(value)}')


class XXVGLinkArray(XXVGLinkCore):
    """ Instantiates an object that represents the VHDL xXVglink_array, i.e. an ensemble of GTYes with
    a knowledge of lane groups.

    The XXVGLinkArray implements a XXVGLinkCore (ensemble of QPLLs and GTYes and date
    encoding/synchonization) and adds without minimal packet framing (SOF, EOF) and checksum
    (CRC32). It provides an additional set of registers to the XXVGLinkCore, and implement internal
    links as well as GTY bypasses. XXVGLink is also aware of logical lane groups (e.g. 'pcb', 'qsfp'
    links).

    XXVGLinkArray is used to implement the corner turn data links by tranmitting data though the
    backplane PCB tracks and backplane QSFP connectors.


    Parameters:

        fpga_instance (chFPGA_controller instance): Instance of the FPGA
            board, which is used to access various system parameters and the
            UDP MMI.

        base_address (int): UDP MMI Address where the first register of the
            XXVGLinkArray is located

        address_increment (int): Address spacing between various subsystems
            (common register block, QPLLs, GTYes)

        lane_groups (list of tuples): Defines the lane groups that are
            supported by the XXVGLinkArray subsystem. Is in the format::

            [ (link_type, number_if_direct_lanes, number_of_gty_links), ...]

        verbose (int): Indicates the verbose level.

    """

    # ########################################
    # XXVGLINK_ARRAY.VHD control and status registers
    # ########################################

    # CONTROL bytes 0-3 are provided by XXVGLinkCore
    RESET_STATS     = BitField(CONTROL, 4, 2, doc='')
    LANE_SEL        = BitField(CONTROL, 4, 3, width=5, doc='')
    RX_CAPTURE_EN   = BitField(CONTROL, 4, 1, doc='Enable capture of raw RX DATA/HEADER')
    RX_RECV_RESET        = BitField(CONTROL, 4, 0, doc='Resets all packet receivers')

    BYPASS_PCB_SHUFFLE  = BitField(CONTROL, 5, 0, doc='')
    BYPASS_QSFP_SHUFFLE = BitField(CONTROL, 5, 1, doc='')

    LANE_MON_EN       = BitField(CONTROL, 6, 6, doc='When True, allows RXCLK_MON_WORD and TXCLK_MON_WORD to update. Disable during register reads.')
    LANE_MON_SRC      = BitField(CONTROL, 6, 4, width=2, doc='Selects source of data being monitored for selected lane (TXCLK domain: 0=TX, 1=RX, RXCLK domain: no effect, always RX FIFO IN)')
    LANE_MON_TYPE      = BitField(CONTROL, 6, 0, width=4, doc='Selects type of data being monitored for selected lane (0: word counter, 1: packet length, 2: current packet length, 3: min packet length, 4: max packet length, 5: error counter, 6: FIFO overflow counter)')


    TX_FORCE_EN   = BitField(CONTROL, 7, 2, doc='Enable forcing of DATA/HEADER')
    TX_FORCE_HEADER   = BitField(CONTROL, 7, 0, width=2, doc='HEADER to be forced')
    TX_FORCE_DATA   = BitField(CONTROL, 15, 0, width=64, doc='DATA to be forced')

    # FIFO_RESET      = BitField(CONTROL, 4+1, 0, doc='Resets the RX FIFO')

    # STATUS bytes 0-1 are provided by XXVGLinkCore
    RX_FIFO_OVERFLOW = BitField(STATUS, 2, 0, doc='Sticky fifo overflow bit for the selected lane. Is cleared when RESET_STATS=1.')
    RESET_MON        = BitField(STATUS, 2, 1, doc='State of the reset line')
    RX_FRAME_DETECT  = BitField(STATUS, 2, 2, doc='Sticky bit indicating that a data frame was detected. Is cleared when RESET_STATS=1.')
    TX_FIFO_OVERFLOW = BitField(STATUS, 2, 3, doc='Sticky fifo overflow bit for the selected lane. Is cleared when RESET_STATS=1.')
    RX_CAPTURE_HEADER = BitField(STATUS, 2, 6, width=2, doc='Captured RX header bits when RX_CAPTURE_EN=1')
    # TEST_CTR         = BitField(STATUS, 2+0, 4, width=3, doc='State of the test pattern counter')

    RXCLK_MON_WORD        = BitField(STATUS,4, 0, width=16, doc='Monitor for the signals in the RXCLK domain. Lane, source and type (i.e meaning) of the word is set by LANE_SEL, LANE_MON_SRC and LANE_MON_TYPE. Is cleared when RESET_STATS=1.')
    TXCLK_MON_WORD        = BitField(STATUS,6, 0, width=16, doc='Monitor for the signals in the TXCLK domain. Lane, source and type (i.e meaning) of the word is set by LANE_SEL, LANE_MON_SRC and LANE_MON_TYPE. Is cleared when RESET_STATS=1.')
    # RX_MAX_FRAME_LENGTH = BitField(STATUS, 2 + 4, 0, width=13, doc='Current value of the maximum frame length detector. Is cleared when RESET_STATS=1.')
    # RX_MIN_FRAME_LENGTH = BitField(STATUS, 2 + 6, 0, width=13, doc='Current value of the minimum frame length detector. Is cleared when RESET_STATS=1.')
    # # RX_CTR              = BitField(STATUS, 2+5, 0, width=8, doc='Free runing counter on the local RX clock. Is cleared when RESET_STATS=1.')
    # TX_WORD_CTR        = BitField(STATUS, 10, 0, width=16, doc='Cleared when RESET_STATS=1.')
    # TX_PACKET_LENGTH   = BitField(STATUS, 12, 0, width=16, doc='Cleared when RESET_STATS=1.')
    # TX_PACKET_CTR   = BitField(STATUS, 13, 0, width=8, doc='Cleared when RESET_STATS=1.')

    RX_CAPTURE_DATA = BitField(STATUS, 14, 0, width=64, doc='Captured RX data word when RX_CAPTURE_EN=1')

    RX_LANE_MONITOR_TABLE = {
        'RX_FIFO_OVERFLOW': 'RX_FIFO_OVERFLOW',
        'TX_FIFO_OVERFLOW': 'TX_FIFO_OVERFLOW',
        'ERROR_CTR': 'RX_ERROR_CTR',
        'MAX_FRAME_LENGTH': 'RX_MAX_FRAME_LENGTH',
        'MIN_FRAME_LENGTH': 'RX_MIN_FRAME_LENGTH',
        'FRAME_DETECT': 'RX_FRAME_DETECT',
        # 'RX_CTR': 'RX_CTR',
        'RX_FRAME_CTR': 'RX_FRAME_CTR'}

    def __init__(self, *, router, router_port, lane_groups, config, verbose=1):

        super().__init__(router=router, router_port=router_port, config=config, verbose=verbose)

        # self.LANE_GROUPS = {}
        # group name : (first lane, number_of_bypass_lanes, number_of_links)
        # self.LANE_GROUPS[0] = self.LANE_GROUPS['pcb'] = (0, self.NUMBER_OF_PCB_DIRECT_LANES, self.NUMBER_OF_PCB_LINKS)
        # self.LANE_GROUPS[1] = self.LANE_GROUPS['qsfp'] = (self.NUMBER_OF_PCB_LANES, self.NUMBER_OF_QSFP_DIRECT_LANES, self.NUMBER_OF_QSFP_LINKS)

        # lane_list = []
        phys_lane = 0
        gty_ix = 0

        self.gty_map = {None: []}  # list of GTY instance for each group. The None group lists them all.
        self.phys_lane_map = {None: []}  # list of the physical lane limbers for each group

        for group, n_direct_lanes, n_links in lane_groups:
            self.gty_map[group] = []
            self.phys_lane_map[group] = []
            # lane_list[group] = []
            for lane in range(n_direct_lanes):
                # lane_list.append((group, lane, phys_lane, None, None))  # internal link, no GTY
                for g in [group, None]:
                    self.gty_map[g].append(None)
                    self.phys_lane_map[g].append(phys_lane)
                phys_lane += 1
            for lane in range(n_direct_lanes, n_direct_lanes + n_links):
                # lane_list.append((group, lane, phys_lane, gty_ix, self.gty[gty_ix]))
                for g in [group, None]:
                    self.gty_map[g].append(self.gty[gty_ix])
                    self.phys_lane_map[g].append(phys_lane)
                phys_lane += 1
                gty_ix += 1
        # self.lane_list = lane_list

        # Create a lane map that maps gty instances to group name and logical
        # lane number, or to physical lane number if the group name is None
        # self.lane_map = {None: {}}
        # for group, lane, phys_lane, gty_ix, gty in self.lane_list:
        #     self.lane_map.setdefault(group, {})[lane] = (phys_lane, gty_ix, gty)

    def is_gty(self, obj):
        """ Test whether an object is a GTY instance.

        Parameters:

            obj: object to test

        Returns:

            A bool.
        """
        return isinstance(obj, GTY)

    def get_physical_lane_numbers(self, lane_group=None):
        """ Returns a list of physical lane numbers that correspond to the specified group.

        Physical lane number can be used to index lane logic in the fpga.
        Lanes in all the groups have a unique physical lane number.

        Parameters:

            group (str): target lane group. If None, physical lane number for all groups are
                returned.

        Returns:

            List of integers.
        """
        if lane_group not in self.phys_lane_map:
            raise ValueError(f'Invalid lane group name {lane_group}')
        return self.phys_lane_map[lane_group]

    def get_lane_numbers(self, lane_group):
        """ Returns a list of logical lane numbers for the specified lane group.

        Parameters:

            lane_group (str): target lane group. Cannot be `None`, as the logical lane numbers are
                not unique between groups.

        Returns:

            List of integers.
        """
        if lane_group not in self.gty_map:
            raise ValueError(f'Invalid lane group name {lane_group}')
        if lane_group is None:
            raise ValueError('Logical lane numbers cannot be obtained for lane group "None": '
                             'the lane numbers are not unique')
        return list(range(len(self.gty_map[lane_group])))

    def get_gty(self, lane=None, lane_group=None):
        """ Returns a single or a list of GTY instances that correspond to the specified group and lanes.

        Parameters:

            lane (int or list of int): Single lane or list of lanes for which to get the GTY
                instance. if `lane` is None, all lanes are returned within the group are returned.
                If group is None, lanes from both groups are queries and physical lane number is
                expected instead of the logical lane number.

            lane_group (str): Name of the lane group in which the GTY belongs
                ('pcb' or 'qsfp'). If None, all GTYes are returned.

        Returns:

            List of GTY instances. Returns None for internal data links.
        """
        if lane_group not in self.gty_map:
            raise ValueError(f'Invalid lane group name {lane_group}')
        gty_map = self.gty_map[lane_group]
        if lane is None:
            return gty_map
        elif isinstance(lane, int):
            return gty_map[lane]
        else:
            return [gty_map[l] for l in lane]

    def set_tx_power(self,  power, lane_group=None):
        """ Sets the power level of the GTYes in the specified lane group.

        Parameters:

            power (int, tuple or list of tuple): If an 'int', power level applied to all GTY in the
                group. Power of an individual lanes can be set by providing a single (lane, power)
                tuple. A list of (lane, power) tuples can be specified to set multiple lanes. If
                `lane_group` is `None`, physical lane numbers are used instead of logical lane
                numbers.

            lane_group (str): lane group name of the target GTYes. If `None`, all groups are
                selected and physical lane numbers should be used.

        """

        if lane_group not in self.gty_map:
            raise ValueError(f'Invalid lane group name {lane_group}')
        gty_map = self.gty_map[lane_group]

        if isinstance(power, int):
            power = [(i, power) for i, gty in enumerate(gty_map) if gty]  # exclude internal links (no GTY)
        elif (isinstance(power, (tuple, list))
                and isinstance(power[0], int)
                and isinstance(power[1], int)
                and len(power) == 2):
            power = [power]
        for lane, pwr in power:
            gty = gty_map[lane]
            if not gty:
                self.logger.warning(f'There is no GTY at the specified lane {int(lane)} of group {lane_group} (it is a direct internal link)')
            else:
                gty.TXDIFFCTRL = pwr

    async def get_rx_lane_monitor(self, names, lane_group=None):
        """ Retreive monitoring info for the specified monitoring points in the target lane group.

        Parameters:

            names (str or list of str): name of the monitoring poins to return.

            lane_group: name of the lane group to query. If None, all lanes from all groups are returned.
        """
        if isinstance(names, str):
            names = [names]
            is_list = False
        else:
            is_list = True

        # Get the physical lane number of target lanes
        phys_lanes = self.get_physical_lane_numbers(lane_group)

        # Build a list of Bitfields to access for each lane
        bitfields = []
        for name in names:
            if name not in self.RX_LANE_MONITOR_TABLE:
                raise ValueError(f"Invalid lane monitor name. valid names are {','.join(self.RX_LANE_MONITOR_TABLE.keys())}")
            bitfields.append(self.get_bitfield(self.RX_LANE_MONITOR_TABLE[name]))

        # get monitoring results
        mon = [list() for _ in names]
        for phys_lane in phys_lanes:
            await asyncio.sleep(0)
            self.LANE_SEL = phys_lane
            for i, bf in enumerate(bitfields):
                mon[i].append(self.read_bitfield(bf))

        return mon if is_list else mon[0]

    def reset_stats(self):
        self.RESET_STATS = 1
        self.RESET_STATS = 0

    # async def get_rx_error_count(self, lane_group=None):
    #     return await self.get_rx_lane_monitor('ERROR_CTR', lane_group)

    async def get_metrics(self, reset=True):
        """ Return metrics on the status of the rx links as a Metrics object.
        """
        metrics = Metrics(
            crate_id=self.fpga.crate.get_string_id() if self.fpga.crate else None,
            crate_number=self.fpga.mb.get_crate_number(),
            slot=(self.fpga.slot or 0) - 1,
            id=self.fpga.get_string_id(),
            type='GAUGE')

        for link_type, link_group in [('pcb_gty', 'pcb'), ('qsfp_gty', 'qsfp')]:
            await asyncio.sleep(0)  # let the ioloop process data

            err, min_len, max_len, frame_det, rx_fifo, tx_fifo = await self.get_rx_lane_monitor(
                ['ERROR_CTR', 'MIN_FRAME_LENGTH', 'MAX_FRAME_LENGTH',
                 'FRAME_DETECT', 'RX_FIFO_OVERFLOW', 'TX_FIFO_OVERFLOW'],
                link_group)
            for lane in range(len(err)):
                metrics.add('fpga_bp_link_errors', value=err[lane], link_type=link_type, lane=lane)
                metrics.add('fpga_bp_link_min_length', value=min_len[lane], link_type=link_type, lane=lane)
                metrics.add('fpga_bp_link_max_length', value=max_len[lane], link_type=link_type, lane=lane)
                metrics.add('fpga_bp_link_frame_detect', value=frame_det[lane], link_type=link_type, lane=lane)
                metrics.add('fpga_bp_link_rx_fifo_overflow', value=rx_fifo[lane], link_type=link_type, lane=lane)
                metrics.add('fpga_bp_link_tx_fifo_overflow', value=tx_fifo[lane], link_type=link_type, lane=lane)
                metrics.add('fpga_bp_link_error_overflow', value=(err[lane] == 255), link_type=link_type, lane=lane)
                metrics.add('fpga_bp_link_length_mismatch', value=(min_len[lane] != max_len[lane]),
                            link_type=link_type, lane=lane)
        for gty_number, gty in enumerate(self.gty):
            await asyncio.sleep(0)  # let the ioloop process data
            #gty_number = lane + link_group*self.NUMBER_OF_PCB_LANES
            metrics.add('fpga_bp_link_tx_power', value=gty.TXDIFFCTRL, gty=gty_number)
            metrics.add('fpga_bp_link_rx_power', value=gty.DMONITOROUT & 0x7F, gty=gty_number)
            metrics.add('fpga_bp_link_block_lock', value=gty.BLOCK_LOCK, gty=gty_number)

        if reset:
            self.reset_stats()

        return metrics

    async def get_bp_rx_status(self, link_group=None):
        """ Checks the status of the rx links. Returns a list of dict, each
        dict containing a number of {error_type:error_info} for the
        corresponding lane.
        """
        status = []
        bypassed = (link_group=='pcb' and self.BYPASS_PCB_SHUFFLE) or (link_group =='qsfp' and self.BYPASS_QSFP_SHUFFLE)

        err, min_len, max_len, frame_det, rx_fifo, tx_fifo = await self.get_rx_lane_monitor(
            ('ERROR_CTR', 'MIN_FRAME_LENGTH', 'MAX_FRAME_LENGTH',
             'FRAME_DETECT', 'RX_FIFO_OVERFLOW', 'TX_FIFO_OVERFLOW'),
            link_group)
        for lane in range(len(err)):
            lane_status = {}
            if not bypassed and err[lane]:
                lane_status['ERR'] = err[lane]
            if not bypassed and max_len[lane] != min_len[lane]:
                lane_status['LEN'] = (min_len[lane], max_len[lane])
            if not bypassed and not frame_det[lane]:
                lane_status['FDET'] = 0
            if rx_fifo[lane] or tx_fifo[lane]:
                lane_status['FIFO'] = ','.join((['RX'] if rx_fifo[lane] else []) + (['TX'] if tx_fifo[lane] else []))
            status.append(lane_status)
        return status

    async def print_rx_lane_monitor(self, reset=False):
        """
        """
        if reset:
            self.reset_stats()

        gty_ids = [(self.fpga.slot, lane) for lane in range(self.NUMBER_OF_PCB_LANES)]
        active_slots = set(self.fpga.crate.slot.keys())
        matching_gty_ids = [self.fpga.crate.get_matching_tx(gty_id) for gty_id in gty_ids]

        print(
            f"{'XXVGLINK_Array Lane #':>20}: "
            f"{' '.join(f'  L{v:2d} ' for v in range(self.NUMBER_OF_LANES))}"
        )
        print(f"{'--------------------':>20}: {' ' + ' '.join(('------' for v in range(self.NUMBER_OF_LANES)))}")
        print(
            f"{'GTY ID':>20}: "
            f"{''.join(f'({id_[0]},{id_[1]})'.rjust(7) for id_ in gty_ids)}"
        )
        print(
            f"{'Matching GTY ID':>20}: "
            f"{''.join(f'({matching_id[0]},{matching_id[1]})'.rjust(7) for matching_id in matching_gty_ids)}"
        )
        print(
            f"{'Matching GTY present':>20}: "
            f"{' '.join(('-N/A-', 'ok ')[matching_id[0] in active_slots].rjust(6) for matching_id in matching_gty_ids)}"
        )
        for name in self.RX_LANE_MONITOR_TABLE:
            vals = await self.get_rx_lane_monitor(name)
            print(f"{name:>20}: {' '.join(f'{v:6d}' for v in vals)}")
        print(
            f"{'DMONITOR':>20}: {'N/A':>6} "
            f"{' '.join(f'{(g.DMONITOROUT & 0x7f):6d}' for g in self.gty)}"
        )
        print(
            f"{'BLOCK_LOCK':>20}: {'N/A':>6} "
            f"{' '.join(f'{g.BLOCK_LOCK:6d}' for g in self.gty)}"
        )

        if self.RESET_MON:
            print('WARNING: BP_SHUFFLE reset is active (areset=1)!')