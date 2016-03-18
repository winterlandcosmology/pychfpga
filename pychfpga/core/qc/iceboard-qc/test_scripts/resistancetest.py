from numpy import *
import time as tm
import os
from other_stuff import date_format, get_repo
from statusReport import EMPTY_TEST_STATUS
from testFail import resistanceFail
from edit_boardfile import read_config

def resistancetest(username=str,board_sn=str,board_vn=str,board_md=str,testStatus = EMPTY_TEST_STATUS()):
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
    file.write('\n\n\n\nResistance Test\n')
    file.write('-----------------\n')
    date_str=date_format(tm.localtime())
    file.write('| Date : ' + date_str + '\n')
    file.write('| Tester: ' + username + '\n')
    repo = get_repo()
    file.write("| On branch '" + str(repo.active_branch) + "' with commit " + str(repo.commit('HEAD')) + \
           " of " + os.path.split(os.path.dirname(repo.git_dir))[-1] + ".\n\n")
    file.flush()

    print 'For this test, please do NOT power up the board. Everything should be done with nothing connected to the power supply!'
    print 'For this test, you will need a multimeter set to measure resistance.'
    raw_input("Press Enter to continue:      ")
    print "Lay the board on a grounding mat in front of you"
    print "To properly orient the board, rotate the board until the McGill Cosmology logo is facing the right way as you look at the board"
    print "The buck regulators to be tested will go in a ANTICLOCKWISE order, starting off with the upper left-most regulator"
    # print "If you're unsure about which regulator is which, please refer to 'https://' for an image guide"
    print "All these regulators should be probed with respect to the board's GROUND. A good ground to pick is the ground to " \
          "the power supply, i.e. centre screw on the connector on the left-hand side of the board"
    print "You should be probing the LONGER pin on the regulators. We mean the one that extends out from ABOVE the coil."
    print "When probing, it's a good habit to press the probe testing the place of interest with the pin tightly pressed" \
          " against your fingertips."
    # print "Again, refer to the website for help."
    raw_input("Press Enter to continue:  ")
    
    file.write('================  ================ ========\n')
    file.write('Regulator         Measured         Status\n')
    file.write('================  ================ ========\n')
    
    print "Please probe the 12V regulator pin. Enter the resistance below (up to 2 decimal points, i.e. '1.00')."
    V12 = float(input("Enter:     "))
    file.write('%-16s  %-16.2f ' % ( '12V', (V12) ))
    if V12 > 5:
        file.write('Pass\n')
    else:
        file.write('Fail\n')
        file.write('================  ================ ========\n')
        file.close()
        return resistanceFail(username,board_sn,board_vn,board_md,testStatus)

    print "Please probe the Vadj regulator pin. Enter the resitance below (up to 2 decimal points, i.e. '1.00')."
    Vadj = float(input("Enter:     "))
    file.write('%-16s  %-16.2f ' % ( 'Vadj', (Vadj) ))
    if Vadj > 5:
        file.write('Pass\n')
    else:
        file.write('Fail\n')
        file.write('================  ================ ========\n')
        file.close()
        return resistanceFail(username,board_sn,board_vn,board_md,testStatus)

    print "Please probe the 3V3 regulator pin. Enter the resitance below (up to 2 decimal points, i.e. '1.00')."
    V3 = float(input("Enter:     "))
    file.write('%-16s  %-16.2f ' % ( '3V3', (V3) ))
    if V3 > 5:
        file.write('Pass\n')
    else:
        file.write('Fail\n')
        file.write('================  ================ ========\n')
        file.close()
        return resistanceFail(username,board_sn,board_vn,board_md,testStatus)

    print "Please probe the 5V regulator pin. Enter the resitance below (up to 2 decimal points, i.e. '1.00')."
    V5 = float(input("Enter:     "))
    file.write('%-16s  %-16.2f ' % ( '5V', (V5) ))
    if V5 > 5:
        file.write('Pass\n')
    else:
        file.write('Fail\n')
        file.write('================  ================ ========\n')
        file.close()
        return resistanceFail(username,board_sn,board_vn,board_md,testStatus)

    print "Please skip the buck regulator next to the power connector."
    raw_input("Press Enter to continue:  ")
    
    print "Please probe the 1VGTX regulator pin. Enter the resitance below (up to 2 decimal points, i.e. '1.00')."
    VGTX1 = float(input("Enter:     "))
    file.write('%-16s  %-16.2f ' % ( '1VGTX', (VGTX1) ))
    if VGTX1 > 5:
        file.write('Pass\n')
    else:
        file.write('Fail\n')
        file.write('================  ================ ========\n')
        file.close()
        return resistanceFail(username,board_sn,board_vn,board_md,testStatus)

    print "Please probe the 1.2VGTX regulator pin. Enter the resitance below (up to 2 decimal points, i.e. '1.00')."
    VGTX12 = float(input("Enter:     "))
    file.write('%-16s  %-16.2f ' % ( '1.2VGTX', (VGTX12) ))
    if VGTX12 > 5:
        file.write('Pass\n')
    else:
        file.write('Fail\n')
        file.write('================  ================ ========\n')
        file.close()
        return resistanceFail(username,board_sn,board_vn,board_md,testStatus)

    print "Please probe the 1VCORE regulator pin. Enter the resitance below (up to 2 decimal points, i.e. '1.00')."
    VCORE1 = float(input("Enter:     "))
    file.write('%-16s  %-16.2f ' % ( '1VCORE', (VCORE1) ))
    if VCORE1 > 5:
        file.write('Pass\n')
    else:
        file.write('Fail\n')
        file.write('================  ================ ========\n')
        file.close()
        return resistanceFail(username,board_sn,board_vn,board_md,testStatus)

    print "Please probe the 1.5V regulator pin. Enter the resitance below (up to 2 decimal points, i.e. '1.00')."
    V15 = float(input("Enter:     "))
    file.write('%-16s  %-16.2f ' % ( '1.5V', (V15) ))
    if V15 > 5:
        file.write('Pass\n')
    else:
        file.write('Fail\n')
        file.write('================  ================ ========\n')
        file.close()
        return resistanceFail(username,board_sn,board_vn,board_md,testStatus)

    print "Please probe the 1.8V regulator pin. Enter the resitance below (up to 2 decimal points, i.e. '1.00')."
    V18 = float(input("Enter:     "))
    file.write('%-16s  %-16.2f ' % ( '1.8V', (V18) ))
    if V18 > 5:
        file.write('Pass\n')
    else:
        file.write('Fail\n')
        file.write('================  ================ ========\n')
        file.close()
        return resistanceFail(username,board_sn,board_vn,board_md,testStatus)

    file.write('================  ================ ========\n')

    file.write('\n**Resistance Test Overall Status: Pass**\n')
    file.close()
    testStatus[4] = True

    return testStatus
    