import sys
from statusReport import EMPTY_TEST_STATUS
from edit_boardfile import updateStatus, update_tracking_from_status
from other_stuff import commit_results
'''
Add functions here to deal with failures in specific tests.
'''

def genericFail(username=str,board_sn=str,board_vn=str,board_md=str,testStatus = EMPTY_TEST_STATUS()):
    '''
    Generic fail process to be called at the end of specific fail functions.
    '''
    print("\nIMPORTANT: If you have any reason whatsoever to suspect it is unsafe to proceed with further testing,"
          "\nplease check with someone and fix the problem appropriately before continuing with the tests.")
    raw_input("Press Enter once you've read the warning:\t")
    print("\nIf you are confident it is safe to do so, would you like to carry on testing?")
    confirm = raw_input("Enter y/n:\t")
    if not (confirm.lower().strip() == 'y'):
        updateStatus(testStatus)
        update_tracking_from_status(testStatus)
        commit_results()
        sys.exit("\nThank you for this testing process! The data has been saved. The testing program will now exit.")
    
def inspectionFail(username,board_sn,board_vn,board_md,testStatus = EMPTY_TEST_STATUS()):
    testStatus[3] = False
    print "\nFailed Inspection test."
    print "If any of the observed issues can be fixed, please do so before proceeding. Record any modified components using the addNote() script."
    print "If the problem cannot be fixed now but does not pose an immediate danger to the board, record it using addNote() and proceed with testing."
    print "Otherwise, INTERRUPT testing immediately until the problem has been dealt with."
    
    genericFail(username, board_sn, board_vn, board_md, testStatus)
    return testStatus
    
def resistanceFail(username=str,board_sn=str,board_vn=str,board_md=str,testStatus = EMPTY_TEST_STATUS()):
    testStatus[4] = False
    print "\nFailed Resistance test."
    print "\nIt is unsafe to proceed to further testing. Please INTERRUPT testing and fix the problem."
    
    genericFail(username, board_sn, board_vn, board_md, testStatus)
    return testStatus

def DCFail(username=str,board_sn=str,board_vn=str,board_md=str,testStatus = EMPTY_TEST_STATUS()):
    testStatus[5] = False
    print("\nOne of the values you entered is not within the acceptable range.")
    print("Please POWER OFF the board NOW to prevent any damage.")
    print("\nIf one of the buck regulators is supplying the wrong voltage, suspect a faulty IC or solder joint.")
    raw_input("Press Enter to continue:\t")
    
    genericFail(username, board_sn, board_vn, board_md, testStatus)
    return testStatus
    
def pllFail(username,board_sn,board_vn,board_md,testStatus = EMPTY_TEST_STATUS()):
    testStatus[6] = False
    print "\nFailed programming PLL."
    print "Make sure you have the jumper set to 'Crystal' and that the FTDI cable is properly connected."
    print "Without programmed PLLs it will not be possible to run the next tests (e.g. ARM, FPGA)."
    
    genericFail(username, board_sn, board_vn, board_md, testStatus)
    return testStatus
    
def mtestFail(username=str,board_sn=str,board_vn=str,board_md=str,testStatus = EMPTY_TEST_STATUS()):
    ''' This test is not performed currently.
    '''
    testStatus[7] = False
    print("\nFailed memory test.")
    print("Things to investigate: correct RAM IC mounted on board, soldering issues, possibility of PCB issues.")
    print("Tests that require the use of the ARM (FPGA tests) may fail if the memory is problematic.")
    
    genericFail(username, board_sn, board_vn, board_md, testStatus)
    return testStatus
    
def armFail(username=str,board_sn=str,board_vn=str,board_md=str,testStatus = EMPTY_TEST_STATUS()):
    testStatus[7] = False
    print "\nFailed programming ARM."
    print "You may want to try running this test again, double checking all Ethernet connections and addresses."
    print "Make sure you've tried at least a few times before giving up."
    print "\nIf the ARM refuses to boot, things to investigate: cables and ports, PLLs programmed, correct RAM IC mounted on board, soldering issues, possibility of PCB issues."
    print "Tests that require the use of the ARM (FPGA tests) will not be able to run if it does not boot up."
    
    genericFail(username, board_sn, board_vn, board_md, testStatus)
    return testStatus
    
def fpgaProgFail(username=str,board_sn=str,board_vn=str,board_md=str,testStatus = EMPTY_TEST_STATUS()):
    testStatus[8] = False
    print "\nFailed programming FPGA."
    print "You may want to try running this test again, double checking all ethernet connections and adresses."
    print "The ARM must have fully booted to program the FPGA. Check by looking at LEDs on the board or pinging its IP."
    print "Make sure you've tried at least a few times before giving up."
    print "\nIf you are unable to program the FPGA, check that you are using a correct bitfile (in chFPGA) and that you are on the right branch of ch_acq, and that the PLLs and ARM were programmed."
    print "Otherwise, investigate potential soldering or PCB issues."
    
    genericFail(username, board_sn, board_vn, board_md, testStatus)
    return testStatus
    
def fpgaTestFail(username=str,board_sn=str,board_vn=str,board_md=str,testStatus = EMPTY_TEST_STATUS()):
    testStatus[9] = False
    print "\nFailed FPGA test."
    print "If top_test is timing out, check all of the Ethernet connections, then reprogram the FPGA and try again."
    print "Note that the cable going to the SFP-Ethernet adapter must be connected to a gigabit adapter on the computer, with Jumbo Frames enabled."
    print "Make sure you've tried at least a few times before giving up."
    print "If you encounter another type of error, you may have to follow the trace to find its cause."
    print "\t'NameError: dict_out is not defined' usually points to a corrupted EEPROM on the Mezzanine."
    # Other common errors?
    
    genericFail(username, board_sn, board_vn, board_md, testStatus)
    return testStatus
    
def gtxFail(username=str,board_sn=str,board_vn=str,board_md=str,testStatus = EMPTY_TEST_STATUS()):
    testStatus[10] = False
    print "\nFailed GTX test."
    print "Please report this issue." # Anything else?
    
    genericFail(username, board_sn, board_vn, board_md, testStatus)
    return testStatus
    
def rampFail(username=str,board_sn=str,board_vn=str,board_md=str,testStatus = EMPTY_TEST_STATUS()):
    testStatus[11] = False
    print "\nFailed Ramp test."
    print "If top_test is timing out, check all of the Ethernet connections, then try again."
    print "Note that the cable going to the SFP-Ethernet adapter must be connected to a gigabit adapter on the computer, with Jumbo Frames enabled."
    print "This may not be the case if you see an 'Empty' error when Timestream Acquisition starts."
    print "Make sure you've tried at least a few times before giving up."
    print "If you encounter another type of error, you may have to follow the trace to find its cause."
    print "\tIf you see an 'Empty' error when Timestream Acquisition starts, check that the cabling is OK and that"
    print "\tJumbo Frames are enabled on the gigabit adapter going from SFP-ethernet adapter to computer."
    
    genericFail(username, board_sn, board_vn, board_md, testStatus)
    return testStatus
    