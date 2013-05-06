#!/usr/bin/env python

'''
Class to test the adc by sweeping an input tone and checking the correlator output.
'''

import numpy as np
import time, pylab
from pychime.common.tests.test_BaseClass import test_BaseClass
from pychime.common.tests import fl6062a

class test_adc_spectrum(test_BaseClass):
    '''
     Test class for testing chFPGA behavior.  Runs through all frequency bins and checks the Power level 
     out.  
    '''
    def configure_board(self):
        self.fpga_ctrl.set_corr_reset(True)
        self.fpga_ctrl.set_FFT_bypass(True, channels=[0,1,2,3,4,5,6,7])
        self.fpga_ctrl.set_data_source('adc', channels=[0,1,2,3,4,5,6,7])
        time.sleep(1)
        self.fpga_ctrl.set_ADC_mode(mode='data')
        time.sleep(1)
        self.fpga_ctrl.start_data_capture(burst_period_in_seconds=0.2, channels=[0,1,2,3,4,5,6,7])
        #self.fpga_ctrl.sync()
        time.sleep(2)
        return

    def measure_adc_response(self,signal_generator):
        freqs_nyquest2 = np.linspace(800e6,400.390625e6,1024)
        freqs_nyquest1 = np.linspace(0,399.609375e6,1024)
        freqs_nyquest3 = np.linspace(800e6, 1199.609375e6,1024)
        freqs1 = np.concatenate([freqs_nyquest1,freqs_nyquest2])
        freqs = np.concatenate([freqs1, freqs_nyquest3]) 
        indicies1 = np.arange(freqs_nyquest1.size)
        indicies2 = np.arange(freqs_nyquest2.size)
        indicies3 = np.concatenate([indicies1,indicies2])
        indicies = np.concatenate([indicies3,indicies1])
        datas = np.empty([8,freqs.size], dtype=np.complex)
        fl6062a.set_amplitude(5, signal_generator)
        for i,freq in enumerate(freqs):
            if (i % 7) == 0:
                fl6062a.set_freq(freq, signal_generator)
                time.sleep(0.5)
                #self.fpga_recv.flush()
                data_ts = self.fpga_recv.read_frames(verbose=0,flush=True)
                data = np.zeros((8,1024), dtype=np.complex) + 1e-8
                for j in xrange(8):
                    try:
                        data[j] = np.fft.fft(data_ts[j])[:1024]
                    except KeyError:
                        print "missed data on channel " + str(j)
                print freq/1e6, data[[0,1,2,3,4,5,6,7],indicies[i]]
                for j in xrange(8):
                    datas[j,i] = data[j,indicies[i]]
        return freqs, datas
        
    def plot_response(self, data_return, fname):
        freqs = data_return[0]
        datas = data_return[1]
        pylab.clf()
        for data in datas:
            mask = (np.abs(data) > 500 ) & (np.abs(data) < 1e6)
            pylab.plot(freqs[mask]/1e6, 10*np.log10(np.abs(data[mask])),'.')
        pylab.xlabel('Freq (MHz)')
        pylab.ylabel('dB')
        pylab.savefig(fname + '_S21_8_chan.pdf')
        pylab.clf()

    def execute(self, fname):
        try:
            self.configure_board()
            self.fpga_recv.flush()
            signal_generator = fl6062a.GPIB(address=2, to=5, ip='192.168.0.37')
            fl6062a.set_freq(410e6,signal_generator)
            filename = fname + '.npy'
            data_return = self.measure_adc_response(signal_generator)
            np.save(fname+'_freq_sweep.npy', data_return[0])
            np.save(fname + '_analog_data.npy', data_return[1])
            self.plot_response(data_return, fname)
        except:
            self.fpga_recv.close()
            raise
