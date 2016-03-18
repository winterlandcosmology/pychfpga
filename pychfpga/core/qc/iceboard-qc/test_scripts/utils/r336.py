from addNote import appendNote
from other_stuff import date_format
import time

board = raw_input("Enter board serial:\t")
while board != '0000':
    appendNote('board' + board + '.txt', [ date_format(time.localtime()) + ':    ' + 'R336x have been previously removed to fix power sequencing issue.' + '\n'])
    board = raw_input("Enter board serial:\t")
