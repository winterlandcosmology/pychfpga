#!/usr/bin/env python

'''
Class with testing function for testing the adc.
'''

from pychfpga.core import Inject_tools as inj
from pychfpga.common.tests.test_BaseClass import test_BaseClass
import numpy as np

class test_adc_dc(test_BaseClass):
    '''
     Test class for testing chFPGA behavior.  Tests DC level returned is the dc level
      injected for the full range of the ADC.  more?
    '''
        
    def check_timestream_dc(self):
        '''
        Checks that dc level injected is what is returned.
        '''
        dc_levels = range(-128,128)
        dcs = []
        for dc_level in dc_levels:
            dc_out = self.inject_dc(dc_level)
            #dc_out = self.inject_dc(dc_level)
            #print dc_fft_out
            print "DC level with " + str(dc_level) + " input is " + str(dc_out[0])
            dcs.append(dc_out[0])
        #put some overflow checks here
        return dcs

    def execute(self): 
        ''' set mode to inject and get dc packets out'''
        inj.set_inject_mode(self.fpga_ctrl, self.fpga_recv, bypass_FFT=True)
        print self.inject_dc(0)
        print "initialized"
        #self.fpga_ctrl.set_FFT_bypass(True)
        dcs = self.check_timestream_dc()        
        print "Test passed??  "
        return dcs