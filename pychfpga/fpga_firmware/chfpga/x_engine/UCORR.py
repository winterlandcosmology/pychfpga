"""
CORR.py module
 Implements interface to the correlator blocks

 History:
 2017-05-04 : JFC : Created
"""
import time
import logging
import numpy as np
import matplotlib.pyplot as plt

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


    def __init__(self, *, router, router_port, verbose=0):
        super().__init__(router=router, router_port=router_port)
        self.verbose = verbose
        self.logger = logging.getLogger(__name__)
        self.corr_config_id = 0  # Number that is increased each time the correlator configuration is changed.
        self.bin_map = None  # cached maps
        self.prod_map = None
        self.last_fft_config_id = None

    def init(self):
        """ Inisializes all modules of a correlator block."""
        self.CORR_ID = (self.fpga.slot-1) if self.fpga.slot else 0
        self.bins_per_frame = self.fpga.FRAME_LENGTH // 2 # total number of bins at the output of the channelizers
        self.NCLK = self.BIN_DECIMATION_FACTOR * self.NCLK_PER_BIN  # Number of clocks available to compute all the products. Will reduce the number of required CMACs, but increase the number of products per CMAC
        self.NPROD = self.NCHAN * (self.NCHAN + 1) // 2 # total number of computed products
        self.NDECIM = self.BIN_DECIMATION_FACTOR

    def status(self):
        """Displays the status of the correlator array"""
        print('======= CORR.core[%i] =============' % self.instance_number)
        print(f' Don\'t accumulate: {self.NO_ACCUM}')
        print(f' Autocorrelation only: {self.AUTOCORR_ONLY}')
        print(f' Firmware integration period: {self.INTEGRATION_PERIOD} frames')
        print(f' Overrun in one of the cores: {self.OVERRUN}')

    def get_params(self):
            return Namespace(
                number_of_correlators=self.fpga.NUMBER_OF_CORRELATORS,  # hard coded in firmware
                number_of_correlated_inputs=self.fpga.NUMBER_OF_INPUTS_TO_CORRELATE,  # hard coded in firmware
                number_of_bins_per_frame=self.fpga.FRAME_LENGTH // 2  # CT engine always sends all bins in corr8
                # fft_bypass = get_common_chan_attr('FFT','BYPASS')
                )

    def get_config(self):
        """ Return a dict containing the essential information required to receive and assemble correlator packets.
        """

        bin_map, prod_map = self.get_bin_map()
        conf = dict(
            NCHAN = self.NCHAN,
            NCLK = self.NCLK,
            bin_map = bin_map,
            prod_map = prod_map,
            firmware_integ_period = self.INTEGRATION_PERIOD + 1
            )
        return conf

    def get_input_bin_map(self):
        return self.fpga.chan[0].FFT.get_bin_map()

    def get_bin_map(self):
        ct_level = self.fpga.CT.CT_LEVEL
        fft = self.fpga.chan[0].FFT

        if fft.config_id == self.last_fft_config_id and self.bin_map is not None and self.prod_map is not None:
            return self.bin_map, self.prod_map

        NDECIM = self.NDECIM
        NCHAN = self.NCHAN
        NCLK = self.NCLK
        NPROD_PER_BINCLK =  self.NPROD // NCLK # number of products for each binclk, i.e number of products in each packet
        print(f"{NCHAN=}, {NCLK=}, {NPROD_PER_BINCLK=}, {self.NPROD=}")


        # Map to convert a (i,j) coordinate in a NCHANxNCHAN matrix to a common-sense linearized product index (pix)
        ij_to_pix_map = self.get_ij_to_prod_map(NCHAN)

        # Get the (iv,jv) coordinate of each product generated by each correlator CMAC.
        # cmac_out_ij[clk, prod] = (iix, jix), where
        #    clk: 0..NCLK-1: each of the NCLK used to produce products for one bin
        #    prod: 0 .. NPROD/NCLK: Product produced by the CMAC on each clock
        #    iv, jv = position of channel in the CMAC input vector (which may not necessarily correspond to the channel number in that position)
        cmac_out_ij = np.array([[(i, (j+i) % NCHAN) if i < NCHAN-j else ((j+i) % NCHAN, i-1)
                               for i in range(NCHAN+1) for j in range(NCHAN//2//NCLK*clk, NCHAN//2//NCLK*(clk+1)) ]
                               for clk in range(NCLK) ])


        fft_bin_map = fft.get_bin_map()
        self.last_fft_config_id = fft.config_id

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
            print(f'CT2 bin_map={ct_bin_map.shape}')

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
            print(f'CT3 bin_map={ct_bin_map.shape}')


         # Compute a map that lists the bins seen by each CMAC after decimation
        # corr_bins[bin_set, corr, bix] = bin
        corr_bins = np.array([ct_bin_map[:, bs::NDECIM] for bs in range(NDECIM)])
        print(f'Post Corr decimation corr_bins={corr_bins.shape}, {NDECIM=}')




        # print(f'{NCLK=}\n{ct2_bins=}\n{fft_bin_map=}\n{cmac_out_ij=}\n{cmac_out_pix=}')

        # Compute map that lists the bins corresponding to every product of every packet sent by each correlator for a specific bin_set
        # bin_map[bin_set, corr, clkbin, prod] = bin
        # Note: bin is the save for every prod and for every NCLK clkbins.
        bin_map = np.array([[[[ bin_ for prod in range(NPROD_PER_BINCLK)] for bin_ in bins for clk in range(NCLK)] for bins in bin_set_map] for bin_set_map in corr_bins])

        # Compute the linearized product index corresponding to every product of every packet sent by each correlator.
        # We take into account the real channel numbers provided into the CMAC input vector position.
        # cmac_out_pix[clk, prod] = pix
        print(f'{cmac_out_ij.shape=}, {chan_map=}')
        cmac_out_pix = ij_to_pix_map[chan_map[cmac_out_ij[...,0]], chan_map[cmac_out_ij[...,1]]]

        # Compute the linearized product index for each (corr, clkbin, prod) data sent by the correlator
        #   prod_map[corr, clkbin, prod] where:
        #     corr: 0 .. NCORR-1: correlator number
        #     clkbin: 0 .. NBINS_PER_CORR * NCLK, each product stored in ram = number of packets
        #     prod: 0 .. NPROD/NCLK, each product in a packet
        prod_map = np.array([[[cmac_out_pix[clk, clkprod] for clkprod in range(NPROD_PER_BINCLK)] for _ in lane_bins for clk in range(NCLK)] for lane_bins in corr_bins[0]])

        print(f'bin_map={bin_map.shape}, prod_map={prod_map.shape}')
        self.bin_map = bin_map
        self.prod_map = prod_map
        return bin_map, prod_map

    @staticmethod
    def get_ij_to_prod_map(NCHAN):
        """ Return an array that maps a (i,j) pair to a product index """
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
        else:
            self.AUTO_BIN_SET = 1
            self.BIN_SET = bin_set

        if correlators is not None:
            raise RuntimeError("'correlators' argument is not supported by UCORR")
        if bandwidth_limit is not None:
            self.logger.warn(f"{self!r}: 'bandwidth_limit' argument is not yet supported by UCORR")
        self.verbose = verbose
        self.corr_config_id += 1  # indicate that some parameters (i.e. integration period) might have changed

    def get_data_receiver(self, sock):
        packet_receiver = UDPPacketReceiver(sock)
        return UCorrFrameReceiver(packet_receiver, config=self.get_config())


    @classmethod
    def get_corr_receiver(cls, packet_receiver, config=None, pre_func=None):
        """ Return an instance of a correlator packet receiver
        """
        return UCorrFrameReceiver(packet_receiver, config=config, pre_func=pre_func)

    @classmethod
    def get_packet_receiver(cls, sock, n_packets=2048, max_packet_size=9000, verbose=0):
        """ Return an instance of a UDP packet receiver
        """
        return UDPPacketReceiver(sock, n_packets=n_packets, max_packet_size=max_packet_size, verbose=verbose)

class UCorrPacketProcessor:
    """
    Packet processor for UCORR FPGA-based N-square firmware correlator.

    The processor reads data in the specified packet buffer, decodes, assembles and integrate the correlation products,  and yields
    all the processed correlation products that are ready to be passed to the user.

    Parameters:

                buf (ndarray): Buffer containing the packets to process

                config (dict): Initial correlator configuration. Can be updated later.

    """
    CORR_PACKET_COOKIE = 0xCF
    NBYTES_PER_HEADER = 10 # Number of bytes in the UDP payload header
    NBYTES_PER_PROD = 5  # Number of bytes used to store each complex correlator product ( (18+18) bits = 36 bits, 5 bytes = 40 bits)


    def __init__(self, buf, config=None, verbose=1):


        # self.pr = packet_receiver
        # self.pre_func = pre_func
        # self.config_getter = None
        # if callable(config):
        #     self.config_getter = config
        #     config = self.config_getter(init=True)

        self.verbose = verbose
        # Define numpy data types that will be used to parse the correlator packets
        self.buf = buf
        self.NCHAN = None
        self.NCLK = None
        self.bin_map = None
        self.prod_map = None
        self.firmware_integ_period = None

        self.update_config(config)

    def update_config(self, config, force=False):
        """ Update the correlator receiver configuration


        `update_config` needs to be called at least once before we start processing packets. It will
        allocate processing buffers based on the correlator configuration. There is some logic to
        reallocate these buffers only if dependent parameters have changed.

        Parameters:

            config (dict): dict containing the configuration of the correlator. Any of the following
                keys can be provided in an update_config call, and multiple calls can be made, but all of them must have been
                provided before data is being processed.

                - NCHAN (int): Number of input channels that are beiing correlated together. Sets the number of products.
                - NCLK (int): Number of clock cycles used to compute one set of products. This sets how many packets are set by bin.
                - bin_map (numpy.ndarray or list): Array of dimensions [NBINSET, NCORR, NPROD_PER_CMAC, NCMAC] that
                  describe the bin numbers for a specific bin set, correlator, bin/clock (packet index for each correlator)
                  and CMAC (complex number index within a packet). The array can be provided as a numpy ndarray or a series of
                  nested lists that convert to the desired 4-dimensional array so it can be sent as a serialized object.
                - prod_map (numpy.ndarray or list): Array of dimensions [NCORR, NPROD_PER_CMAC, NCMAC] that
                  describe the normalized product index for data from a specific correlator, bin/clock (packet index for each correlator)
                  and CMAC (complex number index within a packet). The array can be provided as a numpy ndarray or a series of
                  nested lists that convert to the desired 4-dimensional array so it can be sent as a serialized object.
                - firmware_integration (int): Number of frames integrated in firmware.

        Notes:

            -

        """

        def set_param(name):
            if (v := config.get(name)) is not None:
                if force or (np.all(v != getattr(self, name)) if isinstance(v, np.ndarray) else v != getattr(self, name)):
                    setattr(self, name, v)
                    return True
            return False

        if config is None:
            return

        # get fundametal paramaters form config
        NCHAN_updated = set_param('NCHAN')
        NCLK_updated = set_param('NCLK') # number of clocks required to compute the products for one bin
        bin_map_updated = set_param('bin_map')
        prod_map_updated = set_param('prod_map')
        set_param('firmware_integ_period')


        # Don't update buffers unless we have a full set of parameters
        if not self.config_ready():
            return

        # Derived parameters
        self.NPROD = self.NCHAN * (self.NCHAN + 1) // 2   # Total number of products per correlator frame
        self.NDECIM, self.NCORRS, self.NPROD_PER_CMAC, self.NCMAC = self.bin_map.shape
        self.NBINS = self.bin_map.max() + 1
        self.NPROD_PER_ARRAY = self.NCORRS * self.NPROD_PER_CMAC

        if NCHAN_updated:
            # Compute an array that maps a (i,j) pair to a product index
            self.ij_to_prod_map = UCORR.get_ij_to_prod_map(self.NCHAN)

        if bin_map_updated:
            # Packet buffers views should be updated if the following change: NCMAC (bin_map)
            if self.verbose:
                print('Creating new receive buffer correlator views')
            self.header_dtype = np.dtype(dict(
                names=['cookie', 'stream_id', 'flags', 'ts'],
                offsets=[0, 1, 3, 2],
                formats=['u1', '>u2', 'u1', '>u8']))

            self.product_dtype = np.dtype(dict(
                names=['sat', 'l', 'h'],
                offsets=[4, 0, 1],
                formats=['u1', '<i4', '<i4']))

            self.packet_dtype = np.dtype([
                ('header', self.header_dtype, (1, )),
                ('data', self.product_dtype, (self.NCMAC, ))])

            # Pre-allocate buffers in which recv_into() will put the received data
            # directly. We could use empty() to save some cycles, but the
            # uninitialized data can be confusing for debugging
            # self.buf = np.zeros((self.NPACKETS, self.PACKET_SIZE), dtype=np.uint8)

            # Various views of the buffer to allow quick and easy access to the
            # packet contents. This does not cause new memory allocations.
            self.buf_struct = self.buf[:,:self.packet_dtype.itemsize].view(self.packet_dtype)[:, 0]  # (NPACKETS,)
            self.buf_cookie = self.buf_struct['header']['cookie'][:, 0]
            self.buf_flags = self.buf_struct['header']['flags'][:, 0]
            self.buf_stream_id = self.buf_struct['header']['stream_id'][:, 0]
            self.buf_ts = self.buf_struct['header']['ts'][:, 0]

            self.buf_data_h = self.buf_struct['data']['h']  # (NPACKETS, NPROD)
            self.buf_data_l = self.buf_struct['data']['l']  # (NPACKETS, NPROD)
            self.buf_data_sat = self.buf_struct['data']['sat']  # (NPACKETS, NPROD)
            self.ts_mask = np.uint64(0xFFFFFFFFFFFF)  # just keep the last 48 bits

            # Pre-allocate temporary storage to extract the real/imaginary part from the 5-byte packed product
            NPACKETS = self.pr.buf.shape[0]
            self.temp32 = np.empty((NPACKETS, self.NCMAC), dtype=np.int32)

            # Allocate integration slots used to accumulate the packets as they arrive
            # update on change on  NCORRS, NPROD_PER_CMAC, NCMAC (bin_map)
            self.NINTEG_SLOTS = 3 # Number of data sets (timestamps) to store
            self.current_set = None
            # self.ts = np.zeros((self.NINTEG_SLOTS,), dtype=np.uint64)
            # ts_dict = {} # {timestamp:n, ...): keeps track of the known timestamps and associated capture sets
            self.acc_re = np.zeros((self.NINTEG_SLOTS, self.NDECIM, self.NCORRS, self.NPROD_PER_CMAC, self.NCMAC), dtype=np.int64)
            self.acc_im = np.zeros((self.NINTEG_SLOTS, self.NDECIM, self.NCORRS, self.NPROD_PER_CMAC, self.NCMAC), dtype=np.int64)
            print(f'acc_re shape (N_results, Ncorr, Ncmac, Nprod) = {self.acc_re.shape}')
            # Number of saturations for the real and imaginary part of each product
            self.sat = np.zeros((self.NINTEG_SLOTS, self.NDECIM, self.NCORRS, self.NPROD_PER_CMAC, self.NCMAC, 2), dtype=np.int32)
            # Number of packets received for each NCMAC (and therefore each
            # product). Can be used to know how many packets were lost and to
            # normalize the data
            # self.count = np.zeros((self.NINTEG_SLOTS, self.NDECIM, self.NCORRS, self.NPROD_PER_CMAC, self.NCMAC), dtype=np.uint32)
            self.total_count = np.zeros((self.NINTEG_SLOTS,), dtype=np.uint32)
            self.current_integ = None
            self.flush = True  # start first acquisition with UDP buffer flush

        if NCHAN_updated or bin_map_updated:

            if self.verbose:
                print('Creating new remapped output data buffers')

            # Storage for a single integrated result when streaming
            # Update on change on: NBINS, NPROD (bin_map, NCHAN)
            self.integ_data = np.empty((self.NBINS, self.NPROD), dtype=np.complex128)
            self.integ_sats = np.empty((self.NBINS, self.NPROD), dtype=np.complex64)
            # self.integ_counts = np.empty((self.NBINS, self.NPROD), dtype=np.int32)
            self.integ_total_counts = 0

    def config_ready(self):
        """ Return True if all the fundamental configuration parameters have been specified """
        return all(v is not None for v in (self.NCHAN, self.NCLK, self.bin_map, self.prod_map, self.firmware_integ_period))

    def get_ij_to_prod_map(self):
        return self.ij_to_prod_map

    def get_data_iter(
            self,
            n,
            soft_integ_period=1,
            skip_first_integ=False,
            skip_partial_integs=True,
            config = None,
            verbose=0):
        """
        Returns a generator that yields the correlator data resulting from processing `n` additional lines in the packet buffer.

        Parameters:

            n (int): Number of entries (packets) to process in the buffer

            soft_integ_period (int): Number of correlator frames to
                accumulate in software. A software frame will always be
                aligned to a multiple of soft_integ_period.


            flush (bool): if True, the UDP buffers will be emptied before data is captured. For
                multiple acquisitions, set to True on the first call and False on the subsequent
                ones to allow lossless  acquisition.

            skip_first_integ (bool): if True, the first integration will be dropped if it is missing any data.

            skip_partial_integs (bool): If True, any integration that is missing data will be dropped


            select (bool): If True, the firmware will configure to stream the correlator data if needed.

            out (tuple): (data, data_counts, data_sats)  tuple providing the ndarrays into which data will be stored.
                if None, the results will be yielded as they come.

            verbose (int): verbosity level. 0=Quiet, 1=basic info, 2=detailed info, 3 = debugging info
        """
        self.verbose = verbose

        if not self.config_ready():
            raise RuntimeError('Configuration parameters are missing')


        if config is not None:
            self.update_config(config)

        self.skip_partial_integs = skip_partial_integs
        self.soft_integ_period = soft_integ_period

        if not n:
            return


        # self.firmware_integ_period = self.get_corr_param('INTEGRATION_PERIOD') + 1
        # Compute the software integration period in number of frames. This will be used to determine the integration_number index in the destination matrix.
        # Note that we HAVE TO cast integ_period in an unsigned integer, otherwise any operations with other unsigned (e.g ts differences) will be promoted to a float64.
        integ_period = np.uint64(self.soft_integ_period * self.firmware_integ_period)
        self.expected_packets_per_integ = self.NPROD_PER_ARRAY * self.soft_integ_period

        self.overrun = 0

        self.skip_next_integ = skip_first_integ
        self.n = 0 # number of processed packets

        (buf_ix, ) = np.where(self.buf_cookie[:n] == self.CORR_PACKET_COOKIE)

        if not buf_ix.size:
            return

        ts = self.buf_ts[buf_ix] & self.ts_mask
        integ = ts // integ_period  # watch out! integ_period needs to be uint64 or the result will be "promoted" to float64 (signed OP unsigned=float)
        integ_min = int(integ.min()) # convert to int so we can do math with those and stay integer
        integ_max = int(integ.max())

        if self.current_integ is None:
            self.current_integ = integ_min

        if verbose >= 2:
            print(f'got {len(buf_ix)} corr packets. ts={set(ts)}/{integ_period}, integs={set(integ)} {ts.dtype}//{integ_period.dtype}')

        # slot = self.ts_dict.setdefault(integ, len(ts_dict))
        if integ_max - integ_min >= self.NINTEG_SLOTS-1:
            print(f'Received stale data from integration periods ({set(integ)}), but expected values between {self.current_integ}-{self.current_integ+self.NINTEG_SLOTS-1}. Ignoring data' )
            self.clear_integ_slot()
            self.skip_next_integ = True
            self.current_integ = None
            return

        if integ_min < self.current_integ:
            raise RuntimeError('received timestamp before the current integration period.')

        if integ_max >= self.current_integ + self.NINTEG_SLOTS:
            # Flush integs that would come out of the integ window
            for i in range(self.NINTEG_SLOTS):
                if integ_max >= self.current_integ + self.NINTEG_SLOTS:
                    yield from self.yield_current_integ()
            # If we are still too far, reset the current_integ
            if integ_max  >= self.current_integ+ self.NINTEG_SLOTS:
                self.current_integ = integ_max - self.NINTEG_SLOTS + 1;

        self.accumulate_data(buf_ix, integ % self.NINTEG_SLOTS)

        if verbose >= 2:
            print(f'Current_integ={self.current_integ}, Set counts: {self.total_count}. Expecting {self.expected_packets_per_integ} packets')

        # Flush integs as long as there are complete data sets
        while any(self.total_count >= self.expected_packets_per_integ):
            yield from self.yield_current_integ()

        if self.overrun:
            print(f"*** Warning: the correlator experienced an overrun condition. All products could not be sent within the hardware integration period.")

    def yield_current_integ(self):
        """ Yields the current integration result in the current integration slot,
        clear the integration slot,  and increment the current integration counter.
        """
        cs = self.current_integ % self.NINTEG_SLOTS
        # print(f'{self.total_count[cs] == self.expected_packets_per_integ} {self.skip_partial_integs} {self.skip_next_integ}')
        if self.total_count[cs] == self.expected_packets_per_integ or not (self.skip_partial_integs or self.skip_next_integ):
            if self.verbose:
                print(f'Returning result for integ={self.current_integ}, integ slot={cs}, len={self.total_count[cs]}/{self.expected_packets_per_integ}')

            # watch out: if you extract .real/.imag  *after* doing the advanced indexing, you will
            # store data to a copy
            self.integ_sats.real[self.bin_map, self.prod_map] = self.sat[cs, ..., 0] / 32.
            self.integ_sats.imag[self.bin_map, self.prod_map] = self.sat[cs, ..., 1] / 16.
            self.integ_data.real[self.bin_map, self.prod_map] = self.acc_re[cs]
            self.integ_data.imag[self.bin_map, self.prod_map] = self.acc_im[cs]
            # self.integ_counts[self.bin_map, self.prod_map] = self.count[cs]
            self.integ_total_counts = self.total_count[cs]
            data_valid = True

        else:
            data_valid = False
            if self.verbose:
                print(f'Discarding result for integ={self.current_integ}, integ slot={cs}, len={self.total_count[cs]}/{self.expected_packets_per_integ}')

        # Clear the integration slot
        self.clear_integ_slot(cs)
        self.current_integ += 1
        self.skip_next_integ = False
        if data_valid:
            yield (self.integ_data, self.integ_total_counts, self.integ_sats)

    def clear_integ_slot(self, slice_=slice(None)):
        """ Clear the integration slot(s)
        Parameters:
            slice_ (slice): Slice object indicating which slots to clear. If not specified, all slots are cleared.

        """
        self.total_count[slice_] = 0
        self.acc_re[slice_] = 0
        self.acc_im[slice_] = 0
        self.sat[slice_] = 0
        # self.count[slice_] = 0

    def accumulate_data(self, ix, slots):
        """ Add the data from the packets at index `ix` into the corresponding integration slots `slots`.

        Parameters:

            ix (ndarray): indices of buffer entries containing valid data

            slots (ndarray): integration slot number to which each of the buffer entry should be added

        """
        t1 = time.time()

        # v = a.view(t)[:n, 0]
        # hh = h[:n]
        stream_id = self.buf_stream_id[ix] >> 11
        bin_ = self.buf_stream_id[ix] &  0b111_11111111
        bin_set = (self.buf_flags[ix] >> 4)
        # ts = self.buf_ts[:n] & self.ts_mask

        if self.verbose >=2:
            print(f'Slots {set(slots)}')
            print(f'corr_id shape={stream_id.shape}, {stream_id.min()} - {stream_id.max()}')
            print(f'bin shape={bin_.shape}, {bin_.min()}-{bin_.max()}')
            print(f'{len(stream_id)} corr/bin IDs, Unique corr/bins IDs: {len(set(zip(stream_id, bin_)))}')
            print(f'(Slot,bin_set) pairs: {set(zip(slots,bin_set))}')

        # Extract the real part (in bits 27:10 of the data_h). We shift
        # left the MSB to bit 31 and sift the lsb back down to 0 to sign
        # extend the 18-bit value result within the 32-bit word.
        np.copyto(self.temp32, self.buf_data_h)
        np.left_shift(self.temp32, 4, self.temp32)
        np.right_shift(self.temp32, 14, self.temp32)
        # Add the sign-extended value to the 64-bit accumulator.
        # print(f'bin_ = {corr.shape}')
        # print(f'cmac shape = {cmac.shape}')

        self.acc_re[slots, bin_set, stream_id, bin_] += self.temp32[ix]

        # Extract the imag part (in bits 17:0 of the data_l).
        np.copyto(self.temp32, self.buf_data_l)
        np.left_shift(self.temp32, 14, self.temp32)
        np.right_shift(self.temp32, 14, self.temp32)
        # Add the sign-extended value to the 64-bit accumulator.
        self.acc_im[slots, bin_set, stream_id, bin_] += self.temp32[ix]

        t2 = time.time()

        # Keep track of how many packets were received for each correlator/cmac
        np.add.at(self.total_count, slots, 1)
        # np.add.at(self.count, (slots, bin_set, stream_id, bin_),  1)
        # self.ts[slots_number, corr, cmac]
        # Accumulate the flags for each product by or'ing them together
        # We'll mask those later to save time
        self.sat[slots, bin_set, stream_id, bin_, :, 0] += self.buf_data_sat[ix] & 0x20
        self.sat[slots, bin_set, stream_id, bin_, :, 1] += self.buf_data_sat[ix] & 0x10


        t3 = time.time()
        self.overrun |= any(self.buf_flags[ix] & 1);
        if self.verbose >=2:
            # print 'count=', count[0,0]
            dt1 = t2 - t1
            dt2 = t3 - t2
            dt = t3 - t1
            print(f'Processing & accumulating {len(ix)} packets '
                  f'for software integ(s) # [{set(slots)}] '
                  # f'from correlator frame {ts} '
                  # f'({(ts - self.first_ts)% self.soft_integ_period}/{self.soft_integ_period};  '
                  # f'took {dt * 1000:.3f} ms ({(float(dt) / (self.NBINS) * 1000):.3f} ms/corr frame) '
                  # f'({dt1 * 1000:.3f} + {dt2 * 1000:.3f} ms) '
                  f' overrun={self.overrun}')


    def create_data_set(self, n):
            # Allocate memory to store reordered data
            data_sats = np.empty((n, self.NBINS, self.NPROD), dtype=np.complex64)
            data_counts = np.empty((n,), dtype=np.int32)
            # data_counts = np.empty((n, self.NBINS, self.NPROD), dtype=np.int32)
            data = np.empty((n, self.NBINS, self.NPROD), dtype=np.complex128)
            return (data, data_counts, data_sats)

class UCorrFrameReceiver(UCorrPacketProcessor):
    """
    Data receiver for UCORR FPGA-based N-square firmware correlator.


    The receiver is fast enough to capture data that is integrated in firmware
    down to a rate of about 10 ms/integrated frame, that is, exceeding 500
    Mbits/s, provided the system provides a sufficiently big UDP buffer to hold
    the data until the receiver method is called to process it (see below).

    The receiver can perform real-time software integration of the data. A
    specific number of software-integrated frames can be returned, or data can
    be saved to disk indefinitely until stopped.

    Parameters:

        packet_receiver (object): Instance to the UDP packet receiver that stores UDP packets to be
            processed. The receiver shall have a get_packets() method if the standalone
            `read_corr_frames` method is to be used.

        config (dict): Dict containing initial information on the correlator
            configuration.

        pre_func (function): Function to be called before data acquisition and processing is performed.

            , or function  to be called to get that dict. The dict shall also be passed
            to subsequent correlator packet request calls if the configuration has changed.

            If `corr_params` is a function it should have the signature ``corr_params(init)`` where
            True on initial setup to force an update of all parameters, and generally False on each
            subsequent packet request unless a re-init is requested by the user. The function can be
            used to setup the correlator data transmission path when init=False if needed by the
            firmware. If ``init=False``, the function can return `None` to indicate that no
            configuration change has occured. Since the function will be called on every
            time-sensitive packet requests, care should be taken to avoid any lengthy or unnecessary
            processing.

            Note that the user can manually call the `update_params` method after changes that impact the receiver.


    Data format
    -----------
    The number of correlation products computed ny the N-square correlator is NPROD = NCHAN*(NCHAN+1)/2.

    Correlator products are sent as N_CLKBINS=NCLK*NPROD_PER_CMAC packets from each correlator core in the array, each packet containing N_PROD/NCLK complex products.

    Complex value data results

    Each correlator frame has a (18+18) bit resolution, which is then
    integrated for some time. Assuming the worst case of a saturatet 1
    Gb/s link sending the maximum value if 2**17, we would get 175.8
    correlator frames/s,  with soft integrator values of increase by 2**24.46/s. If we integrate for
    we

    A float32 can represent integers values exactly up to 2**24, which
    leaves room for less than one second of integration in the worst
    case. We cannot thereofre use a complex64 value (float32+float32),
    and therefore use a complex128 format.


    System Requirements
    -------------------

    Jumbo Frames
    ............


    UCAP packets are small and do not require Jumbo frames.

    System UDP Buffers
    ..................


    Correlator packets are sent if very fast bursts and need to be stored in the OS networking stack until Python gets around to read them, otherwise packet loss will occur.

    Insufficient OS buffering is the main cause for UDP packet loss (not the computer speed, not the switches, not Python).

    The UDP buffers shall be increased to reduce packet loss to a minimum::

        sudo sysctl -w net.core.rmem_max=262144000
        sudo sysctl -w net.core.rmem_default=262144000
        sudo sysctl -w net.ipv4.udp_mem='26214400 26214400 262144000'
        sudo sysctl -w net.ipv4.udp_rmem_min=262144000

    To check UDP buffers::

        sysctl -a | grep mem

    The following command can be used to monitor packet dropped by the OS in real time due to insufficient UDP buffer space::

        watch -cd -n .5 "cat  /proc/net/udp"
    """


    # def get_freqs(self):
    #     return np.arange(self.NBINS)/self.NBINS/2*self.corrs[0].fpga._sampling_frequency

    def __init__(self, packet_receiver, pre_func=None, config=None):
        self.pr = packet_receiver
        self.pre_func = pre_func
        super().__init__(buf=self.pr.buf, config=config)

    def read_corr_data_iter(
            self,
            number_of_results = None,
            soft_integ_period=1,
            flush=None,
            align=False,
            data_timeout=0.1,
            flush_timeout=0.001,
            skip_partial_integs=True,
            config = None,
            verbose=0):
        """ Generator that continuously retreives UDP packets and yields processed correlator data when they are ready.

        Parameters:


            number_of_results (int): Number of results to yields back. If None or zero, the generator never ends.

            soft_integ_period (int): Number of correlator frames to
                accumulate in software. A software frame will always be
                aligned to a multiple of soft_integ_period.


            flush (bool): if True, the UDP buffers will be emptied before data is captured. For
                multiple acquisitions, set to True on the first call and False on the subsequent
                ones to allow lossless  acquisition.

            align (bool): if True, a partial first integration will be dropped. Align is automatically set if Flush is set.

            data_timeout (float): Maximum amount of time to wait for data before starting
                processing. Should be matched to a dead time between data set transmission to be
                effective. If too large, the data will be processed only when the buffer is full.

            flush_timeout (float): Maximum amount of time to wait for packets before deciding there
                is no more data in the buffer. Must be smaller than the gap between data packets,
                otherwise it will never stop flushing.



            out (tuple): (data, data_counts, data_sats)  tuple providing the ndarrays into which data will be stored.
                if None, the results will be yielded as they come.

            verbose (int): verbosity level. 0=Quiet, 1=basic info, 2=detailed info, 3 = debugging info
        """
        # self.verbose = verbose
        # self.soft_integ_period = soft_integ_period,
        # self.skip_partial_integs = skip_partial_integs,

        if self.pre_func:
            self.pre_func(self)

        # Flush the UDP buffer by reading data until we timeout. We assume
        # here that we can read the data fast enough to empty the buffer and
        # that no new will come  for the timeout period.
        if flush:
            self.pr.flush(flush_timeout)
            self.clear_integ_slot() # clear ALL integration slots
            align = True
            self.current_integ = None

        self.pr.settimeout(data_timeout)
        n = 0  # number of results sent
        while True:
            # Get some packets until a new timestamp, timeout, or buffer full
            if not (npkts := self.pr.get_packets(verbose=verbose)):
                continue
            # Extract and yield all possible data from the buffer
            for d,c,s in self.get_data_iter(n=npkts, skip_first_integ=align, soft_integ_period = soft_integ_period, skip_partial_integs = skip_partial_integs, verbose=verbose):
                yield d,c,s
                n += 1
                if n >= number_of_results:
                    return
            align = False

    def read_corr_frames(self, number_of_results=1, **kwargs):
        """ Allocate a data set and capture correlator data into it.

        Parameters:


            soft_integ_period (int): Number of correlator frames to
                accumulate in software. A software frame will always be
                aligned to a multiple of soft_integ_period.


            number_of_results (int): Number of software-integrated frames to
                acquire and return.


            flush (bool): if True, the UDP buffers will be emptied before data is captured. For
                multiple acquisitions, set to True on the first call and False on the subsequent
                ones to allow lossless  acquisition.

            data_timeout (float): Maximum amount of time to wait for data before starting
                processing. Should be matched to a dead time between data set transmission to be
                effective. If too large, the data will be processed only when the buffer is full.

            flush_timeout (float): Maximum amount of time to wait for packets before deciding there
                is no more data in the buffer. Must be smaller than the gap between data packets,
                otherwise it will never stop flushing.

            verbose (int): verbosity level. 0=Quiet, 1=basic info, 2=detailed info, 3 = debugging info
        """

        # Pre-Allocate memory to store processed data
        data, counts, sats = self.create_data_set(number_of_results)
        for i, (d,c,s) in enumerate(self.read_corr_data_iter(number_of_results=number_of_results, **kwargs)):
            data[i], counts[i], sats[i] = d, c, s
        return (data, counts, sats)

