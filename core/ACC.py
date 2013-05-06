#!/usr/bin/python
# Disable pylint TAB warnings (W0312) and Line too long (=C0301)
# pylint: disable=W0312,C0301 

"""
ACC.py module 
 Implements interface to the accumulator in the correlator block

 History:
 2012-07-20 JFC: created
"""

from Module import Module_base, BitField
    
class ACC_base(Module_base):
    """ Implements interface to the ACC module within the correlator block CORR_BLOCK"""
    # Create local variables for page numbers tomake the table more readable
    CONTROL = BitField.CONTROL
    STATUS = BitField.STATUS

    # Memory-mapped control registers
    RESET = BitField(CONTROL, 0x00, 7, doc="Resets the module (including the FIFO)")
    INTEGRATION_PERIOD = BitField(CONTROL, 0x04, 0, width=32, doc="Number of frames before integration starts over")
    CAPTURE_PERIOD = BitField(CONTROL, 0x08, 0, width=32, doc="Number of frames before currently integrated values are transmitted")
    PROBE_ID = BitField(CONTROL, 0x09, 0, width=8, doc="Arbitrary 8-bit number that shows in the header of the transmitted frames to identify the source")
    FRAME_CTR = BitField(STATUS, 0x00, 0, width=8, doc="Number of frames received by the module. Rolls over.")
    OVERFLOW = BitField(STATUS, 0x01, 0, doc="Overflow detected on CAPTURE FIFO.  Cleared by any reset")
    
    def __init__(self, parent, fpga_instance, port_number, module_number):
        self.parent = parent
        super(self.__class__, self).__init__(fpga_instance, port_number, module_number)
        self._lock() # Prevent accidental addition of attributes (if, for example, a value is assigned to a wrongly-spelled property)

    def reset(self):
        """ Resets the module"""
        self.pulse_bit('RESET')


    def config(self, integration_period, capture_period=None):
        """
        Configure the integration and capture period of the correlator accumulator.
        All periods are specified as a number of frames.
        """

        if integration_period <= 0 or integration_period >= (2**32)-1: 
            raise SystemError('Capture period of %i frames is out of range. Valid range is between 1 and %i' % (capture_period, 2**32-1))
        self.INTEGRATION_PERIOD = integration_period
        if capture_period is None:
            self.CAPTURE_PERIOD = integration_period
        else:
            self.CAPTURE_PERIOD = capture_period


    def init(self, **kwargs):
        """ Initialize the accumulator module"""
        #self.config(500000)
        self.PROBE_ID = 0xF0 + self.parent.instance_number
        
    def status(self):
        """ Displays the status of the accumulator module"""
        print '-------------- CORR_BLOCK[%i] data capture --------------' % self.parent.instance_number 
        print 'Integrate data over %i frames' % (self.INTEGRATION_PERIOD) 
        print ' Capture cumulated data every %i frames' % (self.CAPTURE_PERIOD) 


