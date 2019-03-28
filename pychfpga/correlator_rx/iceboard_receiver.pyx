# attempt to cythonize timestream c code.
import numpy as np


cimport numpy as np


from libc.stdlib cimport malloc, free

DEF NCOR = 8  # Number of correlator cores in firmware
DEF NCMAC = 34 # Number of CMACs in each firmware correlator core
DEF NPROD = 512 # Number of products per CMAC
DEF NHEADER = 12  # Numbe rof bytes in correlator packet header
DEF PROD_SIZE = 5  # Number of bytes per product
DEF CORR_PACKET_SIZE = NHEADER + PROD_SIZE * NPROD  # Number of bytes in a correlator packet payload (header + products)

cdef extern from "ciceboard_receiver.h":
     int cget_frames(char *filename, char *port, int ntimes_per_file, int ntimes_per_burst, int file_write_loops )

cdef extern from "ciceboard_receiver.h":
     int cget_frame(singleTime *singleTimestamp, char *port, int number_channels, int verbose)

cdef extern from "ciceboard_receiver.h":
        ctypedef struct singleTime:
              np.uint32_t   timestamp
              np.int8_t timestream[16][2048]

# Firmware correlator data receiver C definitions

cdef extern from "ciceboard_receiver.h":
    int cget_corr_frame(accFrame *frame, char *port, int verbose)

cdef extern from "ciceboard_receiver.h":
        ctypedef struct accFrame:
              np.uint32_t   timestamp
              np.int8_t     data[NCMAC][NCOR][CORR_PACKET_SIZE]
              np.int8_t     received_list[NCMAC][NCOR]




cdef class singleTimestamp:
    cdef singleTime *_s

    def __cinit__(self):
        self._s = NULL


cdef class correlatorFrame:
    cdef accFrame *_s

    def __cinit__(self):
        self._s = NULL


### The following definitions are taken verbatem from chFPGA_receiver
def raw_corr_map(Nch = 16, Ncorr = 8, FREQ_CHANNELS_MAX=1024):
        """
        map(corr, cmac, prod) = (bin, i, j)
        """
        N = Nch
        Ncmac = (N+1) # Numbe rof CMACs (before interleaving)
        Ncorr = Ncorr
        Nbins = FREQ_CHANNELS_MAX / Ncorr # Number of bins processed by each correlator
        Nprods = N/2*Nbins # total number of products in a cmac (before interleaving)
        raw_map = np.zeros((Ncorr, Ncmac, Nprods, 3), int) -1
        interleaved_raw_map = np.empty((Ncorr, Ncmac*2, Nprods/2, 3), int)


        # Compute the corelator output map as if we computed all the products for eacb bin in N/2 clocks.
        cmac = np.arange(Ncmac)
        x = np.zeros(Ncmac)
        y = np.zeros(Ncmac)
        b = np.zeros(Ncmac)

        for corr in range(Ncorr):
            for bin_number in range(Nbins):
                for clock in range(N/2):
                    prod = N/2*bin_number + clock

                    b[:] = corr + bin_number * Ncorr

                    x[0] = y[0] = N/2 - 1 - clock # 1st autocorrelator, 7x7, 6x6  ... 0x0
                    x[1] = y[1] = N - 1 - clock #+ (1 if clock % 2 else -1) # 2nd autocorrelator 15x15 .. 8x8

                    x[2:] = (cmac[0:N-1] + N - clock) % N
                    x[2:clock+2] = np.arange(clock)
                    y[2:] = cmac[0:N-1] + 1
                    y[2:2+clock] = N-clock+np.arange(clock)

                    raw_map[corr, :, prod] = np.array([b, x, y]).T  #(b, x , y)

        # Since we need to compute the products in N/4 clocks (there are 4 clocks per bin), we use two CMAC in parallel.
        # The CMACs are interleaved. We update the map to repreent this.
        interleaved_raw_map[:, 0::2] = raw_map[:, :, 0::2]
        interleaved_raw_map[:, 1::2] = raw_map[:, :, 1::2]
        return interleaved_raw_map

def imap( shape):
    """ Return an array of shape `shape` where each element is a 3-element tuple containing the index on that element.
    """
    N1, N2, N3 = shape
    im = np.zeros((N1,N2,N3, 3), int) + 65535
    [b,i,j] = np.meshgrid(range(N1), range(N2), range(N3), indexing='ij')
    im[...,0], im[..., 1], im[..., 2] = b, i, j
    return im

def reverse_map(m):
    (N1, N2, N3) = m.reshape(-1, 3).max(axis=0) + 1  # Find the maximum indices if each dimension
    rm = np.empty((N1, N2, N3, 3), int)
    im = imap(m.shape[:-1])
    rm[m[..., 0], m[..., 1], m[..., 2]] = im
    rm[m[..., 0], m[..., 2], m[..., 1]] = im  # also populate j,i with same values
    return rm


rm = reverse_map(raw_corr_map())

