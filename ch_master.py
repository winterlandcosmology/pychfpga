#!/usr/local/bin/python2.7

"""
Master control program for CHIME.
#
History:
2013-05-13 ADH: First version.
"""

import chrx
from configobj import *
from pychfpga.core.chFPGA_controller import chFPGA_controller as ChimeFpgaFirmware
from pychfpga.core import chFPGA_receiver
from pychfpga.icecore.icearray import IceArray, close_all_sockets
from pychfpga.icecore.iceboard import IceBoard
from validate import Validator
import argparse
import getpass
import logging
import numpy as np
import os
import sys
import socket
import time
import pickle
from pychfpga import calculate_gains
from pychfpga.init_links import *
#import MySQLdb

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

def get_fpga_hk(fpga, field):
  ret = {}
  for f in field.keys():
    if f == "core_temp":
      ret[f] = fpga.SYSMON.temperature()
    elif f == "vcc_int":
      ret[f] = fpga.SYSMON.voltage(fpga.SYSMON.VCCINT_ADDR)
    elif f == "vcc_aux":
      ret[f] = fpga.SYSMON.voltage(fpga.SYSMON.VCCAUX_ADDR)
    elif f == "12v_supply":
      ret[f] = fpga.SYSMON.voltage(fpga.SYSMON.VAUX_VOLT_ADDR, vref = 1.0)
    elif f == "12v_supply_curr":
      ret[f] = fpga.SYSMON.voltage(fpga.SYSMON.VAUX_CURR_ADDR, vref = 1.0)
    elif f == "vrefp":
      ret[f] = fpga.SYSMON.voltage(fpga.SYSMON.VAUX_VREFP_ADDR)
    elif f == "vrefn":
      ret[f] = fpga.SYSMON.voltage(fpga.SYSMON.VAUX_VREFN_ADDR)

  return ret

# Backplane serial number---eventually this should be queried directly from the
# hardware!
crate_sn = "K7BP16-0004"

