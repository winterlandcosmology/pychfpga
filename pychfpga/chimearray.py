#!/usr/bin/python
# Disable pylint TAB warnings (W0312) and Line too long (=C0301)
# pylint: disable=W0312,C0301

"""
chimearray.py module
Example code that demonstrate the use of the 'icecore' library to access arrays of IceBoards in the context of CHIME.

 History:
        2014-03-24 JFC: Created
        2015-04-20 JM: cleaned and simplified (was very messy). Tested with chfpga firmware git tag 8428bb965 and ch_acq software tag dd6df7b5
"""
#import time
import argparse
import logging
import logging.handlers
import __main__

# IT IS IMPORTANT TO START THE PATH AT CHFPGA
from pychfpga.MGADC08 import MGADC08
from pychfpga.core.icecore.session import load_session as load_yaml
from pychfpga.core.icecore import IceBoardPlus
from pychfpga.core.chFPGA_controller import chFPGA_controller
from pychfpga.core import close_all_sockets

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


if __name__ == '__main__':

    reload(logging) # clear any previous logger set-up that is stored in the logging module
    reload(logging.handlers) # we need to reload the handlers as well so they are inheriting from the newly loaded Handler class defined in freshly reloaded logging, not the old one. Otherwise we get errors.

    #close_all_sockets()

    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0]) # description is the first line of the docstring
    parser.add_argument('-t', '--log_target', action='store', type=str, default='stream', help="Logging target ('stream', 'syslog' or a filename)")
    parser.add_argument('-l', '--log_level', action = 'store', type=str, choices=['info','debug'], default='debug', help='Logging level')
    parser.add_argument('-s', '--subarray', action = 'store', type=int, help='Subarrays to include')
    parser.add_argument('-f', '--force', action = 'store', type=int, default=0, help='Forces reprogramming of the FPGAs even if they are already programmed')
    #parser.add_argument('-i', '--if_ip', action = 'store', type=str, default=None, help='IP address of adapter through which the connection to the FPGA will be established. If not specified, the controller will attempt to identify the proper host based on the FPGA IP address.')
    parser.add_argument('-b', '--bitfile', action = 'store', type=str, default= '../../chfpga/xilinx_projects/CHFPGA_MGK7MB_REV2/CHFPGA_MGK7MB_REV2.runs/impl_Rev2/chFPGA_MGK7MB_Rev2.bit',  help='Filename of the bitfile used to to program the FPGAs')
    parser.add_argument('-y', '--yamlfile', action = 'store', type=str, default= 'yaml_iceboard_list.txt',  help='Yaml file with list of boards and their respective IP addresses and handlers.')
    parser.add_argument('-w', '--data_width', action = 'store', type=int, choices=[4,8], default=4, help='Data width of each Re and Im component of the channelizer output')
    parser.add_argument('-g', '--group_frames', action = 'store', type=int, default=4, help='Number of frames to group before sending to the GPU or FPGA correlator. The total size of the frame, including the header and ethernet obverhead, cannot exceed 8 kibytes.')
    parser.add_argument('-e', '--enable_gpu_link', action = 'store', type=int, default=0, help='Enables the GPU link transmission')
    parser.add_argument('-n', '--init', action = 'store', type=int, default=1, help='Initialization level: -1: Just create sockets, 0: connect and read only. 1: initialize hardware')    
    parser.add_argument('-o', '--open_boards', action = 'store', type=int, default=0, help='Establish communication with the boards and initialize the firmware and software')
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
    logger.info('%s: C H I M E A R R A Y' % __file__)
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
    print list(c)
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
    if args.open_boards:
        # Discover mezzanines
        c.discover_mezzanines()
        c.discover_crate()
        # Establish communication with the board and initialize the firmware and software
        c.open(adc_delay_table=ADC_DELAY_TABLE,
               init=args.init,
               sampling_frequency=800e6,
               reference_frequency=10e6, data_width=args.data_width,
               group_frames=args.group_frames,
               enable_gpu_link = args.enable_gpu_link)
