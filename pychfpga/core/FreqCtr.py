#!/usr/bin/python
# Disable pylint Line too long (=C0301)
# pylint: disable=C0301

"""
FreqCtr.py module
 Implements the Frequency Counter interface

History:
    2011-07-13 : JFC : Created from test code in chFPGA.py
    2011-09-08 JFC: Added FMC_REFCLK
    2011-09-25 JFC: Added fan RPM readout
    2012-05-31 JFC: Added data processing frequency readout. Cleanup status() display.
    2012-10-17 JFC: Added correlator frequency
    2012-11-09 JFC: Modified to use Module. Uses fpga SYSTEM_CLOCK_FREQUENCY variable.
"""
from Module import Module_base, BitField

class FreqCtr_base(Module_base):
    """
    Implements the Frequency Counter Interface.
    """

    # Frequency counter port definitions
    PORTS = {
    'ADC_CLK0': 0,
    'ADC_CLK1': 1,
    'ADC_CLK2': 2,
    'ADC_CLK3': 3,
    'ADC_CLK4': 4,
    'ADC_CLK5': 5,
    'ADC_CLK6': 6,
    'ADC_CLK7': 7,
    'ADC_CLK8': 8,
    'ADC_CLK9': 9,
    'ADC_CLK10': 10,
    'ADC_CLK11': 11,
    'ADC_CLK12': 12,
    'ADC_CLK13': 13,
    'ADC_CLK14': 14,
    'ADC_CLK15': 15,
    'FMCA_MGT_PLL_REFCLK0': 16, #
    'FMCB_REFCLK': 17,
    'FMCA_REFCLK': 18,
    'CLK200': 19,
    'CTRL_CLK': 20,
    'FAN': 21,
    'ANT_CLK': 22,
    'CORR_CLK': 23,
    'SYSMON_CLK': 24,
    'GPU_REFCLK': 25,
    'GPU_TXCLK': 26,
    'BP_SHUFFLE_REFCLK': 27,
    'BP_SHUFFLE_TXCLK': 28,
    'FMCA_MGT_PLL_REFCLK1': 29,  #
    'FMCB_MGT_PLL_REFCLK0': 30,  #
    'FMCB_MGT_PLL_REFCLK1': 31,  #
    }

    # Create local variables for page numbers tomake the table more readable
    CONTROL = BitField.CONTROL
    STATUS = BitField.STATUS

    GATE_COUNT = BitField(CONTROL, 3, 0, width=32, doc='Gate time, set in 200 MHz clocks')
    SOURCE = BitField(CONTROL, 4, 0, width= 7, doc='Select signal to be measured')
    START = BitField(CONTROL, 4, 7, doc='When 0, resets the frequency counter.  When high, counts the uncoming clock edges until the gate time is elapsed.')

    FREQ_COUNT = BitField(STATUS, 3, 0, width=32, doc='Frequency count (number of rising edges seen on the source signal during the gate time)')
    DONE = BitField(STATUS, 4, 0, doc='Frequency counting is complete (gate time has been reached).')


    # Registers

    def __init__(self, fpga_instance, base_address, verbose=1):
        self.verbose = verbose
        super(self.__class__, self).__init__(fpga_instance, base_address)
        self._lock() # prevent further property creation to avoid creating attrubutes by mistake

