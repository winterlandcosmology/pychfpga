#!/usr/bin/python
# Disable pylint Line too long (=C0301)
# pylint: disable=C0301

""" Implements the interface to the GPIO FPGA module

.. History:
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
    2012-09-20 JFC: Modified bitfield list into bitfield assignments. Added LOG2_FRAME_LENGTH and NUMBER_OF_ANTENNAS bitfields.
    2012-10-21 JFC: Added HOST_FRAME_READ_RATE bitfield
"""

from ..mmi import MMI, BitField, CONTROL, STATUS
import logging

#import numpy as np

class GPIO(MMI):
    """ Provides access to the system-level GPIO lines """

    ADDRESS_WIDTH = 12

    GLOBAL_TRIG      = BitField(CONTROL, 0x00, 7, doc='Global trigger')
    BUCK_SYNC_ENABLE = BitField(CONTROL, 0x00, 6, doc='Enable generation of the Buck SYNC signals')
    CHAN_CLK_RESET   = BitField(CONTROL, 0x00, 5, doc='Resets the channelizer clocks')
    CHAN_CLK_SRC     = BitField(CONTROL, 0x00, 4, doc='Selects the source of the channelizer clocks: 0: ADC clock, 1: 200 MHz system clock. CHAN_CLK_RESET or ADCDAQ_RESET must be asserted when doinf the change to allow the MMCM to lock properly to the new clock. ')
    ADCDAQ_RESET     = BitField(CONTROL, 0x00, 3, doc='Resets all ADCDAQ modules. This also resets the channelizer clock MMCM, the channelizers and the correlators.')
    ADC1_RESET       = BitField(CONTROL, 0x00, 1, doc='ADC RESET line for the ADC board on FMC1 slot. Common to both ADC chips on that board.')
    ADC0_RESET       = BitField(CONTROL, 0x00, 0, doc='ADC RESET line for the ADC board on FMC0 slot. Common to both ADC chips on that board.')

    BUCK_CLK_DIV     = BitField(CONTROL, 0x01, 0, width=8, doc='Clock divider to set the BUCK SYNC frequency (2-255), where freq = 200 MHz/BUCK_CLK_DIV/2.')

    LCD_E    = BitField(CONTROL, 0x02, 7, doc='LCD Enable (ML605 board)')
    LCD_RS   = BitField(CONTROL, 0x02, 6, doc='LCD RS (0=command, 1=data) (ML605 board)')
    LCD_RW   = BitField(CONTROL, 0x02, 5, doc='LCD Read/Write flag (0=write, 1=read) (ML605 board)')
    LCD_DATA = BitField(CONTROL, 0x02, 0, width=4, doc='LCD 4-bit data bus (ML605 board)')

    BLINKER_RESET              = BitField(CONTROL, 3, 7, doc='When active, stops the LED blinker')
    ANT_RESET                  = BitField(CONTROL, 3, 6, doc='Antenna processing pipeline reset')
    CORR_RESET                 = BitField(CONTROL, 3, 5, doc='Correlator reset')
    CTRL_RESET_TRIG            = BitField(CONTROL, 3, 4, doc='low-to-hich transition generates a ctrl_rst pulse')
    SYSMON_RESET               = BitField(CONTROL, 3, 3, doc='SYSMON reset')
    CLK10_SEL                  = BitField(CONTROL, 3, 2, doc='Selects 10 MHz reference source; 0: Motherboard; 1: Backplane (CRS board)')
    PLL_SYNC                   = BitField(CONTROL, 3, 1, doc='Sets the SYNC line of the external PLL (CRS board)')

    HOST_FRAME_READ_RATE       = BitField(CONTROL, 4, 0, width=5, doc='Indicates how often the host UDP buffers are read. Used to throttle data transmision. Period = 2/125MHz*2^value ')
    BUCK_PHASE                 = BitField(CONTROL, 12, 0, width=64, doc='Phase of each of the 16 Buck sync lines. There are 16 possible phase values for each line. Bits 3:0 is for phase of line 0, bits 7:4 for phase of line 1 etc.')
    ADC_CAL_FREEZE             = BitField(CONTROL, 13, 0, width=8, doc='ADC calibration control')
    ADC_OR_CLEAR               = BitField(CONTROL, 14, 0, width=8, doc='ADC Overrange clear')
    ADC_OV_CLEAR               = BitField(CONTROL, 15, 0, width=8, doc='ADC Overvoltage clear')
    TARGET_FPGA_SERIAL_NUMBER  = BitField(CONTROL, 32, 0, width=64, doc='Target FPGA serial number, used to to setup networking over UDP broadcast. Loading of the networking parameters occurs only on a rising edge of TARGET_LOAD when this matches the actual FPGA serial number.')
    NETWORK_CONFIG_SOURCE      = BitField(CONTROL, 33, 2, width=2, doc='Controls write access to the core registers.  '
                                                                       'When 00, core registers are always accesible. '
                                                                       'When 01, 10 or 11, core registers can be written only if the target serial number matches the actual FPGA serial number. '
                                                                       'This is typically used to bootstrap board-specific networking configuration using UDP broadcasts instead of using the SPI access to the core registers.')
    PWM_OFFSET                 = BitField(CONTROL, 37, 0, width=32, doc='Number of events (frames) to delay before starting to generate the first High of the PWM output')
    PWM_HIGH_TIME              = BitField(CONTROL, 41, 0, width=32, doc='Number of events (frames) to keep the PWM output at High')
    PWM_PERIOD                 = BitField(CONTROL, 45, 0, width=32, doc='Number of events (frames) between PWM High (i.e PWM period)')
    PWM_RESET                  = BitField(CONTROL, 46, 7, width=1, doc='Resets the PWM generator')
    BP_GPIO_INT_EN             = BitField(CONTROL, 46, 4, doc="When '1', the Backplane GPIO_INT is driven by this board. WARNING: Only one board should be enabled at a time.")
    USER_MUX_SOURCE0           = BitField(CONTROL, 46, 0, width=4, doc='Selects which signal is sent to the Motherboard SMA-A output.')

    UART_TX_OE                 = BitField(CONTROL, 47, 7, doc="FPGA I/O line")
    UART_TX_OUT                = BitField(CONTROL, 47, 6, doc="FPGA I/O line")
    UART_RX_OE                 = BitField(CONTROL, 47, 5, doc="FPGA I/O line")
    UART_RX_OUT                = BitField(CONTROL, 47, 4, doc="FPGA I/O line")
    GPIO_RST_OE                = BitField(CONTROL, 47, 3, doc="FPGA I/O line")
    GPIO_RST_OUT               = BitField(CONTROL, 47, 2, doc="FPGA I/O line")
    ARM_IRQ_OE                 = BitField(CONTROL, 47, 1, doc="FPGA I/O line")
    ARM_IRQ_OUT                = BitField(CONTROL, 47, 0, doc="FPGA I/O line")

    USER_MUX_SOURCE3           = BitField(CONTROL, 48, 4, width=4, doc='Selects which signal is sent to the BP_GPIO_INT line.')
    GPIO_IRQ_OE                = BitField(CONTROL, 48, 3, doc="FPGA I/O line")
    GPIO_IRQ_OUT               = BitField(CONTROL, 48, 2, doc="FPGA I/O line")
    FLASH_CS_OE                = BitField(CONTROL, 48, 1, doc="FPGA I/O line")
    FLASH_CS_OUT               = BitField(CONTROL, 48, 0, doc="FPGA I/O line")

    USER_MUX_SOURCE1           = BitField(CONTROL, 49, 0, width=4, doc='Selects which signal is sent to the Motherboard FPGA LED1 and Backplane SMA output.')
    USER_MUX_SOURCE2           = BitField(CONTROL, 49, 4, width=4, doc='Selects which signal is sent to the Motherboard SMA-B and FPGA LED2 output.')

    # USER_BIT0                  = BitField(CONTROL, 50, 7, doc='Control the USER_BIT0 signal that can be routed to any user outputs')  # *** are bit0 and bit1 inverted?
    # USER_BIT1                  = BitField(CONTROL, 50, 6, doc='Control the USER_BIT1 signal that can be routed to any user outputs')
    USER_BITS                  = BitField(CONTROL, 50, 6, width=2, doc='Control the USER_BIT signals that can be routed to any user outputs')
    IRIG_DELAY_RESET           = BitField(CONTROL, 50, 5, doc='Loads the IRIG signal delay value into the delay block')
    IRIG_DELAY_RESET           = BitField(CONTROL, 50, 5, doc='Loads the IRIG signal delay value into the delay block')
    IRIGB_DELAY                = BitField(CONTROL, 50, 0, width=5, doc='IRIG-B signal delay')

    TIMESTAMP_VALID            = BitField(STATUS, 0, 7, doc='Timestamp data valid (i.e. can be read)')
    MMI_COOKIE                 = BitField(STATUS, 0, 0, width=7, doc='Firmware cookie (BSB-MMI access only), should be 0x42')
    # ADC_SYNC_READBACK          = BitField(STATUS, 1, 0, doc='Reads back the SYNC bit for debugging')
    LOG2_FRAME_LENGTH          = BitField(STATUS, 1, 0, width=5, doc='Number of ADC time samples per frame')
    LOG2_SAMPLES_PER_WORD      = BitField(STATUS, 1, 5, width=3, doc='Number of ADC time samples per word')
    NUMBER_OF_CHANNELIZERS     = BitField(STATUS, 2, 0, width=8, doc='Number of implemented antenna processing pipelines')
    NUMBER_OF_CORRELATORS      = BitField(STATUS, 3, 0, width=8, doc='Number of implemented correlators')
    NUMBER_OF_CHANNELIZERS_TO_CORRELATE = BitField(STATUS, 4, 0, width=8, doc='Number of channelizers handled by the correlators')
    NUMBER_OF_CHANNELIZERS_WITH_FFT = BitField(STATUS, 5, 0, width=8, doc='Number of channelizers with FFT')
    NUMBER_OF_GPU_LINKS        = BitField(STATUS, 6, 0, width=8, doc='Number of implemented GPU links')
    #    IMPLEMENT_ANT         = BitField(STATUS, 4, 0, width=8, doc='Indicates whether the antenna processor is implemented or if a dummy mmodule is put in place. There is one bit per antenna.')
    # IMPLEMENT_FFT              = BitField(STATUS, 6, 0, width=16, doc='Indicates whether the antenna processor FFT is implemented. If not, it is bypassed and timestream data is fed to the scaler. There is one bit per antenna. ')
    #    IMPLEMENT_CORR        = BitField(STATUS, 6, 0, width=8, doc='Indicates whether the correlator is implemented. . There is one bit per correlator. ')
    TIMESTAMP                  = BitField(STATUS, 10, 0, width=32, doc='Bitstream timestamp word')
    PLATFORM_ID                = BitField(STATUS, 11, 0, width=8, doc='Which FPGA/board in use.  0 for ML605 eval board, 1 for KC705 evaluation board')
    FPGA_SERIAL_NUMBER         = BitField(STATUS, 19, 0, width=64, doc='FPGA 57-bit serial number')
    NUMBER_OF_CROSSBAR_INPUTS  = BitField(STATUS, 20, 0, width=8, doc='Number of channelizer fed to the crossbar outputs')
    NUMBER_OF_CROSSBAR1_OUTPUTS = BitField(STATUS, 21, 0, width=8, doc='Number of crossbar outputs')
    PROTOCOL_VERSION           = BitField(STATUS, 23, 0, width=16, doc='Protocol version used to manage host software compatibility.')
    CHANNELIZERS_CLOCK_SOURCE  = BitField(STATUS, 24, 0, width=8, doc='Indicates which ADC is used to provide the clock from all channelizers.')
    NUMBER_OF_ADCS             = BitField(STATUS, 25, 0, width=8, doc='Number of ADCs inputs')
    ADC_BITS_PER_SAMPLE        = BitField(STATUS, 26, 0, width=8, doc='Number of bits in a ADC sample')
    ADC_CAL_FROZEN             = BitField(STATUS, 27, 0, width=8, doc='ADC calibration frozen')
    ADC_CAL_SIGNAL_DETECT      = BitField(STATUS, 28, 0, width=8, doc='ADC calibration dignal detect')
    ADC_OR                     = BitField(STATUS, 29, 0, width=8, doc='ADC overrange flag')
    ADC_OV                     = BitField(STATUS, 30, 0, width=8, doc='ADC overvoltage flag')
    ADC_PLL_LOCK0              = BitField(STATUS, 33, 6,  doc='Lock status of the ADC PLL in FMC0')
    ADC_PLL_LOCK1              = BitField(STATUS, 33, 7,  doc='Lock status of the ADC PLL in FMC1')
    CMD_RPLY_PACKET_COUNTERS   = BitField(STATUS, 35, 0, width=16, doc='Number of reply packets received since last FPGA configuration. MSB=Commands, LSB=Replies')
    EXTRA_IO                   = BitField(STATUS, 37, 0, width=8, doc='Various input IO signals provided by the platform. Used to QC the board.')
    ADC_CM_OV                  = BitField(STATUS, 38, 0, width=8, doc='ADC common-mode overvoltage flag')
    ADC_CM_OV                  = BitField(STATUS, 39, 0, width=8, doc='ADC common-mode undervoltage flag')
    ADC_CM_OT1                 = BitField(STATUS, 40, 0, width=8, doc='ADC over thresold1 flag')
    ADC_CM_OT1                 = BitField(STATUS, 41, 0, width=8, doc='ADC over thresold2 flag')
    CT_TYPE                    = BitField(STATUS, 42, 0, width=4, doc='Type of Corner-Turn engine used. 0=None, 1=BCT, 2=UCT')
    CT_LEVEL                   = BitField(STATUS, 42, 4, width=4, doc='Level of corner-turning implemented in the CT Engine. CT_LEVEL=1 Means internal CT only, which is hardwired for BCT and UCT')
    FFT_TYPE                   = BitField(STATUS, 43, 0, width=4, doc='Type of FFT implemented in the channelizers')


    SERIAL_MATCH               = BitField(CONTROL, 37, 7, doc='1 when the programmable target serial number matches the FPGA serial number. This means that the core register can be writtten if  ')
    BP_GPIO_INT_IN             = BitField(CONTROL, 37, 6, doc="FPGA I/O line")
    GPIO_IRQ_IN                = BitField(CONTROL, 37, 5, doc="FPGA I/O line")
    FLASH_CS_IN                = BitField(CONTROL, 37, 4, doc="FPGA I/O line")
    UART_TX_IN                 = BitField(CONTROL, 37, 3, doc="FPGA I/O line")
    UART_RX_IN                 = BitField(CONTROL, 37, 2, doc="FPGA I/O line")
    GPIO_RST_IN                = BitField(CONTROL, 37, 1, doc="FPGA I/O line")
    ARM_IRQ_IN                 = BitField(CONTROL, 37, 0, doc="FPGA I/O line")

    def __init__(self, *,  router,  router_port):
        super().__init__(router=router, router_port=router_port)
        self._lock() # prevent further property creation to avoid creating attributes by mistake

    def get_bitstream_date(self):
        """ Returns a string containing the date-time of the current firmware bitstream."""
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

    def set_pwm(self, enable=True, offset=0, high_time=195312, period=390625, pwm_reset=False):
        """ Set-ups the frame-based PWM generator, typically used to generate
        the noise injection gating signal.

        ``offset``, ``high-time`` and ``period`` are 32-bit values that describe the waveform.

        ``offset``: number of frames to ait after reset before the first HIGH
        ``high_time``: Number of frames to stay high
        ``period``: number of frames between the begginings of the high time

        A channelizer reset (or a SYNC signal, which generates one) must be issued after the values
        are changed to obtain the proper waveform.

        If ``enable`` is false, the PWM generator will be disabled

        If 'pwm_reset' is True, the PWM generator will be reset before being enabled. This is mainly
        useful for testing, as the offset will be taken from the current frame, not frame zero, and
        every board will operate on a random offsets. The PWM generator is always reset by SYNC events.
        A system-wide SYNC will therefore make the PWM signal synchronized across all boards.
        """
        self.logger.info('%r: Setting PWM generator to enable=%i, offset=%i, high_time=%i, period=%i' %
                         (self, enable, offset, high_time, period))
        self.PWM_OFFSET = offset
        self.PWM_HIGH_TIME = high_time - 1  # The actual high time is PWM_HIGH_TIME + 1
        self.PWM_PERIOD = period - 1  # The actual period is PWM_PERIOD + 1
        if enable:
            if pwm_reset:
                self.PWM_RESET = 1  # stops the PWM generator
            self.PWM_RESET = 0  # starts the PWM generator
        else:
            self.PWM_RESET = 1  # stops the PWM generator

    def get_pwm(self):
        """ Return the current settings of the PWM generator.

        Returns:

            A tuple containing the offset, high time, period and reset state.

        """
        return (self.PWM_OFFSET, self.PWM_HIGH_TIME, self.PWM_PERIOD, self.PWM_RESET)

    def get_user_bits(self):
        """ Returns the status of the two user-controlled bits that can be routed to any of the user outputs

        Returns:
            int: the value of the user bits
        """
        return self.USER_BITS

    def set_user_bits(self, value):
        """ Sets the two user-controlled bits that can be routed to any of the user outputs

        Paraeters:
            value (int): the value of the user bits
        """
        self.USER_BITS = value

    USER_OUTPUT_SOURCE_TABLE = {
        'sync_out': 0,  # User-generated SYNC signal (sync_out)
        'pps': 1,  # 1 PPS signal from the IRIG-B decoder (pps_out)
        'pwm': 2,  # Output from the frame-based pwm generator (pwm_out)
        'irigb_trig': 3,  # (not irigb_before_target)
        'bp_trig': 4,  # (bp_trig_reg)
        'bp_time': 5,  # (bp_time_reg)
        'refclk': 6,  # 10 MHz reference clock (clk10)
        'irigb_gen': 7,  # (irigb_gen_out)
        'heartbeat1': 8,  # (gpio_led_int(4))
        'heartbeat2': 9,  # (gpio_led_int(7))
        'debug1': 10,  # (debug1, currently crossbar2.align_pulse)
        'debug2': 11,  # (debug2, currently crossbar0.lane_monitor)
        'user_bit0': 12,  # (user_bit(0))
        'user_bit1': 13,  # (user_bit(1))
        'fmc_refclk': 14,  # Refclk from Mezz selected by user_bits(0:1)  (fmc_refclk(to_integer(unsigned(user_bit)))
        'input': 15  # Do not drive the output, use the connector as an input
        }

    USER_OUTPUTS = {
        0: 'USER_MUX_SOURCE0',
        'sma_a': 'USER_MUX_SOURCE0',

        1: 'USER_MUX_SOURCE1',
        'sma_b_fpga_led2': 'USER_MUX_SOURCE1',
        'sma_b': 'USER_MUX_SOURCE1',
        'led2': 'USER_MUX_SOURCE1',

        2: 'USER_MUX_SOURCE2',
        'bp_sma_fpga_led1': 'USER_MUX_SOURCE2',
        'bp_sma': 'USER_MUX_SOURCE2',
        'led1': 'USER_MUX_SOURCE2',

        3: 'USER_MUX_SOURCE3',
        'bp_gpio_int': 'USER_MUX_SOURCE3'
        }

    def set_user_output_source(self, source='', output=None):
        """
        Set the user output ``output`` to issue the signal specified in
        ``source``. ``source`` and ``output`` are strings.

        If source or output ar eomitted, an error is raised and the list of
        valid values is shown.
        """
        if source not in self.USER_OUTPUT_SOURCE_TABLE:
            raise AttributeError("Invalid source '%s'. Valid sources are %s." % (
                source, ', '.join(list(self.USER_OUTPUT_SOURCE_TABLE.keys()))))
        if output not in self.USER_OUTPUTS:
            raise AttributeError("Invalid output '%s'. Valid outputs are %s." % (
                output, ', '.join(str(k) for k in list(self.USER_OUTPUTS.keys()))))

        self.write_bitfield(self.USER_OUTPUTS[output], self.USER_OUTPUT_SOURCE_TABLE[source])

    def get_user_output_source(self, output):
        """ Return the current user output source as a string """

        if output not in self.USER_OUTPUTS:
            raise AttributeError("Invalid output '%s'. Valid outputs are %s." % (
                output, ', '.join(str(k) for k in list(self.USER_OUTPUTS.keys()))))

        current_value = self.read_bitfield(self.USER_OUTPUTS[output])

        for (source, value) in list(self.USER_OUTPUT_SOURCE_TABLE.items()):
            if current_value == value:
                return source
        raise RuntimeError('Invalid user output source number found on the FPGA')

    def pulse_ant_reset(self):
        """ Pulses the channelizer reset lines. """
        self.pulse_bit('ANT_RESET')

    def set_channelizer_reset(self, state):
        """ sets the channelizer reset lines. """
        self.ANT_RESET = state

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
        return (word >> 8, (word + 1) & 0xFF)  # Add 1 for the reply packet

    def init(self):
        """
        Initializes the GPIO module operations.

        This puts the channelizers and correlators in reset state."""
        self.ANT_RESET = 1
        self.CORR_RESET = 1
        # self.USER_RESET = 0

        # Buck sync is enabled by default and starts immediately when the FPGA is programmed

        # The commented code below was for the CRS platform before platform-specific freqs and enable status could be set in firmware
        # it is started by software
        # if self.PLATFORM_ID == self.fpga._PLATFORM_ID_CRS:
            # self.BUCK_CLK_DIV = 24
            # self.logger.info(f'Enabling CRS Buck sync at {200/16/self.BUCK_CLK_DIV:.3f} MHz NOW!')
            # self.BUCK_SYNC_ENABLE = 1

        self.logger.info(f'{self!r}: Buck switching frequency is set at {200/16/self.BUCK_CLK_DIV:.3f} MHz. Status: {"Enabled" if self.BUCK_SYNC_ENABLE else "DISABLED"}')

        # In the alternate code below, we do not use self.ANT_RESET=1 to reset
        # the antenna because this implies reading the control register, and
        # the read data might not get through if too much data is coming in

        # ant_reset = self.get_bitfield('ANT_RESET')
        # self.write(ant_reset.addr, 1 << ant_reset.bit)
        # self.write(ant_reset.addr, 0x60) # ** debug  BEWARE: This resets the DATA and CORR IP addresses to zero!!!!!***

        # Indicates how often the host UDP buffers are read. Used to throttle
        # data transmission. Period = 2/125MHz*2^value
        self.HOST_FRAME_READ_RATE = 14

    def status(self):
        """ Displays the module status"""
        self.logger.info(f'-------------------------GPIO--------------------------------------')
        self.logger.info(f'Bistream timestamp is: {self.get_bitstream_date()}')




