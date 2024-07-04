#!/usr/bin/env python

'''
Class with testing function for testing the adc. older version, not kept up.  Remove soon.
'''

from pychfpga.core import Inject_tools as inj
from pychfpga.common.tests.test_BaseClass import test_BaseClass
import numpy as np
import pychfpga.pffb as pfb
import pylab

class test_adc(test_BaseClass):
    '''
     Test class for testing chFPGA behavior
    '''
        
    def clean_output(self,output):
        output = np.array(output)
        output = output.reshape(output.shape[0],output.shape[-1])
        spec = np.empty((output.shape[0],output.shape[1]/2),dtype=complex)
        spec.real = output[:,::2]
        spec.imag = output[:,1::2]
        return spec
        
    def check_timestream_dc(self):
        '''
        Checks that dc level injected is what is returned.
        '''
        original_bypass = np.zeros((8,), dtype=np.int)
        for i in range(8):
            original_bypass[i] = self.fpga_ctrl.chan[i].FFT.BYPASS
            self.fpga_ctrl.chan[i].FFT.BYPASS=1
        dc_levels = range(-128,128)
        dcs = []
        for dc_level in dc_levels:
            dc_fft_out = self.inject_dc(dc_level)
            #print dc_fft_out
            print "DC level with " + str(dc_level) + " input is " + str(dc_fft_out[0])
            dcs.append(dc_fft_out[0])
        #put some overflow checks here
        for i in range(8):
            self.fpga_ctrl.chan[i].FFT.BYPASS=original_bypass[i]
        return dcs
        
    def check_fft_level(self):
        sine_amps = [16,120] #range(1,128)
        sine_freqs = np.arange(1,1024)
        spectra = []
        tone=[]
        for sine_amp in sine_amps:
            for sine_freq in sine_freqs:
                sine_fft_out = self.inject_sine(sine_amp=sine_amp, sine_freq=sine_freq, channels=[0], loops=20)
                spec = self.clean_output(sine_fft_out)
                print "Amplitude of FFT of bin " + str(sine_freq) + " with amplitude " + str(sine_amp) + " is " + str(abs(spec[0][sine_freq]))
                spectra.append(spec)
                tone.append(spec[0][sine_freq])
        return spectra, tone
    
    def check_fft_bin_shape(self):
        sine_amp = 32
        sine_freq_center = 31
        sine_freqs = np.arange(sine_freq_center-2,sine_freq_center+2,0.0025)
        spectra = []
        tone = []
        for sine_freq in sine_freqs:
            sine_fft_out = self.inject_sine(sine_amp=sine_amp, sine_freq=sine_freq, channels=[0], loops=20)
            spec = self.clean_output(sine_fft_out)
            print "Output with {0} Amp in bin {1} is {2}".format(sine_amp,sine_freq,spec[0][sine_freq_center])
            spectra.append(spec)
            tone.append(spec[0][sine_freq_center])
        tone = np.array(tone)
        xs = sine_freq_center-2 + np.arange(tone.size)*0.0025
        return xs, spectra, tone
        
    def check_fft_shifts(self):
        shifts = np.arange(11)
        specs = []
        for shift in shifts:
            self.fpga_ctrl.chan[0].FFT.FFT_SHIFT= 2**shift - 1
            data = []
            for i in range(20):
                data.append(self.inject_sine( sine_amp=16.0, sine_freq=510.0, channels=[0]))
            #print data
            data = np.array(data)
            data = data.reshape(data.shape[0],data.shape[-1])
            spec = np.empty((data.shape[0],data.shape[1]/2),dtype=complex)
            spec.real = data[:,::2]
            spec.imag = data[:,1::2]
            specs.append(spec[-1])
        return np.array(specs)       
        
    def execute(self): 
        ''' set mode to inject and get dc packets out'''
        inj.set_inject_mode(self.fpga_ctrl, self.fpga_recv)
        print self.inject_dc(0)
        print "initialized"
        #self.fpga_ctrl.chan[0].FFT.BYPASS=1
        #dcs = self.check_timestream_dc()        
        self.fpga_ctrl.chan[0].FFT.BYPASS=0
        self.fpga_ctrl.chan[0].SCALER.BYPASS=0
        self.fpga_ctrl.chan[0].SCALER.SHIFT_LEFT=0
        self.fpga_ctrl.chan[0].FFT.FFT_SHIFT= 2**7 - 1
        #spectra = self.check_fft_sine()
        x, spectra, tone = self.check_fft_bin_shape()
        xs, sim_spec = pfb.sim_pfb(taps=4, L=2048, window_function=pfb.boxcar, bin_number=31, resolution=2**20)        
        pylab.plot(x,20*np.log10(abs(tone)/abs(tone).max()))
        pylab.plot(xs,20*np.log10(abs(sim_spec)/abs(sim_spec).max()))
        pylab.xlim(x.min(),x.max())
        pylab.ylim(-60,0)
        pylab.savefig('Measured_vs_sim_binshape.pdf')
        return x, spectra, tone, xs, sim_spec

