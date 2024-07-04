#!/usr/bin/python
# Disable pylint Line too long (=C0301)
# pylint: disable=C0301 
'''
   plot_utils.py
    Provides plotting utilities
    
History:
    2012-07-16 : KMB : Created mostly moving functions from chFPGA
    2012-07-17 : KMB : Changed plot timestream to use much faster animation library, more to follow
    2012-07-25 JFC: Added selection of matplotlib backend based on os type so it would work on both windows and mac
    2012-09-18 JFC: Changed backend selection method to use the computer node address.
'''


import numpy as np
import matplotlib
#comment this out if Tk backend not working well  
#I havetrouble sometime with the EPD version, but works well with macports
import os
import uuid

# Selects which backend to use for matplotlib based on the system node ID.
# If not in the list, the default backend is used
# usial choices of packends are 'TkAgg' or 'Qt4Agg' 
BACKEND_SELECTOR_LIST = {
     120196411245L: 'Qt4Agg', # JFC ASUS Computer
     121377386969L: 'TkAgg',  # KMB Windows Qt4Agg works ok
     247356949117810: 'QT4Agg', # KMB mac QT4Agg, MacOSX (slow) and TkAgg also work not sure what is best
     272744403299581: 'TkAgg',  #chime DRAO machine
     255365641873525: 'QT4Agg', # McGill CHIME computer (ACER)
         }
NODE_ID = uuid.getnode()
if NODE_ID in BACKEND_SELECTOR_LIST:
    matplotlib.use(BACKEND_SELECTOR_LIST[NODE_ID])
else:
    matplotlib.use('QT4Agg')

import matplotlib.pyplot as plt
import matplotlib.animation as animation
#from core import chFPGA

def plot_TIMESTREAM_frames(chFPGA, channel=0, raw=0, flush=0):
    """ Plots incoming frames """
    #if isinstance(channels,int): # make sure that channel is a list of channels
    #    channels=[channels]
    def anim_init():
        print 'calling init'
        a = chFPGA.read_frames(raw=raw, flush=flush)
        line.set_data(range(len(a[channel])), a[channel])
        return line

    def animate(i):
        #print 'calling animate'
        a = chFPGA.read_frames(verbose=0, raw=raw, flush=flush)
        line.set_ydata(a[i])
        return line
    
    fig  = plt.figure()
    ax = fig.add_subplot(111, autoscale_on=True, xlim=(0, 2048), ylim=(-128, 128))
    a = chFPGA.read_frames(raw=raw, flush=flush)
    line, = ax.plot(range(len(a[channel])), a[channel], 'o-', lw=2)    
    ani = animation.FuncAnimation(fig, animate, channel*np.ones(500),
                                  interval=20, blit=False, init_func=anim_init)
#    ani = animation.FuncAnimation(fig, animate, 10, 
#                                   init_func=anim_init)
    plt.show()
    

def plot_TIMESTREAM_frames_multichannel(chFPGA, channels=[0], raw=0, flush=0):
    """ Plots incoming frames, expected to be a timestream """
    if isinstance(channels,int): # make sure that channel is a array of channels
        channels=np.array([channels])
    elif isinstance(channels,list):
        channels=np.array(channels)
    
    def anim_init():
        a = chFPGA.read_frames(raw=raw, flush=flush)
        for line in lineObjects:
            line.set_data(range(len(a[0])),a[0])
        return lineObjects
                
    def animate(i):
        a = chFPGA.read_frames(raw=raw, flush=flush)
        for j,line in enumerate(lineObjects):
            line.set_ydata(a[channels[j]])
        return lineObjects

    nchan = channels.size
    
    fig  = plt.figure(5, figsize=(3*nchan,4))
    
    chanIndex = np.arange(nchan)
    axObjects = range(nchan)
    lineObjects = range(nchan)
    a = chFPGA.read_frames(raw=raw, flush=flush)
    for i,ax in enumerate(axObjects):
        ax = fig.add_subplot(1,nchan,i+1, autoscale_on=False, xlim=(0, 2048), ylim=(-128, 128))
        lineObjects[i], = ax.plot(range(len(a[1])), a[1], 'o-', lw=2)
    ani = animation.FuncAnimation(fig, animate, np.ones(50),
                                      interval=20, blit=False, init_func=anim_init)
    try:
        plt.show()
    except KeyboardInterrupt:
        fig._stop()



