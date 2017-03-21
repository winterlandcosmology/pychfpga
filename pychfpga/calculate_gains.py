#!/usr/bin/python
# Disable pylint TAB warnings (W0312) and Line too long (=C0301)
# pylint: disable=W0312,C0301

"""
calculate_gains.py script
 computes and sets ideal gain for 4bit gaussian noise.



#
History:
    2011-08-14 JFC: Created from chFPGA, which now only contains top test code.
    2011-09-09 JFC: Added global FREF
    2011-10-11 JFC: Updated delay tables
    2014-02-21 KMB: Created from top test
"""
import logging
import argparse
import time
import pickle

from pychfpga.core import chFPGA_controller
#from pychfpga.core import chFPGA_receiver
from timestream_receiver import get_frame

import numpy as np

ADC_DELAYS_MGK7MB_REV0_MGAC08_REV2 = (
    ([6,25,25,25,25,25,25,25],     [4]*8), #CH0
    ([21]*8,                       [3]*8), #CH1
    ([18,17,16,16,13,15,14,13],    [3]*8), #CH2
    ([13]*8,                       [3]*8), #CH3
    ([9]*8,                        [3]*8), #CH4
    ([14,12,12,12,12,12,12,12],    [3]*8), #CH5
    ([11,11,13,10,10,8,12,13],     [3]*8), #CH6
    ([12]*8,                       [4]*8), #CH7

    ([15, 14, 16, 14, 13, 18, 15, 15],   [4]*8), #CH8
    ([15, 19, 21, 18, 15, 18, 19, 20],                       [3]*8), #CH9
    ([18, 18, 21, 20, 20, 20, 20, 16],                       [3]*8), #CH10
    ([15, 15, 15, 14, 15, 13, 14, 15],                     [3]*8), #CH11
    ([13, 15, 16, 12, 11, 16, 16, 15],                       [3]*8), #CH12
    ([12, 12, 10, 11, 10, 11, 13, 10],                       [3]*8), #CH13
    ([13, 15, 15, 14, 12, 11, 16, 14],                       [3]*8), #CH14
    ([13, 15, 15, 17, 17, 18, 18, 14],                       [3]*8)  #CH15
    )

