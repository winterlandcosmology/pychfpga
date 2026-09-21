import numpy as np
import os
import h5py
import datetime
from pychfpga.fpga_array import FPGAArray

# =============================
# Connect to a single CRS board  
hwm = 'crs 0041'
ca = FPGAArray(hwm = hwm,
               stderr_log_level = 'debug',
               prog = 2,
               mode = 'shuffle8'
               )

#print('')
#print('Firmware timestamp:')
#print(await ca.ib[0].get_fpga_firmware_timestamp_async())

# =============================
# Instantiate variables

# Values for channelizer
M = 2**14
NBINS = M//2
NPOLS = 8
f_res = 3200/2**14 # frequency resolution in MHz
f = f_res*np.arange(NBINS)

# Values for 100G link 
first_bin = 300*NBINS//1600 # 1536
last_bin= 1500*NBINS//1600  # Last bin is excluded
number_of_bins = last_bin - first_bin
number_of_packets = 128
bins_per_packet = number_of_bins // number_of_packets

# Define object for a single board
i = ca.ib[0]

# Define UCAP object
ucap = i.UCAP
u_receiver = ucap.get_data_receiver()
# u_receiver = i.get_data_receiver(port_number = 0, verbose = 1)

# Check to make sure we're configured to receive channelizer data, not correlated data:
ucap.OUTPUT_SOURCE_SEL = 0

# Make sure we can get data from all channels:
ucap.MODE = 0 

# Define the bin maps
def make_bin_map():
    f_nat = np.arange(8192)
    f_rev = (f_nat>>11) | ((f_nat & 0b11111111111) << 2)
    frev2 = (f_rev & 0b1111111111100) | ((f_rev + np.arange(8192)) & 0b11)
    return frev2

def make_inv_bin_map():
    return np.argsort(make_bin_map())

bin_map = make_bin_map()
inv_bin_map = make_inv_bin_map()

# =============================
# Read FFT data to compute digital gains
n_fft_frames = 1000
print('Capturing FFT data for digital gain computation')

ctr = 0
while True:

    print('Trial ', ctr)

    # Reset the ADC overrange and overvoltage flags
    i.GPIO.ADC_OR_CLEAR = 2**8 - 1
    i.GPIO.ADC_OR_CLEAR = 0
    i.GPIO.ADC_OV_CLEAR = 2**8 - 1
    i.GPIO.ADC_OV_CLEAR = 0
    
    # Set the channelizer to capture 32+32-bit FFT data
    i.set_channelizer(adc_mode=None,
                        adc_sampling_mode=None,
                        adc_bandwidth=None,
                        adcdaq_mode=None,
                        data_source='adc',
                        function=None,
                        funcgen_left_shift=0,
                        funcgen_right_shift=0,
                        # freq_test_bins=None,  # will be passed into function_kwargs
                        fft_bypass=False,
                        fft_shift=0b00000000000000,
                        scaler_bypass=True,
                        scaler_cap_data_type=4,
                        scaler_bypass_data_type=None,
                        postscaler=None,
                        scaler_eight_bit=None,
                        scaler_rounding_mode=None,
                        symmetric_saturation=None,
                        zero_on_sat=None,
                        prober_user_flags=None,
                        offset_binary_encoding=None,
                        local_sync=True,
                        channels=list(np.arange(NPOLS)),
                        gains = None # 1*2**32
                        )
    
    # Start data capture
    i.start_data_capture(period =  4*2**14*(1/3.2e9)*489,
                        source = 'scaler',
                        mode = ucap.MODE,
                        channels = list(np.arange(NPOLS)),
                        data_type = 'fft_2x',
                        select = True
                        )
    
    # Capture data!
    t, d_fft_32_32_raw, c = u_receiver.read_raw_frames(format = '32+32', 
                                                      ncap = n_fft_frames, # n_cap_fft//pc.n_frames_per_capture(), 
                                                      split = True,
                                                      verbose = 0
                                                      )
    
    # **WARNING!**
    
    # The following line assumes that the `period` as set above ensures 
    # that the first frame of any capture is always even, ensuring that 
    # even bins are always captured first.
    
    # Stitch together even and odd bins
    s = d_fft_32_32_raw.shape
    d_fft = d_fft_32_32_raw.reshape(s[0], -1, 2, s[2]).reshape(s[0], -1, s[2]*2, order = 'F')
    d_fft_mean_psd = np.mean(np.abs(d_fft)**2, axis = 1)
    
    # Check the ADC overrange and overvoltage flags
    adc_or = i.GPIO.ADC_OR
    adc_ov = i.GPIO.ADC_OV
    
    print('ADC overrange:', adc_or)
    print('ADC overvoltage:', adc_ov)
    
    if adc_or != 0 or adc_ov != 0:
        print('The ADC overrange and/or overvoltage flags are high. Recapturing new FFT frames before computing digital gains.')

    else:
        print('No ADC overrange or overvoltage flags detected. Moving on to digital gain computation.')
        break

