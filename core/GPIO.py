#!/usr/bin/python
# Disable pylint Line too long (=C0301)
# pylint: disable=C0301 

"""
GPIO.py module 
 Implements SYSTEM-level interface
#
# History:
    2011-08-25 JFC : Created 
    2011-08-30 JFC: Added read_bitstream_* functions and status() 
    2011-09-08 JFC: Added TIMESTAMP_VALID and ADC_SYNC_READBACK in field definitions
    2011-09-14 JFC: Added GLOBAL_RESET bit to match firmware
    2011-09-16 JFC: Added functions to pulse GLOBAL TRIG and GLOBAL RESET
    2011-09-19 JFC: Added ADC_DAQ_SYNC and FR_DIST_SYNC properties
    2011-09-27 JFC: Split ADC_DAQ_SYNC into ADC_DAQ_BUFR_SYNC and ADC_DAQ_SERDES_SYNC 
    2012-07-09 JFC: Assert ANT_RESET on init to allow communications through if the board is sending lots of data
    2012-07-25 JFC: Renamed from SYSMOD.py to GPIO.py
    2012-09-18 JFC: Added set_global_trig()
    2012-09-20 JFC: Modified bitfield list into bitfield assignemnts. Added LOG2_FRAME_LENGTH and NUMBER_OF_ANTENNAS bitfields.
    2012-10-21 JFC: Added HOST_FRAME_READ_RATE bitfield
"""

from Module import Module_base, BitField

#import numpy as np

