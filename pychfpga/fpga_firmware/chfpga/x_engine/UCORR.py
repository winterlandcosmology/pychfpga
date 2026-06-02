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

from pychfpga.common.udp_packet_receiver import UDPPacketReceiver
from pychfpga.utils.ucorr_packet_processor import UCorrPacketProcessor

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
        self.state = state
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
        print(f'======= CORR.core[{int(self.instance_number)}] =============')
        print(f' Don\'t accumulate: {self.NO_ACCUM}')
        print(f' Autocorrelation only: {self.AUTOCORR_ONLY}')
        print(f' Firmware integration period: {self.INTEGRATION_PERIOD} frames')
        print(f' Overrun in one of the cores: {self.OVERRUN}')


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

        # Extract relevant configuration parameters for convenience
        ct_level = config.CT_LEVEL
        NDECIM = config.NDECIM
        NCHAN = config.NCHAN
        NCLK = config.NCLK

        # Derive useful parameters
        NPROD = NCHAN * (NCHAN + 1) // 2
        NPROD_PER_BINCLK =  NPROD // NCLK # number of products for each binclk, i.e number of products in each packet
        print(f"{NCHAN=}, {NCLK=}, {NPROD_PER_BINCLK=}, {NPROD=}")


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


class UCorrFrameReceiver(UCorrPacketProcessor):
    """Convenience wrapper around :class:`UCorrPacketProcessor` for burst reads.


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

    def __init__(
        self,
        packet_receiver,
        config=None,
        pre_func=None,
        receive_window=3,
        verbose=0,
    ):
        self.pre_func = pre_func
        super().__init__(packet_receiver, config=config, receive_window=receive_window, verbose=verbose)
        # Singleton iterator: an islice that stops mid-batch must not abandon unyielded
        # rows — a fresh iterator would let the next pr.receive_packets() overwrite them.
        self.data_iter: Generator = self.get_data_iter()

    def flush(self, flush_timeout=None):
        """Drain UDP, clear assembly state, and restart the streaming iterator."""
        super().flush(flush_timeout)
        self.data_iter = self.get_data_iter()

    def get_data_iter(self):
        """Continuously receive UDP packets, process them, and yield ``(integ_ts, data, sats, counts)`` rows."""
        while True:
            self.pr.receive_packets(verbose=self.verbose)
            if not self.pr.n_stored:
                continue
            result = self.process_packets()
            if not result:
                continue
            integ_ts, data, sats, counts = result
            for it, d, s, c in zip(integ_ts, data, sats, counts):
                yield it, d, s, c

    def read_corr_data_iter(
        self,
        number_of_results=None,
        soft_integ_period=1,
        flush=False,
        data_timeout=0.1,
        flush_timeout=0.001,
        config=None,
        verbose=0,
    ):
        """Yield ``number_of_results`` ``(integ_ts, data, sats, counts)`` tuples.

        The loop pulls batches of packets from the UDP receiver, decodes them
        into integration slots via ``get_data_iter``, and yields each completed
        (data, counts, sats) tuple. Optional flushing and alignment allow you to
        discard partial integrations and start on a clean boundary.

        Parameters:


            number_of_results (int): Number of results to yield. If None or zero, the generator runs indefinitely.

            soft_integ_period (int): Number of hardware frames to accumulate in
                software before yielding a result. Results are aligned to
                multiples of ``soft_integ_period`` when alignment is enabled.

            flush (bool): If True, drain the UDP buffer before starting capture.
                Use True on the first call in a burst series and False on
                subsequent calls to avoid losing in-flight packets.

            align (bool): If True, discard the first partial integration so the
                first yielded result starts on a clean integration boundary.
                This is forced on when ``flush`` is True.

            data_timeout (float): Socket timeout while waiting for packets. Set
                to slightly less than the expected inter-burst gap so the
                buffer can be processed promptly; too large delays processing
                until the buffer fills.

            flush_timeout (float): Socket timeout used while flushing. Must be
                shorter than the gap between packet bursts so flushing stops.

            verbose (int): verbosity level. 0=Quiet, 1=basic info, 2=detailed info, 3 = debugging info
        """
        # self.verbose = verbose
        # self.soft_integ_period = soft_integ_period,
        # self.skip_partial_integs = skip_partial_integs,

        if self.pre_func:
            self.pre_func(self)
        if config is not None:
            self.update_config(config)
        if soft_integ_period != self.soft_integ_period:
            self.update_config({"soft_integ_period": int(soft_integ_period)})
        if flush:
            self.flush(flush_timeout)
        self.pr.settimeout(data_timeout)
        return itertools.islice(self.data_iter, number_of_results) if number_of_results else self.data_iter

    def read_corr_frames(self, number_of_results=1, **kwargs):
        """Capture ``number_of_results`` integrated frames into preallocated arrays.

        Returns ``(integ_ts, data, sats, counts)`` of shapes
        ``(N,)``, ``(N, NBINS, NPROD)``, ``(N, NBINS, NPROD)``, ``(N,)``.
        """
        if (config := kwargs.get("config")) is not None:
            self.update_config(config)
        integ_ts, data, sats, counts = self.create_output_buffer(n_integ=number_of_results)
        integ_ts.fill(0)
        data.fill(0)
        sats.fill(0)
        counts.fill(0)
        n_filled = 0
        for i, (it, d, s, c) in enumerate(self.read_corr_data_iter(number_of_results=number_of_results, **kwargs)):
            integ_ts[i], data[i], sats[i], counts[i] = it, d, s, c
            n_filled = i + 1
        if n_filled != number_of_results:
            warnings.warn(
                f"read_corr_frames: received {n_filled} integ(s), expected {number_of_results}; "
                "remaining rows are zero-filled (timeouts, no packets passing filters, or slow source).",
                UserWarning, stacklevel=2,
            )
        return integ_ts, data, sats, counts
