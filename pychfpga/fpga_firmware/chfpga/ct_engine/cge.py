"""
cge.py module
    Implements interface to the 100G Ethernet FPGA module

Per-lane GT transceiver lab access (BER, eye scan) is via ``CGE.gtx[i]`` (:class:`CGEChannel`).
CMAC core DRP is via ``CGE.core`` (:class:`CGECore`); GT common (QPLL) DRP via ``CGE.gt_common``
(:class:`CGECommon`). PRBS pattern select and checker reset use minimal CMAC GT fabric ports on
:class:`CGE` (not available via DRP).
"""
import collections
import logging
import time

import numpy as np
import matplotlib.pyplot as plt

from ..mmi import MMI, BitField, CONTROL, STATUS, DRP


class _CGEDrpPort:
    """Base for CGE DRP views. Saves/restores CONFIG_PORT_SEL and GT_LANE_SEL."""

    def __init__(self, cge, port, lane=None):
        self._cge = cge
        self._port = port
        self._lane = lane
        self._drp_mux_saved = None
        self.fpga = cge.fpga
        self.read_drp = cge.read_drp
        self.write_drp = cge.write_drp

    def select(self):
        """Select this DRP port (and lane if set); save mux state for :meth:`unselect`."""
        cge = self._cge
        self._drp_mux_saved = (int(cge.CONFIG_PORT_SEL), int(cge.GT_LANE_SEL))
        cge.CONFIG_PORT_SEL = self._port
        if self._lane is not None:
            cge.GT_LANE_SEL = self._lane
        return self

    def unselect(self):
        """Restore CONFIG_PORT_SEL / GT_LANE_SEL saved by :meth:`select`."""
        if self._drp_mux_saved is None:
            return self
        port, lane = self._drp_mux_saved
        self._cge.CONFIG_PORT_SEL = port
        self._cge.GT_LANE_SEL = lane
        self._drp_mux_saved = None
        return self

    def __repr__(self):
        name = self.__class__.__name__
        if self._lane is not None:
            return f'{self.fpga!r}.{name}[{int(self._lane)}]'
        return f'{self.fpga!r}.{name}'


