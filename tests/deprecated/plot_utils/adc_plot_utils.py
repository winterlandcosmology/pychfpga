"""
adc_plot_utils.py script 
Plotting utilities for adc data.


#
History:
    2014-07-21 Juan: First attempt
"""


import matplotlib
matplotlib.use('Agg')    # Animations work well with QT4Agg backend. But in case there is no xserver, use Agg instead
matplotlib.rcParams['toolbar'] = 'None'
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.animation as animation
import logging
from pychfpga.core import chFPGA_controller
from pychfpga.core import chFPGA_receiver
import argparse
import datetime
import os


def adc_histogram(fpgarec, frames_per_plot = 128, interval_sec = 1.):
    """
    Plots histograms for all the ADC channels. fpgarec is the chFPGA_receiver object (previously created).
    frames_per_plot is the number of frames to get (per channel) before plotting the histograms. interval_sec is the interval to refresh plots in sec.
    """
        
    def update(updateflag, fpgarec, frames_per_plot, axishandle, linehandle): #Update plots
        hist_array, std_bits = get_histogram(fpgarec, frames_per_plot) # Get new histograms
        for i in range(16):
            for rect, h in zip(linehandle[i], hist_array[i]): # Update histograms
                rect.set_height(h)                    
                
            linehandle[i].set_label('CH'+str(i)+' (%.2f bits)' %std_bits[i]) # Update rms value in legends
            axishandle[i].legend(fontsize = 12)
            histmax_i = hist_array[i].max()   
            ytop_i = axishandle[i].get_ylim()[1]        
            if (histmax_i < ytop_i/2.) or (histmax_i > ytop_i): # Signal may be either too small or out of range. Update ylim
                axishandle[i].set_ylim(top = histmax_i)

                        
    def data_gen(): # Time to update plots
        while True:
            yield 1

            
    #fpgarec.flush()
    bin_centers = np.arange(-128, 128) # x axis of histogram plots
    hist_array, std_bits = get_histogram(fpgarec, frames_per_plot) # Get histograms of all ADC channels
    fighandle = plt.figure()
    fighandle.suptitle('ADC histogram plots ('+str(frames_per_plot)+' frames per histogram)', fontsize = 14)
    axishandle = [fighandle.add_subplot(4, 4, 1), ]
    linehandle = [axishandle[0].bar(bin_centers, hist_array[0], width = 1), ]        
    axishandle[0].set_xlabel('Bin (LSB)')
    axishandle[0].set_ylabel('Frequency')
    linehandle[0].set_label('CH'+str(0)+' (%.1f bits)' %std_bits[0])
    axishandle[0].legend(fontsize = 12)
    fighandle.subplots_adjust(left=0.04, bottom=0.05, top=0.95, right=0.98, hspace=0.4)
    for i in range(1, 16): #16 channels per motherboard
        axishandle.append(fighandle.add_subplot(4, 4, i+1, sharex = axishandle[0]))
        linehandle.append(axishandle[i].bar(bin_centers, hist_array[i], width = 1)) 
        axishandle[i].set_xlabel('Bin (LSB)')
        axishandle[i].set_ylabel('Frequency')
        linehandle[i].set_label('CH'+str(i)+' (%.2f bits)' %std_bits[i])  
        axishandle[i].legend(fontsize = 12)
            
    axishandle[0].set_xlim((-128, 128))             
                    
    ani = animation.FuncAnimation(fighandle, update, data_gen, fargs = (fpgarec, frames_per_plot, axishandle, linehandle),
                                interval = 1000*interval_sec)  # create animation object
        
    plt.show()
    
    try:
        ani._stop() #This doesn't seem to work well after closing figure.
        print '\nStopping animation'
    except:
        pass
        
    return fighandle
        
        


def get_histogram(fpgarec, frames_per_plot):
    '''
    Gets histograms of all ADC channels
    '''
    
    bin_edges = np.linspace(-128.5, 127.5, 257) # Edges for histogram
    hist_array = np.zeros((16, 256), dtype = np.float) #Each row is a different ADC channel. Each histogram has 256 bins
    data_array = np.zeros((16, 2048*frames_per_plot), dtype = np.int8)
    for i in range(frames_per_plot):
        data_array[:, 2048*i:2048*(i+1)] = get_adc_frames(fpgarec)
            
    for ch in range(16):
        hist_array[ch], b = np.histogram(data_array[ch], bins = bin_edges)
            
    return hist_array/(frames_per_plot*2048.), np.log2(np.std(data_array, axis = 1))
      
    

def get_adc_frames(fpgarec):
    '''
    Gets frames for all ADC channels
    '''
    
    not_ready_flag = True
    data_array = np.zeros((16, 2048), dtype = np.int8)
    while not_ready_flag:
        try:
            adc_data_dict = fpgarec.read_frames() # Read one frame of raw ADC data for each channel. adc_data_dict is a dictionary
            for ch in range(16):
                data_array[ch] = adc_data_dict[ch]
            
            not_ready_flag = False
        
        except KeyError:
            #fpgarec.flush()
            #print 'Missing frames'
            pass
            
    return data_array
    
    
    

