#!/usr/bin/python
# Disable pylint TAB warnings (W0312) and Line too long (=C0301)
# pylint: disable=W0312,C0301 

"""
DC_test.py script 
DC testing script
#
History:
"""

#from pychfpga.core import chFPGA_controller
#from pychfpga.core import chFPGA_receiver
#import pychfpga.plot_utils as pu
#from pychfpga.core import Inject_tools as inj
#from pychfpga.common.tests.test_adc_fft_bin import test_adc_fft_bin
#from pychfpga.common.tests.test_adc_fft_int_power import test_adc_fft_int_power
#from pychfpga.common.tests.test_adc_fft_level import test_adc_fft_level
#from pychfpga.common.tests.test_adc_dc import test_adc_dc
#import pychfpga.common.tests.test_corr as tc
#from pychfpga import top_test

if __name__ == '__main__':
    current1 = 0.076 #The reference current set for 12V
    range1 = 0.1 #percentage error allowed for the current for 12V
    current2 = 0.1 #reference current set for 2.5V
    range2 = 0.04 #percentage error allowed for the current for 2.5V
    current3 = 2.5 #reference current set for 3.3V
    range3 = 0.04 #percentage error for the current for 3.3V
    v1 = [12, 2.5, 3.3] #voltages supplied to the board
    v2 = [1.8, 3.3] #buck regular voltages
    bvoltage1 = 1.800 #reference set for buck regulator 1.8V
    bvoltage2 = 3.300 #reference set for buck regulator 3.3V
    brange1 = 0.03 #percentage error allowed for the buck regulator 1.8V
    brange2 = 0.02 #percentage error allowed for the buck regulator 3.3V
    total_range = [range1, range2, range3]
    current = [current1, current2, current3]
    bvoltage = [bvoltage1, bvoltage2]
    brange = [brange1, brange2]
    
    print '------------------------'
    print 'DC_test.py: powering up the ADC'
    print 'A. Tang'
    print '------------------------'
    print 'Power up the ADC Mezz boards accordingly. There should be 3 power supplies set up: a 3.3V, 2.5V, 12V. Use a connector with wires to connect the power supplies with the proper pins on the ADC board. Make sure the ground is also configured properly. MAKE SURE THE OUTPUT IS OFF BEFORE PLUGGING CONNECTOR TO THE ADC BOARD.'
    test = raw_input("When you're done the above, press Enter to continue...")
    fname = raw_input('Enter a file name:    (ADC_SN000 for example)')
    signature = raw_input('Enter your initials: ')
    print 'Turn on power supply'
    print 'IF YOU SMELL SMOKE, TURN OFF THE POWER SUPPPLY IMMEDIATELY!!!! If the current drawn is really high, >3A, turn off the power supply as well.'
    test = raw_input("If everything looks fine, press Enter to continue")
    print 'Read the current being drawn from the power supply for each of the power supplies'
    input1 = float(input('Enter the current for the 12V power supply in A: '))
    input2 = float(input('Enter the current for the 2.5V power supply in A:  '))
    input3 = float(input('Enter the current for teh 3.3V power supply in A:  '))
    whole_input = [input1, input2, input3]
    whole_input_num = len(whole_input)
    print "Now that the current measurements are done, let's measure the ADC buck regulator voltages."
    print 'Take a multimeter, measure the voltage on the testpoints for buck regulators'
    point1 = float(input('Enter the voltage for the 1.8V testpoint in V: '))
    point2 = float(input('Enter the voltage for the 3.3V testpoint in V: '))
    buck_input = [point1, point2]
    buck_input_num = len(buck_input)
    print 'Turn off all the power supply now.' 

#checking results for the 3 current readings
    print "Results for BOARD", fname
    result = []
    ans = []
    reas = []
    range_val = []
    for i in range(whole_input_num):
        result.append(abs(1.0 - whole_input[i]/current[i]))
        range_val.append(total_range[i]*current[i])
        if (result[i] < total_range[i]):
            ans.append('Pass')
            reas.append('Current is within %g +/- %g' %(current[i], range_val[i]))
        elif ((1.0-whole_input[i]/current[i]) > 0):
            ans.append('Fail')
            reas.append('Current is too low below the range of %g +/- %g' %(current[i], range_val[i]))
        elif ((1.0 - whole_input[i]/current[i] < 0)):
            ans.append('Fail')
            reas.append('Current is too high above the range of %g +/- %g' %(current[i], range_val[i]))
        print "For", v1[i], "V power supply, the current is: ", whole_input[i], ". \nResult: ", ans[i], ". ", reas[i]

#checking results for the 2 buck regular voltages
    bresult = []
    bans = []
    breas = []
    brange_val = []
    for i in range(buck_input_num):
        bresult.append(abs(1.0 - buck_input[i]/bvoltage[i]))
        brange_val.append(brange[i]*bvoltage[i])
        if (bresult[i] <brange[i]):
            bans.append('Pass')
            breas.append('Voltage is within %g +/- %g' %(bvoltage[i], brange_val[i]))
        elif ((1.0 - buck_input[i]/bvoltage[i]) > 0):
            bans.append('Fail')
            breas.append('Current is too low below the range of %g +/- %g' %(bvoltage[i], brange_val[i]))
        elif ((1.0 - buck_input[i]/bvoltage[i]) < 0):
            bans.append('Fail')
            breas.append('Current is too high above the range of %g +/- %g' %(bvoltage[i], brange_val[i]))
        print "For the buck regulators", v2[i], "V, the current is: ", buck_input[i], "Result: ", bans[i], ". ", breas[i]

#writing the results to file
    fo = open(fname + "_log_file.txt", "a")
    fo.write("\nDC Test log for board " + fname + ". Tested by " + signature)
    fo.write("\nThe reference voltages used:")
    for i in range(whole_input_num):
        fo.write("\nFor %g V power supply, the reference current is: %g" %(v1[i], current[i]))
    for i in range(buck_input_num):
        fo.write("\nFor %g V buck regulators, the reference voltage is: %g" %(v2[i], bvoltage[i]))
    fo.write("\n\nThe currents drawn for each of the power supply: \n")
    for i in range(whole_input_num):
        fo.write("\nFor %g V, the current reading is: %g. Result: %s. %s"  %(v1[i], whole_input[i], ans[i], reas[i]))
    for i in range(buck_input_num):
        fo.write("\nFor %g V buck regulators, the voltage reading is: %g. Result: %s. %s" %(v2[i], buck_input[i], bans[i], breas[i]))

#Overall test
    complete_array = ans + bans
    complete = len(complete_array)
    for i in range(complete):
        if (complete_array[i] == 'Pass'):
            final = True
        else:
            final = False
    if (final == True):
            print 'DC test passed! Onto the next!'
            fo.write("\n\n\n DC test passed.")
    elif (final == False):
        print 'DC test fail, debugging needed for the board.'
        fo.write("\n\n\nDC test fail, debugging needed for the board.")
#misc. stuff
    comments = raw_input('Enter any additional comment you may wish to add about this testing run:    ')
    fo.write("\n\nComments: " + comments)
    fo.close()
