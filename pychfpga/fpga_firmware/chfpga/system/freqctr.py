#!/usr/bin/python

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
from ..mmi import MMI, BitField, CONTROL, STATUS

class FreqCtr(MMI):
    """
    Implements the Frequency Counter Interface.
    """
    ADDRESS_WIDTH = 12

    _SYSTEM_CLOCK_FREQUENCY = 200e6  # in Hz

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
    'RAW_CLK': 32,  #
    'MGT_CLK100': 33,  #
    'MGT_CLK200': 34,  #
    'SFP_REFCLK': 35,  #
    'CLK10': 36,  #
    'MGT_CLK125': 37,  #
    'BP_SHUFFLE_REFCLK1': 38,  #
    'PLL_OSCOUT': 39,  #
    }

    GATE_COUNT = BitField(CONTROL, 3, 0, width=32, doc='Gate time, set in 200 MHz clocks')
    SOURCE = BitField(CONTROL, 4, 0, width=7, doc='Select signal to be measured')
    START = BitField(CONTROL, 4, 7, doc='When 0, resets the frequency counter.  When high, counts the uncoming clock edges until the gate time is elapsed.')

    FREQ_COUNT = BitField(STATUS, 3, 0, width=32, doc='Frequency count (number of rising edges seen on the source signal during the gate time)')
    DONE = BitField(STATUS, 4, 0, doc='Frequency counting is complete (gate time has been reached).')

    def __init__(self, *, router, router_port, verbose=1):
        self.verbose = verbose
        super().__init__(router=router, router_port=router_port)
        self._lock()  # prevent further property creation to avoid creating attributes by mistake


    def init(self):
        """
        Initializes the frequency counter module.
        """
        pass


    def read_frequency(self, port, gate_time=0.01):
        """ Read the frequency (in Hz) of the specified frequency counter input port.

        Arguments:
            port (str or int): port name or port number from which to measure the frequency. See  `PORTS` table.

            gate_time (float): Time (in seconds) during which the frequency of the selected source
               will be measured. Affects the measurement time and the resolution. Maximum is limited
               by the gating counter width (32 bits) to 21.47 seconds (2^32/200 MHz).

        Returns:
            (float): Frequency of the selected source (port) in Hz.

        Note:
            The frequency resolution is given by resolution = 2/`gate_time`
        """
        ref_freq = self._SYSTEM_CLOCK_FREQUENCY
        gate_ctr = int(ref_freq * gate_time)
        self.GATE_COUNT = gate_ctr

        if type(port) is str:
            port = self.PORTS[port]
        self.SOURCE = port  # Sets the signal source to be measured
        self.START = 0  # Clears the counter
        self.START = 1  # starts the frequncy counter
        while not self.DONE:
            pass
        freq = self.FREQ_COUNT
        return freq * 2.0 / gate_time

    def status(self):
        """
        Prints the Frequency Counter status.
        """
        fpga = self.fpga
        mb = fpga.mb

        gate_time = 0.05
        resolution = 2.0 / gate_time

        fan_gate_time = 0.2
        fan_resolution = 2.0 / fan_gate_time

        PLL_CLK_SRC = fpga.GPIO.CHAN_CLK_SRC
        ant_clock_source_string = ('ADC', 'SYSTEM CLOCK')[PLL_CLK_SRC]

        bp_shuffle_txclk = self.read_frequency('BP_SHUFFLE_TXCLK', gate_time=gate_time)
        gpu_txclk = self.read_frequency('GPU_TXCLK', gate_time=gate_time)

        print('System Frequencies:')
        print('   IceBoard Reference clock source: %s' % (fpga.mb.get_iceboard_clock_source_sync()))
        print(' External clock sources')
        print('   RAW CLK (no PLL, SE):     %7.3f MHz' % (self.read_frequency('RAW_CLK', gate_time=gate_time) / 1e6))
        print('   Reference clock (via PLL):%7.3f MHz' % (self.read_frequency('CLK10', gate_time=gate_time) / 1e6))
        print('   MGT CLK100 (via PLL, not used):%7.3f MHz' % (self.read_frequency('MGT_CLK100', gate_time=gate_time) / 1e6))
        print('   MGT CLK125 (via PLL, not used):%7.3f MHz' % (self.read_frequency('MGT_CLK125', gate_time=gate_time) / 1e6))
        print('   MGT CLK200 (via PLL, not used):%7.3f MHz' % (self.read_frequency('MGT_CLK200', gate_time=gate_time) / 1e6))
        print('   SFP REFCLK (via PLL):          %7.3f MHz' % (self.read_frequency('SFP_REFCLK', gate_time=gate_time) / 1e6))
        print('   BP Shuffle Ref clock:     %7.3f MHz' % (self.read_frequency('BP_SHUFFLE_REFCLK', gate_time=gate_time) / 1e6))
        print('   GPU link Ref clock:       %7.3f MHz' % (self.read_frequency('GPU_REFCLK', gate_time=gate_time) / 1e6))
        print(' Internally generated system clocks (from SFP_REFCLK)')
        print('   CLK200:                   %7.3f MHz' % (self.read_frequency('CLK200', gate_time=gate_time) / 1e6))
        print('   CTRL_CLK:                 %7.3f MHz' % (self.read_frequency('CTRL_CLK', gate_time=gate_time) / 1e6))
        print('   SYSMON_CLK:               %7.3f MHz' % (self.read_frequency('SYSMON_CLK', gate_time=gate_time) / 1e6))
        print('   Channelizers clock:       %7.3f MHz (Source= %i (%s))' % (self.read_frequency('ANT_CLK', gate_time=gate_time) / 1e6, PLL_CLK_SRC, ant_clock_source_string))
        print('   Correlator:               %7.3f MHz' % (self.read_frequency('CORR_CLK', gate_time=gate_time) / 1e6))
        print('   BP Shuffle TX word clock: %7.3f MHz (%0.3f Gbps)' % (bp_shuffle_txclk / 1e6, bp_shuffle_txclk * 32 * 32 /33 / 1e9))
        print('   GPU link TX word clock:   %7.3f MHz (%0.3f Gbps)' % (gpu_txclk / 1e6, gpu_txclk * 32 * 32 / 33 / 1e9))
        for fmc in range(mb.NUMBER_OF_FMC_SLOTS):
            if mb.is_fmc_present(fmc):
                fmc_name = 'FMC' + chr(ord('A') + fmc)
                print(f' {fmc_name} clocks')
                freq_refclk = self.read_frequency(f'{fmc_name}_REFCLK', gate_time=gate_time) / 1e6
                freq_mgt0 = self.read_frequency(f'{fmc_name}_MGT_PLL_REFCLK0', gate_time=gate_time) / 1e6
                freq_mgt1 = self.read_frequency(f'{fmc_name}_MGT_PLL_REFCLK1', gate_time=gate_time) / 1e6
                print(f'   {fmc_name} Reference:           {freq_refclk:7.3f} MHz')
                print(f'   {fmc_name} MGT PLL Ref clock 0: {freq_mgt0:7.3f} MHz')
                print(f'   {fmc_name} MGT PLL Ref clock 1: {freq_mgt1:7.3f} MHz')
            else:
                print(f' {fmc_name}: Not present')

        print(' ADC clocks')
        for i in range(fpga.NUMBER_OF_ADCS or fpga.NUMBER_OF_CHANNELIZERS):
            # print('   ADC%02i clock:              %7.3f MHz%s' % (i, self.read_frequency('ADC_CLK%i' % i, gate_time=gate_time) / 1e6, '' if fpga.is_fmc_present(0) else ' (No ADC board in FMCA - Cannot clock the channelizers)'))
            print('   ADC%02i clock:              %7.3f MHz' % (i, self.read_frequency('ADC_CLK%i' % i, gate_time=gate_time) / 1e6))
        print('   Resolution:     %10.6f MHz' % (resolution / 1e6))
        print('   Gate time:      %.3f s' % (gate_time))
        print('   FPGA Fan speed: %7.0f RPM (resolution %.0f RPM)' % (self.read_frequency('FAN', gate_time=fan_gate_time) * 60. / 2, fan_resolution * 60. / 2)) # 1 Hz=60 RPM, divide by 2 because there is 2 pulses per fan turn

