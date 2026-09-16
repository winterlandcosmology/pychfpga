#!/usr/bin/env python

'''
ramp test script for ICEboard QC (uses test class from ch_acq/pychfpga/common/tests/ramp_test)
'''

import time as tm
import sys
import os
from datetime import datetime
from other_stuff import read_config, get_repo, date_format
from statusReport import EMPTY_TEST_STATUS
from testFail import rampFail
import fpgaFun

def test():
    # Get config
    config = read_config()
    # Update index of histograms for SPHINX build
    sys.path.append(os.path.join(config['results_directory'], 'ramp_tests'))
    from index_graphs import index_graphs
    # Need to change directory
    cur_dir = os.path.abspath(os.path.curdir)
    os.chdir(config['results_directory'])
    index_graphs()
    os.chdir(cur_dir)

def rampTest(username=str,board_sn=str,board_vn=str,board_md=str,testStatus = EMPTY_TEST_STATUS()):
    testStatus[0] = username
    testStatus[1] = board_sn
    testStatus[2] = board_vn
    # Get config
    config = read_config()

    # Check file exists for this board
    fname = os.path.join(config['results_directory'], 'board' + board_sn + '.txt')
    if not os.path.isfile(fname):
        print "There is no existing file for this board."
        print "Please run the 'starttest()' function from 'iceboardtest.py' to create one."
        print "Aborting test.\n"
        return testStatus

    # Open file and start test
    file = open(fname, 'a')
    file.write('\n\n\n\nRamp test\n')
    file.write('-----------\n')
    date_str=date_format(tm.localtime())
    file.write('| Date : ' + date_str + '\n')
    file.write('| Tester: ' + username + '\n')
    repo = get_repo()
    file.write("| On branch '" + str(repo.active_branch) + "' with commit " + str(repo.commit('HEAD')) + \
           " of " + os.path.split(os.path.dirname(repo.git_dir))[-1] + ".\n\n")
    file.flush()
    
    # Import parameters from config
    config = read_config()
    if username == None:
        username = config['user']
    host_ip = config['host_ip']

    # Intro
    print "\nFor this test, you will need to have both CHIME ADC mezzanines mounted on the board."
    print "As for the FPGA test, you need an ethernet cable going from the ARM port to the network"
    print "and another from the SFP-Ethernet apdapter to the network."
    print "Make sure the computer you are using is connected to the network via a gigabit port with Jumbo Frames enabled."
    
    print "\nYou should connect a fan to the FPGA heatsink, and ensure there is airflow over the mezzanine ADCs."

    # Make directory
    directory = os.path.join(config['results_directory'], 'ramp_tests/QC/sn' + board_sn + '_' +
                             datetime.now().strftime('%Y_%m_%d_%H%M'))
    if not os.path.exists(directory):
        os.makedirs(directory)
    
    # Begin Ramp test
    raw_input("\nPress Enter to begin ramp test:\t")
    test_results, delay_table, stuck_bits, ipmi = fpgaFun.rampTest(board_sn, directory + '/ramp_testing_trial_sn' + board_sn, host_ip=host_ip)

    # Record run and ADC serials
    file.write('Used ADC #' + ipmi[0].board.serial_number + ' on FMC slot 1 and ADC #' +
               ipmi[0].board.serial_number + ' on FMC slot 2.')
    file.write("\n\nRamp test was run successfully. (See BoardTests/ramp_tests/ for all the details.\n\n")

    # Record results to file
    # test_pass = False
    # print("\nPlease take a look at the output of the test: error ratios and histograms (found printed to console "
    #       "\nand in BoardTests/ramp_tests respectively)")
    # if raw_input("Are there any non-zero error ratios, or non-flat histograms? (y/n)\t").lower().strip() == 'y':
    #     bit_errors = raw_input("\nIf there are any non-zero bit error ratios, enter the affected channels "
    #                            "and bits (e.g. ch 10 bit 4, ch 1 bit 7, ...):\n")
    #     file.write("| Bit errors found on: " + bit_errors + "\n")
    #     hist_errors = raw_input("If any of the histograms are not perfectly flat, enter the affected channels:\n")
    #     file.write("| Channels with non-flat histogram: " + hist_errors + "\n")
    #     hist_errors = raw_input("If any of the histograms show very large distorsion, beyond relatively small peaks "
    #                             "away from flat, enter the affected channels:\n")
    #     file.write("| Channels with severely distorted histogram: " + hist_errors + "\n\n")
    #     print("\nSmall peaks around a flat histogram or small (< 0.1) error ratios may be attributed to timing issues "
    #           "with the ADC mezzanines, but a very distorted histogram may point to more serious issues."
    #           "\n If this is found to be the case, the connector to the affected mezzanine or the traces, vias, and "
    #           "solder joints connecting it to the FPGA are possible culprits.")
    # else:
    #     file.write("Ramp test found no errors." + "\n\n")
    #     test_pass = True

    test_pass = test_results['status']
    if not test_pass:
        if test_results['bit_error'] != None:
            print   "\n | Bit errors found on: " + test_results['bit_error'] + "\n"
            file.write("| Bit errors found on: " + test_results['bit_error'] + "\n")

        if test_results['hist_equal'] != None:
            print   "\n | Channels with non-flat histogram: " + test_results['hist_equal'] + "\n"
            file.write("| Channels with non-flat histogram: " + test_results['hist_equal'] + "\n")

        if test_results['hist_expected'] != None:
            print   "\n | Channels with severely distorted histogram: " + test_results['hist_expected'] + "\n"
            file.write("| Channels with severely distorted histogram: " + test_results['hist_expected'] + "\n")
                        
        print("\nSmall peaks around a flat histogram or small (< 0.1) error ratios may be attributed to timing issues "
              "with the ADC mezzanines, but a very distorted histogram may point to more serious issues."
              "\n If this is found to be the case, the connector to the affected mezzanine or the traces, vias, and "
              "solder joints connecting it to the FPGA are possible culprits.")
              
        file.write("\n")
        print "\n FAILED ramp test. \n\n"
        
    else:
        print "\n PASSED ramp test. \n\n"
        file.write("Ramp test found no errors." + "\n\n")

    file.write('Computed ADC delay table:\n')
    file.write('\n::\n')
    for line in delay_table:
        file.write('\n   ' +  repr(line) + ',\\')
    file.write('\n\nStuck bit flags (1 means no stuck bit):\n')
    file.write('\n::\n')
    for line in stuck_bits:
        file.write('\n   ' +  repr(line) + ',\\')

    # Update index of histograms for SPHINX build
    sys.path.append(os.path.join(config['results_directory'], 'ramp_tests'))
    from index_graphs import index_graphs
    cur_dir = os.path.abspath(os.path.curdir)
    os.chdir(config['results_directory'])  # Need to change directory temporarily
    index_graphs()
    os.chdir(cur_dir)
    
    # End test
    print "\nIf there are any special concerns regarding the board for this test, please describe them below. If none, enter 'None'. "
    comments = raw_input("Enter your comments:  ")
    file.write('\n\nComments: ' + comments)
    
    testStatus[11] = test_pass
    if test_pass:
        file.write('\n\n**Ramp Test Overall Status: Pass**')
        file.close()
        return testStatus
    else:
        file.write('\n\n**Ramp Test Overall Status: Fail**')
        file.close()
        return rampFail(username,board_sn,board_vn,board_md,testStatus)
        
        
    #
    # print "Has everything in this test gone smoothly?"
    # check = raw_input("Enter ('Y' or 'N'):  ")
    # if check == 'Y' or check == 'y':
    #     file.write('\n\n**Ramp Test Overall Status: Pass**')
    #     file.close()
    #     testStatus[11] = test_pass
    # else:
    #     file.write('\n\n**Ramp Test Overall Status: Fail**')
    #     print "Please describe why below."
    #     failure = raw_input("Enter your comments:       ")
    #     file.write('\n\nComments:         ' + failure)
    #     file.close()
    #     testStatus[11] = False
    #     return rampFail(username,board_sn,board_vn,board_md,testStatus)
    #
    # return testStatus
    #
    