def capture_correlator_burst(N = 100):
    timestamps = np.empty(N, dtype=np.uint)
    data = np.empty((N, 16, 16, 1024), dtype=complex)

    for i in xrange(N):
        timestamps[i], data[i] = read_correlator_frame()

    return timestamps, data

def read_correlator_frame(raw=False, portno=38000, verbose=0, Ncmac=NCMAC, Ncorr=NCOR, num_products=NPROD):
    """
    read_correlator_frame(raw = False, portno = 38000, verbose=0, Ncmac = 34, Ncorr = 8, num_products = 512)
    Read a correlator frame from an iceboard and return the correlation triangle or raw data.
    This method will call read_raw_correlator_frame() until all CMACs and correlator frames have been received.
    """

    raw_corr_data = np.zeros((Ncorr, Ncmac, num_products), dtype=complex)*np.nan  # Dimensions are: (corr_number, cmac_number,  product_number)

    while True:
        timestamp, raw_data, received_list = read_raw_correlator_frame(udp_port=portno, verbose=verbose)
        if received_list.sum() != Ncorr * Ncmac:
            if verbose:
                print "Read frame is not full! Taking another in stead."
        else:
            break

    for i in xrange(Ncmac):
        for j in xrange(Ncorr):
            w = np.flipud((raw_data[i][j][12:].reshape(num_products, 5).view(np.uint8) * [1, 1 << 8, 1 << 16, 1 << 24, 1 << 32]).sum(-1))
            re = np.int32((w >> 18) & 0x3FFFF)
            re[(re & (1 << 17)) != 0] -= 1 << 18
            im = np.int32(w & 0x3FFFF)
            im[(im & (1 << 17)) != 0] -= 1 << 18
            v = re + 1j*im
            raw_corr_data[j, i, 0:len(v)] = v

    if raw:
        data = raw_corr_data
    else:
        data = raw_corr_data[rm[..., 0], rm[..., 1], rm[..., 2]]
        i, j = np.tril_indices_from(data[0], -1)
        data[:, j, i] = data[:, i, j].conj()  # fill the upper triangle with the conjugate of the lower

    return timestamp, np.swapaxes(data, 0, 2)

def read_raw_correlator_frame(udp_port, verbose):
    """
    Read a raw correlator frame being streamed from an iceboard.

    This calls the C-coded function cget_corr_frame, which waits for all
    NCOR*NCMAC packets to be received for the same timestamp.

    Parameters:

        udp_port (int): UDP port on which to listen to packets

        verbose (int); Level of verbosity of the functon.

    Returns:

        (timestamp, data, received_list) tuple, where:

            timestamp: 32-bit timestamp

            data[NCMAC][NCOR][CORR_PACKET_SIZE]: packet data for each correlator core and CMAC

            received_list[NCMAC][NCOR]: is 1 for every received frame
    """

    udp_port = str(udp_port)

    cdef char* port = udp_port
    cdef accFrame *singleTime_pass = <accFrame *> malloc(sizeof(accFrame))

    n_packets = cget_corr_frame(singleTime_pass, port, verbose)

    if n_packets != NCOR * NCMAC:
        raise RuntimeError('Incomplete correlator frame was returned')

    cdef np.ndarray[np.int8_t, ndim=3] timestream_data = np.zeros((NCMAC, NCOR, CORR_PACKET_SIZE), dtype=np.int8)
    cdef np.ndarray[np.int8_t, ndim=2] received_list = np.zeros((NCMAC, NCOR), dtype=np.int8)

    cdef np.uint32_t timestamp = <np.uint32_t>singleTime_pass.timestamp


    cdef int i
    cdef int j
    cdef int k
    for i in range(NCMAC):
        for j in range(NCOR):
            for k in range(CORR_PACKET_SIZE):
                timestream_data[i, j, k] = <np.int8_t>singleTime_pass.data[i][j][k]
            received_list[i, j] = <np.int8_t>singleTime_pass.received_list[i][j]

    free(singleTime_pass)
    return timestamp, timestream_data, received_list


def read_frame(udp_port, number_channels, verbose):
    '''
    Read a frame from an iceboard for a given number of channels.
    arguments:  udp_port, number_channels, verbose



    '''
    udp_port = str(udp_port)

    cdef char* port = udp_port
    cdef singleTime *singleTime_pass = <singleTime *> malloc(sizeof(singleTime))

    cget_frame(singleTime_pass, port, number_channels, verbose)

    cdef np.ndarray[np.int8_t, ndim=2] timestream_data = np.zeros((number_channels,2048), dtype=np.int8)
    cdef np.uint32_t timestamp = <np.uint32_t>singleTime_pass.timestamp

    cdef int i
    cdef int j
    for i in range(number_channels):
        for j in range(2048):
            timestream_data[i,j] = <np.int8_t>singleTime_pass.timestream[i][j]

    return {timestamp: timestream_data}

def get_frame(udp_port):
    '''
    get a 16-channel frame from an iceboard.
    arguments:  udp_port

    '''
    return read_frame(udp_port, 16, 0)