ADC_DELAYS_MGK7MB_REV2_MGAC08_REV2 = (
    ([16]*8,     [3]*8), #CH0
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

class GainCalc(object):
    def __init__(self, zero=False):
        self.zero = zero
        self.mask = None

    def update(self, signal):
        self.signal = np.array(signal)
        mask = np.ma.make_mask_none((len(signal),))
        #The first bin is always bad for some reason
        mask[0] = True
        self.masked = np.ma.array(np.log(signal), mask=mask)

    def fourier_filter(self, signal, num_components):
        '''
        Filters signal with top-hat in fourier space.  Padded with itself on either     side to improve edge behavior.
        Should extend to other windows.
        not assured to maintain signal size
        '''
        signal = np.array(signal)
        signal_length = signal.size
        f_signal = np.fft.fft(np.r_[signal[signal_length/2:0:-1],signal,signal[-1:-signal_length/2:-1]])
        f_signal[num_components:-num_components] = 0
        filtered = np.fft.ifft(f_signal)[signal_length/2:-signal_length/2+1]
        filtered = (filtered.real).astype(np.int).astype(np.complex)
        return filtered

    def flag_rfi(self, in_arr, fit, threshold):
        '''
        Identifies RFI in the signal spectrum by finding larger than expected jumps in the signal.
        Returns array of flags for each bin
        '''
        rfmask = abs(in_arr) < abs(fit/threshold)
        in_arr.mask = rfmask|in_arr.mask

    def poly_filter(self, signal, threshold, degree):
        '''
        Filters signal using a polynomial fit. Ignores RFI in calculating the polynomial.
        '''
        x = np.ma.array(np.arange(len(signal)), mask=signal.mask)
        fit = np.polyfit(np.ma.compressed(x), np.ma.compressed(signal), degree)
        #fit = np.polyfit(flagged, x, degree)
        fitarr = np.poly1d(fit)(np.arange(len(signal)))
        self.flag_rfi(signal, fitarr, threshold)
        return fitarr

    def iterative_poly_filter(self, signal):
        mask = np.ma.make_mask_none((len(signal),))
        #The first bin is always bad for some reason
        mask[0] = True
        degree = 1
        threshold = 1.2
        masked = np.ma.array(np.log(signal), mask=mask)
        while threshold > 1.01:
            fitarr = self.poly_filter(masked, threshold, degree)
            threshold = 1 + (threshold - 1)*0.8
            if degree < 15:
                degree += 2
        filtered = np.exp(fitarr)
        filtered = (filtered.real).astype(np.int).astype(np.complex)
        return filtered, masked.mask

    def run(self, filtertype='hybrid', num_components = 50):
        if filtertype == 'fourier':
            output = self.fourier_filter(self.signal, num_components)
        elif filtertype == 'poly' or filtertype == 'hybrid':
            output, mask = self.iterative_poly_filter(self.signal)
            self.mask = mask
            if filtertype == 'hybrid':
                in_arr = self.signal.copy()
                in_arr[mask] = output[mask]
                output = self.fourier_filter(in_arr, num_components)
            if self.zero:
                output[mask] = 0
            else:
                output[mask] = self.signal[mask]
        else:
            raise ValueError
        output = (output.real).astype(np.int).astype(np.complex)
        return output


def get_frames(port):
    chanIndex = np.arange(16)
    channels = np.arange(16)
    number_of_frames = 0
    frames = 100
    data_list = np.zeros((frames, 16, 2048))
    while number_of_frames < frames:
        try:
            a = get_frame(port)
            data_list[number_of_frames, :, :] = a.values()[0]
            #for chanNum in chanIndex:
            #    data_list[number_of_frames,chanNum, :] = a[channels[chanNum]]
            number_of_frames += 1
        except KeyError:
            pass
            print "missed some data..."
    #data_list = data_list.astype(np.int8)
    #data_list ^= np.int8(128)
    #data_list /= 2**4
    data_list = (data_list.astype(np.int8) ^ np.int8(128)) >> 4
    #data_list = (np.bitwise_xor(data_list.astype(np.int8), 128*np.ones(data_list.shape, dtype=np.int8)).astype(np.int8))/2**4 #data_list/2**4
    data = data_list[:,:,::2] + 1.0j*data_list[:,:,1::2]
    return data


def calc_gains(g):
    '''
    Expects array in. returns (glin, glog)
    '''
    #2**14 is max for linear gain
    #ignore dc component
    #check for nans
    #print g
    bad_values = (g > 2**31) | ~np.isfinite(g)
    g = np.ma.array(g,mask=bad_values)
    glog = (np.ceil(np.log2(np.ma.median(np.abs(g)/2**13,axis=1)))).astype(np.int)
    glin = np.zeros(g.shape, dtype=np.complex)
    for i, glog_single in enumerate(glog):
        glin[i] = g[i]/2**glog[i]
    glog.data[glog.mask == True] = np.ma.median(glog)
    glog.mask[glog.mask] = False
    glin[bad_values] = 2**14
    return glin, glog.data


def calculate_gains(c, port):
    c.set_data_source('adc')
    c.set_adc_mode('data')
    c.set_fft_bypass(0)
    c.set_fft_shift(1367) #Not sure how to make this a constant
    c.set_scaler_bypass(0)
    #c.set_send_flags()
    c.set_offset_binary_encoding()
    default_log2_gain = 22
    c.set_gain((1,default_log2_gain))
    c.set_local_data_port_number(int(port))
    c.start_data_capture(burst_period_in_seconds=0.001)
    c.sync()
    channels = range(16)
    #for 4 bit number *sqrt2 since real and imag, check this
    idealRMS = 1.5 * np.sqrt(2) #2.83 is 1.5bits  1.5 is 0.6bits
    #glog = 13 # not sure why this isn't 9, but seemed to be the case.
    print "configured for sending data to port {0}".format(port)
    rmss = []
    for i in range(18):
        data = get_frames(port)
        # only do for channel 0 for now
        outrms = data[:,:,:].std(axis=0)
        outrms[outrms < 0.8] = 0.8
        rmss.append(outrms.mean())
        print outrms.mean(axis=1)
        if i == 0:
            g = idealRMS*2**(default_log2_gain)/outrms#idealRMS*2**(default_log2_gain-4)/outrms
        else:
            for j, glog1 in enumerate(glog):
                g[j] = idealRMS * glin[j] * (2**(glog[j]))/outrms[j] #idealRMS*glin*(2**(glog-4))/outrms
                g[j] = (20.0 * g[j] + 80.0 * glin[j] * (2**(glog[j])))/100.0
        glin, glog = calc_gains(g)
        print glog
        bad_gains = glin > 2**14
        glin[bad_gains] = 2**14
        glin = glin.astype(np.int).astype(np.complex)
        gain = []
        for channel in channels:
            gain.append([channel,[glin[channel].tolist(), glog[channel]]])
        c.set_gain(gain)
        time.sleep(1)
    out1 = open('gains_noisy_slot{0}.pkl'.format(c.slot), 'wb')
    pickle.dump(gain, out1)
    out1.close()
    Calc = GainCalc()
    flags = []
    for channel in channels:
        Calc.update(gain[channel][1][0])
        glin_final = Calc.run()
        gain[channel][1][0] = glin_final.tolist()
        flags.append(Calc.mask)
    c.set_gain(gain)
    c.freq_flags = flags
    output = open('/home/chime/ch_acq/gains_slot'+str(c.slot)+'.pkl','wb')
    pickle.dump(gain, output)
    output.close()
    print "Scaler Gain set and saved"
    c.stop_data_capture()


if __name__ == '__main__':

    try:
        logger.info('Deleting previous chFPGA instances in current namespace')
        c.close() # close sockets from previous objects to free them for the new one
        #r.close() # close sockets from previous objects to free them for the new one
        del c
        #del r
    except NameError:
        pass

    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0]) # description is the first line of the docstring
    parser.add_argument('--init', action = 'store', type=int, default=1, help='Initialization level: -1: Just create sockets, 0: connect and read only. 1: initialize hardware')
    parser.add_argument('-f', '--sampling_frequency', action = 'store', type=float, default=800, help='Sampling frequency of the ADC in MHz')
    parser.add_argument('-l', '--log_level', action = 'store', type=str, choices=['info','debug'], default='info', help='Logging level')
    parser.add_argument('-w', '--data_width', action = 'store', type=int, choices=[4,8], default=8, help='Data width of each Re and Im component of the channelizer output')
    parser.add_argument('-g', '--group_frames', action = 'store', type=int, default=4, help='Number of frames to group before sending to the GPU or FPGA correlator. The total size of the frame, including the header and ethernet obverhead, cannot exceed 8 kibytes.')
    parser.add_argument('--enable_gpu_link', action = 'store', type=int, default=0, help='Enables the GPU link transmission')
    parser.add_argument('--ip', action = 'store', type=str, default='10.10.10.11', help='IP address of the board')
    parser.add_argument('--host_ip', action = 'store', type=str, default=None, help='IP address of adapter through which the connection to the FPGA will be established. If not specified, the controller will attempt to identify the proper host based on the FPGA IP address.')
    args = parser.parse_args()

    log_level = {'info': logging.INFO, 'debug': logging.DEBUG}[args.log_level]
    logging.basicConfig(level=log_level, format='%(asctime)s %(name)-32s %(levelname)-10s : %(message)s')

    logger = logging.getLogger(__name__)
    logger.info('------------------------')
    logger.info('calculate_gains.py: Calulates gains for ideal 4-bit noise contribution')
    logger.info('Kevin Bandura')
    logger.info('------------------------')
    logger.info('This module is called with the follwing parameters:' )
    for (key,value) in args.__dict__.items():
        logger.info('   %s = %s' % (key, repr(value)))
    # logger.info('Using Sampling frequency of %0.3f MHz' % args.sampling_frequency)
    # Delete previous instances of 'c' to make sure the sockets are closed. If not, the new object will not be able to open the socket.
    # pylint: disable=E0601


    ADC_DELAY_TABLE = ADC_DELAYS_MGK7MB_REV2_MGAC08_REV2 #ADC_DELAYS_REV2_SN0001 ## select the table corresponding to the FMC serial number
    #FREF = 10 # FMC Reference clock frequency

    # Create the new chFPGA object.
    c = chFPGA_controller.chFPGA_controller(ip_address=args.ip, port_number=41000, adc_delay_table=ADC_DELAY_TABLE, init=args.init, sampling_frequency=args.sampling_frequency * 1e6, reference_frequency=10e6, data_width=args.data_width, group_frames=args.group_frames, enable_gpu_link = args.enable_gpu_link, host_ip = args.host_ip) # pylint: disable=C0103

    time.sleep(0.5)
    logger.info('Getting chFPGA configuration')
    chFPGA_config = c.get_config()
    logger.info('Starting data/correlator receiver threads')
    #r = chFPGA_receiver.chFPGA_receiver(chFPGA_config, ip_address=args.ip, port=41001, host_ip = args.host_ip)
    calculate_gains(c,'41001')

    #np.save('gain.npy',np.array(gain))
