#!/Library/Frameworks/EPD64.framework/Versions/Current/bin/python
import matplotlib.pyplot as plt
#matplotlib.use('Agg')
import numpy as np
import os.path
import time

import GPIB
import utils
import pyvisa

class KeysightN9923(GPIB.GPIB):
    """A class to communicate with instruments over GPIB using a Prologix
       ethernet-GPIB converter.  """

    STATUS_REG_BITS = {
        'REVERSE_GET': 0,
        'FORWARD_GET': 1,
        'EVENT_STATUS_REG_B': 2,
        'ERROR_IN_QUEUE': 3,
        'MESSAGE_IN_QUEUE': 4,
        'EVENT_STATUS_REG': 5,
        'SRQ': 6,
        'PRESET': 7,
        }

    def __init__(self,  interface='lan', ip_addr='10.3.0.6', gpib_addr=14, timeout=0.5):
        # super(hp8753e, self).__init__(interface=interface, gpib_addr=gpib_addr, ip_addr=ip_addr, timeout=timeout)
        self.ip_addr = ip_addr
        self.rm = pyvisa.ResourceManager()
        self.inst = self.rm.open_resource('TCPIP::%s::INSTR' % self.ip_addr)

        # print 'Initializing instrument'
        # print '    Clearing device '
        # self.device_clear()
        # self.command('HOLD')
        # print '    Flushing socket '
        # self.flush_interface()
        # print '    Flushing message queue'
        # self.flush_message_queue()
        # print '    Flushing error queue'
        # self.flush_error_queue()

        # self.command('ESE1\n') # Allow the Operation Complete (OPC) bit to appear on the Status register so we can serial-poll it.
        # print 'Status register value: 0x%02X' % self.read_serial_poll()
        print 'Querying the instrument identification (IDN?)...'
