#!/usr/bin/python
# Disable pylint TAB warnings (W0312) and Line too long (=C0301)
# pylint: disable=W0312,C0301

"""
save_raw_frames.py script 
Saves raw timestream data to files.   


#
History:
    2015-05-26 TP, JM: Updated to work with new firmware. Tested with chfpga firmware git tag 81ee74f05 and ch_acq software tag b7ac220d2038
    2011-08-14 JFC: Created from chFPGA, which now only contains top test code.
    2011-09-09 JFC: Added global FREF 
    2011-10-11 JFC: Updated delay tables
"""
#import time
import argparse
import logging
import logging.handlers
import __main__
import numpy as np
import time
from timestream_receiver import get_frame


# IT IS IMPORTANT TO START THE PATH AT CHFPGA
from pychfpga.MGADC08 import MGADC08
from pychfpga.core.icecore.session import load_session as load_yaml
from pychfpga.core.icecore import IceBoardPlus
from pychfpga.core.chFPGA_controller import chFPGA_controller
from pychfpga.core import close_all_sockets
from core.chFPGA_receiver import chFPGA_receiver

#####################################

# Default data and clock line delays for the two FMC boards/ML605 combination.
# First 8 values are the delays for bits 0 to 7, 8th value is the delay for the clock line.
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


def save_timestream_frames(port, channels=[0], frames=256, filename='data.npy'):
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
    #chFPGA_receiver.send_every_frame(False)
    number_of_frames=0
    missed = 0
    print "Starting Timestream acquisition"
    try:
        while (frames==0) or (frames!=0 and number_of_frames<frames):
            try:
                #print "trying to get a frame"
                a = get_frame(port) #chFPGA_receiver.read_frames(verbose=0)
                #for chanNum in chanIndex:
                #    data_list[number_of_frames,chanNum,:] = a[channels[chanNum]]
                #    #data_list.append(a[channels[chanNum]])
                data_list[number_of_frames,:,:] = a.values()[0]
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
                #chFPGA_receiver.close()
                raise
    except KeyboardInterrupt:
        chFPGA_receiver.close()
        raise
    print "lost {0} to get {1}".format(missed, frames)
    #np.array(data_list)
    print filename
    np.save(filename,data_list)

    print 'Saved {0} frames'.format(number_of_frames)


