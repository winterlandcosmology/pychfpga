import numpy as np
import time, sys, os
import numpy as np
import matplotlib.pyplot as plt

from fpga_array import  FPGAArray, setup_logging 

setup_logging()

ca = FPGAArray(iceboards=72, prog=1, open=1)
ib= ca.ib[0]
#print 'Computing & setting ADC delays...'
#for trial in range(5):
#    delay_table, delays_valid,b=ib.compute_adc_delay_offsets()
#    if all(delays_valid.values()):
#        break
#    ib.set_adc_delays(delay_table)
#    print 'Could not compute ADC delays.. Retrying'


print 'Setting up data processing'
ca.set_sync_method('local_soft_trigger')
ca.set_operational_mode('shuffle16', frames_per_packet=2)
ib.start_data_capture(period=0.1, source='adc')

print 'Getting data receiver'
r=ib.get_data_receiver()
ib.sync()


def print_rms(r, channels = range(8), count=1, delay=1.5):
    """ Prints the number of bits exercised on the channels specified in ``channels``. 
    ``count`` values are printed, with a delay of ``delay`` between each lines.
    """
    i = 0
    print ' '.join(' CH%02i ' % ch for ch in channels)
    while not count or i<count:
        i += 1
        try:
            a = r.read_frames()
            print ' '.join((' %5.1f' % np.log2(a[ch].std())) if ch in a else ' ?? ' for ch in channels)
            time.sleep(delay)
            sys.stdout.flush()
            #os.system("cls" if os.name=='nt' else 'clear')
        except KeyboardInterrupt:
            print "Stopped by User"
            break
        #except KeyError:
        #    print "key error, missing data..."
        #    pass    

def pfb_fft(data, N=4):
    """
    N = window size
    """
    # Compute window function
    data = np.array(data)
    frame_length = len(data[0])
    number_of_frames = len(data)
    sinc_window = np.sinc((np.arange(-frame_length * N/2, frame_length * N/2) + 0.5) / frame_length)
    hamming_window = np.hamming(frame_length * N)
    window = np.reshape((sinc_window * hamming_window), (4, -1))

    windowed_data = [np.sum(data[n:n+4, :] * window, axis=0) for n in range(0, number_of_frames-4+1)]
    fft = np.fft.rfft(windowed_data)
    return fft

        
def plot_spectrum(r, channel = 0, average=1):
    plt.figure(1)
    plt.clf()
    data = [r.read_frames()[channel] for _ in range(average)]
    frame_length = len(data[0])
    a=np.mean(np.abs(pfb_fft(data)), axis=0)
    fs=800
    f=fs-np.fft.fftfreq(frame_length,1./fs)
    rr=range(frame_length)
    plt.plot(f[rr], a[rr])
    plt.xlabel('Frequency (MHz)')
    plt.ylabel('Amplitude')
    plt.title('FFT Channel %02i (averaging=%i)' % (channel, average))
    
def set_test_mode(ib):
    ib.set_data_source('funcgen')
    ib.set_fft_bypass(1)
    [ib.set_funcgen_function('ab',a=i*16,b=0, channels=[i]) for i in range(16)]
    ib.set_fft_bypass(1)
    ib.set_gain((1,27))