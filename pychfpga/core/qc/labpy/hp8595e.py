#!/Library/Frameworks/EPD64.framework/Versions/Current/bin/python
import matplotlib.pyplot as pyplot
#matplotlib.use('Agg')
import pylab
import numpy as np
import os.path
import re

import GPIB
import utils

reload(GPIB)
    
class hp8595e(GPIB.GPIB):
    """
    This class provides an interface tp the HP 8589E Spectrum Analyzer. 
    """

    def __init__(self, gpib_addr=18, interface_type='eth', interface_addr='192.168.0.37', timeout=3):
        super(self.__class__, self).__init__(gpib_addr, interface_type, interface_addr, timeout)

        print 'Querying the instrument identification (ID)...'
        #self.write('ID;\n')
        #self.write('++read eoi\n')
        #id_string = self.sock.recv(16384)
        id_string=self.query('ID?\n', verbose=0, timeout=5) # for some reasons the first read takes a long time
        print 'Instrument response string:', id_string
        print 'Initialization completed'

    def parse_data(data):
        """
           Parse data from hp8753d
        """
        info = data.split('\n')[:-1]
        out =  np.array([element.split(',') for element in info]).astype(float) 
        return out

       
    def meas_spectrum(self, start=None, stop=None, numpts=None, timeout=0.5):
        """
        Measures the spectrum from the speactum analyzer. 
        Returns frequency in Hz and amplitude in watts.
        """
#        if numpts is not None:
#           self.command('POIN'+str(numpts))  # set number of points in sweep
#        if start is not None:
#           self.command('STAR'+str(start))   # set start frequency
#        if stop is not None:
#           self.command('STOP'+str(stop))    # set stop frequency

        cf = self.query_float("CF?\n") 
        print 'Center frequency = %f MHz' % (cf/1e6)
        sp = self.query_float("SP?\n") 
        print 'Span = %f MHz' % (sp/1e6)
        rbw = self.query_float("RW?\n") 
        print 'Resolution bandwidth = %f Hz' % rbw
        vid_avg = self.query_float("VAVG?\n") 
        print 'Video Average = %f' % vid_avg
        ref_level = self.query_float("RL?\n") 
        print 'Reference level = %f dBm' % ref_level

        print 'Reading data...'
        self.command("TDF P\n")# Trace data format, Real numbers
        data = self.query("TRA?\n", wait_for_timeout=True, timeout=0.5)# get data from trace A
        data_arr = np.fromstring(data, sep=',')
        data_lin = 10.**(data_arr/10.)/1000.
        npoints = len(data_arr)
        print 'Received %i points' % npoints
        #print data_arr
        
        freqs = np.arange(npoints)*(sp/(npoints-1) + (cf-sp/2.0))

        info = {}
        info['RBW'] = rbw
        info['VID_AVG'] = vid_avg
        info['REF_LEVEL'] = ref_level

        return freqs, data_lin, info

  
  

if __name__ == "__main__":
   import sys
   if len(sys.argv)<2:
       filename = None
   else:
       filename = sys.argv[1]

   sa = hp8595e()
   (f,a, info) = sa.meas_spectrum()
   utils.plot_spectrum(f, a, title='RBW=%0.0f Hz, VID_AVG=%0.0f, Ref level = %0.1 dBm' % (info['RBW'], info['VID_AVG'], info['REF_LEVEL']) )
   sa.close()
 
   if len(sys.argv)<1:
       raw_input('Press [ENTER] to exit:')

  
   
