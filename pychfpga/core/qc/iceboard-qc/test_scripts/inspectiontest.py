from numpy import *
import time as tm
import os
from other_stuff import date_format
from other_stuff import read_config, get_repo
from statusReport import EMPTY_TEST_STATUS
from testFail import inspectionFail

def inspectiontest(username=str,board_sn=str,board_vn=str,board_md=str,testStatus = EMPTY_TEST_STATUS()):
    # Update testStatus (needed if running this test independently of iceboardtest.py
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
    file.write('\n\n\n\nInspection Test\n')
    file.write('--------------------\n')
    date_str=date_format(tm.localtime())
    file.write('| Date : ' + date_str + '\n')
    file.write('| Tester: ' + username + '\n')
    repo = get_repo()
    file.write("| On branch '" + str(repo.active_branch) + "' with commit " + str(repo.commit('HEAD')) + \
           " of " + os.path.split(os.path.dirname(repo.git_dir))[-1] + ".\n\n")
    file.flush()

    print 'For this test, please do NOT power up the board. Everything should be done with nothing connected to the power supply!'
    print "It is highly suggested for you to view the board under the microscope to see everything properly."
    print "When travelling with the board, please be VERY GENTLE."
    raw_input("Press Enter to continue when you have done the above.... ")
    
    print '\nAre there any unsoldered components?'
    file.write('| Soldering status: ')
    unsol = raw_input("Enter ('Y' or 'N'):     ")
    if unsol == 'Y' or unsol == 'y':
        file.write('Bad. Unsoldered parts on board. \n')
        file.write('| Unsoldered parts: ')
        print 'Please describe the unsoldered parts. Please be specific (describe the component names)'
        solcomment = raw_input("Enter:     ")
        file.write(solcomment + '\n')
        soldering = True
    else:
        file.write('All soldering seems fine. \n')
        soldering = False
    print 'Are there any shorted components?'
    file.write('\n| Shorting status: ')
    shorts = raw_input("Enter ('Y' or 'N') :   ")
    if shorts == 'Y' or shorts == 'y':
        file.write('Bad. Shorted components on board. \n')
        file.write('| Shorted parts: ')
        print 'Please describe the shorted parts. Please be specific (describe the component names)'
        shocomments = raw_input("Enter:     ")
        file.write(shocomments + '\n')
        shorting = True
    else:
        file.write('No unwanted shorted components occur. \n')
        shorting = False
        
    print "Please inspect the GTX backplane connector pins on the back of the board (you will need the microscope). " \
          "Are any of them bent or otherwise unusual?"
    confirm = raw_input("Enter 'Y' or 'N':    ")
    if confirm == 'Y' or confirm == 'y':
        print "Describe the problematic pins and give their location (e.g. '4th pin of 1st column is bent')"
        comment = raw_input("Enter:    ")
        file.write('\nProblem with GTX backplane connector pins: ' + comment  + '\n')
        GTX = True
    else:
        file.write('\nGTX backplane connector pins seem fine.\n')
        GTX = False
        
    print "If there are any additional comments you wish to make (e.g. scratches), please describe below. (If none, enter 'None')"
    comments = raw_input("Enter additional comments:    ")
    file.write('\nAdditional comments: ' + comments + '\n')
    print "Are there any worrying issues regarding the board?"
    lastminutecheck = raw_input("Enter 'Y' or 'N':     ")
    if lastminutecheck == 'Y' or lastminutecheck == 'y':
        print "Please describe any of these last concerns:"
        otherissues = raw_input("Enter:     ")
        file.write('\nMajor issues: ' + otherissues)
        issues = True
    else:
        file.write('\nMajor issues: None \n')
        issues = False
        
    if soldering == False and shorting == False and issues == False and GTX == False:
        file.write('\n**Inspection test: PASS** \n')
        file.close()
        testStatus[3] = True
    else:
        file.write('\n**Inspection test: FAIL** \n')
        file.close()
        return inspectionFail(username,board_sn,board_vn,board_md,testStatus)

    return testStatus