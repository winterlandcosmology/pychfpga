#!/usr/bin/python

"""
CORR.py module
 Implements interface to the correlator blocks

 History:
 2017-05-04 : JFC : Created
"""
import time
import logging
import numpy as np
import socket

from ..mmi import MMI, MMIRouter, BitField, CONTROL, STATUS

#############################################
# Basic Geometry of the firmware correlator
#############################################

# Driving parameters
NCHAN = 16  # Number of channels on which to generate N-squared products
NBINS_TOTAL = 1024  # Number of frequency bins to process
NPROD_PER_CMAC = 512  # Number of products per CMAC, limited by BRAM size (512 x (18+18) bits for the accumulator & capture RAM)
NCLOCKS_PER_BIN = 4  # Number of clocks that in which all products of one bin must be computed. This matches how many clocks is needed to receive all the channels of one bin
NBYTES_PER_PROD = 5  # 5 bytes for each product
NBYTES_PER_HEADER = 12  # Number of bytes in correlator frame header

# Derived parameters
NI_CLOCKS_PER_BIN = NCHAN // 2  # Clocks per bin of a straight Non-interleaved correlator architecture using the minimal amount of CMACs
NI_CMAC_PER_CORR = (NCHAN + 1)  # number of CMACs per correlator if computations are done in NI_CLOCKS_PER_BIN clocks
NPROD_TOTAL = NCHAN * (NCHAN + 1) // 2  # Total number of products per correlator frame
CMAC_INTERLEAVE_FACTOR = NI_CLOCKS_PER_BIN // NCLOCKS_PER_BIN
NCMAC_PER_CORR = CMAC_INTERLEAVE_FACTOR * NI_CMAC_PER_CORR  # Number of interleaved CMACs per core needed to make the computations in the target number of clocks
NBINS_PER_CMAC = NPROD_PER_CMAC // NCLOCKS_PER_BIN  # Number of bins per CMAC. =512/4=128
NBINS_PER_CORR = NBINS_PER_CMAC  # = 128
NCORR = NBINS_TOTAL // NBINS_PER_CORR  # = 8

raw_to_matrix_map = None
raw_to_vector_map = None


class CORR_core(MMI):
    """ Implements interface to one of the correlator"""

    ADDRESS_WIDTH = 12

    # Control registers
    SOFT_RESET         = BitField(CONTROL, 0x00, 7, doc="Resets this correlator core.")
    AUTOCORR_ONLY      = BitField(CONTROL, 0x00, 6, doc="Force the correlator input to be 0x10101010")
    NO_ACCUM           = BitField(CONTROL, 0x00, 5, doc="Disables accumulation - only the last result is saved")
    USER_ID            = BitField(CONTROL, 0x00, 0, width=4, doc="USER ID used in the correlator packet header")
    INTEGRATION_PERIOD = BitField(CONTROL, 0x04, 0, width=32, doc="Duration of te integration period minus one")
    BINS_PER_FRAME     = BitField(CONTROL, 0x06, 0, width=9, doc="Number of frequency bins per frame minus one")

    # Status registers
    STATUS_BYTE   = BitField(STATUS, 0x00, 0, width=8, doc="Status byte")
    IN_FRAME_CTR  = BitField(STATUS, 0x01, 0, width=8, doc="Input frame counter")
    OUT_FRAME_CTR = BitField(STATUS, 0x02, 0, width=8, doc="Output frame counter")

    def __init__(self, *, router, router_port, instance_number, verbose=0):
        super().__init__(router=router, router_port=router_port, instance_number=instance_number)
        self.verbose = verbose
        self.logger = logging.getLogger(__name__)

    def init(self):
        """ Inisializes all modules of a correlator block."""
        self.INTEGRATION_PERIOD = 16384-1
        self.USER_ID = self.fpga.slot - 1 if self.fpga.slot else 0


    def status(self):
        """Displays the status of al the correlator blocks"""
        print('======= CORR.core[%i] =============' % self.instance_number)
        # self.CH_DIST.status()


