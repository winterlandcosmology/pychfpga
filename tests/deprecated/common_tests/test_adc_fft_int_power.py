#!/usr/bin/env python

'''
Class with testing function for testing the adc.
'''

from pychfpga.core import Inject_tools as inj
from pychfpga.common.tests.test_BaseClass import test_BaseClass
import numpy as np

class test_adc_fft_int_power(test_BaseClass):
    '''
     Test class for testing chFPGA behavior.  Runs through all frequency bins and checks the Power level 
     out.  
    '''
        
    def check_fft_int_power(self):
        sine_amps = [18] #range(1,128)
        sine_freqs = np.arange(1,1024, 0.1)
        spectra = []
        powers=[]
        for sine_amp in sine_amps:
            for sine_freq in sine_freqs:
                sine_fft_out = self.inject_sine(sine_amp=sine_amp, sine_freq=sine_freq, channels=[0], loops=20)
                spec = self.clean_output(sine_fft_out)
                power_out = (np.abs(np.array(spec))**2).sum()
                print "Spectrum total power from sine with freq bin " + str(sine_freq) + " is " + str(power_out)
                spectra.append(spec)
                powers.append(power_out)
        return spectra, powers
        
        
    def execute(self): 
        ''' set mode to inject and get dc packets out'''
        inj.set_inject_mode(self.fpga_ctrl, self.fpga_recv)
        print self.inject_dc(0)
        print "initialized"
     
        self.fpga_ctrl.chan[0].FFT.BYPASS=0
        self.fpga_ctrl.chan[0].SCALER.BYPASS=0
        self.fpga_ctrl.chan[0].SCALER.SHIFT_LEFT=0
        self.fpga_ctrl.chan[0].FFT.FFT_SHIFT= 2**7 - 1
        spectra, powers = self.check_fft_int_power()

        return spectra, powers