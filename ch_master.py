#!/usr/local/bin/python2.7

"""
Master control program for CHIME.
#
History:
2013-05-13 ADH: First version.
2015-05-30 JM: Modified to work with new icecore and handle noise injection gating
"""

try:
    import chrx
except ImportError:
    chrx = None
    print 'chrx count not be found'

try:
    from pychfpga import calculate_gains
except:
    calculate_gain = None
    print 'calculate_gain count not be loaded. Missing timestream_receiver in path?'

# from configobj import *
# from pychfpga.core.icecore.session import load_session as load_yaml
# from pychfpga.core.chFPGA_controller import chFPGA_controller as ChimeFpgaFirmware
# from pychfpga.core import chFPGA_receiver
# from pychfpga.core.icecore import IceBoardPlus
# from validate import Validator
import argparse
import getpass
import logging
import numpy as np
import os
import sys
import socket
import time
import pickle
# from pychfpga.MGADC08 import MGADC08
#from pychfpga.init_links import load_gains
#import MySQLdb
from pychfpga.core.icecore import async_return, async, NameSpace
from pychfpga import fpga_array
import signal

# Should put somewhere else. Flatten arbitrarily deep nested lists
# from stack overflow
def flatten(x):
        result = []
        for el in x:
                if hasattr(el, "__iter__") and not isinstance(el, basestring):
                        result.extend(flatten(el))
                else:
                        result.append(el)
        return result

def convert_types(val):
            # Do the annoying conversion of numpy types to native Python types. Sigh.
            found_complex = False
            if isinstance(val, (list, tuple)):
                if len(val) == 0:
                    val = [0]
                #if isinstance(val[0], (list, tuple)):
                val = flatten(val)
                if not isinstance(val[0], str):
                    try:
                        if val[0].dtype.kind in ('i', 'u', 'f'):
                            val = list(np.asscalar(x) for x in val)
                    except:
                        if type(val[0]) == bool:
                            val = list(int(x) for x in val)
                        else:
                            val = list(x for x in val)
                    for i, val_element in enumerate(val):
                        #print val_element
                        if isinstance(val_element, complex):
                                val[i] = [val_element.real, val_element.imag]
                                found_complex = True
                        if isinstance(val_element, (int,np.uint8)):
                                val[i] = float(val_element)
                    if found_complex:
                        val = flatten(val)
                    else:
                        val = list(int(x) for x in val)

            else:
                if isinstance(val, long):
                    val = int(val)
                elif isinstance(val, bool):
                    val = int(val)
                elif isinstance(val, unicode):
                    val = str(val)
                if not isinstance(val, str):
                    try:
                        if val.dtype.kind in ('i', 'u', 'f', 'b'):
                            val = np.asscalar(val)
                    except:
                            # Hopefully already a int/float
                            pass
            return val

@async
def get_fpga_hk(fpga):
    """this should parallelize to make one call for all sensors"""
    sensors = [ fpga.RAIL.MB_VCC1V0,fpga.RAIL.MB_VCC1V0_GTX, 
        fpga.RAIL.MB_VCC12V0, fpga.RAIL.MB_VCC5V5, fpga.RAIL.MB_VCC1V5,
        fpga.RAIL.MB_VCC1V2, fpga.RAIL.MB_VCC3V3, fpga.RAIL.MB_VCC1V8,
        fpga.RAIL.MB_VADJ ]
    with fpga.tuber_context() as ctx:
        a = [ctx.get_motherboard_voltage(s) for s in sensors]
        a += [ctx.get_motherboard_current(fpga.RAIL.MB_VCC12V0)]
        b =  ctx.get_motherboard_temperature(fpga.TEMPERATURE_SENSOR.MB_FPGA_DIE)
        yield ctx._tuber_flush_async()
    async_return(  [aa.result() for aa in a]+[b.result()] )

#should be more elegant way...but list fields here to stay compatible
hk_fields_list = [ "VCC1V0", "VCC1V0_GTX", "VCC12V0", "VCC5V5", "VCC1V5", 
      "VCC1V2", "VCC3V3", "VCC1V8", "VADJ", "VCC12V0_curr", "core_temp"]

@async
def get_all_fpga_slots_hk(c):
        async_return( (yield [get_fpga_hk.async(cc) for cc in c])  )

