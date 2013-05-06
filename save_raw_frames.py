#!/usr/bin/python
# Disable pylint TAB warnings (W0312) and Line too long (=C0301)
# pylint: disable=W0312,C0301 

"""
save_raw_frames.py script 
Saves raw timestream data to files.   


#
History:
    2011-08-14 JFC: Created from chFPGA, which now only contains top test code.
    2011-09-09 JFC: Added global FREF 
    2011-10-11 JFC: Updated delay tables
"""

from pychime.core import chFPGA_controller
from pychime.core import chFPGA_receiver
import pychime.plot_utils as pu
import numpy as np
import time



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

def save_timestream_frames(chFPGA_receiver, channels=[0], frames=256, filename='data.npy'):    
    '''
        Saves data from Acquisition board to numpy array 
    '''
    if isinstance(channels,int): # make sure that channel is a array of channels
        channels=np.array([channels])
    elif isinstance(channels,list):
        channels=np.array(channels)
    nchan = channels.size
    data_list = np.zeros((frames,nchan,2048), dtype=np.int8)
    chanIndex = np.arange(nchan)
    #chFPGA_receiver.frame_receiver._send_every_frame.clear()     
    chFPGA_receiver.send_every_frame(False)
    number_of_frames=0
    missed = 0
    print "Starting Timestream acquisition"
    try:
        while (frames==0) or (frames!=0 and number_of_frames<frames):
            try:
                #print "trying to get a frame"
                a = chFPGA_receiver.read_frames(verbose=0)
                for chanNum in chanIndex:
                    data_list[number_of_frames,chanNum,:] = a[channels[chanNum]]
                    #data_list.append(a[channels[chanNum]])
                number_of_frames+=1
                #print "got a frame"
                if (number_of_frames % 100) == 0:
                    print 'Captured {0} frames'.format(number_of_frames) 
            except KeyError:
                print "missing a frame, skipping"
                print a
                #chFPGA_receiver.flush()
                missed += 1
                pass
            except:
                chFPGA_receiver.close()
                raise
    except KeyboardInterrupt:
        chFPGA_receiver.close()
        raise
    print "lost {0} to get {1}".format(missed, frames)
    #np.array(data_list)
    np.save(filename,data_list)

    print 'Saved {0} frames'.format(number_of_frames)

if __name__ == '__main__':        
    print '------------------------'
    print 'Raw Frame saving script'
    print 'Command line mode'
    print 'KMB'
    print '------------------------'

    # Delete previous instances of 'c' to make sure the sockets are closed. If not, the new object will not be able to open the socket.
    # pylint: disable=E0601    
    try:
        print 'Deleting previous chFPGA instances in current namespace'
        c.close() # close sockets from previous objects to free them for the new one
        r.close() # close sockets from previous objects to free them for the new one
        del c
        del r
    except NameError:
        pass

    #ADC_TEST_MODE = 0     #  0= normal, 1= ramp, 2=pulse (1 high, 10 low)
    ADC_DELAY_TABLE = ADC_DELAYS_REV2_SN0001 # select the table corresponding to the FMC serial number 

    # Create the new chFPGA object.
    c = chFPGA_controller.chFPGA_controller(ip_address='10.10.10.11', port_number=41000, adc_delay_table=ADC_DELAY_TABLE, init=1, sampling_frequency=850e6, reference_frequency=10e6) # pylint: disable=C0103
    chFPGA_config = c.get_config()
    r = chFPGA_receiver.chFPGA_receiver(chFPGA_config, ip_address='10.10.10.11', port=41001)
    c.set_FFT_bypass(True, channels=[0,1,2,3,4,5,6,7])
    c.start_data_capture(burst_period_in_seconds=0.05, number_of_bursts=0, channels=[0,1,2,3,4,5,6,7])
    n=0
    ftime = str(time.time())
    while n < 10:
        save_timestream_frames(r, channels=[0,1,2,3,4,5,6,7], frames=256, filename=ftime+'.{0:04d}.npy'.format(n))
        n+=1
    
    c.close()
    r.close()

