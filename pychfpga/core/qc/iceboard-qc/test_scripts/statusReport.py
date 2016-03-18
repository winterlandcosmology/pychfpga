import os
import time
from other_stuff import date_format

#Constants for test functions
def EMPTY_TEST_STATUS( ): #[ tester's name, serial, revision, inspection, resistance, DC, PLL, ARM, FPGA, FPGA test, GTX, ramp, comments ]
    return [ '', '', '', None, None, None, None, None, None, None, None, None, '' ]

def STATUS_LINES( ): #dummy function to make lines constant accessible to other scripts
    return 21

#Takes input from user, returns array of True/False (and string for comments) for test status: [ tester's name, serial, revision, inspection, resistance, DC, PLL, ARM, FPGA, FGPA test, GTX, comments ]
def getBoardInfo():
    tester = raw_input("Please enter your name\t")
    board = raw_input("Please enter board serial number (e.g. 0025)\t")
    revision = raw_input("Please enter board revision (e.g. Rev2)\t")
    print("\nPlease indicate whether the board passed the following tests.")
    print("Enter 'y' or '' for a PASS, 'n' for a FAIL, or 'None' if the test wasn't performed.\n")
    inspection = checkInput( raw_input("Inspection test:\n") )
    resistance = checkInput( raw_input("Resistance test:\n") )
    dc = checkInput( raw_input("DC test:\n") )
    pll = checkInput( raw_input("Program PLL:\n") )
    arm = checkInput( raw_input("Program ARM:\n") )
    fpga = checkInput( raw_input("Program FPGA:\n") )
    fpgaTest = checkInput( raw_input("FPGA test:\n") )
    gtx = checkInput( raw_input("Program GTX:\n") )
    ramp = checkInput( raw_input("Ramp test:\n"))
    comments = raw_input("Add any additional comments here:\t")
    
    return [ tester, board, revision, inspection, resistance, dc, pll, arm, fpga, fpgaTest, gtx, ramp, comments ]
    
def checkInput( input ):
    if input == 'y' or input == 'Y' or input == '':
        return True
    elif input == 'n' or input == 'N':
        return False
    elif input == 'None' or input == 'none':
        return None
    else:
        print "Can't parse input."
        return 'input error'

#Format for LaTex inventory.tex document. Takes list of form [ tester's name, serial, revision, inspection, resistance, DC, PLL, ARM, FPGA, FPGA Test, GTX, comments ]. Returns full line as string.
def formatLatex( inputList ):
    newLine = '\hline ' + date_format(time.localtime()) + ' & ' + inputList[0] + ' & ' + inputList[1] + ' & ' + inputList[2]
    for i in range(3,12):
        if inputList[i] ==  True:
            newLine += " & \\textcolor{green}{PASS}"
        elif inputList[i] == False:
            newLine += " & \\textcolor{red}{FAIL}"
        elif inputList[i] == None:
            newLine += " & N/A"
    newLine += ' & ' + inputList[11] + ' \\\\ \n'
    
    return newLine
    
#Format for %fname.txt document. Takes list of form [ tester's name, serial, revision, inspection, resistance, DC, PLL, ARM, FPGA, FPGA test, GTX, comments ]. Returns block of lines as list.
def formatTXT( inputList ):
    newLines = []
    newLines.append("Status report of most recent test (Please don't modify this line or add any lines in this block)\n")
    newLines.append("-------------------------------------------------------------------------------------------------\n")
    newLines.append('Date: ' + date_format(time.localtime()) + '\n')
    newLines.append("\n")
    newLines.append("Tester: " + inputList[0] + "\n")
    newLines.append("\n")
    
    #Convert True/False/None to PASS/FAIL/N/A
    testResult = []
    for i in range(3, 12):
        if inputList[i] ==  True:
            testResult.append("PASS")
        elif inputList[i] == False:
            testResult.append("FAIL")
        elif inputList[i] == None:
            testResult.append("N/A")
    newLines.append("=================     ============================\n")
    newLines.append("Inspection test:      " + testResult[0] + "    " + "(" + date_format(time.localtime(), short = True) + ")\n")
    newLines.append("Resistance test:      " + testResult[1] + "    " + "(" + date_format(time.localtime(), short = True) + ")\n")
    newLines.append("DC test:              " + testResult[2] + "    " + "(" + date_format(time.localtime(), short = True) + ")\n")
    newLines.append("Program PLL:          " + testResult[3] + "    " + "(" + date_format(time.localtime(), short = True) + ")\n")
    newLines.append("Program ARM:          " + testResult[4] + "    " + "(" + date_format(time.localtime(), short = True) + ")\n")
    newLines.append("Program FPGA:         " + testResult[5] + "    " + "(" + date_format(time.localtime(), short = True) + ")\n")
    newLines.append("FPGA test:            " + testResult[6] + "    " + "(" + date_format(time.localtime(), short = True) + ")\n")
    newLines.append("Program GTX:          " + testResult[7] + "    " + "(" + date_format(time.localtime(), short = True) + ")\n")
    newLines.append("Ramp Test:            " + testResult[8] + "    " + "(" + date_format(time.localtime(), short = True) + ")\n")
    newLines.append("Comments:             " + inputList[12] + "    " + "(" + date_format(time.localtime(), short = True) + ")\n")
    newLines.append("=================     ============================\n")
    newLines.append("\n")
    newLines.append("(end of status report)\n")
    newLines.append("\n")
    
    return newLines
        