# =============================
# Compute digital gains

# Estimate the mean magnitude of the FFT data
d_fft_mean_mag = np.mean(np.abs(d_fft), axis = 1)
d_fft_mean_mag.shape

# Compute theoretical gains using the mean magnitude of the FFT data
target = 1.5*np.sqrt(2)
target_full = target*2**44
gain_theory = target_full/d_fft_mean_mag[..., bin_map] # here we need to apply the bin_map since I took FFT manually

# IMPORTANT subtelty: usually we want to keep the digital gains with frequency bins 
# in the proper order, and read_fft_frames() does so automatically. However, we 
# must *write* the gains in the scrambled order! Therefore, we must apply the 
# inverse bin map to scramble the frequency bins such that they are written in 
# the native order in which they are processed in firmware
gain_theory_inv_mapped = gain_theory[..., inv_bin_map]

# Buffers for actual gains as read from FPGA (using the word "actual"
# just to be explicit that these gains are not necessarily equivalent to
# gain_theory)
gain_lin_actual = np.zeros(gain_theory.shape)
gain_log_actual = np.zeros(gain_theory.shape)

# Set the gains, and recover the actual gains as stored in the FPGA
for n in range(NPOLS):

    # Ensure that floating point gains are enabled
    i.chan[n].SCALER.USE_FLOAT_GAINS = 1

    # Set the gains; note that SCALER performs additional processing to split gain_theory into
    # linear and logarithmic components (i.e., mantissa and exponent). We therefore need to read
    # the actual gains in the next step, which are directly read from the FPGA.
    i.chan[n].SCALER.set_gain_table(gain_theory_inv_mapped[n], bank = 0, gain_timestamp = None, log_gain = None)

    # Read the actual gains written in the memory bank of the SCALER module in the FPGA.
    # Apply some processing to recover the value in a fully linear unit.
    gain_actual = (np.array(i.chan[n].SCALER.get_gain_table(bank = 0)) & 0xFFFF).flatten() # Full 16-bit value, meaningless without being processed
    gain_lin_actual[n] = gain_actual & 0x7FF # Lower 11 bits, linear (mantissa) component
    gain_log_actual[n] = (gain_actual >> 11) + i.chan[n].SCALER.get_shift_left(bank = 0) # Upper 5 bits, logarithmic (exponential) component, plus common log component

# Return the data arrays, ensuring that bin maps are applied where needed (i.e., if inverse bin
# maps were applied earlier)
glin = gain_lin_actual[..., bin_map]
glog = gain_log_actual[..., bin_map]
gain = glin*2**glog

# =============================
# Save digital gains

