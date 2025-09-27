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
from pychfpga.network_utils import verify_network_settings

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

        # Extract serial number from hwm for CRS board discovery
        serial_number = None
        if hwm and isinstance(hwm, str):
            # Parse hwm format like 'crs 0016' to extract serial number
            parts = hwm.strip().split()
            if len(parts) >= 2 and parts[0].lower() == 'crs':
                serial_number = parts[1].strip()
        
        # Verify network settings before attempting hardware connection
        print("Checking network settings for UDP data reception...")
        if serial_number:
            print(f"Will attempt to discover CRS board with serial number {serial_number}")
            # Use find_best_interface with serial number to discover the board
            from pychfpga.network_utils import find_best_interface
            interface = find_best_interface(serial_number=serial_number)
            all_ok, errors, fix_commands = verify_network_settings(interface=interface)
        else:
            all_ok, errors, fix_commands = verify_network_settings()
        
        if not all_ok:

            # e_msg = '\n" + "="*60'
            e_msg = '\n' + 'NETWORK CONFIGURATION ERROR'
            # e_msg += '\n" + "="*60'
            e_msg += '\n'
            e_msg += '\n' + 'The following network configuration issues were found:'
            e_msg += '\n'
            for error in errors:
                e_msg += '\n' + f"✗ {error}"
            e_msg += '\n'
            e_msg += '\n' + 'To fix these issues, run the following commands:'
            e_msg += '\n'
            for cmd in fix_commands:
                e_msg += '\n' + f"{cmd}"

            e_msg += '\n'
            e_msg += '\n' + 'These settings are required for high-rate UDP data reception'
            e_msg += '\n' + 'to avoid dropped packets. Please configure your network'
            e_msg += '\n' + 'settings and restart the application.'
            # e_msg += '\n" + "="*60'

            # print(e_msg)

            raise RuntimeError(e_msg)
        else:
            print("✓ Network settings verified - ready for UDP data reception")

        ca = fpga_array.FPGAArray(hwm = hwm,
                                    stderr_log_level = stderr_log_level,
                                    prog = prog,
                                    mode = 'corr8' # Always used corr8 for pocket correlator
                                    )

        # Define useful objects
        self.ca = ca
        self.i = ca.ib[0] # we only need to grab the t0.CRS board object
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
        self.inv_bin_map = self.make_inv_bin_map() # generate the inverse frequency bin map (see the function description for details) 

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
        # by calling configure_corr(). When the board is initialized, the correlator is not configured.
        self.corr_is_configured = False

    
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

        print(f'UCAP mode is set to {mode}')

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
                        n_frames = 2,
                        data_source = 'adc',
                        channels = list(np.arange(8)),
                        period = 0.02,
                        split = True,
                        verbose = 0,
                        **function_kwargs
                        ):
        """
        Read ADC frames using UCAP. By default, UCAP is in mode 0, meaning 2 frames for each ADC channel will be
        captured per burst of 16 frames.

        Parameters
        ----------
        n_frames : (int) Number of frames captured per function call. n_frames must be an even-valued integer, as UCAP only
        receives data in bursts of 16 frames. 

        data_source : (str) A string that sets whether real ADC data or data stored in the FUNCGEN buffer are sent out of the FUNCGEN.

            If 'adc', then real digitized data from the ADC will pass through the CHAN signal chain.

            Otherwise, the user can specify a desired function listed in pychfpga/pychfpga/fpga_firmware/chfpga/f_engine/funcgen.py
            Keyword arguments can be included (see below).

        channels : (list) A list of the input channels (i.e., 0 for ADC1, 1 for ADC2, etc.) for which you'd like to recover data.
        The default is to receive data from all channels [0, .., 7].

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

        function_kwargs : (dict) Arguments for FUNCGEN functions passed to set_channelizer(). Used when data_source is not 'adc'.
        See pychfpga/pychfpga/fpga_firmware/chfpga/f_engine/funcgen.py for functions and their arguments.

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

        # # Verify that 'data_source' has been specified correctly
        # valid_data_sources = ['adc', 'funcgen']
        # if data_source not in valid_data_sources:
        #     raise ValueError(f'Data source {data_source} is invalid. Valid data sources are {valid_data_sources}.')

        # Ensure UCAP is configured to receive channelizer data, not correlated data:
        if self.ucap.OUTPUT_SOURCE_SEL != 0:
            self.ucap.OUTPUT_SOURCE_SEL = 0

        # for n in range(self.NPOLS):

        #     if data_source == 'adc':
        #         # Set the data source
        #         self.i.chan[n].FUNCGEN.set_data_source(data_source)

        #     elif data_source == 'funcgen':
        #         # Don't set the data source, but verify that the function 
        #         # specified is the one that is being used
        #         current_data_source = pc.i.chan[n].FUNCGEN.get_data_source() 
        #         if current_data_source != 'buffer':
        #             raise ValueError(f'Data source {data_source} was specified, but {current_data_source} is currently in use. Verify that the function generator has been configured prior to capturing data.')

        #     # Bypass the FFT
        #     self.i.chan[n].FFT.BYPASS = 1

        #     # Bypass the SCALER
        #     self.i.chan[n].SCALER.BYPASS = 1

        # Configure the channelizers
        self.i.set_channelizer(data_source = data_source,
                               function = None,
                               # funcgen_left_shift=None,
                               # funcgen_right_shift=None,
                               channels = channels,
                               **function_kwargs
                               )
        
        # Start data capture, always setting the source to 'adc' (meaning that the data is
        # always captured at the output of the FUNCGEN module, no matter if it is real ADC 
        # data, or a test pattern written to the buffer of the FUNCGEN)
        # self.i.start_data_capture(period = period, 
        #                           source = 'adc'
        #                           )

        self.i.start_data_capture(period = period, 
                                  source = 'adc',
                                  mode = self.ucap.MODE,
                                  channels = channels,
                                  select = True
                                  )

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

        inv_bin_map = np.argsort(self.make_bin_map())

        return inv_bin_map

    def read_fft_frames(self, 
                        n_frames = 2,
                        fft_bit_res = '32+32',
                        data_source = 'adc',
                        channels = list(np.arange(8)),
                        fft_bypass = False,
                        period = 0.02,
                        split = True,
                        verbose = 0,
                        **function_kwargs
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

        data_source : (str) A string that sets whether real ADC data or data stored in the FUNCGEN buffer are sent out of the FUNCGEN.

            If 'adc', then real digitized data from the ADC will pass through the CHAN signal chain.

            Otherwise, the user can specify a desired function listed in pychfpga/pychfpga/fpga_firmware/chfpga/f_engine/funcgen.py
            Keyword arguments can be included (see below).

        channels : (list) A list of the input channels (i.e., 0 for ADC1, 1 for ADC2, etc.) for which you'd like to recover data.
        The default is to receive data from all channels [0, .., 7].

        fft_bypass : (bool) Whether or not to bypass the FFT. Why include an option to bypass the FFT when you are requesting post-gain frames,
        you might ask? Well, sometimes it is useful to use the FUNCGEN to create a simulated spectrum, in which case you wouldn't want 
        the signal you have set in the FUNCGEN to propagate through the firmware FFT. 
        
            With the fft_bypass option, you have the power to create simulated FFT outputs for signal path validation.

            If True, the FFT is bypassed.

            If False, the FFT is not bypassed. Default.

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

        function_kwargs : (dict) Arguments for FUNCGEN functions passed to set_channelizer(). Used when data_source is not 'adc'.
        See pychfpga/pychfpga/fpga_firmware/chfpga/f_engine/funcgen.py for functions and their arguments.

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

        # # Verify that 'data_source' has been specified correctly
        # valid_data_sources = ['adc', 'funcgen']
        # if data_source not in valid_data_sources:
        #     raise ValueError(f'Data source {data_source} is invalid. Valid data sources are {valid_data_sources}.')

        # Ensure UCAP is configured to receive channelizer data, not correlated data:
        if self.ucap.OUTPUT_SOURCE_SEL != 0:
            self.ucap.OUTPUT_SOURCE_SEL = 0

        # # Configure each channelizer
        # for n in range(self.NPOLS):

        #     if data_source == 'adc':
        #         # Set the data source
        #         self.i.chan[n].FUNCGEN.set_data_source(data_source)

        #     elif data_source == 'funcgen':
        #         # Don't set the data source, but verify that the function 
        #         # specified is the one that is being used
        #         current_data_source = pc.i.chan[n].FUNCGEN.get_data_source() 
        #         if current_data_source != 'buffer':
        #             raise ValueError(f'Data source {data_source} was specified, but {current_data_source} is currently in use. Verify that the function generator has been configured prior to capturing data.')

        #     # Do not bypass the FFT
        #     self.i.chan[n].FFT.BYPASS = 0

        #     # Bypass the SCALER
        #     self.i.chan[n].SCALER.BYPASS = 1

        if fft_bit_res == '16+16':

            # Set the SCALER data_type such that we can recover upper 16+16 MSBs of FFT
            # for m in range(self.NPOLS):
            #     self.i.chan[m].SCALER.CAP_DATA_TYPE = 1 

            # Configure the channelizers
            self.i.set_channelizer(data_source = data_source,
                                   # funcgen_left_shift=0,
                                   # funcgen_right_shift=0,
                                   fft_bypass = fft_bypass,
                                   scaler_bypass = True,
                                   scaler_cap_data_type = 1,
                                   scaler_bypass_data_type = None,
                                   channels = channels,
                                   **function_kwargs
                                   )

            # Start data capture (recall that we have bypassed the SCALER, but UCAP still
            # captures data from its output)
            self.i.start_data_capture(period = period, 
                                      source = 'scaler',
                                      mode = self.ucap.MODE,
                                      channels = channels, 
                                      data_type = 1,
                                      select = True
                                      )

            # Capture data!
            t, d_fft, c = self.u_receiver.read_raw_frames(format = fft_bit_res, 
                                                          ncap = ncap, 
                                                          split = split,
                                                          verbose = verbose
                                                          )

        elif fft_bit_res == '32+32':

            for n in range(ncap):

                # for m in range(self.NPOLS):
                #     # Capture even bins
                #     self.i.chan[m].SCALER.CAP_DATA_TYPE = 4 

                # Configure the channelizers to get even FFT bins
                self.i.set_channelizer(data_source = data_source,
                                       # funcgen_left_shift=0,
                                       # funcgen_right_shift=0,
                                       fft_bypass = fft_bypass,
                                       scaler_bypass = True,
                                       scaler_cap_data_type = 4,
                                       scaler_bypass_data_type = None,
                                       channels = channels,
                                       **function_kwargs
                                       )

                # Start data capture for even bins (recall that we have bypassed the SCALER, 
                # but UCAP still captures data from the output of the SCALER)
                self.i.start_data_capture(period = period, 
                                          source = 'scaler',
                                          mode = self.ucap.MODE,
                                          channels = channels, 
                                          data_type = 4,
                                          select = True
                                          )

                # Capture data for even bins
                t, d_fft_even, c = self.u_receiver.read_raw_frames(format = fft_bit_res, 
                                                                   ncap = 1, 
                                                                   split = split,
                                                                   verbose = verbose
                                                                   )

                # for m in range(self.NPOLS):
                #     # Capture odd bins
                #     self.i.chan[m].SCALER.CAP_DATA_TYPE = 5 

                # Configure the channelizers to get odd FFT bins
                self.i.set_channelizer(data_source = data_source,
                                       # funcgen_left_shift=0,
                                       # funcgen_right_shift=0,
                                       fft_bypass = fft_bypass,
                                       scaler_bypass = True,
                                       scaler_cap_data_type = 5,
                                       scaler_bypass_data_type = None,
                                       channels = channels,
                                       **function_kwargs
                                       )

                # Start data capture for odd bins (recall that we have bypassed the SCALER, 
                # but UCAP still captures data from the output of the SCALER)
                self.i.start_data_capture(period = period, 
                                          source = 'scaler',
                                          mode = self.ucap.MODE,
                                          channels = channels, 
                                          data_type = 5,
                                          select = True
                                          )

                # Capture data for odd bins
                t, d_fft_odd, c = self.u_receiver.read_raw_frames(format = fft_bit_res, 
                                                                  ncap = 1, 
                                                                  split = split,
                                                                  verbose = verbose
                                                                  )

                # Stitch together even and odd bins into a single array
                d_fft_full = np.zeros((self.NPOLS, n_frames_per_capture, self.NBINS), dtype = np.complex128)
                d_fft_full[..., ::2] = d_fft_even
                d_fft_full[..., 1::2] = d_fft_odd

                # Either create the d_fft array (i.e., FFT data) or stack new frames onto the array
                if n == 0:
                    # Create d_fft array
                    d_fft = d_fft_full
                else:
                    # Add d_fft_full to to bottom of axis 1 of d_fft
                    d_fft = np.concatenate((d_fft, d_fft_full), axis = 1)
        
        # Return the FFT data with the frequency bins in their proper order
        return d_fft[..., self.bin_map]

    def compute_gains(self,
                      target = 1.5*np.sqrt(2), # default value borrowed from CHIME
                      n_fft_frames = 1000,
                      data_source = 'adc',
                      channels = list(np.arange(8)),
                      fft_bypass = False,
                      bank = 0,
                      return_scaler_frames = False,
                      save_gains = False,
                      digital_gains_parent_path = None,
                      digital_gains_dir_name = None,
                      **function_kwargs
                      ):
        """
        Compute digital gains used to quantize FFT data into 4+4 bits with the SCALER. When floating point
        digital gains are used, the digital gains correspond to an 11-bit "linear" (i.e., mantissa) component 
        which is directly multiplied with the FFT data, and a 5-bit "logarithmic" (i.e., exponent) component 
        which applies a bit shift, with a maximum value of 2^5 = 32 bits, or equivalently a multiplication by 2^32. 
        The maximum digital gain that can be applied is therefore

            maximum_gain = maximum_gain_lin * maximum_gain_log = 2^11 * 2^32.

        The application of the digital gains results in a "post-gain" value with a bit-depth of 48+48 bits, of which the
        upper 4+4 MSBSs are taken and rounded to perform the 4+4-bit quantization.

        Since we can recover the full resolution of the FFT data, we can compute directly the digital gains
        necessary for optimial quantization in the SCALER, which is defined as a "target" SCALER magnitude. 
        We can express the application of digital gains as

            target_full = gain * |FFT|,

        where target_full = target * 2^44, and |FFT| refers to the magnitude of FFT data, averaged for a 
        specified number of frames to reduce noise. The digital gains are therefore simply

            gain = target_full / |FFT|.

        This function uses the set_gain_table() function of the SCALER module (see scaler.py), which performs 
        additional processing of the digital gains as computed with the equation above prior to writing them
        to the memory banks of the SCALER. Therefore, the "theoretical" gains, since they are computed as 
        Python floating point numbers, are optimally broken up into linear and logarithmic components. As such,
        the "theoretical" gains are not necessarily equivalent to those that are actually applied by the SCALER,
        so the "actual" gains are returned to the user by this function. See

            pychfpga/pychfpga/fpga_firmware/chfpga/f_engine/scaler.py 

        for more information on how the "theoretical" gains are processed.

        Parameters
        ----------
        target : (float) The average magnitude of the 4+4-bit SCALER frames. If target is too low, quantization
        noise will begin to dominate over the signal. If target is too high, both the SCALER and the accumulator 
        of the correlator might saturate. The recommended value is borrowed from that used in the CHIME F-Engine,
        which was determined empirically by Prof. Juan Mena-Parra of UofT. 

        n_fft_frames : (int) The number of FFT frames whose magnitudes are average when computing the digital gains.

        data_source : (str) A string that sets whether real ADC data or data stored in the FUNCGEN buffer are sent out of the FUNCGEN.

            If 'adc', then real digitized data from the ADC will pass through the CHAN signal chain.

            Otherwise, the user can specify a desired function listed in pychfpga/pychfpga/fpga_firmware/chfpga/f_engine/funcgen.py
            Keyword arguments can be included (see below).

        channels : (list) A list of the input channels (i.e., 0 for ADC1, 1 for ADC2, etc.) for which you'd like to recover data.
        The default is to receive data from all channels [0, .., 7].

        fft_bypass : (bool) Whether or not to bypass the FFT. Why include an option to bypass the FFT when you are requesting post-gain frames,
        you might ask? Well, sometimes it is useful to use the FUNCGEN to create a simulated spectrum, in which case you wouldn't want 
        the signal you have set in the FUNCGEN to propagate through the firmware FFT. 
        
            With the fft_bypass option, you have the power to create simulated FFT outputs for signal path validation.

            If True, the FFT is bypassed.

            If False, the FFT is not bypassed. Default.

        bank : (int) The FPGA has enough memory to store two sets of digital gains, each of which are stored in different "banks".
        The digital gains computed in a given call of compute_gains() will be written to the specified bank.
        Possible values are 0 or 1.

        return_scaler_frames : (bool) Whether or not to read SCALER frames and return to the user, which can
        be used to verify if the digital gains have been set properly. By default, the number of frames returned is
        equivalent to n_fft_frames.

        save_gains : (bool) Whether or not to save the digital gains in an HDF5 file, which calls save_gains() if True.
        Set to True, for example, if you are saving correlated data and need to apply the digital gains for offline analysis.

        digital_gains_parent_path : (str) A parent directory containing sub-directories of digital gains. Only used if save_gains is True.

        digital_gains_dir_name : (str) The directory in which the digital gains will be stored. This directory lives within
        the parent digital_gains_path directory. Only used if save_gains is True.

        function_kwargs : (dict) Arguments for FUNCGEN functions passed to set_channelizer(). Used when data_source is not 'adc'.
        See pychfpga/pychfpga/fpga_firmware/chfpga/f_engine/funcgen.py for functions and their arguments.

        Returns
        -------
        d_fft : (np.ndarray) The raw FFT frames used for the digital gain computation. The shape is 
        (NPOLS, n_fft_frames, NBINS).

        d_fft_mean_mag : (np.ndarray) The average magnitude of the FFT frames, used for computing the digital gains.
        The shape is (NPOLS, NBINS).

        gain_lin_actual : (np.ndarray) The linear component of the "actual" digital gains. The shape is (NPOLS, NBINS).

        gain_log_actual : (np.ndarray) The logarithmic component of the "actual" digital gains. The shape is (NPOLS, NBINS).

        d_sclr : (np.ndarray) An array of SCALER frames, returned if return_scaler_frames = True. The SCALER frames 
        are captured after the digital gains have been computed and applied, so these frames can be used to verify 
        whether the digital gains were set properly. The shape is (NPOLS, n_fft_frames, NBINS).

        """

        # =========================================================
        # New digital gain computation algorithm for bit-growth FFT.
        # =========================================================

        # Helper array for reading individual bits of ADC_OR and ADC_OV flags
        bitmask = 2**np.arange(8) # 8-bit bitmask for isolating values of individual bits
        bitshifts = np.arange(0, 8)[::-1] # bitshifts to read values of bits as 1 or 0

        # Capture FFT data, ensuring that there are no ADC overrange 
        # or overvoltage flags raised such that the digital gains
        # are not biased
        NTRIES = 10
        for n in range(NTRIES):
            try:
                print(f'FFT capture trial {n + 1} of {NTRIES}')
                
                # Reset the ADC overrange and overvoltage flags
                self.i.GPIO.ADC_OR_CLEAR = 2**8 - 1
                self.i.GPIO.ADC_OR_CLEAR = 0
                self.i.GPIO.ADC_OV_CLEAR = 2**8 - 1
                self.i.GPIO.ADC_OV_CLEAR = 0
        
                # Capture FFT data
                d_fft = self.read_fft_frames(n_frames = n_fft_frames,
                                             fft_bit_res = '32+32',
                                             data_source = data_source,
                                             fft_bypass = fft_bypass,
                                             period = 0.02,
                                             split = True,
                                             verbose = 0,
                                             **function_kwargs
                                             )
        
                # Check the ADC overrange and overvoltage flags
                adc_or = self.i.GPIO.ADC_OR
                adc_ov = self.i.GPIO.ADC_OV

                # Create arrays to print the overrange/overflow status of individual ADCs,
                # reversing the array so that the ADCs are indexed in ascending order from ADC1 to ADC8
                adc_or_arr = np.array([(adc_or & bitmask[m]) >> m for m in bitshifts])[::-1]
                adc_ov_arr = np.array([(adc_ov & bitmask[m]) >> m for m in bitshifts])[::-1]

                print(f'Read {n_fft_frames} FFT frames')
                if adc_or != 0:
                    print(f'ADC overranges detected:', [f'ADC{n + 1}' for n in range(8) if adc_or_arr[n] != 0])
                if adc_ov != 0:
                    print(f'ADC overvoltages detected:', [f'ADC{n + 1}' for n in range(8) if adc_ov_arr[n] != 0])
                # print(f'ADC Overrange = {bin(adc_or).zfill(8)}')
                # print(f'ADC Overvoltage = {bin(adc_ov).zfill(8)}')
                print('')

                # Break loop if no flags are high
                if adc_or == 0 and adc_ov == 0:
                    print('No ADC overvoltage or overrange flags detected. Moving on to digital gain computation.')
                    break

                # Otherwise, raise an exception
                if n == NTRIES - 1:
                   raise Exception('All FFT capture trials unsuccessful. Please try again!')

            except Exception as e:
                raise Exception(e)

        # Compute theoretical gains using the mean magnitude of the FFT data
        target_full = target*2**44
        d_fft_mean_mag = np.mean(np.abs(d_fft), axis = 1)
        gain_theory = target_full/d_fft_mean_mag

        # IMPORTANT subtelty: usually we want to keep the digital gains with frequency bins 
        # in the proper order, and read_fft_frames() does so automatically. However, we 
        # must *write* the gains in the scrambled order! Therefore, we must apply the 
        # inverse bin map to scramble the frequency bins such that they are written in 
        # the native order in which they are processed in firmware
        gain_theory_inv_mapped = gain_theory[..., self.inv_bin_map]

        # Buffers for actual gains as read from FPGA (using the word "actual"
        # just to be explicit that these gains are not necessarily equivalent to
        # gain_theory)
        gain_lin_actual = np.zeros(gain_theory.shape)
        gain_log_actual = np.zeros(gain_theory.shape)

        # Set the gains, and recover the actual gains as stored in the FPGA
        for n in range(self.NPOLS):

            # Ensure that floating point gains are enabled
            if self.i.chan[n].SCALER.USE_FLOAT_GAINS != 1:
                self.i.chan[n].SCALER.USE_FLOAT_GAINS = 1

            # Set the gains; note that SCALER performs additional processing to split gain_theory into
            # linear and logarithmic components (i.e., mantissa and exponent). We therefore need to read
            # the actual gains in the next step, which are directly read from the FPGA.
            self.i.chan[n].SCALER.set_gain_table(gain_theory_inv_mapped[n], bank = bank, gain_timestamp = None, log_gain = None)

            # Read the actual gains written in the memory bank of the SCALER module in the FPGA.
            # Apply some processing to recover the value in a fully linear unit.
            gain_actual = (np.array(self.i.chan[n].SCALER.get_gain_table(bank = bank)) & 0xFFFF).flatten() # Full 16-bit value, meaningless without being processed
            gain_lin_actual[n] = gain_actual & 0x7FF # Lower 11 bits, linear (mantissa) component
            gain_log_actual[n] = (gain_actual >> 11) + self.i.chan[n].SCALER.SHIFT_LEFT # Upper 5 bits, logarithmic (exponential) component, plus common log component
        
        if return_scaler_frames:
            n_sclr_frames = n_fft_frames # capture the same number of frames
            d_sclr = self.read_scaler_frames(n_frames = n_sclr_frames,
                                             data_source = data_source,
                                             channels = channels,
                                             fft_bypass = fft_bypass,
                                             scaler_bypass = False,
                                             period = 0.02, 
                                             split = True,
                                             verbose = 0,
                                             **function_kwargs
                                             )

        else:
            d_sclr = None

        if save_gains:
            self.save_gains(gain_lin = gain_lin_actual[..., self.bin_map], 
                            gain_log = gain_log_actual[..., self.bin_map],
                            digital_gains_parent_path = digital_gains_parent_path,
                            digital_gains_dir_name = digital_gains_dir_name
                            )

        # Return the data arrays, ensuring that bin maps are applied where needed (i.e., if inverse bin
        # maps were applied earlier)
        return (d_fft, 
                d_fft_mean_mag, 
                gain_lin_actual[..., self.bin_map], 
                gain_log_actual[..., self.bin_map], 
                d_sclr
                )
                      
    def save_gains(self, 
                   gain_lin,
                   gain_log,
                   digital_gains_parent_path = None,
                   digital_gains_dir_name = None
                   ):
        """
        Save the digital gains to an HDF5 file. A parent directory "digital_gains_parent_path" containing all
        digital gains files should be specified, and a daughter directory "digital_gains_dir_name" in which the
        digital gains will be stored should also be specified. The digital_gains_dir_name is a 
        datetime value connected to a specific observation when save_gains() is called with observe().

        **NOTE**: be sure that you are saving the *actual* digital gains, which can be read with the 
        SCALER.get_gain_table() method. It is critical that the *actual* digital gains are applied in 
        offline analysis, not the theoretical gains! See compute_gains() for details.

        Parameters
        ----------
        gain_lin : (np.ndarray) The linear component of the "actual" digital gains. The shape is (NPOLS, NBINS).

        gain_log : (np.ndarray) The logarithmic component of the "actual" digital gains. The shape is (NPOLS, NBINS). gain_log should include the "common" log
        gain, which can be read using self.i.chan[n].SCALER.SHIFT_LEFT.

        digital_gains_parent_path : (str) A parent directory containing sub-directories of digital gains. 

        digital_gains_dir_name : (str) The directory in which the digital gains will be stored. This directory lives within
        the parent digital_gains_path directory.

        Returns
        -------
        None.

        """

        # Create the "parent" path for the digital gains if it does not exist
        if not os.path.exists(digital_gains_parent_path):
            print(f'Creating digital gains directory at {digital_gains_parent_path}')
            os.mkdir(digital_gains_parent_path)
        else:
            print(f'Digital gains parent directory is {digital_gains_parent_path}')

        # Create the sub-path and name of the digital gains file
        os.mkdir(digital_gains_dir_name)
        digital_gain_file_name = f'{digital_gains_dir_name}/gains.hdf5'
        print(f'Creating digital gains file {digital_gain_file_name}')

        # Create the gain hdf5 file
        file = h5py.File(digital_gain_file_name, 'w')

        gain_lin_dset = file.create_dataset('gain_lin',
                                            (self.NPOLS, self.NBINS),
                                            data = gain_lin,
                                            dtype = np.uint16
                                            )

        if gain_log.shape == (self.NPOLS, self.NBINS):
            # Assume floating point gains have been used

            gain_log_dset = file.create_dataset('gain_log', 
                                                (self.NPOLS, self.NBINS),
                                                data = gain_log,
                                                dtype = np.uint16
                                                ) 

        if gain_log.shape == (self.NPOLS):
            # Assume constant log gains have been used
            
            gain_log_dset = file.create_dataset('gain_log', 
                                                (self.NPOLS,),
                                                data = gain_log,
                                                dtype = np.uint8 # type might be 'int', but small enough that 8 bits is plenty (max log gain is 31)
                                                ) 

        # Close the file
        file.close()

        print(f'Gains have been saved to {digital_gain_file_name}')

    def read_post_gain_frames(self,
                              n_frames = 2,
                              data_source = 'adc',
                              channels = list(np.arange(8)),
                              fft_bypass = False,
                              period = 0.02,
                              split = True,
                              verbose = 0,
                              **function_kwargs
                              ):
        """
        Read "post-gain" FFT frames using UCAP. "Post-gain" FFT frames refer to the intermediate 48+48-bit value resulting from the 
        multiplication of FFT data by digital gains prior to 4+4-bit quantization in the SCALER. read_post_gain_frames() returns to the
        user to recover the upper 16+16 bits of the post-gain FFT frames.

        Therefore, this function allows the user to use the digital gains to recover 16+16 bits of FFT data, tuned to the bits
        that the user wishes to recover. This method is advantageous compared to reading the upper 16+16 bits of FFT data with read_fft_frames(), 
        as the signal may not necessarily be contained within those bits of the FFT. read_post_gain_frames() enables user-tunability of
        the FFT data so that the bits containing the signal can be recovered. Of course, the 32+32-bit mode of read_fft_frames() enables
        the capture of full-resolution FFT data, but the even and odd bins must be read separately, while this method enables all bins
        to be captured at the same time. 

        By default, UCAP is in mode 0, meaning 2 frames for each ADC channel will be captured per burst of 16 frames.

        Parameters
        ----------
        n_frames : (int) Number of frames captured per function call. n_frames must be an even-valued integer, as UCAP only
        receives data in bursts of 16 frames. 

        data_source : (str) A string that sets whether real ADC data or data stored in the FUNCGEN buffer are sent out of the FUNCGEN.

            If 'adc', then real digitized data from the ADC will pass through the CHAN signal chain.

            Otherwise, the user can specify a desired function listed in pychfpga/pychfpga/fpga_firmware/chfpga/f_engine/funcgen.py
            Keyword arguments can be included (see below).

        channels : (list) A list of the input channels (i.e., 0 for ADC1, 1 for ADC2, etc.) for which you'd like to recover data.
        The default is to receive data from all channels [0, .., 7].

        fft_bypass : (bool) Whether or not to bypass the FFT. Why include an option to bypass the FFT when you are requesting post-gain frames,
        you might ask? Well, sometimes it is useful to use the FUNCGEN to create a simulated spectrum, in which case you wouldn't want 
        the signal you have set in the FUNCGEN to propagate through the firmware FFT. 
        
            With the fft_bypass option, you have the power to create simulated FFT outputs for signal path validation.

            If True, the FFT is bypassed.

            If False, the FFT is not bypassed. Default.

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

        function_kwargs : (dict) Arguments for FUNCGEN functions passed to set_channelizer(). Used when data_source is not 'adc'.
        See pychfpga/pychfpga/fpga_firmware/chfpga/f_engine/funcgen.py for functions and their arguments.

        Returns
        -------
        d_post_gain : (np.ndarray) The 16+16-bit post-gain data. The frequency axis is returned in the proper order.

        The shape of the array depends upon the choice for the "split" parameter.

            If split is True:
                d_post_gain.shape = (self.NPOLS, n_frames, self.NBINS)

            If split is False:
                d_post_gain.shape = (self.NPOLS, n_frames*self.NBINS)

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

        # # Verify that 'data_source' has been specified correctly
        # valid_data_sources = ['adc', 'funcgen']
        # if data_source not in valid_data_sources:
        #     raise ValueError(f'Data source {data_source} is invalid. Valid data sources are {valid_data_sources}.')

        # Ensure UCAP is configured to receive channelizer data, not correlated data:
        if self.ucap.OUTPUT_SOURCE_SEL != 0:
            self.ucap.OUTPUT_SOURCE_SEL = 0

        # # Configure each channelizer
        # for n in range(self.NPOLS):

        #     if data_source == 'adc':
        #         # Set the data source
        #         self.i.chan[n].FUNCGEN.set_data_source(data_source)

        #     elif data_source == 'funcgen':
        #         # Don't set the data source, but verify that the function 
        #         # specified is the one that is being used
        #         current_data_source = pc.i.chan[n].FUNCGEN.get_data_source() 
        #         if current_data_source != 'buffer':
        #             raise ValueError(f'Data source {data_source} was specified, but {current_data_source} is currently in use. Verify that the function generator has been configured prior to capturing data.')

        #     # Do not bypass the FFT
        #     self.i.chan[n].FFT.BYPASS = 0

        #     # Bypass the SCALER, and set the mode so that we send the most significant 16+16 bits of the saturated post-gain FFT values
        #     self.i.chan[n].SCALER.BYPASS = 1
        #     self.i.chan[n].SCALER.CAP_DATA_TYPE = 2 

        # Configure the channelizers
        self.i.set_channelizer(data_source = data_source,
                               # funcgen_left_shift=0,
                               # funcgen_right_shift=0,
                               fft_bypass = fft_bypass,
                               scaler_bypass = True,
                               scaler_cap_data_type = 2,
                               scaler_bypass_data_type = None,
                               channels = channels,
                               **function_kwargs
                               )

        # Start data capture, grabbing data at the output of the
        # scaler, which has been bypassed.
        self.i.start_data_capture(period = period, 
                                  source = 'scaler',
                                  mode = self.ucap.MODE,
                                  channels = channels, 
                                  data_type = 2,
                                  select = True
                                  )

        # Capture data!
        t, d_post_gain, c = self.u_receiver.read_raw_frames(format = '16+16', 
                                                            ncap = ncap, 
                                                            split = True,
                                                            verbose = 0
                                                            )

        return d_post_gain[..., self.bin_map]

    def read_scaler_frames(self,
                           n_frames = 2,
                           data_source = 'adc',
                           channels = list(np.arange(8)),
                           fft_bypass = False,
                           scaler_bypass = False,
                           period = 0.02, 
                           split = True,
                           verbose = 0,
                           **function_kwargs
                           ):
        """
        Read SCALER frames using UCAP. By default, UCAP is in mode 0, meaning 2 frames for each ADC channel will be
        captured per burst of 16 frames.

        SCALER data is returned as 4+4-bit integers.

        Parameters
        ----------
        n_frames : (int) Number of frames captured per function call. n_frames must be an even-valued integer, as UCAP only
        receives data in bursts of 16 frames. 

        data_source : (str) A string that sets whether real ADC data or data stored in the FUNCGEN buffer are sent out of the FUNCGEN.

            If 'adc', then real digitized data from the ADC will pass through the CHAN signal chain.

            Otherwise, the user can specify a desired function listed in pychfpga/pychfpga/fpga_firmware/chfpga/f_engine/funcgen.py
            Keyword arguments can be included (see below).

        channels : (list) A list of the input channels (i.e., 0 for ADC1, 1 for ADC2, etc.) for which you'd like to recover data.
        The default is to receive data from all channels [0, .., 7].

        fft_bypass : (bool) Whether or not to bypass the FFT. Why include an option to bypass the FFT when you are requesting post-gain frames,
        you might ask? Well, sometimes it is useful to use the FUNCGEN to create a simulated spectrum, in which case you wouldn't want 
        the signal you have set in the FUNCGEN to propagate through the firmware FFT. 
        
            With the fft_bypass option, you have the power to create simulated FFT outputs for signal path validation.

            If True, the FFT is bypassed.

            If False, the FFT is not bypassed. Default.

        scaler_bypass : (bool) Whether or not to bypass the Scaler, similar to the fft_bypass argument.
        
            With the scaler_bypass option, you have the power to create simulated Scaler outputs for signal path validation when
            combined with the fft_bypass option.

            If True, the Scaler is bypassed.

            If False, the Scaler is not bypassed. Default.

        period : (float) Time (in seconds) between captured bursts. 0.02 is the minimum recommended to not saturate the ethernet link. 

        split : (bool) Whether to include a "frames" axis for the data array, which allows the user to inspect individual frames.

            If True:
                axis 0: ADC channel
                axis 1: frame number
                axis 2: sample number

            If False:
                axis 0: ADC channel
                axis 1: sample number, with all frames flattened

        verbose : (int) Verbosity level. 0: no messages, 1: basic messages, 2: detailed messages

        function_kwargs : (dict) Arguments for FUNCGEN functions passed to set_channelizer(). Used when data_source is not 'adc'.
        See pychfpga/pychfpga/fpga_firmware/chfpga/f_engine/funcgen.py for functions and their arguments.

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

        # # Verify that 'data_source' has been specified correctly
        # valid_data_sources = ['adc', 'funcgen']
        # if data_source not in valid_data_sources:
        #     raise ValueError(f'Data source {data_source} is invalid. Valid data sources are {valid_data_sources}.')

        # Ensure UCAP is configured to receive channelizer data, not correlated data:
        if self.ucap.OUTPUT_SOURCE_SEL != 0:
            self.ucap.OUTPUT_SOURCE_SEL = 0

        # # Configure each channelizer
        # for n in range(self.NPOLS):

        #     if data_source == 'adc':
        #         # Set the data source
        #         self.i.chan[n].FUNCGEN.set_data_source(data_source)

        #     elif data_source == 'funcgen':
        #         # Don't set the data source, but verify that the function 
        #         # specified is the one that is being used
        #         current_data_source = pc.i.chan[n].FUNCGEN.get_data_source() 
        #         if current_data_source != 'buffer':
        #             raise ValueError(f'Data source {data_source} was specified, but {current_data_source} is currently in use. Verify that the function generator has been configured prior to capturing data.')

        #     # Do not bypass the FFT
        #     self.i.chan[n].FFT.BYPASS = 0

        #     # Do not bypass the SCALER, and set the mode so that we read 4+4-bit SCALER data
        #     self.i.chan[n].SCALER.BYPASS = 0
        #     self.i.chan[n].SCALER.CAP_DATA_TYPE = 2 # Main scaler output

        # Configure the channelizers
        self.i.set_channelizer(data_source = data_source,
                               # funcgen_left_shift=0,
                               # funcgen_right_shift=0,
                               fft_bypass = fft_bypass,
                               scaler_bypass = scaler_bypass,
                               scaler_cap_data_type = 0,
                               scaler_bypass_data_type = None,
                               channels = channels,
                               scaler_eight_bit = False,
                               scaler_rounding_mode = 2,
                               symmetric_saturation = True,
                               zero_on_sat = False,
                               #  offset_binary_encoding=None,
                               **function_kwargs
                               )

        # Start data capture, always setting the source to 'scaler'
        self.i.start_data_capture(period = period, 
                                  source = 'scaler',
                                  mode = self.ucap.MODE,
                                  channels = channels, 
                                  data_type = 0,
                                  select = True
                                  )

        # Capture data!
        t, d_scaler, c = self.u_receiver.read_raw_frames(format = '16+16', 
                                                         ncap = ncap, 
                                                         split = True,
                                                         verbose = verbose
                                                         )

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

    def generate_prod_map(self):
        """
        Generate the product ("prod") map, which resolves the index of a correalted product
        with the two corresponding polarizations of that correlated product.

        Originally, the map simply corresponded to flattened rows of the upper triangle of
        an N^2 matrix, but the new correlator applies some scrambling of the products.

        The map was originally written by JF Cliche. 

        Parameters
        ----------
        None.

        Returns
        -------
        self.prod : (np.ndarray) The product map. The shape is 
 
        """
        # ============================================
        # New correlator product array
        N = 8 # the correlator always has 8 polarizations                                                                                                                                                 |
        prod_map = [ (i, (j + i) % N) if i < N - j else ((j + i) % N, i - 1) for i in range(N + 1) for j in range(N//2)] 
        self.prod = np.array(prod_map)

        # ============================================
        # Old correlator product array
        # prod_idx = np.triu_indices(8)
        # prod = np.zeros((len(prod_idx[0]), 2))

        # for n in range(prod.shape[0]):
        #     prod[n] = (prod_idx[0][n], prod_idx[1][n])

        # self.prod = prod.astype(np.int16)
        # ============================================

        return self.prod

    def read_corr_frames(self,
                         data_source = 'adc',
                         fft_bypass = False,
                         scaler_bypass = False,
                         n_software_frames = 1,
                         number_of_results = 1,
                         flush = True,
                         data_timeout = 0.1,
                         flush_timeout = 0.01,
                         return_format = 'raw',
                         verbose = 0,
                         corr_is_configured = False,
                         autocorr_only = 0,
                         no_accum = 0,
                         n_firmware_frames = 16384 - 1,
                         **function_kwargs
                         ):
        """
        Read correlated and accumulated frames. Correlation is always performed in real-time in the FPGA.
        Accumulation is performed in two steps: (1) n_firmware_frames are accumulated in the FPGA,
        and (2) n_software_frames of pre-data accumulated for n_firmware_frames are accumulated in Python.
        The two-step accumulation reduces the data rate such that data can be sent over the 1G network, 
        enabling longer integration times. 

        This function is a wrapper for read_corr_frames() from 

            pychfpga/pychfpga/fpga_firmware/chfpga/x_engine/UCORR.py

        and essentially performs the same operations, with some added processing of data specific to the 
        POCKET_CORRELATOR class.

        Parameters
        ----------
        data_source : (str) A string that sets whether real ADC data or data stored in the FUNCGEN buffer are sent out of the FUNCGEN.

            If 'adc', then real digitized data from the ADC will pass through the CHAN signal chain.

            Otherwise, the user can specify a desired function listed in pychfpga/pychfpga/fpga_firmware/chfpga/f_engine/funcgen.py
            Keyword arguments can be included (see below).

        fft_bypass : (bool) Whether or not to bypass the FFT. Why include an option to bypass the FFT when you are requesting post-gain frames,
        you might ask? Well, sometimes it is useful to use the FUNCGEN to create a simulated spectrum, in which case you wouldn't want 
        the signal you have set in the FUNCGEN to propagate through the firmware FFT. 
        
            With the fft_bypass option, you have the power to create simulated FFT outputs for signal path validation.

            If True, the FFT is bypassed.

            If False, the FFT is not bypassed. Default.

        scaler_bypass : (bool) Whether or not to bypass the Scaler, similar to the fft_bypass argument.
        
            With the scaler_bypass option, you have the power to create simulated Scaler outputs for signal path validation when
            combined with the fft_bypass option.

            If True, the Scaler is bypassed.

            If False, the Scaler is not bypassed. Default.
            
        n_software_frames : (int) The number of pre-integrated firmware frames to integrate in Python.
        Increasing this value increases the total integration time.

        number_of_results : (int) The number of integrated frames to return. Each "result" contains
        all frequency bins for each correlated product, integrated for the specified number of frames.

        flush : (bool) Whether to flush the UDP buffers. If data is allowed to accumulate in the UDP
        buffers for a while, it's a good idea to flush the buffer and get a fresh start.

        data_timeout : (float) The socket timeout for capturing data in the UDP buffer.

        flush_timeout : (float) The socket timeout for flushing data in the UDP buffer.

        return_format : (str) The format in which to return the data. Only 'raw' has been tested with 
        the Pocket Correlator, and therefore should always be used by default. 'raw' returns the data
        in the native format without rearranging the data (although the bin_map correction is applied
        in this wrapper function).

        verbose : (int) Verbosity level. 0: no messages, 1: basic messages, 2: detailed messages

        corr_is_configured : (bool) Whether or not the correlator is already configured. If False,
        the correaltor will be automatically configured with the following three parameters.

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

        function_kwargs : (dict) Arguments for FUNCGEN functions passed to set_channelizer(). Used when data_source is not 'adc'.
        See pychfpga/pychfpga/fpga_firmware/chfpga/f_engine/funcgen.py for functions and their arguments.

        Returns
        -------
        t1 : (float) A rough timestamp using time.time(). 

            **IMPORTANT**: this value is not synchronized with the first frame for a given result, but is instead 
            read immediately before capturing correlated data. Note that this results in some timing error in the 
            interferometer. For testbed or prototype arrays, such a timestamp may be sufficient, but for science-ready
            arrays, synchronization must be implemented.

            Synchronized timestamps will be included in future upgrades to the Pocket Correlator.

        d_corr : (np.ndarray) The correlated data. The shape is (number_of_results, NBINS, NCORR_PROD). Note that the
        final axis of size NCORR_PROD must be indexed using self.prod, which is created using generate_prod_map().

        count : (np.ndarray) The actual number of software frames that have been integrated for each 
        frequency bins. count should be a 1D array of length NBINS, whose values are equivalent to 
        n_software_frames; any lower values indicate that packets are being dropped, or otherwise not 
        integrated in Python. count there informs the user of dropped packets, which is important to keep
        track of due to the relationship between integration time and noise through the radiometer equation.

        sat : (np.ndarray) The number of saturations that occurred in the firmware accumulator. The shape
        is (number_of_results, NBINS, NCORR_PROD).

        """

        # TEMPORARY: if more than one software frame is requested, throw an error. There is a bug somewhere
        # that shows up for > 1 software frame.
        if n_software_frames > 1:
            raise ValueError(f'{n_software_frames} software frames requested. More than one software frame is untrusted in this commit.')

        # # Verify that 'data_source' has been specified correctly
        # valid_data_sources = ['adc', 'funcgen']
        # if data_source not in valid_data_sources:
        #     raise ValueError(f'Data source {data_source} is invalid. Valid data sources are {valid_data_sources}.')

        # Ensure UCAP is configured to receive correlated data, not channelizer data:
        if self.ucap.OUTPUT_SOURCE_SEL != 1:
            self.ucap.OUTPUT_SOURCE_SEL = 1

        # # Configure each channelizer
        # for n in range(self.NPOLS):

        #     if data_source == 'adc':
        #         # Set the data source
        #         self.i.chan[n].FUNCGEN.set_data_source(data_source)

        #     elif data_source == 'funcgen':
        #         # Don't set the data source, but verify that the function 
        #         # specified is the one that is being used
        #         current_data_source = pc.i.chan[n].FUNCGEN.get_data_source() 
        #         if current_data_source != 'buffer':
        #             raise ValueError(f'Data source {data_source} was specified, but {current_data_source} is currently in use. Verify that the function generator has been configured prior to capturing data.')

        #     # Do not bypass the FFT
        #     self.i.chan[n].FFT.BYPASS = 0

        #     # Do not bypass the SCALER, and set the mode so that we read 4+4-bit SCALER data.
        #     # Note that we need to set the DATA_TYPE control byte rather than the CAP_DATA_TYPE
        #     # control byte now that we want to configure the data type being sent to the correlator.
        #     self.i.chan[n].SCALER.BYPASS = 0
        #     self.i.chan[n].SCALER.DATA_TYPE = 0 # Send 4-bit scaler values

        # Configure the channelizers
        self.i.set_channelizer(data_source = data_source,
                               # funcgen_left_shift=0,
                               # funcgen_right_shift=0,
                               fft_bypass = fft_bypass,
                               scaler_bypass = scaler_bypass,
                               scaler_cap_data_type = 0,
                               scaler_bypass_data_type = None,
                               channels = list(np.arange(8)), # Configure all 8 ADCs by default
                               scaler_eight_bit = False,
                               scaler_rounding_mode = 2,
                               symmetric_saturation = True,
                               zero_on_sat = False,
                               #  offset_binary_encoding=None,
                               **function_kwargs
                               )

        # Configure correlator if it has not been done so
        if not self.corr_is_configured:
            self.configure_corr(autocorr_only = autocorr_only,
                                no_accum = no_accum,
                                n_firmware_frames = n_firmware_frames
                                )

        t1 = time.time() # This is our timestamp  
        d_corr, count, sat, = self.corr_receiver.read_corr_frames(soft_integ_period = n_software_frames,
                                                                  number_of_results = number_of_results,
                                                                  filename = None,
                                                                  flush = flush,
                                                                  data_timeout = data_timeout,
                                                                  flush_timeout = flush_timeout,
                                                                  return_format = return_format,
                                                                  verbose = verbose
                                                                  )
        t2 = time.time()

        print(f'Took {t2 - t1} s to capture correlated data')

        # Reshape the d_corr array according to the number of correlators (4 in the case of corr8) and the number of bins
        # per stream (2048 for corr8 mode); i.e., there are 4 individual firmware correlator modules, each responsible
        # for correlating 2048 frequency bins
        d_corr = d_corr.reshape(number_of_results, self.corr_receiver.NCORR*self.corr_receiver.NBINS_PER_STREAM, self.corr_receiver.NPROD)
        count = count.reshape(number_of_results, self.corr_receiver.NCORR*self.corr_receiver.NBINS_PER_STREAM)
        sat = sat.reshape(number_of_results, self.corr_receiver.NCORR*self.corr_receiver.NBINS_PER_STREAM, self.corr_receiver.NPROD)

        # Apply the bin map when returning the array
        return t1, d_corr[:, self.bin_map], count[:, self.bin_map], sat

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

    def observe(self, 
                data_path = None, # No default data path
                obs_tag = None, 
                digital_gains_path = None, 
                n_vis_per_file = 256,
                n_software_frames = 119, # 119 software frames with 16384 firmware frames is approximately 10 s
                autocorr_only = 0, # Get all products by default
                no_accum = 0, # accumulate by default
                n_firmware_frames = 16384 - 1,
                gain_target = 1.5*np.sqrt(2), # borrowed from CHIME
                n_fft_frames_for_gain_comp = 2*500,
                capture_adc_bursts = True,
                n_adc_frames_per_file = 2*5,
                capture_fft_bursts = True,
                n_fft_frames_per_file = 2*5
                ):

        """
        A method for computing and storing visibility data for a small interferometer. 

        Keep in mind that the default UCAP mode is 0, meaning that bursts of ADC and FFT data have 2 frames per input channel.

        observe() is only used with the channelizers configured to capture real data from each ADC. The channelizer is configured
        automatically when this function is called.

        Parameters
        ----------
        data_path : (str) The path to the directory to which visibility data will be written. If it doesn't exist, a parent directory will be created according
        to data_path. Within this directory, another directory will be created according to the date and time of the acquisition. HDF5 files will be written within 
        this directory, beginning with 0.hdf5 and incrementing the number by 1 with each new file. In this manner, all data can be stored in a parent directory,
        with specific acquisitions identified by the date and time of the acquisiton.

        obs_tag : (str) Observation tag added to end of obs_string, which together correspond to the observation file name.

            For D3A, obs_tag = '_D3A_rfsoc' for backwards compatibility. 

        digital_gains_path : (str) The path to which digital gains will be stored, or the file from which digital gains will be called. If None, then observe()
        will compute the digital gains with self.compute_gains(), which will be stored in the directory f'{data_path}/digital_gains/{obs_string}_digitalgain' as
        the file 'gains.hdf5'. The obs_string corresponds to the date and time of the acquisition, as with the data itself.

        If digitial_gains_path is specified, observe() will search for a 'gains.hdf5' file within the specified directory. These gains will be opened and set 
        in the Scaler during the observation. 

        n_vis_per_file : (int) The number of visibilities to store per HDF5 file. Increasing this number will increase the size of each HDF5 file. Reducing this
        may be useful to collect more ADC and/or FFT snapshots during the observation, as the ADC and/or FFT snapshots are collected when a new HDF5 file is created. 

        n_software_frames : (int) The number of pre-integrated firmware frames to integrate in Python.
        Increasing this value increases the total integration time.

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

        gain_target : (float) The average magnitude of the 4+4-bit SCALER frames. If target is too low, quantization
        noise will begin to dominate over the signal. If target is too high, both the SCALER and the accumulator 
        of the correlator might saturate. The recommended value is borrowed from that used in the CHIME F-Engine,
        which was determined empirically by Prof. Juan Mena-Parra of UofT. 

        To check that you're meeting the target, run compute_gains(), and recover many (e.g., 100) Scaler frames, compute their magnitude, and take the
        mean across the frame ("time") axis. Compare the mean to the target; the mean magnitude should look like noise whose average value should
        approximately meet the target.

        n_fft_frames_for_gain_comp : (int) The number of FFT frames used to calculate the digital gains. It is recommended 
        to set this to approximately 1000 to reduce the noiise of the averaged magnitude of the FFT data.

        gain_type : (str) Specifies whether any post-processing is applied to the computed digital gains. The default is 'raw', meaning no additional processing
        is done after computing digital gains. See compute_gains() for further details --- note that 'raw' has performed the best out of the current algorithms.

        capture_adc_bursts : (bool) Specifies whether to capture ADC frames at the beginning of each HDF5 file. If True, capture ADC data. If False, don't capture
        ADC data.

        n_adc_frames_per_file : (int) The number of ADC frames to capture for each HDF5 file if capture_adc_bursts = True. Note that the data volume for a single capture is

            16384 samples per frames * 16 frames per capture * 16 bits per sample / 8 bits per byte / 1e6 B per MB = 0.524288 MB of data.

        capture_fft_bursts : (bool) Specifies whether to capture FFT frames at the beginning of each HDF5 file. If True, capture FFT data. If False, don't capture
        FFT data.

        n_fft_frames_per_file : (int) The number of FFT frames to capture for each HDF5 file if capture_fft_bursts = True. Note that the data volume for a single capture is

            16384 samples * 16 frames per capture * (32+32) bit per sample / 8 bits per byte / 1e6 B per MB = 2.097152 MB of data.

        Returns
        -------
        None.

        """

        # ===========
        # Configure data paths
        time_string = self.make_time_string()
        self.obs_string = time_string + obs_tag
        self.data_path = data_path
        self.vis_dir_name = f'{self.data_path}/{self.obs_string}'

        # Create general data parent directory if it doesn't exist --- this will house both the visibility data and the digital gains
        if not os.path.exists(self.data_path):
            print(f'Creating data directory at {self.data_path}')
            os.mkdir(self.data_path)
        else:
            print(f'Data directory is {self.data_path}')

        # Create observation-specific directory for the visibilities
        if not os.path.exists(self.vis_dir_name):
            print(f'Creating visibility directory at {self.vis_dir_name}')
            os.mkdir(self.vis_dir_name)
        else:
            print(f'Visibility directory is {self.vis_dir_name}')

        if digital_gains_path is None:
            self.digital_gains_path = f'{self.data_path}/digital_gains'
            self.gain_file_path = f'{self.digital_gains_path}/{self.obs_string}_digitalgain'

            # Create general "parent" digital gains directory if it doesn't exist yet
            if not os.path.exists(self.digital_gains_path):
                print(f'Creating general digital gains directory at {self.digital_gains_path}')
                os.mkdir(self.digital_gains_path)
            else:
                print(f'General digital gains directory is {self.digital_gains_path}')

            # Make observation-specific directory for the digital gains
            # if not os.path.exists(self.gain_file_path):
            #     print(f'Creating digital gains directory at {self.gain_file_path}')
            #     os.mkdir(self.gain_file_path)
            # else:
            #     print(f'Digital gains directory is {self.gain_file_path}')

            # ===========
            # Compute gains.
            
            print('')
            print('')
            print('===================================')
            print('')
            print('---  C O M P U T E   G A I N S  ---')
            print('')
            self.compute_gains(target = gain_target,
                               n_fft_frames = n_fft_frames_for_gain_comp, 
                               data_source = 'adc', # Always read real ADC with the observe method() 
                               channels = list(np.arange(8)), # Always read from all ADC channels
                               fft_bypass = False, # Do not bypass the FFT
                               bank = 0,
                               return_scaler_frames = False,
                               save_gains = True,
                               digital_gains_parent_path = self.digital_gains_path,
                               digital_gains_dir_name = self.gain_file_path
                               )
            print('Gain computation completed!')
            print('')

        else:
            self.digital_gains_path = digital_gains_path
            print('')
            print('')
            print('===================================')
            print('')
            print('    ---   L O A D   G A I N S ---')
            print('')
            print(f'Loading digital gains from file {self.digital_gains_path}/gains.hdf5 ---')
            print('')

            digital_gains_file = h5py.File(f'{self.digital_gains_path}/gains.hdf5', 'r')
            try:
                lin = digital_gains_file['gain_lin'][:]
                log = digital_gains_file['gain_log'][:]
            except:
                # For backwards compatibility:
                lin = digital_gains_file['lin'][:]
                log = digital_gains_file['log'][:]
            # self.set_gains(lin, [int(n) for n in log])
            gain = lin*2**log
            for n in range(self.NPOLS):
                self.i.chan[n].SCALER.set_gain_table(gain[n], bank = 0, gain_timestamp = None, log_gain = None)

        # ===========
        # Configure the correlator

        print('')
        print('')
        print('===================================')
        print('')
        print('    --- C O N F I G U R E  ---')
        print('')

        print('Configuring correlator')
        if not self.corr_is_configured:
            self.configure_corr(autocorr_only = autocorr_only,
                                no_accum = no_accum,
                                n_firmware_frames = n_firmware_frames
                                )

        # print(self.corr.status()) # print corr configuration status

        # ===========
        # Capture and write data

        print('')
        print('')
        print('===================================')
        print('')
        print('    --- O B S E R V E ---')
        print('')

        # Run data capture until keyboard interrupt
        file_number = 0 # starting hdf5 file number

        # Calculate the integration time
        s_per_frame = 2**14*(1/(self.fs*1e6)) # samples/frame * seconds/sample. Make sure sampling frequency is in Hz
        n_firmware_frames = self.corr.INTEGRATION_PERIOD + 1
        integration_time = n_firmware_frames * n_software_frames * s_per_frame
        print(f'Integration time: {integration_time} s')

        # Generate product map
        self.generate_prod_map() # instantiates array self.prod

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
                dset_sat    = f.create_dataset('sat', (n_vis_per_file, self.NBINS, self.NCORR_PROD), dtype = np.complex64) # Create sat dataset

                # Assign attributes to vis dataset:
                dset_vis.attrs['n_firmware_frames'] = n_firmware_frames # Number of firmware frames per visibility
                dset_vis.attrs['n_software_frames'] = n_software_frames # Number of software frames per visibility

                # Make index_map group
                index_map_grp = f.create_group('index_map')
                dset_freq = index_map_grp.create_dataset('freq', (self.NBINS, ), dtype = np.float64)
                dset_prod = index_map_grp.create_dataset('prod', (self.NCORR_PROD, 2), dtype = np.int16)
                dset_time = index_map_grp.create_dataset('time', (n_vis_per_file, ), dtype = np.float64)

                dset_freq[:] = self.f
                dset_prod[:] = self.prod

                # Make numpy buffers for index_map
                t_buf = np.zeros(n_vis_per_file, dtype = np.float64)
                vis_buf = np.zeros((n_vis_per_file, self.NBINS, self.NCORR_PROD), dtype = np.complex128)
                counts_buf = np.zeros((n_vis_per_file, self.NBINS), dtype = np.uint32)
                sat_buf = np.zeros((n_vis_per_file, self.NBINS, self.NCORR_PROD), dtype = np.complex64)

                # Make statistics group
                stats_grp = f.create_group('stats')
                dset_FRAME_COUNT = stats_grp.create_dataset('frame_count', (n_vis_per_file, self.NPOLS), dtype = int)
                # dset_ADC_OVERFLOWS = stats_grp.create_dataset('adc_overflows', (n_vis_per_file, self.NPOLS), dtype = int) # Deprecated as a result of the following two flags
                dset_ADC_OVERRANGE = stats_grp.create_dataset('adc_overrange', (n_vis_per_file, self.NPOLS), dtype = int)
                dset_ADC_OVERVOLTAGE = stats_grp.create_dataset('adc_overvoltage', (n_vis_per_file, self.NPOLS), dtype = int)
                dset_FFT_OVERFLOWS = stats_grp.create_dataset('fft_overflows', (n_vis_per_file, self.NPOLS), dtype = int) # 
                dset_SCALER_OVERFLOWS = stats_grp.create_dataset('scaler_overflows', (n_vis_per_file, self.NPOLS), dtype = int)
                dset_SCALER_STATS_READY = stats_grp.create_dataset('scaler_stats_ready', (n_vis_per_file, self.NPOLS), dtype = int)
                dset_CORR_OVERRUN = stats_grp.create_dataset('corr_overrun', (n_vis_per_file, ), dtype = int)

                # Make numpy buffers for statistics
                frame_count_buf = np.zeros((n_vis_per_file, self.NPOLS), dtype = int)
                # adc_overflows_buf = np.zeros((n_vis_per_file, self.NPOLS), dtype = int)
                adc_overrange_buf = np.zeros((n_vis_per_file, self.NPOLS), dtype = int)
                adc_overvoltage_buf = np.zeros((n_vis_per_file, self.NPOLS), dtype = int)
                fft_overflows_buf = np.zeros((n_vis_per_file, self.NPOLS), dtype = int)
                scaler_overflows_buf = np.zeros((n_vis_per_file, self.NPOLS), dtype = int)
                scaler_stats_ready_buf = np.zeros((n_vis_per_file, self.NPOLS), dtype = int)
                corr_overrun_buf = np.zeros(n_vis_per_file, dtype = int)

                if capture_adc_bursts:
                    # Capture ADC data. Note: a single capture for all 8 pols in mode 0 is equal to
                    # 16384 samples per frames * 16 frames per capture * 16 bits per sample / 8 bits per byte / 1e6 B per MB = 0.524288 MB of data.

                    # Create data set
                    dset_adc = f.create_dataset('adc', (self.NPOLS, n_adc_frames_per_file, self.M), dtype = np.int16) 

                    # Capture ADC frames
                    print(f'Capturing {n_adc_frames_per_file} ADC frames...')
                    d_adc = self.read_adc_frames(n_frames = n_adc_frames_per_file,
                                                 data_source = 'adc',
                                                 period = 0.02,
                                                 split = True,
                                                 verbose = 0
                                                 )
                    
                    print('Done!')

                    # Write to file
                    dset_adc[...] = d_adc

                if capture_fft_bursts:
                    # Capture FFT data. Note: a single capture for all 8 pols in mode 0 is equal to
                    # 16384 samples * 16 frames per capture * (32+32) bit per sample / 8 bits per byte / 1e6 B per MB = 2.097152 MB of data.

                    # Create data set
                    dset_fft = f.create_dataset('fft', (self.NPOLS, n_fft_frames_per_file, self.NBINS), dtype = np.complex128) 
                    
                    # Capture bursts
                    print(f'Capturing {n_fft_frames_per_file} FFT frames...')
                    d_fft = self.read_fft_frames(n_frames = n_fft_frames_per_file,
                                                 fft_bit_res = '32+32',
                                                 data_source = 'adc',
                                                 period = 0.02,
                                                 split = True,
                                                 verbose = 0
                                                 )
                    
                    print('Done!')

                    # Write to file
                    dset_fft[...] = d_fft

                for n in range(n_vis_per_file):

                    print('')
                    print('<<<<<<<<<<<<<<<<>>>>>>>>>>>>>>>>>')
                    print(f'Capture {n} for file {file_name}')

                    # Pulse resets

                    # ADC overrange/overvoltage flag reset, recalling that the reset 
                    # is an 8-bit number so we can index each ADC individually
                    self.i.GPIO.ADC_OR_CLEAR = 2**8 - 1 # 255, i.e., 11111111 for all 8 pols
                    self.i.GPIO.ADC_OR_CLEAR = 0
                    self.i.GPIO.ADC_OV_CLEAR = 2**8 - 1 # 255, i.e., 11111111 for all 8 pols
                    self.i.GPIO.ADC_OV_CLEAR = 0

                    for m in range(self.NPOLS):

                        # DEPRECATED
                        # ADC stats reset
                        # self.i.chan[m].FUNCGEN.RESET_STATS = 1
                        # self.i.chan[m].FUNCGEN.RESET_STATS = 1

                        # FFT stats reset
                        self.i.chan[m].FFT.OVERFLOW_RESET = 1
                        self.i.chan[m].FFT.OVERFLOW_RESET = 0

                        # Scaler stats reset
                        self.i.chan[m].SCALER.STATS_CAPTURE = 1

                    # Capture data, filling the NumPy buffer on each iteration
                    if n == 0:
                        flush = True # Not very pythonic, but flush on first capture
                    else:
                        flush = False # Otherwise, no flush
                        
                    # Capture correlated data (note that many default arguments are used here)
                    t, vis, counts, sat = self.read_corr_frames(data_source = 'adc',
                                                                fft_bypass = False,
                                                                scaler_bypass = False,
                                                                n_software_frames = n_software_frames,
                                                                number_of_results = 1,
                                                                flush = True, # flush,
                                                                corr_is_configured = self.corr_is_configured
                                                                )
                    
                    t_buf[n] = t
                    vis_buf[n] = vis[0] # vis is a 3D array even if we only request 1 result
                    counts_buf[n] = counts[0] # counts is 2D, but 0 axis has length of just 1
                    sat_buf[n] = sat

                    # Capture statistics after recording correlator data
                    print('Capturing statistics')

                    # Helper array for reading individual bits of ADC_OR and ADC_OV flags
                    bitmask = 2**np.arange(8) # 8-bit bitmask for isolating values of individual bits
                    bitshifts = np.arange(0, 8)[::-1] # bitshifts to read values of bits as 1 or 0
                    
                    # Read ADC_OR flag
                    adc_overrange_raw = self.i.GPIO.ADC_OV
                    adc_overrange_buf[n] = np.array([(adc_overrange_raw & bitmask[m]) >> m for m in bitshifts])

                    # Read ADC_OV flag
                    adc_overvoltage_raw = self.i.GPIO.ADC_OV
                    adc_overvoltage_buf[n] = np.array([(adc_overvoltage_raw & bitmask[m]) >> m for m in bitshifts])

                    for m in range(self.NPOLS):

                        # DEPRECATED
                        # ADC stats
                        # adc_overflows_buf[n, m] = self.i.chan[m].FUNCGEN.ADC_OVERFLOW_CTR

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

                    # Create arrays to print the overrange/overflow status of individual ADCs,
                    # reversing the array so that the ADCs are indexed in ascending order from ADC1 to ADC8
                    adc_or_arr = np.array([(adc_overrange_buf[n] & bitmask[m]) >> m for m in bitshifts])[::-1]
                    adc_ov_arr = np.array([(adc_overvoltage_buf[n] & bitmask[m]) >> m for m in bitshifts])[::-1]

                    # Print some statistics:
                    if adc_overrange_raw != 0:
                        print(f'ADC overranges detected:', [f'ADC{n + 1}' for n in range(8) if adc_or_arr[n] != 0])
                    else:
                        print('No ADC overranges detected')
                    if adc_overvoltage_raw != 0:
                        print(f'ADC overvoltages detected:', [f'ADC{n + 1}' for n in range(8) if adc_ov_arr[n] != 0])
                    else:
                        print('No ADC overvoltages detected')
                    # print(f'ADC Overranges: {bin(adc_overrange_buf[n])[2:].zfill(8)}')
                    # print(f'ADC Overvoltage: {bin(adc_overvoltage_buf[n])[2:].zfill(8)}')
                    print(f'FFT Overflows: {fft_overflows_buf[n]}')
                    print(f'Scaler Overflows: {scaler_overflows_buf[n]}')
                    print(f'Correlator Overruns: {corr_overrun_buf[n]}')

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
                # dset_ADC_OVERFLOWS[...] = adc_overflows_buf
                dset_ADC_OVERRANGE[...] = adc_overrange_buf
                dset_ADC_OVERVOLTAGE[...] = adc_overvoltage_buf
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
            # dset_ADC_OVERFLOWS[...] = adc_overflows_buf
            dset_ADC_OVERRANGE[...] = adc_overrange_buf
            dset_ADC_OVERVOLTAGE[...] = adc_overvoltage_buf
            dset_FFT_OVERFLOWS[...] = fft_overflows_buf
            dset_SCALER_OVERFLOWS[...] = scaler_overflows_buf
            dset_SCALER_STATS_READY[...] = scaler_stats_ready_buf
            dset_CORR_OVERRUN[...] = corr_overrun_buf

            print('Done!')
            print('')
            print(f'Ending observation {self.obs_string}.')
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
