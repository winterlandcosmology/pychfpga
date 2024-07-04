#!/usr/bin/env python

'''
Class to test the adc by sweeping an input tone and checking the correlator output.
2015-05-07 JM: Updated to work with new ICEboard firmware/software.
'''

import numpy as np
import time, pylab
import matplotlib.font_manager 
from pychfpga.common.tests.test_BaseClass import test_BaseClass
from pychfpga.common.tests import fl6062a

class test_adc_spectrum(test_BaseClass):
    '''
     Test class for testing chFPGA behavior.  Runs through all frequency bins and checks the Power level 
     out.  
    '''
    def configure_board(self, channels=None):
        #self.fpga_ctrl.set_corr_reset(True)
        self.fpga_ctrl.set_data_source('adc', channels=channels)
        #time.sleep(1)
        self.fpga_ctrl.set_ADC_mode(mode='data')
        #time.sleep(1)
        self.fpga_ctrl.set_FFT_bypass(True, channels=channels) 
        self.fpga_ctrl.set_scaler_bypass(True)
        self.fpga_ctrl.set_gain((1,27))
        self.fpga_ctrl.set_offset_binary_encoding(0)
        self.fpga_ctrl.start_data_capture(burst_period_in_seconds=0.2, channels=channels)
        self.fpga_ctrl.sync()
        time.sleep(2)
        return

    def measure_adc_response(self,signal_generator,amplitude_dBm=-6, points_per_nyquist_band=1024, offset=0, channels=None):
        freqs_nyquest2 = np.linspace(800e6,400.390625e6,1024)
        freqs_nyquest1 = np.linspace(0,399.609375e6,1024)
        freqs_nyquest3 = np.linspace(800e6, 1199.609375e6,1024)
        freqs1 = np.concatenate([freqs_nyquest1,freqs_nyquest2])
        freqs = np.concatenate([freqs1, freqs_nyquest3]) 
        indicies1 = np.arange(freqs_nyquest1.size)
        indicies2 = np.arange(freqs_nyquest2.size)
        indicies3 = np.concatenate([indicies1,indicies2])
        indicies = np.concatenate([indicies3,indicies1])
        freqs = freqs[offset::1024/points_per_nyquist_band]
        indicies = indicies[offset::1024/points_per_nyquist_band]
        nchannels = len(channels)
        datas = np.zeros([nchannels,freqs.size], dtype=np.complex)*np.NaN
        all_timestreams = np.zeros([freqs.size,nchannels,2048])*np.NaN
        fl6062a.set_amplitude(amplitude_dBm, signal_generator)
        for i,freq in enumerate(freqs):
            #if (i % 7) == 0:
                fl6062a.set_freq(freq, signal_generator)
                time.sleep(0.5)
                #self.fpga_recv.flush()
                data_ts = self.fpga_recv.read_frames(verbose=0,flush=True)
                data = np.zeros((nchannels,1024), dtype=np.complex)*np.NaN# + 1e-8
                for j in xrange(nchannels):
                    try:
                        data[j] = np.fft.fft(data_ts[channels[j]])[:1024]
                        all_timestreams[i,j,:] = data_ts[channels[j]]
                    except KeyError:
                        print "missed data on channel " + str(channels[j])
                print freq/1e6, data[range(nchannels),indicies[i]]
                for j in xrange(nchannels):
                    datas[j,i] = data[j,indicies[i]]
        return freqs, datas, all_timestreams
        
    def plot_response(self, data_return, fname, channels=None):
        freqs = data_return[0]
        sorted_freq_indices = np.argsort(freqs)
        freqs = freqs[sorted_freq_indices]
        datas = data_return[1]
        pylab.clf()
        for i in xrange(datas.shape[0]):
            #mask = (np.abs(data) > 500 ) & (np.abs(data) < 1e6)
            pylab.plot(freqs/1e6, 20*np.log10(np.abs(datas[i, sorted_freq_indices])),'.-', label='channel'+str(channels[i]))
        pylab.xlabel('Freq (MHz)')
        pylab.ylabel('dB')
        leg_prop = matplotlib.font_manager.FontProperties(size=6)
        pylab.legend(prop=leg_prop, loc="best")
        pylab.savefig(fname + '_S21.pdf')
        #pylab.clf()

    def execute(self, fname,GPIB_ip='10.10.10.137',amplitude_dBm=-6, points_per_nyquist_band=1024, offset=0, channels=None):
        try:
            print 'configuring board'
            self.configure_board(channels=channels)
            print 'flushing'
            self.fpga_recv.flush()
            print 'start signal generator'
            signal_generator = fl6062a.GPIB(address=2, to=5, ip=GPIB_ip)
            fl6062a.set_freq(410e6,signal_generator)
            print 'getting data'
            data_return = self.measure_adc_response(signal_generator,amplitude_dBm, points_per_nyquist_band, offset, channels)
            print 'saving'
            np.savez(fname+'_S21.npz', freq=data_return[0], data=data_return[1], all_timestreams=data_return[2])
            #np.save(fname + '_analog_data.npy', data_return[1])
            #np.save(fname + "all_timestreams.npy", data_return[2])
            self.plot_response(data_return, fname, channels)
            return data_return
        except:
            self.fpga_recv.close()
            raise
