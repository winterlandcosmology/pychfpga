#!/usr/bin/env python

'''
for testing ADC-FPGA communication by sending ADC ramps.
'''

import numpy as np
import os
import matplotlib
matplotlib.use('Agg')
import time, pylab, csv
from pychfpga.common.tests.test_BaseClass import test_BaseClass
from pychfpga.core import chFPGA_receiver
from collections import Counter
# from pychfpga import save_raw_frames

def save_timestream_frames(chFPGA_receiver, channels=[0], frames=256, filename='data.npy'):    
    '''
        Saves data from Acquisition board to numpy array (taken from ch_acq commit d4bf1d69d9d1028e1a73a39
    '''

    if isinstance(channels,int): # make sure that channel is a array of channels
        channels=np.array([channels])
    elif isinstance(channels,list):
        channels=np.array(channels)
    nchan = channels.size
    data_list = np.zeros((frames,nchan,2048), dtype=np.int8)
    chanIndex = np.arange(nchan)
    #chFPGA_receiver.frame_receiver._send_every_frame.clear()     
    #chFPGA_receiver.send_every_frame(False)
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
            except ValueError:
                print "got a weird frame... carrying on!"
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

class test_adc_ramp_histogram(test_BaseClass):
    '''
     Test class for testing ADC-FPGA communication by sending ADC ramps.   
    '''
    def configure_board(self):
        self.fpga_ctrl.set_fft_bypass(True, channels=range(16))
        self.fpga_ctrl.set_scaler_bypass(True, channels=range(16))
        self.fpga_ctrl.set_data_source('adc', channels=range(16))
        self.fpga_ctrl.set_ADC_mode(mode='ramp')
        #self.fpga_ctrl.set_gain((1,27))
        self.fpga_ctrl.set_data_width(8)
        self.fpga_ctrl.set_offset_binary_encoding(0)
        time.sleep(1)
        self.fpga_ctrl.start_data_capture(burst_period_in_seconds=0.1, channels=range(16), source='adc')
        self.fpga_ctrl.sync()
        time.sleep(2)
        return

        
    def plot_histogram(self, filename):
        datas = np.load(filename + '.npy')
        self.test_hist_equal = np.zeros(16, dtype=np.bool_)
        self.test_hist_expected = np.zeros(16, dtype=np.bool_)
        expected_value = datas.shape[2]
        pylab.clf()
        for i in xrange(16):
            hist, bins = np.histogram(datas[:,i,:].flatten(), bins=256, range=(-128.5, 127.5))
            center = (bins[:-1] + bins[1:]) / 2
            pylab.bar(center, hist, align='center', width=1.0)
            #pylab.hist(datas[:,i,:].flatten(), bins=256, range = (-128,127))
            pylab.title(filename + ' Channel '+str(i))
            pylab.xlim(-128,127)
            pylab.savefig(filename + '_chan' +str(i)+'.pdf')
            pylab.clf()
            chist = Counter(hist)
            self.test_hist_equal[i] = (len(chist) == 1)
            self.test_hist_expected[i] = (chist.most_common(n=1)[0][0] == expected_value)
            

    def compute_bit_errors(self, fname):
        perfect_ramp = (np.arange(2048) % 256) - 128
        bits = [1,2,4,8,16,32,64,128]
        datas = np.load(fname + '.npy')
        infocsv = open(fname+'.txt', 'w')
        self.bit_error_rate = np.zeros((16, len(bits)), dtype=np.float64)
        self.test_bit_error_rate = np.zeros((16, len(bits)), dtype=np.bool_)
        writer = csv.writer(infocsv)
        for i in xrange(16):
            xored = np.bitwise_xor(datas[:,i,:], perfect_ramp)
            for j,bit in enumerate(bits):
                bad_bit = (np.bitwise_and(xored, bit)>>j).sum()*1.0/len(xored.flatten())
                writer.writerow([i, j, bad_bit])
                self.bit_error_rate[i,j] = bad_bit
                self.test_bit_error_rate[i,j] = (bad_bit == 0.0)
                print 'chan {0}, bit {1}, error rate {2:.3f}'.format(i, j, bad_bit)


    def compress_file(self, fname):
        # Save data as compressed archive and delete uncompressed file
        filename = fname + '.npy'
        data = np.load(filename)
        np.savez_compressed(fname, data=data)
        os.remove(filename)


    def execute(self, fname ):
        try:
            self.configure_board()
            self.fpga_recv.flush()
            filename = fname + '.npy'
            save_timestream_frames(self.fpga_recv, channels = range(16), frames=256, filename = filename)
            self.fpga_ctrl.stop_data_capture()
            self.plot_histogram(fname)
            self.compute_bit_errors(fname)
            self.compress_file(fname)
            #confirm = raw_input('Start print_ramp_errors? This will print error counts until a KeyboardInterrupt. (y/n)\n')
            #if confirm == 'y' or confirm == 'Y':
            #    self.fpga_ctrl.ANT.print_ramp_errors()
        except:
            self.fpga_recv.close()
            raise

