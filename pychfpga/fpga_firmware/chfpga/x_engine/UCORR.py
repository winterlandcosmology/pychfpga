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

    _MAP_CACHE = {}
    _IJ_MAP_CACHE = {}

    @classmethod
    def clear_map_cache(cls):
        cls._MAP_CACHE.clear()

    def __init__(self, *, router, router_port, config, verbose=0):
        super().__init__(router=router, router_port=router_port)
        self.config = config
        self.verbose = verbose
        self.logger = logging.getLogger(__name__)

    def init(self):
        """ Inisializes all modules of a correlator block."""
        self.CORR_ID = (self.fpga.slot-1) if self.fpga.slot else 0
        self.config.update(
            bins_per_frame=self.fpga.FRAME_LENGTH // 2, # total number of bins at the output of the channelizers
            NCHAN=int(self.NCHAN),
            NPROD = self.NCHAN * (self.NCHAN + 1) // 2, # total number of computed products
            NCLK=int(self.BIN_DECIMATION_FACTOR * self.NCLK_PER_BIN),  # Number of clocks available to compute all the products. Will reduce the number of required CMACs, but increase the number of products per CMAC
            NDECIM=int(self.BIN_DECIMATION_FACTOR),
            CT_LEVEL=int(self.fpga.CT.CT_LEVEL),
            READOUT_BINS=list(self.fpga.CORR_READOUT_BINS),  # per-correlator clkbin stolen for readout
            firmware_integ_period=int(self.INTEGRATION_PERIOD + 1),
            capture_engine="UCORR",
            sample_freq_mhz=float(self.fpga.config.sample_freq_mhz),
            fft_bin_map=None,
            bin_map=None,
            prod_map=None,
            conj_map=None,
            autocorr_only=None
            )

    def status(self):
        """Displays the status of the correlator array"""
        print(f'======= CORR.core[{int(self.instance_number)}] =============')
        print(f' Don\'t accumulate: {self.NO_ACCUM}')
        print(f' Autocorrelation only: {self.AUTOCORR_ONLY}')
        print(f' Firmware integration period: {self.INTEGRATION_PERIOD} frames')
        print(f' Overrun in one of the cores: {self.OVERRUN}')

    def set_offset_binary_encoding(self, offset):
        """Match scaler/crossbar offset-binary mode on the UCORR input path."""
        enabled = bool(offset)
        self.OFFSET_ENCODING = enabled
        self.config.update(offset_binary_encoding=enabled)

    def update_config(self):
        """Refresh correlator maps if needed and return ``self.config``.

        Build ``bin_map`` / ``prod_map`` onto ``config`` from primary parameters.

        Maps produced here (and exposed via :meth:`update_config`) are authoritative
        for packet processing; receivers should transmit ``bin_map`` /
        ``prod_map`` rather than recomputing them.

        Identical ``(CT_LEVEL, NDECIM, NCHAN, NCLK, fft_bin_map)`` inputs share one
        class-level cache entry so every board in an array reuses the same arrays
        instead of rebuilding a ~13 M-element map.

        If the following primary parameters are changed:
        - CT_LEVEL: CT level
        - NDECIM: number of decimation bins
        - NCHAN: number of channels
        - NCLK: number of clocks
        - fft_bin_map: FFT bin map
        - autocorr_only: when set, the last map axis is the diagonal CMACs only

        The following parameters are then updated:
        - bin_map: (bin_set, corr, clkbin, prod) = bin
        - prod_map: (corr, clkbin, prod) = pix
        - conj_map: (corr, clkbin, prod) = True where the firmware product is conj(V_ij)
        - NDECIM: number of decimation bins
        - NCORRS: number of correlators
        - NPROD_PER_CMAC: number of products per CMAC
        - NCMAC: number of CMACs
        - NBINS: number of bins
        - NPROD_PER_ARRAY: number of products per array


        """
        config = self.config
        fft = self.fpga.chan[0].FFT
        ctx = ('update_config', self)
        

        fft.update_config()  # make sure the FFT bin map is updated
        if fft.config.has_changed('bin_map', context=ctx):
            config.update(fft_bin_map=fft.config.bin_map, fft_bin_ix_map=fft.config.bin_ix_map)
            fft.config.ack('bin_map', context=ctx)


        fft_bin_map = config.fft_bin_map
        if not isinstance(fft_bin_map, np.ndarray):
            fft_bin_map = np.asarray(fft_bin_map)
            config.update(fft_bin_map=fft_bin_map)

        map_attrs = ('CT_LEVEL', 'NDECIM', 'NCHAN', 'NCLK', 'fft_bin_map', 'autocorr_only')
        if config.has_changed(map_attrs, context=ctx):
            # Cache always holds the full triangle. autocorr_only slices a copy
            # so toggling the flag restores the cached maps rather than the slice.
            map_params = self.compute_bin_prod_maps(config.CT_LEVEL, config.NDECIM, config.NCHAN, config.NCLK, config.fft_bin_map, config.autocorr_only)
            config.update(**map_params)
            config.ack(map_attrs, context=ctx)

        return self.config

    @classmethod
    def compute_bin_prod_maps(cls, ct_level, NDECIM, NCHAN, NCLK, fft_bin_map, autocorr_only=False):
        """Return derived correlator maps for one set of primary parameters."""

        # Key the cache on the bin map's *content*, not on id(fft_bin_map). id() is the object's memory address,
        # and Python reuses the address of a freed array for new arrays, so an id() key can return the maps that
        # were computed for a different, earlier array. That shows up intermittently as one board's bin_map
        # differing from the others ("Attribute 'bin_map' differs across states" in get_corr_data_config), or
        # silently as products filed under the wrong bins. Keying on the bytes makes equal maps share an entry
        # and different maps never collide.
        fbm = np.ascontiguousarray(fft_bin_map, dtype=np.int64)
        cache_key = (ct_level, NDECIM, NCHAN, NCLK, fbm.shape, fbm.tobytes())
        if cache_key not in cls._MAP_CACHE:
 
            NPROD = NCHAN * (NCHAN + 1) // 2
            NPROD_PER_BINCLK = NPROD // NCLK  # products per binclk, i.e. products in each packet

            # Map to convert a (i,j) coordinate in a NCHANxNCHAN matrix to a common-sense linearized product index (pix)
            ij_to_prod_map = cls.get_ij_to_prod_map(NCHAN)

            # Get the (iv,jv) coordinate of each product generated by each correlator CMAC.
            # cmac_out_ij[clk, prod] = (iix, jix), where
            #    clk: 0..NCLK-1: each of the NCLK used to produce products for one bin
            #    prod: 0 .. NPROD/NCLK: Product produced by the CMAC on each clock
            #    iv, jv = position of channel in the CMAC input vector (which may not necessarily correspond to the channel number in that position)
            cmac_out_ij = np.array([[(i, (j+i) % NCHAN) if i < NCHAN-j else ((j+i) % NCHAN, i-1)
                                for i in range(NCHAN+1) for j in range(NCHAN//2//NCLK*clk, NCHAN//2//NCLK*(clk+1)) ]
                                for clk in range(NCLK) ])

            if not ct_level:
                raise RuntimeError(f'Invalid CT_LEVEL with value {ct_level}')

            # Get CT Level 1 bin map
            if ct_level >= 1:
                # In CT_LEVEL=1, there are 4 parallel bin streams coming from the FFT. We need to process all of them locally, so we need 4 parallel correlators on a single board.
                # ct_bin_map[corr, bix] = bin
                ct_bin_map = fbm
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
                # Non-vectorized version: ct_bin_map = np.array([[b for clk,b in enumerate(lane_bins) if not (clk < 256 and clk & 0b11 == lane)] for lane, lane_bins in enumerate(ct_bin_map)])
                # Vectorized version:
                n_lanes, n_clk = ct_bin_map.shape
                clk = np.arange(n_clk)
                lane = np.arange(n_lanes)[:, None]
                keep = ~((clk < 256) & ((clk & 0b11) == lane))  # (n_lanes, n_clk) array of booleans, because of 'lane' broadcasting with 'clk'
                n_keep = int(keep.sum(axis=1)[0]) # number of bins kept per lane
                if set(keep.sum(axis=1)) != set((n_keep,)):
                    raise RuntimeError('CT2 culling did not drop the same number of bins on every lane')
                ct_bin_map = ct_bin_map[keep].reshape(n_lanes, n_keep)  # ct_bin_map[kep] creates a 1D array (C-order) that selects the bins, then reshape(n_lanes, n_keep) reshapes it back to the original shape, minus the unkept bins
                chan_map = np.arange(NCHAN)

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

            # Compute a map that lists the bins seen by each CMAC after decimation
            # corr_bins[bin_set, corr, bix] = bin
            # non-vectorized version: corr_bins = np.array([ct_bin_map[:, bs::NDECIM] for bs in range(NDECIM)])
            n_ct_bins = ct_bin_map.shape[1]
            corr_bins = np.stack([ct_bin_map[:, bs::NDECIM] for bs in range(NDECIM)])  # (NDECIM, NCORRS, n_ct_bins/NDECIM)

            n_decim, n_corrs, n_bins = corr_bins.shape


            # Compute map that lists the bins corresponding to every product of every packet sent by each correlator for a specific bin_set
            # bin_map[bin_set, corr, clkbin, prod] = bin
            # non-vectorized version: bin_map = np.array([[[[ bin_ for prod in range(NPROD_PER_BINCLK)] for bin_ in bins for clk in range(NCLK)] for bins in bin_set_map] for bin_set_map in corr_bins])
            # Note: bin is the same for every prod and for every NCLK clkbins.
            bin_map = np.repeat(corr_bins[..., None], NCLK, axis=-1)
            bin_map = bin_map.reshape(n_decim, n_corrs, n_bins * NCLK)
            bin_map = np.ascontiguousarray(
                np.repeat(bin_map[..., None], NPROD_PER_BINCLK, axis=-1),
                dtype=np.intp,
            )

            # Compute the linearized product index corresponding to every product of every packet sent by each correlator.
            # We take into account the real channel numbers provided into the CMAC input vector position.
            # cmac_out_pix[clk, prod] = pix
            # print(f'{cmac_out_ij.shape=}, {chan_map=}')
            cmac_out_pix = ij_to_prod_map[chan_map[cmac_out_ij[...,0]], chan_map[cmac_out_ij[...,1]]]
            # The CMAC computes X[iv] * conj(X[jv]) with iv < jv in *vector position* (UCORR44.vhd xy_swap keeps
            # it upper-triangular). ij_to_prod_map is symmetric, so when chan_map reverses the channel order of a
            # pair (CT3: channels 16-31 x 32-47) the firmware product is conj of the convention V_ij = X_i conj(X_j),
            # i < j. cmac_out_conj[clk, prod] flags those products; the packet processor un-conjugates them.
            cmac_out_conj = chan_map[cmac_out_ij[...,0]] > chan_map[cmac_out_ij[...,1]]

            # Compute the linearized product index for each (corr, clkbin, prod) data sent by the correlator
            #   prod_map[corr, clkbin, prod] where:
            #     corr: 0 .. NCORR-1: correlator number
            #     clkbin: 0 .. NBINS_PER_CORR * NCLK, each product stored in ram = number of packets
            #     prod: 0 .. NPROD/NCLK, each product in a packet
            # non-vectorized version: prod_map = np.array([[[cmac_out_pix[clk, clkprod] for clkprod in range(NPROD_PER_BINCLK)] for _ in lane_bins for clk in range(NCLK)] for lane_bins in corr_bins[0]])        # prod_map[corr, clkbin, prod] = cmac_out_pix[clk, prod] (independent of bin)
            prod_map = np.ascontiguousarray(
                np.broadcast_to(
                    cmac_out_pix[None, None, :, :],
                    (n_corrs, n_bins, NCLK, NPROD_PER_BINCLK),
                ).reshape(n_corrs, n_bins * NCLK, NPROD_PER_BINCLK),
                dtype=np.intp,
            )

            # conj_map[corr, clkbin, prod]: True where the firmware product is conj(V_ij) (same layout as prod_map)
            conj_map = np.ascontiguousarray(
                np.broadcast_to(
                    cmac_out_conj[None, None, :, :],
                    (n_corrs, n_bins, NCLK, NPROD_PER_BINCLK),
                ).reshape(n_corrs, n_bins * NCLK, NPROD_PER_BINCLK),
                dtype=bool,
            )

            ndecim, ncorrs, nprod_per_cmac, ncmac = bin_map.shape


            maps = dict(
                ij_to_prod_map=ij_to_prod_map,
                bin_map=bin_map,
                prod_map=prod_map,
                conj_map=conj_map,
                NDECIM=ndecim,
                NCORRS=ncorrs,
                NPROD_PER_CMAC=nprod_per_cmac,
                NCMAC=ncmac,
                NBINS=int(bin_map.max()) + 1,
                NPROD_PER_ARRAY=ncorrs * nprod_per_cmac,
            )
            cls._MAP_CACHE[cache_key] = maps

        else: # If we have a cached value, retreive it
            maps = cls._MAP_CACHE[cache_key]

        # Adjust the maps if autocorrelation-only is enabled
        if autocorr_only:
            maps = maps.copy() # we don't want to modify the cached maps
            ac_ix = np.arange(NCHAN) * (NCHAN // 2 // NCLK)
            maps['bin_map'] = np.ascontiguousarray(maps['bin_map'][..., ac_ix])
            maps['prod_map'] = np.ascontiguousarray(maps['prod_map'][..., ac_ix])
            maps['conj_map'] = np.ascontiguousarray(maps['conj_map'][..., ac_ix])
            maps['NCMAC'] = NCHAN

        return maps

    @classmethod
    def get_ij_to_prod_map(cls, NCHAN):
        """ Return an array that maps a (i,j) pair to a product index 
        Parameters:
            NCHAN (int): Number of channels
        Returns:
            np.ndarray: Array that maps a (i,j) pair to a product index
        """
        cached = cls._IJ_MAP_CACHE.get(NCHAN)
        if cached is not None:
            return cached
        z = np.empty((NCHAN, NCHAN), dtype=np.intp)
        i, j = np.triu_indices(NCHAN)
        z[i, j] = np.arange(len(i))
        z[j, i] = np.arange(len(i))
        cls._IJ_MAP_CACHE[NCHAN] = z
        return z


    def select(self):
        """No-op: UCAP and CORR are merged; both always stream."""
        pass

    def start_correlator(self,
                         integration_period,
                         autocorr_only=False,
                         no_accum=False,
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

            bin_set (int or None): When the correlator decimates bins, select which bin of each decimation group
                is being sent. If `None`, the bin set is rotated automatically in each successive
                firmware integration period.

            verbose (int): Sets the verbosity level

        """
        self.verbose = verbose

        # Match CORR.start_correlator: hold the array in reset while programming,
        # then release both the GPIO CORR_RESET and the UCORR soft reset so the
        # pipeline can emit packets. init() leaves GPIO CORR_RESET asserted;
        # stop_correlator leaves SOFT_RESET asserted.
        self.fpga.set_corr_reset(1)
        self.SOFT_RESET = 1
        self.INTEGRATION_PERIOD = integration_period-1
        self.AUTOCORR_ONLY = autocorr_only
        self.NO_ACCUM = no_accum
        # Sticky in FPGA (and copied into every packet FLAGS LSB) until this pulse.
        self.pulse_bit("OVERRUN_RESET")
        if bin_set is None:
            self.AUTO_BIN_SET = 1
            active_bin_set = None
        else:
            self.AUTO_BIN_SET = 1
            self.BIN_SET = bin_set
            active_bin_set = int(bin_set)

        self.config.update(
            firmware_integ_period=int(integration_period),  
            autocorr_only=bool(autocorr_only),
            no_accum=bool(no_accum),
            auto_bin_set=active_bin_set is None,
            bin_set=active_bin_set
        )
        self.SOFT_RESET = 0
        self.fpga.set_corr_reset(0)

    def stop_correlator(self):
        """Stop the correlator array from sending data."""
        self.SOFT_RESET = 1

