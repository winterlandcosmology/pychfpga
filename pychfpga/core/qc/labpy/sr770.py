#!/Library/Frameworks/EPD64.framework/Versions/Current/bin/python
#pylint: disable=C0301
"""
sr770.py module
Implements the control and data acquisition functions for the Stanford Research SR770 FFT signal analyzer 

History:
    2012-12-06 JFC: Created during the last week
"""
import matplotlib.pyplot as plt
#matplotlib.use('Agg')
#import pylab
import numpy as np
#import os.path
import time

import GPIB
#import utils

reload(GPIB)
    
class sr770(GPIB.GPIB):
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
    
    def __init__(self,  interface='usb', gpib_addr=10, ip_addr='192.168.0.37', usb_port='COM4:', timeout=0.5):
        super(self.__class__, self).__init__(interface=interface, gpib_addr=gpib_addr, ip_addr=ip_addr, usb_port=usb_port, timeout=timeout)

        print 'Initializing instrument'
        try:
            print '    Clearing device '
    
            self.device_clear()
            #self.command('HOLD')
            print '    Flushing interface buffers'
            self.flush_interface()
            #print '    Flushing message queue'
            #self.flush_message_queue()
            #print '    Flushing error queue'
            #self.flush_error_queue()
            self.set_gpib_terminator('\n') # Terminate GPIB commands with linefeeds
            self.command('OUTP 1') # Send all replies to the GPIB interface
            #self.command('ESE1\n') # Allow the Operation Complete (OPC) bit to appear on the Status register so we can serial-poll it.
            print 'Status register value: 0x%02X' % self.read_serial_poll()
            print 'Querying the instrument identification (*IDN?)...'
            id_string = self.query('*IDN?')
            print 'Instrument response: ', id_string
            #self.clear_ESR()
            #self.clear_error()
            print 'Status register value: 0x%02X' % self.read_serial_poll()
            print 'Initialization completed'
        except:
            self.close()
            raise
            
    def parse_data(self, data_string):
        """
        Parse ascii data from the SR770 and returns a numpy array 
        """
        value_strings = data_string.strip().split(',')
        out =  np.array([float(value_string) for value_string in value_strings if value_string]) 
        return out

#    def clear_ESR(self):
#        """
#        Clears the Event Status register.
#        """
#        while True:
#            status = self.query_int('ESR?\n', timeout=3)
#            if status:
#                print 'Discarding Event Status register value: 0x%02X' % status
#            else:
#                break
#