class CORR(MMIRouter):
    """ Instantiates a container for all correlators blocks"""

    ROUTER_PORT_NUMBER_WIDTH = 4

    REQUIRES_OFFSET_BINARY_ENCODING = True
    BIT_WIDTH = 4

    def __init__(self, *, router, router_port, verbose=0):
        self.verbose = verbose
        self.logger = logging.getLogger(__name__)
        super().__init__(router=router, router_port=router_port, instance_number=instance_number)
        self.corr = []
        for i in range(self.fpga.NUMBER_OF_CORRELATORS):
            self.corr.append(CORR_core(router=self, router_port=i, instance_number=i))

    def __getitem__(self, key):
        """    Returns the correlator instance specified by the index"""
        return self.corr[key]

    def init(self):
        """ Initializes all correlators"""
        # Correlator geometry parameters
        self.NUMBER_OF_CORRELATORS = self.fpga.NUMBER_OF_CORRELATORS
        self.NUMBER_OF_CORRELATED_CHANNELS = self.fpga.NUMBER_OF_INPUTS_TO_CORRELATE

        # derived parameters
        self.NUMBER_OF_CMACS_PER_CORRELATOR = 2 * (self.NUMBER_OF_CORRELATED_CHANNELS + 1)  # per correlator
        self.PRODUCTS_PER_BIN = self.NUMBER_OF_CORRELATED_CHANNELS // 4  # per CMAC

        for corr in self.corr:
            corr.init()

    def status(self):
        """ Displays the status of all correlators"""
        for corr in self.corr:
            corr.status()

    def get_params(self):
            return Namespace(
                number_of_correlators=self.fpga.NUMBER_OF_CORRELATORS,  # hard coded in firmware
                number_of_correlated_inputs=self.fpga.NUMBER_OF_INPUTS_TO_CORRELATE,  # hard coded in firmware
                number_of_bins_per_frame=self.fpga.CROSSBAR.BIN_SEL[0].NUMBER_OF_SELECTED_WORDS  # depends on crossbar configuration
                )

    def start_correlator(
            self,
            integration_period=16384,
            autocorr_only=False,
            correlators=None,
            bins_per_frame=128,
            bandwidth_limit=0.5e9,
            verbose=1):
        """ Start the correlator with specified parameters.

        """

        if correlators is None:
            correlators = list(range(self.fpga.NUMBER_OF_CORRELATORS))

        Ncorr = len(set(correlators))  # Number of active correlators
        Ncmac = 4 if autocorr_only else self.NUMBER_OF_CMACS_PER_CORRELATOR
        # Compute number of products. Assumes the CROSSBAR is setup this way...
        Nprod = self.PRODUCTS_PER_BIN * self.fpga.FRAME_LENGTH // 2 // self.fpga.NUMBER_OF_CORRELATORS
        frame_rate = self.fpga.FRAME_RATE
        integ_rate = frame_rate / integration_period
        cmac_frame_size = (42 + 12 + 5 * Nprod)  # for all specified correlators, in bytes
        all_corr_frame_size = Ncorr * Ncmac * cmac_frame_size  # for all specified correlators, in bytes

        bit_rate = integ_rate * all_corr_frame_size * 8
        min_integ_period = frame_rate / (bandwidth_limit / 8 / all_corr_frame_size)
        autocorr_only_bit_rate = integ_rate * Ncorr * 4 * cmac_frame_size
        if verbose:
            print('Integration rate: %.1f integ/s (%.3fs/integ)' % (integ_rate, 1 / integ_rate))
            print('Bit rate =%.3f Gbps' % (bit_rate / 1e9))
        if bit_rate > bandwidth_limit:
            raise ValueError(
                'The correlator setting would make it produce %.3f Gbps of data, which exceeds the specified bandwith '
                'limit of %.3f Gbps. Try using a longer integration period (%i frames min).'
                'Note that sending only the autocorrlation products with autocorr_only=True will produce %.3f Gbps)' % (
                    bit_rate / 1e9,
                    bandwidth_limit / 1e9,
                    min_integ_period,
                    autocorr_only_bit_rate / 1e9))

        self.fpga.set_corr_reset(1)
        for i, corr in enumerate(self.corr):
            corr.SOFT_RESET = 1  # make sure we stop sending readouts in progres
            corr.INTEGRATION_PERIOD = integration_period - 1
            corr.AUTOCORR_ONLY = autocorr_only
            corr.BINS_PER_FRAME = bins_per_frame - 1
            corr.SOFT_RESET = i not in correlators
        self.fpga.set_corr_reset(0)

    def stop_correlator(self):
        """ Stop all correlator cored from sending data.
        """
        for corr in self.corr:
            corr.SOFT_RESET = 1

###################################################
# Functions to support correlator data processing
###################################################


