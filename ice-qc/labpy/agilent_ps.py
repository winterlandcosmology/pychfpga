#!/Library/Frameworks/EPD64.framework/Versions/Current/bin/python
#import matplotlib.pyplot as pyplot
#matplotlib.use('Agg')
#import pylab
#import numpy as np
#import os.path
#import re

import GPIB
#import utils

# reload(GPIB)

# Define instrument model codes to easily refer to specific DVMs
AGILENTE3648A = 'AgilentE3648A'
AGILENTE3631A = 'AgilentE3631A'
AGILENTE3631A_RAILS = {
    'P6V': (0,6),
    'P25V' : (0, 25),
    'N25V': (-25, 0)
    }

# The following dictionnary lists the supported DVMs and provides a tuple containing (instrument name, ID string)
SUPPORTED_PS = {
    AGILENTE3648A: ('Agilent E3648A dual channel power supply', 'E3648A,', 2),
    AGILENTE3631A: ('Agilent E3631A triple channel power supply', 'E3631A', 3),
}

class AgilentPS(GPIB.GPIB):
    """
    A class to communicate with an Agilent power supply. The class supports:
        - Agilent E3648A Dual output power supply.
    """

    def __init__(self, interface = 'prologix_eth', gpib_addr=5, ip_addr='10.10.10.137', timeout=2):

        # self.dummy = (interface =='dummy') # save a dummy flag for use my the methods of this class
        super(AgilentPS, self).__init__(interface= interface, gpib_addr=gpib_addr, ip_addr=ip_addr, timeout = timeout)

        self.model = None
        print 'Initializing instrument',
        # self.device_clear()
        id_string=self.query('*IDN?', verbose=0, timeout=1)
        print 'Instrument Identification string:', id_string
        for instrument_code, (instrument_name, instrument_id_string, Noutputs) in SUPPORTED_PS.items():
            if instrument_id_string in id_string:
                self.model = instrument_code
                self.Noutputs = Noutputs
                print 'Connected to: %s' % instrument_name

        if self.model is None:
            raise GPIB.GPIBException('The identification command did not return the expected instrument ID string')

    def select_output(self, output):
        """
        Selects the power supply output to be affected by subsequent commands.
        """
        if output is None:
            return
        if self.model == AGILENTE3648A:
            if output not in (1, 2):
                raise ValueError('Bad output number. Must be 1 or 2')
            self.command('INST OUT%i' % output)
        elif self.model == AGILENTE3631A:
            if output not in ('P6V', 'P25V', 'N25V'):            
                raise ValueError('Bad output number. Must be P6V, P25V or N25V')
            self.command('INST %s' % output)

    def set_voltage(self, output, voltage):
        """
        Sets the output voltage - Valid range is 0 to 21V - default is None
        Returns the power supply setpoint voltage
        """

        self.flush_interface(timeout=0.1)
        self.select_output(output)

        if self.model == AGILENTE3631A:
            vmin, vmax = AGILENTE3631A_RAILS[output.upper()]
        elif self.model == AGILENTE3648A:
            vmin, vmax = (0, 21)
        # print vmin, vmax, vmin > voltage > vmax
        if voltage is not None:
            if vmin <= voltage <= vmax :
                self.command('VOLT %s' % voltage)
                self.waituntilready()
            else:
                raise ValueError('Invalid voltage - must be between %fV and %fV - no action performed' % (vmin, vmax))

        setpoint = self.query_float('VOLT?')
        return setpoint

    def set_current(self, output=None, current=None):
        """
        Sets the maximum output current of the specified output.
        """

        self.flush_interface(timeout=0.01)
        self.select_output(output)
        if current is not None:
            self.command('CURR %s' % current)
            self.waituntilready()

    def get_current(self, output=None):
        """
        Measures the output current of the specified output.
        """

        self.select_output(output)
        return self.query_float('MEAS:CURR?')

    def get_voltage(self, output=None):
        """
        Measures the output voltage of the specified output.
        """

        self.select_output(output)
        return self.query_float('MEAS:VOLT?')


    def waituntilready(self):
        while not(self.query_float('*OPC?')):
            time.sleep(0.01)

    def beep(self):
        self.command('SYST:BEEP')

    def output_enable(self, state):
        self.command('OUTP %s' % (('OFF','ON')[bool(state)]))

if __name__ == "__main__":

    print 'Creating ps object'
    ps = AgilentPS()
    # print 'Curent DMM voltage is %f V' % dmm.get_dc_voltage()