def adc_timestream(fpgarec, interval_sec = 1.):
    """
    Plots timestreams for all the ADC channels. fpgarec is the chFPGA_receiver object (previously created).
    interval_sec is the interval to refresh plots in sec.
    """
        
    def update(updateflag, fpgarec, axishandle, linehandle): #Update plots
        data_array = get_adc_frames(fpgarec)
        for i in range(16):
            linehandle[i].set_ydata(data_array[i])
            absmax_i = abs(data_array[i].astype(np.int16)).max()   
            ytop_i = axishandle[i].get_ylim()[1]        
            if (absmax_i < ytop_i/2.) or (absmax_i > ytop_i): # Signal may be either too small or out of range. Update ylim
                axishandle[i].set_ylim((-absmax_i, absmax_i)) 

                        
    def data_gen(): # Time to update plots
        while True:
            yield 1

            
    data_array = get_adc_frames(fpgarec) # Get one frame for all ADC channels
    fighandle = plt.figure()
    fighandle.suptitle('ADC timestream plots', fontsize = 14)
    axishandle = [fighandle.add_subplot(4, 4, 1), ]
    linehandle = [axishandle[0].plot(data_array[0])[0], ]        
    absmax_i = abs(data_array[0].astype(np.int16)).max() 
    axishandle[0].set_ylim((-absmax_i, absmax_i)) 
    axishandle[0].grid(True)
    axishandle[0].set_xlabel('Sample time')
    axishandle[0].set_ylabel('Amplitude (LSB)')
    linehandle[0].set_label('CH'+str(0))
    axishandle[0].legend(fontsize = 12)
    fighandle.subplots_adjust(left=0.04, bottom=0.05, top=0.95, right=0.98, hspace=0.4)
    for i in range(1, 16): #16 channels per motherboard
        axishandle.append(fighandle.add_subplot(4, 4, i+1, sharex = axishandle[0]))
        linehandle.append(axishandle[i].plot(data_array[i])[0]) 
        absmax_i = abs(data_array[i].astype(np.int16)).max() 
        axishandle[i].set_ylim((-absmax_i, absmax_i))
        axishandle[i].grid(True) 
        axishandle[i].set_xlabel('Sample time')
        axishandle[i].set_ylabel('Amplitude (LSB)')
        linehandle[i].set_label('CH'+str(i))  
        axishandle[i].legend(fontsize = 12)
            
    axishandle[0].set_xlim((0, 2047))             
                    
    ani = animation.FuncAnimation(fighandle, update, data_gen, fargs = (fpgarec, axishandle, linehandle),
                                interval = 1000*interval_sec)  # create animation object
        
    plt.show()
    
    try:
        ani._stop() #This doesn't seem to work well after closing figure.
        print '\nStopping animation'
    except:
        pass
        
    return fighandle
        
        


def adc_spectra(fpgarec, frames_per_plot = 128, interval_sec = 1.):
    """
    Plots spectra for all the ADC channels. fpgarec is the chFPGA_receiver object (previously created).
    frames_per_plot is the number of spectra to average (per channel). interval_sec is the interval to refresh plots in sec.
    """
        
    def update(updateflag, fpgarec, frames_per_plot, axishandle, linehandle): #Update plots
        spectra_array = get_spectra(fpgarec, frames_per_plot) # Get new spectra
        for i in range(16):
            linehandle[i].set_ydata(spectra_array[i]) 
            axishandle[i].set_ylim((10**np.floor(np.log10(spectra_array[i].min())), 10**np.ceil(np.log10(spectra_array[i].max())))) 
                        
    def data_gen(): # Time to update plots
        while True:
            yield 1

            
    spectra_array = get_spectra(fpgarec, frames_per_plot) # Get new spectra
    fighandle = plt.figure()
    fighandle.suptitle('ADC spectra plots ('+str(frames_per_plot)+' frames averaged)', fontsize = 14)
    axishandle = [fighandle.add_subplot(4, 4, 1), ]
    linehandle = [axishandle[0].semilogy(spectra_array[0])[0], ]  
    axishandle[0].grid(True)
    axishandle[0].set_xlabel('Freq. bin')
    axishandle[0].set_ylabel('Power')
    linehandle[0].set_label('CH'+str(0))
    axishandle[0].legend(fontsize = 12)
    fighandle.subplots_adjust(left=0.04, bottom=0.05, top=0.95, right=0.98, hspace=0.4)
    for i in range(1, 16): #16 channels per motherboard
        axishandle.append(fighandle.add_subplot(4, 4, i+1, sharex = axishandle[0]))
        linehandle.append(axishandle[i].semilogy(spectra_array[i])[0]) 
        axishandle[i].grid(True)        
        axishandle[i].set_xlabel('Freq. bin')
        axishandle[i].set_ylabel('Power')
        linehandle[i].set_label('CH'+str(i))  
        axishandle[i].legend(fontsize = 12)
            
    axishandle[0].set_xlim((0, 1023))                 
                    
    ani = animation.FuncAnimation(fighandle, update, data_gen, fargs = (fpgarec, frames_per_plot, axishandle, linehandle),
                                interval = 1000*interval_sec)  # create animation object
        
    plt.show()
    
    try:
        ani._stop() #This doesn't seem to work well after closing figure.
        print '\nStopping animation'
    except:
        pass
        
    return fighandle
        
        


