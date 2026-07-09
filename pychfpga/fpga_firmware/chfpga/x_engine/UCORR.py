"""
CORR.py module
 Implements interface to the correlator blocks

 History:
 2017-05-04 : JFC : Created
"""
import time
import logging
import warnings

import numpy as np
import matplotlib.pyplot as plt

from pychfpga.common.state import State
from pychfpga.common.ucorr_frame_receiver import UCorrFrameReceiver
from pychfpga.common.udp_packet_receiver import UDPPacketReceiver

from ..mmi import MMI, BitField, CONTROL, STATUS

class UCORR(MMI):
    """ Implements interface the UCORR44_ARRAY correlator array"""

    ADDRESS_WIDTH = 16

    REQUIRES_OFFSET_BINARY_ENCODING = False
    BIT_WIDTH = 4 # (4+4) bit correlator

    # Control registers
    SOFT_RESET         = BitField(CONTROL, 0x00, 7, doc="Resets the correlator array.")
    OVERRUN_RESET      = BitField(CONTROL, 0x00, 6, doc="Resets the overrun flag")
    CORR_ID            = BitField(CONTROL, 0x00, 2, width=4, doc="Correlator ID, placed in th e MSB of the stream ID")
    NO_ACCUM           = BitField(CONTROL, 0x00, 1, doc="Don't accumulate values - only last product of period is kept")
    AUTOCORR_ONLY      = BitField(CONTROL, 0x00, 0, doc="Force the correlator to output autocorrelations only")
    # NO_ACCUM           = BitField(CONTROL, 0x00, 5, doc="Disables accumulation - only the last result is saved")
    # USER_ID            = BitField(CONTROL, 0x00, 0, width=4, doc="USER ID used in the correlator packet header")
    INTEGRATION_PERIOD = BitField(CONTROL, 0x02, 0, width=16, doc="Duration of te integration period minus one")
    # BINS_PER_FRAME_OLD = BitField(CONTROL, 0x05, 0, width=7, doc="Number of frequency bins per frame minus one")
    # BINS_PER_FRAME     = BitField(CONTROL, 0x06, 0, width=9, doc="Number of frequency bins per frame minus one")
    OFFSET_ENCODING      = BitField(CONTROL, 0x03, 7, doc=" Set to 1 if incoming data is offset binary encoded")
    AUTO_BIN_SET      = BitField(CONTROL, 0x03, 6, doc="Enables automatic bin set increments, i.e. each integration will have a different bin set.")
    BIN_SET = BitField(CONTROL, 0x03, 0, width=4, doc="Bin set to be sent what AUTO_BIN_SET=0")

    # Status registers
    OVERRUN   = BitField(STATUS, 0, 0, doc="An overrun has occured in one of the correlator cores")
    RST_STATUS   = BitField(STATUS, 0, 1, doc="reset line status")
    NCHAN = BitField(STATUS, 1, 0, width=8, doc="NUmber of input channels to correlate")
    NCORR   = BitField(STATUS, 2, 4, width=4, doc="Number of correlator cores")
    BIN_DECIMATION_FACTOR   = BitField(STATUS, 2, 0, width=4, doc="Bin decimation factor")
    IN_FRAME_CTR  = BitField(STATUS, 3, 0, width=8, doc="Input frame counter")
    OUT_FRAME_CTR = BitField(STATUS, 4, 0, width=8, doc="Output frame counter")
    NCLK_PER_BIN  = BitField(STATUS, 5, 0, width=4, doc="Number of clocks required to send all channels for one bin")


    def __init__(self, *, router, router_port, state, verbose=0):
        super().__init__(router=router, router_port=router_port)
        self.config = state
        self.verbose = verbose
        self.logger = logging.getLogger(__name__)
        self.config.update(
            corr_config_id=0,
            bin_map=None,
            prod_map=None,
            last_fft_config_id=None,
            soft_integ_period=1,
        )

    def init(self):
        """ Inisializes all modules of a correlator block."""
        self.CORR_ID = (self.fpga.slot-1) if self.fpga.slot else 0
        self.config.update(
            bins_per_frame=self.fpga.FRAME_LENGTH // 2, # total number of bins at the output of the channelizers
            NCHAN=int(self.NCHAN),
            NCLK=int(self.BIN_DECIMATION_FACTOR * self.NCLK_PER_BIN),  # Number of clocks available to compute all the products. Will reduce the number of required CMACs, but increase the number of products per CMAC
            NDECIM=int(self.BIN_DECIMATION_FACTOR),
            CT_LEVEL=int(self.fpga.CT.CT_LEVEL),
            firmware_integ_period=int(self.INTEGRATION_PERIOD + 1),
        )
        self.config.NPROD = self.config.NCHAN * (self.config.NCHAN + 1) // 2 # total number of computed products

    def status(self):
        """Displays the status of the correlator array"""
        print(f'======= CORR.core[{int(self.instance_number)}] =============')
        print(f' Don\'t accumulate: {self.NO_ACCUM}')
        print(f' Autocorrelation only: {self.AUTOCORR_ONLY}')
        print(f' Firmware integration period: {self.INTEGRATION_PERIOD} frames')
        print(f' Overrun in one of the cores: {self.OVERRUN}')


    def get_config(self):
        """Return the correlator config dict. Deprecated: use ``config`` directly."""
        warnings.warn(
            "UCORR.get_config() is deprecated; use UCORR.config directly.",
            DeprecationWarning,
            stacklevel=2,
        )
        self.get_bin_map()
        return self.config.as_dict()

    def get_input_bin_map(self):
        return self.fpga.chan[0].FFT.get_bin_map()

    def get_bin_map(self):
        fft = self.fpga.chan[0].FFT
        if (
            fft.config_id == self.config.last_fft_config_id
            and self.config.bin_map is not None
            and self.config.prod_map is not None
        ):
            return self.config.bin_map, self.config.prod_map

        self.config.update(
            CT_LEVEL=int(self.fpga.CT.CT_LEVEL),
            NDECIM=int(self.config.NDECIM),
            NCHAN=int(self.config.NCHAN),
            NCLK=int(self.config.NCLK),
            firmware_integ_period=int(self.INTEGRATION_PERIOD + 1),
            fft_bin_map=fft.get_bin_map(),
        )
        self.update_corr_maps(self.config)
        self.config.last_fft_config_id = fft.config_id
        return self.config.bin_map, self.config.prod_map

    @classmethod
    def update_corr_maps(cls, config):
        """ Update the correlator bin and product maps and derived geometry parameters based on the new configuration.

        If the following parameters are changed:
        - CT_LEVEL: CT level
        - NDECIM: number of decimation bins
        - NCHAN: number of channels
        - NCLK: number of clocks
        - fft_bin_map: FFT bin map

        The following parameters are then updated:  
        - bin_map: (bin_set, corr, clkbin, prod) = bin
        - prod_map: (corr, clkbin, prod) = pix
        - NDECIM: number of decimation bins
        - NCORRS: number of correlators
        - NPROD_PER_CMAC: number of products per CMAC
        - NCMAC: number of CMACs
        - NBINS: number of bins
        - NPROD_PER_ARRAY: number of products per array

        Parameters:
            config (dict or State): Correlator configuration
        Returns:
            None
        """
        map_attrs = ('CT_LEVEL', 'NDECIM', 'NCHAN', 'NCLK', 'fft_bin_map')
        if not config.has_changed(map_attrs, context=map_attrs):
            return
        logger = logging.getLogger(__name__)

        # Extract relevant configuration parameters for convenience
        ct_level = config.CT_LEVEL
        NDECIM = config.NDECIM
        NCHAN = config.NCHAN
        NCLK = config.NCLK

        # Derive useful parameters
        NPROD = NCHAN * (NCHAN + 1) // 2
        NPROD_PER_BINCLK =  NPROD // NCLK # number of products for each binclk, i.e number of products in each packet
        logger.debug(
            "NCHAN=%s, NCLK=%s, NPROD_PER_BINCLK=%s, NPROD=%s",
            NCHAN, NCLK, NPROD_PER_BINCLK, NPROD)


        # Map to convert a (i,j) coordinate in a NCHANxNCHAN matrix to a common-sense linearized product index (pix)
        ij_to_pix_map = cls.get_ij_to_prod_map(NCHAN)

        # Get the (iv,jv) coordinate of each product generated by each correlator CMAC.
        # cmac_out_ij[clk, prod] = (iix, jix), where
        #    clk: 0..NCLK-1: each of the NCLK used to produce products for one bin
        #    prod: 0 .. NPROD/NCLK: Product produced by the CMAC on each clock
        #    iv, jv = position of channel in the CMAC input vector (which may not necessarily correspond to the channel number in that position)
        cmac_out_ij = np.array([[(i, (j+i) % NCHAN) if i < NCHAN-j else ((j+i) % NCHAN, i-1)
                               for i in range(NCHAN+1) for j in range(NCHAN//2//NCLK*clk, NCHAN//2//NCLK*(clk+1)) ]
                               for clk in range(NCLK) ])


        fft_bin_map = config.fft_bin_map
        if not isinstance(fft_bin_map, np.ndarray):
            fft_bin_map = np.asarray(fft_bin_map)
            config.fft_bin_map = fft_bin_map

        if not ct_level:
            raise RuntimeError(f'Incalid CT_LEVEL with value {ct_level}')


        # Get CT Level 1 bin map
        if ct_level >= 1:
            # In CT_LEVEL=1, there are 4 parallel bin streams coming from the FFT. We need to process all of them locally, so we need 4 parallel correlators on a single board.
            # ct_bin_map[corr, bix] = bin
            ct_bin_map = fft_bin_map
            chan_map = np.arange(NCHAN)

        # Get CT Level 2 bin map
        if ct_level >= 2:
            # In CT_LEVEL=2, we split the FFT bin stream across 4 boards.  Each board has one stream and therefore one correlator.

            # Cull bins
            # The 2nd CT can transfer 25 Gbps/lane, but our data has 25.6 Gbps/lane.
            # We need to drop some bins in order to fit the pipe. Here we remove 256 bins total, i.e. 64 bins per lane.
            # The culling takes into account the bin rotation and remove only the first 256 bins (first 50 MHz) of the spectrum
            # In fact, the bin rotation is essential to ensure the culling affects all lanes equally.
            # ---------
            # ct2_bins: (lane, binid) = bin, ct2_bins.shape = (4,1984), 1984=2048-256/4
            ct_bin_map = np.array([[b for clk,b in enumerate(lane_bins) if not (clk < 256 and clk & 0b11 == lane)] for lane, lane_bins in enumerate(ct_bin_map)])
            chan_map = np.arange(NCHAN)
            logger.debug('CT2 bin_map=%s', ct_bin_map.shape)

        # Get CT Level 3 bin map
        if ct_level >= 3:
            # In CT_LEVEL=3, we further split the CT_LEVEL=2 bin stream between two boards in two backplanes. Even and odd bin indices alternatively go to local and remote board.
            # Channels are sent in two clocks, 16 at a time, on a 50 Gbps logical link (made of two 25 Gbps physical links).
            # We need one correlator per board, and the correlator needs to assemble two consecutive words to build the CMAC input vector of 64 channels.

            s = ct_bin_map.shape
            ct_bin_map = ct_bin_map.reshape(s[0], 2, -1, order='F').transpose((1,0,2)).reshape(s[0]*2, -1, order='C')

            # Compute the channel map that describe the real channel numbers present in each position of the CMAC input vector
            # Level-3 CT Sends the Level-2 32 channels to its destination slot in two clocks, 16 channels at a time, using 2 links.
            # The receiver receives two 16-chan words, one local, one remote. The firmware swaps them based on the slot_group such that
            # low chanels (from slot group 0) are always before high channels (from slot group 1). We therefore receive
            #  clock 0 = (low half of low channels + low half of high channels)  = CH0-15 + CH32-47
            #  clock 1 = (high half of low channels + high half of high channels)  = CH16-31 + CH48-63
            chan_map = np.array([i+NCHAN//4*clk + NCHAN//2*lane for clk in range(2) for lane in range(2) for i in range(NCHAN//4)], dtype=np.intp)
            # ct_bin_map = ct_bin_map[[0,4,1,5,2,6,3,7]] # take into account the cabling
            logger.debug('CT3 bin_map=%s', ct_bin_map.shape)


        # Compute a map that lists the bins seen by each CMAC after decimation
        # corr_bins[bin_set, corr, bix] = bin
        corr_bins = np.array([ct_bin_map[:, bs::NDECIM] for bs in range(NDECIM)])
        logger.debug('Post Corr decimation corr_bins=%s, NDECIM=%s', corr_bins.shape, NDECIM)




        # print(f'{NCLK=}\n{ct2_bins=}\n{fft_bin_map=}\n{cmac_out_ij=}\n{cmac_out_pix=}')

        # Compute map that lists the bins corresponding to every product of every packet sent by each correlator for a specific bin_set
        # bin_map[bin_set, corr, clkbin, prod] = bin
        # Note: bin is the save for every prod and for every NCLK clkbins.
        bin_map = np.array([[[[ bin_ for prod in range(NPROD_PER_BINCLK)] for bin_ in bins for clk in range(NCLK)] for bins in bin_set_map] for bin_set_map in corr_bins])

        # Compute the linearized product index corresponding to every product of every packet sent by each correlator.
        # We take into account the real channel numbers provided into the CMAC input vector position.
        # cmac_out_pix[clk, prod] = pix
        logger.debug('cmac_out_ij.shape=%s, chan_map=%r', cmac_out_ij.shape, chan_map)
        cmac_out_pix = ij_to_pix_map[chan_map[cmac_out_ij[...,0]], chan_map[cmac_out_ij[...,1]]]

        # Compute the linearized product index for each (corr, clkbin, prod) data sent by the correlator
        #   prod_map[corr, clkbin, prod] where:
        #     corr: 0 .. NCORR-1: correlator number
        #     clkbin: 0 .. NBINS_PER_CORR * NCLK, each product stored in ram = number of packets
        #     prod: 0 .. NPROD/NCLK, each product in a packet
        prod_map = np.array([[[cmac_out_pix[clk, clkprod] for clkprod in range(NPROD_PER_BINCLK)] for _ in lane_bins for clk in range(NCLK)] for lane_bins in corr_bins[0]])

        logger.debug('bin_map=%s, prod_map=%s', bin_map.shape, prod_map.shape)
        config.bin_map = bin_map
        config.prod_map = prod_map
        config.NDECIM, config.NCORRS, config.NPROD_PER_CMAC, config.NCMAC = bin_map.shape
        config.NBINS = int(bin_map.max()) + 1
        config.NPROD_PER_ARRAY = config.NCORRS * config.NPROD_PER_CMAC


    @classmethod
    def get_ij_to_prod_map(cls, NCHAN):
        """ Return an array that maps a (i,j) pair to a product index 
        Parameters:
            NCHAN (int): Number of channels
        Returns:
            np.ndarray: Array that maps a (i,j) pair to a product index
        """
        z = np.empty((NCHAN, NCHAN), dtype=np.intp)
        i, j = np.triu_indices(NCHAN)
        z[i, j] = np.arange(len(i))
        z[j, i] = np.arange(len(i))
        return z


    def select(self):
        """ Configures UCAP to stream the correlator data instead of raw data.
        """
        self.fpga.UCAP.OUTPUT_SOURCE_SEL = 1

    def start_correlator(self,
                         integration_period,
                         autocorr_only=False,
                         no_accum=False,
                         correlators=None,
                         bandwidth_limit=None,
                         bin_set=None,
                         verbose=0
                         ):
        """ Configure and starts the correlator.

        Parameters:

            integration_period (int): Firmware integration period in frames (max 65536)

            autocorr_only (bool): If True, only the autocorrelation products are sent. This can be
                used to reduce network bandwidth.

            no_accum (bool): If True, data is not accumulated; only the last product is stored. This
                can be useful to reduce integration overflows when using deterministic test signals.

            correlators: Not used

            bandwidth_limit: Not used

            bin_set (int or None): When the correlator decimates bins, select which bin of each decimation group
                is being sent. If `None`, the bin set is rotated automatically in each successive
                firmware integration period.

            verbose (int): Sets the verbosity level

        """
        self.logger.info(f' Starting correlator')
        self.INTEGRATION_PERIOD = integration_period-1
        self.AUTOCORR_ONLY = autocorr_only
        self.NO_ACCUM = no_accum
        if bin_set is None:
            self.AUTO_BIN_SET = 1
            active_bin_set = None
        else:
            self.AUTO_BIN_SET = 1
            self.BIN_SET = bin_set
            active_bin_set = int(bin_set)

        if correlators is not None:
            raise RuntimeError("'correlators' argument is not supported by UCORR")
        if bandwidth_limit is not None:
            self.logger.warn(f"{self!r}: 'bandwidth_limit' argument is not yet supported by UCORR")
        self.verbose = verbose
        self.config.update(
            firmware_integ_period=int(integration_period),
            autocorr_only=bool(autocorr_only),
            no_accum=bool(no_accum),
            auto_bin_set=True,
            bin_set=active_bin_set,
            verbose=verbose,
            corr_config_id=int(self.config.corr_config_id) + 1,  # indicate that some parameters (i.e. integration period) might have changed
        )

    def get_data_receiver(self, sock):
        packet_receiver = UDPPacketReceiver(sock)
        self.get_bin_map()
        return UCorrFrameReceiver(packet_receiver, config=self.config)


    @classmethod
    def get_corr_receiver(cls, packet_receiver, config=None, pre_func=None):
        """Return an instance of a correlator packet receiver."""
        return UCorrFrameReceiver(packet_receiver, config=config, pre_func=pre_func)

    @classmethod
    def get_packet_receiver(cls, sock, n_packets=2048, max_packet_size=9000, verbose=0):
        """ Return an instance of a UDP packet receiver
        """
        return UDPPacketReceiver(sock, n_packets=n_packets, max_packet_size=max_packet_size, verbose=verbose)