class CGECore(_CGEDrpPort):
    """CMAC core DRP (PG203). Call :meth:`select` before access."""

    CTL_TX_PTP_1STEP_ENABLE = BitField(DRP, 0x000, 0, doc='CTL_TX_PTP_1STEP_ENABLE')
    CTL_TX_IGNORE_FCS = BitField(DRP, 0x001, 0, doc='CTL_TX_IGNORE_FCS')
    CTL_TX_FCS_INS_ENABLE = BitField(DRP, 0x002, 0, doc='CTL_TX_FCS_INS_ENABLE')
    CTL_TX_OPCODE_GPP = BitField(DRP, 0x006, 0, width=16, doc='CTL_TX_OPCODE_GPP[15:0]')
    CTL_TX_ETHERTYPE_PPP = BitField(DRP, 0x007, 0, width=16, doc='CTL_TX_ETHERTYPE_PPP[15:0]')
    CTL_TX_OPCODE_PPP = BitField(DRP, 0x008, 0, width=16, doc='CTL_TX_OPCODE_PPP[15:0]')
    CTL_TX_VL_LENGTH_MINUS1 = BitField(DRP, 0x00C, 0, width=16, doc='CTL_TX_VL_LENGTH_MINUS1[15:0]')
    CTL_TX_IPG_VALUE = BitField(DRP, 0x00D, 0, width=4, doc='CTL_TX_IPG_VALUE[3:0]')
    CTL_TX_ETHERTYPE_GPP = BitField(DRP, 0x0B0, 0, width=16, doc='CTL_TX_ETHERTYPE_GPP[15:0]')
    CTL_RX_CHECK_PREAMBLE = BitField(DRP, 0x0A2, 0, doc='CTL_RX_CHECK_PREAMBLE')
    CTL_RX_IGNORE_FCS = BitField(DRP, 0x0A3, 0, doc='CTL_RX_IGNORE_FCS')
    CTL_RX_FORWARD_CONTROL = BitField(DRP, 0x0A4, 0, doc='CTL_RX_FORWARD_CONTROL')
    CTL_RX_DELETE_FCS = BitField(DRP, 0x0A5, 0, doc='CTL_RX_DELETE_FCS')
    CTL_RX_CHECK_ACK = BitField(DRP, 0x0A8, 0, doc='CTL_RX_CHECK_ACK')
    CTL_RX_CHECK_SFD = BitField(DRP, 0x0A9, 0, doc='CTL_RX_CHECK_SFD')
    CTL_RX_PROCESS_LFI = BitField(DRP, 0x0AA, 0, doc='CTL_RX_PROCESS_LFI')
    CTL_RX_MIN_PACKET_LEN = BitField(DRP, 0x0AE, 0, width=8, doc='CTL_RX_MIN_PACKET_LEN[7:0]')
    CTL_RX_MAX_PACKET_LEN = BitField(DRP, 0x0AF, 0, width=15, doc='CTL_RX_MAX_PACKET_LEN[14:0]')
    CTL_RX_OPCODE_GPP = BitField(DRP, 0x0B1, 0, width=16, doc='CTL_RX_OPCODE_GPP[15:0]')
    CTL_RX_RSFEC_FILL_ADJUST = BitField(DRP, 0x157, 0, width=2, doc='CTL_RX_RSFEC_FILL_ADJUST[1:0]')
    CTL_RX_RSFEC_AM_THRESHOLD = BitField(DRP, 0x158, 0, width=9, doc='CTL_RX_RSFEC_AM_THRESHOLD[8:0]')
    CTL_TX_CUSTOM_PREAMBLE_ENABLE = BitField(DRP, 0x159, 0, doc='CTL_TX_CUSTOM_PREAMBLE_ENABLE')


class CGECommon(_CGEDrpPort):
    """GT common (QPLL) DRP on a CGE block. Call :meth:`select` before access."""

    QPLL_INIT_CFG = BitField(DRP, 0x0030, 0, width=16, doc="0-65535")
    QPLL_LPF = BitField(DRP, 0x0031, 11, width=4, doc="0-15")
    QPLL_INIT_CFG = BitField(DRP, 0x0031, 0, width=8, doc="0-255")
    QPLL_CFG = BitField(DRP, 0x0032, 0, width=16, doc="0-65535")
    QPLL_REFCLK_DIV = BitField(DRP, 0x0033, 11, width=5, doc="1: 16, 2: 0, 3: 1, 4: 2, 5: 3, 6: 5, 8: 6, 10: 7, 12: 13, 16: 14, 20: 15")
    QPLL_CFG = BitField(DRP, 0x0033, 0, width=11, doc="0-2047")
    QPLL_LOCK_CFG = BitField(DRP, 0x0034, 0, width=16, doc="0-65535")
    QPLL_COARSE_FREQ_OVRD = BitField(DRP, 0x0035, 10, width=6, doc="0-63")
    QPLL_CP = BitField(DRP, 0x0035, 0, width=10, doc="0-1023")
    QPLL_DMONITOR_SEL = BitField(DRP, 0x0036, 15, doc="0-1")
    QPLL_FBDIV_MONITOR_EN = BitField(DRP, 0x0036, 14, doc="0-1")
    QPLL_CP_MONITOR_EN = BitField(DRP, 0x0036, 13, doc="0-1")
    QPLL_COARSE_FREQ_OVRD_EN = BitField(DRP, 0x0036, 11, doc="0-1")
    QPLL_FBDIV = BitField(DRP, 0x0036, 0, width=10, doc="0-1023")
    QPLL_FBDIV_RATIO = BitField(DRP, 0x0037, 6, doc="0-1")
    QPLL_CLKOUT_CFG = BitField(DRP, 0x0037, 2, width=4, doc="0-15")
    BIAS_CFG0 = BitField(DRP, 0x003E, 0, width=16, doc="BIAS_CFG[15:0 ] 0-65535")
    BIAS_CFG1 = BitField(DRP, 0x003F, 0, width=16, doc="BIAS_CFG[31:16] 0-65535")
    BIAS_CFG2 = BitField(DRP, 0x0040, 0, width=16, doc="BIAS_CFG[47:32] 0-65535")
    BIAS_CFG3 = BitField(DRP, 0x0041, 0, width=16, doc="BIAS_CFG[63:48] 0-65535")
    COMMON_CFG0 = BitField(DRP, 0x0043, 0, width=16, doc="COMMON_CFG[15:0 ] 0-65535")
    COMMON_CFG1 = BitField(DRP, 0x0044, 0, width=16, doc="COMMON_CFG[31:16] 0-65535")