def make_time_string():
    """
    Breaks down a datetime.datetime.now object into
    constituent units and refactors them in a single
    string. Used for creating file names.

    Parameters
    ----------
    None.

    Returns
    -------
    Datetime string in the format

        YEAR MONTH DAY T HOUR MINUTE SECOND Z

    For example:

        11:31:05 AM on May 1, 2023

        2023 05 01 T 11 31 05 Z

        20230501T113105Z

    """

    t = datetime.datetime.now()
    y = t.year
    mth = t.month
    d = t.day
    h = t.hour
    m = t.minute
    s = t.second

    # Hacky, but add a 0 if a given unit is only 1 number
    # to be consistent with ICE file names
    if len(str(mth)) == 1:
        mth = f'0{mth}'

    if len(str(d)) == 1:
        d = f'0{d}'

    if len(str(h)) == 1:
        h = f'0{h}'

    if len(str(m)) == 1:
        m = f'0{m}'

    if len(str(s)) == 1:
        s = f'0{s}'

    return f'{y}{mth}{d}T{h}{m}{s}Z'

# Set the data paths
digital_gains_parent_path = '/home/ih/baseband_gains'
digital_gains_dir_name = f'{make_time_string()}_baseband'

# Create the "parent" path for the digital gains if it does not exist
if not os.path.exists(digital_gains_parent_path):
    print(f'Creating digital gains directory at {digital_gains_parent_path}')
    os.mkdir(digital_gains_parent_path)
else:
    print(f'Digital gains parent directory is {digital_gains_parent_path}')

# Create the sub-path and name of the digital gains file
os.mkdir(f'{digital_gains_parent_path}/{digital_gains_dir_name}')
digital_gain_file_name = f'{digital_gains_parent_path}/{digital_gains_dir_name}/gains.hdf5'
print(f'Creating digital gains file {digital_gain_file_name}')

# Create the gain hdf5 file
file = h5py.File(digital_gain_file_name, 'w')

gain_lin_dset = file.create_dataset('gain_lin',
                                    (NPOLS, NBINS),
                                    data = glin,
                                    dtype = np.uint16
                                    )

if glog.shape == (NPOLS, NBINS):
    # Assume floating point gains have been used

    gain_log_dset = file.create_dataset('gain_log', 
                                        (NPOLS, NBINS),
                                        data = glog,
                                        dtype = np.uint16
                                        ) 

if glog.shape == (NPOLS):
    # Assume constant log gains have been used
    
    gain_log_dset = file.create_dataset('gain_log', 
                                        (NPOLS,),
                                        data = glog,
                                        dtype = np.uint8 # type might be 'int', but small enough that 8 bits is plenty (max log gain is 31)
                                        ) 

# Close the file
file.close()

print(f'Gains have been saved to {digital_gain_file_name}')

# =============================
# Configure CRS board to begin transmitting over 100G link

# Instantiate UCORN
ucorn = i.UCORN

# Put UCORN module into reset mode first
ucorn.TX_RESET = 1

ucorn.RX_WRITE_EN = 1 # allow the buffer to be written with real data from the channelizers
ucorn.TX_TRIG_SEL = 0 # do not do trigger-based sending. send data at the native clock rate instead.
ucorn.TX_TOGGLE_BANK = 1 # bank toggling must be enabled with 1 since we'll be reading and writing at the same time
ucorn.TX_INSERT_TIMESTAMP = 1  # insert the timestamp at the appropriate place in the payload header
ucorn.RX_MASK_LOW_BINS = 1 # prevent F-Engine data to be written in the first bins, as these are used for storing packet headers

real_bins = [first_bin + n + number_of_packets * np.arange(bins_per_packet) for n in range(number_of_packets)]

playlist = [(dict(dest_mac_addr=f"01:23:34:67:89:{n + 1:02X}",
                  dest_ip_addr=f"10.88.0.{n + 1}",
                  dest_ip_port=41001
                  ),
             bin_map[real_bins[n]])
             for n in range(number_of_packets)]

# Set the playlist to send data from that bin
ucorn.set_playlist(playlist = playlist, sacrificial_bins = bin_map[np.arange(128)])

ucorn.TX_RESET = 0