if __name__ == '__main__':

    import argparse
    import logging
    #from pychfpga import save_raw_frames
    # from pychfpga.icecore import hardware_map
    # from pychfpga.icecore import tuber


    from pychfpga.icecore.icearray import IceArray, close_all_sockets
    # from pychfpga.icecore.fpga_bitstream import FpgaBitstream
    from pychfpga.icecore.iceboard import IceBoard
    from pychfpga.core.chFPGA_controller import chFPGA_controller as ChimeFpgaFirmware
    from pychfpga.core import chFPGA_receiver
    ADC_DELAY_TABLE= (
    ([18, 18, 18, 16, 16, 17, 16, 17],     [3]*8), #CH0
    ([23, 24, 22, 22, 23, 23, 22, 22],                       [4]*8), #CH1
    ([25, 25, 25, 25, 25, 25, 25, 23],    [3]*8), #CH2
    ([22, 23, 23, 22, 24, 22, 22, 21],                       [3]*8), #CH3
    ([20, 22, 22, 22, 22, 21, 22, 22],                        [3]*8), #CH4
    ([18, 18, 20, 19, 18, 19, 20, 18],    [3]*8), #CH5
    ([23, 24, 22, 23, 23, 21, 23, 23],    [3]*8), #CH6
    ([22, 20, 21, 23, 20, 21, 21, 21],                       [4]*8), #CH7

    ([19, 21, 18, 20, 20, 18, 21, 18],   [3]*8), #CH8
    ([20, 18, 17, 18, 18, 16, 20, 20],                       [4]*8), #CH9
    ([24, 24, 20, 22, 20, 22, 21, 22],                       [3]*8), #CH10
    ([22, 23, 22, 21, 22, 22, 22, 21],                     [3]*8), #CH11
    ([20, 20, 21, 20, 19, 18, 18, 18],                       [3]*8), #CH12
    ([23, 21, 21, 22, 19, 20, 23, 20],                       [3]*8), #CH13
    ([22, 23, 23, 23, 23, 23, 23, 24],                       [3]*8), #CH14
    ([17, 19, 18, 19, 19, 21, 19, 19],                       [3]*8)  #CH15
    )
    #ADC_DELAY_TABLE= (
    #([16]*8,     [3]*8), #CH0
    #([7]*8,                       [3]*8), #CH1
    #([22]*8,    [3]*8), #CH2
    #([19]*8,                       [3]*8), #CH3
    #([15]*8,                        [3]*8), #CH4
    #([14, 13, 14, 14, 13, 14, 15, 14],    [3]*8), #CH5
    #([18]*8,     [3]*8), #CH6
    #([17]*8,                       [4]*8), #CH7

    #([15, 17, 15, 18, 17, 14, 17, 15],   [3]*8), #CH8
    #([16]*8,                       [4]*8), #CH9
    #([20]*8,                       [3]*8), #CH10
    #([18]*8,                     [3]*8), #CH11
    #([15]*8,                       [3]*8), #CH12
    #([18]*8,                       [3]*8), #CH13
    #([18]*8,                       [3]*8), #CH14
    #([16]*8,                       [3]*8)  #CH15
    #)
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0]) # description is the first line of the docstring
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0]) # description is the first line of the docstring
    parser.add_argument('-s', '--subarray', action = 'store', nargs='+', type=int, help='Space-separated list of subarrays to include')
    # parser.add_argument('-g', '--group_frames', action = 'store', type=int, default=4, help='Number of frames to group before sending to the GPU or FPGA correlator. The total size of the frame, including the header and ethernet obverhead, cannot exceed 8 kibytes.')
    # parser.add_argument('--enable_gpu_link', action = 'store', type=int, default=0, help='Enables the GPU link transmission')
    parser.add_argument('--force', action = 'store', type=int, default=0, help='Forces reprogramming of the FPGAs even if they are already programmed')
    parser.add_argument('-i', '--if_ip', action = 'store', type=str, default=None, help='IP address of adapter through which the connection to the FPGA will be established. If not specified, the controller will attempt to identify the proper host based on the FPGA IP address.')
    parser.add_argument('--bitfile', action = 'store', type=str, default= '../../chfpga/xilinx_projects/CHFPGA_MGK7MB_REV2/CHFPGA_MGK7MB_REV2.runs/impl_Rev2/chFPGA_MGK7MB_Rev2.bit', 
        help='Location of bitfile to program fpgas')
    parser.add_argument('-l', '--log_level', action = 'store', type =  str, default = 'info', help = 'Log level: accpets either "debug" or "info" (default).')
    #parser.add_argument('--subarray', action = 'store', type=int, default=2, help='Which subarray to use')
    args = parser.parse_args()
    
    log_level = {'info': logging.INFO, 'debug': logging.DEBUG}[args.log_level]
    logging.basicConfig(level=log_level, format='%(asctime)s %(name)-32s %(levelname)-10s : %(message)s')
    logging.getLogger('sqlalchemy.engine.base.Engine').setLevel(logging.WARN)

    logger = logging.getLogger(__name__)
    logger.info('------------------------')
    logger.info('ramp_test.py: chFGPA ramp test script')
    logger.info('------------------------')
    logger.info('This module is called with the follwing parameters:' )
    for (key,value) in args.__dict__.items():
        logger.info('   %s = %s' % (key, repr(value)))
        
    close_all_sockets()
    IceArray.close_all_sessions() # close all previously opened sessions

    ca = IceArray(uri='sqlite:///test.db', interface_ip_addr=args.if_ip)
    ca.load_iceboards('iceboard_list.txt')
    ca.discover() # automatically update the hardware map database with discovered resources

    bitfile_filename = args.bitfile
    fpga_bitstream = ca.get_fpga_bitstream(bitfile_filename, ChimeFpgaFirmware) # Get a new bitstream from the database (or create a new database entry if it does not exist yet)
    c = ca.get_iceboards(subarray=args.subarray).index_by(IceBoard.serial_number) # get one or more IceBoards from specified subarray
    c.set_fpga_firmware(fpga_bitstream, force=args.force)
    c.open( \
        adc_delay_table=ADC_DELAY_TABLE, \
        init=1, \
        sampling_frequency=800 * 1e6, \
        reference_frequency=10e6, data_width=8, \
        group_frames=1, \
        enable_gpu_link = 0, \
        verbose=5)
    print c.fpga.get_temperatures()
    #rs = [chFPGA_receiver.chFPGA_receiver(c_element.fpga.get_config(), ip_address=c_element.fpga_ip_addr, port=c_element.fpga_port_number+1, host_ip = '10.10.10.83') for c_element in c]
    for i, c_element in enumerate(c):
        r = chFPGA_receiver.chFPGA_receiver(c_element.fpga.get_config(), ip_address=c_element.fpga_ip_addr, port=c_element.fpga_port_number+1, host_ip = args.if_ip)
        test = test_adc_ramp_histogram(c_element.fpga, r)
        test.execute('ramp_testing_trial_sn'+str(c_element.serial_number)+'_'+str(i))
        r.close()
    #[r.close() for r in rs]