class CGEChannel(_CGEDrpPort):
    """Per-lane GTY channel DRP on a CGE block (UG578 GTYE3_CHANNEL).

    Same high-level API as :class:`xxvglink.GTY` for ``get_ber``, ``get_eye_diagram``, and
    ``plot_eye_diagram``. Those methods call :meth:`select` once; other DRP access requires
    the caller to select first. PRBS fabric is on :class:`CGE` (bytes 7-8); use
    ``select_gt_lane`` then set ``RXPRBSSEL`` etc. on the parent block.
    """

    # GT channel DRP (UG578 GTYE3_CHANNEL; same addresses as xxvglink.GTY)
    RX_PRBS_ERR_CNT_LO = BitField(DRP, 0x25E, 0, width=16, doc='RX_PRBS_ERR_CNT[15:0]; read before 0x25F')
    RX_PRBS_ERR_CNT_HI = BitField(DRP, 0x25F, 0, width=16, doc='RX_PRBS_ERR_CNT[31:16]; saturates at 0xFFFFFFFF')
    GEARBOX_MODE = BitField(DRP, 0x099, 11, width=5, doc='GEARBOX_MODE[4:0]')
    RXGEARBOX_EN = BitField(DRP, 0x064, 0, doc='RXGEARBOX_EN')
    RXBUF_EN = BitField(DRP, 0x064, 3, doc='RXBUF_EN')
    TXGEARBOX_EN = BitField(DRP, 0x07C, 13, doc='TXGEARBOX_EN')
    TXBUF_EN = BitField(DRP, 0x07C, 7, doc='TXBUF_EN')
    RX_DEBUG_CFG = BitField(DRP, 0x0A5, 0, width=16, doc='DRP 0x0A5 is RXCDR_CFG3_GEN3 on GTY (legacy name)')
    DMONITOR_CFG0 = BitField(DRP, 0x065, 0, width=10, doc='DMONITOR_CFG0[9:0]')
    DMONITOR_CFG1 = BitField(DRP, 0x03A, 8, width=8, doc='DMONITOR_CFG1[7:0] at DRP 0x03A[15:8]; bit 0 enables monitor')
    RXDFE_GC_CFG0 = BitField(DRP, 0x09F, 0, width=16, doc='RXDFE_GC_CFG0')
    RXDFE_GC_CFG1 = BitField(DRP, 0x0A0, 0, width=16, doc='RXDFE_GC_CFG1')
    RXDFE_GC_CFG2 = BitField(DRP, 0x0A1, 0, width=16, doc='RXDFE_GC_CFG2')
    ES_PMA_CFG = BitField(DRP, 0x051, 0, width=10, doc='ES_PMA_CFG[9:0]')
    RX_CM_SEL = BitField(DRP, 0x061, 0, width=2, doc='RX_CM_SEL[1:0]')
    RX_CM_TRIM = BitField(DRP, 0x061, 2, width=4, doc='RX_CM_TRIM[3:0]')
    ES_EYE_SCAN_EN = BitField(DRP, 0x03C, 8, doc='ES_EYE_SCAN_EN')
    ES_ERRDET_EN = BitField(DRP, 0x03C, 9, doc='ES_ERRDET_EN')
    ES_CONTROL = BitField(DRP, 0x03C, 10, width=6, doc='ES_CONTROL[5:0]; RUN=bit0, ARM=bit1')
    ES_PRESCALE = BitField(DRP, 0x03C, 0, width=5, doc='ES_PRESCALE[4:0]')
    ES_SDATA_MASK0 = BitField(DRP, 0x049, 0, width=16, doc='ES_SDATA_MASK0[15:0]')
    ES_SDATA_MASK1 = BitField(DRP, 0x04A, 0, width=16, doc='ES_SDATA_MASK1[15:0]')
    ES_SDATA_MASK2 = BitField(DRP, 0x04B, 0, width=16, doc='ES_SDATA_MASK2[15:0]')
    ES_SDATA_MASK3 = BitField(DRP, 0x04C, 0, width=16, doc='ES_SDATA_MASK3[15:0]')
    ES_SDATA_MASK4 = BitField(DRP, 0x04D, 0, width=16, doc='ES_SDATA_MASK4[15:0]')
    ES_QUAL_MASK0 = BitField(DRP, 0x044, 0, width=16, doc='ES_QUAL_MASK0[15:0]')
    ES_QUAL_MASK1 = BitField(DRP, 0x045, 0, width=16, doc='ES_QUAL_MASK1[15:0]')
    ES_QUAL_MASK2 = BitField(DRP, 0x046, 0, width=16, doc='ES_QUAL_MASK2[15:0]')
    ES_QUAL_MASK3 = BitField(DRP, 0x047, 0, width=16, doc='ES_QUAL_MASK3[15:0]')
    ES_QUAL_MASK4 = BitField(DRP, 0x048, 0, width=16, doc='ES_QUAL_MASK4[15:0]')
    RX_EYESCAN_VS_UT_SIGN = BitField(DRP, 0x097, 9, doc='RX_EYESCAN_VS_UT_SIGN (was ES_VERT_OFFSET[8])')
    RX_EYESCAN_VS_NEG_DIR = BitField(DRP, 0x097, 10, doc='RX_EYESCAN_VS_NEG_DIR (was ES_VERT_OFFSET[7])')
    RX_EYESCAN_VS_CODE = BitField(DRP, 0x097, 2, width=7, doc='RX_EYESCAN_VS_CODE[6:0] (was ES_VERT_OFFSET[6:0])')
    ES_HORZ_OFFSET = BitField(DRP, 0x04F, 4, width=12, doc='ES_HORZ_OFFSET[11:0] at DRP 0x04F[15:4]')
    ES_ERROR_COUNT = BitField(DRP, 0x251, 0, width=16, doc='es_error_count; valid in END/WAIT')
    ES_SAMPLE_COUNT = BitField(DRP, 0x252, 0, width=16, doc='es_sample_count; valid in END/WAIT')
    ES_CONTROL_STATUS = BitField(DRP, 0x253, 0, width=4, doc='es_control_status: [0]=DONE, [3:1]=state')
    # TX_MARGIN_FULL_0[6:0] = {TXBUFDIFFCTRL[2:0], TXDIFFCTRL[3:0]} at DRP 0x07F[15:9] (UG578)
    TX_MARGIN_FULL_0 = BitField(DRP, 0x07F, 9, width=7, doc='TX driver margin / TXDIFFCTRL encoding (DRP 0x07F[15:9])')

    def __init__(self, cge, port, lane):
        super().__init__(cge, port, lane)
        self.instance_number = lane
        self.config = cge.config
        self.logger = logging.getLogger(__name__)

    def get_prbs_error_ctr(self):
        """UG578 RX_PRBS_ERR_CNT: read low half (0x25E) then high (0x25F). Port must be selected."""
        lo = int(self.RX_PRBS_ERR_CNT_LO)
        hi = int(self.RX_PRBS_ERR_CNT_HI)
        return lo | (hi << 16)

    def _set_es_vert_offset(self, v_offset, ut_sign=0):
        """Map legacy 9-bit ES_VERT_OFFSET packing to RX_EYESCAN_VS_* (DRP 0x097). Port must be selected."""
        self.RX_EYESCAN_VS_CODE = abs(v_offset) & 0x7F
        self.RX_EYESCAN_VS_NEG_DIR = int(v_offset < 0)
        self.RX_EYESCAN_VS_UT_SIGN = int(bool(ut_sign))

    def set_txdiffctrl(self, txdiffctrl):
        """Set TXDIFFCTRL[3:0] via DRP TX_MARGIN_FULL_0 (UG578 0x07F[15:9]). Port must be selected."""
        self.TX_MARGIN_FULL_0 = int(txdiffctrl) & 0x0F

    def configure(self):
        """Default GTY DRP settings (same intent as xxvglink.GTY.configure). Selects DRP for the call."""
        self.select()
        try:
            self.RX_DEBUG_CFG = 0x14
            self.DMONITOR_CFG0 = 0x101
            self.DMONITOR_CFG1 = 1
        finally:
            self.unselect()

    def _select_gt_prbs_fabric(self):
        """PRBS-31 on TX/RX and pulse RX checker reset (CMAC GT fabric, not DRP)."""
        cge = self._cge
        cge.select_gt_lane(self._lane)
        cge.RXPRBSSEL = 4
        cge.TXPRBSSEL = 4
        cge.RXDFELPMRESET = 1
        time.sleep(0.5)
        cge.RXDFELPMRESET = 0
        cge.RXPRBSCNTRESET = 1
        time.sleep(0.5)
        cge.RXPRBSCNTRESET = 0

    def get_ber(self, tx_init_power=11, tx_power=15, period=0.3, verbose=1):
        """BER from RX_PRBS_ERR_CNT delta over ``period`` (DRP error count, DRP TX swing)."""
        DATA_RATE = 25.78125e9  # per 25G serdes lane (100G CAUI-4)
        self._select_gt_prbs_fabric()
        self.select()
        try:
            self.set_txdiffctrl(tx_init_power)
            time.sleep(0.5)
            self.set_txdiffctrl(tx_power)

            cnt0 = self.get_prbs_error_ctr()
            time.sleep(period)
            cnt1 = self.get_prbs_error_ctr()
            cnt = (cnt1 - cnt0) & 0xFFFFFFFF
        finally:
            self.unselect()

        err = (float(cnt) * 16) / (period * DATA_RATE)
        err_max = (float(cnt) * 16 + 1) / (period * DATA_RATE)

        if verbose:
            print(f'{self!r} BER = {err:1.1e} ({cnt} errors, BER<{err_max:1.1e})')
        return err

    def get_eye_diagram(
            self,
            horiz_offset=list(range(-32, 32, 4)),
            vert_offset=list(range(-127, 127, 16)),
            max_scaler=12,
            ut_sign=0,
            prescale_step=6):
        """
        Return a (M x N) matrix of BER values for M horizontal and N vertical offsets.
        Horiz_offset : -32 to 32
        Vert offset: : -127 to 127
        """
        prescale_step = 4
        self.select()
        try:
            return self._eye_diagram_scan(
                horiz_offset, vert_offset, max_scaler, ut_sign, prescale_step)
        finally:
            self.unselect()

    def _eye_diagram_scan(
            self,
            horiz_offset,
            vert_offset,
            max_scaler,
            ut_sign,
            prescale_step):
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
        nh = len(horiz_offset)
        nv = len(vert_offset)

        for (iv, v_offset) in enumerate(vert_offset):
            ih = 0
            processed_ih = []
            dir = 1
            while True:
                h_offset = horiz_offset[ih]
                self._set_es_vert_offset(v_offset, ut_sign)
                self.ES_HORZ_OFFSET = h_offset & 0xFFF
                progress = (iv * nh + len(set(processed_ih))) / float(nh * nv - 1) * 100
                print(f'{progress:3.0f}% Vert offset {int(iv):2}/{int(nv - 1):2}= {int(v_offset):3}, '
                      f'Horiz offset {int(ih):3}/{int(nh - 1):3}= {int(h_offset):3}: ', end=' ')

                while True:
                    self.ES_PRESCALE = prescale
                    self.ES_CONTROL = 0
                    self.ES_CONTROL = 1
                    while (int(self.ES_CONTROL_STATUS) & 1) == 0:
                        time.sleep(0.2)
                    error_count = int(self.ES_ERROR_COUNT)
                    sample_count = int(self.ES_SAMPLE_COUNT)

                    print(f'(Prescale={int(prescale)} => {int(error_count)} err / {int(sample_count)} samples) ', end=' ')
                    if sample_count < 100:
                        if prescale == 0:
                            break
                        prescale = max(0, prescale - prescale_step)
                    elif error_count < 100:
                        if prescale == max_scaler:
                            break
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
                elif dir == 1 and ih == len(horiz_offset) - 1:
                    break

                if error_count == 0:
                    if dir == 1:
                        print('Swapping direction!')
                        old_ih = ih
                        dir = -1
                        ih = len(horiz_offset) - 1
                    else:
                        break
                else:
                    ih = ih + dir

        EyeDiag = collections.namedtuple('EyeDiag', ['gty', 'horiz_offsets', 'vert_offsets', 'ber_map'])
        return EyeDiag(gty=self, horiz_offsets=horiz_offset, vert_offsets=vert_offset, ber_map=ber)

    def plot_eye_diagram(self, eye_diag=None, **kwargs):
        if eye_diag is None:
            eye_diag = self.get_eye_diagram(**kwargs)
        extent = (
            min(eye_diag.horiz_offsets),
            max(eye_diag.horiz_offsets),
            min(eye_diag.vert_offsets),
            max(eye_diag.vert_offsets))
        plt.imshow(np.log10(eye_diag.ber_map + 1e-12), origin='lower', extent=extent, aspect=0.1, vmin=-12, vmax=1)
        plt.xlabel('Horizontal sampling offset')
        plt.ylabel('Vertical sampling offset')
        iceboard = eye_diag.gty.fpga
        plt.title(
            f'Eye diagram for IceBoard SN{iceboard.serial} '
            f'(Icecrate {iceboard.crate.__class__.__name__} SN{iceboard.crate.serial} '
            f'Slot {int(iceboard.slot)}) Lane {int(eye_diag.gty.instance_number + 1)}')


