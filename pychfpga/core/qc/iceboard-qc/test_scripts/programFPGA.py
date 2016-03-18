from numpy import *
import time as tm
import os
from other_stuff import date_format, get_repo, read_config
from statusReport import EMPTY_TEST_STATUS
from testFail import fpgaProgFail
import fpgaFun

def programFPGA(username=str,board_sn=str,board_vn=str,board_md=str,testStatus = EMPTY_TEST_STATUS()):
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

    file = open(fname, 'a')
    file.write('\n\n\n\nProgramming the FPGA Test\n')
    file.write('-----------------------------\n')
    date_str = date_format(tm.localtime())
    file.write('| Date : ' + date_str + '\n')
    file.write('| Tester: ' + username + '\n')
    repo = get_repo()
    file.write("| On branch '" + str(repo.active_branch) + "' with commit " + str(repo.commit('HEAD')) + \
           " of " + os.path.split(os.path.dirname(repo.git_dir))[-1] + ".\n")
    file.flush()

    # Import parameters from config
    if username == None:
        username = config['user']

    print "Please have everything set up as for the Programming the ARM Test."
    print "You need to have already installed a heatsink on the FPGA, and running a fan over it is recommended."
    print "Make sure you can ping the motherboard, in the same method as that of Programming ARM Test. You may need to turn the board on/off"
    print "a few times to make sure it works."
    print "Please make sure the Ethernet cable has a good connection with the SFP adapter to ensure no communication problems."

    print "\nIf you just ran the 'programARM' test, you will need to reboot the board so that it broadcasts a hostname"\
          " with the serial number that was just written to its EEPROM."
    
    print "\nWe will now program the FPGA of board " + board_sn + "."
    raw_input("Press Enter to proceed with programming (this may take some time):\t")
    fpgaFun.programFpga(board_sn, force=True)

    # Command to program FPGA using arm.py in old ch_acq, for reference
    #print 'Type in python pychfpga\\arm.py --ip 10.10.10.NUM -f "..\\chFPGA\\xilinx_projects\\CHFPGA_MGK7MB_REV2\\CHFPGA_MGK7MB_REV2.runs\\impl_Rev2\\chFPGA_MGK7MB_Rev2.bit" under this new directory'
    #print 'where NUM is the number of the board (such that 10.10.10.NUM is the IP address programmed onto the board).'
    #notimportant = raw_input("Press Enter to continue:  ")

    print "\nCheck the last line of the block of text. Does it say 'Done configuring FPGA'?"
    program = raw_input("Enter 'Y' or 'N':  ")
    if program == 'Y' or program == 'y':
        file.write('\nOutput stating programming successful: Pass')
    else:
        file.write('Output stating programming successful: Fail')
        file.write('\n\n**FPGA Programming Test Overall Status: Fail**')
        file.close()
        return fpgaProgFail(username,board_sn,board_vn,board_md,testStatus)
    print "\nCheck to see there are blinking red lights at the bottom edge of the FPGA. Are they there?"
    lights = raw_input("Enter 'Y' or 'N':   ")
    if lights == 'Y' or lights == 'y':
        file.write('\n\nBlinking red lights seen on board: Pass')
    else:
        file.write('\n\nBlinking red lights seen on board: Fail')
        file.write('\n\n**FPGA Programming Test Overall Status: Fail**')
        file.close()
        return fpgaProgFail(username,board_sn,board_vn,board_md,testStatus)
    print "\nYou should also be able to see the current draw has gone up. This is normal!"

    print "\nIf there are any special concerns regarding the board for this test, please describe them below. If none, enter 'None'. "
    comments = raw_input("Enter your comments:  ")
    file.write('\n\nComments: ' + comments)

    print "Has everything in this test gone smoothly?"
    check = raw_input("Enter ('Y' or 'N'):  ")
    if check == 'Y' or check == 'y':
        file.write('\n\n**FPGA Programming Test Overall Status: Pass**')
        testStatus[8] = True
        file.close()
    else:
        file.write('\n\n**FPGA Programming Test Overall Status: Fail**')
        print "Please describe why below."
        failure = raw_input("Enter your comments:       ")
        file.write('\n\nReason for failure:         ' + failure)
        file.close()
        return fpgaProgFail(username,board_sn,board_vn,board_md,testStatus)

    return testStatus