class GPIO_base(Module_base):
    """ Provides accesss to the system-level GPIO lines """

    # Create local variables for page numbers tomake the table more readable
    CONTROL = BitField.CONTROL
    STATUS = BitField.STATUS

    GLOBAL_TRIG = BitField(CONTROL, 0x00, 7, doc='Global trigger')
    BUCK_SYNC_ENABLE = BitField(CONTROL, 0x00, 6, doc='Enable generation of the Buck SYNC signals')
    GLOBAL_RESET = BitField(CONTROL, 0x00, 5, doc='Resets the whole FPGA')
    ADC_DAQ_BUFR_SYNC = BitField(CONTROL, 0x00, 4, doc='ADC_DAQ SYNC line. Common to all ADC_DAQs.')
    ADC_DAQ_SERDES_SYNC = BitField(CONTROL, 0x00, 3, doc='ADC_DAQ SYNC line. Common to all ADC_DAQs.')
    FR_DIST_SYNC = BitField(CONTROL, 0x00, 2, doc='FR_DIST line. Common to all FR_DISTs.')
    ADC_RESET = BitField(CONTROL, 0x00, 0, doc='ADC RESET line. Common to both ADCs.')

    BUCK_CLK_DIV = BitField(CONTROL, 0x01, 0, width=8, doc='Clock divider to set the BUCK SYNC frequency (2-255). Relative to the internal ADC word clock (200 MHz)')

    LCD_E = BitField(CONTROL, 0x02, 7, doc='LCD Enable')
    LCD_RS = BitField(CONTROL, 0x02, 6, doc='LCD RS (0=command, 1=data)')
    LCD_RW = BitField(CONTROL, 0x02, 5, doc='LCD Read/Write flag (0=write, 1=read)')
    LCD_DATA = BitField(CONTROL, 0x02, 0, width=4, doc='LCD 4-bit data bus')

    USER_RESET = BitField(CONTROL, 0x03, 7, doc='User reset')
    ANT_RESET = BitField(CONTROL, 0x03, 6, doc='Antenna processing pipeline reset')
    CORR_RESET = BitField(CONTROL, 0x03, 5, doc='Correlator reset')
    CORR_IP_PORT_OFFSET = BitField(CONTROL, 0x03, 2, width=2, doc='Correlator output data IP port offset from the base port')
    DATA_IP_PORT_OFFSET = BitField(CONTROL, 0x03, 0, width=2, doc='Captured data IP port offset from the base port')
    HOST_FRAME_READ_RATE = BitField(CONTROL, 0x04, 0, width=5, doc='Indicates how often the host UDP buffers are read. Used to throttle data transmision. Period = 2/125MHz*2^value ')


    TIMESTAMP_VALID = BitField(STATUS, 0x00, 7, doc='Timestamp data valid (i.e. can be read)')
    ADC_SYNC_READBACK = BitField(STATUS, 0x00, 0, doc='Reads back the SYNC bit for debugging')
    LOG2_FRAME_LENGTH = BitField(STATUS, 0x01, 0, width=8, doc='Number of time samples per frame')
    NUMBER_OF_ANTENNAS = BitField(STATUS, 0x02, 0, width=4, doc='Number of implemented antenna processing pipelines')
    NUMBER_OF_CORRELATORS = BitField(STATUS, 0x02, 4, width=4, doc='Number of implemented correlators')
    NUMBER_OF_ANTENNAS_TO_CORRELATE = BitField(STATUS, 0x03, 0, width=4, doc='Number of antennas connected to the correlators')
    IMPLEMENT_ANT = BitField(STATUS, 4, 0, width=8, doc='Indicates whether the antenna processor is implemented or if a dummy mmodule is put in place. There is one bit per antenna.')
    IMPLEMENT_FFT = BitField(STATUS, 5, 0, width=8, doc='Indicates whether the antenna processor FFT is implemented. If not, it is bypassed and timestream data is fed to the scaler. There is one bit per antenna. ')
    IMPLEMENT_CORR = BitField(STATUS, 6, 0, width=8, doc='Indicates whether the correlator is implemented. . There is one bit per correlator. ')
    TIMESTAMP = BitField(STATUS, 0x0A, 0, width=32, doc='Bitstream timestamp word')
    PLATFORM_ID = BitField(STATUS, 11, 0, width=8, doc='Which FPGA/board in use.  0 for ML605 eval board, 1 for KC705 evaluation board')


    def __init__(self, fpga):
        super(self.__class__, self).__init__(fpga, fpga.SYSTEM_PORT, fpga.SYSTEM_GPIO_MODULE)
        self._lock() # prevent further property creation to avoid creating attrubutes by mistake


    #def read_bitstream_data(self):
    #    return self.read(0x80+0x07, type=np.dtype('>u4'))

    def get_bitstream_date(self):
        """ Returns a string containing the date-time of the currrent firmware bitstream."""
        #timestamp = self.read_bitstream_data()
        timestamp = self.TIMESTAMP
        seconds = (timestamp >> 0) & 0x3F
        minutes = (timestamp >> 6) & 0x3F
        hour = (timestamp >> 12) & 0x1F
        year = (timestamp >> 17) & 0x3F
        month = (timestamp >> 23) & 0x0F
        day = (timestamp >> 27) & 0x1F
        string = '%04i-%02i-%02i %02i:%02i:%02i' % (year + 2000, month, day, hour, minutes, seconds)
        return string

    def set_global_trig(self, trigger_state):
        """ Sets the global trigger line to the specified state. """
        self.GLOBAL_TRIG = trigger_state

    def pulse_global_trig(self):
        """ Pulses the global trigger line. """
        self.pulse_bit('GLOBAL_TRIG')

    def pulse_ant_reset(self):
        """ Pulses the antenna processor reset line. """
        self.pulse_bit('ANT_RESET')

    def global_reset(self):
        """ Pulses the global reset line. """
        self.pulse_bit('GLOBAL_RESET')

    def init(self):
        """ 
        Initializes the GPIO module operations.
        This puts the antenna processors and correlators in reset state."""
        # reset the antenna processors. This causes them to stop sending data.
        self.ANT_RESET = 1  
        self.CORR_RESET = 1  
        # In the alternate code below, we do not use self.ANT_RESET=1 to reset the antenna because this implies reading the control register, and the read data might not get through if too much data is coming in
        # ant_reset = self.bitfield('ANT_RESET')
        # self.write(ant_reset.addr, 1 << ant_reset.bit)
        self.HOST_FRAME_READ_RATE = 14  #Indicates how often the host UDP buffers are read. Used to throttle data transmision. Period = 2/125MHz*2^value
        
    def status(self):
        """ Displays the module status"""
        print '-------------------------GPIO--------------------------------------'
        print 'Bistream timestamp is: %s' % self.get_bitstream_date()
        print '----------------------------------------------------------------------'