# FPGA housekeeping.
fpga_hk_field = {      "core_temp" : "deg C",
                        "VCC1V0" : "V",
                        "VCC1V0_GTX" : "V",
                        "VCC12V0" : "V",
                        "VCC12V0_curr" : "A",
                        "VCC5V5" : "V",
                        "VCC1V5" : "V",
                        "VCC1V2" : "V",
                        "VCC3V3" : "V",
                        "VCC1V8" : "V",
                        "VADJ" : "V",
                }

# Backplane serial number---eventually this should be queried directly from the
# hardware!
crate_sn = "K7BP16-0004"

# Dictionary of correlators.
correlator_hash = {
                 "stone"        : ["0001"],
                 "abbot"        : ["0003"],
                 "vincente"     : ["29821-0000-0028"],
                 "blanchard"    : ["0029","0030"],
                 "slot7"      : ["0031", "0032"],
                 "first9ucrate" : ["0034"],
                 "testing2": ["0031"],
                 "slot16":['0034', '0036'],
                 "slot15":['0005', '0006'],
                 "slot14":['0038', '0040'],
                 "slot13":['0027', '0039'],
                 "slot12":['0037', '0016'],
                 "slot11":['0015', '0014'],
                 "slot10":['0023', '0022'],
                 "slot9":['0025', '0024'],
                 "slot8":['0019', '0020'],
                 "slot6":['0042', '0008'],
                 "slot5":['0011', '0012'],
                 "slot4":['0004', '0021'],
                 "slot3":['0026', '0013'],
                 "slot2":['0018', '0017']
                }


# Current archive format version. Prefixed by "NT_" to signify that these data
# do not have the time-transpose completed.
archive_version = "NT_2.2.0"

class FpgaBitstream(object):
        """ Helper object used to load and store a FPGA bitstream. You don't have
        to use it, but it makes the code look nicer"""
        bitstream = None

        def __init__(self, filename):
            with open(filename, 'rb') as file_:
                    self.bitstream = file_.read()

        def __str__(self):
            """ Return the bitstream as a string. """
            return self.bitstream

remap_adc_sma =  [12,13,14,15,8,9,10,11,4,5,6,7,0,1,2,3]
remap_slot = [5,1,4,0,13,9,12,8,15,11,14,10,7,3,6,2]

def load_gains(ib, bank=0):
    for cc in ib:
        filename = '/home/kbandura/ch_acq/gains_slot'+str(cc.slot)+'.pkl'
        try:
            g_array = pickle.load(open(filename, 'rb'))
            log = logging.getLogger()
            log.info("setting gains on SN {0}, SLOT {1}".format(ib.serial, ib.slot))
            cc.set_gain(g_array, bank=bank)  # *** should this be bank=all_bank
        except IOError:
            log = logging.getLogger()
            log.warn('Could not load gain file %s. Gains are not set.' % filename)
            raise

