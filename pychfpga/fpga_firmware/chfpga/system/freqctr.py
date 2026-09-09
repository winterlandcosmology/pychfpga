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
import asyncio
import time

from ..mmi import MMI, BitField, CONTROL, STATUS

class FreqCtr(MMI):
    """
    Implements the Frequency Counter Interface.
    """
    ADDRESS_WIDTH = 12

    _SYSTEM_CLOCK_FREQUENCY = 200e6  # in Hz
    # Extra time allowed after the FPGA gate before declaring the counter stuck.
    _FREQ_DONE_TIMEOUT_MARGIN = 1.0

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

    def __init__(self, *, router, router_port, config, verbose=1):
        self.verbose = verbose
        super().__init__(router=router, router_port=router_port)
        self.config = config
        self._lock()  # prevent further property creation to avoid creating attributes by mistake


    def init(self):
        """
        Initializes the frequency counter module.
        """
        pass

    def _start_frequency_count(self, port, gate_time):
        """Program and start a frequency measurement. Returns the numeric port id."""
        if isinstance(port, str):
            port = self.PORTS[port]
        self.GATE_COUNT = int(self._SYSTEM_CLOCK_FREQUENCY * gate_time)
        self.SOURCE = port  # Sets the signal source to be measured
        self.START = 0  # Clears the counter
        self.START = 1  # starts the frequency counter
        return port

    def _frequency_from_count(self, gate_time):
        return self.FREQ_COUNT * 2.0 / gate_time

    def _frequency_done_timeout(self, port, gate_time):
        return RuntimeError(
            f'{self!r}: Frequency counter did not complete within '
            f'{gate_time + self._FREQ_DONE_TIMEOUT_MARGIN:.3f} s '
            f'(port={port}, gate_time={gate_time})')

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

            Prefer :meth:`read_frequency_async` from async code so the gate wait
            yields to other boards.
        """
        port = self._start_frequency_count(port, gate_time)
        t0 = time.monotonic()
        deadline = t0 + gate_time + self._FREQ_DONE_TIMEOUT_MARGIN
        while not self.DONE:
            now = time.monotonic()
            if now >= deadline:
                raise self._frequency_done_timeout(port, gate_time)
            remaining = gate_time - (now - t0)
            time.sleep(remaining if remaining > 0 else 0.001)
        return self._frequency_from_count(gate_time)

    async def read_frequency_async(self, port, gate_time=0.01):
        """Async version of :meth:`read_frequency`.

        Sleeps for the FPGA gate time instead of busy-waiting on ``DONE``, so
        other boards can make progress on the event loop.

        Parameters:
            port (str or int): port name or port number from which to measure the frequency. See  `PORTS` table.

            gate_time (float): Time (in seconds) during which the frequency of the selected source
               will be measured. Affects the measurement time and the resolution. Maximum is limited
               by the gating counter width (32 bits) to 21.47 seconds (2^32/200 MHz).

        Returns:
            (float): Frequency of the selected source (port) in Hz.

        Note:
            The frequency resolution is given by resolution = 2/`gate_time`

            Prefer :meth:`read_frequency` from synchronous code so the gate wait
            yields to other boards.
        """
        port = self._start_frequency_count(port, gate_time)
        t0 = time.monotonic()
        deadline = t0 + gate_time + self._FREQ_DONE_TIMEOUT_MARGIN
        while not self.DONE:
            now = time.monotonic()
            if now >= deadline:
                raise self._frequency_done_timeout(port, gate_time)
            remaining = gate_time - (now - t0)
            await asyncio.sleep(remaining if remaining > 0 else 0.001)
        return self._frequency_from_count(gate_time)

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
        print(f'   IceBoard Reference clock source: {fpga.mb.get_iceboard_clock_source_sync()}')
        print(' External clock sources')
        print(f"   RAW CLK (no PLL, SE):     {self.read_frequency('RAW_CLK', gate_time=gate_time) / 1000000.0:7.3f} MHz")
        print(f"   Reference clock (via PLL):{self.read_frequency('CLK10', gate_time=gate_time) / 1000000.0:7.3f} MHz")
        print(f"   MGT CLK100 (via PLL, not used):{self.read_frequency('MGT_CLK100', gate_time=gate_time) / 1000000.0:7.3f} MHz")
        print(f"   MGT CLK125 (via PLL, not used):{self.read_frequency('MGT_CLK125', gate_time=gate_time) / 1000000.0:7.3f} MHz")
        print(f"   MGT CLK200 (via PLL, not used):{self.read_frequency('MGT_CLK200', gate_time=gate_time) / 1000000.0:7.3f} MHz")
        print(f"   SFP REFCLK (via PLL):          {self.read_frequency('SFP_REFCLK', gate_time=gate_time) / 1000000.0:7.3f} MHz")
        print(f"   BP Shuffle Ref clock:     {self.read_frequency('BP_SHUFFLE_REFCLK', gate_time=gate_time) / 1000000.0:7.3f} MHz")
        print(f"   GPU link Ref clock:       {self.read_frequency('GPU_REFCLK', gate_time=gate_time) / 1000000.0:7.3f} MHz")
        print(' Internally generated system clocks (from SFP_REFCLK)')
        print(f"   CLK200:                   {self.read_frequency('CLK200', gate_time=gate_time) / 1000000.0:7.3f} MHz")
        print(f"   CTRL_CLK:                 {self.read_frequency('CTRL_CLK', gate_time=gate_time) / 1000000.0:7.3f} MHz")
        print(f"   SYSMON_CLK:               {self.read_frequency('SYSMON_CLK', gate_time=gate_time) / 1000000.0:7.3f} MHz")
        print(f"   Channelizers clock:       {self.read_frequency('ANT_CLK', gate_time=gate_time) / 1000000.0:7.3f} MHz (Source= {int(PLL_CLK_SRC)} ({ant_clock_source_string}))")
        print(f"   Correlator:               {self.read_frequency('CORR_CLK', gate_time=gate_time) / 1000000.0:7.3f} MHz")
        print(f'   BP Shuffle TX word clock: {bp_shuffle_txclk / 1000000.0:7.3f} MHz ({bp_shuffle_txclk * 32 * 32 / 33 / 1000000000.0:0.3f} Gbps)')
        print(f'   GPU link TX word clock:   {gpu_txclk / 1000000.0:7.3f} MHz ({gpu_txclk * 32 * 32 / 33 / 1000000000.0:0.3f} Gbps)')
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
            adc_mhz = self.read_frequency(f'ADC_CLK{i}', gate_time=gate_time) / 1e6
            print(f'   ADC{i:02d} clock:              {adc_mhz:7.3f} MHz')
        print(f'   Resolution:     {resolution / 1000000.0:10.6f} MHz')
        print(f'   Gate time:      {gate_time:.3f} s')
        print(f"   FPGA Fan speed: {self.read_frequency('FAN', gate_time=fan_gate_time) * 60.0 / 2:7.0f} RPM (resolution {fan_resolution * 60.0 / 2:.0f} RPM)") # 1 Hz=60 RPM, divide by 2 because there is 2 pulses per fan turn