class CGE(MMI):
    """

    """

    # Map of loopback lanes for the 4-lane 100G link
    # This assumes we use a QSFP loopback dongle that loops each lane unto itself.
    LOOPBACK_LANE_MAP = {0:0, 1:1, 2:2, 3:3}

    CONTROL = BitField.CONTROL
    STATUS = BitField.STATUS

    # BSB RAM target (control byte 6). AXI port removed (cmac_usplus_1, ENABLE_AXI_INTERFACE=0).
    PORT_CORE_DRP = 1  # Core DRP port registers.
    PORT_GT_CMN_DRP = 2  # GT common (PLL) DRP port registers.
    PORT_GT_CH_DRP = 3  # GT channel DRP (muxed by GT_LANE_SEL).

    # 100GE Link control and status registers
    RESET_TX_DATAPATH = BitField(CONTROL, 0, 0, doc='When 1, the TX datapath is reset')
    RESET_RX_DATAPATH = BitField(CONTROL, 0, 1, doc='When 1, the RX datapath is reset.')
    CMAC_SYS_RESET = BitField(CONTROL, 0, 2, doc='When 1, The CMAC is reset.')
    CORE_TX_RESET = BitField(CONTROL, 0, 3, doc='When 1, The 100G Core logic is reset.')
    CTL_TX_RSFEC_ENABLE = BitField(CONTROL, 0, 4, doc='RS-FEC enable (fabric ctl_tx_rsfec_enable)')
    TEST_PACKET_ENABLE = BitField(CONTROL, 0, 7, doc='When 1, the test packet generator is enabled')
    TEST_PACKET_WORDS = BitField(CONTROL, 1, 0, width=8, doc='Number of 32-byte words in the UDP packets in addition to the 22 bytes payload header')
    TEST_PACKET_PERIOD = BitField(CONTROL, 3, 0, width=16, doc='Time between packets in  322MHz clocks periods')
    CAPTURE_BYTE_NUMBER = BitField(CONTROL, 5, 0, width=16, doc='Index of byte to capture')
    CONFIG_PORT_SEL = BitField(CONTROL, 6, 0, width=3, doc='BSB RAM target: 1=core DRP, 2=GT cmn, 3=GT ch DRP (lane from GT_LANE_SEL)')
    GT_LANE_SEL = BitField(CONTROL, 6, 4, width=2, doc='GT lane 0-3 for GT channel DRP and PRBS fabric (bytes 7-8)')

    TX_GT_LOCKED_LED = BitField(STATUS, 0, 0, doc='TX GT locked (from CMAC)')
    TX_OVFOUT_STICKY = BitField(STATUS, 0, 1, doc='TX overflow sticky')
    TX_UNFOUT_STICKY = BitField(STATUS, 0, 2, doc='TX underflow sticky')
    FIFO_OVERFLOW_STICKY = BitField(STATUS, 0, 3, doc='TX data FIFO overflow sticky')
    FLAG_FIFO_OVERFLOW_STICKY = BitField(STATUS, 0, 4, doc='TX flag FIFO overflow sticky')
    CAPTURE_TLAST = BitField(STATUS, 0, 5, doc='Captured tlast (reads 0)')
    RST_STATUS = BitField(STATUS, 0, 6, doc='BSB reset status')
    USR_TX_RESET = BitField(STATUS, 0, 7, doc='CMAC usr_tx_reset readback')
    IN_FRAME_CTR = BitField(STATUS, 1, 0, width=8, doc='Counts the number of frames coming in (test frames are counted when enabled).')
    OUT_FRAME_CTR = BitField(STATUS, 2, 0, width=8, doc='Counts the number of frames going out to the CMAC.')
    PACKET_LENGTH = BitField(STATUS, 4, 0, width=16, doc='length of incoming packets')
    CAPTURE_BYTE = BitField(STATUS, 5, 0, width=8, doc='Captured byte')
    ACTIVE_CONFIG_PORT = BitField(STATUS, 6, 0, width=3, doc='Active RAM target (readback of CONFIG_PORT_SEL)')
    ACTIVE_GT_LANE_SEL = BitField(STATUS, 6, 4, width=2, doc='Active GT lane (readback of GT_LANE_SEL)')
    # DATA_FIFO_OVERFLOW  = BitField(STATUS, 2+2, 0, width=8, doc='Indicates if the data FIFO has overflows on the last 8 GPU links. Bit 0 is for lane 0.')
    # FRAME_FIFO_OVERFLOW = BitField(STATUS, 2+3, 0, width=8, doc='Indicates if the frame header FIFO has overflows on the last 8 GPU links. Bit 0 is for lane 0.')

    # CMAC GT fabric PRBS / reset (control bytes 7-8, muxed by GT_LANE_SEL). TX swing: DRP on CGEChannel.
    RXDFELPMRESET = BitField(CONTROL, 7, 7, doc='RX DFE/LPM reset (muxed lane)')
    RXPRBSCNTRESET = BitField(CONTROL, 7, 6, doc='RX PRBS counter reset (muxed lane)')
    TXPRBSSEL = BitField(CONTROL, 7, 3, width=3, doc='TX PRBS pattern select (muxed lane)')
    RXPRBSSEL = BitField(CONTROL, 7, 0, width=3, doc='RX PRBS pattern select (muxed lane)')

    # Add DRP registers here...

    def __init__(self, *, router, router_port, config, verbose=1):
        self.logger = logging.getLogger(__name__)
        self.verbose = verbose
        super().__init__(router=router, router_port=router_port)
        self.config = config
        self.core = CGECore(self, self.PORT_CORE_DRP)
        self.gt_common = CGECommon(self, self.PORT_GT_CMN_DRP)
        # Per-lane GT access (same API as xxvglink array GTX instances)
        self.gtx = [CGEChannel(self, self.PORT_GT_CH_DRP, lane) for lane in range(4)]

    def init(self):
        self.TEST_PACKET_ENABLE = 0
        self.CTL_TX_RSFEC_ENABLE = 1

    def select_gt_lane(self, lane):
        """Select GT lane for PRBS fabric (bytes 7-8) and GT channel DRP."""
        self.GT_LANE_SEL = lane

    def select_core_drp(self):
        """Select CMAC core DRP port."""
        self.CONFIG_PORT_SEL = self.PORT_CORE_DRP

    def select_gt_cmn_drp(self):
        """Select GT common (QPLL) DRP port."""
        self.CONFIG_PORT_SEL = self.PORT_GT_CMN_DRP

    def select_gt_ch_drp(self, lane):
        """Select GT channel DRP port and lane (CONFIG_PORT_SEL + GT_LANE_SEL)."""
        self.CONFIG_PORT_SEL = self.PORT_GT_CH_DRP
        self.GT_LANE_SEL = lane

    def set_fec(self, state):
        """Enable or disable IEEE 802.3bj RS-FEC on TX (and RX where applicable).

        Uses fabric ``ctl_tx_rsfec_enable`` (no AXI on cmac_usplus_1). Pulse CMAC
        datapath resets after a FEC mode change if link does not recover.

        Parameters:
            state (bool): True to enable RS-FEC, False to disable
        """
        self.CTL_TX_RSFEC_ENABLE = int(bool(state))

    def get_eye_diagram(self, lane=0, **kwargs):
        """Eye diagram on GT lane ``lane`` (delegates to :class:`CGEChannel`)."""
        return self.gtx[lane].get_eye_diagram(**kwargs)

    def plot_eye_diagram(self, lane=0, **kwargs):
        return self.gtx[lane].plot_eye_diagram(**kwargs)

    def capture_bytes(self, N=64, fmt='hex'):
        """ Captures N bytes
        """
        b = bytearray(N)
        for i in range(N):
            self.CAPTURE_BYTE_NUMBER = i
            b[i] = self.CAPTURE_BYTE

        if fmt == 'hex':
            print('\n'.join(f'{b[i: i+16].hex()} {b[i+16: i+32].hex()}' for i in range(0, len(b), 32)))
            return None

        return b

    def set_enable(self, enable):
        """
        Enable link.
        """
        self.logger.info(f'{self!r}: set_enable()  is not implemented on {__name__}. Command is ignored. ')

    # def reset(self):
    #     """ Resets the UDP/MAC stack, the SGMII interface and the GTX """
    #     self.RESET = 1
    #     self.RESET = 0

    def get_lane_numbers(self):
        """ Returns a list of logical lane numbers for the GPU links.

        Four serdes lanes (CAUI-4). Use ``gtx[lane]`` for per-lane GT/BER/eye access.

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

    # def status(self):
    #     """ Displays the status of the GPU GTX hardware"""
    #     # for gtx in self.GTX_COMMON:
    #     #     gtx.status()
