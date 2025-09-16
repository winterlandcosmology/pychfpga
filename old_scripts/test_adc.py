#!/usr/bin/python

"""
top_test.py script
 Instantiates a chFPGA object 'c' for interactive testing. Import in ipython using "r -i top_test" so the created chFPGA object "c" is accessible in the ipython interactive workspace.


#
# History:
# 2011-08-14 : JFC : Created from chFPGA, which now only contains top test code.
"""

import chFPGA

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
SN002_adc_delays=(
	[13,19,19,19,19,19,19,19],
	[8]*8,
	[20]*8,
	[14]*8,
	[12]*8,
	[11]*8,
	[13]*8,
	[9]*8
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
	c.SPI.CLK_ENABLE=0
	c.SYSMOD.BUCK_SYNC_ENABLE=1
	c.plot_ADC_frame(channels=[2], frames=2048, filename='762.5MHzp3dbm_775MHzp3dbm.dat')
	#c.plot_ADC_frame(channels=[0], frames=2048, filename='50ohm_term.dat')

