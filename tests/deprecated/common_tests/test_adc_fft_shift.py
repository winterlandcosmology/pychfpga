#!/usr/bin/env python

'''
Class with testing function for testing the adc.  Tests FFT shift to use
'''

from pychfpga.core import Inject_tools as inj
from pychfpga.common.tests.test_BaseClass import test_BaseClass
import numpy as np
import pychfpga.pffb as pfb
import pylab

class test_adc_fft_shift(test_BaseClass):
    '''
     Test class for testing chFPGA behavior.  Checks the output while changing the shift internal
     to the FFT.  Each shift is a half/doubling of the gain
    '''
    
        
    def check_fft_shifts(self):
        shifts = np.arange(11)
        specs = []
        for shift in shifts:
            self.fpga_ctrl.chan[0].FFT.FFT_SHIFT= 2**shift - 1
            data = []
            for i in range(20):
                data.append(self.inject_sine( sine_amp=16.0, sine_freq=510.0, channels=[0]))
            #print data
            spec = self.clean_output(data)
            specs.append(spec[-1])
        return np.array(specs)       
        
    def execute(self): 
        ''' set mode to inject and get dc packets out'''
        inj.set_inject_mode(self.fpga_ctrl, self.fpga_recv)
        print self.inject_dc(0)
        print "initialized"       
        self.fpga_ctrl.chan[0].FFT.BYPASS=0
        self.fpga_ctrl.chan[0].SCALER.BYPASS=0
        self.fpga_ctrl.chan[0].SCALER.SHIFT_LEFT=0
        self.fpga_ctrl.chan[0].FFT.FFT_SHIFT= 2**7 - 1
        spectra = self.check_fft_shifts()
        return spectra