"""
This module runs quality control testing suite for the MGADC08 - McGill CHIME ADC Mezzanine.

To use just run as 'python iceboardtest.py' or 'run iceboardtest.py' in ipython.
This script MUST be run in its own directory.
"""

import os
import sys
import numpy as np
import yaml
from collections import OrderedDict
import unittest
# Update path to give access to packages above the directory of this module,
# even if this module is run as a script (the directory of a script is never
# considered to be a package so from ../pgk does not work)

import util
util.add_paths('..', '../..')

from icecore import IceBoardPlus
from icecore import load_yaml
from icecore import Xreport as xr
import labpy

TEST_CONFIG_FILE = './mgadc08_test_config.yaml'


# import inspectiontest
# import resistancetest
# import DCtest
# import programPLLtest
# import programARMtest
# import programFPGA
# import FPGAtest
# import GTXtest
# import rampTest
# import testFail
# import traceback
# from statusReport import EMPTY_TEST_STATUS
# from edit_boardfile import new_file_header, update_tracking_file, update_tracking_from_status
# from edit_boardfile import updateStatus
# from other_stuff import read_config, commit_results, pull_all

# # TODO: Pull from origin before starting QC

# def starttest():
#     '''Starts a testing session: Prompts for information about the board to be tested, creates a file to store results in
#     if doesn't already exist, then proceeds to testing.
#     '''

#     print "***********************************************"
#     print "| I C E B O A R D  T E S T I N G  S C R I P T |"
#     print "***********************************************"

#     # Import parameters from config
#     config = read_config()
#     if config['default_config']:
#         print "Did not find config.yaml file. Will use default values from config_example.yaml.\n"
#     username = config['user']
#     location = config['location']
#     board_md = config['board_md']
#     res_dir = config['results_directory']
#     hw_dir = config['hw_track_directory']

#     #Pass/Fail status of tests. This list will be passed from method to method.
#     testStatus = EMPTY_TEST_STATUS()

#     # Pull latest version of hardware tracking and results repositories
#     pull_all()

#     print "Welcome fellow CHIME member to the Iceboard testing script!"
#     print "We'll need some information from you... Please enter everything as prompted below:"
#     print "What is your name? (if left blank, will use config.yaml entry)"
#     uname_input = raw_input("Enter:	")
#     if not uname_input.strip() == '':
#         username = uname_input
#     print "What is the location of the board? (if left blank, will use config.yaml entry)"
#     loc_input = raw_input("Enter:	")
#     if not loc_input.strip() == '':
#         location = loc_input
#     print "What is the serial number of the board? (e.g. 0009) Please be CONSISTENT in your file naming!!"
#     board_sn = raw_input("Enter:	")
#     print "What is the revision of this board? (e.g. Rev1)"
#     board_vn = raw_input("Enter:	")
#     fname = os.path.join(res_dir, 'board{:0>4d}.txt'.format(int(board_sn)))
#     hw_fname = os.path.join(hw_dir, 'iceboard{:0>4d}.yaml'.format(int(board_sn)))
#     if not (os.path.isfile(fname) and os.path.isfile(hw_fname)): # Only necessary if file doesn't already exist
#         print "What is the PCB serial number of this board? It is printed on the top-left edge of the board, above " \
#               "the 'FMC B' label and components. (e.g. 04-14-017)"
#         # TODO: Is this still the case on the new boards (PCB serial location)?
#         pcb_sn = raw_input("Enter:\t")
#     else:
#         print "\nA file already exists for this board. Will record following results there."
#         pcb_sn = None # This is only needed if a new file is being created

#     if not os.path.isfile(fname):
#         new_file_header(fname, board_sn, board_md, board_vn, pcb_sn) # Create new file with header
#     if not os.path.isfile(hw_fname):
#         update_tracking_file(hw_fname, overwrite=True, model=board_md, location=location, serial=board_sn,
#                              pcb_serial=pcb_sn, version=board_vn, notes=['Created by {} at {}'.format(username, location)])
#     else:
#         update_tracking_file(hw_fname, overwrite=True, location=location)

#     raw_input("Press enter when ready to begin testing...    ")

#     # Loop through tests until user chooses to end
#     carry_on = True
#     while carry_on:
#         try:
#             testStatus = choosetest(username,board_sn,board_vn,board_md,testStatus)
#             print "Do you wish to proceed to another test?"
#             proceed = raw_input("Enter 'Y' or 'N':  ")
#             if proceed == 'Y' or proceed == 'y':
#                 continue
#             else:
#                 updateStatus(testStatus)
#                 update_tracking_from_status(testStatus)
#                 carry_on = False
#                 commit_results()
#                 print "\nThank you for this testing process! The data has been saved. The testing program will now exit."
#         except SystemExit:
#             carry_on = False
#             commit_results()
#         except KeyboardInterrupt:
#             carry_on = False
#             commit_results()
#         except:
#             print("\nAn exception occurred. Trace below:\n")
#             traceback.print_exc()
#             testFail.genericFail(username,board_sn,board_vn,board_md,testStatus) # Will ask whether or not to continue


def top_menu(cfg):
    menu = cfg.top_menu
    key, descr, func_name = select_menu_item(menu)
    if not func_name:
        return
    else:
        globals()[func_name](cfg)

def sync_test_results(cfg):
    print 'synchronizing data'
    pass

def run_test(username=None,board_sn=None,board_vn=None,board_md=None,testStatus = ''):
    pass
    # print "Select the test you wish to proceed to.\n"
    # print "1. Inspection test"
    # print "2. Resistance test."
    # print "3. DC test."
    # print "4. Programming PLL."
    # print "5. Programming the ARM."
    # print "6. Programming the FPGA."
    # print "7. FPGA test."
    # print "8. GTX test."
    # print "9. Ramp test.\n"

    # if choice == 1:
    #     testStatus = inspectiontest.inspectiontest(username,board_sn,board_vn,board_md,testStatus)
    # elif choice == 2:
    #     testStatus = resistancetest.resistancetest(username,board_sn,board_vn,board_md,testStatus)
    # elif choice == 3:
    #     testStatus = DCtest.DCtest(username,board_sn,board_vn,board_md,testStatus)
    # elif choice == 4:
    #     testStatus = programPLLtest.programPLLtest(username,board_sn,board_vn,board_md,testStatus)
    # elif choice == 5:
    #     testStatus = programARMtest.programARMtest(username,board_sn,board_vn,board_md,testStatus)
    # elif choice == 6:
    #     testStatus = programFPGA.programFPGA(username,board_sn,board_vn,board_md,testStatus)
    # elif choice == 7:
    #     testStatus = FPGAtest.FPGAtest(username,board_sn,board_vn,board_md, testStatus)
    # elif choice == 8:
    #     testStatus = GTXtest.GTXtest(username,board_sn,board_vn,board_md,testStatus)
    # elif choice == 9:
    #     testStatus = rampTest.rampTest(username, board_sn, board_vn, board_md, testStatus)
    return testStatus


if __name__ == "__main__":

# print globals().keys()