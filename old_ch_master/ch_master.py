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
        with fpga.tuber_context() as ctx:
                t = yield ctx.get_motherboard_temperature(fpga.TEMPERATURE_SENSOR.MB_FPGA_DIE)
        # ret["VCC1V0"] = fpga.get_motherboard_voltage(fpga.RAIL.MB_VCC1V0)
        # ret["VCC1V0_GTX"] = fpga.get_motherboard_voltage(fpga.RAIL.MB_VCC1V0_GTX)
        # ret["VCC12V0"] = fpga.get_motherboard_voltage(fpga.RAIL.MB_VCC12V0)
        # ret["VCC12V0_curr"] = fpga.get_motherboard_current(fpga.RAIL.MB_VCC12V0)
        # ret["VCC5V5"] = fpga.get_motherboard_voltage(fpga.RAIL.MB_VCC5V5)
        # ret["VCC1V5"] = fpga.get_motherboard_voltage(fpga.RAIL.MB_VCC1V5)
        # ret["VCC1V2"] = fpga.get_motherboard_voltage(fpga.RAIL.MB_VCC1V2)
        # ret["VCC3V3"] = fpga.get_motherboard_voltage(fpga.RAIL.MB_VCC3V3)
        # ret["VCC1V8"] = fpga.get_motherboard_voltage(fpga.RAIL.MB_VCC1V8)
        # ret["VADJ"] = fpga.get_motherboard_voltage(fpga.RAIL.MB_VADJ)
        async_return(t)

#should be more elegant way...but list fields here to stay compatible
hk_fields_list = ["core_temp"]

@async
def get_all_fpga_slots_hk(c):
        async_return( (yield [get_fpga_hk.async(cc) for cc in c])  )

