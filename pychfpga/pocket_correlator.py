"""
Module for capturing and writing raw corelated data from a t0.CRS board.

The script can be run within an ipython session.

Make sure you've got the latest firmware that supports corr8 mode with all 8 channelizers!

You can connect to a board with serial number 0016 in ipython by running

    run sifpga_crs_corr_capture.py --serial 0016 --stderr_log_level info --prog 2

The following is directly copied from pychfpga/pychfpga/fpga_firmware/chfpga/ct_engine/ucap.py:

    System Requirements:

    The Ethernet interface must be set to receive Jumbo frames:

            sudo ifconfig eno1 mtu 9000

    The UDP buffers shall be increased to reduce packet loss to a minimum::

        sudo sysctl -w net.core.rmem_max=26214400
        sudo sysctl -w net.core.rmem_default=26214400
        sudo sysctl -w net.ipv4.udp_mem='26214400 26214400 26214400'
        sudo sysctl -w net.ipv4.udp_rmem_min=26214400

    Check udp buffers::

        sysctl -a | grep mem

    Monitor UDP buffer::

        watch -cd -n .5 "cat /proc/net/udp"

Some of the function descriptions have been borrowed from the following scripts:
    pychfpga/pychfpga/fpga_firmware/chfpga/ct_engine/ucap.py
    pychfpga/pychfpga/fpga_firmware/chfpga/chfpga.py

"""

# Import common packages
import numpy as np
import h5py
import time
import datetime
import os
import argparse

# Import custom packages
from pychfpga import fpga_array

