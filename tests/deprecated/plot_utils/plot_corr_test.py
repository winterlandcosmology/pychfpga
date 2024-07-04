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

from pychfpga.core import chFPGA_controller
from pychfpga.core import chFPGA_receiver
import pychfpga.plot_utils as pu
from pychfpga.core import Inject_tools as inj
from pychfpga.common.tests.test_adc import test_adc
reload(chFPGA_controller) # just to make sure that any changes to the code are reloaded
reload(chFPGA_receiver) # just to make sure that any changes to the code are reloaded
reload(pu)
reload(inj)

# Default data and clock line delays for the two FMC boards/ML605 combination.
# First 8 values are the delays for bits 0 to 7, 8th value is the delay for the clock line.
SN001_ADC_DELAYS = (
    [13,19,19,19,19,19,19,19]+[13], # CH0
    [18]*8+[0], #CH1
    [10]*8+[13], #CH2
    [19]*8+[13], #CH3
    [18]*8+[0], #CH4
    [16]*8+[0], #CH5
    [18]*8+[0], #CH6
    [14]*8+[0] #CH7
    )
# SN001_adc_delays=(
    # [5+16,8+16,8+16,8+16,8+16,8+16,8+16,8+16]+[0], # CH0
    # [2+16]*8+[0], #CH1
    # [5+16]*8+[0], #CH2
    # [1+16]*8+[0], #CH3
    # [16]*8+[0], #CH4
    # [15]*8+[0], #CH5
    # [18]*8+[0], #CH6
    # [13]*8+[0] #CH7
    # )

# SN001_adc_delays=(
    # [5,12,12,12,12,12,12,12]+[0], # CH0
    # [8]*8+[0], #CH1
    # [8]*8+[0], #CH2
    # [6]*8+[0], #CH3
    # [5]*8+[0], #CH4
    # [4]*8+[0], #CH5
    # [4]*8+[0], #CH6
    # [4]*8+[0] #CH7
    # )

#SN002_adc_delays=(
#    [16,22,22,22,22,22,22,22]+[0], #CH0 (BUFR)
#    [21]*8, #CH1 (BUFR)
#    [22]*8+[0], #CH2 (PLL)
#    [18]*8+[0], #CH3 (PLL)
#    [17]*8, #CH4 (BUFR)
#    [17]*8, #CH5 (BUFR)
#    [18]*8, #CH6 (BUFR)
#    [14]*8, #CH7 (BUFR)
#    )
    
SN002_ADC_DELAYS = (
    [17,15,15,15,15,15,15,3]+[0], #CH0 (BUFR)
    [15]*8, #CH1 (BUFR)
    [27,14,29,29,29,29,29,15]+[0], #CH2 (PLL)
    [15]*8+[0], #CH3 (PLL)
    [17]*8, #CH4 (BUFR)
    [17]*8, #CH5 (BUFR)
    [18]*8, #CH6 (BUFR)
    [14]*8, #CH7 (BUFR)
    )

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
    import plot_utils    
    #ADC_TEST_MODE = 0     #  0= normal, 1= ramp, 2=pulse (1 high, 10 low)
    ADC_DELAY_TABLE = ADC_DELAYS_REV2_SN0001 # select the table corresponding to the FMC serial number
    #FREF = 10 # FMC Reference clock frequency 

    # Create the new chFPGA object.
    c = chFPGA_controller.chFPGA_controller(adc_delay_table=ADC_DELAY_TABLE) # pylint: disable=C0103
    r = chFPGA_receiver.chFPGA_receiver()
    c.sync()
    #inj.set_inject_mode(c,r)
    #dcs = inj.check_fft_dc(c,r)
    # Displays the system frequenciesS
    c.FreqCtr.status()
    c.set_FFT_bypass(False, channels=[0,1,2,3,4,5,6,7])
    c.set_data_source('adc', channels=[0,1,2,3])
    #c.set_funcgen_function(function='real_ramp')
    #c.CORR_BLOCK[0].CH_DIST.select_words(8)
    c.start_data_capture(burst_period_in_seconds=1.0, number_of_bursts=0)
    c.start_corr_capture()
    plot_utils.plot_corr_frames(r)
    c.close()
    r.close()