def plot_SPECTRUM_frames(chFPGA, channels=[0], raw=0, flush=0):
    """ Plots incoming frames, expected to be fourier transformed """
    if isinstance(channels,int): # make sure that channel is a array of channels
        channels=np.array([channels])
    elif isinstance(channels,list):
        channels=np.array(channels)

    
    def anim_init():
        a = chFPGA.read_frames(raw=raw, flush=flush)
        fa.real = a[channels[0]][::2]
        fa.imag = a[channels[0]][1::2]
        for line in lineMagObjects:
            line.set_data(range(len(fa)),10*np.log10(np.abs(fa)**2+1e-2))
        for line in linePhaseObjects:
            line.set_data(range(len(fa)),np.angle(fa))
        return lineMagObjects, linePhaseObjects
    
    def animate(i):
        try:
            a = chFPGA.read_frames(raw=raw, flush=flush)
            #print "animating"
            for j,line in enumerate(lineMagObjects):
                fa.real = a[channels[j]][::2]
                fa.imag = a[channels[j]][1::2]
                line.set_ydata(10*np.log10(np.abs(fa)**2+1e-2))
                linePhaseObjects[j].set_ydata(np.angle(fa))
        except:
            pass
        return lineMagObjects,linePhaseObjects
    
    nchan = channels.size
    
    fig  = plt.figure(5, figsize=(3*nchan,4))
    
    chanIndex = np.arange(nchan)
    axMagObjects = range(nchan)
    lineMagObjects = range(nchan)
    axPhaseObjects = range(nchan)
    linePhaseObjects = range(nchan)
    a = chFPGA.read_frames(raw=raw, flush=flush)
    try:
        fa = np.empty((a[channels[0]].size)/2,dtype=np.complex64)
        fa.real = a[channels[0]][::2]
        fa.imag = a[channels[0]][1::2]
    except KeyError:
        print "didn't get a frame 0, just going to guess size and hope for later"
        fa = np.ones(1024,dtype=np.complex64)
    for i,ax in enumerate(axMagObjects):
        ax = fig.add_subplot(2,nchan,i+1, autoscale_on=True, xlim=(0, 2048), ylim=(-128, 128))
        lineMagObjects[i], = ax.plot(range(len(fa)),10*np.log10(np.abs(fa)**2+1e-2), 'o-', lw=2)
    for i,ax in enumerate(axPhaseObjects):
        ax = fig.add_subplot(2,nchan,nchan+i+1, autoscale_on=True, xlim=(0, 2048), ylim=(-128, 128))
        linePhaseObjects[i], = ax.plot(range(len(fa)),np.angle(fa), 'o-', lw=2)
    ani = animation.FuncAnimation(fig, animate, np.ones(50),
                                  interval=20, blit=False, init_func=anim_init)
    print "Starting animation"
    plt.show()
    print "Finished animation"


def plot_corr_frames(chFPGA, freqs=256, raw=0, flush=0):
    """ Plots incoming frames, expected to be a timestream """
    
    def anim_init():
        a = chFPGA.read_corr_frames( flush=flush)
        #dout = np.array([output[0],output[1],output[2],output[3],output[4]])
        #a = unscramble(dout)
        for line in lineObjects:
            line.set_data(range(len(a[0])),a[0])
        return lineObjects
                
    def animate(i):
        a = chFPGA.read_corr_frames( flush=flush)
        #dout = np.array([output[0],output[1],output[2],output[3],output[4]])
        #a = unscramble(dout)
        for j,line in enumerate(lineObjects):
            line.set_ydata(a[j])
        return lineObjects

    fig  = plt.figure()
    ncorr=10
    chanIndex = np.arange(freqs)
    lineObjects = range(ncorr)
    ax = fig.add_subplot(111, autoscale_on=True, xlim=(0, 2048), ylim=(-128, 128))
    a = chFPGA.read_corr_frames( flush=flush)
    #dout = np.array([output[0],output[1],output[2],output[3],output[4]])
    #a = unscramble(dout)
    for i in range(len(lineObjects)):
        lineObjects[i], = ax.plot(range(len(a[1])), a[1], '-', lw=2)  
    ani = animation.FuncAnimation(fig, animate, np.ones(500),
                                  interval=20, blit=False, init_func=anim_init)
#    ani = animation.FuncAnimation(fig, animate, 10, 
#                                   init_func=anim_init)
    plt.show()


def save_DATA_frames(chFPGA, channels=[0], frames=1, raw=0, flush=0, filename='data.npy'):    
    '''
        Saves data from Acquisition board to numpy array 
    '''
    data_list = []
    if isinstance(channels,int): # make sure that channel is a array of channels
        channels=np.array([channels])
    elif isinstance(channels,list):
        channels=np.array(channels)
    nchan = channels.size
    chanIndex = np.arange(nchan)
                                     
    number_of_frames=0
    try:
        while (frames==0) or (frames!=0 and number_of_frames<frames):
            try:
                a = chFPGA.read_frames(verbose=0, raw=raw, flush=flush)
                number_of_frames+=1
                if filename:
                    for chanNum in chanIndex:
                        data_list.append(a[channels[chanNum]])
                if (number_of_frames % 100) == 0:
                    print 'Captured {0} frames'.format(number_of_frames) 
            except:
                raise
    except KeyboardInterrupt:
        pass
    np.array(data_list)
    np.save(filename,data_list)

    print 'Saved {0} frames'.format(number_of_frames)

                                     
