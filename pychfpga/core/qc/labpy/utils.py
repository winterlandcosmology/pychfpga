import matplotlib.pyplot as pyplot
#matplotlib.use('Agg')
import pylab
import numpy as np
import re



def loadTxtData( fname ):
   '''
      load a hp8753e raw data txt file
   '''
   try:
      fileob = open(fname, 'r')
      data = fileob.readlines()
   except:
      print "Couldn't open data file"
   return data


def mag_in_dB(complex_array):
    """
    Returns the magnitude of complex array in dB.
    """
    return 20*np.log10(np.abs(complex_array))

def phase_in_deg(complex_array):
    """
    Returns the phase of a complex array in degrees.
    """
    return 180/np.pi*np.unwrap(np.angle(complex_array))

def plotNetworkData( frequency, out, fname ):
   '''
      Plot Network data from hp41961a data
   '''
   #l1 = pylab.plot(out[:,1],out[:,2],'r-', label=columns[2])
   l1, = pylab.plot(frequency,out[:,0],'b-', label='REAL')
   pylab.xlabel('Frequency')
   ax2 = pylab.twinx()
   l2, = pylab.plot(frequency,out[:,1],'b:', label='IMAG')
   #pylab.legend()
   pylab.legend((l1,l2),('REAL','IMAG'))
   pylab.savefig(fname+'.pdf')
   pylab.clf()

def plotGammaDataRL(frequency, data, filename=None, title=None, clear=1, plot_phase=1, xscale='lin'):
    '''
       Plot Network data from hp8753d data
    '''
    #l1 = pylab.plot(out[:,1],out[:,2],'r-', label=columns[2])
    if clear:
        pyplot.clf()


    if xscale == 'log':
        plot_fn = 'semilogx'
    elif xscale == 'lin':
        plot_fn = 'plot'
    else:
        raise ValueError("xscale must be either 'lin' or 'log'")

    l1, = getattr(pyplot, plot_fn)(frequency, mag_in_dB(data), 'b-', label='Magnitude (dB)')
    ax1 = pyplot.gca()
    title_string = ''
    if title is not None:
       title_string = title
    if filename is not None:
        if title_string:
            title_string += '\n'
        title_string += 'Filename: %s.pdf' % filename

    pyplot.title(title_string)
    pyplot.xlabel('Frequency (Hz)')
    pyplot.ylabel('Response (dB)')
    pyplot.grid(1, which='both')  # Shows the grid on major and minor ticks
    #ax2 = pyplot.twinx()
    if plot_phase:
        ax2 = pylab.twinx()
        l2, = getattr(ax2, plot_fn)(frequency, phase_in_deg(data), 'b:', label='Phase (deg)')
        ax2.yaxis.tick_right()
        pyplot.ylabel('Response for phase (degrees)')
    #pylab.legend()
    pyplot.grid(0)
    pyplot.rcParams['legend.loc'] ='best'  # Tell matplotlib to place the legend at the best location, out of the plot's way
    if plot_phase:
        pyplot.legend((l1, l2), ('Mag(db)', 'Phase (deg)'))
    else:
        pyplot.legend((l1, ), ('Mag(db)',))

    if filename is not None:
       pyplot.savefig(filename + '.pdf')
 #   pylab.clf()
    # Prepare for the user to plot additional data on top of this plot
    pyplot.sca(ax1) # set current y axis back to magnitude axis in case we want to plot something else on top of this graph
    pyplot.hold(1)
    pyplot.show()


def plot_spectrum(freq, ampl, title=None, freq_units='Hz', ampl_units='dBm', filename = None):
   '''
      Plot spectrum. The frequency vector is in Hz, and the the amplitude vector is in linear units]
   '''
   #l1 = pylab.plot(out[:,1],out[:,2],'r-', label=columns[2])
   if ampl_units=='W':
       y_values=ampl
       y_units = ampl_units
   elif ampl_units=='dBm':
       y_values =  10*np.log10(ampl*1000.)
       y_units = ampl_units

   l1, = pylab.plot(freq, y_values, 'b-', label='amplitude')

   pylab.grid(1)
   pylab.ylabel(y_units)
   pylab.xlabel('Hz')
   if title is not None:
        pylab.title(title)
   if filename is not None:
       pylab.savefig(filename+'.pdf')


def load_ltspice_plot(filename):
    """
    Loads LTSpice plots saved in Magnitude (dB) and Phase (degrees) format (in File->Export).
    Returns a 3 coulumn array:
      Column 0 is frequency
      Column 1 is Magnitude in dB
      Column 2 is Phase in degrees
    """
    try:
      file_object = open(filename, 'r')
      all_lines = file_object.readlines()
    except:
        print "Couldn't open data file"
        return None
    file_object.close()
    regexp = re.compile('([\d.eE+-]+)')
    data = np.zeros((0,3))
    for line in all_lines[1:]: # Skip first header line
        parsed_data = re.findall(regexp, line)
        print ' parsed data:', parsed_data
        if len(parsed_data) == 3:
           values = np.array(parsed_data).astype(float)
           data = np.vstack((data, values))

    return data

def csv_save(filename, data, headers=None):
    """
    Saves a array of vectors into a comma separated file with optional headers
    """

    fileob = open(filename, 'w+')
    for i, datum in enumerate(out):
      fileob.write( '%f, %f , %f \n' % (frequency[i], np.real(datum), np.imag(datum)))
    fileob.close()


def npz2txt(filename):
  dat = np.load(filename)

  freqs = dat['freqs']
  spec = dat['spec']

  fname = filename[:-4] + '.txt'
  with open(fname,'w+') as fileob:
    for i, datum in enumerate(spec):
       fileob.write(str(freqs[i])+ ', ' + str(datum)+ '\n')
