#!/usr/bin/python
# Disable pylint TAB warnings (W0312) and Line too long (=C0301)
# pylint: disable=W0312,C0301

"""
top_test.py script
 Instantiates a chFPGA object 'c' for interactive testing. Import in ipython using "r -i top_test" so the created chFPGA object "c" is accessible in the ipython interactive workspace.


#
History:
    2011-08-14 JFC: Created from chFPGA, which now only contains top test code.
    2011-09-09 JFC: Added global FREF
    2011-10-11 JFC: Updated delay tables
"""
import logging
import argparse
import time
import __main__

# from pychfpga.icecore import hardware_map

# from icecore import hardware_map
# from icecore import tuber

# from pychfpga.icecore.icearray import IceArray, close_all_sockets
# from pychfpga.icecore.fpgabitfile import FpgaBitFile

from core.icecore import IceBoardPlus, IceBoardPlusHandler, IceCrate, HardwareMap, discover_iceboards
from core.chFPGA_controller import chFPGA_controller
from core.chFPGA_receiver import chFPGA_receiver

# from icecore.icearray import IceArray, close_all_sockets
from core.icecore_ext.fpga_bitstream import FpgaBitstream

# from pychfpga.core import chFPGA_controller
# from pychfpga.core.chFPGA_controller import chFPGA_controller as ChimeFpgaFirmware
# from pychfpga.core import chFPGA_receiver
import plot_utils.plot_utils as pu
# from pychfpga.core import Inject_tools as inj

# from pychfpga.common.tests.test_adc_fft_bin import test_adc_fft_bin
# from pychfpga.common.tests.test_adc_fft_int_power import test_adc_fft_int_power
# from pychfpga.common.tests.test_adc_fft_level import test_adc_fft_level
# from pychfpga.common.tests.test_adc_dc import test_adc_dc
# from pychfpga.common.tests.test_adc_spectrum import test_adc_spectrum
# import pychfpga.common.tests.test_corr as tc
# from pychfpga import receiver_corr_fast

# print 'Reloading modules'

# Default data and clock line delays for the two FMC boards/ML605 combination.
# First 8 values are the delays for bits 0 to 7, 8th value is the delay for the clock line.
#SN001_ADC_DELAYS = (
#    [13,19,19,19,19,19,19,19]+[13], # CH0
#    [18]*8+[0], #CH1
#    [10]*8+[13], #CH2
#    [19]*8+[13], #CH3
#    [18]*8+[0], #CH4
#    [16]*8+[0], #CH5
#    [18]*8+[0], #CH6
#    [14]*8+[0] #CH7
#    )
# SN001_adc_delays=(
    # [5+16,8+16,8+16,8+16,8+16,8+16,8+16,8+16]+[0], # CH0
    # [2+16]*8+[0], #CH1
    # [5+16]*8+[0], #CH2
    # [1+16]*8+[0], #CH3
    # [16]*8+[0], #CH4
    # [15]*8+[0], #CH5
    # [18]*8+[0], #CH6
    # [13]*8+[0] #CH7
    # )

# SN001_adc_delays=(
    # [5,12,12,12,12,12,12,12]+[0], # CH0
    # [8]*8+[0], #CH1
    # [8]*8+[0], #CH2
    # [6]*8+[0], #CH3
    # [5]*8+[0], #CH4
    # [4]*8+[0], #CH5
    # [4]*8+[0], #CH6
    # [4]*8+[0] #CH7
    # )

#SN002_adc_delays=(
#    [16,22,22,22,22,22,22,22]+[0], #CH0 (BUFR)
#    [21]*8, #CH1 (BUFR)
#    [22]*8+[0], #CH2 (PLL)
#    [18]*8+[0], #CH3 (PLL)
#    [17]*8, #CH4 (BUFR)
#    [17]*8, #CH5 (BUFR)
#    [18]*8, #CH6 (BUFR)
#    [14]*8, #CH7 (BUFR)
#    )

#SN002_ADC_DELAYS = (
#    [17,15,15,15,15,15,15,3]+[0], #CH0 (BUFR)
#    [15]*8, #CH1 (BUFR)
#    [27,14,29,29,29,29,29,15]+[0], #CH2 (PLL)
#    [15]*8+[0], #CH3 (PLL)
#    [17]*8, #CH4 (BUFR)
#    [17]*8, #CH5 (BUFR)
#    [18]*8, #CH6 (BUFR)
#    [14]*8, #CH7 (BUFR)
#    )


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

