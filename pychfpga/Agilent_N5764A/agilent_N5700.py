#!/Library/Frameworks/EPD64.framework/Versions/Current/bin/python
#import matplotlib.pyplot as pyplot
#matplotlib.use('Agg')
#import pylab
#import numpy as np
#import os.path
#import re

import GPIB
import time
#import utils

reload(GPIB)

# Define instrument model codes to easily refer to specific DVMs
AGILENTN5764A = 'N5764A'

# The following dictionnary lists the supported DVMs and provides a tuple containing (instrument name, ID string)
SUPPORTED_PS = {
    AGILENTN5764A: ('Agilent Power Supply', 'Agilent Technologies,N5764A')
}

class agilent_N5700(GPIB.GPIB):
    """
    A class to communicate with an Agilent N5700 power supply
    """

    def __init__(self, interface='lan', gpib_addr=14, ip_addr='10.10.10.220', ip_port=5025, timeout=0.5, verbose=1):

        super(agilent_N5700, self).__init__(interface=interface, gpib_addr=gpib_addr, ip_addr=ip_addr, ip_port=ip_port, timeout=timeout)

        if verbose:
            print 'Initializing instrument'
        self.device_clear()

        id_string = self.query('*IDN?', verbose=0, timeout=1)
        if verbose:
            print 'Instrument Identification string:', id_string
        for (instrument_code, (instrument_name, instrument_id_string)) in SUPPORTED_PS.items():
            if instrument_id_string in id_string:

                if verbose:
                    print 'Connected to: %s' % instrument_name
                self.command('STATus:OPERation:ENABle %i' % 0x0500)  # We wish to know is in constant current or constant voltage mode
                self.instrument_model = instrument_code
                self.instrument_name = instrument_name
                break

        if instrument_code is None:
            raise GPIB.GPIBException('The identification command did not return the expected instrument ID string')

    def waituntilready(self):
        while not(self.query_float('*OPC?')):
            time.sleep(0.01)

    def status(self):

        """
        Returns a dictionary containting the output voltage and current
        """
        self.flush_interface(timeout=0.01)

        meas = {'current': 0, 'voltage': 0}
        current = self.query_float('MEAS:CURR?', timeout=2)
        voltage = self.query_float('MEAS:VOLT?', timeout=2)
        power = round(current * voltage, 3)
        meas['current'] = current
        meas['voltage'] = voltage
        meas['power'] = power

        failmode = int(self.query_float('STAT:QUES:COND?'))
        if failmode != 0:
            meas['status'] = 'FAULT'
        else:
            OpState = int(self.query_float('STATus:OPERation:CONDition?'))
            if bool((OpState & (1 << 8)) >> 8):  # Voltage regulating
                meas['status'] = 'OK'
            elif bool((OpState & (1 << 10)) >> 10):  # Current limiting
                meas['status'] = 'ILIMIT'
            else:
                meas['status'] = 'OFF'

        return meas

    def measure(self):
        print 'Please use status function instead'
        return self.status()

    def output(self, state=None, readonly=True):

        """
        Turns on and off the output and measures current output state input
        state can be varius spellings of 'on'/ 'off', None, 0, or 1   (default
        is None)

        Returns status dictionary
        """
        self.flush_interface(timeout=0.01)
        outstate = []
        if state is None:
            pass
        elif not readonly:
            if state in ['on', 'On', 'ON', 1, True]:
                state = 1
            elif state in ['off', 'Off', 'OFF', 0, False]:
                state = 0
            else:
                raise GPIB.GPIBException('Unknown desired output power state')

            self.command('OUTP:STAT %s' % state)
            self.waituntilready()
            # self.command('*WAI')

        outstate = self.query_float('OUTP:STAT?')
        return {'PowerEnabled': bool(outstate)}

    def control_voltage(self, voltage=None, readonly=True):
        """
        Sets or gets output voltage - Valid range is 0 to 21V - default is 0,
        readonly must be set to False

        Returns the power supply setpoint voltage
        """

        self.flush_interface(timeout=0.01)

        if voltage is not None:
            if voltage > 21 or voltage < 0:
                raise ValueError('Invalid voltage - must be in range [0..21] - no action performed')
            elif readonly:
                raise RuntimeError('readonly = True - cannot change output voltage')
            else:
                self.command('VOLT %s' % voltage)
                # self.command('*WAI')
                self.waituntilready()

        setpoint = self.query_float('VOLT?')
        return setpoint

    def control_current(self, current=None, readonly=True):
        """
        Sets/gets the current limit - Valid range is 0 to 76A - default is 0,
        readonly must be set to False

        Returns the power supply setpoint current limit
        """

        self.flush_interface(timeout=0.01)

        if current is not None:
            if current > 76 or current < 0:
                raise ValueError('Invalid current limit - must be in range [0..76] - no action performed')
            elif readonly:
                raise RuntimeError('readonly = True - cannot change current limit')
            else:
                self.command('CURR %s' % current)
                # self.command('*WAI')
                self.waituntilready()

        setpoint = self.query_float('CURR?')
        return setpoint

    def set_voltage(self, voltage=None):
        """
        Sets the output voltage - Valid range is 0 to 21V - default is None
        Returns the power supply setpoint voltage
        """
        return self.control_voltage(voltage=voltage, readonly=False)

    def set_current_limit(self, current=None, ocp=None):
        """
        Sets the current limit and can enable disable ocp  - Valid range is 0
        to 76A - by default current is None and ocp is None

        Returns the power supply current limit
        """

        if ocp is not None:
            self.protection(ocp=ocp, readonly=False)
        return self.control_current(current=current, readonly=False)

    def clear(self):
        """
        If any of the protection has triggered will need to clear it. Will
        return a False if everything is good
        """
        self.protection(clear=True, readonly=False)[0]
        problem = self.protection()[0]
        return problem

    def get_voltage_setting(self):
        """
        Returns the power supply setpoint voltage
        """
        return self.control_voltage(voltage=None, readonly=True)

    def get_current_limit(self):
         """
         Returns the power supply current limit
         """
         return self.control_current(current=None, readonly=True)


    def protection(self, uvl=None, ovp=None, ocp=None,ilim=None, clear=None, readonly=True, history=False):
        """
        Adjusts power supply protection settings
        Returns two dictionaries the first with the current protection settings, the second with the fail modes
        Warning - when clearing - return status is 'dont trust anything' - run protection another time
        """
        self.flush_interface(timeout=0.01)
        if readonly == False:
            if clear == 1:
                self.command('OUTPut:PROT:CLEar')
                #self.command('*WAI')
                self.waituntilready()

            if uvl != None:
                self.command('VOLT:LIM:LOW %s' % uvl)
            if ovp != None:
                self.command('VOLT:PROT %s' % ovp)
            if ocp != None:
                self.command('CURR:PROT:STAT %s' % ocp)
                #Note that OCP is not the current limit, only behaviour on current limit (can be 0 or 1)
                #With OCP active current switches to triggered current (by default and not changed by this program so far 0A)
            if ilim != None:
                self.command('CURR %s' % ilim)


        elif uvl != None or ocp != None or ovp != None or ilim != None or clear!=None:  #Readonly is true and we're trying to change things
            raise GPIB.GPIBException('readonly = True - cannot change settings')

        uvlmeas=self.query_float('VOLT:LIM:LOW?')
        ovpmeas= self.query_float('VOLT:PROT?')
        ocpmeas=self.query_float('CURR:PROT:STAT?')
        ilimmeas=self.query_float('CURR?')

        if not(history):  #By default just read the main status register not the register that clears itself after reading
            failmode=int(self.query_float('STAT:QUES:COND?'))
        else: #If you really want the register that clears itself set History=True
            failmode=int(self.query_float('STAT:QUES?'))  #Will spot if previously things went wrong or if currently things are wrong
            print "Not that reliable and it clears itself after!"

        problem = False
        if failmode != 0:
            print 'A power supply problem is present'
            problem = True

        UNR=bool( ( failmode & ( 1 << 10 ) ) >> 10 )  #True if Unregulated output
        if UNR==1:
            UNRMes='Unregulated output'
        else:
            UNRMes='Output is regulated'

        INH=bool( ( failmode & ( 1 << 9 ) ) >> 9 )  #True if output turned off by J1 inhibit signal
        if INH==1:
            INHMes='Inhibt signal on J1 turned off output'
        else:
            INHMes='No Inhibt signal on J1 has been detected'

        OT=bool( ( failmode & ( 1 << 4 ) ) >> 4 ) #True if output turned off by power supply temperature monitor
        if OT==1:
            OTMes='Power supply got too hot and turned off output'
        else:
            OTMes='Power supply temperature okay'

        PF=bool( ( failmode & ( 1 << 2 ) ) >> 2 ) #True if output turned off because AC power failed
        if PF==1:
            PFMes='Input Power faliure and output turned off '
        else:
            PFMes='Input power okay'

        OC=bool( ( failmode & ( 1 << 1 ) ) >> 1 ) #True if output turned off because of Over current
        if OC==1:
            OCMes='OCP triggered, output off '
        else:
            OCMes='OCP did not trigger'

        OV=bool( ( failmode & ( 1 << 0 ) ) >> 0 ) #True if output turned off because of Over voltage
        if OV==1:
            OVMes='Over voltage protection triggered, output turned off '
        else:
            OVMes='No over voltage detected'
        failmode = {'UNR': [UNR,UNRMes], 'INH': [INH, INHMes], 'OT': [OT, OTMes], 'PF': [PF, PFMes], 'OC':[OC, OCMes], 'OV':[OV, OVMes]}


        protectionstatus={'uvl':uvlmeas, 'ovp':ovpmeas, 'ocp':ocpmeas, 'ilim':ilimmeas}
        if clear==1 and problem == 0:
            problem= 'dont trust anything'

        return problem, protectionstatus, failmode


    def pollstatus(self, polltime=0.1, runtime=None):
        """
        Runs the status command every polltime seconds for a time duration of runtime seconds. If runtime = None, runs for an hour
        """
        if runtime==None:
            runtime=60*60 #One hour
        starttime=time.time()
        while time.time()<runtime+starttime:
            time.sleep(polltime)
            print self.status()




if __name__ == "__main__":
    pass
