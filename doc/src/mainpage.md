\mainpage
\author J.-F. Cliche

About this document
===================

This document describes the Python-based  host software for the CHIME chFPGA data acquisition/channelization and correlation firmware implemented in a Virtex 6 FPGA on the ML605 evaluation board.

This document is generated automatically using [Doxygen](http://www.doxygen.org) whiches parses the source code of this project and extracts the embedded documentation. 

Note:
   - In Doxygen
      - a Python Module is called a "namespace"
	  - a Python class is named a "class"

Overview
==============

[chFPGA_controller](../html/classpychime_1_1core_1_1ch_f_p_g_a__controller_1_1ch_f_p_g_a__controller.html) (in module [core/chFPGA_controller](../html/namespacepychime_1_1core_1_1ch_f_p_g_a__controller.html)) is the class that instantiate a connection to a chFPGA board and establish a  communication channel to control the operation of the firmware.

[chFPGA_receiver](../html/classpychime_1_1core_1_1ch_f_p_g_a__receiver_1_1ch_f_p_g_a__receiver.html) (in the module [core/chFPGA_receiver](../html/namespacepychime_1_1core_1_1ch_f_p_g_a__receiver.html)) is the class that instantiate a receiver. This is a standalone process that received the timestream, spectrum and correlated frames from the FPGA board. 

Example:

\code
# Create a controller for the board located at IP address 10.10.10.11
c = chFPGA_controller.chFPGA_controller(ip_address='10.10.10.11', port_number=41000, adc_delay_table=ADC_DELAY_TABLE, init=1, sampling_frequency=850e6, reference_frequency=10e6)

# get the FPGA configuration information
chFPGA_config = c.get_config()

# Create a data receiver for the board located at IP address 10.10.10.11
r = chFPGA_receiver.chFPGA_receiver(chFPGA_config, ip_address='10.10.10.11', port=41001)

# Start the correlator with a integration period of 1 second
c.start_corr_capture(integration_period=1.0)

# Read a correlator frame (note: the first frame is sometimes missing data and should be discarded)
corr_data = r.read_corr_frames()

\endcode
	




