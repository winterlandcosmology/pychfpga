#!/usr/bin/python

""" timestream_correlator_setup.py script   Script to get correlator setup to
stream just timestreams.  Then need to use chime_timestream_receiver.c to save
to hdf5 
# History:     2013-10-15 KMB: First attempt  """

from pychfpga.core import chFPGA_controller
#from pychfpga.core import chFPGA_receiver
#import numpy as np
import time, sys, os


ADC_DELAYS_REV2_SN0001_KC705_FMC700 = (
    [13,10,9,10,9,10,9,9], #CH0
    [7]*8, #CH1 
    [11,11,8,9,7,8,8,7], #CH2 
    [6]*8, #CH3
    [14]*8, #CH4
    [14]*8, #CH5 
    [13]*8, #CH6 
    [0]*8, #CH7
    )
 

if __name__ == '__main__':
    ADC_DELAY_TABLE = ADC_DELAYS_REV2_SN0001_KC705_FMC700 # ADC_DELAYS_REV2_SN0001 # select the table corresponding to the FMC serial number
    c = chFPGA_controller.chFPGA_controller(ip_address='10.10.10.11', port_number=41000, adc_delay_table=ADC_DELAY_TABLE, init=1, sampling_frequency=800e6, reference_frequency=10e6) # pylint: disable=C0103
    chFPGA_config = c.get_config()
    #r = chFPGA_receiver.chFPGA_receiver(chFPGA_config, ip_address='10.10.10.11', port=41001)
    c.set_FFT_bypass(True, channels=[0,1,2,3,4,5,6,7])
    c.start_data_capture(burst_period_in_seconds=0.02, frames_per_burst=4)
    c.set_corr_reset(1)
    time.sleep(1)
    c.set_corr_reset(1)
    time.sleep(1)
    c.sync()
    c.close()
    print "\nCorrelator set up to send only timestreams, in bursts of 4 every 0.02 seconds"

    