#        self.write('IDN?\n')
#        self.write('++read eoi\n')
#        id_string = self.sock.recv(16384)
        time.sleep(.5)
        while True:
            id_string=self.query('*IDN?', verbose=0, timeout=5) # for some reasons the first read takes a long time
            print 'Instrument response string:', id_string
            if 'N9923' in id_string:
                break

        # self.clear_ESR()
        # #self.clear_error()
        # print 'Status register value: 0x%02X' % self.read_serial_poll()
        print 'Initialization completed'

    def query(self, cmd, verbose=None, timeout=None):
      return self.inst.query(cmd)

    def command(self, cmd):
      self.inst.write(cmd)

    def write(self, cmd):
      self.inst.write(cmd)

    def read(self):
      return self.inst.read()

    def clear(self):
      return self.inst.clear()

    def parse_data(self, data):
       """
          Parse data from hp8753d
       """
       info = data.split('\n')[:-1]
       out =  np.array([element.split(',') for element in info]).astype(float)
       return out

    # def clear_ESR(self):
    #     """
    #     Clears the Event Status register.
    #     """
    #     while True:
    #         status = self.query_int('ESR?\n', timeout=3)
    #         if status:
    #             print 'Discarding Event Status register value: 0x%02X' % status
    #         else:
    #             break

    # def wait_for_OPC(self):
    #     """
    #     Wait for the last operation to complete using serial poll.

    #     This is done by doing a non- blocking serial poll on the status byte.
    #     This requires the OPC bitof the Event Status register (ESR) to be
    #     enabled to show on the ESR summary flag in the status register.
    #     """
    #     while not (self.read_serial_poll(timeout=2) & (1<<self.STATUS_REG_BITS['EVENT_STATUS_REG'])):
    #         print '.',
    #     print

    # def read_error(self):
    #     error_string = self.query('OUTPERRO\n', timeout = 8)
    #     fields = error_string.rstrip().split(',')
    #     return (int(fields[0]), fields[1])

    # def clear_error(self, print_errors =1):
    #     while True:
    #         err = self.read_error()
    #         if not err[0]:
    #             break
    #         if print_errors:
    #             print 'Error %i: %s' % (err[0], err[1])

    # def flush_message_queue(self, timeout=3):
    #     # Flush the socket buffer first

    #     while True:
    #         status = self.read_serial_poll()
    #         if status & (1<<self.STATUS_REG_BITS['MESSAGE_IN_QUEUE']):
    #             message = self.read(timeout=8)
    #             print 'Discarding Message: ', message
    #         else:
    #             break

    # def flush_error_queue(self, timeout=3):
    #     # Flush the socket buffer first

    #     while True:
    #         status = self.read_serial_poll()
    #         if status & (1<<self.STATUS_REG_BITS['ERROR_IN_QUEUE']):
    #             err = self.read_error()
    #             print 'Discarding Error %i: %s' % (err[0], err[1])
    #         else:
    #             break

    def get_s_params(self, spar_list = ['S11', 'S21', 'S12', 'S22'], start=None, stop=None, numpts=None, timeout=1, plot=False):

        if isinstance(spar_list,str): # make sure the S-parameter list is still a list even if we pass only one s-parameter string
            spar_list = [spar_list]

        # Set variables for ease of change - assumes 'debug is true.
        # If debug is set to false then Spectrum Analyzer preset defaults for
        # start frequency, stop frequency and number of points are utilized.
        # numpts = 21
        # start = 1.28579E9
        # stop = 2.28579E9
        # Open a VISA resource manager pointing to the installation folder for the KeysightVisa libraries.
        #Set Timeout - 10 seconds
        self.inst.timeout = 10000
        # Clear the event status registers and empty the error queue
        # Define Error Check Function
        def Errcheck():
            myError = []
            ErrorList = self.query("SYST:ERR?").split(',')
            Error = ErrorList[0]
            if int(Error) == 0:
                # print ("+0, No Error!")
                pass
            else:
                while int(Error)!=0:
                    print ("Error #: " + ErrorList[0])
                    print ("Error Description: " + ErrorList[1])
                    myError.append(ErrorList[0])
                    myError.append(ErrorList[1])
                    ErrorList = self.query("SYST:ERR?").split(',')
                    Error = ErrorList[0]
                    myError = list(myError)
            return myError
        print "Clearing instrument"
        self.write("*CLS")
        time.sleep(0.5)
        # Query identification string *IDN?
        print self.query("*IDN?")
        Errcheck()
        # Preset the FieldFox and wait for operation complete via the *OPC?, i.e.
        # the operation complete query.
        print "Presetting"
        self.write("SYST:PRES")
        time.sleep(.5)
        Errcheck()
        self.write('*OPC?')
        print "Preset complete, *OPC? returned : " + self.read()
        # Set mag to Spectrum Analyzer and wait for operation complete via the *OPC?, i.e.
        # the operation complete query.
        print "Selecting instrument"
        print "The instrument supports the fillowing modes:", self.query('INST:CAT?')
        self.query('INST:SEL "NA";*OPC?')
        Errcheck()


        if plot:
            plt.figure(1)
            plt.clf()
            plt.title("Keysight FieldFox Spectrum Trace Data via Python - PyVisa- SCPI")
            plt.xlabel("Frequency")
            plt.ylabel("Amplitude (dBm)")


        if numpts is not None:
            self.write("SENS:SWE:POIN " + str(numpts))
        if start is not None:
            self.write("SENS:FREQ:START " + str(start))
        if stop is not None:
            self.write("SENS:FREQ:STOP " + str(stop))
        # Determine, i.e. query, number of points in trace for ASCII transfer - query
        print "Fetching sweep parameters"
        numpts = self.query_int("SENS:SWE:POIN?")
        print "   Number of trace points %i" % numpts
        # Determine, i.e. query, start and stop frequencies, i.e. stimulus begin and end points
        start = self.query_float("SENS:FREQ:START?")
        stop = self.query_float("SENS:FREQ:STOP?")
        print "  start frequency = %f" % start
        print "  stop frequency = %f" % stop
        # Set trigger mode to hold for trigger synchronization
        Errcheck()
        # print "stopping continuous mode"
        self.query("INIT:CONT OFF;*OPC?")
        # Use of Python numpy import to comupte linear step size of stimulus array
        # based on query of the start frequency - stop frequency and number of points.
        # 'Other' analyzers support a SCPI "SENSe:X?" query which will provide the stimulus
        # array as a SCPI query.

        Errcheck()

        freqs = np.linspace(start,stop,numpts)
        all_mags = []
        for meas_type in spar_list:
        # meas_type = spar_list[0]
            # print "Setting measurement type to %s" % meas_type
            self.query('CALC:PAR1:DEF %s;*OPC?' % meas_type)
            Errcheck()

            # print freqs
            # Assert a single trigger and wait for trigger complete via *OPC? output of a 1
            self.query("INIT:IMM;*OPC?")
            # print "Single Trigger complete, *OPC? returned : " + self.read()
            # Query the FieldFox response data
            data = self.query("TRACE1:DATA? FDATA")
            # print data # This is one long comma separated string list of values.
            # Use split to turn long string to an array of values
            mag = np.array([float(x) for x in data.split(",")])
            all_mags.append(10**(mag/10))
            Errcheck()
            # On exit clean a few items up.


            # Now plot the x - y data
            maxResponseVal= max(mag)
            minResponseVal = min(mag)
            #if debug:
            print "Max value = %f" % maxResponseVal + " Min Value = %f" % minResponseVal
            # plt.title ("Keysight FieldFox Spectrum Trace Data via Python - PyVisa- SCPI")
            # plt.xlabel("Frequency")
            # plt.ylabel("Amplitude (dBm)")
            if plot:
                plt.plot(freqs,mag)
                plt.autoscale(True, True, True)
                plt.show(block=False)

        # Return the FieldFox back to free run trigger mode
        self.write("INIT:CONT ON")
        # Send a corrupt SCPI command end of application as a debug test
        if False:
            self.write("INIT:CONT OOOOOOOOOO")
        # Call the ErrCheck function and ensure no errors occurred between start of program
        # (first Errcheck() call and end of program (last Errcheck() call.
        self.clear()
        # self.close()

        return freqs, all_mags
    def set_cw_source(self, freq, power=None):
        """

        The CW signals will be stable for 100s. Make sure your measurement is finished within this time frame.
        """
        # self.command('SOUR:MODE CW') # kick the network analyser in CW mode early
        self.set_power(power)
        self.command('FREQ:SPAN 0') # kick the network analyser in CW mode early
        self.command('FREQ:CENTER {freq}'.format(freq=freq)) # kick the network analyser in CW mode early
        self.command('SWE:TIME 100') # kick the network analyser in CW mode early
        self.query('INIT:CONT 0;*OPC?') # Disable continuous mode
        self.command("INIT:IMM") # trigger sweep. We don't wait for OPC? because we don't want to wait until the end of the sweep.
        # time.sleep(0.3) # make sure the sweep has started

    def set_power(self, power):
        if power is not None:
            self.command('SOUR:POW {power}'.format(power=power)) # 

    # def old_get_s_params(self, spar_list = ['S11', 'S21', 'S12', 'S22'], start=None, stop=None, numpts=None, timeout=1):
    #     """
    #     Performs one or more S parameter measurmeents on the HP8753E vector network analyzer.
    #     Start frequency, stop frequency, number of points will be changed if specified, otherwise they will keep theur current values.
    #     """
    #     if isinstance(spar_list,str): # make sure the S-parameter list is still a list even if we pass only one s-parameter string
    #         spar_list = [spar_list]

    #     self.command('HOLD') # stop sweeping so we can have quicker responses from the instrument
    #     # print 'Flusing error queue:'
    #     # self.clear_error(print_errors=1)

    #     if numpts is not None:
    #         self.command('POIN'+str(numpts))  # set number of points in sweep
    #     if start is not None:
    #         self.command('STAR'+str(start))   # set start frequency
    #     if stop is not None:
    #         self.command('STOP'+str(stop))    # set stop frequency

    #     self.command('LOGFREQ') # Set log frequency range
    #     #self.command('PWRRPMAN')              # manual power range
    #     #self.command('POWR03')                # set power range
    #     self.command('SOUPON') # Source power on

    #     print 'Getting instrument configuration'
    #     numpts = self.query_float('POIN?', timeout = 3)         # read back actual numpts (2^n+1)
    #     start = self.query_float('STAR?')
    #     stop = self.query_float('STOP?')

    #     n_spar = len(spar_list)
    #     #frequency = np.arange(numpts) / float(numpts-1) * (stop-start) + start
    #     print 'Instrument parameters'
    #     print '   Start frequency: %f' % start
    #     print '   Stop frequency: %f' % stop
    #     print '   Number of points: %f' % numpts
    #     print
    #     dat_all = []
    #     for channel in range(n_spar):
    #         print 'Acquiring %s in channel %i' % (spar_list[channel], channel)
    #         print '   Setting up measurement mode %s' % spar_list[channel]
    #         #self.command('CHAN'+str(channel+1))
    #         self.command(spar_list[channel])
    #         self.command('SMIC') # Selects SMITH chart, needed if we want the complex values to be sent
    #         # print '   Emptying event queue'
    #         # Flush status register queue
    #         self.clear_ESR()
    #         #print ' Data in buffer:', self.read(wait_for_timeout=1, timeout=5)
    #         print '   Performing single sweep'
    #         self.command('OPC;SING') # OPC: set Operation complete flag when finished (it will appear in the status register so the serial-poll can see it). SING: Start Single sweep command. A '1' will be issued when operation is completed.
    #         self.wait_for_OPC() # wait for the operation to complete using serial poll. We can't use the OPC? query because the Prologix read command times out for long acquisitions.

    #         print '   Configuring data transfer'
    #         self.command('FORM4')
    #         print 'Status register value: 0x%02X' % self.read_serial_poll()

    #         print '   Reading data'
    #         resstr = self.query( 'OUTPFORM', terminator='timeout', timeout=0.5, verbose = 0)  # Fixme: used to have  wait_for_timeout=True
    #         print '   Received %i bytes' % len(resstr)
    #         print '   Parsing data'
    #         dat = self.parse_data(resstr)
    #         print '   received %i complex values' % len(dat)
    #         complex_array = dat[:,0] + 1.0j*dat[:,1]
    #         dat_all.append(complex_array)
    #     print 'Reading frequency list'
    #     limits = self.query( 'OUTPLIML', terminator='timeout', timeout=0.5) # Get list containing frequency points  (Fixme: used to have wait_for_timeout=True)
    #     limits_array = self.parse_data(limits)
    #     print '   received %i points' % len(limits_array)
    #     frequency = limits_array[:,0]
    #     self.command('CONT') # go back to continuous sweep for the user convenience
    #     self.local() # go back to local control to be polite to the user
    #     return frequency, dat_all

    meas_s_params = get_s_params  # for backwards compatibility

    def plot_s_params(self, frequency, data, filename=None, title=None, clear=1, plot_phase=1, xscale='lin'):
        """ Make a S parameter plot from previously measured data and optionally save it in the specified filename as a PDF file. """
        utils.plotGammaDataRL(frequency=frequency, data=data, filename=filename, title=title, clear=clear, plot_phase=plot_phase)