# FPGA housekeeping.
fpga_hk_field = {      "core_temp" : "deg C",
                 # "VCC1V0" : "V",
                 # "VCC1V0_GTX" : "V",
                 # "VCC12V0" : "V",
                 # "VCC12V0_curr" : "A",
                 # "VCC5V5" : "V",
                 # "VCC1V5" : "V",
                 # "VCC1V2" : "V",
                 # "VCC3V3" : "V",
                 # "VCC1V8" : "V",
                 # "VADJ" : "V",
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

remap_adc_sma =  [12, 13, 14, 15, 8, 9, 10, 11, 4, 5, 6, 7, 0, 1, 2, 3]
remap_slot = [5, 1, 4, 0, 13, 9, 12, 8, 15, 11, 14, 10, 7, 3, 6, 2]

def load_gains(ib, bank=0):
    for cc in ib:
        filename = '/home/chime/ch_acq/gains_slot'+str(cc.slot)+'.pkl'
        try:
            g_array = pickle.load(open(filename, 'rb'))
            print 'Setting gains on IceBoard SN%s, slot %i' % (cc.serial, cc.slot)
            cc.set_gain(g_array, bank=bank)  # *** should this be bank=all_bank
        except IOError:
            log = logging.getLogger()
            log.warn('%.32r: Could not load gain file %s. Gains are not set.' % ('ch_master.load_gains', filename))

if __name__ == "__main__":
    # Set up logger.
    log = logging.getLogger("")
    log.handlers = []  # Clear all existing log handlers
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
                                        default = "config.yaml:pathfinder",
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
                                        default = 1,
                                        help = "1 configure and control fpga.  0 to ignore fpga and just get data from gpu")

    args = parser.parse_args()

    self = 'ch_master'  # use until ch_master is an object

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

    conf = NameSpace(fpga_array.load_yaml_config(args.conf_file))
    fpga_array_params = conf.fpga.pop('fpga_array_params')
    adc_delay_params = conf.fpga.pop('adc_delay_params')
    # I separate fpga_array_params from conf since the writer to the hdf5 file cannot
    # handle the hardware map object. fpga_array_params is used to program the boards.
    # However, the writer still needs some parameters from fpga_array_params so
    # have to add the manually.
    for key in fpga_array_params.keys():
        if key != 'hwm':
            conf.fpga[key] = fpga_array_params[key]

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


    # JFC: Removed delay loading. This is now done below with ca.set_adc_delays().

    # if (int(args.configure_fpga) > 0):
    #     n = 16 #int(conf["n_antenna"])
    #     adc_delay = []
    #     for i in range(n):
    #         name = "ch%02d" % i
    #         tmp_delay = []
    #         if not name in conf.fpga.adc_delay:
    #             log.critical("%.32r: Could not find fpga.adc_delay.%s entry in " \
    #                                      "configuration file." % (self, name))
    #             exit()
    #         else:
    #             this_chan = conf.fpga.adc_delay[name]
    #         for j in range(len(this_chan)):
    #             k = int(this_chan[j])
    #             tmp_delay.append(k)
    #         if len(tmp_delay) != 16:
    #             log.critical("%.32r: Entry fpga.adc_delay.%s needs 16 integer entries." % \
    #                                      (self, name))
    #             exit()
    #         adc_delay.append((tmp_delay[:8], tmp_delay[8:]))

    # Create the acquisition object. Pass it the configuration settings so that it
    # can initialise.
    acq = chrx.acq(conf, log, 16, fpga_hk_field) if chrx else None

    if (int(args.configure_fpga) > 0):
            # Create and initialize the FPGA objects and corresponding hardware.
            ca = fpga_array.FPGAArray(**fpga_array_params)
            if not ca.ib:
                raise RuntimeError('%.32r: No IceBoard could be found. Are the boards powered up? Is the networking functional?' % self)

            # is this needed?
            ca.ib.set_adc_mask(0) # (needed?) null the ADC data before it gets to the channelizers to reduce power consumption

            # Setting ADC delays
            log.info("Setting ADC delays")
            ca.ib.set_adc_delays(**adc_delay_params)

            # is this needed?
            ca.ib.set_corr_reset(1)
            time.sleep(0.1)
            ca.ib.set_corr_reset(0)

            # Get noise injection parameters
            ni = conf.fpga.ni
            ni_26m = conf.fpga.ni_26m

            # Calculate gains if requested
            if (int(args.compute_gain) > 0):
                if ni.board:
                    ca.set_noise_injection(board=ni.board, enable=ni.enable, offset=0, high_time=3, period=4, local_sync=True)
                if ni_26m.board:
                    ca.set_noise_injection(board=ni_26m.board, enable=ni_26m.enable, offset=0, high_time=3, period=4, local_sync=True)
                #Shouldn't need the 'for' loop here, but initial testing failed if those were called in parallel.
                for ib in ca.ib:
                    if ib.slot in conf.fpga.calculate_gain_slots:
                        fpga_config = ib.get_config()
                        calculate_gains.calculate_gains(ib, str(ib.fpga_port_number + 1))
            all_chan = range(16)  # range(conf["n_antenna"])
            ca.ib.set_data_source("adc")  # This should come first.
            ca.ib.set_FFT_bypass(False, channels=all_chan)
            ca.ib.set_FFT_shift(conf.fpga.fft_shift, channels=all_chan)

            ca.ib.set_synchronized_gain_switching(enable=0)
            ca.ib.set_next_gain_bank(bank=0)
            all_banks = ca.ib.get_current_gain_bank()

            # Load and set the gains
            log.info("%.32r: Loading initial gains" % self)
            load_gains(ca.ib, bank=0)

            for bankset in ca.ib.get_current_gain_bank():
                    log.info('%.32r: Using gain banks %s' % (self, ', '.join([str(i) for i in bankset])))

            gpu_intergration_period = conf.gpu.gpu_intergration_period
            enable_gain_switching = conf.acq.enable_gain_switching
            gain_switch_frame = conf.fpga.gain_switch_frame
            if enable_gain_switching > 0:
                # set frame number to switch gains at.
                ca.ib.set_gain_switch_frame_number(frame=gain_switch_frame)
                # set to only change when at configured frame number
                ca.ib.set_synchronized_gain_switching(enable=1)
                # set to use bank 1 next, change in loop below. have to do this after config to wait for
                # frame number

            for bankset in ca.ib.get_current_gain_bank():
                    log.info('%.32r: Using gain banks %s' % (self, ', '.join([str(i) for i in bankset])))

            for enabled_sync in ca.ib.get_synchronized_gain_switching():
                    log.info('%.32r: Gain sync status is %s' % (self, ', '.join([str(i) for i in enabled_sync])))

            for frames_set in ca.ib.get_gain_switch_frame_number():
                    log.info('%.32r: Gain sync frame is %s' % (self, ', '.join([str(i) for i in frames_set])))

            # for i, c_element in enumerate(c):
            #   gain_pkl_file = open('/home/chime/ch_acq/gains_'+str(c_element.fpga.GPIO.FPGA_SERIAL_NUMBER)+'.pkl', "rb")
            #   gains = pickle.load(gain_pkl_file)
            #   c_element.fpga.set_gain(gains, channels = all_chan)
            log.info("%.32r: Sending local sync to each board" % self)
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

            # Setup noise injection (enable PWM signals)
            if ni.board:
                ca.set_noise_injection(board=ni.board, enable=ni.enable, offset=ni.offset, high_time=ni.high_time, period=ni.period)
            if ni_26m.board:
                ca.set_noise_injection(board=ni_26m.board, enable=ni_26m.enable, offset=ni_26m.offset, high_time=ni_26m.high_time, period=ni_26m.period)
                   # Initialize data shufling and transmission to the GPU
            log.info("%.32r: Setting FPGA operational mode" % self)
            ca.set_operational_mode(conf.fpga.operational_mode, frames_per_packet=fpga_array_params.group_frames)
            log.info("%.32r: Synchronizing the array..." % self)

            ca.sync()  # synchronize all the boards in the array

            log.info("%.32r: Unmasking the ADC data" % self)
            ca.ib.set_adc_mask(0xFF) # restore normal ADC data

            log.info("%.32r: Waiting for 2 seconds" % self)
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
            log.info("%.32r: Getting configuration data from all FPGAs" % self)
            fpga_conf = {ib.slot:vars(ib.get_config()) for ib in ca.ib}

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
                        corr_name = repr(ca.ib[0])
                        continue
                    corr_name = corr
                    break
            else:
                #Assume array is whole pathfinder.
                #need to change this
                corr_name = 'pathfinder'
            if not corr_name:
                try:
                    log.critical("%.32r: Could not find hash for ADC serial numbers %s." %
                                             (self, fpga_conf.adc_serial))
                except KeyError:
                    log.critical("%.32r: Could not find key \"adc_serial\" in FPGA configuration." % self)
                exit()
    else:
            time_str = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            corr_name = "NoFPGA_information"

    acq_base_dir = "%s/%s_%s_corr" % (conf.acq.base_path, time_str, \
                                                                     corr_name)
    os.makedirs(acq_base_dir)
    if not os.path.exists(acq_base_dir):
        log.critical("%.32r: Could not create directory \"%s\"." % (self, acq_base_dir))
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
        log.error("%.32r: Could not create lockfile \"%s\"." % (self, log_file_lock))
    else:
        fp.close()

    # Start writing to a log file in this directory.
    acq_log_path = "%s/ch_master.log" % (acq_base_dir)
    log_file = logging.FileHandler(acq_log_path)
    log_file.setLevel(logging.DEBUG)
    log_file.setFormatter(log_fmt)
    log.addHandler(log_file)
    log.info("%.32r: Now logging to \"%s\"." % (self, acq_log_path))

    log.info("%.32r: Sampling frequency is %0.3f MHz." %
             (self, float(fpga_array_params.samp_freq)))

    if acq:
        if (int(args.configure_fpga) > 0):
                # Pass FPGA configuration variables to header.
                for fpga_slot, slot_conf in fpga_conf.items():
                    for name in slot_conf:
                        if name == 'antenna_scaler_gain':
                            # Eventually, the gains will be updated whenever they change,
                            # presumably by moving this call somewhere in the loop at the end
                            # of this program.
                            for val in slot_conf[name]:
                                v = convert_types(val)
                                inp = remap_slot[fpga_slot-1] * 16 + remap_adc_sma[int(val[0])]
                                acq.pass_fpga_gain(inp, v)
                        else:
                            val = slot_conf[name]
                            val = convert_types(val)
                            if name != 'antenna_adc_data_acquisition_delay_tables': # THE FORMAT OF THE DELAY TABLES HAS CHANGED. FOR NOW JUST NOT WRITE, BIT HAVE TO FIX THIS
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
            log.critical("%.32r: Could not find git tag in \"/etc/CHIME/version\"." % self)
            exit()
        tag = fp.read().replace("\n", "")
    else:
        tag = args.git_tag
    log.info("%.32r: Git version is %s." % (self, tag))

    # Stop the acq
    def stop_acq(*args):
      os.remove(log_file_lock)
      acq.stop()

    if acq:
        acq.add_header_item("git_version_tag", tag)

        # Add the user notes.
        acq.add_header_item("notes", args.notes)

        # Stop the acq on SIGTERM
        signal.signal(signal.SIGTERM, stop_acq)

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
    poll_rate = conf.acq.acq_loop_poll_rate #in seconds
    poll_rate_in_frames = poll_rate/2.56e-6  #should use fpga config frequency?
    reload_gains_frame = conf.fpga.reload_gains_frame
    frame_range = 2*poll_rate_in_frames

    bank_switch_frame = conf.fpga.bank_switch_frame
    gain_reload_period = conf.fpga.gain_reload_period  #in frames
    if ( int(args.configure_fpga) > 0):
            # init gains function kind of a hack.  Should fix.
            current_bank = 0
            next_bank = 1
            if (enable_gain_switching > 0):
                ca.ib.set_next_gain_bank(bank=next_bank)
            all_next_bank = ca.ib.get_next_gain_bank()
            for bankset in all_next_bank:
                    log.info('%.32r: Set next gain bank to %s' % (self, ', '.join([str(i) for i in bankset])))
            all_banks = ca.ib.get_current_gain_bank()
            for bankset in all_banks:
                    log.info('%.32r: Currently using gain banks %s' % (self, ', '.join([str(i) for i in bankset])))

    #t0 = time.time()

    try:
        while True:

            # print board info at regular interval
            #t1 = time.time()
            #if t1-t0 > 60:
                #t0 = t1
                #try: ca.print_iceboard_info()
                #except: pass

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
              if ( int(enable_gain_switching) > 0):
                try:
                    time.sleep(0.1)
                    true_fpga_frame_count = ca.ib[0].get_frame_number()
                    fpga_frame_count = true_fpga_frame_count % gain_reload_period  #now need a reset since 48bit counter
                    try:
                        # Well before switch time.  Set gains in next bank, read back what we set.
                        if (abs(fpga_frame_count - reload_gains_frame) < frame_range) and not gains_reloaded:
                                load_gains(ca.ib, bank=next_bank)
                                fpga_gains = {ib.slot: ib.get_gain(bank=next_bank) for ib in ca.ib}
                                gains_reloaded = True
                                bank_switched = False
                                log.info("%.32r: Loaded gains into bank %d" % (self, next_bank))
                                all_banks = ca.ib.get_current_gain_bank()
                                for bankset in all_banks:
                                        log.info('%.32r: Using gain banks %s' % (self, ', '.join([str(i) for i in bankset])))
                        #log.debug("checked for reload gain time")
                        # Right before switch time
                        if (abs(fpga_frame_count - (gain_switch_frame + gpu_intergration_period)) < frame_range) and not hdf5_gains_switched and gains_reloaded:
                                for fpga_slot, slot_gain in fpga_gains.items():
                                        for val in slot_gain:
                                                v = convert_types(val)
                                                inp = remap_slot[fpga_slot-1] * 16 + remap_adc_sma[int(val[0])]
                                                if acq:
                                                    acq.pass_fpga_gain(inp, v)
                                hdf5_gains_switched = True
                                log.info('%.32r: Changed gains in hdf5 file' % self)
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
                                log.debug("%.32r: changed which gain bank will be written to over to %d" % (self, next_bank))
                                all_banks = ca.ib.get_current_gain_bank()
                                for bankset in all_banks:
                                        log.info('%.32r: Using gain banks %s' % (self, ', '.join([str(i) for i in bankset])))
                        #log.debug("checked for gain back switch prep time")
                    except:
                        log.critical("%.32r: something went wrong with gain switching, still aquiring data..." % self)
                except:
                    log.info("%.32r: couldn't read fpga frame number... will try again." % self)
              else:
                pass
            else:
                log.info("%.32r: acquiring data..." % self)
            time.sleep(poll_rate)
        stop_acq()
    except(KeyboardInterrupt, SystemExit):
        pass
    finally:
        if acq:
            stop_acq()

if acq:
    signal.signal(signal.SIGTERM, acq.stop)

# Remove log file lock and exit.
os.remove(log_file_lock)
log.info("%.32r:Exiting ch_master now." % self)
