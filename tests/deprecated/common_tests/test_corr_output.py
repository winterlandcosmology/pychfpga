#!/usr/bin/python
# Disable pylint TAB warnings (W0312) and Line too long (=C0301)
# pylint: disable=W0312,C0301

"""
top_test.py script
 Instantiates a chFPGA object 'c' for interactive testing. Import in ipython using "r -i top_test" so the created chFPGA object "c" is accessible in the ipython interactive workspace.


#
History:
    2011-08-14 JFC: Created from chFPGA, which now only contains top test code.
    2011-09-09 JFC: Added global FREF
    2011-10-11 JFC: Updated delay tables
"""
import time
import numpy as np
from pychfpga.core import chFPGA_controller
from pychfpga.core import chFPGA_receiver
import pychfpga.plot_utils as pu
from pychfpga.core import Inject_tools as inj
from pychfpga.common.tests import test_corr as test_corr_module
test_corr = test_corr_module.test_corr

# Default data and clock line delays for the two FMC boards/ML605 combination.
# First 8 values are the delays for bits 0 to 7, 8th value is the delay for the clock line.


ADC_DELAYS_REV2_SN0001 = (
    [20,26,25,25,25,25,25,24], #CH0
    [23]*8, #CH1
    [24,22,20,20,20,20,20,17], #CH2
    [19]*8+[0], #CH3
    [17]*8, #CH4
    [17]*8, #CH5
    [19,19,19,18,17,16,20,20], #CH6
    [16]*8, #CH7
    )


if __name__ == '__main__':
    print '------------------------'
    print 'top_test.py: chFGPA test script'
    print 'J.-F. Cliche'
    print '------------------------'
    import pylab
    # Delete previous instances of 'c' to make sure the sockets are closed. If not, the new object will not be able to open the socket.
    # pylint: disable=E0601
    try:
        print 'Deleting previous chFPGA instances in current namespace'
        c.close() # close sockets from previous objects to free them for the new one
        r.close() # close sockets from previous objects to free them for the new one
        del c
        del r
    except NameError:
        pass

    #ADC_TEST_MODE = 0     #  0= normal, 1= ramp, 2=pulse (1 high, 10 low)
    ADC_DELAY_TABLE = ADC_DELAYS_REV2_SN0001 # select the table corresponding to the FMC serial number
    #FREF = 10 # FMC Reference clock frequency

    # Create the new chFPGA object.
    c = chFPGA_controller.chFPGA_controller(adc_delay_table=ADC_DELAY_TABLE) # pylint: disable=C0103
    r = chFPGA_receiver.chFPGA_receiver()
    c.sync()
    #inj.set_inject_mode(c,r)
    #dcs = inj.check_fft_dc(c,r)
    # Displays the system frequencies
    c.FreqCtr.status()
    corr = test_corr(c,r)
    data = corr.execute()




