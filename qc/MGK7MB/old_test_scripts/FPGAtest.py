
import time as tm
import os
import traceback
import pprint
import yaml
from other_stuff import date_format, read_config, get_repo
from statusReport import EMPTY_TEST_STATUS
from testFail import fpgaTestFail
from fpgaFun import top_test

# Use JF's Xreport
#import sys
#sys.path.append('../../../../../../icecore/python/tests/xreport')
#from xreport import XReport as xr
#from pychfpga.core.icecore.xreport import XReport as xr  # TODO: Add xreport to ch_acq
#import xreport as xr
import nose
import unittest
import argparse


def FPGAtest(username=str,board_sn=str,board_vn=str,board_md=str,testStatus = EMPTY_TEST_STATUS(), use_xr=False):
    ''' Run FPGAtest. If use_xr is set to True, will output results to an XReport generated file
        'board[sn]_fpgatest_[date]'. Otherwise (default) will behave like a traditional test.
    :param username:
    :param board_sn:
    :param board_vn:
    :param board_md:
    :param testStatus:
    :param use_xr: Run as XReport test and output to 'board[sn]_fpgatest_[date]'. Default is False: run as traditional test.
    :return: updated testStatus
    '''

    # Get config
    config = read_config()
    # Check file exists for this board
    fname = os.path.join(config['results_directory'], 'board' + board_sn + '.txt')
    if not os.path.isfile(fname):
        print "There is no existing file for this board."
        print "Please run the 'starttest()' function from 'iceboardtest.py' to create one."
        print "Aborting test.\n"
        return testStatus

    if use_xr:
        path = os.path.abspath(__file__)
        args = [path, path + ':TestFpga', '--xargs', '--username', username, '--board_sn', board_sn, '--board_vn', board_vn,
                '--board_md', board_md]
        print args

        x = xr()  # Plugin is enabled by default. No need to use the option --with-xreport
        nose.run(argv=args, addplugins=[x], suite=[TestFpga()])
        xr_fname = os.path.join(config['results_directory'], 'board{:0>4d}_fpgatest_{:s}'.format(int(board_sn), tm.strftime('%d_%m_%Y_%H%M')))
        x.write_rst(xr_fname)
        x.write_xml(xr_fname)

        # Get rST output
        test = None
        for i in x.etree.iter('testname'):
            if i == 'runTest':
                test = i.getparent()  # Find the group that stores the output for this test
        if test is None:
            print "\nCould not pull test results from XReport output.\nWill not append to board file."
        else:
            rst_results = ''
            for details in test.findall('details'):
                for node in details.iterdescendants():
                    rst_results += node.text
            f = open(fname, 'a')
            f.write(rst_results)
            f.close()
            print "Wrote rST results to board file."

        testStatus[9] = x.all_nodes_passed()
        return testStatus
    else:
        return trad_test(username, board_sn, board_vn, board_md, testStatus)