def get_raw_corr_map(N=16, Nbins=128, Ncorr=8):
    """
    Creates a map that maps a correlator frame array indexed by (correlator_number,
    cmac_number, product_number) into a n array index (bin_number, i, j).

    This map can be used to remap the raw correlator packets contents into a more usefully indexed array.

    Parameters:

        N (int): number of input channels that are being correlated. This must
            match the number of correlators for which the firmware correlator was
            implemented.

        Nbins (int): Number of frequency bins provided to each correlator per frame.

        Ncorr: Number of correlator cores, each of which processing a different set of `Nbins` frequency bins.


    Returns:

        raw_to_matrix_map, raw_to_vector_map

        where

            - raw_to_matrix_map is a numpy array of shape (3, NBINS, NCHAN,
              NCHAN) where each element [:, bin_number, i, j] returns the
              tuple ``(corr_number, cmac_number, prod_number)`` so that::

                raw_data(raw_to_matrix[0], raw_to_matrix[1], raw_to_matrix[2])

              or equivalently::

                raw_data(tuple(raw_to_matrix))

              extracts the raw data and reorders it in a matrix that represents (freq_bin_number, i, j)

            - raw_to_matrix_map is a numpy array of shape (3, NBINS, NPROD)
              where each element [:, bin_number, prod] returns the tuple
              ``(corr_number, cmac_number, prod_number)`` and can be used to
              index raw data as above.


    The basic correlator structure is made of a fixed array (Y) of N samples
    and a rotating array (X) of also N samples. On each clock, we rotate X,
    but Y is fixed.

    We treat the diagonal products (autocorrelations) separately from the
    upper triangle, with their own multipliers. This way, we can produce only
    autocorrelations if needed.

    The upper triangle consist of N*(N-1)/2 products. We can compute the
    products in N/2 clocks using N-1 multipliers. On the first clock, the N-1
    multipliers compute the N-1 products that have a lag of 1 (e.g ((1,0),
    (2,1) ...). On the second clock, we compute the N-2 products with a lag of
    2, and 1 product with a lag of N-1, so we keep all N-1 multipliers busy.
    On clock N/2, we compute the N/2 products with lag N/2 and N/2-1 product
    with lag N/2+1.

    We need two multipliers to also compute the N autocorrelations in N/2
    clocks. One multiplier proguce the products for (0,0), (1,1), ... (N/2-1,
    N/2-1), while the second multiplier produce the products of (N/2, N/2) ...
    (N-1, N-1).

    In total, we use N-1 multipliers for the upper triangle, and 2 multipliers
    for the diagonal, with a total of N+1 multipliers per correlator.

    CMAC 0 is for the lower channel autocorrelations, CMAC1 for the upper
    channel autocorrelations, and CMAC 2 ... N are for the upper triangle
    computations.

    In practice, the data arrives 4 samples at a time (four (4+4) bit complex
    numbers in a 32-bit word). This means that a new data set arrives every
    N/4 clock. The example given above takes N/2 clocks to compute all
    products, which is 2x too slow.  To solve the problem we double the number
    of multipliers to 2*(N+1), and products are computed two at a time. The
    extra set of multipliers is interleaved with th eoriginal ones, meaning
    that the data will come out as if we were computing corr0:clk0,
    corr0:clk1, corr1:clk0, corr1:clk1  etc.

    For the computation below, we assume a non-interleaves correlator that
    compute the products with N+1 multipliers in N/2 clocks, and then apply
    the interleving operation to get the results of a 2*(N+1) multipliers
    computing in N/4 clocks.

    Each multiplier therefore shall store N/4 products per frequency bin (one product per clock). The
    accumulator can store a total of 512 products, or 512/(N/4) frequency bins
    (128 bins for N=16). If the channelizer produce 1024 frequency bins, we
    need 1024/512*N/4=N/2 correlators to process all the products (8 correlators for N=16).

    The corner-turn engine will be typically set-up to distribute 1/8th of the
    bins to each correlator in a round robin fashion, i.e. correlator 0 has
    bins 0, 8, 16, 24 ..., while correlator 1 has bins 1, 9, ...

    The CMAC products are read out in the reverse order than they were written.

    Data formats:

        raw: [CORR, CMAC, PROD]
        matrix: [BIN, ch_i, ch_j]
        vector: [bin, product]

    The `raw` format is as close to the correlator output format, and is
    processed just enough to allow the data to be usable (i.e, complex numbers
    are extracted, and specific bin/product can be indexed directly)


    """
    if N <4 or N % 4:
        raise ValueError('The number of channels must be a multiple of 4 with this correlator architecture')

    # N = NCHAN

    # Derived parameters
    NCHAN = N
    NCLOCKS_PER_BIN = NCHAN // 2 // 2  # 1/2 because we process only half of the matrix, 1/2 because we interleave
    NI_CLOCKS_PER_BIN = NCHAN // 2  # Clocks per bin of a straight Non-interleaved correlator architecture using the minimal amount of CMACs
    NI_CMAC_PER_CORR = (NCHAN + 1)  # number of CMACs per correlator if computations are done in NI_CLOCKS_PER_BIN clocks
    NPROD_TOTAL = NCHAN * (NCHAN + 1) // 2  # Total number of products per correlator frame
    CMAC_INTERLEAVE_FACTOR = NI_CLOCKS_PER_BIN // NCLOCKS_PER_BIN
    NCMAC_PER_CORR = CMAC_INTERLEAVE_FACTOR * NI_CMAC_PER_CORR  # Number of interleaved CMACs per core needed to make the computations in the target number of clocks
    NBINS_PER_CMAC = NBINS_PER_CORR =Nbins   # Number of bins per CMAC. =512/4=128
    NPROD_PER_CMAC = Nbins * NCLOCKS_PER_BIN  # Number of products per CMAC, limited by BRAM size (512 x (18+18) bits for the accumulator & capture RAM)

    # Create the arrays that will be used to index the raw data into the target array
    # The first dimension is for the 3 indexes of the array (CORR, CMAC, PROD)
    # and will be used to index the raw data with::
    #
    #   raw_data(map[0], map[1], map[2])
    #
    # which will return an array with the remaining dimensions of ``map``
    #
    # We use int16 values to store indices to fit the biggest index, which is
    # the number of bins (0..1023)
    raw_to_matrix_map = np.empty((3, NBINS_TOTAL, N, N), np.int16)
    raw_to_vector_map = np.empty((3, NBINS_TOTAL, NPROD_TOTAL), np.int16)

    # We will first compute the correlator output as if we don't interleave the CMACs.
    # This means that the products for each bin are computed in N/2 clocks.

    # The vectors X & Y contain the data to be correlated.  X rotates down on each clock. Y does not.
    # Each column describe the sample number after each NI_CLOCKS_PER_BIN = N/2 clocks
    # X & Y shapes are (N, N/2)
    # X = [[0, 15, 14, 13 ... 9],
    #      [1, 0, 15, 14, ... 10],
    #      ...
    #      [15, 14, 13, 11, ... 8]]
    # Y = [[0, 0, 0,...],
    #      [1,1,1,1 ...],
    #      ...
    #      [15, 15, 15, 15]]
    X = (np.arange(N)[:, None] - np.arange(NI_CLOCKS_PER_BIN)) % N  # [i,clk]
    Y = np.tile(np.arange(N)[:, None], (1, NI_CLOCKS_PER_BIN))  # [i,clk]

    # ni_i and ni_j are i,j index of the product that are outputted by each CMAC on each clock.
    # Those have a dimension of (CMAC, clock). This covers only one bin,
    # as all bins have the same order and will be tiled later.
    # There are ordered in the order they arrive and are *written* in the CMAC
    # [(7,7), (6,6), (5,5), (4,4), (3,3), (2,2), (1,1), (0,0)]
    # [(15,15), (14,14), (13,13), (12,12), (11,11), (10,10), (9,9), (8,8)]
    # [(0,1), (0,15), (0,14), (0,13), (0,12), (0,11), (0,10), (0,9)]
    # ...
    # [(14,15), (13,15), (12,15), (11,15), (10,15), (9,15), (8,15), (7,15)]

    # Define the arrays
    ni_i = np.zeros((NI_CMAC_PER_CORR, NI_CLOCKS_PER_BIN), dtype=int)
    ni_j = np.zeros((NI_CMAC_PER_CORR, NI_CLOCKS_PER_BIN), dtype=int)

    # First handle non-rotated elements
    ni_i[0] = ni_j[0] = X[N // 2 - 1]  # autocorrelation
    ni_i[1] = ni_j[1] = X[N - 1]  # autocorrelation
    ni_i[2:] = X[:N - 1]
    ni_j[2:] = Y[1:]

    # Handle rotated-in i indices: they use different indices and are complex conjugate
    for clock in range(NI_CLOCKS_PER_BIN):
        ni_j[2:2 + clock, clock] = Y[:clock, clock]

        # These products use the opposite complex conjugate in the fpga, so
        # swap the axis. This keeps the pairs in the upper triangle.

        tmp = ni_i[2:2 + clock, clock].copy()  # make sure we make a copy, not just a view
        ni_i[2:2 + clock, clock] = ni_j[2:2 + clock, clock]
        ni_j[2:2 + clock, clock] = tmp

    # Now interleave the computations by doubing the CMACs so we compute the products in N/4 instead of N/2.
    i_i = np.zeros((NCMAC_PER_CORR, NCLOCKS_PER_BIN), dtype=int)
    i_j = np.zeros((NCMAC_PER_CORR, NCLOCKS_PER_BIN), dtype=int)
    for i in range(2):
        i_i[i::2] = ni_i[:, i::2]
        i_j[i::2] = ni_j[:, i::2]

    # Create an array that identify the bin index for each product coming out of each CMAC (after interleaving)
    # The bin index represented the order of the bin in the packet, no the actual bin number.
    # This is [0,0,0,0,1,1,1,1,2,2,2,2,...127,127,127,127]
    # This is the same index for each CMAC
    # cmac_bin_index shape is (NCMAC_PER_CORR, NPROD_PER_CMAC)
    cmac_bin_index = np.tile(np.arange(NBINS_PER_CMAC, dtype=int), (NCMAC_PER_CORR, 1)).repeat(NCLOCKS_PER_BIN, axis=1)

    # replicate the i_i and i_j matrix index for each bin.
    # ii_i and ii_j shape is (NCMAC_PER_CORR, NPROD_PER_CMAC)
    ii_i = np.tile(i_i, (1, NBINS_PER_CMAC))
    ii_j = np.tile(i_j, (1, NBINS_PER_CMAC))

    # Reverse readout order of the products.
    # Products are read back the opposite order they are written.
    cmac_bin_index = np.fliplr(cmac_bin_index)
    ii_i = np.fliplr(ii_i)
    ii_j = np.fliplr(ii_j)

    # Product number, in the order they are received
    # [0, 1, 2... 511]

    corr_vector = np.arange(Ncorr, dtype=int)
    cmac_vector = np.arange(NCMAC_PER_CORR, dtype=int)
    prod_vector = np.arange(NPROD_PER_CMAC, dtype=int)

    shape = (Ncorr, NCMAC_PER_CORR, NPROD_PER_CMAC)
    corr_matrix = np.broadcast_to(corr_vector[:, None, None], shape)
    cmac_matrix = np.broadcast_to(cmac_vector[None, :, None], shape)
    prod_matrix = np.broadcast_to(prod_vector[None, None, :], shape)

    # Compute the bin number that correspond to each bin index.
    # By default, each correlator gets 1/Ncorr of the bins, so the bin_number
    # is bin_index * Ncorr, offset by the correlator number.
    # [1016,1016,1016,1016, 1008,1008,1008,1008, ... 0,0,0,0]
    # [1016,1016,1016,1016, 1008,1008,1008,1008, ... 0,0,0,0]
    # ...
    # freq_bin shape is (NCORR, NCMAC_PER_CORR, NPROD_PER_CMAC)
    freq_bin = corr_matrix + Ncorr * cmac_bin_index[None, :, :]

    # Assign the (corr,cmac,prod) numbers to each (bin,i,j). We had to convert
    # the right hand size to matrices because numpy was confused on how to
    # broadcast those when they appear in a tuple.
    raw_to_matrix_map[:, freq_bin, ii_i, ii_j] = (
        corr_matrix,  # int scalar, broadcasted to all elements
        cmac_matrix,  # 1xNCMAC column, broadcasted to every product
        prod_matrix  # NPROD x NCMAC array indicating the product number
        )

    # Copy the (corr,cmac,prod) coordinate from the upper to the lower
    # triangle so the data will appear both at (i,j) and (j,i)
    i, j = np.triu_indices(N, 1)  # don't include diagonal
    raw_to_matrix_map[..., j, i] = raw_to_matrix_map[..., i, j]

    # Compute to map that convert the raw data into a linearized list of products in the order
    #
    # [(0,0), (0,1), ... (0,15), (1,1), (1,2)...(1,15), (2,2), ... (15,15)]
    #
    # It happens that np.triu_indices() returns the i and j indices exactly in
    # that order, so we use it to reindex out matrix into a linearized product
    # vector.
    i, j = np.triu_indices(N)
    raw_to_vector_map = raw_to_matrix_map[..., i, j]
    return raw_to_matrix_map, raw_to_vector_map


def get_raw_to_matrix_map():

    global raw_to_matrix_map, raw_to_vector_map
    if raw_to_matrix_map is None:
        raw_to_matrix_map, raw_to_vector_map = get_raw_corr_map()

    return raw_to_matrix_map

def get_raw_to_vector_map():

    global raw_to_matrix_map, raw_to_vector_map
    if raw_to_vector_map is None:
        raw_to_matrix_map, raw_to_vector_map = get_raw_corr_map()

    return raw_to_vector_map

# def imap(self, shape):
#     """ Return an array of shape `shape` where each element is a 3-element tuple containing the index on that element.
#     """
#     N1, N2, N3 = shape
#     im = np.zeros((N1,N2,N3, 3), int) + 65535
#     [b,i,j] = np.meshgrid(range(N1), range(N2), range(N3), indexing='ij')
#     im[...,0], im[..., 1], im[..., 2] = b, i, j
#     return im

# def reverse_map(self, m):
#     (N1, N2, N3) = m.reshape(-1, 3).max(axis=0) + 1  # Find the maximum indices if each dimension
#     rm = np.empty((N1, N2, N3, 3), int)
#     im = self.imap(m.shape[:-1])
#     rm[m[..., 0], m[..., 1], m[..., 2]] = im
#     rm[m[..., 0], m[..., 2], m[..., 1]] = im  # also populate j,i with same values
#     return rm


class CorrFrameReceiver(object):
    """
    Pure Python socket receiver to capture the data from the FPGA-based
    16-channel full N-square firmware correlator and integrates it in real
    time.

    Note that the packets are larger than 1500 bytes, which requires the
    networking equipment and the computer interface to be configured to
    receive Jumbo frames.

    The receiver is fast enough to capture data that is integrated in firmware
    down to a rate of about 10 ms/integrated frame, that is, exceeding 500
    Mbits/s, provided the system provides a sufficiently big UDP buffer to hold
    the data until the receiver method is called to process it (see below).

    The receiver can perform real-time software integration of the data. A
    specific number of software-integrated frames can be returned, or data can
    be saved to disk indefinitely until stopped.

    Parameters:

        socket (socket.socket): An opened and bound UDP socket to which the
            FPGA correlator data will be sent. The socket will not be closed
            when the call is completed.

    System Requirements:

    The transmit rate must be fast enough to accommodate the desired bandwidth
    by setting ``ib.GPIO.HOST_FRAME_READ_RATE = rate``. ``rate`` =16 limits to about
    260 Mbps but is slow enough to allow python to process the data with a
    small standard UDP buffer. ``rate`` =15 is good for about 500 Mbps, and
    ``rate`` =16 is good for the full Gigabit bandwidth. The latetr two require
    bigger UDP buffers. See below::

        ib.GPIO.HOST_FRAME_READ_RATE = 14

    The Ethernet interface must be set to receive Jumbo frames::

        sudo ifconfig eno1 mtu 9000

    The UDP buffers shall be increased to reduce packet loss to a minimum::

        sudo sysctl -w net.core.rmem_max=26214400
        sudo sysctl -w net.core.rmem_default=26214400
        sudo sysctl -w net.ipv4.udp_mem='26214400 26214400 26214400'
        sudo sysctl -w net.ipv4.udp_rmem_min=26214400

    Check udp buffers::

        sysctl -a | grep mem

    Monitor UDP buffer::

        watch -cd -n .5 "grep :A6  /proc/net/udp"
    """

    def __init__(self, socket, N=16, Nbins=128, Ncorr=8, packets_per_chunk=1*34*8, ignore_packet_size=False):

        NCHAN = N
        NCORR = Ncorr

        NCLOCKS_PER_BIN = NCHAN // 2 // 2  # 1/2 because we process only half of the matrix, 1/2 because we interleave
        NI_CLOCKS_PER_BIN = NCHAN // 2  # Clocks per bin of a straight Non-interleaved correlator architecture using the minimal amount of CMACs
        NI_CMAC_PER_CORR = (NCHAN + 1)  # number of CMACs per correlator if computations are done in NI_CLOCKS_PER_BIN clocks
        NPROD_TOTAL = NCHAN * (NCHAN + 1) // 2  # Total number of products per correlator frame
        CMAC_INTERLEAVE_FACTOR = NI_CLOCKS_PER_BIN // NCLOCKS_PER_BIN
        NCMAC_PER_CORR = CMAC_INTERLEAVE_FACTOR * NI_CMAC_PER_CORR  # Number of interleaved CMACs per core needed to make the computations in the target number of clocks
        NBINS_PER_CMAC = NBINS_PER_CORR = Nbins   # Number of bins per CMAC. =512/4=128
        NPROD_PER_CMAC = Nbins * NCLOCKS_PER_BIN  # Number of products per CMAC, limited by BRAM size (512 x (18+18) bits for the accumulator & capture RAM)

        self.NCHAN = NCHAN
        self.socket = socket
        self.NPACKETS = packets_per_chunk
        self.NCORR = NCORR
        self.NCMAC = NCMAC_PER_CORR
        self.NPROD = NPROD_PER_CMAC
        self.PACKET_SIZE = NBYTES_PER_HEADER + NPROD_PER_CMAC * NBYTES_PER_PROD
        self.ignore_packet_size = ignore_packet_size
        # Define numpy data types that will be used to efficiently parse the data
        self.product_dtype = np.dtype(dict(
            names=['sat', 'h', 'l'],
            offsets=[4, 1, 0],
            formats=['u1', '<i4', '<i4']))

        self.packet_dtype = np.dtype([
            ('cookie', np.uint8),
            ('proto', np.uint8),
            ('corr', np.uint8),
            ('cmac', np.uint8),
            ('geometry', '<u4'),
            ('ts', '<u4'),
            ('data', self.product_dtype, (self.NPROD, ))])

        # Pre-allocate buffers in which recv_into() will put the received data
        # directly. We could use empty() to save some cycles, but the
        # uninitialized data can be confusing for debugging
        self.buf = np.zeros((self.NPACKETS, self.PACKET_SIZE), dtype=np.uint8)

        # Various views of the buffer to allow quick and easy access to the
        # packet contents. This does not cause new memory allocations.
        self.buf_struct = self.buf.view(self.packet_dtype)[:, 0]  # (NPACKETS,)
        self.buf_data_h = self.buf_struct['data']['h']  # (NPACKETS, NPROD)
        self.buf_data_l = self.buf_struct['data']['l']  # (NPACKETS, NPROD)
        self.buf_data_sat = self.buf_struct['data']['sat']  # (NPACKETS, NPROD)
        self.buf_ts = self.buf_struct['ts']  # (NPACKETS,)
        self.buf_corr = self.buf_struct['corr']  # (NPACKETS,)
        self.buf_cmac = self.buf_struct['cmac']  # (NPACKETS,)

        # Initialize variables used by the packet receiver
        self.n = 0  # number of packets currently stored in the buffer
        self.last_ts = None  # timestamp of the last packet written in the buffer

        # Pre-allocate temporary storage to extract the real/imaginary part from the 5-byte packed product
        self.temp32 = np.empty((self.NPACKETS, self.NPROD), dtype=np.int32)

        # Pre-compute the remapping vectors that will be used to convert the
        # raw integrated results (Nresults, NCORR, NPROD) into more palatable
        # arrays
        self.raw_to_matrix_map, self.raw_to_vector_map = get_raw_corr_map(N=N, Nbins=Nbins, Ncorr=Ncorr)

    def flush(self, timeout=0.001, timestamp_jump_threshold=2):
        """ Flush the UDP buffer until the timout occurs or the packet timestamp jumps by more `threshold` or more.

        This function clears the local software buffer.

        If a timestamp jump is detected, we assume that we are now reading the
        part of a frame that could fit in the UDP buffer because we started
        flushing it. The frame is likely partial. For this reason, onece we
        detect a large jump, we continue flushing until the next timestamp
        arrives. This assumes that the packets will arrive grouped by timestamps number.

        The first packet with a new timestamp following a timestamp jump is left on top of the buffer.
        """
        print('Flushing UDP buffer...')

        old_timeout = self.socket.gettimeout()
        self.socket.settimeout(timeout)
        flushed_bytes = 0
        flushed_packets = 0
        self.n = 0
        self.last_ts = None
        jump = timestamp_jump_threshold
        while True:
            try:
                s = self.socket.recv_into(self.buf[0])
                if self.buf_struct['cookie'][0] != 0xbf:
                    continue
                ts = self.buf_ts[0]
                if ts != self.last_ts:
                    if self.last_ts is not None and ts-self.last_ts >= jump:
                        if jump == 1:
                            print('    Flushing stopped because we found a timestamp jump from %i to %i' % (
                                self.last_ts,
                                ts))
                            self.last_ts = ts
                            self.n = 1
                            break
                        else:
                            print('    Detected a timestamp jump from %i to %i. '
                                  'Now flushing the rest of the packets with the same timestamp' % (
                                    self.last_ts,
                                    ts))
                            self.last_ts = ts
                            jump = 1
                            continue
                    print(('    Flushing correlator timestamp %i' % ts))
                    self.last_ts = ts
                flushed_packets += 1
                flushed_bytes += s
            except socket.timeout:
                print('    Flushing stopped because no data has been received for the timeout period ')
                break
        self.socket.settimeout(old_timeout)
        print(('Flushed %i UDP packets in total (%.1f kbytes)' % (flushed_packets, flushed_bytes / 1024.)))

    def align(self):
        """
        Flush packets until we receive the packet that is part of the first
        frame of the specified integration period.

        This first packet is left in the buffer.
        """
        # If the first packet in the buffer is already on an integration
        # boundary, we don't need to drop packets to align
        if self.n and not (self.buf_ts[0] % self.soft_integ_period):
            print("align: We're already aligned, no need to flush packets!")
            return

        print('Waiting for first frame of the specified integration period')
        while True:
            try:
                self.socket.recv_into(self.buf[0])
                if self.buf_struct['cookie'][0] != 0xbf:
                    continue
                ts = self.buf_ts[0]
                if ts != self.last_ts:  # we have a new timestamp
                    self.last_ts = ts
                    integ_index = ts % self.soft_integ_period
                    if integ_index == 0:  # if the new frame is on an integration period
                        self.n = 1
                        break
                    print('   Discarding correlator timestamp %i (integration index %i/%i)' % (
                            ts,
                            integ_index,
                            self.soft_integ_period))
            except socket.timeout:
                continue


    def read_corr_frames(
            self,
            soft_integ_period=1,
            number_of_results=1,
            filename=None,
            flush=True,
            align=True,
            data_timeout=0.001,
            flush_timeout=0.001,
            return_format='raw'):
        """ Read correlator frames

        Parameters:

            number_of_results (int): Number of software-integrated frames to
                acquire and return. If a `filename` is specified, only the
                last frame is returned. Also only if `filename` is specified,
                a `number_of_results` =None will result in indefinite data
                capture until the capture is stopped.

            soft_integ_period (int): Number of correlator frames to
                accumulate in software. A software frame will always be
                aligned to a multiple of soft_integ_period.




        The receiver can do software integration for unlimited time at a firmware integration period of 5000 frames (12.8 ms).


        """
        # integration_period = self.CORR[0].INTEGRATION_PERIOD + 1
        # integration_time = 2.56e-6 * integration_period

        # average_data_rate = (NCORR * NCMAC * (42 + PACKET_SIZE) * 8) / integration_time
        # min_transmit_time = (NCORR * NCMAC * (42 + PACKET_SIZE) * 8) / 1e9
        # integ_time = max(integ_time, integration_time)
        # expected_chunks = int(integ_time * NCORR * NCMAC / integration_time / self.NPACKETS)
        # expected_packets = expected_chunks * self.NPACKETS
        # expected_corr_frames = expected_packets / (NCORR * NCMAC)

        # print 'Correlator is sending data at %.3f Gb/s, correlator frame period= %i channelizer frames = %.3f ms, minimum transmit time = %.3f' % (average_data_rate/1e9, integration_period, integration_time*1000, min_transmit_time*1000)
        # print 'We expect around %.1f packets and %.1f correlator frames in the requested integration period of %.3fs' % (expected_packets, expected_corr_frames, integ_time)

        if return_format not in ('raw', 'matrix', 'vector'):
            raise ValueError('Invalid return format "%s"' % return_format)

        self.soft_integ_period = soft_integ_period

        # Storage for the accumulated value
        self.acc_re = np.zeros((number_of_results, self.NCORR, self.NCMAC, self.NPROD), dtype=np.int64)
        self.acc_im = np.zeros((number_of_results, self.NCORR, self.NCMAC, self.NPROD), dtype=np.int64)
        print(f'acc_re shape (N_results, Ncorr, Ncmac, Nprod) = {self.acc_re.shape}')
        # Number of saturations for the real and imaginary part of each product
        self.sat = np.zeros((number_of_results, self.NCORR, self.NCMAC, self.NPROD, 2), dtype=np.int32)
        self.sat_cplx = np.zeros((number_of_results, self.NCORR, self.NCMAC, self.NPROD), dtype=np.complex64)
        # Number of packets received for each NCMAC (and therefore each
        # product). Can be used to know how many packets were lost and to
        # normalize the data
        self.count = np.zeros((number_of_results, self.NCORR, self.NCMAC), dtype=np.uint32)
        # self.ts = np.zeros((number_of_results, self.NCORR, self.NCMAC), dtype=np.uint64)

        # Complex value data results
        #
        # Each correlator frame has a (18+18) bit resolution, which is then
        # integrated for some time. Assuming the worst case of a saturatet 1
        # Gb/s link sending the maximum value if 2**17, we would get 175.8
        # correlator frames/s,  with soft integrator values of increase by 2**24.46/s. If we integrate for
        # we
        #
        # A float32 can represent integers values exactly up to 2**24, which
        # leaves room for less than one second of integration in the worst
        # case. We cannot thereofre use a complex64 value (float32+float32),
        # and thereofre use a complex128 format.
        self.data = np.zeros((number_of_results, self.NCORR, self.NCMAC, self.NPROD), dtype=np.complex128)

        # packets_per_chunk = corr_frames_per_chunk * NCORR * NCMAC

        # sock = self.get_data_socket()
        self.socket.settimeout(data_timeout)
        # chunks = 0
        timeouts = 0
        # data_timeouts = 0
        # size = 0
        # dt = 0
        # packets_per_chunk = 0

        # discard_if_incomplete = True
        # first_integ = True

        # acquire frames. Check timestamp. drop frames until we have an
        # almost full first frame. Drop frames until we get the first frame of
        # a soft frame.

        # Clear the software packet buffer
        # self.n = 0
        # self.last_ts = None

        # Flush the UDP buffer by reading data until we timeout. We assume
        # here that we can read the data fast enough to empty the buffer and
        # that no new will come  for the timeout period.
        if flush:
            self.flush(flush_timeout)

        # Make sure we have at least one packet in the buffer so we have a reference timestamp
        if not self.n:
            while True:
                try:
                    s = self.socket.recv_into(self.buf[0])
                    if self.buf_struct['cookie'][0] != 0xbf:
                        continue
                    self.n = 1
                    break
                except socket.timeout:
                    continue
        # get the timestamp
        self.last_ts = self.buf_ts[self.n-1]

        self.first_ts = 0
        # Wait for a new timestamp that is the first of an integ period
        if align:
            self.align()
        else:
            self.first_ts = self.last_ts

        current_integ = (self.last_ts - self.first_ts) // self.soft_integ_period
        integ_number = 0
        packets = 0
        timeouts = 0
        bad_packets = 0
        print(f'Accumulating software frame #{current_integ}, '
              f'starting with correlator frame number {self.last_ts} ')
        while True:
            try:
                s = self.socket.recv_into(self.buf[self.n])
            except socket.timeout:
                timeouts += 1
                continue
            if self.buf_struct['cookie'][self.n] != 0xbf:
                continue

            # Ignore packets that don't have the right length
            if s != self.PACKET_SIZE and not self.ignore_packet_size:
                bad_packets += 1
                continue
            # size += s
            packets += 1
            ts = self.buf_ts[self.n]
            # If we start a new timestamp, process what was in the buffer (if
            # any) and make sure that the new sample is at the top of the
            # buffer.
            if ts != self.last_ts:
                if self.n:
                    self.accumulate_data(self.n, integ_number, self.last_ts)
                    self.buf[0, :] = self.buf[self.n, :]
                self.n = 1
                self.last_ts = ts
                # If the timestamp change imply and integration period change,
                # increase the counter, and exit if we have all the
                # integration periods we wanted.
                integ = (ts - self.first_ts) // self.soft_integ_period
                # if the packet belongs to another integration period, update the integration ts and count
                if integ != current_integ:
                    integ_number += 1
                    current_integ = integ
                    if integ_number == number_of_results:
                        break
            # If this is the last entry in the buffer, process the data
            elif self.n == self.NPACKETS - 1:
                self.accumulate_data(self.n + 1, integ_number, self.last_ts)
                self.n = 0
            else:
                self.n += 1
            # packets += n
            # chunks += 1
            # packets_per_chunk += n
            # # z=zeros(h.shape,dtype=int64)
            # l=empty((1*8*34,512), dtype=np.int32)
            # h=a.view(t)[:,0]['data']['h'].copy();np.left_shift(h,4,h);np.right_shift(h,14,h);np.add(z,h,out=z)
        # print 'Got %i packets in %i chunks with %i timeouts total and %i data timeouts. Processing took on average %i packets/chunk at %.3f ms/chunk, %.1f bytes/packet' % (packets, chunks, timeouts, data_timeouts, float(self.NPACKETS)/chunks, (float(dt) / chunks) * 1000, float(size)/packets)
        print(('Received %i packets, %i no-data-available events' % (packets, timeouts)))
        print(('Got %.1f%% of the packets, and between %.1f%% and %.1f%% of the correlator frames' % (
                float(packets)/(self.NCORR * self.NCMAC * self.soft_integ_period * number_of_results) * 100,
                np.min(self.count)/float(self.soft_integ_period) * 100,
                np.max(self.count)/float(self.soft_integ_period) * 100)))
        # self.sat.real /= 32.
        # self.sat.imag /= 16.
        self.sat_cplx.real = self.sat[..., 0] / 32.
        self.sat_cplx.imag = self.sat[..., 1] / 16.
        self.data.real = self.acc_re
        self.data.imag = self.acc_im
        if return_format == 'raw':
            return (self.data, self.count, self.sat_cplx)
        elif return_format == 'matrix':
            m = self.raw_to_matrix_map
            matrix = self.data[:, m[0], m[1], m[2]]
            # conjugate the lower triangle
            (i, j) = np.triu_indices(self.NCHAN)
            matrix[..., j, i] = matrix[..., i, j].conjugate()
            return (matrix,
                    self.count[:, m[0], m[1]],
                    self.sat_cplx[:, m[0], m[1], m[2]])
        elif return_format == 'vector':
            print('Returning vector format')
            m = self.raw_to_vector_map
            return (self.data[:, m[0], m[1], m[2]],
                    self.count[:, m[0], m[1]],
                    self.sat_cplx[:, m[0], m[1], m[2]])

    def accumulate_data(self, number_of_packets, integ_number, ts, verbose=1):
        """ Add the data from the packets 0 to `number_of_packets` in to the
        software accumulator array for software integration number
        `integ_number`.
        """
        t1 = time.time()

        # v = a.view(t)[:n, 0]
        # hh = h[:n]

        corr = self.buf_corr[:number_of_packets]
        cmac = self.buf_cmac[:number_of_packets]
        # print 'corr=', corr
        # print 'cmac=', cmac

        # ts = self.buf_ts[:n]

        # Extract the real part (in bits 27:10 of the data_h). We shift
        # left the MSB to bit 31 and sift the lsb back down to 0 to sign
        # extend the 18-bit value result within the 32-bit word.
        np.copyto(self.temp32, self.buf_data_h)
        np.left_shift(self.temp32, 4, self.temp32)
        np.right_shift(self.temp32, 14, self.temp32)
        # Add the sign-extended value to the 64-bit accumulator.
        print(f'corr shape = {corr.shape}')
        print(f'cmac shape = {cmac.shape}')

        self.acc_re[integ_number, corr, cmac] += self.temp32[:number_of_packets]

        # Extract the real part (in bits 17:0 of the data_l).
        np.copyto(self.temp32, self.buf_data_l)
        np.left_shift(self.temp32, 14, self.temp32)
        np.right_shift(self.temp32, 14, self.temp32)
        # Add the sign-extended value to the 64-bit accumulator.
        self.acc_im[integ_number, corr, cmac] += self.temp32[:number_of_packets]

        t2 = time.time()

        # Keep track of how many packets were received for each correlator/cmac
        self.count[integ_number, corr, cmac] += 1
        # self.ts[integ_number, corr, cmac]
        # Accumulate the flags for each product by or'ing them together
        # We'll mask those later to save time
        self.sat[integ_number, corr, cmac, :, 0] += self.buf_data_sat[:number_of_packets] & 0x20
        self.sat[integ_number, corr, cmac, :, 1] += self.buf_data_sat[:number_of_packets] & 0x10

        t3 = time.time()

        if verbose:
            # print 'count=', count[0,0]
            dt1 = t2 - t1
            dt2 = t3 - t2
            dt = t3 - t1
            print(f'Processing & accumulating {number_of_packets} packets '
                  f'for software frame {integ_number} '
                  f'from correlator frame {ts} '
                  f'({(ts - self.first_ts)% self.soft_integ_period}/{self.soft_integ_period};  '
                  f'took {dt * 1000:.3f} ms ({(float(dt) / (self.NCORR * self.NCMAC) * 1000):.3f} ms/corr frame) '
                  f'({dt1 * 1000:.3f} + {dt2 * 1000:.3f} ms)')


    def plot_corr_frames(
            self,
            soft_integ_period=1,
            flush=True,
            align=False,
            data_timeout=0.001,
            flush_timeout=0.001
            ):

        import matplotlib.pyplot as plt

        # fig =  plt.figure()
        fig = plt.gcf()
        d, c, sat = self.read_corr_frames(soft_integ_period=soft_integ_period, flush=flush, align=align)
        p = plt.plot(np.arange(1024)/1024*400, d[0,:4,1,:256].real[...,::-1].flatten(order='F'))[0]

        while True:
            d, c, sat = r.read_corr_frames(soft_integ_period=soft_integ_period, flush=False, align=False)
            p.set_ydata( d[0,:4,1,:256].real[...,::-1].flatten(order='F'))
            fig.canvas.draw()
            fig.canvas.flush_events()
