#!/usr/bin/python

"""
run_corr.py script 
 Script to run the 4-channel correlator.
#
History:
    2012-09-28 KMB: First attempt 
"""

from pychime.core import chFPGA_controller
from pychime.core import chFPGA_receiver
from pychime import receiver_corr_fast
import numpy as np
import time, pylab, file_utils, os

class run_corr():
    '''
    class for running chFPGA correlator
     out.  
    '''
    def __init__(self,fpga_ctrl, fpga_recv, integration_period=1):
        '''
            The baseclass has one data member, called data. 
            It is meant to hold the results of executing the algorithm once.
            you must call alg_BaseClass.__init__(self) from your derived __init__
            method.
        '''
        self.fpga_ctrl = fpga_ctrl
        self.fpga_recv = fpga_recv
        #set bypass FFT and initial settings'''
        self.fpga_ctrl.set_FFT_bypass(False, channels=[0,1,2,3,4,5,6,7])

# WE CHANGED THIS
        for chan in range(8):
            self.fpga_ctrl.ANT[chan].FFT.FFT_SHIFT=2**5-1    

        self.fpga_ctrl.set_gain(log2_gain=1, channels=[0,1,2,3,4,5,6,7])
        self.fpga_ctrl.set_data_source('adc')
        #self.fpga_ctrl.set_ADC_mode(mode='data')
        print "set adc mode"
        time.sleep(1)
        #self.fpga_ctrl.set_corr_reset(False)
        #self.fpga_ctrl.start_data_capture(burst_period_in_seconds=0.9, number_of_bursts=0)
        #Currently 0.25s is the fastest will go with regular reciever.
        #self.fpga_ctrl.start_corr_capture(integration_period=1)
        #print "set to 1s"
        # time.sleep(3)
        # self.fpga_ctrl.start_corr_capture(integration_period=0.1)
        # print "set to 0.1s"
        # time.sleep(2)
        # self.fpga_ctrl.start_corr_capture(integration_period=0.05)
        # print "set to 0.05s"
        # time.sleep(2)
        self.integration_period=integration_period
        self.HOUR = 3600/self.integration_period
        self.fpga_ctrl.start_corr_capture(integration_period=self.integration_period)
        print "set to " + str(self.integration_period) + "s"
        time.sleep(2)
        #self.fpga_ctrl.sync()  #sync means crash!
        #self.fpga_recv.flush()



    def clean_spec(self,output):
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
        
        
    def get_data(self): 

        #spectrum = self.fpga_recv.read_frames()
        #spectrum = self.clean_spec(spectrum)
        data = self.fpga_recv.read_corr_frames()
        #dout = np.array([output[0],output[1],output[2],output[3],output[4]])
        #data = self.unscramble(dout)
        return data

    def init_file(self, fcount):
        filename = 'out'
        #if fcount == 0:
        fname=filename+str(time.time())+'.'
        print fname
        fout = open(fname+'%04i'%fcount, 'w+b')
        est_clk = 65
        acc_len = 65536 #fake for now
        file_utils.write_header(fout, est_clk, acc_len)
        return fout
    def init_housekeeping(self):
        nowtime=time.time()
        #nowtime = 1338143259.2
        basename = 'out_'+ str(nowtime)+'\\'
        #print basename
        os.mkdir(basename)
        os.chdir(basename)
        timeFileName = 'time_file.txt'
        timefile = open(timeFileName, 'w+')
        temperatureFileName = 'temperature_file.txt'
        temperaturefile = open(temperatureFileName, 'w+')
        return timefile, temperaturefile

    def convert_format(self, accumulator):
        #want 1024 int32 real, int32 imag
        ncorr, nfreq = accumulator.shape
        interleave_a = np.zeros([ncorr,2*nfreq],dtype=np.int32)
        gain = 1
        acc_real = (gain*accumulator).real.astype(np.int32)
        acc_imag = (gain*accumulator).imag.astype(np.int32)
        #interleave_a[:,::2] = acc_real
        #interleave_a[:,1::2] = acc_imag
        ## Find a better way?
        #Should be 1024 eventually
        for i in range(nfreq):
            interleave_a[:,i * 2]     = acc_real[:,i]
            interleave_a[:,i * 2 + 1] = acc_imag[:,i]
        #print interleave_a
        return interleave_a

    def execute(self):
        nfiles = 0
        #Add spectrum file as well
        try:
            timefile, temperaturefile = self.init_housekeeping()
            while nfiles < (3000):
                fileHandle = self.init_file(nfiles)
                for i in xrange(3600):
                    data = self.get_data()
                    fileHandle.write(data)
                    #interleave_a = self.convert_format(data)
                    #interleave_a = data
                    #for ia in interleave_a:
                    #    fileHandle.write(ia)
                    if (i % (1/self.integration_period)) == 0:
                        nowtime=time.time()
                        print '. ',
                        temperature = c.ADC_BOARD.AmbTemp.temperature
                        temperaturefile.write(str(temperature) + '\n' )
                        timefile.write(str(nowtime) + '\n')
                fileHandle.close()
                nfiles += 1
            timefile.close()
            temperaturefile.close()
        except KeyboardInterrupt:
            self.fpga_ctrl.close()
            self.fpga_recv.close()
            timefile.close()
            temperaturefile.close()
            fileHandle.close()
            raise

# Default data and clock line delays for the two FMC boards/ML605 combination.
# First 8 values are the delays for bits 0 to 7, 8th value is the delay for the clock line.
ADC_DELAYS_REV2_SN0001 = (
    [20,26,25,25,25,25,25,24], #CH0
    [23]*8, #CH1 
    [24,22,20,20,20,20,20,17], #CH2 
    [19]*8+[0], #CH3
    [17]*8, #CH4
    [17]*8, #CH5 
    [19,19,19,18,17,16,20,20], #CH6 
    [16]*8, #CH7
    )


ADC_DELAY_TABLE = ADC_DELAYS_REV2_SN0001 # select the table corresponding to the FMC serial number

if __name__ == "__main__":
    c = chFPGA_controller.chFPGA_controller(ip_address='10.10.10.11', port_number=41000, adc_delay_table=ADC_DELAY_TABLE, init=1, sampling_frequency=850e6, reference_frequency=10e6) # pylint: disable=C0103
    chFPGA_config = c.get_config()
    r = receiver_corr_fast.chFPGA_receiver(chFPGA_config, ip_address='10.10.10.11', port=41001)
    #c.sync() sync means crash!!!

    #channels=[0,1,2,3]
    corr = run_corr(c,r, integration_period=1)
    corr.execute()
    c.close()
    r.close()



