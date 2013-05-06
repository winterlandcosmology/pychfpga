#!/usr/bin/env python

'''
Class to test the adc by sweeping an input tone and checking the correlator output.
'''

import numpy as np
import time, pylab
from pychime.common.tests.test_BaseClass import test_BaseClass
from pychime.common.tests import fl6062a
from pychime import save_raw_frames

class test_adc_histogram(test_BaseClass):
    '''
     Test class for testing chFPGA behavior.  Runs through all frequency bins and checks the Power level 
     out.  
    '''
    def configure_board(self):
        self.fpga_ctrl.set_corr_reset(True)
        self.fpga_ctrl.set_FFT_bypass(True, channels=[0,1,2,3,4,5,6,7])
        self.fpga_ctrl.set_data_source('adc', channels=[0,1,2,3,4,5,6,7])
        self.fpga_ctrl.set_ADC_mode(mode='data')
        time.sleep(1)
        self.fpga_ctrl.start_data_capture(burst_period_in_seconds=0.1, channels=[0,1,2,3,4,5,6,7])
        #self.fpga_ctrl.sync()
        time.sleep(2)
        return

    def configure_signal_generator(self,signal_generator):
        freq = 604296875.0
        fl6062a.set_freq(freq, signal_generator)
        fl6062a.set_amplitude(10.5, signal_generator)

        
    def plot_histogram(self, filename):
        datas = np.load(filename + '.npy')
        pylab.clf()
        
        for i in xrange(8):
            pylab.hist(datas[:,i,:].flatten(), bins=256, range = (-128,127))
            pylab.title(filename + ' Channel '+str(i))
            pylab.xlim(-128,127)
            pylab.savefig(filename + '_chan' +str(i)+'.pdf')
            pylab.clf()

    def execute(self, fname ):
        try:
            self.configure_board()
            self.fpga_recv.flush()
            signal_generator = fl6062a.GPIB(address=2, to=5, ip='192.168.0.37')
            self.configure_signal_generator(signal_generator)
            filename = fname + '.npy'
            save_raw_frames.save_timestream_frames(self.fpga_recv, channels = [0,1,2,3,4,5,6,7], frames=256, filename = filename)
            self.plot_histogram(fname)
        except:
            self.fpga_recv.close()
            raise
