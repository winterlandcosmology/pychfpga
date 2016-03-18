import os
import time

def reformat( fname ):
    STATUS_LINES_NUM = 16 #number of lines in block of text to append (and possibly overwrite)
    newLines = []
    
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
        if line == "|Status report of most recent test (Please don't modify this line or add any lines in this block)\n":
            foundLine = True
            linePos = index
            break
    #check that previous status report exists and is in expected format. If it is as expected, delete previous status report with user confirmation.
    if foundLine:
        overwrite = True
        for i in range(linePos, linePos + STATUS_LINES_NUM):
            if content[i][0] != '|':
                overwrite = False
                print "\nThere is something wrong with the previous status report that was found (should be " + str(STATUS_LINES_NUM) + " lines, with last 2 empty, and every line beginning with a '|')."
                print "To not take any chances, this script will not overwrite."
                print "\nWould you like to append this status report to the previous one or quit?"
                confirm = raw_input("Enter 'Y' or 'y' to continue, or anything else to quit.\t")
                if confirm != 'Y' and confirm != 'y':
                    print "\nStatus report not updated. You can do it manually by editing the file and running the updateStatus.updateManually()."
                    return
                    
        newReport = [ 'Tristan', '', '', None, None, None, None, None, None, None, None, '', '' ]
        for i in range(3,11):
            if 'PASS' in content[linePos+i+2]:
                newReport[i] = True
            elif 'FAIL' in content[linePos+i+2]:
                newReport[i] = False
        newReport[12] = content[linePos+13].replace('|Comments: ', '')
        newReport[12] = newReport[12].replace('\n', '')
        newLines = formatTXT( newReport )
        
        if overwrite:
            confirm = raw_input("\nThis will overwrite previous status report.\nEnter 'Y' or 'y' to continue, or anything else to quit.\t")
            if confirm != 'Y' and confirm != 'y':
                print "\nStatus report not updated. You can do it manually by editing the file and running the updateStatus.updateManually()."
                return
            else: del content[linePos:(linePos + STATUS_LINES_NUM)]
    else:
        print "\nDid not find previous status report. Quitting."
        return
    
    #insert lines and write to file
    content[linePos:linePos] = newLines
    f = open(fname, 'w')
    f.writelines(content)
    f.close()
    print "Status report successfully written."

#Need to modify for new format
def formatTXT( inputList ):
    newLines = []
    newLines.append("Status report of most recent test (Please don't modify this line or add any lines in this block)\n")
    newLines.append("------\n")
    newLines.append('Date: ' + date_format(time.localtime()) + '\n')
    newLines.append("\n")
    newLines.append("Tester: " + inputList[0] + "\n")
    newLines.append("\n")
    
    #Convert True/False/None to PASS/FAIL/N/A
    testResult = []
    for i in range(3, 11):
        if inputList[i] ==  True:
            testResult.append("PASS")
        elif inputList[i] == False:
            testResult.append("FAIL")
        elif inputList[i] == None:
            testResult.append("N/A")
    newLines.append("=================     ============================\n")
    newLines.append("Inspection test:      " + testResult[0] + "\n")
    newLines.append("Resistance test:      " + testResult[1] + "\n")            
    newLines.append("DC test:              " + testResult[2] + "\n")
    newLines.append("Program PLL:          " + testResult[3] + "\n")
    newLines.append("Program ARM:          " + testResult[4] + "\n")
    newLines.append("Program FPGA:         " + testResult[5] + "\n")
    newLines.append("FPGA test:            " + testResult[6] + "\n")
    newLines.append("Program GTX:          " + testResult[7] + "\n")
    newLines.append("Modifications:        " + inputList[11] + "\n")
    newLines.append("Comments:             " + inputList[12] + "\n")
    newLines.append("=================     ============================\n")
    newLines.append("\n")
    newLines.append("(end of status report)\n")
    newLines.append("\n")
    
    return newLines
    
def date_format(date):
    #finds the date, converts to string and adds 0 if <10 for day, month
    #day
    day=str(date[2])
    if int(day)<10:
        day ='0' + day
    #month
    month=str(date[1])
    if int(month)<10:
        month ='0' + month
    #year
    year=str(date[0])
    #hour
    hour=str(date[3])
    if int(hour)<10:
       hour ='0' + hour
    #minutes
    minute=str(date[4])
    if int(minute)<10:
        minute ='0' + minute
    
    #returns 'dd/mm/yyyy, hh:mm'
    return day + '/' + month + '/' + year + ', ' + hour + ':' + minute