if __name__ == '__main__':

    # close_all_sockets() # close any previously opened sockets
    # try:
    #     logger.info('Deleting previous chFPGA instances in current namespace')
    #     r.close() # close sockets from previous objects to free them for the new one
    #     del r
    # except NameError:
    #     pass

    # Get command line arguments
    default_bitfile = (
        '../../chfpga/xilinx_projects/CHFPGA_MGK7MB_REV2/CHFPGA_MGK7MB_REV2.runs/impl_Rev2/CHFPGA_MGK7MB_REV2.bit')

    # Configure the various loggers to provide adequate levels of details
    log_levels = {'info': logging.INFO, 'debug': logging.DEBUG}

    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0]) # description is the first line of the docstring
    parser.add_argument('--init', action = 'store', type=int, default=1, help='Initialization level: -1: Just create sockets, 0: connect and read only. 1: initialize hardware')
    parser.add_argument('-t', '--log_target', action='store', type=str, default='syslog', help="Logging target ('stream', 'syslog' or a filename)")
    parser.add_argument('-l', '--log_level', action='store', type=str, choices=log_levels, default='debug', help='Logging level')
    parser.add_argument('-b', '--iceboards', action='store', nargs='+', type=str, help="Space-separated list of the iceboard hostnames (e.g. 10.10.10.7 or iceboard0007.local if the mDNS system is operational")
    parser.add_argument('-s', '--subarray', action='store', nargs='+', type=int, help='Space-separated list of subarrays to include')
    parser.add_argument('--bitfile', action='store', type=str, default= default_bitfile,  help='Filename of the bitfile used to to program the FPGAs')
    parser.add_argument('-f', '--sampling_frequency', action = 'store', type=float, default=800, help='Sampling frequency of the ADC in MHz')
    parser.add_argument('-i', '--if_ip', action='store', type=str, default=None, help='IP address of adapter through which the connection to the FPGA will be established. This is used solely for direct UDP communications with the FPGA.')
    parser.add_argument('--force', action='store', type=int, default=0, help='Force FPGA programming even if the firmware is already programmed.')
    parser.add_argument('-w', '--data_width', action = 'store', type=int, choices=[4,8], default=8, help='Data width of each Re and Im component of the channelizer output')
    parser.add_argument('-g', '--group_frames', action = 'store', type=int, default=4, help='Number of frames to group before sending to the GPU or FPGA correlator. The total size of the frame, including the header and ethernet obverhead, cannot exceed 8 kibytes.')
    parser.add_argument('--enable_gpu_link', action = 'store', type=int, default=0, help='Enables the GPU link transmission')
    parser.add_argument('--sn', action = 'store', type=int, default=7, help='Serial number of the Iceboard')

    args = parser.parse_args()

    __main__._host_interface_ip_addr = args.if_ip

    # log_level = {'info': logging.INFO, 'debug': logging.DEBUG}[args.log_level]
    # logging.basicConfig(level=log_level, format='%(asctime)s %(name)-32s %(levelname)-10s : %(message)s')
    # logging.getLogger('sqlalchemy.engine.base.Engine').setLevel(logging.WARN)

    # logger = logging.getLogger(__name__)

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



    logger.info('------------------------')
    logger.info('top_test.py: chFGPA test script')
    logger.info('J.-F. Cliche')
    logger.info('------------------------')
    logger.info('This module is called with the follwing parameters:' )
    for (key,value) in args.__dict__.items():
        logger.info('   %s = %s' % (key, repr(value)))
    # logger.info('Using Sampling frequency of %0.3f MHz' % args.sampling_frequency)
    # Delete previous instances of 'c' to make sure the sockets are closed. If not, the new object will not be able to open the socket.
    # pylint: disable=E0601


    #ADC_TEST_MODE = 0     #  0= normal, 1= ramp, 2=pulse (1 high, 10 low)
    ADC_DELAY_TABLE = ADC_DELAYS_MGK7MB_REV2_MGAC08_REV2 #ADC_DELAYS_REV2_SN0001 ## select the table corresponding to the FMC serial number
    #FREF = 10 # FMC Reference clock frequency


    # Associate the bitstream with the target Handler
    # chFPGA_controller.register_fpga_bitstream(fpga_bitstream)

    # Close all previous sessions with the layout/hardware map database
    # IceArray.close_all_sessions() # close all previously opened sessions

    # Create the array object and update the hardware database from a file and from auto-discovery
    # array = IceArray(uri='sqlite:///test.db', interface_ip_addr=args.host_ip)
    # array.load_iceboards('iceboard_list.txt') # update iceboard definitions in database with the data in this CSV file so we can start with an empty database if needed
    # array.discover() # automatically update the hardware map database with discovered resources. This will probe the baords and will update the 'present' field.

    hwm = HardwareMap()  # Create empty hardware map

    # Add iceboards. For now, we know only their hostname
    if args.iceboards:
        for hostname in args.iceboards:
            hwm.add(IceBoardPlus(hostname=hostname))
    hwm.flush()

    # -------------------------------
    # Check if specified iceboards are on-line before going any further
    # -------------------------------
    ib = hwm.query(IceBoardPlus)

    for i in ib:
        if not i.ping():
            raise RuntimeError("%r could not be found at '%s'"
                               % (i, i.tuber_uri))

    if ib.count():

        # Configure the FPGA with the bitstream associated with the handler
        if args.force > -1:
            fpga_bitstream = FpgaBitstream(args.bitfile)
            ib.set_handler(chFPGA_controller, fpga_bitstream)
            ib.set_fpga_bitstream(force=args.force)
        else:
            ib.set_handler(chFPGA_controller)

        ib.discover_serial()  # auto-discover the serial number of every IceBoard
        # ib.discover_crate()  # auto-discover crates and add them to the hardware map (requires chFPGA_controller handler for now)
        ib.discover_mezzanines() # auto-discover mezzanines and add them to the hardware map (requires chFPGA_controller handler to read McGill MGADC08 EEPROMs)

        c=ib[0]

        # Establish communication with the board and initialize the firmware and software
        c.open(adc_delay_table=ADC_DELAY_TABLE,
               init=args.init,
               sampling_frequency=args.sampling_frequency * 1e6,
               reference_frequency=10e6, data_width=args.data_width,
               group_frames=args.group_frames,
               enable_gpu_link = args.enable_gpu_link)

        logger.info('Getting chFPGA configuration')
        chFPGA_config = c.get_config()
        logger.info('Starting data/correlator receiver threads')
        r = chFPGA_receiver(chFPGA_config)
        c.set_local_data_port_number(r.port_number)

