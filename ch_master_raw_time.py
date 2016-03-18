#!/usr/local/bin/python2.7

"""
Master control program for CHIME raw adc acquisitions.
#
History:
2015-10-12 JM: First version.
"""

import configobj
import argparse
import logging
import os
import sys
import pickle
from validate import Validator
from pychfpga import chime_array


class ch_master(object):

  def __init__(self, argv=[], **kwargs):
    """
    Read and parse configuration file for raw adc mode operation

    See end of module for example on how to use this class

    Parameters
    ----------
    - conf_file: Configuration file.
    - spec_file: Configuration file specifications.

    The configuration files are specified either by parsing a list of command line
    arguments specified in ``argv`` or by directly using keyword arguments.

    Example::
      ch_master_raw_adc(argv=['--conf_file', 'ch_master_raw_adc.conf', '--spec_file', 'ch_master_raw_adc.conf'])  is equivalent to:
      ch_master_raw_adc(conf_file='ch_master_raw_adc.conf', spec_file='ch_master_raw_adc.conf')
    """

    # Process command line arguments
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0]) # description is the first line of the docstring

    parser.add_argument("-c", "--conf_file", action = "store", default = "ch_master_raw_time.conf", help = "Configuration file.")
    parser.add_argument("-s", "--spec_file", action = "store", default = "ch_master_raw_time.spec", help = "Configuration file specifications.")

    args = parser.parse_args()
    
    # Bring all keywoards argument into the args namespace
    for k, v in kwargs.items():
      setattr(args, k, v)

    # Get configuration from file
    self.val_conf = Validator()
    self.conf = configobj.ConfigObj(args.conf_file, configspec=args.spec_file)
    ret = self.conf.validate(self.val_conf, preserve_errors = True)

    # Setup log log
    log_level_dict = {'info': logging.INFO, 'debug': logging.DEBUG, 'warn': logging.WARNING, 'error': logging.ERROR}
    self.log_target = self.conf["log_target"]
    self.log_level = self.conf["log_level"]
    if self.log_target == 'stream':
      log_handler = logging.StreamHandler()
    elif self.log_target == 'syslog':
        log_handler = logging.handlers.SysLogHandler()
    else:
        log_handler = logging.FileHandler(self.log_target)

    self.log = logging.getLogger('')
    self.log.handlers = []  # Clear all existing handlers
    self.log.setLevel(logging.DEBUG)  # pass all messages to the handlers which will filter what they want
    # Set-up log for this test run
    log_handler.setLevel(log_level_dict[self.log_level])
    self.log.addHandler(log_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(log_level_dict[self.log_level])
    self.log.addHandler(stream_handler)
    self.log.info("Finished reading configuration file")


  def init(self):
    """
    Initialization of the CHIME hardware 
    """

    self.log.info("Creating hardware map and initializing hardware (chime_array)")
    self.ca = chime_array.ChimeArray(log_target=self.log_target, log_level=self.log_level,
                                yamlfile=[self.conf["fpga"]["yamlfile"]], subarrays=[self.conf["fpga"]["subarrays"]],
                                prog=self.conf["fpga"]["prog"], bitfile=self.conf["fpga"]["bitfile"],
                                no_mezz=self.conf["fpga"]["no_mezz"], open=self.conf["fpga"]["open"],
                                sampling_frequency=self.conf["fpga"]["sampling_frequency"], data_width=self.conf["fpga"]["data_width"],
                                group_frames=self.conf["fpga"]["group_frames"], sync_method=self.conf["fpga"]["sync_method"],
                                sync_source=self.conf["fpga"]["sync_source"])
    self.c = self.ca.ib # Collection of iceboards
    self.log.info("chime_array initalization finished")

    # Set ADC delays. This MUST be added to chime_array.py in order to keep ch_master clean
    self.log.info("Setting ADC delays")
    self.ca.set_adc_delays(self.conf["fpga"]["delay_file"])

    self.log.info("Seting operational mode to raw_time")
    self.ca.set_operational_mode('raw_time', chan8_channel_map=self.conf["fpga"]["adc_channels"])
    self.log.info("Selected ADC channels: %s" %(', '.join('%i' %(k) for k in self.conf["fpga"]["adc_channels"])))

    self.log.info("Finished initialization of the CHIME hardware")


if __name__ == '__main__':

    cm = ch_master(argv=sys.argv[1:])
    cm.init()