def unscramble(data):
    '''
    Assumes data is (5,512) in shape array
    writes to (10,256) shape, where the 10 
    are correlation pairs:  AA, AB,AC,AD,BB,BC,BD,CC,CD,DD
    and the 256 are frequency channels.  Will need to further combine output from 4
    Correlators to get all frequencies. Hopefully will see a pattern to put in for loop.  Also should change to 
    better support the actual data coming out
    '''
    corr_output = np.zeros((10,256), dtype=np.complex)
    corr_output[0,::2] = data[4,::4] #AA
    corr_output[0,1::2] = data[0,3::4] #AA
    corr_output[1,::2] = data[3,::4] #AB
    corr_output[1,1::2] = data[1,3::4] #AB
    corr_output[2,::2] = data[3,1::4] #AC
    corr_output[2,1::2] = data[1,2::4] #AC
    corr_output[3,::2] = data[3,2::4] #AD
    corr_output[3,1::2] = data[1,1::4] #AD
    corr_output[4,::2] = data[4,1::4] #BB
    corr_output[4,1::2] = data[0,2::4] #BB
    corr_output[5,::2] = data[2,::4] #BC
    corr_output[5,1::2] = data[2,3::4] #BC
    corr_output[6,::2] = data[2,1::4] #BD
    corr_output[6,1::2] = data[2,2::4] #BD
    corr_output[7,::2] = data[4,2::4] #CC
    corr_output[7,1::2] = data[0,1::4] #CC
    corr_output[8,::2] = data[1,::4] #CD
    corr_output[8,1::2] = data[3,3::4] #CD
    corr_output[9,::2] = data[4,3::4] #DD
    corr_output[9,1::2] = data[0,::4] #DD
    return corr_output
                                     
#legacy version may not still work.  
#def save_frames(self, filename, channels=0, frames=1, raw=0):
#    """ Save incoming frames to disk """
#    if filename:
#        file=open(filename,'w')
#    else:
#        file=None
#    
#    if isinstance(channels,int): # make sure that channel is a list of channels
#        channels=[channels]
#    
#    number_of_frames=0
#    try:
#        while (continuous==1) or (number_of_frames<frames):
#            try:
#                print 'Reading data...'
#                #sync_again=(number_of_frames==0) or bool(reset)
#                a=self.read_frames(raw=raw) #(number_of_frames==0)
#                ch1_data=a[ch1][:length]
#                number_of_frames+=1
#                file.write(np.int8(ch1_data))
#            except:
#                raise
#    except KeyboardInterrupt:
#        pass
#    if file:
#        file.close()
#    print 'Saved %i frames' % number_of_frames



if __name__ == '__main__':
    from core import chFPGA_controller
    from core import chFPGA_receiver
    ADC_TEST_MODE = 0     #  0= normal, 1= ramp, 2=pulse (1 high, 10 low)
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
    FREF = 10 # FMC Reference clock frequency
    # Create the new chFPGA object.
    c = chFPGA_controller.chFPGA_controller(adc_delay_table=ADC_DELAYS_REV2_SN0001)
    
    channels=[0]
    # source can be:  'func_zero', func_one, func_ramp, func_real_ramp, inject, adcdaq_data, adcdaq_ramp
    c.set_data_source('adcdaq_data')
    c.set_ADC_mode(channels=channels, mode='data')
    c.start_data_capture(burst_period_in_seconds=0.1, number_of_bursts=0)
    c.sync()
    
    cr = chFPGA_receiver.chFPGA_receiver()
    #plot_TIMESTREAM_frames_multichannel(cr, channels=[5], raw=0, flush=0)
    c.ANT[0].FFT.BYPASS=1
    c.ANT[0].SCALER.BYPASS=1
    plot_TIMESTREAM_frames_multichannel(cr, channels=[0,1,2,3,4,5,6,7], raw=0, flush=0)


    #c.ANT[0].SCALER.SHIFT_LEFT=1
    for channel in channels:
        c.ANT[channel].FFT.BYPASS=0
        c.ANT[channel].SCALER.BYPASS=0
        c.ANT[channel].SCALER.SHIFT_LEFT=0
        c.ANT[channel].FFT.FFT_SHIFT= 2**7 - 1


    plot_SPECTRUM_frames(cr, channels=channels, raw=0, flush=0)
    c.close()
    cr.close()
    

                                     
                                     
                                     
                                     
                                     
                                     
                                     
                                     
                                     
                                     
