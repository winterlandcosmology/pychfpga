from numpy import *
import time as tm
import os
from other_stuff import date_format, read_config, get_repo
from statusReport import EMPTY_TEST_STATUS
from testFail import mtestFail, armFail
from fpgaFun import get_boards, write_ipmi

def programARMtest(username=None,board_sn=None,board_vn=None,board_md=None,testStatus = EMPTY_TEST_STATUS()):
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
    file.write('\n\n\n\nProgramming the ARM Test\n')
    file.write('-------------------------\n')
    date_str=date_format(tm.localtime())
    file.write('| Date : ' + date_str + '\n')
    file.write('| Tester: ' + username + '\n')
    repo = get_repo()
    file.write("| On branch '" + str(repo.active_branch) + "' with commit " + str(repo.commit('HEAD')) +
               " of " + os.path.split(os.path.dirname(repo.git_dir))[-1] + ".\n\n")
    file.flush()

    # TODO: This test should no longer add to DHCP server. Instead, write IPMI and SPI.

    print "For this test, you'll need an Ethernet cable and an SD card loaded with the latest ICEboard software."
    print "Insert the SD card in the slot on the top right corner of board. The card should slide"
    print "comfortably in with the golden plates facing down."

    print "\nLook on the righthand side of the board. You should see a set of"
    print "switches labelled BTMode Switches. We'll need to configure them."
    print "Please leave the GP switches ALONE for this part."

    raw_input("Press Enter to continue: 	")

    print "\nFor SW1, turn switches 2,3,5 on (up). For SW2, turn switches 2,4 on (up)."
    print "The switch configuration should look like below:"
    print "DUUDUDDD DUDUDDDD"
    print "You will also need to flip the switches for the FPGA Config Mode. The set of 4 switches are located above the heat sink."
    print "Flip the configuration to be UDDD."
    print "Please note the orientation of the switch is upsidedown, so technically, switches 1,2,3 are actually up and 4 is down."
    raw_input("Press Enter to proceed...\t") #  You may consult https:// for details."

    print "\nLook at the bottom right corner of the board. There should be two Ethernet ports."
    print "Connect an Ethernet cable going from the LEFT-MOST INNER port to the local network (so that this computer " \
          "is on the same network)."
    print "Power on the board."

    print "\nWait a few minutes, you should be able to see some LEDs flashing on the right side of the board."
    print "Have any lights flashed after waiting a few minutes since the board turned on?"
    lights = raw_input("Enter 'Y' or 'N': 	")
    if lights == 'Y' or lights == 'y':
        file.write('LED lights flashed after initiating board. Hints at proper connection and properly programmed SD card.\n\n')
    else:
        file.write('N.B. LED lights *DID NOT flash* after initiating board.\n\n')

    print "\nMake sure this ICEboard is the only new (i.e. which hasn't undergone QC) one on the network."
    print "All boards initially appear under the same host name, so it won't be possible to tell them apart if there are many."
    raw_input("Press Enter to continue: 	")

    # Find the 'blank' iceboard on the network
    while True:
        ib = get_boards()
        if len(ib) == 0:
            print "\nCouldn't find the board on the network. Make sure it is connected and has booted."
        elif len(ib) > 1:
            print "\nFound more than one board without a serial on the network. Make sure only " + board_sn + " is connected."
        else:
            print "\nFound one board without a serial on the network."
            ib = ib[0]
            break
        if not (raw_input("Try again? (y/n)\t").lower().strip() == 'y'):
            raise Exception("ARMtest: Didn't find just a single board with no serial on the network, found " + str(len(ib)))

    # Get MAC and IP
    MACright = ib._get_arm_mac()
    print "\nRead MAC address: " + MACright
    file.write('MAC address of left Ethernet connector: ' + MACright)
    IPright = ib._get_arm_ip()
    print "Read IP address: " + IPright
    file.write('\n\nIP address of left Ethernet connector:  ' + IPright)

    print "\nLet's try logging in via ssh on the board! In a terminal equipped with ssh, type in 'ssh root@IP' where " \
          "\nIP is the IP address of board. (you can also use, e.g., 'iceboard0048.local' if your computer supports it)"
    print "The password is blank. At the command line, you should see you logged in as root@iceboard."
    print "Were you successful in logging in via ssh "
    ssh = raw_input("Enter 'Y' or 'N': 	")
    if ssh == 'Y' or ssh == 'y':
        file.write('\n\nLogging into the board via ssh: Pass')
    else:
        file.write('\n\nLogging into the board via ssh: Fail')
        file.write('\n\n**ARM Programming Test Overall Status: Fail**\n')
        file.close()
        return armFail(username,board_sn,board_vn,board_md,testStatus)

    print "\nWe will now write the IPMI information to the board's EEPROM and SPI registers."
    print "\nThe following was provided:  Model '%s', Serial '%s', Version '%s'" % (board_md, board_sn, board_vn)
    print   "Compare to an example board: Model '%s', Serial '%s', Version '%s'" % ('MGK7MB', '0048', 'Rev4')
    ipmi_pn = 'MGK7MB'
    ipmi_sn = '{:04d}'.format(int(board_sn))
    ipmi_vn = board_vn[3:]
    correct = False
    while not correct:
        print "\n Template (for e.g. 0048)                    This board (" + board_sn + ")"
        print   "-----------------------------------------------------------------------------"
        print   "  part_number='MGK7MB',                       part_number='%s'" % (ipmi_pn)
        print   "  serial_number='0048',                       serial_number='%s'" % (ipmi_sn)
        print   "  product_version='4'                         product_version='%s'" % (ipmi_vn)
        print "\nThe fields above (right column) will be written to the board."
        print "Please verify it is correct and formatted according to the template (left column)."
        if not (raw_input("Is this the case? (y/n)\t").lower().strip() == 'y'):
            print "Please enter the correct values in the format shown above when prompted,"
            print "and we will check again."
            ipmi_pn = raw_input("part_number:\t").strip().upper()
            ipmi_sn = raw_input("serial_number:\t").strip()
            ipmi_vn = raw_input("product_version:\t").strip()
        else:
            correct = True

    # Write IPMI
    print "\nWriting IPMI..."
    write_ipmi(ib, ipmi_pn, ipmi_sn, ipmi_vn)
    print "Done."

    # Read back and check
    print "\nReading back IPMI:"
    ipmi = ib._get_motherboard_ipmi()
    ipmi_str = "\n   ipmi.board"
    for key in ipmi.board.__dict__:
        ipmi_str += "\n       " + "%-16s    %12s" % (key, str(ipmi.board.__dict__[key]))
    ipmi_str += "\n   ipmi.product"
    for key in ipmi.product.__dict__:
        ipmi_str += "\n       " + "%-16s    %12s" % (key, str(ipmi.product.__dict__[key]))
    print ipmi_str

    file.write("\n\nRead following from IPMI:\n\n::\n" + ipmi_str + '\n')

    if not (raw_input("\nDoes the above match the correct format laid out previously? (y/n)\t").lower().strip() == 'y'):
        file.write('\n\n| IPMI could not be written corectly.\n| **Board does not have a valid IPMI at this point.**')
        file.write('\n\n**ARM Programming Test Overall Status: Fail**\n')
        file.close()
        return armFail(username,board_sn,board_vn,board_md,testStatus)
    else:
        file.write('\n\nIPMI was written and read back successfully.')


    # Memory test
    #print("\nThe next step is to perform a memory test")
    #print("You will need a different SD card, labeled 'MTEST'. It should be in the drawer under the monitor.")
    #print("You will also need to read the RS232 from the board. For this, take the FTDI cable and three pin adapter (also in the drawer).")
    #print("Load the SD card into the board, flip it upside down, and find the RS232 connector (three small holes just to the left of the ARM).")
    #print("Attach the adapter and connect the yellow cable to the pin closest to the ARM, the black cable in the centre, and the orange one furthest from the ARM.")
    #print("Open up 'Termite' on the computer and boot up the board. You should see the RS232 output scroll across the screen.")
    #print("You must now reboot the board and interrupt the bootloader by entering any key in Termite IMMEDIATELY.")
    #print("Once you are in the shell, enter 'mtest' to begin the memory test.")
    #print("You should see it go through an iteration (printing one line) every few seconds. If you see a bunch of reading/writing scrolling across the screen,\nyou interrupted the boot too late. Try again and make sure you interrupt the first boot stage, immediately after it starts.")
    #print("Let mtest run through a few iterations (5-10) to confirm it doesn't produce any errors.")
    #errors = raw_input("\nDid mtest produce any errors? (y/n)\t")
    #if errors == 'Y' or errors == 'y':
    #    file.write("\nMemory Test produces errors: FAIL\n")
    #    file.write('\nARM Programming Test Overall Status: Fail\n')
    #    mtestFail(username, board_sn, board_vn, board_md, testStatus)
    #else:
    #    file.write("\nMemory Test produces no errors: Pass\n")

    print "\nYou should now reboot the board so that it will broadcast a hostname using the serial number that was " \
          "just written to the EEPROM."
    
    print "\nIf there are any special concerns regarding the board for this test, please describe them below. " \
          "If none, enter 'None'. "
    comments = raw_input("Enter your comments: 	")
    file.write('\n\nComments: ' + comments)

    print "Has everything in this test gone smoothly?"
    check = raw_input("Enter ('Y' or 'N'): 	")
    if check == 'Y' or check == 'y':
        file.write('\n\n**ARM Programming Test Overall Status: Pass**\n')
        file.close()
        testStatus[7] = True
    else:
        file.write('\n\n**ARM Programming Test Overall Status: Fail**\n')
        file.close()
        testStatus[7] = False
        return armFail(username,board_sn,board_vn,board_md,testStatus)

    return testStatus