def trad_test(username=str,board_sn=str,board_vn=str,board_md=str,testStatus = EMPTY_TEST_STATUS(), use_xr=False):
    testStatus[0] = username
    testStatus[1] = board_sn
    testStatus[2] = board_vn
    # Get config
    config = read_config()

    # Setup logging
    import logging.handlers
    log_handler = logging.handlers.SysLogHandler()
    logger = logging.getLogger('')
    logger.handlers = []  # Clear all existing handlers
    logger.setLevel(logging.DEBUG)
    logger.addHandler(log_handler)

    # Check file exists for this board
    fname = os.path.join(config['results_directory'], 'board' + board_sn + '.txt')
    if not os.path.isfile(fname):
        logger.info("There is no existing file for this board.")
        logger.info("Please run the 'starttest()' function from 'iceboardtest.py' to create one.")
        logger.info("Aborting test.\n")
        return testStatus

    f = fake_file() if use_xr else open(fname, 'a')
    f.write('\n\n\n\nFPGA Test\n' +
          '------------\n')
    date_str=date_format(tm.localtime())
    repo = get_repo()
    f.write('| Date : ' + date_str + '\n' +
            '| Tester: ' + username + '\n' +
            "| On branch '" + str(repo.active_branch) + "' with commit " + str(repo.commit('HEAD')) +
            " of " + os.path.split(os.path.dirname(repo.git_dir))[-1] + ".\n\n")
    f.flush()

    # Import parameters from config
    if username == None:
        username = config['user']
    host_ip = config['host_ip']

    # Run top_test
    try:
        logger.info('\nRunning top_test...')
        [c, r] = top_test(board_sn, init_FMC=False, host_ip=host_ip, force=True)
        logger.info('Done.')
    except:
        trace_str = traceback.format_exc()
        f.write("Failed to run top_test. Traceback:\n\n")
        f.write("::\n\n   " + trace_str.replace('\n', '\n   ') + "\n\n")
        f.write("**FPGA test Overall Status: Fail**\n\n")
        f.close()
        testStatus[9] = False
        logger.info("Top test did not run successfully. The following exception occurred:")
        traceback.print_exc()
        return fpgaTestFail(username,board_sn,board_vn,board_md,testStatus)
    f.write("Successfully ran top_test.\n\n")


    # Get config and record formatted result
    try:
        logger.info('\nReading config...')
        fpga_config = vars(c.get_config())
        logger.info('Done.')
    except:
        trace_str = traceback.format_exc()
        f.write("Failed to get FPGA config. Traceback:\n\n")
        f.write("::\n\n   " + trace_str.replace('\n', '\n   ') + "\n\n")
        f.write("**FPGA test Overall Status: Fail**\n\n")
        f.close()
        testStatus[9] = False
        logger.info("Could not get FPGA config. The following exception occurred:")
        traceback.print_exc()
        return fpgaTestFail(username,board_sn,board_vn,board_md,testStatus)
    format_conf = ''
    pp = pprint.PrettyPrinter()
    keys = fpga_config.keys()
    keys.remove('antenna_scaler_gain') # Cannot print all of this, will only show a few values
    format_conf += '::\n\n' + \
                   '   antenna_scaler_gain: \n'
    for i in fpga_config['antenna_scaler_gain']:
        format_conf += '       [' + str(i[0]) + ', [[' + str(i[1][0][0]) + ', ... ,' + str(i[1][0][-1]) + '], ' + str(i[1][1]) + ']]\n'
    format_conf += '\n'
    for key in keys:
        format_conf += '::\n\n' + \
                       '   ' + key + ':\n'
        pretty_val = pp.pformat(fpga_config[key]).replace("\n", "\n       ")
        format_conf += '       ' + pretty_val + '\n\n'
    f.write("Read FPGA config:\n\n")
    f.write(format_conf)

    # Check temperatures and power. First read expected values
    exp_temps = yaml.load(open('expected_values/i2c_temps.yaml'))
    exp_power = yaml.load(open('expected_values/i2c_power.yaml'))


    # Probe temperatures and check against safe limits
    # Get sensor keys, excluding backplane ones
    sensors = vars(c.TEMPERATURE_SENSOR)
    sensors.pop('BP_SLOT1')
    sensors.pop('BP_SLOT16')

    # Read temps
    try:
        logger.info("\nReading temperatures over i2c...")
        temps = {}
        for key in sensors:
            temps.update({key: c.get_motherboard_temperature(sensors[key])})
        logger.info("Done.")
    except:
        trace_str = traceback.format_exc()
        f.write("Failed to read temperatures over i2c. Traceback:\n\n")
        f.write("::\n\n   " + trace_str.replace('\n', '\n   ') + "\n\n")
        f.write("**FPGA test Overall Status: Fail**\n\n")
        f.close()
        testStatus[9] = False
        logger.info("Could not read temperatures over i2c. The following exception occurred:")
        traceback.print_exc()
        return fpgaTestFail(username,board_sn,board_vn,board_md,testStatus)

    # Check results against expected values
    fail = False
    fail_list = []
    f.write('| Read temperature sensors over i2c.\n')
    f.write('| Expected range is ' + str(exp_temps['MIN']) + ' C to ' + str(exp_temps['MAX']) + ' C\n\n')
    f.write('=' * 15 + ' ' + '=' * 8 + ' ' + '=' * 8)
    f.write('\n' + "%15s %8s %8s" % ('Sensor', 'T(C)', 'Result'))
    f.write('\n' + '=' * 15 + ' ' + '=' * 8 + ' ' + '=' * 8)
    logger.info("\n%-15s %8s %8s" % ('Sensor', 'T(C)', 'Result'))
    for key in temps:
        if temps[key] > exp_temps['MAX'] or temps[key] < exp_temps['MIN']:
            fail = True
            fail_list.append(key)
            formatted_temp = "%-15s %8.2f %8s" % (key, temps[key], 'FAIL')
            f.write('\n' + formatted_temp)
            logger.info(formatted_temp)
        else:
            formatted_temp = "%-15s %8.2f %8s" % (key, temps[key], 'PASS')
            f.write('\n' + formatted_temp)
            logger.info(formatted_temp)
    f.write('\n' + '=' * 15 + ' ' + '=' * 8 + ' ' + '=' * 8 + '\n')
    if fail:
        f.write('\nSome temperature readings ' + repr(fail_list) + '  were outside reasonable range: Fail')
        f.write('\n\n**FPGA Test Overall Status: Fail**')
        f.close()
        testStatus[9] = False
        for key in fail_list:
            logger.info('\nFor ' + key + ', read ' + str(temps[key]) +
                        'C. \nPlease POWER DOWN the board and investigate the issue before continuing.')
        return fpgaTestFail(username,board_sn,board_vn,board_md,testStatus)


    # Probe currents and voltages and check against safe limits
    # Get sensor keys, excluding mezzanine ones
    rails = vars(c.RAIL)
    rails.pop('MEZZ_VADJ')
    rails.pop('MEZZ_VCC3V3')
    rails.pop('MEZZ_VCC12V0')

    # Read voltages
    try:
        logger.info("\nReading voltages over i2c...")
        volt = {}
        for key in rails:
            volt.update({key: c.get_motherboard_voltage(rails[key])})
        logger.info("Done.")
        f.write('\n\n| Read voltages over i2c.\n')
    except:
        trace_str = traceback.format_exc()
        f.write("\nFailed to read voltages over i2c. Traceback:\n\n")
        f.write("::\n\n   " + trace_str.replace('\n', '\n   ') + "\n\n")
        f.write("**FPGA test Overall Status: Fail**\n\n")
        f.close()
        testStatus[9] = False
        logger.info("Could not read voltages over i2c. The following exception occurred:")
        traceback.print_exc()
        return fpgaTestFail(username,board_sn,board_vn,board_md,testStatus)

    # Read currents
    try:
        logger.info("Reading currents over i2c...")
        curr = {}
        for key in rails:
            curr.update({key: c.get_motherboard_current(rails[key])})
        logger.info("Done.")
        f.write('| Read currents over i2c.\n')
    except:
        trace_str = traceback.format_exc()
        f.write("\nFailed to read currents over i2c. Traceback:\n\n")
        f.write("::\n\n   " + trace_str.replace('\n', '\n   ') + "\n\n")
        f.write("**FPGA test Overall Status: Fail**\n\n")
        f.close()
        testStatus[9] = False
        logger.info("Could not read currents over i2c. The following exception occurred:")
        traceback.print_exc()
        return fpgaTestFail(username,board_sn,board_vn,board_md,testStatus)

    # Check results against expected values
    # First check if FMCs are present and select appropriate value
    if c.is_mezzanine_present(1) or c.is_mezzanine_present(2):
        old_exp = exp_power['MB_VADJ']
        exp_power['MB_VADJ'] = exp_power['MB_VADJ_FMC']
        f.write("|FMC boards are present, will use an expected current of {:.2f} A for 'MB_VADJ' instead of "
                   "{:.2f} A\n".format(exp_power['MB_VADJ'][1], old_exp[1]))
    fail = False
    fail_list = []
    exp_summary = ''  # Make a line listing the expected values used for power checks
    for key in rails:
        exp_summary += key + ': ' + repr(exp_power[key]) + '; '
    f.write('| Using measurements from board ' + exp_power['ref_board'] + ' made on ' + exp_power['date_of_ref'] +
            ' as reference (shown below).\n')
    f.write('| Tolerance is ' + str(exp_power['TOLERANCE_V'] * 100) + '% for voltage, and ' +
            str(exp_power['TOLERANCE_C'] * 100) + '% for current.\n')
    f.write('| Expect [V, A]: ' + exp_summary + '\n\n')
    f.write('=' * 15 + ' ' + '=' * 8 + ' ' + '=' * 8 + ' ' + '=' * 8)
    f.write('\n' + "%15s %8s %8s %8s" % ('Rail', 'V', 'A', 'Result'))
    f.write('\n' + '=' * 15 + ' ' + '=' * 8 + ' ' + '=' * 8 + ' ' + '=' * 8)
    logger.info("\n%-15s %8s %8s %8s" % ('Rail', 'V', 'A', 'Result'))
    # Loop over the rails
    for key in rails:
    
        # Require special criteria for MB_VCC12V0, since the measured current
        # will depend on whether or not there is a fan plugged in.
        if key == "MB_VCC12V0":
            
            test_failed = ((abs(volt[key] - exp_power[key][0]) / exp_power[key][0]) > exp_power['TOLERANCE_V']) or \
                          (abs(curr[key]) > exp_power[key][1])

            if abs(curr[key]) < 0.02:
                print "\n\n PLEASE ATTACH A FAN TO THE FPGA!\n\n"
                          
        else:
            
            test_failed = ((abs(volt[key] - exp_power[key][0]) / exp_power[key][0]) > exp_power['TOLERANCE_V']) or \
                          ((abs(curr[key] - exp_power[key][1]) / exp_power[key][1]) > exp_power['TOLERANCE_C'])
    
    
        # Print pass or fail
        if test_failed:
            fail = True
            fail_list.append(key)
            formatted_power = "%-15s %8.2f %8.2f %8s" % (key, volt[key], curr[key], 'FAIL')
            f.write('\n' + formatted_power)
            logger.info(formatted_power)
        else:
            formatted_power = "%-15s %8.2f %8.2f %8s" % (key, volt[key], curr[key], 'PASS')
            f.write('\n' + formatted_power)
            logger.info(formatted_power)
            
    f.write('\n' + '=' * 15 + ' ' + '=' * 8 + ' ' + '=' * 8 + ' ' + '=' * 8 + '\n')
    
    # Print final results if fail
    if fail:
        f.write('\nSome power readings ' + repr(fail_list) + '  were outside reasonable range: Fail')
        f.write('\n\n**FPGA Test Overall Status: Fail**')
        f.close()
        testStatus[9] = False
        for key in fail_list:
            logger.info('\nCurrent and voltage at ' + key + ' are ' + str(volt[key]) + 'V and ' + str(curr[key]) +
                        'A. \nPlease POWER DOWN the board and investigate the issue before continuing.')
        return fpgaTestFail(username,board_sn,board_vn,board_md,testStatus)

    # Print final results if pass
    logger.info('\n\n----- FPGA test overall status: PASS -----  \n\n ')

    f.write('\n**FPGA Test Overall Status: Pass**')
    f.close()
    testStatus[9] = True

    return testStatus
    

