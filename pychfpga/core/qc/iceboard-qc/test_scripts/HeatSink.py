from numpy import *
from math import *
import pylab as plt
import time as tm
import os
import shutil
import iceboardtest
import sys
import traceback
import programFPGA
import updateStatus
from other_stuff import date_format
from statusReport import EMPTY_TEST_STATUS
from testFail import fpgaTestFail
import fpgaFun
import git
import datetime
import tuber

def HeatSink(username=str,board_sn=str,board_vn=str,board_md=str, board_type=str()):
    class IceBoard(tuber.TuberObject):
        pass
    fname = board_type + '.txt'
    file = open(fname, 'a')
    date_str=date_format(tm.localtime())
    file.write('\n#Board: ' + board_sn +'\tDate : ' + date_str )
    # Import parameters from config
    import yaml
    config = yaml.load(open('config.yaml'))
    if username == None:
        username = config['user']
    host_ip = config['host_ip']
    ch_acq_path = config['ch_acq_path']

    # Import expected values for i2c
    temps_exp = yaml.load(open('expected_values/i2c_temps.yaml'))
    power_exp = yaml.load(open('expected_values/i2c_power.yaml'))

    print "\nFor this test, you need two Ethernet cables and an SFP/Ethernet adapter for the board."
    print "You must have already installed a heatsink on the FPGA, and you should run a fan over it for this test."
    # print "Please consult http://kingspeak.physics.mcgill.ca/twiki/bin/edit/Chime/IceBoardQCManual for details regarding the connector. Or ask Kevin."

    print "\nFirst, connect the board's ethernet port to the network and also connect the FPGA to the network using the SFP to ethernet adapter."

    # Get correct ch_acq path
    if ch_acq_path is None:
        ch_acq_path = '../../ch_acq/'
        print '\nThis test requires modules from ch_acq.\nUsing path ' + ch_acq_path + '.'
        confirm = raw_input('Check that this is correct. Would you like to modify it? (y/n)\t')
        if confirm == 'y' or confirm == 'Y':
            ch_acq_path = raw_input("Enter path (ending with a '/'):\t")
        print "If it is not already the case, set ch_acq to the 'master' git branch."
    
    #probing temp for everything BEFORE PROGRAM THE FPGA
    sensor = ['MOTHERBOARD_TEMPERATURE_FPGA_DIE','MOTHERBOARD_TEMPERATURE_POWER','MOTHERBOARD_TEMPERATURE_FPGA','MOTHERBOARD_TEMPERATURE_PHY']
    for i in arange(len(sensor)):
        file.write('    ' + sensor[i])
    #ip = sys.argv[2]
    ip = '10.10.10.' + str(int(board_sn))
    initial_time = datetime.datetime.now()
    num_data_points = 60*3/5
    for i in arange(num_data_points):
        time_now = (datetime.datetime.now() - initial_time).seconds
        ib = IceBoard(hostname=ip)
        temperature = zeros((len(sensor)))
        for i in arange(len(sensor)):
            temperature[i] = ib.get_motherboard_temperature(sensor[i])
        print ip, temperature
        file.write('\n' + str(time_now))
        for i in arange(len(temperature)):
            file.write('\t' + str(temperature[i]))
        tm.sleep(5)
    # Run top_test
    print "\nWe will now attempt to run top_test."
    raw_input("Press Enter to proceed with top_test (this may take some time):\t")
    success = False
    [c,r] = [None,None]
    try:
        [c,r] = fpgaFun.top_test(board_sn, ch_acq_path=ch_acq_path, host_ip=host_ip)
        success = True
    except Exception as e:
        traceback.print_exc(e)
        print "\nTop_test did not run successfully."
        
    print "You should also see the current draw to be above 2A at this point."
        
    # Capture standard output
    from cStringIO import StringIO
    import sys
    import logging
    class Capturing(list):
        '''
        Captures standard output and error. Taken from http://stackoverflow.com/a/16571630 .
        '''
        def __enter__(self):
            self._stdout = sys.stdout
            #self._stderr = sys.stderr
            sys.stdout = sys.stderr = self._stringio = StringIO()
            self._handler = logging.StreamHandler(self._stringio)
            logging.getLogger().addHandler(self._handler)
            return self
        def __exit__(self, *args):
            self.extend(self._stringio.getvalue().splitlines())
            sys.stdout = self._stdout
            #sys.stderr = self._stderr
            logging.getLogger().removeHandler(self._handler)

    print "\nGetting the temperatures..."
    initial_time = datetime.datetime.now()
    dt = 1
    temp = 1
    #while dt > 0.01:
    #    time_now = (datetime.datetime.now() - initial_time).seconds
    #    temp1 = c.fpga.SYSMON.temperature()
    #    file.write('\n' + str(time_now) + '\t' + str(temp1))
    #    dt = abs(temp-temp1)
    #    print dt
    #    temp = temp1
    #    tm.sleep(5)
    initial_time = datetime.datetime.now()
    num_data_points = 60*3/5
    file.write('\n#After programmming FPGA')
    for i in arange(num_data_points):
        time_now = (datetime.datetime.now() - initial_time).seconds
        ib = IceBoard(hostname=ip)
        temperature = zeros((len(sensor)))
        for i in arange(len(sensor)):
            temperature[i] = ib.get_motherboard_temperature(sensor[i])
        print ip, temperature
        file.write('\n' + str(time_now))
        for i in arange(len(temperature)):
            file.write('\t' + str(temperature[i]))
        tm.sleep(5)
    file.close()
    print "Done"
 