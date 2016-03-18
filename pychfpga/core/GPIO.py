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
import logging

#import numpy as np

class GPIO_base(Module_base):
    """ Provides accesss to the system-level GPIO lines """

    # Create local variables for page numbers tomake the table more readable
    CONTROL = BitField.CONTROL
    STATUS = BitField.STATUS

    GLOBAL_TRIG      = BitField(CONTROL, 0x00, 7, doc='Global trigger')
    BUCK_SYNC_ENABLE = BitField(CONTROL, 0x00, 6, doc='Enable generation of the Buck SYNC signals')
    CHAN_CLK_RESET   = BitField(CONTROL, 0x00, 5, doc='Resets the channelizer clocks')
    CHAN_CLK_SRC     = BitField(CONTROL, 0x00, 4, doc='Selects the source of the channelizer clocks: 0: ADC clock, 1: 200 MHz system clock. CHAN_CLK_RESET or ADCDAQ_RESET must be asserted when doinf the change to allow the MMCM to lock properly to the new clock. ')
    ADCDAQ_RESET     = BitField(CONTROL, 0x00, 3, doc='Resets all ADCDAQ modules. This also resets the channelizer clock MMCM, the channelizers and the correlators.')
    ADC1_RESET       = BitField(CONTROL, 0x00, 1, doc='ADC RESET line for the ADC board on FMC1 slot. Common to both ADC chips on that board.')
    ADC0_RESET       = BitField(CONTROL, 0x00, 0, doc='ADC RESET line for the ADC board on FMC0 slot. Common to both ADC chips on that board.')

    BUCK_CLK_DIV     = BitField(CONTROL, 0x01, 0, width=8, doc='Clock divider to set the BUCK SYNC frequency (2-255), where freq = 200 MHz/BUCK_CLK_DIV/2.')

    LCD_E    = BitField(CONTROL, 0x02, 7, doc='LCD Enable')
    LCD_RS   = BitField(CONTROL, 0x02, 6, doc='LCD RS (0=command, 1=data)')
    LCD_RW   = BitField(CONTROL, 0x02, 5, doc='LCD Read/Write flag (0=write, 1=read)')
    LCD_DATA = BitField(CONTROL, 0x02, 0, width=4, doc='LCD 4-bit data bus')

    BLINKER_RESET                       = BitField(CONTROL, 3, 7, doc='When active, stops the LED blinker')
    ANT_RESET                           = BitField(CONTROL, 3, 6, doc='Antenna processing pipeline reset')
    CORR_RESET                          = BitField(CONTROL, 3, 5, doc='Correlator reset')
    CORR_IP_PORT_OFFSET                 = BitField(CONTROL, 3, 2, width=2, doc='Correlator output data IP port offset from the base port')
    DATA_IP_PORT_OFFSET                 = BitField(CONTROL, 3, 0, width=2, doc='Captured data IP port offset from the base port')
    HOST_FRAME_READ_RATE                = BitField(CONTROL, 4, 0, width=5, doc='Indicates how often the host UDP buffers are read. Used to throttle data transmision. Period = 2/125MHz*2^value ')
    BUCK_PHASE                          = BitField(CONTROL, 12, 0, width=64, doc='Phase of each of the 16 Buck sync lines. There are 16 possible phase values for each line. Bits 3:0 is for phase of line 0, bits 7:4 for phase of line 1 etc.')
    TARGET_MAC_ADDR                     = BitField(CONTROL, 18, 0, width=48, doc='Target MAC address, used to to setup networking over UDP broadcast')
    TARGET_IP_ADDR                      = BitField(CONTROL, 22, 0, width=32, doc='Target IP address, used to to setup networking over UDP broadcast')
    TARGET_IP_PORT                      = BitField(CONTROL, 24, 0, width=16, doc='Target IP base port number, used to to setup networking over UDP broadcast')
    TARGET_FPGA_SERIAL_NUMBER           = BitField(CONTROL, 32, 0, width=64, doc='Target FPGA serial number, used to to setup networking over UDP broadcast. Programming occurs only when this matches the actual FPGA serial number.')
    TARGET_LOAD                         = BitField(CONTROL, 33, 7, doc='When 1, loads programs the networking with the target values if the target FPGA serial number matches the actual FPGA serial number.')
    NETWORK_CONFIG_SOURCE               = BitField(CONTROL, 33, 2, width=2, doc='Selects which method is used to set the FPGA Ethernet networking.')
    TARGET_SUBARRAY                     = BitField(CONTROL, 33, 0, width=2, doc='Target subarray to be used during network setup over UDP broadcast')
    PWM_OFFSET                          = BitField(CONTROL, 37, 0, width=32, doc='Number of events (frames) to delay before starting to generate the first High of the PWM output')
    PWM_HIGH_TIME                       = BitField(CONTROL, 41, 0, width=32, doc='Number of events (frames) to keep the PWM output at High')
    PWM_PERIOD                          = BitField(CONTROL, 45, 0, width=32, doc='Number of events (frames) between PWM High (i.e PWM period)')
    PWM_RESET                           = BitField(CONTROL, 46, 7, width=1, doc='Resets the PWM generator')
    BP_GPIO_INT_EN                      = BitField(CONTROL, 46, 4, doc="When '1', the Backplane GPIO_INT is driven by this board. WARNING: Only one board should be enabled at a time.")
    USER_MUX_SOURCE                     = BitField(CONTROL, 46, 0, width=4, doc='Selects which signal is sent to the SMA-A output.')

    TIMESTAMP_VALID                     = BitField(STATUS, 0, 7, doc='Timestamp data valid (i.e. can be read)')
    ADC_SYNC_READBACK                   = BitField(STATUS, 1, 0, doc='Reads back the SYNC bit for debugging')
    LOG2_FRAME_LENGTH                   = BitField(STATUS, 1, 0, width=8, doc='Number of time samples per frame')
    NUMBER_OF_CHANNELIZERS              = BitField(STATUS, 2, 0, width=8, doc='Number of implemented antenna processing pipelines')
    NUMBER_OF_CORRELATORS               = BitField(STATUS, 3, 0, width=8, doc='Number of implemented correlators')
    NUMBER_OF_CHANNELIZERS_TO_CORRELATE = BitField(STATUS, 4, 0, width=8, doc='Number of channelizers handled by the correlators')
    NUMBER_OF_CHANNELIZERS_WITH_FFT     = BitField(STATUS, 5, 0, width=8, doc='Number of channelizers with FFT')
    NUMBER_OF_GPU_LINKS                 = BitField(STATUS, 6, 0, width=8, doc='Number of implemented GPU links')
    #    IMPLEMENT_ANT                  = BitField(STATUS, 4, 0, width=8, doc='Indicates whether the antenna processor is implemented or if a dummy mmodule is put in place. There is one bit per antenna.')
    IMPLEMENT_FFT                       = BitField(STATUS, 6, 0, width=16, doc='Indicates whether the antenna processor FFT is implemented. If not, it is bypassed and timestream data is fed to the scaler. There is one bit per antenna. ')
    #    IMPLEMENT_CORR                 = BitField(STATUS, 6, 0, width=8, doc='Indicates whether the correlator is implemented. . There is one bit per correlator. ')
    TIMESTAMP                           = BitField(STATUS, 10, 0, width=32, doc='Bitstream timestamp word')
    PLATFORM_ID                         = BitField(STATUS, 11, 0, width=8, doc='Which FPGA/board in use.  0 for ML605 eval board, 1 for KC705 evaluation board')
    FPGA_SERIAL_NUMBER                  = BitField(STATUS, 19, 0, width=64, doc='FPGA 57-bit serial number')
    NUMBER_OF_CROSSBAR_INPUTS           = BitField(STATUS, 20, 0, width=8, doc='Number of channelizer feds to the crossbar outputs')
    NUMBER_OF_CROSSBAR_OUTPUTS          = BitField(STATUS, 21, 0, width=8, doc='Number of crossbar outputs')
    PROTOCOL_VERSION                    = BitField(STATUS, 23, 0, width=16, doc='Protocol version used to manage host software compatibility.')
    CHANNELIZERS_CLOCK_SOURCE           = BitField(STATUS, 24, 0, width=8, doc='Indicates which ADC is used to provide the clock from all channelizers.')
    ADC_PLL_LOCK0                       = BitField(STATUS, 33, 6,  doc='Lock status of the ADC PLL in FMC0')
    ADC_PLL_LOCK1                       = BitField(STATUS, 33, 7,  doc='Lock status of the ADC PLL in FMC1')
    CMD_RPLY_PACKET_COUNTERS            = BitField(STATUS, 35, 0, width=16, doc='Number of command and reply packets received since last FPGA configuration. MSB=Commands, LSB=Replies')
    NUMBER_OF_BP_SHUFFLE_LANES          = BitField(STATUS, 36, 0, width=8, doc='Number of backplane links (including the direct internal link)')


    def __init__(self, fpga, base_address):
        super(GPIO_base, self).__init__(fpga, base_address)
        self.logger = logging.getLogger(__name__)
        self._lock() # prevent further property creation to avoid creating attrubutes by mistake



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

    def set_pwm(self, offset=0, high_time=195312, period=390625, reset=False):
        """ Set-ups the frame-based PWM generatot, typically used to generate
        the noise injecting gating signal.

        All values are 32-bit. A channelizer reset (or a SYNC signal, which
        generates one) must be issued after the values are changed to obtain
        the proper waveform.

        If 'reset' is True, a channelizer reset signal is issued. This is
        mainly useful for testing. A system-wide SYNC (which will cause the
        channelizer reset) is actually needed to make this signal synchronized
        with other boards.
        """
        self.PWM_OFFSET = offset
        self.PWM_HIGH_TIME = high_time
        self.PWM_PERIOD = period
        if reset:
            self.PWM_RESET = 1
            self.PWM_RESET = 0

    def get_pwm(self):
        """ Return the current values of the frame-based PWM generator as a
        tuple."""
        return (self.PWM_OFFSET, self.PWM_HIGH_TIME, self.PWM_PERIOD)

    USER_OUTPUT_SOURCE_TABLE = {
        'sync': 0,  # User-generated SYNC signal
        'pps': 1,  # 1 PPS signal from the IRIG-B decoder
        'pwm': 2,  # Output from the frame-based pwm generator
        'irigb_trig': 3,
        'bp_trig': 4,
        'bp_time': 5,
        # 'bp_buck_sync': 6,
        'refclk': 6,  # 10 MHz reference clock
        'irigb_gen': 7,
        'raw_clk': 8,
        }  # Backplane TRIG signal

    def set_user_output_source(self, source):
        """ Set the user output source. 'source' is a string."""
        if source not in self.USER_OUTPUT_SOURCE_TABLE:
            raise AttributeError("Invalid source '%s'. Valid sources are %s." % (
                source, ','.join(self.USER_OUTPUT_SOURCE_TABLE.keys())))
        self.USER_MUX_SOURCE = self.USER_OUTPUT_SOURCE_TABLE[source]

    def get_user_output_source(self):
        """ Return the current user output source as a string """
        current_value = self.USER_MUX_SOURCE
        for (source, value) in self.USER_OUTPUT_SOURCE_TABLE.items():
            if current_value == value:
                return source
        raise RuntimeError('Invalid user output source number found on the FPGA')

    def pulse_ant_reset(self):
        """ Pulses the channelizer reset lines. """
        self.pulse_bit('ANT_RESET')

    def global_reset(self):
        """ Pulses the global reset line. """
        self.pulse_bit('GLOBAL_RESET')

    def get_command_count(self):
        """ Return a (cmd, rply) typle indicating the number of command and
        reply packets that were processed by the FPGA. Both values are modulo
        256.

        The numbers include the command & reply packet needed to request the
        counts from the FPGA.
        """
        word = self.CMD_RPLY_PACKET_COUNTERS
        return (word >>8, (word+1) & 0xFF)  # Add 1 for the reply packet

    def init(self):
        """
        Initializes the GPIO module operations.
        This puts the antenna processors and correlators in reset state."""
        # reset the antenna processors. This causes them to stop sending data.
        self.ANT_RESET = 1
        self.CORR_RESET = 1
        # In the alternate code below, we do not use self.ANT_RESET=1 to reset the antenna because this implies reading the control register, and the read data might not get through if too much data is coming in
        #ant_reset = self.get_bitfield('ANT_RESET')
        #self.write(ant_reset.addr, 1 << ant_reset.bit)
        #self.write(ant_reset.addr, 0x60) # ** debug  BEWARE: This resets the DATA and CORR IP addresses to zero!!!!!***
        self.HOST_FRAME_READ_RATE = 14  #Indicates how often the host UDP buffers are read. Used to throttle data transmision. Period = 2/125MHz*2^value

    def status(self):
        """ Displays the module status"""
        self.logger.info('-------------------------GPIO--------------------------------------')
        self.logger.info('Bistream timestamp is: %s' % self.get_bitstream_date())