class fake_file():
    ''' This class is meant to allow the FPGAtest to be run in the traditional way, or as
        an xreport test, by replacing the file object used to write record results.
    '''
    def __init__(self):
        self.xr = xr
        self.buffer = ''
    def write(self, lines):
        # Write to buffer
        self.buffer += lines
    def flush(self):
        xr.rst(self.buffer)
        self.buffer = ''
    def close(self):
        xr.rst(self.buffer)
        self.buffer = ''
        pass

class TestFpga(unittest.TestCase):
    ''' FPGA test
    '''

    def setUp(self):
        parser = argparse.ArgumentParser(description=self.__doc__.split('\n')[0])  # description is the first line of the docstring
        parser.add_argument('--username', action='store', type=str, help="User performing QC.")
        parser.add_argument('--board_sn', action='store', type=str, help="Board serial number.")
        parser.add_argument('--board_md', action='store', type=str, help="Board model.")
        parser.add_argument('--board_vn', action='store', type=str, help="Board version number.")
        self.args = parser.parse_args(xr.xargs)

    def runTest(self):
        trad_test(self.args.username, self.args.board_sn, self.args.board_vn, self.args.board_md, use_xr=True)

if __name__ == '__main__':
    FPGAtest('Tristan', '0048', 'Rev4', 'MGK7MB', use_xr=True)  # For testing