def appendLatex( newLine = '' ):
    #read file as list
    f = open('inventory/inventory.tex', 'r')
    content = f.readlines()
    f.close()
    
    #look for marker line in file
    foundLine = False
    for index, line in enumerate(content):
        if line == '%append new board here\n':
            foundLine = True
            linePos = index
    if foundLine == False:
        print "Couldn't find that line."
    
    #append new line describing board to that position
    content.insert(linePos, newLine)
    f = open('inventory/inventory.tex', 'w')
    f.writelines(content)
    f.close()
    
def appendTXT( fname, newLines = [] ):
    STATUS_LINES_NUM = STATUS_LINES() #number of lines in block of text to append (and possibly overwrite)
    
    #check file exists
    if not os.path.isfile(fname):
        print "\nFile " + fname + " doesn't exist."
        return
    #read file as list
    f = open(fname, 'r')
    content = f.readlines()
    f.close()
    
    #look for marker line in file
    linePos = 9 #default position to append, otherwise will append where previous report was
    foundLine = False
    for index, line in enumerate(content):
        if line == "Status report of most recent test (Please don't modify this line or add any lines in this block)\n":
            foundLine = True
            linePos = index
            break
    #check that previous status report exists and is in expected format. If it is as expected, update previous status report with new results.
    if foundLine:
        normal = True
        if content[linePos + STATUS_LINES_NUM - 2] != '(end of status report)\n':
            normal = False
            print "\nThere is something wrong with the previous status report that was found (should be " + str(STATUS_LINES_NUM) + " lines, ending with '(end of status report)' and an empty line."
            print "To not take any chances, this script will not overwrite."
            print "\nWould you like to append this status report to the previous one or quit?"
            confirm = raw_input("Enter 'Y' or 'y' to continue, or anything else to quit.\t")
            if confirm != 'Y' and confirm != 'y':
                print "\nStatus report not updated. You can do it manually by editing the file and running the updateStatus.updateManually()."
                return
        if normal:
            confirm = raw_input("\nThis will update previous status report. Only new test results will be overwritten, others will maintain previous status.\nEnter 'Y' or 'y' to continue, or anything else to quit.\t")
            if confirm != 'Y' and confirm != 'y':
                print "\nStatus report not updated. You can do it manually by editing the file and running the updateStatus.updateManually()."
                return
            else:
                # If the current status report has no result for a test (i.e. 'N/A'), use the result from previous report
                oldLines = content[linePos:(linePos + STATUS_LINES_NUM)]
                for index, line in enumerate(newLines):
                    if line[len(line) - 18:len(line) - 15] == 'N/A':
                        newLines[index] = oldLines[index]
                del content[linePos:(linePos + STATUS_LINES_NUM)]
    else:
        print "\nDid not find previous status report. Writing to default position."
    #insert lines and write to file
    content[linePos:linePos] = newLines
    f = open(fname, 'w')
    f.writelines(content)
    f.close()
    print "Status report successfully written."
    
def addRampTest( ):
    '''
    Used to add ramp test entries to the status report of board files when test was created
    '''
    board = raw_input("Enter board serial:\t")
    while board != '0000':
        status = raw_input("\nEnter 'y' or '' for a PASS, 'n' for a FAIL, or 'None' if the test wasn't performed.")
        #read file as list
        f = open("board" + board + ".txt", 'r')
        content = f.readlines()
        f.close()
        #find status
        foundLine = False
        for index, line in enumerate(content):
            if line[0:12] == "Program GTX:":
                foundLine = True
                linePos = index
                break
        if foundLine:
            result = checkInput(status)
            if result == True:
                content[linePos+1:linePos+1] = "Ramp Test:            " + "PASS\n"
            elif result == False:
                content[linePos+1:linePos+1] = "Ramp Test:            " + "FAIL\n"
            elif result == None:
                content[linePos+1:linePos+1] = "Ramp Test:            " + "N/A\n"
            elif result == 'input error':
                print("Could not parse input for board " + board + ". Try again.")
            f = open("board" + board + ".txt", 'w')
            f.writelines(content)
            f.close()
            print("Successfully added '" + content[linePos+1] + "' to file.")
        else:
            print("Could not find previous test status for board " + board + " Try manually.")
        board = raw_input("Enter board serial:\t")
        

    