#    def read_error(self):
#        error_string = self.query('OUTPERRO\n', timeout = 8)
#        fields = error_string.rstrip().split(',')
#        return (int(fields[0]), fields[1])
#
#    def clear_error(self, print_errors =1):
#        while True:
#            err = self.read_error()
#            if not err[0]:
#                break
#            if print_errors:
#                print 'Error %i: %s' % (err[0], err[1])
#
#    def flush_message_queue(self, timeout=3):
#        # Flush the socket buffer first
#
#        while True: 
#            status = self.read_serial_poll()
#            if status & (1<<self.STATUS_REG_BITS['MESSAGE_IN_QUEUE']):
#                message = self.read(timeout=8)
#                print 'Discarding Message: ', message
#            else:
#                break
#
#    def flush_error_queue(self, timeout=3):
#        # Flush the socket buffer first
#
#        while True:
#            status = self.read_serial_poll()
#            if status & (1<<self.STATUS_REG_BITS['ERROR_IN_QUEUE']):
#                err = self.read_error()
#                print 'Discarding Error %i: %s' % (err[0], err[1])
#            else:
#                break
    def get_spectrum(self, trace_number=0):
        """
        reads the spectrum data on the specified trace number (0 or 1). Returns the data as a numpy float array. The units depend on the instruments settings.
        """
        self.command('SPEC?%i' % trace_number)
        data = self.read()
        return self.parse_data(data)

    def get_span(self):
        """
        Returns the current span, in Hz
        """
        span_code = self.query_int('SPAN?')
        print 'Returned Span code = %i' % span_code
        return 100e3/2**(19-span_code)

    def get_start_frequency(self):
        """
        Returns the current start frequency, in Hz.
        """
        return self.query_float('STRF?')

    def set_start_frequency(self, start_freq):
        """
        Sets the start frequency. The frequency is specified in Hz. 
        The actual frequency that is set is rounted to a multiple of the resolution for the current span (resolution = span/400).
        The actual frequency may be modified whan the span is changed. The span should be set before.
        Setting a new frequency will require the instrument to settle for a time depending on the span.
        """
        self.command('STRF %f' % start_freq)

    def get_center_frequency(self):
        """
        Returns the currently set center frequency (which is a multiple of the current resolution)         
        """
        return self.query_float('CTRF?')

    def set_center_frequency(self, center_freq):
        self.command('CTRF %f' % center_freq)

    def set_span(self, span):
        """
        Sets the span of the instrument, specified in Hz. The SR770 has a fixed numbe rof allowed span, which are 100 kHz / 2**N, where N is an integer.
        The actual span will be the nearest span equal or larger than the requested one.
        Setting a new span will require the instrument to settle for a time depending on the span.
        """
        span_code = 19-int(np.floor(np.log2(100e3/span)))
        if span_code < 0 or span_code > 19:
            raise Exception('Invalid span')
        self.command('SPAN%i' % span_code)

    def get_frequencies(self):
        """
        Return a numpy vector of the 400 frequencies (in Hz) of the data returned by the instrument.  
        """
        start = self.get_start_frequency()
        span = self.get_span()
        resolution = span / 400
        freqs = start + np.arange(400) * resolution
        return freqs

    def get_input_range(self):
        """
        Returns the current Input Range of the instrument.
        """
        return self.query_float('IRNG?')

    def wait_for_opc(self):
        """
        Wait for the last operation to complete. This is done by doing a non-blocking serial poll on the status byte. This requires the OPC bitof the Event Status register (ESR) to be enabled to show on the ESR summary flag in the status register.
        """
        #print 'Waiting for operation to complete',
        while not (self.read_serial_poll(timeout=2) & 0x02):
            print '.',
            time.sleep(0.5)
        print

    def wait_for_average(self):
        """
        Wait for the linear average to complete. This should be executed after the average is turned on AND a new acquisition is started in order to determine when the average has completed.
        """
        print '   Waiting for average to complete',
        while not (self.query_int('FFTS?') & 0x10):
            print '.',
            time.sleep(0.5)
        print

    def clear_settling_flag(self):
        """
        Clears the settling flag. This should be done if the flag is to be used later in order to determine when the instrument has finished settling. Otherwise the flag may be set from a previous operation (by the computer or manually)
        """
        self.query_int('FFTS?7')

    def wait_for_settling_flag(self):
        """
        Wait for the FFT to settle after  a frequency change.
        """
        print '   Waiting for settling',
        while not self.query_int('FFTS?7'):
            print '.',
            time.sleep(0.3)
        print

    def wait_for_scan_complete(self):
        """
        Wait for the scan complete. This is done by doing a non-blocking serial poll on the status byte. This requires the OPC bitof the Event Status register (ESR) to be enabled to show on the ESR summary flag in the status register.
        """
        while not (self.read_serial_poll(timeout=2) & 0x01):
            print '.',
            time.sleep(0.5)
        print

    def clear_error(self):
        print 'Clearing errors ',
        while True:
            err = self.query_int('ERRS?')
            if not err:
                break
            print 'Error byte 0x%2X' % err,
            print


    def command_and_wait(self, *args, **kwargs):
        self.command(*args, **kwargs)
        self.wait_for_opc()

    def meas_spectrum(self, start_freq=None, stop_freq=None, center_freq=None, span=None, average=2):
        #print 'Requested measurement from %f Hz to %f Hz, average = %i' % (start_freq, stop_freq, average)
        #print '   Stopping scan',
        #self.command_and_wait('STOP')
        self.clear_settling_flag()
        need_to_settle = False
        #self.wait_for_opc()
        if span is not None: # Must set span first so we can set the start or center frequency correctly
            self.set_span(span)
            need_to_settle = True
        if stop_freq is not None:
            self.set_span(stop_freq - start_freq)
            need_to_settle = True

        if start_freq is not None:
            self.set_start_frequency(start_freq)
            need_to_settle = True
        if center_freq is not None:
            self.set_center_frequency(center_freq)
            need_to_settle = True

        self.command_and_wait('AVGO0;STRT') # start showing the current acquired data, without averaging 
        self.command('AUTS0') # autoscale, so we can see what is happening on the SR770 screen

        # if we changed a frequency of span, we need to wait for the instrument to settle, otherwise the acquired data will have no meaning
        if need_to_settle:
            self.wait_for_settling_flag()


        print '   Actual measurement made from %f with span %f' % (self.get_start_frequency(), self.get_span())
        print '   Setting averaging',
        self.command_and_wait('STOP;AVGT0;AVGM0;NAVG%i;AVGO1' % average)

        print '   Starting scan',
        self.command_and_wait('STRT')
        print '   Waiting for scan to complete',
        self.wait_for_scan_complete()
        #print '   Waiting for average to complete',
        self.wait_for_average()
        print '   Reading data',
        spec = self.get_spectrum()
        freq = self.get_frequencies()
        print
        return (freq, spec)

    def meas_log_ssb(self, carrier_freq=50e3, min_offset_freq = 1.0, max_offset_freq=50e3, average=2, carrier_power = 0):
        offset_freq = min_offset_freq
        plt.figure(1)
        plt.clf()
        plt.hold(True)
        self.command('MEAS0,1;UNIT0,3') # Measure PSD in dbVrms
        #print 'Autoranging',
        #self.command_and_wait('ARNG1') # Perform a single autorange
        #self.command_and_wait('ARNG0') # Perform a single autorange
        self.clear_error()
        input_range = self.get_input_range()
        print 'Input range is %f dBV' % input_range            

        # Prepare the arrays that will contain the final PSDs
        psd = np.array([])
        freqs = np.array([])
        psd_floor = np.array([])
        
        while True:
            f1 = carrier_freq
            f2 = min(carrier_freq + 5.0 * offset_freq, 100e3)
            print 'Measuring SSB spectrum from %f Hz to %f Hz (+%f Hz), center=%f Hz, span=%s Hz' % (f1, f2, f2 - carrier_freq, carrier_freq, offset_freq)
            #(f,s) = self.meas_spectrum(f1,f2, average=average)
            (f, s) = self.meas_spectrum(center_freq=carrier_freq, span=offset_freq, average=average)
            resolution = f[1] - f[0]
            noise_floor = 10*np.log10( 10**(-165.2 / 10) + 10**((-117.4 + input_range) / 10) / resolution) # compute the noise floor for that resolution and input range.
            print '   Measured from %f Hz (offset %f Hz) to %f Hz (offset +%f Hz) (span %f Hz) ' % (min(f), min(f) - carrier_freq, max(f), max(f) - carrier_freq, max(f) - min(f))
            offset_freq *= 4
            #plt.semilogx(f - carrier_freq, s)
            plt.plot(f - carrier_freq, s)
            plt.grid(1, which='both')
            plt.plot([f[0]- carrier_freq, f[-1] - carrier_freq], [noise_floor, noise_floor])
            plt.draw() 
            if offset_freq >= max_offset_freq*.99:
                break
            # find the index of the points that we want to keep
            if len(freqs):
                ix, = np.where(f-carrier_freq > max(freqs))
            else: # if this is the first set of spectrum that we store
                ix, = np.where(f > carrier_freq+5*resolution)
            freqs = np.hstack((freqs, f[ix] - carrier_freq))
            psd = np.hstack((psd, s[ix] - carrier_power))
            psd_floor = np.hstack((psd_floor , np.ones(len(ix)) * noise_floor- carrier_power))
        plt.figure(2)
        plt.clf()
        plt.hold(1)
        plt.loglog(freqs, 10**(psd/20), 'r-')
        plt.loglog(freqs, 10**(psd_floor/20), 'k-')
        plt.grid(1, which='both')
        plt.xlabel('Offset Frequency (Hz)')
        plt.ylabel('Single_sided noise (1/rt(Hz)')
        self.local()
        print 'Done.'
        return (freqs, psd, psd_floor)
if __name__ == "__main__":
    import sys
    if len(sys.argv)<2:
        filename = raw_input('Input filename, or press [ENTER] to plot only:')
    else:
        filename = sys.argv[1]

 
    if len(sys.argv)<1:
        raw_input('Press [ENTER] to exit:')

  
   