def doSparam(s_params=['S11', 'S21', 'S12', 'S22'], filename=None):
   """
   Reads the specified S parameters from the HP8753e and plot the data. If a filename is specified, the data and the plot will be saved on disk.
   """

   if isinstance(s_params, str):
       s_params = [s_params]

   pdf_filename = None

   na=hp8753e(gpib_addr=14, interface_addr='192.168.0.37' )
   (frequency, data) = na.meas_s_params(spar_list = s_params)
   na.close()

   for plot_number in range(len(s_params)):
       s_parameter = s_params[plot_number].upper()
       out = data[plot_number]
       print 'Plotting parameter %s' % s_parameter
       if filename:
           basename, ext = os.path.splitext(filename) # separate basename from extension
           pdf_filename = s_parameter + '_' + basename
           data_filename = s_parameter + '_' + basename + '.txt'
           fileob = open(data_filename, 'w+')
           for i, datum in enumerate(out):
              fileob.write( '%f, %f , %f \n' % (frequency[i], np.real(datum), np.imag(datum)))
           fileob.close()
       plt.figure(s_parameter)
       plt.clf()
       utils.plotGammaDataRL(frequency, out, title=s_parameter, filename=pdf_filename)
       plt.show()

if __name__ == "__main__":
   import sys
   if len(sys.argv)<2:
       filename = raw_input('Input filename, or press [ENTER] to plot only:')
   else:
       filename = sys.argv[1]

   doSparam(filename = filename)

   if len(sys.argv)<1:
       raw_input('Press [ENTER] to exit:')



