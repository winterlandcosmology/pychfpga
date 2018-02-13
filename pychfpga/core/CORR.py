#!/usr/bin/python
# Disable pylint Line too long (=C0301)
# pylint: disable=C0301

"""
CORR.py module
 Implements interface to the correlator blocks

 History:
 2017-05-04 : JFC : Created
"""
# import ACC
import logging
from Module import Module_base, BitField


class CORR_core(Module_base):
    """ Implements interface to one of the correlator"""

    CONTROL = BitField.CONTROL
    STATUS = BitField.STATUS

    # Control registers
    SOFT_RESET         = BitField(CONTROL, 0x00, 7, doc="Resets this correlator core.")
    AUTOCORR_ONLY         = BitField(CONTROL, 0x00, 6, doc="Force the correlator input to be 0x10101010")
    NO_ACCUM         = BitField(CONTROL, 0x00, 5, doc="Disables accumulation - only the last result is saved")
    USER_ID            = BitField(CONTROL, 0x00, 0, width=4, doc="USER ID used in the correlator packet header")
    INTEGRATION_PERIOD = BitField(CONTROL, 0x04, 0, width=32, doc="Duration of te integration period -1")
    BINS_PER_FRAME     = BitField(CONTROL, 0x05, 0, width=8, doc="Number of frequency bins per frame")

    # Status registers
    STATUS_BYTE = BitField(STATUS, 0x00, 0, width=8, doc="Status byte")
    IN_FRAME_CTR          = BitField(STATUS, 0x01, 0, width=8, doc="Input frame counter")
    OUT_FRAME_CTR          = BitField(STATUS, 0x02, 0, width=8, doc="Output frame counter")

    def __init__(self, fpga_instance, base_address, instance_number, verbose=0):
        super(CORR_core, self).__init__(fpga_instance, base_address, instance_number)
        self.verbose = verbose
        self.logger = logging.getLogger(__name__)

    def init(self):
        """ Inisializes all modules of a correlator block."""
        # self.CH_DIST.init()
        # self.ACC.init()
        # self.SOFT_RESET = self.instance_number!=0
        self.INTEGRATION_PERIOD = 16384-1

    def status(self):
        """Displays the status of al the correlator blocks"""
        print '======= CORR.core[%i] =============' % self.instance_number
        # self.CH_DIST.status()




class CORR(object):
    """ Instantiates a container for all correlators blocks"""

    def __init__(self, fpga_instance, base_address, address_increment, verbose=0):
        self.fpga = fpga_instance
        self.verbose = verbose
        self.logger = logging.getLogger(__name__)
        self.corr = []
        for i in range(self.fpga.NUMBER_OF_CORRELATORS):
            self.corr.append(CORR_core(self.fpga, base_address + i * address_increment, i))

    def __getitem__(self, key):
        """    Returns the correlator instance specified by the index"""
        return self.corr[key]



    def init(self):
        """ Initializes all correlators"""
        self.NUMBER_OF_CORRELATED_CHANNELS = 16
        self.NUMBER_OF_CMACS_PER_CORRELATOR = 2*(self.NUMBER_OF_CORRELATED_CHANNELS + 1) # per correlator
        self.PRODUCTS_PER_BIN = self.NUMBER_OF_CORRELATED_CHANNELS / 4  # per CMAC

        for corr in self.corr:
            corr.init()


    def status(self):
        """ Displays the status of all correlators"""
        for corr in self.corr:
            corr.status()

    def start_correlator(self, integration_period=16384, autocorr_only=False, correlators=None, bandwidth_limit=0.5e9, verbose=1):
        """ Start the correlator with specified parameters.

        """

        if correlators is None:
            correlators = range(self.fpga.NUMBER_OF_CORRELATORS)

        Ncorr = len(set(correlators)) # Number of active correlators
        Ncmac = 4 if autocorr_only else self.NUMBER_OF_CMACS_PER_CORRELATOR
        Nprod = self.PRODUCTS_PER_BIN * self.fpga.FRAME_LENGTH / 2 / self.fpga.NUMBER_OF_CORRELATORS  # assumes the CROSSBAR is setup this way...
        frame_rate = self.fpga.FRAME_RATE
        integ_rate = frame_rate / integration_period
        cmac_frame_size = (42 + 12 + 5*Nprod) # for all specified correlators, in bytes
        all_corr_frame_size = Ncorr * Ncmac * cmac_frame_size # for all specified correlators, in bytes

        bit_rate = integ_rate * all_corr_frame_size * 8
        min_integ_period = frame_rate / (bandwidth_limit/8/all_corr_frame_size)
        autocorr_only_bit_rate = integ_rate * Ncorr * 4 * cmac_frame_size
        if verbose:
            print 'Integration rate: %.1f integ/s (%.3fs/integ)' % (integ_rate, 1/integ_rate)
            print 'Bit rate =%.3f Gbps' % ( bit_rate/ 1e9)
        if bit_rate > bandwidth_limit:
            raise ValueError('The correlator setting would make it produce %.3f Gbps of data, which exceeds the specified bandwith '
                             'limit of %.3f Gbps. Try using a longer integration period (%i frames min).'
                             'Note that sending only the autocorrlation products with autocorr_only=True will produce %.3f Gbps)' %
                             (bit_rate/1e9, bandwidth_limit/1e9, min_integ_period, autocorr_only_bit_rate/1e9))

        self.fpga.set_corr_reset(1)
        for i, corr in enumerate(self.corr):
            corr.SOFT_RESET = 1 # make sure we stop sending readouts in progres
            corr.INTEGRATION_PERIOD = integration_period - 1
            corr.AUTOCORR_ONLY = autocorr_only
            corr.SOFT_RESET = i not in correlators
        self.fpga.set_corr_reset(0)

    def stop_correlator(self):
        """ Stop all correlator cored from sending data.
        """
        for corr in self.corr:
            corr.SOFT_RESET = 1