if __name__ == "__main__":
    # Set up logger.
    log = logging.getLogger("")
    log.handlers = []  # Clear all existing handlers
    log.setLevel(logging.DEBUG)
    log_stdout = logging.StreamHandler(sys.stdout)
    log_stdout.setLevel(logging.DEBUG)
    log_fmt = logging.Formatter("%(asctime)s %(levelname)s >> %(message)s", \
                                                            "%b %d %H:%M:%S")
    log_stdout.setFormatter(log_fmt)
    log.addHandler(log_stdout)

    # Debugging log, this should be removed/moved to data dir also
    # Start writing to a log file in this directory.
    logname = "ch_master_debug.log"
    log_to_file = logging.FileHandler(logname)
    log_to_file.setLevel(logging.DEBUG)
    log_to_file.setFormatter(log_fmt)
    log.addHandler(log_to_file)

    # Get command line arguments.
    parser = argparse.ArgumentParser(description = __doc__.split('\n')[0])
    parser.add_argument("-g", "--git-tag", action = "store",
                                        default = "",
                                        help = "Current git tag, use: "
                                                   "-g `git describe --tags` ")
    parser.add_argument("-c", "--conf_file", action = "store",
                                        default = "config.yaml:kmb_dev_wvu1",
                                        help = "Configuration file:object.")
    parser.add_argument("-n", "--notes", action = "store",
                                        default = "None.",
                                        help = "Acquisition notes.")
    parser.add_argument("-s", "--spec_file", action = "store",
                                        default = "ch_master.spec",
                                        help = "Configuration file specifications.")
    parser.add_argument("-a", "--compute_gain", action = "store",
                                        default = 0,
                                        help = "1 to calculate and save FFT scaler gains")
    parser.add_argument("-f", "--configure_fpga", action = "store",
                                        default = 0,
                                        help = "1 configure and control fpga.  0 to ignore fpga and just get data from gpu")

    args = parser.parse_args()

    # Be paranoid: if the executable is being run from /usr/sbin we can be
    # reasonably assured that the git tag recorded in /etc/CHIME is correct. If it
    # is not being run from there, force the user manually insert the git tag as
    # an option.
    if sys.argv[0] != "/usr/sbin/ch_master.py":
        if not len(args.git_tag):
            print "If you are not running this as a daemon from \"/usr/sbin\", you "
            print "MUST use the -g option and manually specify which git tag you are "
            print "running (e.g., ./ch_master -g `git describe --tags`)."
            exit()

    # val_conf = Validator()
    # conf = ConfigObj(args.conf_file, configspec = args.spec_file)
    # ret = conf.validate(val_conf, preserve_errors = True)

    conf_dict = fpga_array.load_yaml_config(args.conf_file)
    conf = NameSpace(conf_dict)
    conf_dict['fpga'].pop('fpga_array_params')
    print conf_dict
    fpga_array_params = conf.fpga.pop('fpga_array_params')
    # if ret != True:
    #     for entry in flatten_errors(conf, ret):
    #         sec_list, key, error = entry
    #         if key is not None:
    #             sec_list.append(key)
    #         else:
    #             sec_list.append("[missing section]")
    #         sec_string = ".".join(sec_list)
    #         if error == False:
    #             error = "Missing value or section."
    #         log.critical("Error parsing %s: %s" % (sec_string, error))
    #     exit()

    # Build up the adc_delay_table.
    # Should be 16 different sets of 16.  Have a pickle file, change
    # this to point to it and use each when programming the fpga.
    if (int(args.configure_fpga) > 0):
        n = 16 #int(conf["n_antenna"])
        adc_delay = []
        for i in range(n):
            name = "ch%02d" % i
            tmp_delay = []
            if not name in conf.fpga.adc_delay:
                log.critical("Could not find fpga.adc_delay.%s entry in " \
                                         "configuration file." % (name))
                exit()
            else:
                this_chan = conf.fpga.adc_delay[name]
            for j in range(len(this_chan)):
                k = int(this_chan[j])
                tmp_delay.append(k)
            if len(tmp_delay) != 16:
                log.critical("Entry fpga.adc_delay.%s needs 16 integer entries." % \
                                         (name))
                exit()
            adc_delay.append((tmp_delay[:8], tmp_delay[8:]))

    # Create the acquisition object. Pass it the configuration settings so that it
    # can initialise.
    acq = chrx.acq(conf_dict, log, 16, fpga_hk_field) if chrx else None

    if (int(args.configure_fpga) > 0):
            # Create the FPGA controller object.
            # Will now create an array of controller objects indexed by serial number
            # And program board firmware if needed/requested currently will always reprogram
            ca = fpga_array.FPGAArray(**fpga_array_params)     # Create FPGA array
            sync_board = ca.ib.get(serial=conf.fpga.sync_board) if conf.fpga.sync_board else None
            ca.set_sync_method(conf.fpga.sync_method, source='bp_trig', master=sync_board, master_time_source='bp_time' if sync_board else None)

            # c = ca.ib

            # # ca = load_yaml(open('pychfpga/yaml_iceboard_list.txt'))
            # # close_all_sockets()
            # # IceArray.close_all_sessions()
            # # ca = IceArray(uri=conf.fpga.db_file, interface_ip_addr=conf.fpga.host_ip)
            # # Might want to move the list somewhere else/into conf file?
            # # ca.load_iceboards('/home/chime/ch_acq/pychfpga/iceboard_list.txt')
            # # ca.discover()
            # fpga_bitstream = FpgaBitstream(conf.fpga.bitfile_name)
            # ChimeFpgaFirmware.register_fpga_bitstream(fpga_bitstream)

            # # bitfile_filename = conf.fpga.bitfile_name
            # # fpga_bitstream = ca.get_fpga_bitstream(bitfile_filename, ChimeFpgaFirmware)
            # c = ca.query(IceBoardPlus).filter_by(subarray=conf.fpga.subarray)

            # for ib in c:
            #     if not ib.ping():
            #             ca.delete(ib)
            # ca.commit()
            # c.set_fpga_bitstream(force=conf.fpga.force)

            # c = ca.get_iceboards(subarray=[conf.fpga.subarray]).index_by(IceBoard.serial_number)
            # c.set_fpga_firmware(fpga_bitstream, force=conf.fpga.force)
            # c.discover_mezzanines()
            # c.discover_crate()
            # c.open( \
            #             adc_delay_table=adc_delay, \
            #             init=1, \
            #             sampling_frequency=conf.fpga.samp_freq * 1e6, \
            #             reference_frequency=conf.fpga.ref_freq, \
            #             data_width=conf.fpga.data_width, \
            #             group_frames=conf.fpga.group_frames, \
            #             enable_gpu_link = conf.fpga.enable_gpu_link)
            # #Temp solution to load adc_delay from table...
            try:
                delays = pickle.load(open('pychfpga/delays_aug_2015.pkl'))
                for ib in ca.ib:
                    ib.set_adc_delays_with_check(delays[int(ib.serial)])
                    log.info("set delays on SN {0}, SLOT {1}".format(ib.serial, ib.slot))
            except IOError:
                log.warn("Error loading/setting delay tables.  Using default delays from config file for all boards")
            #for cc in c:
            #  cc.GPU.LINK_ENABLE=1
            #  log.info("GPU link enabled on SN {0}, SLOT {1}".format(cc.serial, cc.slot))
            if not ca.ib:
                raise RuntimeError('No IceBoard could be found. Are the boards powered up? Is the networking functional?')

            ca.ib.set_corr_reset(1)
            time.sleep(0.1)
            ca.ib.set_corr_reset(0)
            # fpga = chFPGA_controller.chFPGA_controller( \
            #            ip_address = conf.fpga.ip_address, \
            #            port_number = conf.fpga.port, \
            #            adc_delay_table = adc_delay, \
            #            verbose = 0, \
            #            init = 1, \
            #            sampling_frequency = conf.fpga.samp_freq * 1e6, \
            #            reference_frequency = conf.fpga.ref_freq, \
            #            data_width=conf.fpga.data_width, \
            #            group_frames=conf.fpga.group_frames, \
            #            enable_gpu_link = conf.fpga.enable_gpu_link, \
            #            host_ip = conf.fpga.host_ip)


            # Set FPGA controller parameters.
            # Calculate new gains if necessary
            # Get config here to be able to create receiver object
            # Gains will need to be able to handle multiple boards, currently file
            # Will be overwritten when used for more than one board.
            # Make compute gains smarter -> write to db? need boards to actually be different

            # Get noise injection parameters
            gpu_intergration_period = conf.gpu.gpu_intergration_period
            ni_board = conf.fpga.ni_board
            ni_enable = conf.fpga.ni_enable
            ni_offset = conf.fpga.ni_offset * gpu_intergration_period
            ni_high_time = conf.fpga.ni_high_time * gpu_intergration_period - 1 # the -1 is due to the convention in function set_frame_pwm()
            ni_period = conf.fpga.ni_period * gpu_intergration_period - 1
            ni_board_26m = conf.fpga.ni_board_26m
            ni_enable_26m = conf.fpga.ni_enable_26m
            ni_offset_26m = conf.fpga.ni_offset_26m * gpu_intergration_period
            ni_high_time_26m = conf.fpga.ni_high_time_26m * gpu_intergration_period - 1 # the -1 is due to the convention in function set_frame_pwm()
            ni_period_26m = conf.fpga.ni_period_26m * gpu_intergration_period - 1

            if (int(args.compute_gain) > 0):
                    #Shouldn't need for loop here, but initial testing failed in parallel.
                    if ni_enable:
                            ca.set_noise_injection(ni_board, ni_enable, 0, 3, 4)
                            ni_board.sync()
                    if ni_enable_26m:
                            ca.set_noise_injection(ni_board_26m, ni_enable_26m, 0, 3, 4)
                            ni_board_26m.sync()
                    calculate_gain_slots = conf.fpga.calculate_gain_slots
                    for ib in ca.ib:
                        if ((ib.slot or -1) in calculate_gain_slots):
                                fpga_config = ib.get_config()
                                calculate_gains.calculate_gains(ib, str(ib.fpga_port_number + 1))

            all_chan = range(16)  # range(conf["n_antenna"])
            ca.ib.set_data_source("adc")  # This should come first.
            ca.ib.set_FFT_bypass(False, channels=all_chan)
            ca.ib.set_FFT_shift(conf.fpga.fft_shift, channels=all_chan)

            # Load and set the gains
            load_gains(ca.ib, bank=0)

            for bankset in ca.ib.get_current_gain_bank():
                    log.info('Using gain banks %s' % ( ', '.join([str(i) for i in bankset])))

            # set to only change when at configured frame number
            ca.ib.set_synchronized_gain_switching(enable=1)
            # set frame number to switch gains at.
            gain_switch_frame = conf.fpga.gain_switch_frame
            ca.ib.set_gain_switch_frame_number(frame=gain_switch_frame)

            # set to use bank 1 next, change in loop below. have to do this after config to wait for
            # frame number
            for bankset in ca.ib.get_current_gain_bank():
                    log.info('Using gain banks %s' % ( ', '.join([str(i) for i in bankset])))

            for enabled_sync in ca.ib.get_synchronized_gain_switching():
                    log.info('gain sync status is %s' % ( ', '.join([str(i) for i in enabled_sync])))

            for frames_set in ca.ib.get_gain_switch_frame_number():
                    log.info('gain sync frame is %s' % ( ', '.join([str(i) for i in frames_set])))

            # for i, c_element in enumerate(c):
            #   gain_pkl_file = open('/home/chime/ch_acq/gains_'+str(c_element.fpga.GPIO.FPGA_SERIAL_NUMBER)+'.pkl', "rb")
            #   gains = pickle.load(gain_pkl_file)
            #   c_element.fpga.set_gain(gains, channels = all_chan)
            ca.ib.sync()
            #ca.ib.set_send_flags()
            ca.ib.set_offset_binary_encoding(True)
            # ca.ib.sync()
            # # Get sync_board. Currently board SN0008 (slot 16)
            # sync_board = conf.fpga.sync_board
            ## enable slow stream
            for ib in ca.ib:
                    ib.set_local_data_port_number((ib.slot or 1) + 41100)
                    ib.start_data_capture(period=30, source='adc', offset=(ib.slot or 1) - 1)
            # This is another hack. Have to fix it for DRAO. REALLY: HAVE TO CHANGE IT
            # shuffle_init(list(c),sync_board,frames_per_packet=4, cb1_lanes=16, cb1_bins=64, cb2_lanes=16, cb2_bins=8, cb2_bypass=0, remap=True )
            d_slots = conf.fpga.destination_slots #[int(ii) for ii in conf.fpga.destination_slots]
            # shuffle_init(list(c), ni_board, ni_board_26m, sync_board, dsmap = d_slots, frames_per_packet=conf.fpga.group_frames, cb1_lanes=16, cb1_bins=64, cb2_lanes=16, cb2_bins=8, cb2_bypass=0, remap=True,
            #                          ni_enable = ni_enable, ni_offset = ni_offset,
            #                          ni_high_time = ni_high_time, ni_period = ni_period,
            #                          ni_enable_26m = ni_enable_26m, ni_offset_26m = ni_offset_26m,
            #                          ni_high_time_26m = ni_high_time_26m, ni_period_26m = ni_period_26m,
            #                          window_start=0, window_stop=50)

            # Setup noise injection enable PWM signals
            if ni_board:
                ca.set_noise_injection(ni_board, ni_enable, ni_offset, ni_high_time, ni_period)
            if ni_board_26m:
                ca.set_noise_injection(ni_board_26m, ni_enable_26m, ni_offset_26m, ni_high_time_26m, ni_period_26m)
            # Initialize data shufling and transmission to the GPU
            ca.set_operational_mode(conf.fpga.operational_mode, frames_per_packet=fpga_array_params.group_frames)
            ca.sync()  # synchronize all the boards in the array

            time.sleep(2)
            #shuffle_init(list(c), ni_board, ni_board_26m, sync_board, dsmap = d_slots, frames_per_packet=4, cb1_lanes=16, cb1_bins=64, cb2_lanes=16, cb2_bins=8, cb2_bypass=0, remap=True,
            #             ni_enable = ni_enable, ni_offset = ni_offset,
            #             ni_high_time = ni_high_time, ni_period = ni_period,
            #             ni_enable_26m = ni_enable_26m, ni_offset_26m = ni_offset_26m,
            #             ni_high_time_26m = ni_high_time_26m, ni_period_26m = ni_period_26m,
            #             window_start=200, window_stop=200)

            #Make sure FPGA throttling is fast enough to send all the data
            #FPGA doesn't seem to change this without a reset...
            #read_rate = int(np.floor(np.log2(conf.fpga.int_period * 4 * 125e6 / \
            #                2 / (conf["n_antenna"] * (conf["n_antenna"] + 1)))))
            #fpga.GPIO.HOST_FRAME_READ_RATE = read_rate

            # Start the correlator.
            ##fpga.start_corr_capture(integration_period = conf.fpga.int_period)
            #log.info("Correlator started with an integration time of %.1f s" % \
            #         (conf.fpga.int_period))




            #Read the FPGA setting back from the FPGA
            fpga_conf = {ib.slot:ib.get_config() for ib in ca.ib}

            # Create the output directory.
            time_str = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            corr_name = None
            if (len(fpga_conf) == 1):
                fpga_conf1 = fpga_conf[fpga_conf.keys()[0]]
                for corr, ser_list in correlator_hash.iteritems():
                    not_found = False
                    if type(fpga_conf1.adc_serial) is list:
                        for ser in fpga_conf1.adc_serial:
                            if not ser in ser_list:
                                not_found = True
                                break
                    else:
                        print fpga_conf1.adc_serial
                        if not fpga_conf1.adc_serial in ser_list:
                                not_found = True
                    if not_found:
                        corr_name = "testing_board" #repr(ca.ib[0])
                        continue
                    corr_name = corr
                    break
            else:
                #Assume array is whole pathfinder.
                #need to change this
                corr_name = 'pathfinder'
            if not corr_name:
                try:
                    log.critical("Could not find hash for ADC serial numbers %s." %
                                             fpga_conf.adc_serial)
                except KeyError:
                    log.critical("Could not find key \"adc_serial\" in FPGA configuration.")
                exit()
    else:
            time_str = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            corr_name = "NoFPGA_information"

    acq_base_dir = "%s/%s_%s_corr" % (conf.acq.base_path, time_str, \
                                                                     corr_name)
    os.makedirs(acq_base_dir)
    if not os.path.exists(acq_base_dir):
        log.critical("Could not create directory \"%s\"." % (acq_base_dir))
        exit()

    # Create a symbolic link to the output directory.
    if conf.acq.curfile:
        os.unlink(conf.acq.curfile)
        os.symlink("%s/%s_%s_corr" % (conf.acq.base_path, time_str, corr_name),
                             conf.acq.curfile)

    # Lock the logfile.
    log_file_lock = "%s/.ch_master.log.lock" % acq_base_dir
    fp = open(log_file_lock, "w")
    if not fp:
        log.error("Could not create lockfile \"%s\"." % log_file_lock)
    else:
        fp.close()

    # Start writing to a log file in this directory.
    acq_log_path = "%s/ch_master.log" % (acq_base_dir)
    log_file = logging.FileHandler(acq_log_path)
    log_file.setLevel(logging.DEBUG)
    log_file.setFormatter(log_fmt)
    log.addHandler(log_file)
    log.info("Now logging to \"%s\"." % (acq_log_path))

    log.info("Sampling frequency is %0.3f MHz." %
             float(fpga_array_params.samp_freq))

    if acq:
        if (int(args.configure_fpga) > 0):
                # Pass FPGA configuration variables to header.
                for fpga_slot, slot_conf in fpga_conf.items():
                    #print slot_conf.config_protocol_version
                    for name, conf_values in vars(slot_conf).items():
                        if name == 'antenna_scaler_gain':
                            # Eventually, the gains will be updated whenever they change,
                            # presumably by moving this call somewhere in the loop at the end
                            # of this program.
                            for val in conf_values:
                                v = convert_types(val)
                                if fpga_slot:
                                    inp = remap_slot[fpga_slot-1] * 16 + remap_adc_sma[int(val[0])]
                                else:
                                    inp = int(val[0])
                                acq.pass_fpga_gain(inp, v)
                        else:
                            val = conf_values
                            val = convert_types(val)
                            name = 'Slot_'+ str(fpga_slot) + '_' + name
                            acq.add_header_item(name, val)
        else:
            acq.add_header_item("fpga_info",
                                                    "no communication with fpga for this dataset")

        # Add some acquisition information to the header, for kicks.
        acq.add_header_item("system_user", getpass.getuser())
        acq.add_header_item("collection_server", socket.gethostname())
        acq.add_header_item("instrument_name", corr_name)
        acq.add_header_item("archive_version", archive_version)
        acq.add_header_item("acquisition_name", "%s_%s_corr" % (time_str, corr_name))
        acq.add_header_item("acquisition_type", "corr")

    # Get the git tag and write it to the header.
    if not len(args.git_tag):
        fp = open("/etc/CHIME/version", "r")
        if not fp:
            log.critical("Could not find git tag in \"/etc/CHIME/version\".")
            exit()
        tag = fp.read().replace("\n", "")
    else:
        tag = args.git_tag
    log.info("Git version is %s." % (tag))

    if acq:
        acq.add_header_item("git_version_tag", tag)

        # Add the user notes.
        acq.add_header_item("notes", args.notes)

        # Start the acquisition.        
        acq.start(acq_base_dir, crate_sn, int(conf.fpga.subarray))

    if (int(args.configure_fpga) > 0):
        ca.ib.CROSSBAR.LANE_MONITOR_RESET=1
        ca.ib.CROSSBAR.LANE_MONITOR_RESET=0
        ca.ib.CROSSBAR2.LANE_MONITOR_RESET=1
        ca.ib.CROSSBAR2.LANE_MONITOR_RESET=0
        ca.ib.CROSSBAR.LANE_MONITOR_SEL = 6
        ca.ib.CROSSBAR2.LANE_MONITOR_SEL = 6

    gains_reloaded = False
    hdf5_gains_switched = False
    bank_switched = True
    hk_rate_in_frames = int(conf.acq.fpga_hk.rate / 2.56e-6)
    poll_rate = conf['acq']['acq_loop_poll_rate'] #in seconds
    poll_rate_in_frames = poll_rate/2.56e-6  #should use fpga config frequency?
    reload_gains_frame = conf.fpga.reload_gains_frame
    frame_range = 2*poll_rate_in_frames
    enable_gain_switching = conf.acq.enable_gain_switching
    bank_switch_frame = conf.fpga.bank_switch_frame
    gain_reload_period = conf.fpga.gain_reload_period
    if ( int(args.configure_fpga) > 0):
            # init gains function kind of a hack.  Should fix.
            current_bank = 0
            next_bank = 1
            ca.ib.set_next_gain_bank(bank=next_bank)
            all_next_bank = ca.ib.get_next_gain_bank() 
            for bankset in all_next_bank:
                    log.info('Set next gain bank to %s' % ( ', '.join([str(i) for i in bankset])))
            all_banks = ca.ib.get_current_gain_bank()
            for bankset in all_banks:
                    log.info('currently using gain banks %s' % ( ', '.join([str(i) for i in bankset])))


    try:
        while True:
            # Pass the acquisition object the board temperatures. This is a temporary
            # way of doing this!  Check on frame number.  If less than 10sec from 'reload_gains_time'
            # then start reloading the gains. and switch banks.
            # If less than 10s from gains_switch_time, Change to new gains into hdf5 file.
            #
            if (int(args.configure_fpga) > 0):
                #try:
                         #fpga_frame_count = c[0].get_frame_number()
                         #if ((fpga_frame_count % hk_rate_in_frames) < poll_rate_in_frames):
                         #    hk_return = get_all_fpga_slots_hk(c)
                         #    i = 0
                         #    for hk in hk_return:
                         #      acq.pass_fpga_amb_temp(i, {hk_fields_list[0] : hk})
                         #      i += 1
                                     #log.debug("Slot number: %d "  % c_element.slot )
                                     #log.debug("Crossbar1 fifo overflow %d "  % c_element.CROSSBAR.CB1_LANE_MONITOR )
                                     #log.debug("Crossbar2 fifo overflow %d "  % c_element.CROSSBAR2.CB2_LANE_MONITOR )
                         #    log.info("Read FPGA housekeeping.")
                #except:
                #     log.critical("Did not get FPGA housekeeping, still aquiring data...")
                         #Right now can miss gain setting stuff if hk takes more than 10s.  Really need to disentangle the two.
                #print enable_gain_switching
                if ( enable_gain_switching  > 0):
                  try:
                    time.sleep(0.1)
                    true_fpga_frame_count = ca.ib[0].get_frame_number()
                    fpga_frame_count = true_fpga_frame_count % gain_reload_period
                    #print fpga_frame_count
                    try:
                        # Well before switch time.  Set gains in next bank, read back what we set.
                        if (abs(fpga_frame_count - reload_gains_frame) < frame_range) and not gains_reloaded:
                                load_gains(ca.ib, bank=next_bank)
                                log.debug("Loaded gains")
                                fpga_gains = {}
                                for ib in ca.ib:
                                    if ib.slot == None:
                                        fpga_gains[0] = ib.get_gain(bank=next_bank)
                                        log.debug("read back gain from slot")
                                    else:
                                        fpga_gains[ib.slot] = ib.get_gain(bank=next_bank)
                                #fpga_gains = {ib.slot: ib.get_gain(bank=next_bank) for ib in ca.ib}
                                gains_reloaded = True
                                bank_switched = False
                                log.info("Loaded gains into bank %d" % next_bank)
                                log.info("Frame number %d" % fpga_frame_count)
                                log.info("True frame number %d" % true_fpga_frame_count)
                                all_banks = ca.ib.get_current_gain_bank()
                                for bankset in all_banks:
                                        log.info('Using gain banks %s' % ( ', '.join([str(i) for i in bankset])))
                        #log.debug("checked for reload gain time")
                        # Right before switch time
                        if (abs(fpga_frame_count - (gain_switch_frame + gpu_intergration_period)) < frame_range) and not hdf5_gains_switched and gains_reloaded:
                                for fpga_slot, slot_gain in fpga_gains.items():
                                        for val in slot_gain:
                                                v = convert_types(val)
                                                if fpga_slot < 1:
                                                    inp = int(val[0])
                                                else:
                                                    inp = remap_slot[fpga_slot-1] * 16 + remap_adc_sma[int(val[0])]
                                                inp = int(inp)
                                                acq.pass_fpga_gain(inp, v)
                                hdf5_gains_switched = True
                                log.debug('Changed gains in hdf5 file')
                                log.info("Frame number %d" % fpga_frame_count)
                                log.info("True frame number %d" % true_fpga_frame_count)
                        #log.debug("checked for switch gains in hdf5 file time")
                        #shortly after after switch
                        if (abs(fpga_frame_count - (bank_switch_frame+gpu_intergration_period)) < frame_range) and not bank_switched and hdf5_gains_switched:
                                ca.ib.set_next_gain_bank(bank = current_bank)
                                current_bank = (current_bank + 1) % 2
                                next_bank = (next_bank + 1) % 2
                                new_gain_switch_frame = (1+(true_fpga_frame_count / gain_reload_period))*gain_reload_period + gain_switch_frame
                                ca.ib.set_gain_switch_frame_number(frame=new_gain_switch_frame)
                                gains_reloaded = False
                                hdf5_gains_switched = False
                                bank_switched = True
                                log.debug("changed which gain bank will be written to over to %d" % next_bank)
                                log.info("Frame number %d" % fpga_frame_count)
                                log.info("True frame number %d" % true_fpga_frame_count)
                                all_banks = ca.ib.get_current_gain_bank()
                                for bankset in all_banks:
                                        log.info('Using other gain banks %s' % ( ', '.join([str(i) for i in bankset])))
                        #log.debug("checked for gain back switch prep time")
                    except:
                        log.critical("something went wrong with gain switching, still aquiring data...")
                        raise
                  except:
                    log.info("couldn't read fpga frame number... will try again.")
                    raise
                else:
                    print "what going on!"
            else:
                log.info("acquiring data...")
            time.sleep(poll_rate)
            hk_return = ca.ib[0].get_motherboard_temperature(ca.ib[0].TEMPERATURE_SENSOR.MB_FPGA_DIE)
            print hk_return
    except(KeyboardInterrupt, SystemExit):
        raise
        #if acq:
        #    acq.stop()
    finally:
        #if acq:
        #    acq.stop()
        raise

if acq:
    signal.signal(signal.SIGTERM, acq.stop)

# Remove log file lock and exit.
os.remove(log_file_lock)
log.info("Exiting ch_master now.")
