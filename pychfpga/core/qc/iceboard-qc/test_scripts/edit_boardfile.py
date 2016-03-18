'''This is a collection of functions that edit the RestructuredText board results files.
Functions that were previously found in their own 'addNote.py' and 'updateStatus.py' have been moved here.
'''

from other_stuff import date_format
from statusReport import STATUS_LINES
import time
import os
import statusReport
from statusReport import EMPTY_TEST_STATUS
from other_stuff import read_config, commit_hw_files

def new_file_header(fname, board_sn, board_md, board_vn, pcb_sn):
    if not os.path.isfile(fname):
        file = open(fname, 'w')
        file.write('=========================\n')
        file.write('ICE board ' + board_sn + 'QC testing\n')
        file.write('=========================\n')
        file.write('| Quality control testing results for ICE board serial number ' + board_sn + '\n')
        file.write('| Revision number: ' + board_vn + '\n')
        file.write('| Board model: ' + board_md + '\n')
        file.write('| PCB serial: ' + pcb_sn + '\n')
        date_str=date_format(time.localtime())
        file.write('| File created on : ' + date_str + '\n')
        file.write('\n\n---------------------\n\n')
        file.close()
        print "File " + fname + " was created.\n"
    else:
        print "File already exists! Leaving as is.\n"

def addNote( fname ):
    '''
    Prompts the user for input and adds it to the specified file's Board Notes section.
    :param fname: Name of board file to append to (e.g. 'board0012.txt')
    '''
    input = raw_input("Please enter any modified components or other relevant notes here (these will be added to the 'Board Notes' section at top of file):\n")
    appendNote( fname, ['| ' + date_format(time.localtime()) + ':    ' + input + '\n'] )

def appendNote( fname, newLines = [] ):
    '''
    Finds the 'Board Notes' section in the specified file and appends given lines to it.
    Users should use 'addNote()', since it uses the standard format.
    :param fname: Name of board file to append to (e.g. 'board0012.txt')
    :param newLines: List of lines to append. Should be formatted appropriately (i.e. see 'addNote()')
    '''
    #check file exists
    if not os.path.isfile(fname):
        print "\nFile " + fname + " doesn't exist."
        return
    #read file as list
    f = open(fname, 'r')
    content = f.readlines()
    f.close()

    #look for marker line in file
    linePos = 9 #default position to append, otherwise will append where previous Notes were, or following status report
    foundNotes = False
    for index, line in enumerate(content): #find previously existing section
        if line == "Board Notes\n":
            foundNotes = True
            linePos = index
            foundEnd = False
            for index2, line2 in enumerate(content,index): #find end marker
                if line2 == '| (add here)\n':
                    linePos = index2 - index
                    foundEnd = True
                    break
            if not foundEnd:
                print("Missing marker '(add here)' at the end of previous notes. Quitting.")
                return
            print("Found previous Notes. Will append to these.")
            break
    if not foundNotes:
        newLines.insert(0,"-----------\n")
        newLines.insert(0,"Board Notes\n")
        newLines.append("| (add here)\n")
        newLines.append("\n")
        print("Did not find previous Notes. Creating new section.")
        for index, line in enumerate(content):
            if line == "Status report of most recent test (Please don't modify this line or add any lines in this block)\n":
                linePos = index + STATUS_LINES()
                break

    #insert lines and write to file
    content[linePos:linePos] = newLines
    f = open(fname, 'w')
    f.writelines(content)
    f.close()
    print "Successfully added note."

def updateStatusManually( ):
    '''
    Prompts user for test results for a board and updates that board file.
    To leave a previous result entry unchanged, enter 'None' for when prompted for that test
    '''
    board = statusReport.getBoardInfo()
#    confirm = raw_input("\nWould you like to append these results to the inventory.tex file? (y/n)\t")
#    if confirm == 'y' or confirm == 'Y':
#        statusReport.appendLatex( statusReport.formatLatex(board) )
    fname = os.path.join(read_config()['results_directory'],'board' + str(board[1]) + '.txt')
    confirm = raw_input("\nWould you like to add these results to the " + fname + " file status report? (y/n)\t")
    if confirm == 'y' or confirm == 'Y':
        statusReport.appendTXT( fname, statusReport.formatTXT( board) )

def updateStatus( board = EMPTY_TEST_STATUS() ):
    '''
    Updates test status to the supplied list of results.
    :param board: List of test results for that board. Uses format from 'statusReport.EMPTY_TEST_STATUS()'.
    '''
    fname = os.path.join(read_config()['results_directory'],'board' + str(board[1]) + '.txt')
    confirm = raw_input( "Would you like to update the status report for this board? (this will overwrite previous status report) (y/n)\t" )
    if confirm.lower().strip() != 'y':
        pass
    else:
#        confirm = raw_input("\nWould you like to append these results to the inventory.tex file? (y/n)\t")
#        if confirm == 'y' or confirm == 'Y':
#            statusReport.appendLatex( statusReport.formatLatex(board) )
        statusReport.appendTXT( fname, statusReport.formatTXT( board ) )

    confirm = raw_input( "\nWould you like to add anything to the 'Board Notes' at the top of the file? (y/n)\t" )
    if confirm != 'Y' and confirm != 'y':
        return
    else:
        addNote( fname )

def update_tracking_file(filename, overwrite=False, model=None, location=None, serial=None, pcb_serial=None, version=None, qc_status=None, notes=None):
    ''' Update the hardware tracking file of an IceBoard and commit changes to hardware_tracking repo.

        filename: string
            Filename of tracking file to update/create.
        overwrite: boolean
            Whether or not to prompt the user for confirmation before overwriting an existing file.
        model: string
            board model number (e.g. MGK7MB)
        location: string
            most recent location of board.
        serial: string
            The board's serial, in format 'XXXX'.
        pcb_serial: string
            The board's pcb serial. If unavailable, make None.
        version: string
            The board's revision, e.g. 'Rev4'.
        qc_status: dict
            Dictionary of boolean values for each QC test status.
            True for Pass, False for Fail, None if previous status is to be maintained.
            keys: ins_test, dc_test, res_test, prog_arm, prog_fpga, fpga_test, gtx_test, ramp_test
        notes: list of string
            List of notes to add to board. Will be assigned a timestamp.
    '''
    # Add hw_objects.py to path.
    import sys
    hw_track_dir = os.path.dirname(os.path.abspath(filename))
    if not hw_track_dir in sys.path:
        sys.path.append(hw_track_dir)
    from hw_objects import IceBoardHw

    # Check if file exists, load existing or create
    if os.path.isfile(filename):
        print "Found existing hardware file: {}".format(os.path.abspath(filename))
        ib_hw = IceBoardHw.from_file(filename)
        ib_hw.model = ib_hw.model if model is None else model
        ib_hw.serial = ib_hw.serial if serial is None else serial
        ib_hw.pcb_serial = ib_hw.pcb_serial if pcb_serial is None else pcb_serial
        if location is not None:
            prev_loc = ib_hw.location
            ib_hw.location = location
            if prev_loc != location:
                if notes is None:  # Add a note about location update
                    notes = ["Location changed from {} to {}".format(prev_loc, location)]
                else:
                    notes.append("Location changed from {} to {}".format(prev_loc, location))
        ib_hw.location = ib_hw.location if location is None else location
        ib_hw.version = ib_hw.version if version is None else version
        if qc_status is not None:
            ib_hw.update_qc_status(**qc_status)
        if notes is not None:
            for note in notes:
                ib_hw.add_note(note)
    else:
        print "Creating new hardware file in {}".format(os.path.abspath(filename))
        if model is None or location is None or serial is None or pcb_serial is None or version is None:
            print "WARNING: Insufficient arguments were supplied to initialize a new hardware file!"
            print "         DID NOT create a tracking file at {}".format(os.path.abspath(filename))
            return None
        ib_hw = IceBoardHw(os.path.basename(os.path.splitext(filename)[0]), model, location, serial, pcb_serial,
                           version)
        if qc_status is not None:
            ib_hw.update_qc_status(**qc_status)
        if notes is not None:
            for note in notes:
                ib_hw.add_note(note)

    # Write to file and commit
    ib_hw.write(overwrite=overwrite)
    commit_hw_files()


def update_tracking_from_status(testStatus = EMPTY_TEST_STATUS()):
    ''' Parse a standard 'testStatus' list and pass it to 'update_tracking_file()'.
    '''
    confirm = raw_input( "\nWould you like to update the hardware tracking file for this board with the latest test " +
                         "status? (this will overwrite previous status) (y/n)\t" )
    if confirm.lower().strip() != 'y':
        return
    fname = os.path.join(read_config()['hw_track_directory'], 'iceboard{:0>4d}.yaml'.format(int(testStatus[1])))
    qc_status = { 'ins_test': testStatus[3],
                  'res_test': testStatus[4],
                  'dc_test': testStatus[5],
                  'prog_arm': testStatus[7],
                  'prog_fpga': testStatus[8],
                  'fpga_test': testStatus[9],
                  'gtx_test': testStatus[10],
                  'ramp_test': testStatus[11] }
    notes = [testStatus[12]] if not (testStatus[12] is None or testStatus[12].strip() == '' or
                                     testStatus[12].strip().lower() == 'none') else None
    update_tracking_file(fname, overwrite=True, qc_status=qc_status, notes=notes)

