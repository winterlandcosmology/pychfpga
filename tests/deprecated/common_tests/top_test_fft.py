#!/usr/bin/python

"""
top_test.py script 
 Instantiates a chFPGA object 'c' for interactive testing. Import in ipython using "r -i top_test" so the created chFPGA object "c" is accessible in the ipython interactive workspace.


#
# History:
# 2011-08-14 : JFC : Created from chFPGA, which now only contains top test code.
"""
#import matplotlib
#matplotlib.use("TkAgg")
import chFPGA
reload(chFPGA) # just to make sure that any changes to the code are reloaded


SN001_adc_delays=(
	[13,19,19,19,19,19,19,19],
	[15]*8,
	[20]*8,
	[14]*8,
	[12]*8,
	[11]*8,
	[13]*8,
	[9]*8
	)
#SN002_adc_delays=(
#	[13,19,19,19,19,19,19,19],
#	[8]*8,
#	[20]*8,
#	[14]*8,
#	[12]*8,
#	[11]*8,
#	[13]*8,
#	[9]*8
#	)

SN002_adc_delays=(
	[16,22,22,22,22,22,22,22]+[0], #CH0 (BUFR)
	[21]*8, #CH1 (BUFR)
	[22]*8+[0], #CH2 (PLL)
	[18]*8+[0], #CH3 (PLL)
	[17]*8, #CH4 (BUFR)
	[17]*8, #CH5 (BUFR)
	[18]*8, #CH6 (BUFR)
	[14]*8, #CH7 (BUFR)
	)

if __name__=='__main__':		
	print '------------------------'
	print 'top_test.py: chFGPA test script'
	print 'J.-F. Cliche'
	print '------------------------'

	# Delete previous instances of 'c' to make sure the sockets are closed. If not, the new object will not be able to open the socket.
	try:
		print 'Deleting previous chFPGA instances in current namespace'
		c.close() # close sockets from previous objects to free them for the new one
		del c
	except:
		pass

	ADC_TEST_MODE=0 	#  0= normal, 1= ramp, 2=pulse (1 high, 10 low)
	ADC_DELAY_TABLE=SN002_adc_delays # select the table corresponding to the FMC serial number

	# Create the new chFPGA object.
	c=chFPGA.chFPGA(adc_test_mode=ADC_TEST_MODE, adc_delay_table=ADC_DELAY_TABLE);
	print
	
	# Displays the system frequencies
	c.FreqCtr.status()
	# Continuously plot the ADC output
	#c.plot_ADC_frame(channels=[0], frames=0)
	c.plot_ADC_frame_fft(channels=[0,1,2,3,4,5,6,7], frames=0)

