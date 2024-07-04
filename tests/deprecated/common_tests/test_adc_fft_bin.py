#!/usr/bin/env python

'''
Class with testing function for testing the adc.
'''

from pychfpga.core import Inject_tools as inj
from pychfpga.common.tests.test_BaseClass import test_BaseClass
import numpy as np
#import pychfpga.pffb as pfb
import pylab

class test_adc_fft_bin(test_BaseClass):
    '''
     Test class for testing chFPGA poly-phase filter-bank FFT bin shape.  
     Looks closely at bin number 31 (), Might want to change to be configurable.
    '''
        
    
    def check_fft_bin_shape(self, channels=[0]):
        sine_amp = 32
        sine_freq_center = 31
        sine_freqs = np.arange(sine_freq_center-2,sine_freq_center+2,0.0025)
        spectra = []
        tone = []
        for sine_freq in sine_freqs:
            sine_fft_out = self.inject_sine(sine_amp=sine_amp, sine_freq=sine_freq, channels=channels, loops=20)
            spec = self.clean_output(sine_fft_out)
            print "Output with {0} Amp in bin {1} is {2}".format(sine_amp,sine_freq,spec[0][sine_freq_center])
            spectra.append(spec)
            tone.append(spec[0][sine_freq_center])
        tone = np.array(tone)
        xs = sine_freq_center-2 + np.arange(tone.size)*0.0025
        return xs, spectra, tone
            
        
    def execute(self): 
        ''' set mode to inject and get dc packets out'''
        inj.set_inject_mode(self.fpga_ctrl, self.fpga_recv, bypass_FFT=False)
        print self.inject_dc(0)
        print "initialized"
        channels = [0,1,2,3]
        for channel in channels:
            self.fpga_ctrl.chan[channel].FFT.BYPASS=0
            self.fpga_ctrl.chan[channel].SCALER.BYPASS=0
            self.fpga_ctrl.chan[channel].SCALER.SHIFT_LEFT=0
            self.fpga_ctrl.chan[channel].FFT.FFT_SHIFT= 2**7 - 1

        x, spectra, tone = self.check_fft_bin_shape(channels)
        xs, sim_spec = pfb.sim_pfb(taps=4, L=2048, window_function=pfb.boxcar, bin_number=31, resolution=2**20)
        xs_exp, sim_spec_exp = pfb.sim_pfb(taps=4, L=2048, window_function=pfb.sinc_hanning_window, bin_number=31, resolution=2**20)
        # pylab.plot(x,20*np.log10(abs(tone)/abs(tone).max()), label='data')
        # pylab.plot(xs,20*np.log10(abs(sim_spec)/abs(sim_spec).max()), label='FFT expected')
        # pylab.plot(xs_exp,20*np.log10(abs(sim_spec_exp)/abs(sim_spec_exp).max()), label='PFB expected')
        # pylab.xlim(x.min(),x.max())
        # pylab.ylim(-60,0)
        # pylab.legend()
        # pylab.savefig('Measured_vs_sim_binshape.pdf')
        return x, spectra, tone, xs, sim_spec
