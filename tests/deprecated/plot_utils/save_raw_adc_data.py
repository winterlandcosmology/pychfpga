#!/usr/bin/python

"""
save_raw_adc_data.py script 
 Saves raw ADC data.
#
History:
    2014-06-12 JM: First attempt 
    
Basic instructions:
    Save this file in ch_acq. Go to ch_acq folder and run
        $ python save_raw_adc_data.py -i host_ip -l bufferlength -f folderpath -p burst_period
    
    where 
    -host_ip is the ip of the host talking to the fpga, 
    -bufferlength is the number of frames saved in each npy file 
    -folderpath is the path where the  folder with the raw data is saved
    -and burst_period is the burst period in seconds (how often the ICEboard sends frames)
"""

from pychfpga.core import chFPGA_controller
from pychfpga.core import chFPGA_receiver
import numpy as np
import logging
import datetime, sys, os
import argparse



            

#Define delays
                          
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

ADC_DELAYS_MGK7MB_REV2_MGAC08_REV2 = (
    ([13]*8,     [3]*8), #CH0
    ([7]*8,                       [3]*8), #CH1 
    ([22]*8,    [3]*8), #CH2 
    ([19]*8,                       [3]*8), #CH3
    ([15]*8,                        [3]*8), #CH4
    ([14, 13, 14, 14, 13, 14, 15, 14],    [3]*8), #CH5 
    ([18]*8,     [3]*8), #CH6 
    ([17]*8,                       [4]*8), #CH7

    ([15, 17, 15, 18, 17, 14, 17, 15],   [3]*8), #CH8
    ([16]*8,                       [4]*8), #CH9
    ([20]*8,                       [3]*8), #CH10
    ([18]*8,                     [3]*8), #CH11
    ([15]*8,                       [3]*8), #CH12
    ([18]*8,                       [3]*8), #CH13
    ([18]*8,                       [3]*8), #CH14
    ([16]*8,                       [3]*8)  #CH15
    )



def save_raw_adc_data(fpgarec,adc_channels=range(16),folderpath=None,bufferlength=100000,newfolder=True):
    '''
    Saves raw ADC for 16-ch correlator.
    -fpgarec is the chFPGA_receiver object, previously created and configured to send data
    -adc_channels is a list with the channels to read
    -folderpath is the path where the the folder with the raw data is saved (without final /)
    -The raw data (for selected channels) is saved in numpy files of bufferlength frames
    '''

    #Preliminaries
    if folderpath:
        foldername = folderpath
        if foldername[-1]=='/':
            foldername = foldername[:-1]
    else:
        foldername = os.getcwd()
    
    if newfolder:
        foldername = foldername+'/'+datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        os.makedirs(foldername) #Create folder to save files
        
 
    #Define stuff
    framelength=2048
    n_channels = len(adc_channels) # Number of ADC channels to analyze
    raw_data_array = np.zeros((framelength,n_channels),dtype=np.int8) # Array with raw data
        
    raw_data_buffer = np.zeros((bufferlength,framelength,n_channels),dtype=np.int8) # buffer that saves raw data
    
    print '\n\nData will be saved in '+foldername
    print '\nData acquisition started\n'
    t = 0
    cont = True
    fpgarec.flush()
    while cont:            
        try:
            data = fpgarec.read_frames() # Read on frame of raw ADC data or FFT data for each channel. adc_data is a dictionary
            for ch in range(n_channels): #Turn data to array
                raw_data_array[:,ch] = data[adc_channels[ch]]
                
            raw_data_buffer[t] = raw_data_array #write to buffer
            t += 1
            if t==bufferlength: #buffer full. Write to numpy file
                savetime = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                np.save(foldername+'/'+savetime, raw_data_buffer)
                print datetime.datetime.now().strftime("%Y%m%d_%H%M%S: saved ")+ str(bufferlength) +' frames in ' + savetime + '.npy' 
                t = 0               
            
        except KeyboardInterrupt:
            if t>0: #Save remaining frames
                savetime = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                np.save(foldername+'/'+savetime, raw_data_buffer[:t,:,:])
                print datetime.datetime.now().strftime("%Y%m%d_%H%M%S: saved ")+ str(t) +' frames in ' + savetime + '.npy' 
                
            print "\nAcquisition stopped by user\n"
            cont = False 
                       
        except KeyError:
            fpgarec.flush()
            print 'Could not read data'
            pass



if __name__ == '__main__':
    
    parser = argparse.ArgumentParser() 
    parser.add_argument('-f', '--folderpath', action = 'store', type=str, default=None, help='Path where the folder with the raw data is saved. If not specified, data is saved in working directory')
    parser.add_argument('-l', '--bufferlength', action = 'store', type=int, default=10000, help='Number of frames saved in each npy file.')
    parser.add_argument('-i','--host_ip', action = 'store', type=str, default='10.10.10.200', help='IP address of adapter through which the connection to the FPGA will be established. Default: lab gamma ip.')
    parser.add_argument('-p','--burst_period', action = 'store', type=float, default=0.01, help='Burst period in seconds. Default: 0.01.')
    args = parser.parse_args()                
    
    logging.basicConfig(level=logging.DEBUG, format='%(asctime)s %(name)-32s %(levelname)-10s : %(message)s')
    ADC_DELAY_TABLE = ADC_DELAYS_MGK7MB_REV2_MGAC08_REV2 # ADC_DELAYS_REV2_SN0001 # select the table corresponding to the FMC serial number
    
    c = chFPGA_controller.chFPGA_controller(ip_address='10.10.10.11', port_number=41000, adc_delay_table=ADC_DELAY_TABLE, init=1, sampling_frequency=800e6, reference_frequency=10e6, data_width=8, group_frames=2, enable_gpu_link = 1, host_ip = args.host_ip) # pylint: disable=C0103
    chFPGA_config = c.get_config()
    r = chFPGA_receiver.chFPGA_receiver(chFPGA_config, ip_address='10.10.10.11', port=41001, host_ip = args.host_ip)
    
    c.set_data_source('adc')
    c.set_adc_mode('data')
    c.set_FFT_bypass(True)
    c.set_scaler_bypass(False)
    c.set_gain((1,27))
         
    c.start_data_capture(burst_period_in_seconds=args.burst_period, number_of_bursts=0)
    c.sync()
    
    save_raw_adc_data(r, bufferlength=args.bufferlength, folderpath=args.folderpath)
    
    c.close()
    r.close()