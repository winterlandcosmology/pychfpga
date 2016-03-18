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
AGILENT34410A = 'Agilent34410A'
AGILENT34980A = 'Agilent34980A'

# The following dictionnary lists the supported DVMs and provides a tuple containing (instrument name, ID string)
SUPPORTED_DVMS = {
    AGILENT34410A: ('Agilent 34410A 6.5 digit multimeter', '34410'),
    AGILENT34980A: ('Agilent 34980A Multifunction Switch/Measure Mainframe and Modules', '34980'),
}

class agilent_dmm(GPIB.GPIB):
    """
    A class to communicate with an Agilent digital multimeter. The class supports:
        - Agilent 34410A 6.5 digit multimeter.
        - Agilent 34980A Multifunction Switch/Measure Mainframe and Modules
    """

    def __init__(self, interface = 'lan', gpib_addr=14, ip_addr='192.168.0.138', ip_port = 5025, timeout=0.5):

        super(agilent_dmm, self).__init__(interface= interface, gpib_addr=gpib_addr, ip_addr=ip_addr, ip_port = ip_port, timeout = timeout)

        if self.is_dummy:
            print 'Using dummy Agilent Multimeter (will always return zero values)'
            return

        self.dvm_model = None
        print 'Initializing instrument'
        self.device_clear()

        id_string=self.query('*IDN?', verbose=0, timeout=1)
        print 'Instrument Identification string:', id_string
        for (instrument_code, (instrument_name, instrument_id_string)) in SUPPORTED_DVMS.items():
            if instrument_id_string in id_string:
                self.dvm_model = instrument_code
                print 'Connected to: %s' % instrument_name

        if self.dvm_model is None:
            raise GPIB.GPIBException('The identification command did not return the expected instrument ID string')

    def set_channel(self, channel_list):
        """
        Sets the multiplexer channel on the Agilent 34980. The command is ignored for other instruments
        """
        if self.dvm_model == AGILENT34980A:
            self.command('ROUT:CLOS (@%s)' % channel_list)

    def get_measure(self, meas_type, channel_list=None):
        """
        Reads the DVM voltage or current using the default MEASure command on the specified channels.
        'meas_type' is either:
            'VOLT:DC'
            'VOLT:AC'
            'CURR:DC'
            'CURR:AC'

        If 'channel_list' is omitted or is None, the currently selected channel is selected.

        'meas_type' can also be an array of strings the same length as channel_list in order to specify a different measurment type for each channel.
        Consecutive measurements of the same type are performed as a single command, so it will be more efficient to order the measurments accordingly.

        Examples:

        get_measure('VOLT:DC')
        get_measure('CURR:AC', ['1001','1002,'1003']) # measures AC current on specified channels
        get_measure(['VOLT:DC', 'VOLT:DC', 'VOLT:AC'], ['1001','1002,'1003']) # measures DC voltages on channels 1001 and 1002, and AC voltage on channel 1003.

        """
        valid_meas_types = ['VOLT:DC', 'VOLT:AC', 'CURR:DC', 'CURR:AC', 'RES'];

        if channel_list is None: # If no channnel is specified, measure the current channel.

            if not isinstance(meas_type, str):
                raise GPIBException('A single measurement type must be specified as a string (not a list).')
            if meas_type not in valid_meas_types:
                raise GPIBException("'%s' is not a valid measurement type. Valid types are: %s" % (meas_type, ', '.join(valid_meas_types)))

            if self.is_dummy:
                return 0
            else:
                v = self.query_float('MEAS:%s?' % meas_type)
                return v # return the value

        # Past this point we do a multi-channel measurement
        if isinstance(channel_list, str):
            channel_list =[channel_list]
        else:
            channel_list = list(channel_list); # make a copy of the list becasue we are going to change it

        print 'Channel list:', channel_list
        if isinstance(meas_type, str):
            meas_type = [meas_type]*len(channel_list)
        else:
            meas_type = list(meas_type)

        if len(meas_type) != len(channel_list):
            raise GPIBException('The number of measurement types must match the number of channels.')

        measured_values = []
        chanel_index = 0
        while channel_list:
            # return ll zeros if we use a dummy instrument
            channel_group = [channel_list.pop(0)] # tahe the first channel of the list
            meas_type_group = meas_type.pop(0)
            if meas_type_group not in valid_meas_types:
                raise GPIBException("'%s' is not a valid measurement type. Valid types are: %s" % (meas_type_group, ', '.join(valid_meas_types)))

            while channel_list and meas_type[0] == meas_type_group: # Keep poping element as long as they are the same measurement type as the first one
                channel_group.append(channel_list.pop(0))
                meas_type.pop(0)

            # Now proceed to read the channels from that group
            if self.is_dummy:
                group_values = [0] * len(channel_group)
            else:
                if self.dvm_model == AGILENT34980A:
                    command_string = 'MEAS:%s? (@%s)' % (meas_type_group, ','.join(channel_group))
                    print 'Measuring multi-channel values from the DMM using command: %s' % command_string
                    reply = self.query(command_string, timeout=2)
                    group_values = [float(x) for x in reply.split(',')] # split the string and convert each element in a float to form a list
                else:
                    group_values = []
                    for channel in channel_group:
                        self.set_channel(channel) # Set the channel in case this is an instrument that supports it (otherwise it will be ignored)
                        command_string = 'MEAS:%s?' % meas_type_group
                        print 'Measuring single-channel values from the DMM using command: %s' % command_string
                        group_values.append(self.query_float(command_string, timeout=1)) # add the measured voltage to the list. Give it an extra timeout time to allow it to change mode
            measured_values+=group_values;
        return measured_values # return the list


    def get_dc_voltage(self, channel_list=None):
        """
        Reads the DVM voltage using the default MEASure command.
        If a channel list is specified, a measure will be taken for each channel and a list will be returned.
        Examples:

        dmm.get_dc_voltage()  # returns the voltage on current switch setting
        dmm.get_dc_voltage(['1001', '1003']) # return the voltages on channels 1001 and 1003.
        """
        return self.get_measure('VOLT:DC', channel_list)

    def get_ac_voltage(self, channel_list=None):
        """
        Reads the DVM AC voltage using the default MEASure command.
        If a channel list is specified, a measure will be taken for each channel and a list will be returned.
        Examples:

        dmm.get_ac_voltage()  # returns the voltage on current switch setting
        dmm.get_ac_voltage(['1001', '1003']) # return the voltages on channels 1001 and 1003.
        """
        return self.get_measure('VOLT:AC', channel_list)


    def get_dc_current(self, channel_list=None):
        """
        Reads the DVM current using the default MEASure command.
        If a channel list is specified, a measure will be taken for each channel and a list will be returned.

        Examples:

        dmm.get_dc_current()  # returns the current on current switch setting
        dmm.get_dc_current(['1001', '1003']) # return the current on channels 1001 and 1003.
        """
        return self.get_measure('CURR:DC', channel_list)

    def get_resistance(self, channel_list=None):
        """
        MEASure:RESistance? [{<range>|AUTO|MIN|MAX|DEF} [,{<resolution>|MIN|MAX|DEF}] ]
        """

        return self.get_measure('RES', channel_list)

    def select_resistance_measurement(self):
        self.command('CONF:RES')

    def select_voltage_measurement(self):
        self.command('CONF:VOLT')


    def beep(self):
        self.command('SYST:BEEP')

    def set_beeper(self, state):
        """
        When True, the multimeter will emit a beep when measurements are stable.
        """
        self.command('SYST:BEEP:STAT %i' % bool(state))

    def local(self):
        if not self.is_gpib:
            raise RuntimeError('the LOCAL command is only supported through the GPIB interface')
        super(agilent_dmm, self).local()
        # self.command('SYST:LOC')  # Not supported
        # self.command('DIAG:LOCAL')  # Works only with VXI

    def display(self, line1=None, line2=None):
        """ Prints a message on the multimeter display.

        An empty string restarts the standard display.
        """

        for line, msg in zip((1,2),(line1,line2)):
            if msg is not None:
                if msg:
                    self.command('DISP:WIND%i:TEXT "%s"' % (line, msg))
                else:
                    self.command('DISP:WIND%i:TEXT:CLE' % (line))
                    self.command('DISP:WIND%i:STAT 1'% (line)) # restart standard display

if __name__ == "__main__":

    # delete previous dmm object if they exist
    try:
        dmm.close()
    except:
        pass # ignore the error if the dmm object does not exist

    print 'Creating DMM object'
    dmm = agilent_dmm()
    print 'Curent DMM voltage is %f V' % dmm.get_dc_voltage()