def get_spectra(fpgarec, frames_per_plot):
    '''
    Computes power spectra of all ADC channels
    '''
    
    spectra_array = (abs(np.fft.fft(get_adc_frames(fpgarec), axis = 1))[:, :1024])**2/(2048.)**2 # Get frames, then fft, +ve freqs only, magnitude squared.
    for i in range(1, frames_per_plot):
        spectra_array += (abs(np.fft.fft(get_adc_frames(fpgarec), axis = 1))[:, :1024])**2/(2048.)**2 #Normalize by 2048 so numbers don't get too big
            
    return spectra_array        
                        
        
        
        
##########################################################          
# ADC delay table            
##########################################################          
              
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
##########################################################          
##########################################################          
                 
    
def ice_setup(burst_period_in_seconds = 0.01, host_ip = '10.10.10.200'):
    '''
    Configuring the ICEBoard to send raw ADC data
    '''
    
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)-32s %(levelname)-10s : %(message)s')
    ADC_DELAY_TABLE = ADC_DELAYS_MGK7MB_REV2_MGAC08_REV2 # ADC_DELAYS_REV2_SN0001 # select the table corresponding to the FMC serial number
    c = chFPGA_controller.chFPGA_controller(ip_address = '10.10.10.11', port_number = 41000, adc_delay_table=ADC_DELAY_TABLE, init = 1, sampling_frequency = 800e6,
                                            reference_frequency = 10e6, data_width = 8, group_frames = 2, enable_gpu_link = 1, host_ip = host_ip) # pylint: disable=C0103
    chFPGA_config = c.get_config()
    r = chFPGA_receiver.chFPGA_receiver(chFPGA_config, ip_address = '10.10.10.11', port = 41001, host_ip = host_ip)
    c.set_data_source('adc')
    c.set_adc_mode('data')
    c.set_FFT_bypass(True)
    
    c.set_scaler_bypass(False)
    c.set_gain((1,27)) 
         
    c.start_data_capture(burst_period_in_seconds = burst_period_in_seconds)
    c.sync()
    
    return c, r
    
    
    
if __name__ == '__main__':
    
    parser = argparse.ArgumentParser() 
    parser.add_argument('-f', '--folder', action = 'store', type = str, default = None, 
                        help = 'Folder where the figure is saved. If not specified and save figure is True, figure is saved in working directory.')
    parser.add_argument('-p', '--plot_type', action = 'store', type = str, default = 'histogram', choices = ('histogram', 'timestream', 'spectra'), 
                        help = 'Type of plot: histogram (default), timestream or spectra.')
    parser.add_argument('-n', '--frames_per_plot', action = 'store', type = int, default = 128, help = 'Number of frames per plot for histogram and spectra plots.')
    parser.add_argument('-t', '--interval_sec', action = 'store', type = float, default = 0.1, 
                        help = 'Interval to refresh plots in sec (in general the refresh time depends on interval_sec and burst_period).')
    parser.add_argument('-i','--host_ip', action = 'store', type = str, default = '10.10.10.200', 
                        help = 'IP address of adapter through which the connection to the FPGA will be established. Default: lab gamma ip.')
    parser.add_argument('-b','--burst_period', action = 'store', type = float, default = 0.01, help = 'Burst period in seconds. Default: 0.01.')
    args = parser.parse_args()                
    
    
    if args.folder:
        if args.folder[-1]=='/':
            figname = args.folder[:-1]
    else:
        figname = os.getcwd()
        
                
    c, r = ice_setup(burst_period_in_seconds = args.burst_period, host_ip = args.host_ip)
    print '\nICEBoard configuration successful'
    
    print '\nStarting animation. Close figure to save and terminate. Press Ctrl+C to close without saving\n'
    if args.plot_type == 'histogram':
        fighandle = adc_histogram(r, frames_per_plot = args.frames_per_plot, interval_sec = args.interval_sec) 
    elif args.plot_type == 'timestream':
        fighandle = adc_timestream(r, interval_sec = args.interval_sec) 
    else:
        fighandle = adc_spectra(r, frames_per_plot = args.frames_per_plot, interval_sec = args.interval_sec) 

             
    figname += '/'+ args.plot_type+'_test_'+datetime.datetime.now().strftime("%Y%m%d_%H%M%S")+'.png'
    print '\nSaving figure as '+figname
    fighandle.set_size_inches((24., 11.725))
    fighandle.savefig(figname, format='png')

    print '\nClosing FPGA controller and receiver objects'
    c.close()
    r.close()