# Dictionary of correlators.
correlator_hash = {"stone"        : ["0001"],
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

# FPGA housekeeping.
fpga_hk_field = {      "core_temp" : "deg C",
                         "vcc_int" : "V",
                         "vcc_aux" : "V",
                      "12v_supply" : "V",
                 "12v_supply_curr" : "A",
                           "vrefp" : "V",
                           "vrefn" : "V",
                }

# Current archive format version. Prefixed by "NT_" to signify that these data
# do not have the time-transpose completed.
archive_version = "NT_2.2.0"

remap_adc_sma =  [12,13,14,15,8,9,10,11,4,5,6,7,0,1,2,3]
remap_slot = [5,1,4,0,13,9,12,8,15,11,14,10,7,3,6,2]

if __name__ == "__main__":
  # Set up logger.
  log = logging.getLogger("")
  log.setLevel(logging.DEBUG)
  log_stdout = logging.StreamHandler(sys.stdout)
  log_stdout.setLevel(logging.DEBUG)
  log_fmt = logging.Formatter("%(asctime)s %(levelname)s >> %(message)s", \
                              "%b %d %H:%M:%S")
  log_stdout.setFormatter(log_fmt)
  log.addHandler(log_stdout)

  # Get command line arguments.
  parser = argparse.ArgumentParser(description = __doc__.split('\n')[0])
  parser.add_argument("-g", "--git-tag", action = "store", \
                      default = "", help = "Current git tag, use: " + \
                             "-g `git describe --tags` ")
  parser.add_argument("-c", "--conf_file", action = "store", \
                      default = "ch_master.conf", \
                      help = "Configuration file.")
  parser.add_argument("-n", "--notes", action = "store", default = "None.", \
                      help = "Acquisition notes.")
  parser.add_argument("-s", "--spec_file", action = "store", \
                      default = "ch_master.spec", \
                      help = "Configuration file specifications.")
  parser.add_argument("-a", "--compute_gain", action = "store", \
                       default = 0, \
                       help = "1 to calculate and save FFT scaler gains")
  parser.add_argument("-f", "--configure_fpga", action = "store", \
                       default = 1, \
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

  val_conf = Validator()
  conf = ConfigObj(args.conf_file, configspec = args.spec_file)
  ret = conf.validate(val_conf, preserve_errors = True)

  if ret != True:
    for entry in flatten_errors(conf, ret):
      sec_list, key, error = entry
      if key is not None:
        sec_list.append(key)
      else:
        sec_list.append("[missing section]")
      sec_string = ".".join(sec_list)
      if error == False:
        error = "Missing value or section."
      log.critical("Error parsing %s: %s" % (sec_string, error))
    exit()

  # Build up the adc_delay_table.
  # Should be 16 different sets of 16.  Have a pickle file, change
  # this to point to it and use each when programming the fpga.
  if (int(args.configure_fpga) > 0):
    n = 16 #int(conf["n_antenna"])
    adc_delay = []
    for i in range(n):
      name = "ch%02d" % i
      tmp_delay = []
      if not name in conf["fpga"]["adc_delay"]:
        log.critical("Could not find fpga.adc_delay.%s entry in " \
                     "configuration file." % (name))
        exit()
      else:
        this_chan = conf["fpga"]["adc_delay"][name]
      for j in range(len(this_chan)):
        k = int(this_chan[j])
        tmp_delay.append(k)
      if len(tmp_delay) != 16:
        log.critical("Entry fpga.adc_delay.%s needs 16 integer entries." % \
                     (name))
        exit()
      adc_delay.append((tmp_delay[:8],tmp_delay[8:]))

  # Create the acquisition object. Pass it the configuration settings so that it
  # can initialise.
  acq = chrx.acq(conf, log, 16, fpga_hk_field)
  if (int(args.configure_fpga) > 0): 
      # Create the FPGA controller object.
      # Will now create an array of controller objects indexed by serial number
      # And program board firmware if needed/requested currently will always
      # reprogram
      close_all_sockets()
      IceArray.close_all_sessions()
      ca = IceArray(uri=conf["fpga"]["db_file"],
                    interface_ip_addr=conf["fpga"]["host_ip"])
      # Might want to move the list somewhere else/into conf file?
      #ca.load_iceboards('/home/chime/ch_acq/pychfpga/iceboard_list.txt')
      ca.load_iceboards('/home/kbandura/git/ch_acq/pychfpga/iceboard_list.txt')
      ca.discover()
      bitfile_filename = conf["fpga"]["bitfile_name"]
      fpga_bitstream = ca.get_fpga_bitstream(bitfile_filename,
                                             ChimeFpgaFirmware)
      c = ca.get_iceboards(subarray=[conf["fpga"]["subarray"]]) \
                                    .index_by(IceBoard.serial_number)
      c.set_fpga_firmware(fpga_bitstream, force=conf["fpga"]["force"])
      # Will need to loop this with different delay tables, or reset delay
      # tables later....
      c.open( \
            adc_delay_table=adc_delay, \
            init=1, \
            sampling_frequency=conf["fpga"]["samp_freq"] * 1e6, \
            reference_frequency=conf["fpga"]["ref_freq"], \
            data_width=conf["fpga"]["data_width"], \
            group_frames=conf["fpga"]["group_frames"], \
            enable_gpu_link = conf["fpga"]["enable_gpu_link"])
      #Temp solution to load adc_delay from table...
      try:
          delays = pickle.load(open('pychfpga/delays_mar14_2015_no_errors.pkl'))
          for ice in c:                                             
              ice.fpga.set_adc_delays_with_check(delays[ice.serial_number])
              print "set delays on SN {0}, SLOT {1}".format(ice.serial_number, ice.slot_number)
      except:
          log.info("Error loading/setting delay tables.  Using default from config file for all boards")
      for cc in c:
        cc.fpga.GPU.LINK_ENABLE=1
        print "GPU link enabled on SN {0}, SLOT {1}".format(cc.serial_number, cc.slot_number)
      c.fpga.set_corr_reset(1)
      time.sleep(0.1)
      c.fpga.set_corr_reset(0)
      # fpga = chFPGA_controller.chFPGA_controller( \
      #            ip_address = conf["fpga"]["ip_address"], \
      #            port_number = conf["fpga"]["port"], \
      #            adc_delay_table = adc_delay, \
      #            verbose = 0, \
      #            init = 1, \
      #            sampling_frequency = conf["fpga"]["samp_freq"] * 1e6, \
      #            reference_frequency = conf["fpga"]["ref_freq"], \
      #            data_width=conf["fpga"]["data_width"], \
      #            group_frames=conf["fpga"]["group_frames"], \
      #            enable_gpu_link = conf["fpga"]["enable_gpu_link"], \
      #            host_ip = conf["fpga"]["host_ip"])


      # Set FPGA controller parameters.
      # Calculate new gains if necessary
      # Get config here to be able to create receiver object
      # Gains will need to be able to handle multiple boards, currently file
      # Will be overwritten when used for more than one board.  
      # Make compute gains smarter -> write to db? need boards to actually be different
      if (int(args.compute_gain) > 0):
          #Shouldn't need for loop here, but initial testing failed in parallel.
          for i, c_element in enumerate(c):
            fpga_config = c_element.fpga.get_config()
            #fpga_rec = chFPGA_receiver.chFPGA_receiver(fpga_config, \
            #              ip_address=c_element.fpga_ip_addr, \
            #              port=c_element.fpga_port_number+1, \
            #              host_ip = conf["fpga"]["host_ip"])
            calculate_gains.calculate_gains(c_element.fpga,str(c_element.fpga_port_number+1))
            #fpga_rec.close()
      all_chan = range(16)#range(conf["n_antenna"])
      c.fpga.set_data_source("adc") # This should come first.
      c.fpga.set_FFT_bypass(False, channels = all_chan)
      c.fpga.set_FFT_shift(conf["fpga"]["fft_shift"], channels = all_chan)
      # init gains function kind of a hack.  Should fix.  
      init_gains(c)
      # for i, c_element in enumerate(c):      
      #   gain_pkl_file = open('/home/chime/ch_acq/gains_'+str(c_element.fpga.GPIO.FPGA_SERIAL_NUMBER)+'.pkl', "rb")
      #   gains = pickle.load(gain_pkl_file)
      #   c_element.fpga.set_gain(gains, channels = all_chan)
      c.fpga.sync()
      c.fpga.set_send_flags()
      c.fpga.set_offset_binary_encoding()
      c.fpga.sync()
      shuffle_init(list(c),c[8],frames_per_packet=4, cb1_lanes=16, cb1_bins=64, cb2_lanes=16, cb2_bins=8, cb2_bypass=0, remap=1 )

      #Make sure FPGA throttling is fast enough to send all the data
      #FPGA doesn't seem to change this without a reset...
      #read_rate = int(np.floor(np.log2(conf["fpga"]["int_period"] * 4 * 125e6 / \
      #                2 / (conf["n_antenna"] * (conf["n_antenna"] + 1)))))
      #fpga.GPIO.HOST_FRAME_READ_RATE = read_rate

      # Start the correlator.
      ##fpga.start_corr_capture(integration_period = conf["fpga"]["int_period"])
      #log.info("Correlator started with an integration time of %.1f s" % \
      #         (conf["fpga"]["int_period"]))
      

      
      #Read the FPGA setting back from the FPGA 
      fpga_conf = {}
      for i, c_element in enumerate(c):
        fpga_conf[c_element.slot_number] = vars(c_element.fpga.get_config()) 
      
      # Create the output directory.
      time_str = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
      corr_name = None
      if (len(fpga_conf) == 1):
        fpga_conf1 = fpga_conf[fpga_conf.keys()[0]]
        for corr, ser_list in correlator_hash.iteritems():
          not_found = False
          if type(fpga_conf1["adc_serial"]) is list:
            for ser in fpga_conf1["adc_serial"]:
              if not ser in ser_list:
                not_found = True
                break
          else:
            print fpga_conf1["adc_serial"]
            if not fpga_conf1["adc_serial"] in ser_list:
                not_found = True
          if not_found:
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
                       fpga_conf["adc_serial"])
        except KeyError:
          log.critical("Could not find key \"adc_serial\" in FPGA configuration.")
        exit()
  else:
      time_str = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
      corr_name = "NoFGPA_information"

  acq_base_dir = "%s/%s_%s_corr" % (conf["acq"]["base_path"], time_str, \
                                   corr_name)
  os.makedirs(acq_base_dir)
  if not os.path.exists(acq_base_dir):
    log.critical("Could not create directory \"%s\"." % (acq_base_dir))
    exit()

  # Create a symbolic link to the output directory.
  os.unlink(conf["acq"]["curfile"])
  os.symlink("%s/%s_%s_corr" % (conf["acq"]["base_path"], time_str, corr_name),
             conf["acq"]["curfile"])

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
        
  log.info("Sampling frequency is %0.3f MHz." % \
           float(conf["fpga"]["samp_freq"]))

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
              inp = remap_slot[fpga_slot] * 16 + remap_adc_sma[int(val[0])]
              acq.pass_fpga_gain(inp, v)
          else:
            val = slot_conf[name]
            val = convert_types(val)
            name = 'Slot_'+ str(fpga_slot+1) + '_' + name
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
  acq.add_header_item("git_version_tag", tag)

  # Add the user notes.
  acq.add_header_item("notes", args.notes)

  # Start the acquisition.
  acq.start(acq_base_dir, crate_sn, int(conf["fpga"]["subarray"]))

  try:
    while True:
      # Pass the acquisition object the board temperatures. This is a temporary
      # way of doing this!
      if (int(args.configure_fpga) > 0):
        i = 0
        for c_element in c:
          acq.pass_fpga_amb_temp(i, get_fpga_hk(c_element.fpga, fpga_hk_field))
          i += 1
        log.info("Read FPGA housekeeping.")
      else:
        log.info("acquiring data...")
      time.sleep(conf["acq"]["fpga_hk"]["rate"])
    acq.stop()
  except(KeyboardInterrupt, SystemExit):
    acq.stop()

# Remove log file lock and exit.
os.remove(log_file_lock)
log.info("Exiting ch_master now.")