#    def read(self, addr, type=np.uint8):
#        """ Reads from the register of the frequency counter"""
#        fpga = self.fpga
#        data = fpga.Read(fpga.SYSTEM_PORT, fpga.SYSTEM_FREQ_CTR_MODULE, addr, type)
#        return data
#
#    def write(self, addr, data):
#        """ Writes to the register of the frequency counter"""
#        fpga = self.fpga
#        fpga.Write(fpga.SYSTEM_PORT, fpga.SYSTEM_FREQ_CTR_MODULE, addr, data)

    def init(self):
        """
        Initializes the frequency counter module.
        """
        pass


    def read_frequency(self, port, gate_time=0.01):
        """ Reads the frequency (in Hz) of the specified frequency counter input port
        """
        ref_freq = self.fpga._SYSTEM_CLOCK_FREQUENCY
        #gate_ctr = np.array([ref_freq*gate_time], np.dtype('>u4'))
        #gate_ctr.dtype = np.uint8
        gate_ctr = int(ref_freq*gate_time)
        #print gate_ctr
        #self.write(0x00, gate_ctr)
        self.GATE_COUNT = gate_ctr

        if type(port) is str:
            port = self.PORTS[port]
        self.SOURCE = port # Sets the signal source to be measured
        self.START = 0 # Clears the counter
        self.START = 1 # starts the frequncy counter
        #self.write(0x04, (port << 4) + 0x00) # Reset frequency counter
        #self.write(0x04, (port << 4) + 0x01) # Start frequency counter
        while not self.DONE:
            pass
        freq = self.FREQ_COUNT
        return freq * 2.0 / gate_time

    def status(self):
        """
        Prints the Frequency Counter status.
        """
        fpga = self.fpga

        gate_time = 0.05
        resolution = 2.0 / gate_time

        fan_gate_time = 0.2
        fan_resolution = 2.0 / fan_gate_time

        PLL_CLK_SRC = fpga.GPIO.CHAN_CLK_SRC
        ant_clock_source_string = ('ADC','SYSTEM CLOCK')[PLL_CLK_SRC]

        bp_shuffle_txclk = self.read_frequency('BP_SHUFFLE_TXCLK', gate_time=gate_time)
        gpu_txclk = self.read_frequency('GPU_TXCLK', gate_time=gate_time)

        print 'System Frequencies:'
        print '   IceBoard Reference clock source: %s' % (fpga.get_clock_source())
        print '   System clock:             %7.3f MHz' % (self.read_frequency('CLK200', gate_time=gate_time) / 1e6)
        print '   CTRL_CLK:                 %7.3f MHz' % (self.read_frequency('CTRL_CLK', gate_time=gate_time) / 1e6)
        print '   SYSMON_CLK:               %7.3f MHz' % (self.read_frequency('SYSMON_CLK', gate_time=gate_time) / 1e6)
        print '   Channelizers clock:       %7.3f MHz (Source= %i (%s))' % (self.read_frequency('ANT_CLK', gate_time=gate_time) / 1e6, PLL_CLK_SRC, ant_clock_source_string)
        print '   Correlator:               %7.3f MHz' % (self.read_frequency('CORR_CLK', gate_time=gate_time) / 1e6)
        print '   FMCA Reference:           %7.3f MHz%s' % (self.read_frequency('FMCA_REFCLK', gate_time=gate_time) / 1e6, '' if fpga.is_fmc_present(0) else ' (ADC board not present)')
        print '   FMCA MGT PLL Ref clock 0: %7.3f MHz' % (self.read_frequency('FMCA_MGT_PLL_REFCLK0', gate_time=gate_time) / 1e6)
        print '   FMCA MGT PLL Ref clock 1: %7.3f MHz' % (self.read_frequency('FMCA_MGT_PLL_REFCLK1', gate_time=gate_time) / 1e6)
        print '   FMCB Reference:           %7.3f MHz%s' % (self.read_frequency('FMCB_REFCLK', gate_time=gate_time) / 1e6, '' if fpga.is_fmc_present(1) else ' (ADC board not present)')
        print '   FMCB MGT PLL Ref clock 0: %7.3f MHz' % (self.read_frequency('FMCB_MGT_PLL_REFCLK0', gate_time=gate_time) / 1e6)
        print '   FMCB MGT PLL Ref clock 1: %7.3f MHz' % (self.read_frequency('FMCB_MGT_PLL_REFCLK1', gate_time=gate_time) / 1e6)
        print '   BP Shuffle Ref clock:     %7.3f MHz' % (self.read_frequency('BP_SHUFFLE_REFCLK', gate_time=gate_time) / 1e6)
        print '   BP Shuffle TX word clock: %7.3f MHz (%0.3f Gbps)' % (bp_shuffle_txclk / 1e6, bp_shuffle_txclk*32*32/33/1e9)
        print '   GPU link Ref clock:       %7.3f MHz' % (self.read_frequency('GPU_REFCLK', gate_time=gate_time) / 1e6)
        print '   GPU link TX word clock:   %7.3f MHz (%0.3f Gbps)' % (gpu_txclk / 1e6, gpu_txclk*32*32/33/1e9)
        for i in range(fpga.NUMBER_OF_ANTENNAS):
            print '   ADC%02i clock:              %7.3f MHz%s' % (i, self.read_frequency('ADC_CLK%i' % i, gate_time=gate_time) / 1e6, '' if fpga.is_fmc_present(0) else ' (No ADC board in FMCA - Cannot clock the channelizers)')
        print '   Resolution:     %10.6f MHz' % (resolution / 1e6)
        print '   Gate time:      %.3f s' % (gate_time)
        print '   FPGA Fan speed: %7.0f RPM (resolution %.0f RPM)' % (self.read_frequency('FAN', gate_time=fan_gate_time)*60. / 2, fan_resolution*60. / 2) # 1 Hz=60 RPM, divide by 2 because there is 2 pulses per fan turn