if __name__ == '__main__':

    reload(logging) # clear any previous logger set-up that is stored in the logging module
    reload(logging.handlers) # we need to reload the handlers as well so they are inheriting from the newly loaded Handler class defined in freshly reloaded logging, not the old one. Otherwise we get errors.

    close_all_sockets()

    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0]) # description is the first line of the docstring
    parser.add_argument('-t', '--log_target', action='store', type=str, default='stream', help="Logging target ('stream', 'syslog' or a filename)")
    parser.add_argument('-l', '--log_level', action = 'store', type=str, choices=['info','debug'], default='debug', help='Logging level')
    parser.add_argument('-s', '--subarray', action = 'store', type=int, help='Subarrays to include')
    parser.add_argument('-f', '--force', action = 'store', type=int, default=0, help='Forces reprogramming of the FPGAs even if they are already programmed')
    parser.add_argument('-b', '--bitfile', action = 'store', type=str, default= '../../chfpga/xilinx_projects/CHFPGA_MGK7MB_REV2/CHFPGA_MGK7MB_REV2.runs/impl_Rev2/chFPGA_MGK7MB_Rev2.bit',  help='Filename of the bitfile used to to program the FPGAs')
    parser.add_argument('-y', '--yamlfile', action = 'store', type=str, default= 'yaml_iceboard_list.txt',  help='Yaml file with list of boards and their respective IP addresses and handlers.')
    parser.add_argument('-d', '--delayfile', action = 'store', type=str, default= 'delays_aug_2015.pkl',  help='Pickle file with ADC delays.')
    parser.add_argument('-n', '--nframes', action = 'store', type=int, default= 256,  help='Number of frames to save.')
    parser.add_argument('-f', '--filename', action = 'store', type=str, default= None,  help='Name of file to save raw data.')



    #parser.add_argument('-w', '--data_width', action = 'store', type=int, choices=[4,8], default=4, help='Data width of each Re and Im component of the channelizer output')
    #parser.add_argument('-g', '--group_frames', action = 'store', type=int, default=4, help='Number of frames to group before sending to the GPU or FPGA correlator. The total size of the frame, including the header and ethernet obverhead, cannot exceed 8 kibytes.')
    #parser.add_argument('-e', '--enable_gpu_link', action = 'store', type=int, default=0, help='Enables the GPU link transmission')
    #parser.add_argument('-n', '--init', action = 'store', type=int, default=1, help='Initialization level: -1: Just create sockets, 0: connect and read only. 1: initialize hardware')    
    #parser.add_argument('-o', '--open_boards', action = 'store', type=int, default=0, help='Establish communication with the boards and initialize the firmware and software')
    args = parser.parse_args()
    log_levels = {'info': logging.INFO, 'debug': logging.DEBUG}

    #__main__._host_interface_ip_addr = args.if_ip # NOT NEEDED SO FAR BUT ANYWAYS

    # Select delay table
    ADC_DELAY_TABLE = ADC_DELAYS_MGK7MB_REV2_MGAC08_REV2 
    # -------------------------------
    # Set-up logging
    # -------------------------------

    logger = logging.getLogger('')
    logger.handlers = []  # Clear all existing handlers

    # Make sure SQLAlchemy does not log too much
    sql_logger = logging.getLogger('sqlalchemy.engine.base.Engine')
    sql_logger.setLevel(logging.INFO)

    if args.log_target == 'stream':
        log_handler = logging.StreamHandler()
    elif args.log_target == 'syslog':
        log_handler = logging.handlers.SysLogHandler()
    else:
        log_handler = logging.FileHandler(args.log_target)

    # Set-up log for this test run
    logger.setLevel(log_levels[args.log_level])
    logger.addHandler(log_handler)

    logger.info('%s: ------------------------' % __file__)
    logger.info('%s: SAVE RAW FRAMES' % __file__)
    logger.info('%s: ------------------------' % __file__)
    logger.info('%s: Called with: %s' % (__file__, ', '.join('%s=%s' % (key, repr(value)) for (key,value) in args.__dict__.items())))
    # Create the new chFPGA object.

    # Get fpga bitstream
    with open(args.bitfile, 'rb') as bitfile:
        fpga_bitstream = bitfile.read()

    # Create new fpga query object
    with open(args.yamlfile, 'rb') as yamlfile:
        ca =  load_yaml(yamlfile)   
    c = ca.query(IceBoardPlus).filter_by(subarray=args.subarray) # c is kind of standard notation for a list of iceboards now.
    
    # Associate the fpga_bitstream with the target Handler    
    c.set_handler(chFPGA_controller, fpga_bitstream)
    
    # Program fpga
    c.set_fpga_bitstream(force = args.force)

    # Print resuts
    print 'The following IceBoards were found in Subarray %r:' % args.subarray
    for ib in c:
        print "  IceBoard SN%s in slot %r. Handler = '%s'" % (ib.serial, ib.slot, ib.handler_name)
        #setattr(__main__, 'c%i' % int(ib.serial), ib) #TAB COMPLETION DOES NOT WORK WITH THIS

    # Open boards
    c.discover_mezzanines()
    # Establish communication with the board and initialize the firmware and software
    c.open(adc_delay_table=ADC_DELAY_TABLE,
           init=args.init,
           sampling_frequency=800e6,
           reference_frequency=10e6, data_width=4,
           group_frames=4,
           enable_gpu_link = 1)

    # Set delays
    with open(args.delayfile) as delayfile:
        delays = pickle.load(delayfile)

    for cc in enumerate(c):
        cc.set_adc_delays_with_check(delays[int(cc.serial)])

    # Start capturing data
    c.set_data_source('adc')
    c.set_adc_mode('data')
    for cc in c:
        cc.start_data_capture(burst_period_in_seconds=0.05, source='adc')
        rr = chFPGA_receiver(cc.get_config())
        data = []
        timestamps = []
        n = 0
        while n < args.nframes:
            try:
                new_frames = rr.read_frames()
                frame_array = [new_frames[i] for i in range(16)]
                data.append(np.vstack(frame_array))
                timestamps.append(new_frames['timestamp'])
                n += 1
            except:
                print "missing a frame, skipping"

        data = np.hstack(data)
        timestamps = np.array(timestamps)
        np.savez('rawdata_SN%s_Slot%d_'+str(time.time())+'.npz', data=data, timestamps=timestamps)
        cc.stop_data_capture()
        rr.close()
