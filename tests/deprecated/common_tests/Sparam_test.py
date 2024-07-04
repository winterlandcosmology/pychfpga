
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
    print 'Sparam_test.py: Testing the receiver boards S-parameters'
    print 'A. Tang'
    print '------------------------'
    print 'Step 1: Connecting everything together'
    print '------------------------'
    print "Hook up the receiver board with a power supply. The limit set for the receiver board should be 12.1V with 0.4A. Connect the receiver board to the transmitter with (the long yellow cable). Power up the transmitter with a power supply with limit set to 5.1V and 0.1A. "
    test = raw_input("Press Enter to continue....      ")
    print "Make sure the GPIO (small yellow box thing) is attached to the network analyzer. Make sure the ethernet cable goes from the GPIO to the computer's network. In your computer, configure the IP address to the same one as the GPIO. (You may need to get the IP address from someone.)
    test = raw_input("When you're done the above, press Enter to continue...")
    fname1 = raw_input('Enter a file name:    (preferably the board name')
    signature = raw_input('Enter your initials: ')
    print 'Turn on the network analyzer. It make take a moment for the analyzer to load. Check the GPIO to confirm there is a green light (confirming power) and an amber light (confirming the ethernet connection).'
    test = raw_input("Press Enter when the network is up and running")
    print '------------------------'
    print 'Step 2: Setting up the basic stuff'
    print '------------------------'
    print "Press on the 'Menu' pad on the network analyzer. Select 'NUMBER of POINTS' option."
    print "Enter the highest number of points option (should be 1601). Either manually enter it or just press the UP ARROW pad on the network analyzer until it reaches 1601."
    print "(It's a good idea to go back to 'Menu' and select 'NUMBER of POINTS' again to confirm.)"
    test = raw_input("Press Enter to continue...     ")
    print "Press the 'Menu' pad on the network analyzer. Select 'SWEEP TYPE MENU' option. Then select 'LIN FREQ'. This option should be now underlined, if it's not already done so."
    test = raw_input("Press Enter to continue...     ")
    print "Press the 'Start' pad on the network analyzer. Enter your starting frequency (e.g. 100MHz)."
    test = raw_input("Press Enter to continue...     ")
    print "Press the 'Stop' pad on the network analyzer. Enter your stopping frequency (e.g. 1.2GHz)."
    test = raw_input("Press Enter to continue...     ")
    print "Now we set the power, you might be able to get away with not doing this but usually the network analyzer can't handle that much power. Let's set it to lower."
    test = raw_input("Press Enter to continue...     ")
    print "Press the 'Menu' pad on the network analyzer. Then select the 'POWER' option."
    print "If the 'PWR RANGE' option has 'auto' underlined, press the 'PWR RANGE' option to set it to 'man'."
    test = raw_input("Press Enter to continue...     ")
    print "Press 'POWER RANGE'. Then select either 'RANGE 1' or 'RANGE 2' (recommended).
    print '------------------------'
    print 'Step 3: Calibrating the network analyzer'
    print '------------------------'
    print "You'll need two long SMA cables for this and the calibration kit."
    test = raw_input("When you have the materials, press Enter to continue: ")
    decide = raw_input("Do you wish to perform a S11 or S22 calibration?  (y/n):    "

    while ((decide != 'Y') and (decide != 'y') and (decide !='N') and (decide != 'n')):
        decide = raw_input("Please enter either 'y' or 'n':    ")
    if (decide == 'Y') or (decide == 'Y'):
        print "----------------------------------"
        print "Step 3a: Performing S11 or S22 calibration set up"
        print "----------------------------------"
        print "PLEASE READ: The following procedure applies to both S11 test and the S22 test. If you're only testing S11, then perform the following steps on for port 1. If you're testing S22, do the test for port 2. If you're doing both (most likely), then do the test for BOTH ports." 
        test = raw_input("Press Enter to continue...      "
        print "Get a long SMA connector and connect to the port. Leave the cable hanging."
        test = raw_input("Press Enter to continue")
        print "Press 'Cal' pad on the network analyzer. Select 'CALIBRATE MENU'. Then pick your test(S11 or S22)."
        test = raw_input("Press Enter to continue...     ")
        print "In the calibration kit, pick the connector that is OPEN one ended (a flat surface with white circle in middle and a orange dot in the centre)."
        print "Attach it to the free end of the SMA cable. When you have screwed on the connection very tightly, leave the cable hanging and press 'OPEN' option."
        print "You should hear a beep, which means the calibration is done. The 'OPEN' option should now be underlined, denoting it's been calibrated."
        test = raw_input("Press Enter to continue...     ")
        print "Now, let's do the short. Detach the open connector from the SMA cable and put it back in the calibration kit."
        print "Pick the connector that has one end COMPLETELY covered in solder. It should appear like a round bump. Again, attach it tightly to the free end of the SMA cable and leave it hanging."
        print "Press the 'SHORT' option. Again, wait for the beep and see that the 'SHORT' option is underlined."
        test = raw_input("Press Enter to continue...     ")
        print "Detach the short connector from the SMA cable and put it back in the calibration kit"
        print "Pick up the connector that has four 50 Ohms resistors in parallel soldered at one end. Attach it tightly to the free end of SMA cable."
        print "Leave the cable hanging freely and press 'LOAD' option. Wait for the beep and see that the 'LOAD' option is underlined."
        test = raw_input("Press Enter to continue")
        print "Detach the load connector and put it back in the calibration kit. Press the 'DONE' option at the bottom."
        test = raw_input("Press Enter to continue")
        print "You have finished the S11/S22 calibration!"

    decide 2 = raw_input("Do you wish to perform a S21 or S12 calibration? (y/n):    "
    elif (decide == 'N') or (decide == 'n'):
        print "-----------------------------------"
        print "Step 3b: Performing the S12 or S21 calibration set up."
        print "-----------------------------------"
        print "Get two long SMA cables and attach one to each port. Leave them hanging."
        test = raw_input("Press Enter to continue...    ")
        print "Press the 'Cal' pad on the network analyzer. Select the 'CALIBRATION MENU' option. Then choose the 'FULL 2-PORT' option."
        test = raw_input("Press Enter to continue...    ")
        print "In the cslibration kit, find the double-ended connector. Connect the two SMA cables using the connector tightly."
        print "Press the 'REFLECTION/TRANSMISSION/ISOLATION' option. You should wait to hear a beep (which indicates it's calibrated). The 'TRANSMISSION' option should now be underlined."
        test = raw_input("Press Enter to continue...    ")
        print "When you're done, press the 'DONE' option at the bottom."
        test = raw_input("Press Enter to continue...    ")
        print "------------------------------------"
