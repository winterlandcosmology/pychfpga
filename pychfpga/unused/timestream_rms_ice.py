#!/usr/bin/python

"""
timestream_rms.py script 
 Script to run the 5-channel system and just print to screen the rms of the adc timestream input.
#
History:
    2012-12-04 KMB: First attempt 
"""


#from pychfpga.core import chFPGA_receiver
from timestream_receiver import get_frame
import numpy as np
import time, sys, os, logging
import argparse
import pickle

from pychfpga.MGADC08 import MGADC08
from pychfpga.core.icecore.session import load_session as load_yaml
from pychfpga.core.icecore import IceBoardPlus
from pychfpga.core.chFPGA_controller import chFPGA_controller
from pychfpga.core import close_all_sockets

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

def print_RMS(port):
    channels = [0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15]
    cont = True
    while cont:
        try:
            a = get_frame(port)
            for chan in channels:
                sys.stdout.write("ch%d %f\n" % (chan, np.log2(a.values()[0][chan,:].std())))
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

    reload(logging) # clear any previous logger set-up that is stored in the logging module
    reload(logging.handlers) # we need to reload the handlers as well so they are inheriting from the newly loaded Handler class defined in freshly reloaded logging, not the old one. Otherwise we get errors.

    close_all_sockets()

    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0]) # description is the first line of the docstring
    parser.add_argument('-t', '--log_target', action='store', type=str, default='syslog', help="Logging target ('stream', 'syslog' or a filename)")
    parser.add_argument('-l', '--log_level', action = 'store', type=str, choices=['info','debug'], default='debug', help='Logging level')    
    parser.add_argument('-f', '--force', action = 'store', type=int, default=0, help='Forces reprogramming of the FPGAs even if they are already programmed')
    parser.add_argument('-i', '--if_ip', action = 'store', type=str, default=None, help='IP address of adapter through which the connection to the FPGA will be established. If not specified, the controller will attempt to identify the proper host based on the FPGA IP address.')
    parser.add_argument('-b', '--bitfile', action = 'store', type=str, default= '/home/chime/firmware/chFPGA_MGK7MB_Rev2_July27_2015.bit',  help='Filename of the bitfile used to to program the FPGAs')
    parser.add_argument('-s', '--subarray', action = 'store', type=int, help='Subarrays to include')
    parser.add_argument('-y', '--yamlfile', action = 'store', type=str, default= 'yaml_iceboard_list.txt',  help='Yaml file with list of boards and their respective IP addresses and handlers.')
    parser.add_argument('-d', '--delayfile', action = 'store', type=str, default= 'delays_aug_2015.pkl',  help='Pickle file with ADC delays.')
    parser.add_argument('-o', '--slot', action = 'store', type = int, default=1, help = "which slot to run on.")
    args = parser.parse_args()
    log_levels = {'info': logging.INFO, 'debug': logging.DEBUG}

    # logging.basicConfig(level=log_level, format='%(asctime)s  %(context)s %(name)-32s %(levelname)-10s : %(message)s')

    logger = logging.getLogger('')
    logger.handlers = []  # Clear all existing handlers

    if args.log_target == 'stream':
        log_handler = logging.StreamHandler()
    elif args.log_target == 'syslog':
        log_handler = logging.handlers.SysLogHandler()
    else:
        log_handler = logging.FileHandler(args.log_target)

    # Set-up log for this test run
    logger.setLevel(log_levels[args.log_level])
    logger.addHandler(log_handler)

    #handler = logging.handlers.SysLogHandler()
    # handler = logging.StreamHandler()
    # handler.addFilter(CompletionFilter)
    ADC_DELAY_TABLE = ADC_DELAYS_MGK7MB_REV2_MGAC08_REV2 # ADC_DELAYS_REV2_SN0001 # select the table corresponding to the FMC serial number
    #IceArray.close_all_sessions() # close all previously opened sessions
    # Get fpga bitstream
    with open(args.bitfile, 'rb') as bitfile:
        fpga_bitstream = bitfile.read()

    # Create new fpga query object
    with open(args.yamlfile, 'rb') as yamlfile:
        ca =  load_yaml(yamlfile)   
    c_rack = ca.query(IceBoardPlus).filter_by(subarray=args.subarray) # c is kind of standard notation for a list of iceboards now.
    c = c_rack(slot=args.slot)
    # Associate the fpga_bitstream with the target Handler    
    c.set_handler(chFPGA_controller, fpga_bitstream)
    
    # Program fpga
    c.set_fpga_bitstream(force = args.force)
    
    # Discover mezzanines
    c.discover_mezzanines()

    # Open boards
    c.open( \
        adc_delay_table=ADC_DELAY_TABLE, \
        init=1, \
        sampling_frequency=800* 1e6, \
        data_width=4, \
        group_frames=4, \
        enable_gpu_link = 1)

    # Set delays
    with open(args.delayfile) as delayfile:
        delays = pickle.load(delayfile)
    c.set_corr_reset(1)
    time.sleep(0.1)
    c.set_corr_reset(0)
    #for i, c_element in enumerate(c):
    c.set_adc_delays_with_check(delays[int(c.serial)])
    chFPGA_config = c.get_config()
    #r = chFPGA_receiver.chFPGA_receiver(chFPGA_config, \
    #              ip_address=c_element.fpga_ip_addr, \
    #              port=c_element.fpga_port_number+1, \
    #              host_ip = args.if_ip)
    port = str(c.fpga_port_number+1)
    c.set_data_source('adc')
    c.set_adc_mode('data')
    c.set_FFT_bypass(True)
    c.set_scaler_bypass(True)
    c.set_gain((1,27))
    c.set_offset_binary_encoding(0)
    c.set_local_data_port_number(int(port))
    c.start_data_capture(burst_period_in_seconds=0.1, number_of_bursts=0)
    c.sync()
    time.sleep(4)
    print port
    print_RMS(port)
    #r.close()




    