class POCKET_CORRELATOR:
    # Include a super parent class?
    # Maybe logging needs to be inherited?

    def __init__(self,
                 hwm,
                 stderr_log_level = 'info',
                 prog = 1,
                 ):

        """
        The POCKET_CORRELATOR class is a wrapper for the FPGAArray class of fpga_array.py designed to optimize data collection 
        with a t0.CRS board. It provides an additional layer of processing to simplify measuremsnts of ADC, FUNCGEN, FFT, SCALER,
        and correlated data. 

        This class was initially designed to function as an automated script for a single-board digital backend 
        for computing and storing visibilities at the Deep Dish Development Array (D3A) at the Dominion Radio Astrophysical 
        Observatory (DRAO), the site of the Canadian Hydrogen Observatory and Radio transient Detector (CHORD).

        Parameters
        ----------
        hwm : (str) The hardware map (hwm) provides a description of the part number and serial number of the part
        to which you'd like to connect. This script is designed for t0.CRS boards, so the part number will always
        be "crs" (unless this script is upgraded in the future.) 

        Ex) hwm = 'crs 0016' will search the network for t0.CRS SN0016.

        stderr_log_level : (str) Specifies the logging level. See pychfpga/pychfpga/fpga_array.py for details.
        Default is 'info'. 'debug' provides the most information, while 'warning' is the most silent.

        prog : (str) Specifies whether to force the FPGA to be programmed. '0' does not program the FPGA.
        '1' programs the FPGA only if it is not already programmed; if it is, the board is not re-programmed.
        '2' forces the FPGA to be programmed, even if it is already programmed.

        Returns
        -------
        None.

        """

        ca = fpga_array.FPGAArray(hwm = hwm,
                                  stderr_log_level = stderr_log_level,
                                  prog = prog,
                                  mode = 'corr8' # Always used corr8 for pocket correlator
                                  )

        # Define useful objects
        self.i = ca.ib[0] # we only need to grab the t0.CRS board
        # self.i.GPIO.HOST_FRAME_READ_RATE = 14
        self.ucap = self.i.UCAP # UCAP module
        self.set_ucap_mode(mode = 0) # by default, capture data from all channels
        self.u_receiver = self.ucap.get_data_receiver() # instantiate a UCAP data receiver

        # Define useful variables
        self.fs = 3200 # MHz - Old firmware: 3000 MHz
        self.M = 2**14 # number of samples per ADC frame
        self.NBINS = int(self.M/2) # number of frequency bins, half the number of ADC samples per frame
        self.f_res = self.fs/self.M # frequency resolution of FFT
        self.f = self.f_res*np.arange(self.NBINS) # an array containing the frequency bins
        self.bin_map = self.make_bin_map() # generate the frequency bin map (see the function description for details) 
        
        for n in range(self.NPOLS):

            # Configure each FFT module
            fft = self.i.chan[n].FFT
            fft.FFT_SHIFT = 0b00000000000000 # ensure that the shift schedule is always set to 0
            fft.PIPELINE_DELAY = fft.MEASURED_PIPELINE_DELAY # set pipeline delay equal to measured delay

            # Configure each SCALER module
            scaler = self.i.chan[n].SCALER
            scaler.USE_OFFSET_BINARY = 0 # don't use offset binary encoding
            scaler.ROUNDING_MODE = 2 # 0: truncate, 1: standard rounding, 2: convergent/banker's rounding
            scaler.FOUR_BITS = 1 # scale to 4 bits
            scaler.SATURATE_ON_MINUS_7 = 1 # this is typically set as default

        # Define a bool to check if the correlator is configured; it can be configured
        # by calling configure_corr()
        self.corr_is_configured = False # we'll configur the correlator later

    
    def set_ucap_mode(self, 
                      mode = 0
                      ):
        """

        Parameters
        ----------
        mode : (int)

        Returns
        -------
        n_frames_per_capture : (int) Number of frames per capture given the UCAP mode currently in use.

        """

        valid_modes = [0, 1, 2, 3]
        if mode not in valid_modes:
            raise ValueError(f'Valid UCAP modes are {valid_modes}, but {mode} was passed.')

        self.ucap.MODE = mode # by default, capture data from all channels

        if mode == 0:
            self.NPOLS = 8
        elif mode == 1:
            self.NPOLS = 4
        elif mode == 2:
            self.NPOLS = 2
        elif mode == 3:
            self.NPOLS = 1

        return self.NPOLS

    def n_frames_per_capture(self):
        """
        Return the number of frames per capture (i.e., a single burst of 16 frames of data) based on the 
        UCAP mode that is currently in use.

        Parameters
        ----------
        None.

        Returns
        -------
        n_frames_per_capture : (int) Number of frames per capture given the UCAP mode currently in use.

        """

        if self.ucap.MODE == 0:
            n_frames_per_capture = 2
        elif self.ucap.MODE == 1:
            n_frames_per_capture = 4
        elif self.ucap.MODE == 2:
            n_frames_per_capture = 8
        elif self.ucap.MODE == 3:
            n_frames_per_capture = 16

        return n_frames_per_capture

    def read_adc_frames(self,
                        n_frames = 1,
                        data_source = 'adc',
                        period = 0.02,
                        split = True,
                        verbose = 0
                        ):
        """
        Read ADC frames using UCAP. By default, UCAP is in mode 0, meaning 2 frames for each ADC channel will be
        captured per burst of 16 frames.

        Parameters
        ----------
        n_frames : (int) Number of frames captured per function call. n_frames must be an even-valued integer, as UCAP only
        receives data in bursts of 16 frames. 

        data_source : (str) The upstream source of data; can be either 'adc' or 'funcgen'.

            If 'adc', then real digitized data from the ADC will pass through the CHAN signal chain.

            If 'funcgen', then FUNCGEN data pass through the CHAN signal chain. This is currently unsupported, and the
            FUNCGEN module must therefore be used manually.

        period : (float) Time (in seconds) between captured bursts. 0.02 is the minimum recommended to not saturate the ethernet link. 

        split : (bool) Whether to include a "frames" axis for the data array, which allows the user to inspect individual frames.

            If True:
                axis 0: ADC channel
                axis 1: frame number
                axis 2: sample number

            If False:
                axis 0: ADC channel
                axis 1: sample number, with all frames flattened

        verbose (int): Verbosity level. 0: no messages, 1: basic messages, 2: detailed messages

        Returns
        -------
        d_adc : (np.ndarray) The ADC data. The 14-bit ADC data is stored as two-byte/16-bit integers, so the data
        needs to be shifted down by 2 bits in order to recover the correct values. This shift is done automatically
        within this function.

        The shape of the array depends upon the choice for the "split" parameter.

            If split is True:
                d_adc.shape = (self.NPOLS, n_frames, self.M)

            If split is False:
                d_adc.shape = (self.NPOLS, n_frames*self.M)

        """

        n_frames_per_capture = self.n_frames_per_capture()

        # Verify that the number of frames requested is divisible by the number of frames per capture:
        if n_frames % n_frames_per_capture != 0:
            raise ValueError(f'{n_frames} frames were requested, but the number of frames captured must be a multiple of {n_frames_per_capture} frames per capture.')

        if n_frames < n_frames_per_capture:
            raise ValueError(f'UCAP is configured in mode {self.ucap.MODE}. The minimum number of frames is therefore {n_frames_per_capture}, but {n_frames} were requested.')

        # Divide the number of captures by the number of frames per capture to
        # get the number of captures needed    
        ncap = int(n_frames/n_frames_per_capture)

        # Verify that 'data_source' has been specified correctly
        valid_data_sources = ['adc', 'funcgen']
        if data_source not in valid_data_sources:
            raise ValueError(f'Data source {data_source} is invalid. Valid data sources are {valid_data_sources}.')

        # Ensure UCAP is configured to receive channelizer data, not correlated data:
        if self.ucap.OUTPUT_SOURCE_SEL != 0:
            self.ucap.OUTPUT_SOURCE_SEL = 0

        if data_source == 'adc':

            # Configure each channelizer
            for n in range(self.NPOLS):
                # Set the data source
                self.i.chan[n].FUNCGEN.set_data_source(data_source)

                # Bypass the FFT
                if not self.i.chan[n].FFT.BYPASS:
                    self.i.chan[n].FFT.BYPASS = 1

                # Bypass the SCALER
                if not self.i.chan[n].SCALER.BYPASS:
                    self.i.chan[n].SCALER.BYPASS = 1
            
            # Start data capture, always setting the source to 'adc'
            self.i.start_data_capture(period = period, 
                                      source = 'adc'
                                      )

        elif data_source == 'funcgen':

            raise NotImplementedError('\'funcgen\' is currently unsupported as a data source. The FUNCGEN module must \
                be configured and used manually.')

        # Capture data!
        t, d_adc, c = self.u_receiver.read_raw_frames(format = '16', 
                                                      ncap = ncap, 
                                                      split = split, 
                                                      verbose = verbose
                                                      )
        
        # Shift to 14 bits
        d_adc = d_adc >> 2 

        # Ignore the t and c variables, and return only the ADC data
        return d_adc

    def make_bin_map(self):
        """
        The FFT module implements a decimation-in-frequency FFT, where the time-domain data
        is properly ordered, and the frequency bins are output in bit-reversed order. 

        In addition, in order for the bits corresponding to the first few frequency bins to
        be allocated for packet header information when the t0.CRS board is in 'chan8' mode (i.e.,
        with a corner-turn engine implemented), an additional level of "scrambling" is added
        to the output of the FFT.

        As a result of these two levels of scrambling, the frequency bins that are captured
        by UCAP are not ordered according to increasing frequency. The make_bin_map() function
        returns an array that maps the scrambled data into the proper, user-friendly order.

        Parameters
        ----------
        None.

        Returns
        -------
        bin_map : (np.ndarray) An array used to index a frequency bin axis to convert it from
        "scrambled" to proper order. Computed with JF Python sorcery.

        """

        f_nat = np.arange(self.NBINS)
        f_rev = (f_nat >> 11) | ((f_nat & 0b11111111111) << 2)
        bin_map = (f_rev & 0b1111111111100) | ((f_rev + np.arange(self.NBINS)) & 0b11)

        return bin_map

    def make_inv_bin_map(self):
        """
        It may be necessary to take a properly-ordered frequency axis and scramble it; for example,
        if generating a simulated FFT spectrum with the FUNCGEN module, it is much easier to generate
        that signal with the frequency axis in the proper order, but it needs to be stored in the 
        FUNCGEN memory banks in scrambled order.

        This function generates an array corresponding to the mapping needed to be applied to a properly-
        ordered frequency axis to scramble it.

        Parameters
        ----------
        None.

        Returns
        -------
        bin_map : (np.ndarray) An array used to index a frequency bin axis to convert it from
        proper to "scrambled" order.

        """

        inv_bin_map = np.argsort(make_bin_map())

        return inv_bin_map

    def read_fft_frames(self, 
                        n_frames = 1,
                        fft_bit_res = '32+32',
                        data_source = 'adc',
                        period = 0.02,
                        split = True,
                        verbose = 0
                        ):
        """
        Read FFT frames using UCAP. By default, UCAP is in mode 0, meaning 2 frames for each ADC channel will be
        captured per burst of 16 frames.

        Parameters
        ----------
        n_frames : (int) Number of frames captured per function call. n_frames must be an even-valued integer, as UCAP only
        receives data in bursts of 16 frames. 

        fft_bit_res : (str) The bit resolution of the captured FFT data. The FFT module generates 32+32-bit values for 4 frequency
        bins on every clock cycle, and the output bus of the FFT module is 128 bits wide. 

            If fft_bit_res is '16+16', 4 frequency bins are captured for a single clock cycle, such that
            128 bits / 4 frequency bins = 32 = 16+16 bits per frequency bin can be captured. The upper 16+16 bits of the
            FFT are captured in this case.

            If fft_bit_res is '32+32', only 2 frequency bins are captured for a single clock cycle, such that
            128 bits / 2 frequency bins = 64 = 32+32 bits per frequency bin (i.e., the full resolution of the FFT 
            will be returned). Therefore, since we cannot capture every frequency bin at full resoltion at the same time, 
            the even and odd bins are captured separately (that is, they come from physically different FFT frames). 

                **As such, the injected signal must be roughly stationary in time, such that we can justify combining even and odd 
                bins from different frames. Caution is therefore warranted in using this mode, though it is preferable to ]
                recover the full FFT resolution.**

        data_source : (str) The upstream source of data; can be either 'adc' or 'funcgen'.

            If 'adc', then real digitized data from the ADC will pass through the CHAN signal chain.

            If 'funcgen', then FUNCGEN data pass through the CHAN signal chain. This is currently unsupported, and the
            FUNCGEN module must therefore be used manually.

        period : (float) Time (in seconds) between captured bursts. 0.02 is the minimum recommended to not saturate the ethernet link. 

        split : (bool) Whether to include a "frames" axis for the data array, which allows the user to inspect individual frames.

            If True:
                axis 0: ADC channel
                axis 1: frame number
                axis 2: sample number

            If False:
                axis 0: ADC channel
                axis 1: sample number, with all frames flattened

        verbose (int): Verbosity level. 0: no messages, 1: basic messages, 2: detailed messages

        Returns
        -------
        d_fft : (np.ndarray) The FFT data. The frequency axis is returned in the proper order.

        The shape of the array depends upon the choice for the "split" parameter.

            If split is True:
                d_fft.shape = (self.NPOLS, n_frames, self.NBINS)

            If split is False:
                d_fft.shape = (self.NPOLS, n_frames*self.NBINS)

        """

        n_frames_per_capture = self.n_frames_per_capture()

        # Verify that the number of frames requested is divisible by the number of frames per capture:
        if n_frames % n_frames_per_capture != 0:
            raise ValueError(f'{n_frames} frames were requested, but the number of frames captured must be a multiple of {n_frames_per_capture} frames per capture.')

        if n_frames < n_frames_per_capture:
            raise ValueError(f'UCAP is configured in mode {self.ucap.MODE}. The minimum number of frames is therefore {n_frames_per_capture}, but {n_frames} were requested.')

        # Divide the number of captures by the number of frames per capture to
        # get the number of captures needed    
        ncap = int(n_frames/n_frames_per_capture)

        # Verify that 'data_source' has been specified correctly
        valid_data_sources = ['adc', 'funcgen']
        if data_source not in valid_data_sources:
            raise ValueError(f'Data source {data_source} is invalid. Valid data sources are {valid_data_sources}.')

        # Ensure UCAP is configured to receive channelizer data, not correlated data:
        if self.ucap.OUTPUT_SOURCE_SEL != 0:
            self.ucap.OUTPUT_SOURCE_SEL = 0

        if data_source == 'adc':

            # Configure each channelizer
            for n in range(self.NPOLS):
                # Set the data source
                self.i.chan[n].FUNCGEN.set_data_source(data_source)

                # Do not bypass the FFT
                if self.i.chan[n].FFT.BYPASS:
                    self.i.chan[n].FFT.BYPASS = 0

                # Bypass the SCALER
                if not self.i.chan[n].SCALER.BYPASS:
                    self.i.chan[n].SCALER.BYPASS = 1

            if fft_bit_res == '16+16':

                # Set the SCALER data_type such that we can recover 
                for m in range(self.NPOLS):
                    self.i.chan[m].SCALER.DATA_TYPE = 0

                # Start data capture (recall that we have bypassed the SCALER, but UCAP still
                # captures data from its output)
                self.i.start_data_capture(period = 0.02, 
                                          source = 'scaler' 
                                          )

                # Capture data!
                t, d_fft, c = self.u_receiver.read_raw_frames(format = fft_bit_res, 
                                                              ncap = n_cap_fft, 
                                                              split = split,
                                                              verbose = verbose
                                                              )

            elif fft_bit_res == '32+32':

                for n in range(ncap):
                    for m in range(self.NPOLS):
                        # Capture even bins
                        self.i.chan[m].SCALER.DATA_TYPE = 2

                    # Start data capture for even bins
                    self.i.start_data_capture(period = period, 
                                              source = 'scaler'
                                              )

                    # Capture data for even bins
                    t, d_fft_even, c = self.u_receiver.read_raw_frames(format = fft_bit_res, 
                                                                       ncap = 1, 
                                                                       split = split,
                                                                       verbose = verbose
                                                                       )

                    for m in range(self.NPOLS):
                        # Capture odd bins
                        self.i.chan[m].SCALER.DATA_TYPE = 3

                    # Start data capture for odd bins
                    self.i.start_data_capture(period = period, 
                                              source = 'scaler'
                                              )

                    # Capture data for odd bins
                    t, d_fft_odd, c = self.u_receiver.read_raw_frames(format = fft_bit_res, 
                                                                      ncap = 1, 
                                                                      split = split,
                                                                      verbose = verbose
                                                                      )

                    d_fft_full = np.zeros((self.NPOLS, n_frames_per_capture, self.NBINS), dtype = np.complex128)
                    d_fft_full[..., ::2] = d_fft_even
                    d_fft_full[..., 1::2] = d_fft_odd

                    if n == 0:
                        # Create d_fft array
                        d_fft = d_fft_full
                    else:
                        # Add d_fft_full to to bottom of axis 1 of d_fft
                        d_fft = np.concatenate((d_fft, d_fft_full), axis = 1)

        elif data_source == 'funcgen':

            raise NotImplementedError('\'funcgen\' is currently unsupported as a data source. The FUNCGEN module must \
                be configured and used manually.')
        
        # Return the FFT data with the frequency bins in their proper order
        return d_fft[..., self.bin_map]

    def set_gains(self, 
                  lin_gain, 
                  log_gain
                  ):
        """
        Description.

        Parameters
        ----------
        the : TYPE
            DESCRIPTION.

        Returns
        -------
        None.

        """

        # if not type(lin_gain) == np.ndarray:
        #     # If not passed as an array, create an array of
        #     # linear gains of constant value
        #     lin_gain = np.repeat(lin_gain, self.NBINS) #, dtype = '>i2')

        #     # From this point forward, lin_gain is an ndarray

        # # Set gains on all channelizers
        # for n in range(self.NPOLS):
        #     self.i.chan[n].SCALER.set_gain_table(lin_gain[n], bank = 0)
        #     self.i.chan[n].SCALER.SHIFT_LEFT = log_gain[n]

        return 
  
    def compute_gains(self,
                      target = 1.5*np.sqrt(2), # default value borrowed from CHIME
                      gain_type = 'raw', 
                      number_of_fft_averages = 250,
                      rms_accuracy_tol = None, # %
                      save_gains = False
                      ):
        """
        Description.

        Parameters
        ----------
        the : TYPE
            DESCRIPTION.

        Returns
        -------
        None.

        """

        # =========================================================
        # New digital gain computation algorithm for bitgrowth FFT.
        # =========================================================

        # START WRITING HERE!

        # =====================================================================================
        # Old digital gain computation algorithm used with fixed-point FFT. Kept for reference.
        # =====================================================================================
        # # Capture ncap FFT frames of data, checking for FFT overflows. If there are any, start the FFT capture again
        # # until we have a clean set of data.
        
        # ctr = 0
        # while True:
        #     print(f'Gain computation trial {ctr}')
        #     ctr += 1

        #     fft_overflows = np.zeros(self.NPOLS)

        #     for n in range(self.NPOLS):
        #         # Pulse FFT overflow reset
        #         self.i.chan[n].FFT.OVERFLOW_RESET = 1
        #         self.i.chan[n].FFT.OVERFLOW_RESET = 0
            
        #     # ==================
        #     # Capture ncap FFT frames of data
        #     fft_data = self.read_fft_frames(period = 0.02,
        #                                     data_source = 'adc',
        #                                     ncap = int(number_of_fft_averages/self.n_frames_per_capture),
        #                                     split = True,
        #                                     verbose = 0)

        #     for n in range(self.NPOLS):
        #         # Check FFT overflow status on each pol, and pulse the reset again.
        #         fft_overflows[n] = self.i.chan[n].FFT.OVERFLOW_COUNT
        #         self.i.chan[n].FFT.OVERFLOW_RESET = 1
        #         self.i.chan[n].FFT.OVERFLOW_RESET = 0

        #     print(f'FFT Overflows: {fft_overflows}')

        #     if all(fft_overflows == 0):
        #         break
        
        # # ==================
        # # Compute FFT RMS. The np.abs just converts the np.complex128 to np.float64, since the magnitude is real-valued
        # fft_rms = np.sqrt(np.mean(np.abs(fft_data*np.conj(fft_data)), axis = 1))

        # # Check the target Scaler RMS
        # if target < 0 or target > np.sqrt(7**2 + 7**2):
        #     raise ValueError(f'Scaler target magnitude {target} is outside of bounds 0 < target < {np.sqrt(7**2 + 7**2)}')
        # target *= 2**31 # target RMS is scaled by 31 bits, since the scaler takes 4 MSBs of 35 bit number

        # # Compute the gain needed to shift the fft_rms to the target
        # gain = target / fft_rms

        # # Treat any possible infinities
        # gain[gain == np.inf] = 2**31 

        # # ==================
        # # Break down the raw gains into linear and log gains, where gain = lin * 2**log

        # # IMPORTANT! let the linear gains absorb some of the bits that would otherwise 
        # # be used in the log gains so that we have better linear gain resolution.
        # # print('log2 gain', np.log2(gain)) # need to make a loop to check best log
        # trim_bits = 12 # need a way to cycle through the number that preserves the largest linear gain

        # # Compute the log gains. Take log2 of the raw gains so that we get an idea of how many bit shifts
        # # we need to do (plus some decimal shifting that will be absorbed in the linear gains). Take its
        # # ceiling, and clip it just in case it's too big or too small. Reduce this value by trim_bits to
        # # obtain the log gain. The rest of the gain will be allocated to the linear gains.
        # log = np.clip((np.ceil(np.log2(np.median(gain, axis = 1)))), 0, 31) - trim_bits
        # print('log', log)

        # # Allocate the remainder of the raw gains to the linear gains. 
        # lin = (gain / 2**np.repeat(log.reshape(self.NPOLS, 1), self.NBINS, axis = 1)) + 0.5 # add 0.5 for rounding

        # # ==================
        # # Now that we have our raw gains, we can apply some additional processing.
        # if gain_type == 'raw':
        #     print('Setting gains to raw')
        #     # self.set_gains(lin, [int(n) for n in log]) # cast log gains to int

        # elif gain_type == 'flat':
        #     print('Setting gains to flat')
        #     lin = np.mean(lin[:, np.where(self.f > 300)[0]], axis = 1) # Determine the mean of the linear gains, ignoring frequencies below 300 MHz.
        #     # self.set_gains(lin_flat, [int(n) for n in log]) # cast log gains to int

        # elif gain_type == 'smooth':

        #     # Functions for fitting polynomials to gain

        #     def make_A(x, order):
        #         A = np.zeros((len(x), order))
        #         for n in range(order):
        #             A[:, n] = x**n
        #         return A

        #     def lstsqr_fit(x, y, sigma, order):
        #         sigma = np.diag(sigma) # assume sigma is the variance, not std dev
        #         sigma_inv = np.linalg.inv(sigma)
        #         A = make_A(x, order)
        #         m = np.linalg.inv(A.T @ sigma_inv @ A) @ A.T @ sigma_inv @ y
        #         return m

        #     def make_poly(x, coeffs):
        #         y = np.zeros(len(x))
        #         for n in range(len(coeffs)):
        #             y += coeffs[n]*x**n
        #         return y

        #     print('Smoothing gains...')

        #     # The following is hardcoded for now.
        #     order = 8
        #     lin_smooth = np.zeros((self.NPOLS, len(self.f)))
        #     for n in range(self.NPOLS):
        #         # Trim first 300 MHz when running the fit
        #         m = lstsqr_fit(x = self.f, # [np.where(self.f > 50)[0]], 
        #                        y = lin[n], # , np.where(self.f > 50)[0]], 
        #                        sigma = np.repeat(10, len(self.f)), # 10 is just a guess
        #                        # sigma = np.repeat(10, len(self.f[np.where(self.f > 50)[0]])), # 10 is just a guess
        #                        order = order) 
        #         lin_smooth[n] = make_poly(x = self.f, coeffs = m)
        #     # lin_smooth = lin_smooth.astype(np.int16) # cast to np.int16

        #     # Set lin = lin_smooth
        #     print('Setting gains to smooth')
        #     lin = lin_smooth

        # # Cast the linear gains to np.int16
        # lin = lin.astype(np.int16)

        # # Anywhere that the linear gain is saturated (> 32767) or negative shall be set to 0
        # lin = np.where((0 < lin) & (lin < 32767), lin, 0)

        # # Set the gains
        # self.set_gains(lin, [int(n) for n in log]) # cast log gains to int

        # # ==================
        # # If rms_accuracy_tol is not None, capture SCALER data and check that they are at or near the target

        # if rms_accuracy_tol is not None:
        #     print('Checking RMS of Scaler output is within the specified tolerance.')
        #     scaler_data = self.read_scaler_frames(data_source = 'adc', ncap = 100)
        #     scaler_data_rms = np.sqrt(abs(np.mean(scaler_data * scaler_data.conj(), axis = 1)))
        #     mean_abs_rms = np.mean(scaler_data_rms, axis = 1)

        #     tol = (rms_accuracy_tol/100) * (target / 2**31) # Calculate 
        #     tol_range = np.array([(target / 2**31) - tol, (target / 2**31) + tol]) # Tolerance range

        #     out_of_tol_channels = []
        #     out_of_tol_mean_rms = []
        #     for n in range(self.NPOLS):
        #         if tol_range.min() < mean_abs_rms[n] < tol_range.max():
        #             out_of_tol_channels.append(n)
        #             out_of_tol_mean_rms.append(mean_abs_rms[n])

        #     if not len(out_of_tol_channels) == 0:
        #         raise ValueError(f'Channels {out_of_tol_channels} are outside the allowed Scaler RMS range of {rms_accuracy_tol}%. \n \
        #                            The allowed range is {tol_range} LSBs RMS. \n \
        #                            The mean values on these channels are {out_of_tol_mean_rms} LSBs RMS.')

        # else:
        #     print('Skipping Scaler tolerance check.')
        #     scaler_data = None
        # ==================================================================
        # End of old digital gain computation algorithm. Kept for reference.
        # ==================================================================

        if save_gains:
            # Save gains, calling save_gains()
            print('Saving gains')
            self.save_gains(lin, log, gain_type)

        return fft_data, fft_rms, gain, lin, log, scaler_data
                      
    def save_gains(self, 
                   lin_gain,
                   log_gain,
                   gain_type):
        """
        Description.

        Parameters
        ----------
        the : TYPE
            DESCRIPTION.

        Returns
        -------
        None.

        """

        # Digital gains
        if not os.path.exists(self.digital_gains_path):
            print(f'Creating digital gains directory at {self.digital_gains_path}')
            os.mkdir(self.digital_gains_path)
        else:
            print(f'Digital gains directory is {self.digital_gains_path}')

        digital_gain_file_name = f'{self.gain_file_path}/gains.hdf5'
        print(f'Creating digital gains file {digital_gain_file_name}')

        # Create the gain hdf5 file
        f = h5py.File(f'{self.gain_file_path}/gains.hdf5', 'w')

        lin_gain_dset = f.create_dataset('lin',
                                         (self.NPOLS, self.NBINS),
                                         data = lin_gain,
                                         dtype = np.int16)

        lin_gain_dset.attrs['gain_type'] = gain_type
        
        log_gain_dset = f.create_dataset('log', 
                                         (self.NPOLS,),
                                         data = log_gain,
                                         dtype = np.int8) # type might be 'int', but small enough that 8 bits is plenty (max log gain is 31)

        f.close()

    def read_post_gain_frames(self):
        """
        POST-GAIN FUNCTION HERE

        The user can choose
                which 16+16 bits are captured by applying a digital gain. The upper 16+16 MSBs of the post-digital gain 
                48+48-bit value will be returned. 
        """

        return

    def read_scaler_frames(self,
                           n_frames = 1,
                           data_source = 'adc',
                           period = 0.02, 
                           split = True,
                           verbose = 0
                           ):
        """
        Read SCALER frames using UCAP. By default, UCAP is in mode 0, meaning 2 frames for each ADC channel will be
        captured per burst of 16 frames.

        SCALER data is returned as 4+4-bit integers.

        Parameters
        ----------
        n_frames : (int) Number of frames captured per function call. n_frames must be an even-valued integer, as UCAP only
        receives data in bursts of 16 frames. 

        data_source : (str) The upstream source of data; can be either 'adc' or 'funcgen'.

            If 'adc', then real digitized data from the ADC will pass through the CHAN signal chain.

            If 'funcgen', then FUNCGEN data pass through the CHAN signal chain. This is currently unsupported, and the
            FUNCGEN module must therefore be used manually.

        period : (float) Time (in seconds) between captured bursts. 0.02 is the minimum recommended to not saturate the ethernet link. 

        split : (bool) Whether to include a "frames" axis for the data array, which allows the user to inspect individual frames.

            If True:
                axis 0: ADC channel
                axis 1: frame number
                axis 2: sample number

            If False:
                axis 0: ADC channel
                axis 1: sample number, with all frames flattened

        verbose (int): Verbosity level. 0: no messages, 1: basic messages, 2: detailed messages

        Returns
        -------
        d_scaler : (np.ndarray) The 4+4-bit SCALER data. The frequency axis is returned in the proper order.

        The shape of the array depends upon the choice for the "split" parameter.

            If split is True:
                d_scaler.shape = (self.NPOLS, n_frames, self.NBINS)

            If split is False:
                d_scaler.shape = (self.NPOLS, n_frames*self.NBINS)

        """

        n_frames_per_capture = self.n_frames_per_capture()

        # Verify that the number of frames requested is divisible by the number of frames per capture:
        if n_frames % n_frames_per_capture != 0:
            raise ValueError(f'{n_frames} frames were requested, but the number of frames captured must be a multiple of {n_frames_per_capture} frames per capture.')

        if n_frames < n_frames_per_capture:
            raise ValueError(f'UCAP is configured in mode {self.ucap.MODE}. The minimum number of frames is therefore {n_frames_per_capture}, but {n_frames} were requested.')

        # Divide the number of captures by the number of frames per capture to
        # get the number of captures needed    
        ncap = int(n_frames/n_frames_per_capture)

        # Verify that 'data_source' has been specified correctly
        valid_data_sources = ['adc', 'funcgen']
        if data_source not in valid_data_sources:
            raise ValueError(f'Data source {data_source} is invalid. Valid data sources are {valid_data_sources}.')

        # Ensure UCAP is configured to receive channelizer data, not correlated data:
        if self.ucap.OUTPUT_SOURCE_SEL != 0:
            self.ucap.OUTPUT_SOURCE_SEL = 0

        if data_source == 'adc':

            # Configure each channelizer
            for n in range(self.NPOLS):
                # Set the data source
                self.i.chan[n].FUNCGEN.set_data_source(data_source)

                # Do not bypass the FFT
                if not self.i.chan[n].FFT.BYPASS:
                    self.i.chan[n].FFT.BYPASS = 0

                # Do not bypass the SCALER, and set the mode so that we read 4+4-bit SCALER data
                if not self.i.chan[n].SCALER.BYPASS:
                    self.i.chan[n].SCALER.BYPASS = 0
                    self.i.chan[n].SCALER.DATA_TYPE = 0 # Send 4-bit scaler values
            
            # Start data capture, always setting the source to 'scaler'
            self.i.start_data_capture(period = period, 
                                      source = 'scaler'
                                      )

        elif data_source == 'funcgen':

            raise NotImplementedError('\'funcgen\' is currently unsupported as a data source. The FUNCGEN module must \
                be configured and used manually.')

        # Capture data!
        t, d_scaler, c = self.u_receiver.read_raw_frames(format = '16+16', 
                                                         ncap = ncap, 
                                                         split = True,
                                                         verbose = verbose)

        # Shift the raw data by 12 bits, as the data is packed into the upper 4+4 bits
        # of a 16+16-bit number 
        d_scaler /= 2**12

        # Return the SCALER data with the frequency bins in the proper order 
        return d_scaler[..., self.bin_map]

    def configure_corr(self,
                       autocorr_only = 0,
                       no_accum = 0,
                       n_firmware_frames = 16384 - 1
                       ):
        """
        Configure the receiver for correlated data.

        Parameters
        ----------
        autocorr_only : (int) Bit to choose between return all correlated products, or only autocorrelations.
        Default is to send all N(N-1)/2 correlated products.

            0: return all products
            1: return autocorrelations only

        no_accum : (int) Bit to choose between accumulating (i.e., summing) correalted SCALER frames, and not
        accumulating. Default is to accumulate.

            0: do accumulate
            1: do not accumulate

        n_firmware_frames : (int) Number of frames to accumulate in firmware. Take care not to set this too 
        low (see minimum), as this will increase the data rate, and could lead to overrun condition. Also take
        care not to set this too high (see maximum), as this may cause increased saturation. The firmware 
        accumulator can add numbers up to 18+18-bit values, at which point they saturate.

        Use the recommended value!

            8192 - 1 : minimum
            16384 - 1: recommended
            32768 - 1: maximum

        Returns
        -------
        None.

        """

        # Configure the parameters of the correlator
        self.corr = self.i.CORR # UCORR object
        self.corr_receiver = self.i.get_corr_receiver() # instantiate a correlator receiver object
        self.corr.AUTOCORR_ONLY = autocorr_only 
        self.corr.NO_ACCUM = no_accum 
        self.NCORR_PROD = int(self.NPOLS*(self.NPOLS + 1) // 2)
        self.corr.INTEGRATION_PERIOD = n_firmware_frames
        
        # Update corr_is_configured flag to True
        self.corr_is_configured = True

    def generate_prod_array(self):
        """
        Generate product array

        Parameters
        ----------
        None.

        Returns
        -------


        """

        prod_idx = np.triu_indices(8)
        prod = np.zeros((len(prod_idx[0]), 2))

        for n in range(prod.shape[0]):
            prod[n] = (prod_idx[0][n], prod_idx[1][n])

        self.prod = prod.astype(np.int16)

        return self.prod

    def read_corr_frames(self,
                         data_source = 'adc',
                         soft_integ_period = 1,
                         number_of_results = 1,
                         filename = None,
                         flush = True,
                         align = True,
                         data_timeout = 0.1,
                         flush_timeout = 0.01,
                         return_format = 'raw',
                         verbose = 0,
                         corr_is_configured = False,
                         autocorr_only = 0,
                         no_accum = 0,
                         n_firmware_frames = 16384 - 1
                         ):
        """
        Description.

        Parameters
        ----------
        the : TYPE
            DESCRIPTION.

        Returns
        -------
        None.

        """

        # NOTE: this does not presume that gains have been computed.

        # Verify that 'data_source' has been specified correctly
        valid_data_sources = ['adc', 'funcgen']
        if data_source not in valid_data_sources:
            raise ValueError(f'Data source {data_source} is invalid. Valid data sources are {valid_data_sources}.')

        # Ensure UCAP is configured to receive correlated data, not channelizer data:
        if self.ucap.OUTPUT_SOURCE_SEL != 1:
            self.ucap.OUTPUT_SOURCE_SEL = 1

        if data_source == 'adc':

            # Configure each channelizer
            for n in range(self.NPOLS):
                # Set the data source
                self.i.chan[n].FUNCGEN.set_data_source(data_source)

                # Do not bypass the FFT
                if not self.i.chan[n].FFT.BYPASS:
                    self.i.chan[n].FFT.BYPASS = 0

                # Do not bypass the SCALER, and set the mode so that we read 4+4-bit SCALER data
                if not self.i.chan[n].SCALER.BYPASS:
                    self.i.chan[n].SCALER.BYPASS = 0
                    self.i.chan[n].SCALER.DATA_TYPE = 0 # Send 4-bit scaler values

        elif data_source == 'funcgen':

            raise NotImplementedError('\'funcgen\' is currently unsupported as a data source. The FUNCGEN module must \
                be configured and used manually.')

        # Configure correlator if has not been done so already
        if not self.corr_is_configured:
            self.configure_corr(autocorr_only = autocorr_only,
                                no_accum = no_accum,
                                n_firmware_frames = n_firmware_frames
                                )

        t1 = time.time() # This is our timestamp
        d, count, sat, = self.corr_receiver.read_corr_frames(soft_integ_period = soft_integ_period,
                                                             number_of_results = number_of_results,
                                                             filename = None,
                                                             flush = flush,
                                                             align = align,
                                                             data_timeout = data_timeout,
                                                             flush_timeout = flush_timeout,
                                                             return_format = return_format,
                                                             verbose = verbose
                                                             )
        t2 = time.time()

        print(f'Took {t2 - t1} s to capture corr data')

        return t1, d[:, self.bin_map, :], count, sat

    # Deprecated function --- keeping it around in case any of the processing ever becomes useful!
    # def optimize_fft_shift(self,
    #                        shift_buffer = 3
    #                        ):
    #     """
    #     Description.

    #     Parameters
    #     ----------
    #     the : TYPE
    #         DESCRIPTION.

    #     Returns
    #     -------
    #     None.

    #     """

    #     # First, set the most aggressive shift schedule.
    #     # <Insert here>

    #     # I found at that if you want to create a list of an N-bit number that flips one bit at a time, there's a funny
    #     # Fibonacci-like pattern that describes the decimal value of the binary shift schedule!
    #     fib = 1
    #     shifts = []
    #     for n in range(14):
    #         shifts.append(0b11111111111111 - fib)
    #         fib += 2**(n + 1)

    #     stage = 0
    #     while True:
    #         print(f'Testing shift schedule', bin(shifts[stage]))
    #         ovfl_pols = []
    #         for n in range(self.NPOLS):
    #             # Check each pol for overflows, and save the pol number if one is detected
    #             if self.i.chan[n].FFT.OVERFLOW_COUNT != 0:
    #                 ovfl_pols.append(n)
    #         if len(ovfl_pols) !=  0 or stage == 13:
    #             print('Detected overflows on channel(s)', ovfl_pols)
    #             # Adjust the shift schedule by shift_buffer stages as soon as we have any overflows, and break
    #             print(f'Adjusting shift schedule by {shift_buffer} stages')
    #             stage = stage - shift_buffer
    #             final_shift = shifts[stage]
    #             for n in range(self.NPOLS):
    #                 print(f'Applying final shift schedule to Pol {n}:', bin(shifts[stage]))
    #                 # Configure each FFT shift
    #                 self.i.chan[n].FFT.FFT_SHIFT = shifts[stage] 
    #             break
    #         else:
    #             # Change the shift schedule by 1 stage
    #             stage += 1
    #             for n in range(self.NPOLS):
    #                 # Configure each FFT shift
    #                 self.i.chan[n].FFT.FFT_SHIFT = shifts[stage]

    #     return final_shift

    def make_time_string(self):
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

    def observe(self, 
                data_path = None, # No default data path
                digital_gains_path = None, 
                n_vis_per_file = 256,
                integration_time = 10,
                autocorr_only = 0, # Get all products by default
                no_accum = 0, # accumulate by default
                n_firmware_frames = 16384 - 1,
                gain_target = 1.5*np.sqrt(2), # borrowed from CHIME
                number_of_fft_averages = 1000,
                gain_type = 'raw',
                capture_adc_bursts = True,
                n_adc_bursts = 5,
                capture_fft_bursts = True,
                n_fft_bursts = 5
                ):\

        """
        A method for computing and storing visibility data. 

        Keep in mind that the default UCAP mode is 0, meaning that bursts of ADC and FFT data have 2 frames per input channel. Therefore, any burst 
        of ADC or FFT data will return double the number of frames requested. 

        Parameters
        ----------
        data_path : (str) The path to the directory to which visibility data will be written. If it doesn't exist, a parent directory will be created according
        to data_path. Within this directory, another directory will be created according to the date and time of the acquisition. HDF5 files will be written within 
        this directory, beginning with 0.hdf5 and incrementing the number by 1 with each new file. In this manner, all data can be stored in a parent directory,
        with specific acquisitions identified by the date and time of the acquisiton.

        digital_gains_path : (str) The path to which digital gains will be stored, or the file from which digital gains will be called. If None, then observe()
        will compute the digital gains with self.compute_gains(), which will be stored in the directory f'{data_path}/digital_gains/{acq_string}_digitalgain' as
        the file 'gains.hdf5'. The acq_string corresponds to the date and time of the acquisition, as with the data itself.

        If digitial_gains_path is specified, observe() will search for a 'gains.hdf5' file within the specified directory. These gains will be opened and set 
        in the Scaler during the observation. 

        n_vis_per_file : (int) The number of visibilities to store per HDF5 file. Increasing this number will increase the size of each HDF5 file. Reducing this
        may be useful to collect more ADC and/or FFT snapshots during the observation, as the ADC and/or FFT snapshots are collected when a new HDF5 file is created. 

        integration_time : (int or float) The integration time for correlated data.

        autocorr_only : (int, 0 or 1 only) Specifies whether or not to return all correlated products or autocorrelations only. If '0', all correlation products 
        are returned. If '1', only autocorrelations are returned. Note that self.prod and the shape of the visibility array are the same in each case.

        no_accum : (int, 0 or 1 only) Specifies whether to accumulate correlated frames or not in the firmware accumulator/integrator. If '0', the accumulation/integration
        of correlated frames is enabled. If '1', accumulation/integration is disabled.

        n_firmware_frames : (int) The number of frames to be accumulated/integrated in the firmware accumulator/integrator. The minimum is 8192 - 1, the maximum is
        32768 - 1, and the default is 16384 - 1. Note that the data rate of correlated through the 1G ethernet link is proportional to this setting. The more firmware frames 
        that are integrated, the slower the data rate --- beware, as this could also lead to the saturation of the firmware accumulator/integrator. Likewise, the 
        fewer firmware frames that are integrated, the data rate will increase, but there is less likelihood of saturation. 

        gain_target : (float) The mean of the absolute value of many Scaler frames after applying digital gains. The target value balances the minimization of 
        quantization noise and saturations of the Scaler. The default is 1.5*sqrt(2), inspired by the CHIME value.

        To check that you're meeting the target, run compute_gains(), and then take many (e.g., 100) Scaler frames, compute their magnitude, and take the
        mean across the frame axis. Compare the mean to the target.

        number_of_fft_averages : (int) The number of FFT frames used to calculate the digital gains. It is recommended to set this to approximately 1000.

        gain_type : (str) Specifies whether any post-processing is applied to the computed digital gains. The default is 'raw', meaning no additional processing
        is done after computing digital gains. See compute_gains() for further details --- note that 'raw' has performed the best out of the current algorithms.

        capture_adc_bursts : (bool) Specifies whether to capture ADC frames at the beginning of each HDF5 file. If True, capture ADC data. If False, don't capture
        ADC data.

        n_adc_bursts : (int) The number of ADC frames to capture for each HDF5 file if capture_adc_bursts = True.

        capture_fft_bursts : (bool) Specifies whether to capture FFT frames at the beginning of each HDF5 file. If True, capture FFT data. If False, don't capture
        FFT data.

        n_fft_bursts : (int) The number of FFT frames to capture for each HDF5 file if capture_fft_bursts = True.

        Returns
        -------
        None.

        """

        # ===========
        # Configure data paths
        time_string = self.make_time_string()
        self.acq_string = f'{time_string}_D3A_rfsoc'
        self.data_path = data_path
        self.vis_dir_name = f'{self.data_path}/{self.acq_string}'

        # Create general data parent directory if it doesn't exist --- this will house both the visibility data and the digital gains
        if not os.path.exists(self.data_path):
            print(f'Creating data directory at {self.data_path}')
            os.mkdir(self.data_path)
        else:
            print(f'Data directory is {self.data_path}')

        # Create acquisition-specific directory for the visibilities
        if not os.path.exists(self.vis_dir_name):
            print(f'Creating visibility directory at {self.vis_dir_name}')
            os.mkdir(self.vis_dir_name)
        else:
            print(f'Visibility directory is {self.vis_dir_name}')

        if digital_gains_path is None:
            self.digital_gains_path = f'{self.data_path}/digital_gains'
            self.gain_file_path = f'{self.digital_gains_path}/{self.acq_string}_digitalgain'

            # Create general digital gains directory if it doesn't exist yet
            if not os.path.exists(self.digital_gains_path):
                print(f'Creating general digital gains directory at {self.digital_gains_path}')
                os.mkdir(self.digital_gains_path)
            else:
                print(f'General digital gains directory is {self.digital_gains_path}')

            # Make acquisition-specific directory for the digital gains
            if not os.path.exists(self.gain_file_path):
                print(f'Creating digital gains directory at {self.gain_file_path}')
                os.mkdir(self.gain_file_path)
            else:
                print(f'Digital gains directory is {self.gain_file_path}')

            # ===========
            # Compute gains.

            print('')
            print('')
            print('===================================')
            print('')
            print('--- C O M P U T E   G A I N S  ---')
            print('')
            self.compute_gains(target = gain_target,
                               gain_type = gain_type, 
                               number_of_fft_averages = number_of_fft_averages, 
                               rms_accuracy_tol = None, # let's just ignore this parameter for now
                               save_gains = True)
            print('Gain computation completed!')
            print('')

        else:
            self.digital_gains_path = digital_gains_path
            print('')
            print('')
            print('===================================')
            print('')
            print('---   L O A D   G A I N S ---')
            print('')
            print(f'Loading digital gains from file {self.digital_gains_path}/gains.hdf5 ---')
            print('')
            digital_gains_file = h5py.File(f'{self.digital_gains_path}/gains.hdf5', 'r')
            lin = digital_gains_file['lin'][:]
            log = digital_gains_file['log'][:]
            self.set_gains(lin, [int(n) for n in log])

        # ===========
        # Configure the channelizer and correlator. This is done after gain
        # computation because the compute gains algorithm changes the
        # configuration.

        print('')
        print('')
        print('===================================')
        print('')
        print('    --- C O N F I G U R E  ---')
        print('')

        print('Configuring firmware')

        # Set up the channelizer. Input data should be coming
        # from the ADC, and nothing should be bypassed.
        self.i.set_channelizer(data_source = 'adc',
                               fft_bypass = 0,
                               scaler_bypass = 0
                               )

        # Configure correlator
        if not self.corr_is_configured:
            self.configure_corr(autocorr_only = autocorr_only,
                                no_accum = no_accum,
                                n_firmware_frames = n_firmware_frames
                                )

        print(self.corr.status()) # print corr configuration status

        # ===========
        # Capture and write data

        print('')
        print('')
        print('===================================')
        print('')
        print('      --- O B S E R V E  ---')
        print('')

        # Run data capture until keyboard interrupt
        file_number = 0 # starting hdf5 file number

        # Calculate how many frames we need for desired  integration time
        s_per_frame = 2**14*(1/(self.fs*1e6)) # samples/frame * seconds/sample. Make sure sampling frequency is in Hz
        set_integration_time = integration_time # s
        n_firmware_frames = self.corr.INTEGRATION_PERIOD + 1
        n_software_frames = int(set_integration_time / s_per_frame / n_firmware_frames)
        actual_integration_time = n_firmware_frames * n_software_frames * s_per_frame
        print(f'Target integration time: {set_integration_time} s')
        print(f'Actual integration time: {actual_integration_time} s')

        # Generate product array
        prod_idx = np.triu_indices(8)
        prod = np.zeros((len(prod_idx[0]), 2))
        for n in range(prod.shape[0]):
            prod[n] = (prod_idx[0][n], prod_idx[1][n])
        prod = prod.astype(np.int16)

        try:
            while True:

                # Create vis file
                file_name = f'{self.vis_dir_name}/{file_number}.hdf5'
                print('')
                print('+++++++++++++++++++++++++++++++++')
                print(f'Creating new data file \'{file_name}\'')
                f = h5py.File(file_name, 'w')

                # Create data sets
                dset_vis    = f.create_dataset('vis', (n_vis_per_file, self.NBINS, self.NCORR_PROD), dtype = np.complex128) # Create vis dataset
                dset_counts = f.create_dataset('counts', (n_vis_per_file, self.NBINS), dtype = np.uint32) # Create counts dataset
                dset_sat    = f.create_dataset('sat', (n_vis_per_file, self.NBINS, self.NCORR_PROD), dtype = np.complex64) # Create vis dataset

                # Assign attributes to vis dataset:
                dset_vis.attrs['n_firmware_frames'] = n_firmware_frames # Number of firmware frames per visibility
                dset_vis.attrs['n_software_frames'] = n_software_frames # Number of software frames per visibility

                shifts = np.zeros(self.NPOLS)
                for n in range(self.NPOLS):
                    shifts[n] = self.i.chan[n].FFT.FFT_SHIFT
                unique_shifts = np.unique(shifts)
                if len(unique_shifts) == 1:
                    dset_vis.attrs['fft_shift_schedule'] = unique_shifts[0] # Final shift schedule, represented as an int
                else:
                    raise ValueError('The FFT shift schedule is different for each channelizer. Please ensure that the shift schedule is the same for each channzelizer!')

                # Make index_map group
                index_map_grp = f.create_group('index_map')
                dset_freq = index_map_grp.create_dataset('freq', (self.NBINS, ), dtype = np.float64)
                dset_prod = index_map_grp.create_dataset('prod', (self.NCORR_PROD, 2), dtype = np.int16)
                dset_time = index_map_grp.create_dataset('time', (n_vis_per_file, ), dtype = np.float64)

                dset_freq[:] = self.f
                dset_prod[:] = prod

                # Make numpy buffers for index_map
                t_buf = np.zeros(n_vis_per_file, dtype = np.float64)
                vis_buf = np.zeros((n_vis_per_file, self.NBINS, self.NCORR_PROD), dtype = np.complex128)
                counts_buf = np.zeros((n_vis_per_file, self.NBINS), dtype = np.uint32)
                sat_buf = np.zeros((n_vis_per_file, self.NBINS, self.NCORR_PROD), dtype = np.complex64)

                # Make statistics group
                stats_grp = f.create_group('stats')
                dset_FRAME_COUNT = stats_grp.create_dataset('frame_count', (n_vis_per_file, self.NPOLS), dtype = int)
                dset_ADC_OVERFLOWS = stats_grp.create_dataset('adc_overflows', (n_vis_per_file, self.NPOLS), dtype = int)
                dset_FFT_OVERFLOWS = stats_grp.create_dataset('fft_overflows', (n_vis_per_file, self.NPOLS), dtype = int)
                dset_SCALER_OVERFLOWS = stats_grp.create_dataset('scaler_overflows', (n_vis_per_file, self.NPOLS), dtype = int)
                dset_SCALER_STATS_READY = stats_grp.create_dataset('scaler_stats_ready', (n_vis_per_file, self.NPOLS), dtype = int)
                dset_CORR_OVERRUN = stats_grp.create_dataset('corr_overrun', (n_vis_per_file, ), dtype = int)

                # Make numpy buffers for statistics
                frame_count_buf = np.zeros((n_vis_per_file, self.NPOLS), dtype = int)
                adc_overflows_buf = np.zeros((n_vis_per_file, self.NPOLS), dtype = int)
                fft_overflows_buf = np.zeros((n_vis_per_file, self.NPOLS), dtype = int)
                scaler_overflows_buf = np.zeros((n_vis_per_file, self.NPOLS), dtype = int)
                scaler_stats_ready_buf = np.zeros((n_vis_per_file, self.NPOLS), dtype = int)
                corr_overrun_buf = np.zeros(n_vis_per_file, dtype = int)

                if capture_adc_bursts:
                    # Capture ADC data. Note: a single capture for all 8 pols in mode 0 is equal to
                    # 2*16384*16*8/8/1e6 = 0.524288 MB of data.

                    # Create data set
                    assert(1==0) # FIXME the self.n_frames_per_capture doesn't work here
                    dset_adc = f.create_dataset('adc', (self.NPOLS, self.n_frames_per_capture*n_adc_bursts, self.M), dtype = np.int16) 

                    # Capture bursts
                    print(f'Capturing {self.n_frames_per_capture*n_adc_bursts} ADC frames...')
                    d_adc = self.read_adc_frames(period = 0.02,
                                                 ncap = n_adc_bursts,
                                                 split = True,
                                                 verbose = 0)

                    # Write to file
                    dset_adc[...] = d_adc

                if capture_fft_bursts:
                    # Capture FFT data. Note: a single capture for all 8 pols in mode 0 is equal to
                    # 2*8192*(16+16)*8/8/1e6 = 0.524288 MB of data.

                    # Create data set
                    dset_fft = f.create_dataset('fft', (self.NPOLS, self.n_frames_per_capture*n_adc_bursts, self.NBINS), dtype = np.complex128) 
                    
                    # Capture bursts
                    print(f'Capturing {self.n_frames_per_capture*n_adc_bursts} FFT frames...')
                    d_fft = self.read_fft_frames(data_source = 'adc',
                                                 period = 0.02,
                                                 ncap = n_fft_bursts,
                                                 split = True,
                                                 verbose = 0)

                    # Write to file
                    dset_fft[...] = d_fft

                for n in range(n_vis_per_file):

                    # This is done automatically on each capture, but just in case...
                    self.ucap.OUTPUT_SOURCE_SEL = 1 # u.OUTPUT_SOURCE_SEL = 1 --> correlator data

                    print('')
                    print('<<<<<<<<<<<<<<<<>>>>>>>>>>>>>>>>>')
                    print(f'Capture {n} for file {file_name}')

                    # Pulse resets
                    for m in range(self.NPOLS):

                        # ADC stats reset
                        self.i.chan[m].FUNCGEN.RESET_STATS = 1
                        self.i.chan[m].FUNCGEN.RESET_STATS = 0

                        # FFT stats reset
                        self.i.chan[m].FFT.OVERFLOW_RESET = 1
                        self.i.chan[m].FFT.OVERFLOW_RESET = 0

                        # Scaler stats reset
                        self.i.chan[m].SCALER.STATS_CAPTURE = 1

                    # Capture data, filling the numpy buffer on each iteration
                    if n == 0:
                        flush = True # Not very pythonic, but flush on first capture
                    else:
                        flush = False # Otherwise, no flush
                        
                    # Capture correlated data (note that many default arguments are used here)
                    t, vis, counts, sat = self.read_corr_frames(soft_integ_period = n_software_frames,
                                                                number_of_results = 1,
                                                                flush = flush,
                                                                corr_is_configured = self.corr_is_configured)

                    t_buf[n] = t
                    vis_buf[n] = vis[0] # vis is a 3D array even if we only request 1 result
                    counts_buf[n] = counts[0] # counts is 2D, but 0 axis has length of just 1
                    sat_buf[n] = sat

                    # Capture statistics after recording correlator data
                    print('Capturing statistics')
                    for m in range(self.NPOLS):

                        # ADC stats
                        adc_overflows_buf[n, m] = self.i.chan[m].FUNCGEN.ADC_OVERFLOW_CTR

                        # FFT stats
                        fft_overflows_buf[n, m] = self.i.chan[m].FFT.OVERFLOW_COUNT

                        # Scaler stats
                        scaler = self.i.chan[m].SCALER
                        if scaler.STATS_READY:
                            scaler_stats_ready_buf[n, m] = scaler.STATS_READY
                            frame_count_buf[n, m] = scaler.STATS_FRAME_COUNT
                            scaler_overflows_buf[n, m] = scaler.STATS_SCALER_OVERFLOWS
                        else:
                            print('Scaler stats not ready')
                        scaler.STATS_CAPTURE = 0 # reset Scaler stats capture

                    # Correlator stats
                    corr_overrun_buf[n] = self.corr.OVERRUN

                    # Print some statistics:
                    print(f'FFT Overflows: {fft_overflows_buf[n]}')
                    print(f'Scaler Overflows: {scaler_overflows_buf[n]}')
                    print(f'Correlator Overflows: {corr_overrun_buf[n]}')

                # Once the loop is complete, write the numpy buffers to disk
                print('')
                print('=================================')
                print(f'Writing {file_name} to disk...')

                # Write data returned by correlator
                dset_time[...] = t_buf
                dset_vis[...] = vis_buf
                dset_counts[...] = counts_buf
                dset_sat[...] = sat_buf

                # Write statistics
                dset_FRAME_COUNT[...] = frame_count_buf
                dset_ADC_OVERFLOWS[...] = adc_overflows_buf
                dset_FFT_OVERFLOWS[...] = fft_overflows_buf
                dset_SCALER_OVERFLOWS[...] = scaler_overflows_buf
                dset_SCALER_STATS_READY[...] = scaler_stats_ready_buf
                dset_CORR_OVERRUN[...] = corr_overrun_buf

                print('Data written!')

                # Close file
                print(f'Closing file {file_name}')
                f.close()

                # Increment file number
                file_number += 1

        except KeyboardInterrupt:
            print('')
            print('')
            print('===================================')
            print('')
            print('Saving the buffer before ending observation...')

            # Write data returned by correlator
            dset_time[...] = t_buf
            dset_vis[...] = vis_buf
            dset_counts[...] = counts_buf
            dset_sat[...] = sat_buf

            # Write statistics
            dset_FRAME_COUNT[...] = frame_count_buf
            dset_ADC_OVERFLOWS[...] = adc_overflows_buf
            dset_FFT_OVERFLOWS[...] = fft_overflows_buf
            dset_SCALER_OVERFLOWS[...] = scaler_overflows_buf
            dset_SCALER_STATS_READY[...] = scaler_stats_ready_buf
            dset_CORR_OVERRUN[...] = corr_overrun_buf

            print('Done!')
            print('')
            print(f'Ending observation {self.acq_string}.')
            print('')

        return


def create_pc(args = None):
    """
    Creates pocket correlator object in the command line using argparse. 

    Returns
    -------
    pc object - see the POCKET_CORRELATOR docstring.

    """
    
    # The description is that which is included in the PC docstring,
    # not including the parameters/returns. Grab parser object:
    parser = argparse.ArgumentParser()
    
    # ====================
    # Add arguments:
        
    parser.add_argument('--serial', type = str, 
                        help = '(str) The serial number of the target hardware. The PC class is designed'
                        'to be used with a single t0.CRS board; therefore, when creating the FPGAArray instance,'
                        'the part \'crs\' is automatically assumed.'
                        )

    parser.add_argument('--stderr_log_level', type = str, 
                        help = '(str) The level of logging printed to the screen when controlling the CRS board. Options are:'
                        '\'error\' (only shows errors)'
                        '\'warning\' (quiet, only shows warnings)'
                        '\'info\' (standard, provides key information)'
                        '\'debug\' (lowest-level, provides highest density of information)'
                        )

    parser.add_argument('--prog', type = int, 
                        help = '(int) The prog argument defines whether to force the FPGA to be reprogrammed when the script is re-run.'
                        'The FPGA will be programmed by default if the CRS board is power cycled. The options for prog are:'
                        '0: do not program the FPGA'
                        '1: do not force the FPGA to be re-programmed (if, for instance, the script is re-run)'
                        '2: force the FPGA to be programmed'
                        )

    args = parser.parse_args() # args is a Namespace

    # # If a config is specified, load it:
    # if args.config is not None:
    #     with open(args.config, 'r') as file:
    #         config_args = yaml.safe_load(file)
    #         parser.set_defaults(**config_args)
            
    #     # Reload arguments to override config file values with command line values
    #     args = parser.parse_args()
        
    # ====================
    # Applying some processing to certain parameters that accept multiple types:
    
    # First change args from Namespace to dict:
    args_dict = vars(args) # vars([object]) -> dictionary

    # Add 'crs' to serial number to create a hwm:
    args_dict['hwm'] = 'crs ' + args_dict['serial']

    args_dict.pop('serial')
    
    # Need to convert some strings to ints explicitly:
    args_dict['prog'] = int(args_dict['prog'])
        
    # ====================
    # Instantiate and return the SOIL object:

    pc = POCKET_CORRELATOR(**args_dict)
    
    return pc

if __name__ == '__main__':
    pc = create_pc()