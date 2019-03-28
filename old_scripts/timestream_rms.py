#!/usr/bin/python

"""
timestream_rms.py script 
 Script to run the 5-channel system and just print to screen the rms of the adc timestream input.
#
History:
    2012-12-04 KMB: First attempt 
"""

from pychfpga.core import chFPGA_controller
from pychfpga.core import chFPGA_receiver
import numpy as np
import time, sys, os

ADC_DELAYS_REV2_SN0001 = (
    [20,26,25,25,25,25,25,24], #CH0
    [22]*8, #CH1 
    [22,22,20,20,20,20,20,19], #CH2 
    [18]*8+[0], #CH3
    [17]*8, #CH4
    [17]*8, #CH5 
    [19,19,19,18,17,16,20,20], #CH6 
    [16]*8, #CH7
    )

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

def print_RMS(r):
    channels = [0,1,2,3,4,5,6,7]
    cont = True
    while cont:
        try:
            a = r.read_frames()
            for chan in channels:
                try:
                    sys.stdout.write("ch%d %f\n" % (chan, np.log2(a[chan].std())))
                except KeyError:
                    sys.stdout.write('Missing channnel %d\n' % (chan))
            for i in xrange(2):
                sys.stdout.write("\n")
            time.sleep(1.5)
            sys.stdout.flush()
            os.system("cls" if os.name=='nt' else 'clear')
        except KeyboardInterrupt:
            print "Stopped by User"
            cont = False
        except KeyError:
            print "key error, missing data..."
            pass    

if __name__ == '__main__':
    ADC_DELAY_TABLE = ADC_DELAYS_REV2_SN0001_KC705_FMC700 # ADC_DELAYS_REV2_SN0001 # select the table corresponding to the FMC serial number
    c = chFPGA_controller.chFPGA_controller(ip_address='10.10.10.11', port_number=41000, host_ip="10.10.10.12", adc_delay_table=ADC_DELAY_TABLE, init=1, sampling_frequency=800e6, reference_frequency=10e6) # pylint: disable=C0103
    chFPGA_config = c.get_config()
    r = chFPGA_receiver.chFPGA_receiver(chFPGA_config, ip_address='10.10.10.11', port=41001, host_ip="10.10.10.12")
    c.set_FFT_bypass(True, channels=[0,1,2,3,4,5,6,7])
    c.start_data_capture(burst_period_in_seconds=1.5, number_of_bursts=0)
    time.sleep(2)
    print_RMS(r)
    c.close()
    r.close()
    
