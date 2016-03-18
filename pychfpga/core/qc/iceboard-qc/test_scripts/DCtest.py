from numpy import *
import time as tm
import os
from other_stuff import date_format, get_repo
from statusReport import EMPTY_TEST_STATUS
from testFail import DCFail
from edit_boardfile import read_config

def DCtest(username=str,board_sn=str,board_vn=str,board_md=str,testStatus = EMPTY_TEST_STATUS()):
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
    file.write('\n\n\n\nDC Test\n')
    file.write('--------\n')
    date_str=date_format(tm.localtime())
    file.write('| Date : ' + date_str + '\n')
    file.write('| Tester: ' + username + '\n')
    repo = get_repo()
    file.write("| On branch '" + str(repo.active_branch) + "' with commit " + str(repo.commit('HEAD')) + \
               " of " + os.path.split(os.path.dirname(repo.git_dir))[-1] + ".\n\n")
    file.flush()

    print 'For this test, please do power up the board. '# Please configure the set up to that as shown on http://kingspeak.physics.mcgill.ca/twiki/bin/view/Chime/IceBoardQCManual'
    print 'For this board, you will need a multimeter, a power supply, and a twisted pair cable with two banana plugs ' \
          'on one side to measure voltage.'
    raw_input("Press Enter to continue:      ")
    print "Lay the board on a grounding mat in front of you"
    print "To properly orient the board, rotate the board until the McGill Cosmology logo is facing the right way as you look at the board"
    print "The buck regulators to be tested will go in a ANTICLOCKWISE order, starting off with the upper left-most regulator"
    # print "If you're unsure about which regulator is which, please refer to 'https://' for an image guide"
    print "All these regulators should be probed with respects to the board's GROUND. A good ground to pick is the " \
          "ground to the power supply, i.e. centre screw on the connector on the left-hand side of the board"
    print "You should be probing the LONGER pin on the regulators. We mean the one that extends out from ABOVE the coil."
    print "When probing, it's a good habit to press the probe testing the place of interest with the pin tightly pressed " \
          "against your fingertips."
    #print "Again, refer to the website for help."
    raw_input("Press Enter to continue:  ")
    print "I M P O R T A N T"
    print "-----------------"
    print "For this test, it is REALLY EASY to kill the board"
    raw_input("Press Enter to continue:  ")
    print "You MUST take utmost care not to accidently fry the board!"
    raw_input("Press Enter to continue:  ")
    print "Whatever you do, do NOT short the smaller pin on the buck regulator to anything else"
    raw_input("Press Enter to continue:  ")
    print "Be SURE you are measuring the longer pin on the regulators"
    raw_input("Press Enter to continue:  ")
    print "Make sure you are pressing the probe tightly against your fingers to prevent it from shorting anything else!"
    raw_input("Press Enter to continue:  ")

    file.write('================  ======== ======== ========\n')
    file.write('Voltage Supply    Measured Expected Status\n')
    file.write('================  ======== ======== ========\n')
    
    print "Please read the voltage measurement on power supply. Enter the voltage below (up to 3 sig. figs. , i.e. '1.00')."
    volt = float(input("Enter:     "))
    file.write('%-16s  %-8.1f %-8.1f ' % ('Voltage', volt, 16.0 ))
    if abs((volt-16.00))/16. < 0.02:
        file.write('Pass\n')
    else:
        file.write('Fail\n')
        file.write('================  ======== ======== ========\n')
        file.close()
        print "\nThe board needs a 16V power supply.\n"
        DCFail(username, board_sn, board_vn, board_md, testStatus)

    print "Please read the current measurement on power supply. Enter the voltage below (up to 3 sig. figs. , i.e. '1.00')."
    cur = float(input("Enter:     "))
    file.write('%-16s  %-8.2f %-8.2f ' % ('Current', cur, 1.00 ))
    if abs((cur-0.90))/0.9 < 0.15:
        file.write('Pass\n')
    else:
        file.write('Fail\n')
        file.write('================  ======== ======== ========\n')
        file.close()
        print "\nCurrent not within acceptable range.\n"
        return DCFail(username, board_sn, board_vn, board_md, testStatus)
    file.write('================  ======== ======== ========\n\n')

    file.write('================  ======== ======== ========\n')
    file.write('Regulator         Measured Expected Status\n')
    file.write('================  ======== ======== ========\n')

    print "Please probe the 12V regulator pin. Enter the voltage below (up to 3 sig. figs. , i.e. '1.00')."
    V12 = float(input("Enter:     "))
    file.write('%-16s  %-8.1f %-8.1f ' % ('12V', V12, 12.0 ))
    if abs((V12-12.00))/12. < 0.02:
        file.write('Pass\n')
    else:
        file.write('Fail\n')
        file.write('================  ======== ======== ========\n')
        file.close()
        return DCFail(username, board_sn, board_vn, board_md, testStatus)

    print "Please probe the Vadj regulator pin. Enter the voltage below (up to 3 sig. figs, i.e. '1.00')."
    Vadj = float(input("Enter:     "))
    file.write('%-16s  %-8.2f %-8.2f ' % ('Vadj', Vadj, 2.50 ))
    if abs((Vadj-2.50))/2.5 < 0.02:
        file.write('Pass\n')
    else:
        file.write('Fail\n')
        file.write('================  ======== ======== ========\n')
        file.close()
        return DCFail(username, board_sn, board_vn, board_md, testStatus)

    print "Please probe the 3V3 regulator pin. Enter the voltage below (up to 3 sig. figs. , i.e. '1.00')."
    V3 = float(input("Enter:     "))
    file.write('%-16s  %-8.2f %-8.2f ' % ('3V3', V3, 3.30 ))
    if abs((V3-3.30))/3.3 < 0.02:
        file.write('Pass\n')
    else:
        file.write('Fail\n')
        file.write('================  ======== ======== ========\n')
        file.close()
        return DCFail(username, board_sn, board_vn, board_md, testStatus)

    print "Please probe the 5V regulator pin. Enter the voltage below (up to 3 sig. figs., i.e. '1.00')."
    V5 = float(input("Enter:     "))
    file.write('%-16s  %-8.2f %-8.2f ' % ('5V', V5, 5.50 ))
    if abs(V5-5.50)/5.5 < 0.02:
        file.write('Pass\n')
    else:
        file.write('Fail\n')
        file.write('================  ======== ======== ========\n')
        file.close()
        return DCFail(username, board_sn, board_vn, board_md, testStatus)

    print "Please skip the buck regulator next to the power connector."
    raw_input("Press Enter to continue:  ")
    
    print "Please probe the 1VGTX regulator pin. Enter the voltage below (up to 3 sig. figs., i.e. '1.00')."
    VGTX1 = float(input("Enter:     "))
    file.write('%-16s  %-8.2f %-8.2f ' % ('1VGTX', VGTX1, 1.05 ))
    if abs(VGTX1-1.05)/1.05 < 0.02:
        file.write('Pass\n')
    else:
        file.write('Fail\n')
        file.write('================  ======== ======== ========\n')
        file.close()
        return DCFail(username, board_sn, board_vn, board_md, testStatus)

    print "Please probe the 1.2VGTX regulator pin. Enter the voltage below (up to 3 sig. figs., i.e. '1.00')."
    VGTX12 = float(input("Enter:     "))
    file.write('%-16s  %-8.2f %-8.2f ' % ('1.2VGTX', VGTX12, 1.20 ))
    if abs(VGTX12-1.20)/1.2 < 0.02:
        file.write('Pass\n')
    else:
        file.write('Fail\n')
        file.write('================  ======== ======== ========\n')
        file.close()
        return DCFail(username, board_sn, board_vn, board_md, testStatus)

    print "Please probe the 1VCORE regulator pin. Enter the voltage below (up to 3 sig. figs., i.e. '1.00')."
    VCORE1 = float(input("Enter:     "))
    file.write('%-16s  %-8.2f %-8.2f ' % ( '1VCORE', VCORE1, 1.00 ))
    if abs(VCORE1-1.00)/100. < 0.02:
        file.write('Pass\n')
    else:
        file.write('Fail\n')
        file.write('================  ======== ======== ========\n')
        file.close()
        return DCFail(username, board_sn, board_vn, board_md, testStatus)

    print "Please probe the 1.5V regulator pin. Enter the voltage below (up to 3 sig. figs., i.e. '1.00')."
    V15 = float(input("Enter:     "))
    file.write('%-16s  %-8.2f %-8.2f ' % ('1.5V', V15, 1.50 ))
    if abs(V15-1.50)/100. < 0.02:
        file.write('Pass\n')
    else:
        file.write('Fail\n')
        file.write('================  ======== ======== ========\n')
        file.close()
        return DCFail(username, board_sn, board_vn, board_md, testStatus)

    print "Please probe the 1.8V regulator pin. Enter the voltage below (up to 3 sig. figs., i.e. '1.00')."
    V18 = float(input("Enter:     "))
    file.write('%-16s  %-8.2f %-8.2f ' % ('1.8V', V18, 1.80 ))
    if abs(V18-1.80)/100. < 0.02:
        file.write('Pass\n')
    else:
        file.write('Fail\n')
        file.write('================  ======== ======== ========\n')
        file.close()
        return DCFail(username, board_sn, board_vn, board_md, testStatus)

    file.write('================  ======== ======== ========\n\n')

    file.write('\n**DC Test Overall Status: Pass**\n')
    file.close()
    testStatus[5] = True
    
    print "\nIf this hasn't been done, you should attach a heat sink on the FPGA at this point."
    # print "Please consult http://kingspeak.physics.mcgill.ca/twiki/bin/edit/Chime/IceBoardQCManual as this is not part of the testing process"

    return testStatus
    