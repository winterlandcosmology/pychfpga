#!/usr/bin/env python

'''
BaseClass for testing chime FPGA board.  
KMB:  2012-09-20, Changed to expect a dictionary from inject
KMB: 2014-jun-16 - Removed inject support
'''

from pychfpga.core import Inject_tools as inj
import numpy as np

class test_BaseClass:
    '''
     Base class for all  alg operations on chFPGA
    '''
    def __init__(self,fpga_ctrl, fpga_recv):
        '''
            The baseclass has one data member, called data. 
            It is meant to hold the results of executing the algorithm once.
            you must call alg_BaseClass.__init__(self) from your derived __init__
            method.
        '''
        self.fpga_ctrl = fpga_ctrl
        self.fpga_recv = fpga_recv

    #need to make this more robust.  Now expects a dictionary input
    def clean_output(self,output):
        out_list=[]
        for item in output.items():
            if type(item[0]) == int:
                out_list.append(item[1])
        out_list = np.array(out_list)
        out_list = out_list.reshape(out_list.shape[0],out_list.shape[-1])
        spec = np.empty((out_list.shape[0],out_list.shape[1]/2),dtype=complex)
        spec.real = out_list[:,::2]
        spec.imag = out_list[:,1::2]
        return spec

    # def inject_dc(self, dc_level=1, channels=[0,1,2,3,4,5,6,7]):
    #     '''
    #     Injects a DC level given by dc_level.  
    #     '''
    #     data = np.ones(2048)*dc_level
    #     return inj.inject(self.fpga_ctrl,self.fpga_recv, channels=channels, data=data)
        
    # def inject_sine(self, sine_amp=1, sine_freq=1.0, channels=[0,1,2,3,4,5,6,7], loops= 20):
    #     '''
    #     Injects a sine wave with amplitude sine_level and frequency in frequency bin, assumes 2048 point fft.
    #     '''
    #     t = np.arange(2048*loops)
    #     freq = sine_freq/2048.0
    #     data = sine_amp*np.sin(2.0*np.pi*freq*t)
    #     data = data.reshape(loops,2048)
    #     for datum in data:
    #         data_output = inj.inject(self.fpga_ctrl,self.fpga_recv, channels=channels, data=datum)
    #     return data_output

    def execute(self, channel ): 
        ''' This method must be declared in all derived classes 
        the derived class execute will override this one '''
        pass
