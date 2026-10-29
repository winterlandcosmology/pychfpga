"""
Module for capturing and writing raw corelated data from a t0.CRS board.

The script can be run within an ipython session.

Make sure you've got the latest firmware that supports corr8 mode with all 8 channelizers!

You can connect to a board with serial number 0016 in ipython by running

    run sifpga_crs_corr_capture.py --serial 0016 --stderr_log_level info --prog 2

The following is directly copied from pychfpga/pychfpga/fpga_firmware/chfpga/f_engine/ucap.py:

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
    pychfpga/pychfpga/fpga_firmware/chfpga/f_engine/ucap.py
    pychfpga/pychfpga/fpga_firmware/chfpga/chfpga.py

"""

# Import common packages
import asyncio
import numpy as np
import h5py
import time
import datetime
import os
import argparse

# Import custom packages
from pychfpga import fpga_array
from pychfpga.network_utils import verify_network_settings

# Number of ADCs (channelizers) on a single t0.CRS board.
ADCS_PER_BOARD = 8

# Debugging suggestions shown to the user when packet loss is detected. These mirror the suggestions
# in f-engine-tools/useful_scripts/capture_data.py so that the advice is identical everywhere.
PACKET_LOSS_SUGGESTIONS = (
    'Try the following, in order:\n'
    '  1. Recapture the data (packet loss is sometimes transient).\n'
    '  2. Increase `period` (the burst period) in the capture call.\n'
    '  3. Run the network setting commands (sysctl UDP buffers) documented at the top of '
    'pocket_correlator.py in a terminal.\n'
    '  4. Ensure jumbo frames are enabled on the ethernet interface (e.g. sudo ifconfig eno1 mtu '
    '9000).\n'
    '  5. Power cycle the crate.'
)

class POCKET_CORRELATOR:

    async def init_async(self,
                         hwm,
                         stderr_log_level = 'info',
                         prog = 1,
                         ):
        """Asynchronous initializer, invoked when the instance is awaited.

        See :meth:`__init__` for the parameter descriptions. Do not call this
        directly; instead ``await`` the instance::

            pc = await POCKET_CORRELATOR('crs 0016', prog = 1)
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

        # Construct (and await) the FPGA array. corr8 is always used for the pocket correlator.
        self.ca = await fpga_array.FPGAArray(hwm = hwm,
                                             stderr_log_level = stderr_log_level,
                                             prog = prog,
                                             mode = 'corr8',
                                             )

        # Define useful objects
        self.i = self.ca.ib[0]  # we only need to grab the t0.CRS board object

        # The crate slot for the single board, used by the packet-loss / flag reporting helpers.
        _, slot = self.i.get_id()
        self.slots = [slot if slot is not None else 0]
        self.n_boards = 1

        # Default UCAP capture mode. Mode 0 captures 2 contiguous frames from all 8 channels, and is
        # the only mode in which all eight channelizers are read out (required to compute gains and
        # visibilities for all inputs). The class is robust to other modes: frames-per-burst and the
        # number of captured channels are always derived from the mode in use (see
        # n_frames_per_burst() and npols_for_mode()), never hardcoded.
        self.ucap_mode = 0
        self.NPOLS = self.npols_for_mode(self.ucap_mode)

        # Instantiate the raw-data and correlator receivers. These share a single packet receiver;
        # raw channelizer packets and correlator packets are distinguished by their packet headers.
        self.r = await self.ca.get_data_receiver_async()
        self.cr = await self.ca.get_corr_receiver_async()

        # Define useful variables
        self.fs = 3200  # MHz - Old firmware: 3000 MHz
        self.M = 2**14  # number of samples per ADC frame
        self.sample_period = 1 / (self.fs * 1e6)  # sample period in seconds
        self.frame_period = self.M * self.sample_period
        self.NBINS = int(self.M / 2)  # number of frequency bins, half the number of ADC samples per frame
        self.f_res = self.fs / self.M  # frequency resolution of FFT
        self.f = self.f_res * np.arange(self.NBINS)  # an array containing the frequency bins

        # Put the channelizers into the default correlator-ready state (see
        # set_default_channelizers): live ADC input plus the invariant scaler/FFT settings defined
        # by the SCALER_* / FFT_SHIFT / OFFSET_BINARY_ENCODING class constants.
        await self.set_default_channelizers()

        # Track whether a raw (ADC / FFT / scaler) capture has reconfigured the channelizers away
        # from the correlator-ready state, so read_corr_frames() knows when to restore it.
        self._channelizers_corr_ready = True

        # The correlator is not configured until configure_corr() (or read_corr_frames) is called.
        self.corr_is_configured = False

        # Holds the summary dict returned by the most recent raw capture, for convenience.
        self.last_capture_info = None

        return self

    def __init__(self,
                 hwm,
                 stderr_log_level = 'info',
                 prog = 1,
                 ):
        """
        The POCKET_CORRELATOR class is a wrapper for the FPGAArray class of fpga_array.py designed to
        optimize data collection with a t0.CRS board. It provides an additional layer of processing
        to simplify measurements of ADC, FUNCGEN, FFT, SCALER, and correlated data.

        This class was initially designed to function as an automated script for a single-board
        digital backend for computing and storing visibilities at the Deep Dish Development Array
        (D3A) at the Dominion Radio Astrophysical Observatory (DRAO), the site of the Canadian
        Hydrogen Observatory and Radio transient Detector (CHORD).

        Because the underlying FPGAArray is asynchronous, a POCKET_CORRELATOR instance must be
        *awaited* on construction::

            pc = await POCKET_CORRELATOR('crs 0016', prog = 1)   # IPython / Jupyter

        or, in a script::

            pc = asyncio.run(POCKET_CORRELATOR('crs 0016', prog = 1).init())

        Parameters:
            hwm (str): Hardware map: part and serial, for example
                ``'crs 0016'`` to find t0.CRS SN0016. This wrapper is for
                CRS boards, so the part is ``crs`` unless upgraded later.
            stderr_log_level (str): Logging level. See
                :mod:`pychfpga.fpga_array`. Default ``'info'``.
            prog (str or int): FPGA programming: ``0`` never, ``1`` only if
                not already programmed, ``2`` always reprogram.

        Returns:
            None
        """
        # Stash the constructor arguments; no hardware is touched here. The caller must ``await``
        # this instance (which runs init_async) before using it.
        self._init_kwargs = dict(hwm = hwm,
                                 stderr_log_level = stderr_log_level,
                                 prog = prog,
                                 )
        self._ready = False

    def __await__(self):
        # Awaiting the instance runs the asynchronous initializer and returns the ready instance.
        yield from self.init_async(**self._init_kwargs).__await__()
        self._ready = True
        return self

    async def init(self):
        """Convenience coroutine so ``asyncio.run(POCKET_CORRELATOR(...).init())`` works."""
        return await self

    # ------------------------------------------------------------------
    # Invariant channelizer settings.
    #
    # These are applied identically in every capture (raw and correlated) so that no capture can
    # accidentally change them. Only data_source, fft_bypass, scaler_bypass, and the scaler capture
    # data_type vary from one capture to the next.
    # ------------------------------------------------------------------
    FFT_SHIFT = 0b00000000000000          # FFT shift schedule, always 0 (bit-growth FFT)
    SCALER_ROUNDING_MODE = 2              # 0: truncate, 1: standard rounding, 2: convergent/banker's
    SCALER_EIGHT_BIT = False             # 4+4-bit scaler output (not 8+8)
    SCALER_SYMMETRIC_SATURATION = True   # symmetric saturation about zero
    OFFSET_BINARY_ENCODING = False       # two's-complement (not offset-binary) encoding

    async def set_default_channelizers(self, sync = True):
        """Configure all channelizers into the default, correlator-ready state.

        Live ADC data flows through the FFT and scaler, with the invariant settings defined by the
        class constants above (no FFT shift, convergent rounding, 4+4-bit output, symmetric
        saturation, two's-complement encoding). This is the state required for computing gains and
        for correlated capture, and raw captures restore it afterwards.

        Parameters:
            sync (bool): If True (default), run an array sync after configuring.

        Returns:
            None
        """
        await self.ca.set_channelizers_async(data_source = 'adc',
                                             fft_bypass = False,
                                             scaler_bypass = False,
                                             fft_shift = self.FFT_SHIFT,
                                             scaler_rounding_mode = self.SCALER_ROUNDING_MODE,
                                             scaler_eight_bit = self.SCALER_EIGHT_BIT,
                                             symmetric_saturation = self.SCALER_SYMMETRIC_SATURATION,
                                             offset_binary_encoding = self.OFFSET_BINARY_ENCODING,
                                             channels = list(range(ADCS_PER_BOARD)),
                                             sync = sync,
                                             )
        self._channelizers_corr_ready = True

    def npols_for_mode(self, mode):
        """Return the number of channels (polarizations) captured in a given UCAP mode.

        Parameters:
            mode (int): UCAP capture mode (0-3).

        Returns:
            int: Number of channels read out per burst (8, 4, 2, or 1).
        """
        npols_by_mode = {0: 8, 1: 4, 2: 2, 3: 1}
        if mode not in npols_by_mode:
            raise ValueError(f'Valid UCAP modes are {list(npols_by_mode)}, but {mode} was passed.')
        return npols_by_mode[mode]

    def n_frames_per_burst(self, mode = None):
        """
        Return the number of frames captured per burst for the given UCAP mode.

        A UCAP burst always spans 16 ADC frames; depending on the mode, those are split across a
        different number of channels, so the number of frames returned *per channel* per burst
        differs. The class is thus robust to the mode in use rather than assuming mode 0.

        Parameters
        ----------
        mode : (int or None) UCAP mode. If None, uses the current ``self.ucap_mode``.

        Returns
        -------
        n_frames_per_burst : (int) Number of frames per channel per burst for the given UCAP mode.

        """
        if mode is None:
            mode = self.ucap_mode

        n_frames_by_mode = {0: 2, 1: 4, 2: 8, 3: 16}
        if mode not in n_frames_by_mode:
            raise ValueError(f'Valid UCAP modes are {list(n_frames_by_mode)}, but {mode} was passed.')

        return n_frames_by_mode[mode]

    # ==================================================================
    # Capture-check helpers.
    #
    # These verify that a capture was healthy: that UCAP did not overrun, that no ADC overrange /
    # overvoltage flags are high, and that no UDP packets were dropped. They are ported from
    # f-engine-tools/useful_scripts/capture_data.py and adapted for the single-board CRS case. The
    # raw data methods call these and bundle the results into an ``info`` dict that is returned to
    # the user alongside the data.
    # ==================================================================

    def check_ucap_overrun(self, raise_on_overrun = True):
        """Check whether UCAP is in an OVERRUN state.

        UCAP reports an OVERRUN condition when a transmission is requested before the previous one
        has completed, i.e. UCAP is being asked to send data more quickly than the FPGA can. This is
        a sticky flag, so it should be checked after configuring a capture but before reading data.

        Parameters:
            raise_on_overrun (bool): If True (default), raise ``RuntimeError`` when an overrun is
                detected. If False, only return the flag.

        Returns:
            int: The UCAP OVERRUN flag (non-zero indicates an overrun).
        """
        overrun = int(self.i.UCAP.OVERRUN)

        if overrun and raise_on_overrun:
            raise RuntimeError(
                'The UCAP module reports an OVERRUN condition (UCAP is being asked to send data more '
                'quickly than the FPGA can).\n'
                'Try increasing `period` (the burst period) in the capture call.\n'
                f'Current OVERRUN flag: {overrun}'
            )

        return overrun

    def reset_adc_flags(self):
        """Reset (clear) the sticky ADC overrange and overvoltage flags on the board.

        These flags are sticky, so they must be cleared immediately before a capture in order for
        the values read back afterwards with :meth:`read_adc_flags` to reflect only that capture.

        Returns:
            None
        """
        self.i.GPIO.ADC_OR_CLEAR = 2**ADCS_PER_BOARD - 1
        self.i.GPIO.ADC_OR_CLEAR = 0
        self.i.GPIO.ADC_OV_CLEAR = 2**ADCS_PER_BOARD - 1
        self.i.GPIO.ADC_OV_CLEAR = 0

    def read_adc_flags(self, verbose = True):
        """Read back the ADC overrange and overvoltage flags after a capture.

        The overrange (OR) and overvoltage (OV) flags are read as 8-bit values (one bit per ADC) and
        decoded into arrays indexed by ADC in ascending order (ADC1 to ADC8).

        Parameters:
            verbose (bool): If True, print which ADCs have flags high.

        Returns:
            tuple: ``(adc_or_arr, adc_ov_arr, adc_or_high, adc_ov_high)`` where ``adc_or_arr`` and
            ``adc_ov_arr`` are length-8 integer arrays (1 indicates the flag is high) and
            ``adc_or_high`` / ``adc_ov_high`` are booleans indicating whether any flag is high.
        """
        bitmask = 2**np.arange(ADCS_PER_BOARD)       # bitmask for isolating individual bits
        bitshifts = np.arange(0, ADCS_PER_BOARD)[::-1]  # bitshifts to read each bit as 0 or 1

        adc_or = self.i.GPIO.ADC_OR
        adc_ov = self.i.GPIO.ADC_OV

        # Decode the 8-bit values into per-ADC flags, reversing so ADCs are in ascending order.
        adc_or_arr = np.array([(adc_or & bitmask[m]) >> m for m in bitshifts])[::-1]
        adc_ov_arr = np.array([(adc_ov & bitmask[m]) >> m for m in bitshifts])[::-1]

        adc_or_high = bool(np.any(adc_or_arr != 0))
        adc_ov_high = bool(np.any(adc_ov_arr != 0))

        if verbose:
            if adc_or_high:
                flagged = [f'ADC{m + 1}' for m in range(ADCS_PER_BOARD) if adc_or_arr[m] != 0]
                print(f'ADC overrange flags are high for: {flagged}')
            if adc_ov_high:
                flagged = [f'ADC{m + 1}' for m in range(ADCS_PER_BOARD) if adc_ov_arr[m] != 0]
                print(f'ADC overvoltage flags are high for: {flagged}')
            if not (adc_or_high or adc_ov_high):
                print('No ADC overrange or overvoltage flags are high.')

        return adc_or_arr, adc_ov_arr, adc_or_high, adc_ov_high


    def check_dropped_packets(self, c, data_label = 'data', raise_on_loss = False, verbose = True):
        """Check a raw capture for dropped packets and report debugging information.

        The number of packets expected per frame is ``self.r.NUMBER_OF_SUBFRAMES``. The counts array
        ``c`` returned by ``read_raw_frames`` has one entry per (input, frame) whose maximum value is
        ``NUMBER_OF_SUBFRAMES``; a value below this indicates dropped packets. The input axis is the
        axis whose length equals the number of captured channels; all other axes are reduced over.

        Parameters:
            c (np.ndarray): The counts array returned by ``read_raw_frames``.
            data_label (str): A label for the data type (e.g. 'ADC', 'FFT', 'Scaler'), used in
                messages.
            raise_on_loss (bool): If True, raise a ``ValueError`` (with debugging suggestions) when
                any packet loss is detected. If False (default), only report and return the summary.
            verbose (bool): If True, print a summary of the packet capture.

        Returns:
            dict: A summary with keys ``packet_loss`` (bool), ``subframes_per_frame`` (int),
            ``percent_captured`` (per-input array of the minimum percent captured),
            ``min_percent`` / ``mean_percent`` (float), ``max_dropped_per_frame`` /
            ``min_dropped_per_frame`` (int), and ``dropped_inputs`` (list of ADC indices, 1-based,
            that dropped at least one packet).
        """
        subframes_per_frame = int(self.r.NUMBER_OF_SUBFRAMES)

        c = np.asarray(c)

        # Identify the input axis (the axis whose length is the number of captured channels) so this
        # is robust to the axis ordering of the counts array (which depends on chan_first).
        n_inputs = c.shape[0]
        counts_by_input = c.reshape(n_inputs, -1)

        min_per_input = counts_by_input.min(axis = 1)
        max_dropped_per_frame = int(subframes_per_frame - counts_by_input.min()) if counts_by_input.size else 0
        min_dropped_per_frame = int(subframes_per_frame - counts_by_input.max()) if counts_by_input.size else 0

        percent_captured = 100.0 * min_per_input / subframes_per_frame
        min_percent = float(percent_captured.min()) if percent_captured.size else 100.0
        mean_percent = float(100.0 * counts_by_input.mean() / subframes_per_frame) if counts_by_input.size else 100.0

        dropped_inputs = [n + 1 for n in range(n_inputs) if min_per_input[n] < subframes_per_frame]
        packet_loss = len(dropped_inputs) > 0

        summary = {
            'packet_loss': packet_loss,
            'subframes_per_frame': subframes_per_frame,
            'percent_captured': percent_captured,
            'min_percent': min_percent,
            'mean_percent': mean_percent,
            'max_dropped_per_frame': max_dropped_per_frame,
            'min_dropped_per_frame': min_dropped_per_frame,
            'dropped_inputs': dropped_inputs,
        }

        if verbose:
            if packet_loss:
                print(f'{data_label} packet loss detected: {len(dropped_inputs)} input(s) affected '
                      f'(ADCs {dropped_inputs}); worst input captured {min_percent:.2f}% of packets, '
                      f'mean {mean_percent:.2f}%.')
            else:
                print(f'{data_label}: no packet loss ({mean_percent:.2f}% of packets captured).')

        if packet_loss and raise_on_loss:
            raise ValueError(
                f'{data_label} packet loss detected ({len(dropped_inputs)} input(s) affected; up to '
                f'{max_dropped_per_frame} of {subframes_per_frame} packets dropped in a single '
                f'frame).\n{PACKET_LOSS_SUGGESTIONS}'
            )

        return summary


    def adc_rms_power_dBm(self, d_adc):
        """Estimate the RMS power at the ADC input, in dBm, for each channel.

        Follows the calculation in f-engine-tools/useful_scripts/snapshot_signal_quality.py: the RMS
        voltage is the standard deviation of the ADC samples (assuming a zero-mean signal), converted
        to volts using the ADC full-scale, then to power into a 50 ohm... (100 for differential) load.

        Parameters:
            d_adc (np.ndarray): ADC data with the channel axis first (e.g. shape
                ``(n_channels, n_frames, M)`` or ``(n_channels, n_samples)``).

        Returns:
            np.ndarray: RMS power in dBm, one value per channel.
        """
        d_adc = np.asarray(d_adc)
        n_channels = d_adc.shape[0]

        # RMS voltage in LSBs, using the standard deviation (assumes a zero-mean signal).
        rms_voltage_LSBs = np.array([np.std(d_adc[n].flatten()) for n in range(n_channels)])
        rms_voltage_V = rms_voltage_LSBs * (1 / 2**14)
        rms_power_lin = rms_voltage_V**2 / 100
        rms_power_dBm = 10 * np.log10(rms_power_lin / 1e-3)

        return rms_power_dBm


    async def _configure_and_capture(self,
                                     *,
                                     n_frames,
                                     data_source,
                                     channels,
                                     fft_bypass,
                                     scaler_bypass,
                                     source,
                                     data_type,
                                     read_format,
                                     period,
                                     split,
                                     data_label,
                                     check_adc_flags,
                                     check_ucap_overrun,
                                     check_packets,
                                     raise_on_issue,
                                     flush,
                                     verbose,
                                     **function_kwargs):
        """Configure the channelizers, start a capture, and read the raw frames.

        This private helper contains the capture logic shared by :meth:`read_adc_frames`,
        :meth:`read_fft_frames`, :meth:`read_post_gain_frames`, and :meth:`read_scaler_frames`. It
        follows the recipe in f-engine-tools/useful_scripts/capture_data.py: configure the
        channelizers for the requested source (keeping the invariant scaler/FFT settings), start the
        capture, re-synchronize, check for a UCAP overrun, optionally reset and read the ADC flags,
        read the raw frames, and optionally check for dropped packets.

        After any raw capture, the channelizers are no longer in the correlator-ready state; this is
        recorded in ``self._channelizers_corr_ready`` so :meth:`read_corr_frames` restores them.

        Parameters:
            n_frames (int): Number of frames to capture per channel. Must be a positive multiple of
                the number of frames per burst for the current UCAP mode.
            data_source (str): ``'adc'`` for live ADC data, otherwise a FUNCGEN function name.
            channels (list): Channels to capture.
            fft_bypass, scaler_bypass (bool): Whether to bypass the FFT / scaler.
            source (str): UCAP capture source (``'funcgen'`` or ``'scaler'``).
            data_type: Scaler capture data type passed to ``start_data_capture_async``.
            read_format (str): Format passed to ``read_raw_frames``.
            period (float): Burst period in seconds.
            split (bool): Whether to split the capture into a per-frame axis.
            data_label (str): Human-readable label for messages ('ADC', 'FFT', 'Scaler', ...).
            check_adc_flags, check_ucap_overrun, check_packets (bool): Which health checks to run.
            raise_on_issue (bool): If True, raise when UCAP overruns, when ADC flags are high, or
                when packets were dropped. If False, these are reported but the (possibly imperfect)
                data is still returned so the user can inspect it.
            flush (bool): Whether to flush the receiver before capturing.
            verbose (int): Verbosity forwarded to ``read_raw_frames``.
            function_kwargs (dict): Extra arguments for FUNCGEN functions.

        Returns:
            tuple: ``(t, d, c, info)`` where ``info`` is a dict with keys ``adc_or_arr``,
            ``adc_ov_arr``, ``adc_or_high``, ``adc_ov_high``, ``ucap_overrun``, and ``packets``.
        """
        mode = self.ucap_mode
        n_frames_per_burst = self.n_frames_per_burst(mode)

        # Verify that the number of frames requested is a multiple of the frames per burst.
        if n_frames % n_frames_per_burst != 0:
            raise ValueError(
                f'{n_frames} frames were requested, but the number of frames captured must be a '
                f'multiple of {n_frames_per_burst} (the frames per burst in UCAP mode {mode}).'
            )
        if n_frames < n_frames_per_burst:
            raise ValueError(
                f'UCAP is in mode {mode}, so the minimum number of frames is {n_frames_per_burst}, '
                f'but {n_frames} were requested.'
            )

        # Number of bursts to capture.
        ncap = int(n_frames / n_frames_per_burst)
        # Configure the channelizers for the requested source, keeping the invariant settings. We do
        # not sync here; the sync is done explicitly once the capture has been started.
        await self.ca.set_channelizers_async(data_source = data_source,
                                             fft_bypass = fft_bypass,
                                             scaler_bypass = scaler_bypass,
                                             fft_shift = self.FFT_SHIFT,
                                             scaler_rounding_mode = self.SCALER_ROUNDING_MODE,
                                             scaler_eight_bit = self.SCALER_EIGHT_BIT,
                                             symmetric_saturation = self.SCALER_SYMMETRIC_SATURATION,
                                             offset_binary_encoding = self.OFFSET_BINARY_ENCODING,
                                             channels = channels,
                                             sync = False,
                                             **function_kwargs)

        # A raw capture takes the channelizers out of the correlator-ready state.
        self._channelizers_corr_ready = False

        # Start data capture.
        await self.ca.start_data_capture_async(period,
                                               source = source,
                                               mode = mode,
                                               channels = channels,
                                               data_type = data_type)

        # Re-synchronize so all frames are aligned.
        await self.ca.sync_async()

        # Check first that UCAP is not in an OVERRUN state. Whether this raises follows
        # raise_on_issue, consistent with the ADC-flag and packet-loss checks below.
        ucap_overrun = None
        if check_ucap_overrun:
            ucap_overrun = self.check_ucap_overrun(raise_on_overrun = raise_on_issue)

        if check_adc_flags:
            # Clear the sticky flags immediately before the capture so the values read back
            # afterwards reflect only this capture.
            self.reset_adc_flags()

        # Source index: 0 for funcgen/adc, 1 for scaler/fft.
        source_index = 0 if source in ('funcgen', 'adc') else 1

        t, d, c = self.r.read_raw_frames(ncap = ncap,
                                         format = read_format,
                                         split = split,
                                         source = source_index,
                                         flush = flush,
                                         verbose = verbose)

        adc_or_arr = adc_ov_arr = None
        adc_or_high = adc_ov_high = False
        if check_adc_flags:
            adc_or_arr, adc_ov_arr, adc_or_high, adc_ov_high = self.read_adc_flags(verbose = bool(verbose))

        packets = None
        if check_packets:
            packets = self.check_dropped_packets(c,
                                                 data_label = data_label,
                                                 raise_on_loss = raise_on_issue,
                                                 verbose = bool(verbose))

        # If the user asked to raise on any issue, raise now if the UCAP overran or ADC flags were
        # high. (Packet loss and the overrun have already raised above when raise_on_issue is set,
        # but we keep this combined check for a clear, single error message.)
        if raise_on_issue and (ucap_overrun or adc_or_high or adc_ov_high):
            raise RuntimeError(
                f'{data_label} capture issue: UCAP overrun = {bool(ucap_overrun)}, '
                f'ADC overrange = {adc_or_high}, ADC overvoltage = {adc_ov_high}. '
                'Pass raise_on_issue = False to inspect the data anyway.'
            )

        info = {
            'adc_or_arr': adc_or_arr,
            'adc_ov_arr': adc_ov_arr,
            'adc_or_high': adc_or_high,
            'adc_ov_high': adc_ov_high,
            'ucap_overrun': ucap_overrun,
            'packets': packets,
        }
        self.last_capture_info = info

        return t, d, c, info


    async def read_adc_frames(self,
                              n_frames = 2,
                              data_source = 'adc',
                              channels = list(range(ADCS_PER_BOARD)),
                              period = 0.02,
                              split = True,
                              check_adc_flags = True,
                              check_ucap_overrun = True,
                              check_packets = True,
                              raise_on_issue = False,
                              flush = True,
                              verbose = 0,
                              **function_kwargs
                              ):
        """
        Read raw ADC time-series frames using UCAP, bypassing the FFT and scaler.

        By default UCAP is in mode 0, so two frames per channel are captured per burst (16 ADC
        frames). The data is captured at the output of the FUNCGEN module, so ``data_source`` other
        than ``'adc'`` can be used to inject a test waveform.

        Parameters
        ----------
        n_frames : (int) Number of frames to capture per channel. Must be a positive multiple of the
        frames per burst for the current UCAP mode (2 in mode 0).

        data_source : (str) ``'adc'`` for real digitized data, or a FUNCGEN function name from
        pychfpga/pychfpga/fpga_firmware/chfpga/f_engine/funcgen.py (with keyword arguments below).

        channels : (list) Input channels to recover (0 = ADC1, ...). Default is all eight.

        period : (float) Time (in seconds) between captured bursts. 0.02 is the minimum recommended
        to not saturate the ethernet link.

        split : (bool) Whether to include a "frames" axis for the data array.

            If True:  axis 0: ADC channel, axis 1: frame number, axis 2: sample number
            If False: axis 0: ADC channel, axis 1: sample number (all frames flattened)

        check_adc_flags : (bool) If True (default), reset and read the ADC overrange / overvoltage
        flags around the capture.

        check_ucap_overrun : (bool) If True (default), verify that UCAP is not in an OVERRUN state.

        check_packets : (bool) If True (default), check for dropped packets.

        raise_on_issue : (bool) If True, raise when UCAP overruns, when ADC flags are high, or when
        packets are dropped. If False (default), these are reported but the data is still returned so
        the user can inspect it.

        flush : (bool) If True (default), flush the receiver state before capturing.

        verbose : (int) Verbosity level. 0: no messages, 1: basic messages, 2: detailed messages

        function_kwargs : (dict) Arguments for FUNCGEN functions passed to set_channelizers(). Used
        when data_source is not 'adc'.

        Returns
        -------
        d_adc : (np.ndarray) The 14-bit ADC data (already shifted down from the 16-bit storage
        format). Shape is ``(n_channels, n_frames, self.M)`` if split is True, else
        ``(n_channels, n_frames * self.M)``.

        info : (dict) Capture-health summary (ADC flags, UCAP overrun, dropped packets). See
        :meth:`_configure_and_capture`.
        """
        t, d_adc, c, info = await self._configure_and_capture(
            n_frames = n_frames,
            data_source = data_source,
            channels = channels,
            fft_bypass = True,
            scaler_bypass = True,
            source = 'funcgen',
            data_type = None,
            read_format = '14',
            period = period,
            split = split,
            data_label = 'ADC',
            check_adc_flags = check_adc_flags,
            check_ucap_overrun = check_ucap_overrun,
            check_packets = check_packets,
            raise_on_issue = raise_on_issue,
            flush = flush,
            verbose = verbose,
            **function_kwargs
        )

        return d_adc, info


    async def read_fft_frames(self,
                             n_frames = 2,
                             fft_bit_res = '32+32',
                             data_source = 'adc',
                             channels = list(range(ADCS_PER_BOARD)),
                             fft_bypass = False,
                             period = 0.02,
                             split = True,
                             check_adc_flags = True,
                             check_ucap_overrun = True,
                             check_packets = True,
                             raise_on_issue = False,
                             flush = True,
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

        fft_bypass : (bool) Whether or not to bypass the FFT. Why include an option to bypass the FFT when you are requesting FFT frames,
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
        d_fft : (np.ndarray) The complex FFT data. The receiver returns the frequency axis already in
        ascending order. Shape is ``(n_channels, n_frames, self.NBINS)`` if split is True, else
        ``(n_channels, n_frames * self.NBINS)``.

        info : (dict) Capture-health summary (ADC flags, UCAP overrun, dropped packets). See
        :meth:`_configure_and_capture`.
        """

        # The FFT is captured at the scaler port with the scaler bypassed. Two resolutions are
        # supported, and in both cases the receiver returns the frequency axis already in ascending
        # order (no bin map needed) and deinterleaves the even/odd bins internally:
        #
        #   '16+16': the upper 16+16 bits of every bin are captured in one pass (data_type 'fft').
        #   '32+32': the full 32+32-bit resolution is captured; even and odd bins come from
        #            different physical frames, so the signal must be roughly stationary in time.
        #            The receiver stitches them back together (data_type 'fft_2x', read format
        #            '32+32d').
        if fft_bit_res == '16+16':
            data_type = 'fft'
            read_format = '16+16'
        elif fft_bit_res == '32+32':
            data_type = 'fft_2x'
            read_format = '32+32d'
        else:
            raise ValueError(f"fft_bit_res must be '16+16' or '32+32', but '{fft_bit_res}' was passed.")

        t, d_fft, c, info = await self._configure_and_capture(
            n_frames = n_frames,
            data_source = data_source,
            channels = channels,
            fft_bypass = fft_bypass,
            scaler_bypass = True,
            source = 'scaler',
            data_type = data_type,
            read_format = read_format,
            period = period,
            split = split,
            data_label = 'FFT',
            check_adc_flags = check_adc_flags,
            check_ucap_overrun = check_ucap_overrun,
            check_packets = check_packets,
            raise_on_issue = raise_on_issue,
            flush = flush,
            verbose = verbose,
            **function_kwargs
        )

        # The receiver already returns the frequency axis in ascending order with length NBINS.
        return d_fft, info


    async def compute_gains(self,
                            target = 1.5*np.sqrt(2), # default value borrowed from CHIME
                            n_fft_frames = 1000,
                            data_source = 'adc',
                            channels = list(range(ADCS_PER_BOARD)),
                            period = 0.02,
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
        The maximum digital gain is
        ``maximum_gain_lin * maximum_gain_log = 2^11 * 2^32``.

        The application of the digital gains results in a "post-gain" value with a bit-depth of 48+48 bits, of which the
        upper 4+4 MSBSs are taken and rounded to perform the 4+4-bit quantization.

        Since we can recover the full resolution of the FFT data, we can compute directly the digital gains
        necessary for optimial quantization in the SCALER, which is defined as a "target" SCALER magnitude.
        We can express the application of digital gains as::

            target_full = gain * |FFT|,

        where ``target_full = target * 2^44``, and ``|FFT|`` refers to the magnitude of FFT data, averaged for a
        specified number of frames to reduce noise. The digital gains are therefore simply::

            gain = target_full / |FFT|.

        This function uses the set_gain_table() function of the SCALER module (see scaler.py), which performs
        additional processing of the digital gains as computed with the equation above prior to writing them
        to the memory banks of the SCALER. Therefore, the "theoretical" gains, since they are computed as
        Python floating point numbers, are optimally broken up into linear and logarithmic components. As such,
        the "theoretical" gains are not necessarily equivalent to those that are actually applied by the SCALER,
        so the "actual" gains are returned to the user by this function. See

            pychfpga/pychfpga/fpga_firmware/chfpga/f_engine/scaler.py

        for more information on how the "theoretical" gains are processed.

        Gains are handled in natural (ascending frequency) order throughout;
        ``set_gain_table()`` and ``get_gain_table()`` handle the native FFT bin reordering internally.

        Parameters:
            target (float): Average magnitude of 4+4-bit SCALER frames. Too
                low and quantization noise dominates; too high and the
                scaler or correlator accumulator may saturate. The default
                is the CHIME F-engine empirical target.
            n_fft_frames (int): Number of FFT frames whose magnitudes are
                averaged when computing gains.
            data_source (str): ``'adc'`` for live ADC data, otherwise a
                FUNCGEN function name from
                :mod:`pychfpga.fpga_firmware.chfpga.f_engine.funcgen`.
            channels (list): Input channels to recover (0 = ADC1, …).
                Default is all eight.
            fft_bypass (bool): Bypass the FFT (useful when FUNCGEN already
                holds a simulated spectrum). Default False.
            bank (int): Gain bank to write (0 or 1).
            return_scaler_frames (bool): If True, also return scaler frames
                after the new gains are applied (same count as
                ``n_fft_frames``).
            save_gains (bool): If True, write gains via ``save_gains()``.
            digital_gains_parent_path (str): Parent directory for gain
                files. Used only if ``save_gains`` is True.
            digital_gains_dir_name (str): Subdirectory under the parent.
                Used only if ``save_gains`` is True.
            function_kwargs (dict): FUNCGEN arguments forwarded to
                ``set_channelizer()`` when ``data_source`` is not ``'adc'``.

        Returns:
            tuple: ``(d_fft, d_fft_mean_mag, gain, d_sclr)`` where ``gain`` is the full
            floating-point actual gain as a single value per bin, with shape
            ``(NPOLS, NBINS)``. ``d_sclr`` is ``None`` unless ``return_scaler_frames`` is True.
        """

        # =========================================================
        # Digital gain computation for the bit-growth FFT.
        # =========================================================

        # Capture FFT data, retrying until no ADC overrange or overvoltage flags are raised (so that
        # the digital gains are not biased by clipped data). read_fft_frames resets and reads the ADC
        # flags itself and reports them in info; raise_on_issue is left False so we control retries.
        NTRIES = 10
        d_fft = None
        for n in range(NTRIES):
            print(f'FFT capture trial {n + 1} of {NTRIES}')

            d_fft, info = await self.read_fft_frames(n_frames = n_fft_frames,
                                                     fft_bit_res = '32+32',
                                                     data_source = data_source,
                                                     channels = channels,
                                                     fft_bypass = fft_bypass,
                                                     period = period,
                                                     split = True,
                                                     check_adc_flags = True,
                                                     check_packets = True,
                                                     raise_on_issue = False,
                                                     verbose = 0,
                                                     **function_kwargs)

            print(f'Read {n_fft_frames} FFT frames')

            # Break loop if no ADC flags are high.
            if not (info['adc_or_high'] or info['adc_ov_high']):
                print('No ADC overvoltage or overrange flags detected. Moving on to digital gain computation.')
                break

            # Otherwise, raise after exhausting all trials.
            if n == NTRIES - 1:
                raise RuntimeError('All FFT capture trials had ADC flags high. Please try again!')

        # Compute theoretical gains using the mean magnitude of the FFT data. These are in natural
        # (ascending frequency) order, which is also what set_gain_table() expects; it reorders into
        # the native FFT bin order internally.
        target_full = target*2**44
        d_fft_mean_mag = np.mean(np.abs(d_fft), axis = 1)
        gain_theory = target_full/d_fft_mean_mag

        # Buffer for the actual gains read back from the FPGA (a single floating-point value per bin;
        # "actual" because set_gain_table() quantizes gain_theory, so it may differ slightly).
        gain = np.zeros(gain_theory.shape)

        # Set the gains, and recover the actual gains as stored in the FPGA.
        for n in range(self.NPOLS):

            scaler = self.i.chan[n].SCALER

            # Ensure floating-point (not complex) gains are enabled.
            if scaler.USE_COMPLEX_GAINS != 0:
                scaler.USE_COMPLEX_GAINS = 0
            if scaler.USE_FLOAT_GAINS != 1:
                scaler.USE_FLOAT_GAINS = 1

            # Write the theoretical gains. The scaler splits each into a mantissa/exponent pair and
            # handles the native FFT bin reordering internally.
            scaler.set_gain_table(gain_theory[n], bank = bank, gain_timestamp = None, log_gain = None)

            # Read back the actual gains. With floating-point gains enabled, get_gain_table() returns
            # the full gain (mantissa * 2**exponent, including the common log gain) as a single value
            # per bin, in natural frequency order.
            gain[n] = np.array(scaler.get_gain_table(bank = bank))

        if return_scaler_frames:
            d_sclr, _ = await self.read_scaler_frames(n_frames = n_fft_frames,
                                                      data_source = data_source,
                                                      channels = channels,
                                                      fft_bypass = fft_bypass,
                                                      scaler_bypass = False,
                                                      period = period,
                                                      split = True,
                                                      verbose = 0,
                                                      **function_kwargs)
        else:
            d_sclr = None

        if save_gains:
            self.save_gains(gain = gain,
                            digital_gains_parent_path = digital_gains_parent_path,
                            digital_gains_dir_name = digital_gains_dir_name
                            )

        return (d_fft,
                d_fft_mean_mag,
                gain,
                d_sclr
                )

    def save_gains(self,
                   gain,
                   digital_gains_parent_path = None,
                   digital_gains_dir_name = None
                   ):
        """
        Save the digital gains to an HDF5 file. A parent directory "digital_gains_parent_path"
        containing all digital gains files should be specified, and a daughter directory
        "digital_gains_dir_name" in which the digital gains will be stored should also be specified.
        The digital_gains_dir_name is a datetime value connected to a specific observation when
        save_gains() is called from observe().

        **NOTE**: be sure that you are saving the *actual* digital gains, which can be read with the
        SCALER.get_gain_table() method. It is critical that the *actual* digital gains are applied in
        offline analysis, not the theoretical gains! See compute_gains() for details.

        The gains are stored as a single floating-point value per bin (mantissa * 2**exponent,
        including the common log gain), in natural (ascending frequency) order, in a dataset named
        ``gain``.

        Parameters
        ----------
        gain : (np.ndarray) The full "actual" digital gains as a single value per bin. The shape is
        (NPOLS, NBINS).

        digital_gains_parent_path : (str) A parent directory containing sub-directories of digital
        gains.

        digital_gains_dir_name : (str) The directory in which the digital gains will be stored. This
        directory lives within the parent digital_gains_parent_path directory.

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

        # Create the gain hdf5 file, storing the gains as a single floating-point value per bin.
        with h5py.File(digital_gain_file_name, 'w') as file:
            file.create_dataset('gain',
                                (self.NPOLS, self.NBINS),
                                data = gain,
                                dtype = np.float64
                                )

        print(f'Gains have been saved to {digital_gain_file_name}')

    async def read_post_gain_frames(self,
                                   n_frames = 2,
                                   data_source = 'adc',
                                   channels = list(range(ADCS_PER_BOARD)),
                                   fft_bypass = False,
                                   period = 0.02,
                                   split = True,
                                   check_adc_flags = True,
                                   check_ucap_overrun = True,
                                   check_packets = True,
                                   raise_on_issue = False,
                                   flush = True,
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
        d_post_gain : (np.ndarray) The 16+16-bit post-gain data. The receiver returns the frequency
        axis already in ascending order. Shape is ``(n_channels, n_frames, self.NBINS)`` if split is
        True, else ``(n_channels, n_frames * self.NBINS)``.

        info : (dict) Capture-health summary (ADC flags, UCAP overrun, dropped packets). See
        :meth:`_configure_and_capture`.
        """
        # Post-gain data is captured at the scaler port with the scaler bypassed, using the 'fft_gain'
        # data type (the upper 16+16 bits of the post-gain value). The receiver returns the frequency
        # axis already in ascending order.
        t, d_post_gain, c, info = await self._configure_and_capture(
            n_frames = n_frames,
            data_source = data_source,
            channels = channels,
            fft_bypass = fft_bypass,
            scaler_bypass = True,
            source = 'scaler',
            data_type = 'fft_gain',
            read_format = '16+16',
            period = period,
            split = split,
            data_label = 'post-gain FFT',
            check_adc_flags = check_adc_flags,
            check_ucap_overrun = check_ucap_overrun,
            check_packets = check_packets,
            raise_on_issue = raise_on_issue,
            flush = flush,
            verbose = verbose,
            **function_kwargs
        )

        return d_post_gain, info

    async def read_scaler_frames(self,
                                n_frames = 2,
                                data_source = 'adc',
                                channels = list(range(ADCS_PER_BOARD)),
                                fft_bypass = False,
                                scaler_bypass = False,
                                period = 0.02,
                                split = True,
                                check_adc_flags = True,
                                check_ucap_overrun = True,
                                check_packets = True,
                                raise_on_issue = False,
                                flush = True,
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
        d_scaler : (np.ndarray) The 4+4-bit SCALER data (already shifted down from the 16+16-bit
        storage format). The receiver returns the frequency axis already in ascending order. Shape is
        ``(n_channels, n_frames, self.NBINS)`` if split is True, else
        ``(n_channels, n_frames * self.NBINS)``.

        info : (dict) Capture-health summary (ADC flags, UCAP overrun, dropped packets). See
        :meth:`_configure_and_capture`.
        """
        # Scaler data is captured at the scaler port with the 'fft_4bit' data type (the 4+4-bit scaler
        # output). The invariant scaler settings (convergent rounding, 4+4-bit, symmetric saturation,
        # two's-complement encoding) are applied by _configure_and_capture. The receiver returns the
        # frequency axis already in ascending order.
        t, d_scaler, c, info = await self._configure_and_capture(
            n_frames = n_frames,
            data_source = data_source,
            channels = channels,
            fft_bypass = fft_bypass,
            scaler_bypass = scaler_bypass,
            source = 'scaler',
            data_type = 'fft_4bit',
            read_format = '16+16',
            period = period,
            split = split,
            data_label = 'Scaler',
            check_adc_flags = check_adc_flags,
            check_ucap_overrun = check_ucap_overrun,
            check_packets = check_packets,
            raise_on_issue = raise_on_issue,
            flush = flush,
            verbose = verbose,
            **function_kwargs
        )

        # The 4+4-bit scaler values are packed into the upper bits of a 16+16-bit number; shift down.
        d_scaler = d_scaler / 2**12

        return d_scaler, info

    async def configure_corr(self,
                            n_firmware_frames = 16384,
                            autocorr_only = 0,
                            no_accum = 0,
                            verbose = 0
                            ):
        """
        Configure and start the firmware correlator, and cache the correlator metadata.

        Parameters
        ----------
        n_firmware_frames : (int) Number of frames to accumulate in firmware. Take care not to set
        this too low (see minimum), as this will increase the data rate, and could lead to an overrun
        condition. Also take care not to set this too high (see maximum), as this may cause increased
        saturation. The firmware accumulator can add up to 18+18-bit values, at which point they
        saturate. ``start_correlator`` subtracts 1 internally, so pass the number of frames directly.

        Use the recommended value!

            8192 : minimum
            16384: recommended
            32768: maximum

        autocorr_only : (int) Bit to choose between returning all correlated products, or only
        autocorrelations. Default is to send all N(N+1)/2 correlated products.

            0: return all products
            1: return autocorrelations only

        no_accum : (int) Bit to choose between accumulating (i.e., summing) correlated SCALER frames,
        and not accumulating. Default is to accumulate.

            0: do accumulate
            1: do not accumulate

        verbose : (int) Verbosity level. 0: no messages, 1: basic messages, 2: detailed messages

        Returns
        -------
        None.

        """

        # Keep a handle to the UCORR module for stats (e.g. OVERRUN) during observing.
        self.corr = self.i.CORR

        # Start the firmware correlator across the array.
        await self.ca.start_correlators_async(integration_period = n_firmware_frames,
                                              autocorr_only = autocorr_only,
                                              no_accum = no_accum,
                                              verbose = verbose
                                              )

        # Refresh the correlator receiver config and cache the derived metadata. The receiver returns
        # products in upper-triangle order and bins in ascending frequency order. NBINS from the
        # correlator config matches self.NBINS (M / 2), so we reuse self.NBINS throughout.
        self.cr.update_config()
        self.NPROD = int(self.cr.NPROD)
        self.NDECIM = int(self.cr.NDECIM)
        self.firmware_integ_period = int(self.cr.firmware_integ_period)
        self.expected_packets = int(self.cr.expected_packets)

        # Product map: prod[k] = (i, j) for the k-th correlation product, in the receiver's order.
        self.generate_prod_map()

        # Boolean mask (length self.NBINS) of FFT bins expected to contain valid correlator data
        # (readout / culled bins are False).
        self.good_bin_mask = self.cr.get_good_bin_mask()

        # Update corr_is_configured flag to True
        self.corr_is_configured = True

    def generate_prod_map(self):
        """
        Generate the product ("prod") map, which resolves the index of a correlated product into the
        two corresponding polarizations (i, j) of that product.

        The correlator receiver reorders products into upper-triangle order; this map is obtained
        directly from the receiver via ``get_ij_to_prod_map()`` and inverted so that ``prod[k]`` gives
        the ``(i, j)`` pair for product index ``k``.

        Parameters
        ----------
        None.

        Returns
        -------
        self.prod : (np.ndarray) The product map, shape ``(NPROD, 2)``, dtype int.
        """
        # get_ij_to_prod_map() returns an (NCHAN, NCHAN) array mapping (i, j) -> product index k.
        # Invert it so that prod[k] = (i, j). We take the upper triangle (i <= j) to get each product
        # exactly once.
        ij_to_prod = np.asarray(self.cr.get_ij_to_prod_map())
        n = ij_to_prod.shape[0]
        self.prod = np.zeros((self.NPROD, 2), dtype = int)
        for i in range(n):
            for j in range(i, n):
                k = int(ij_to_prod[i, j])
                self.prod[k] = (i, j)
        return self.prod

    async def read_corr_frames(self,
                              data_source = 'adc',
                              fft_bypass = False,
                              scaler_bypass = False,
                              n_software_frames = 1,
                              number_of_results = 1,
                              flush = True,
                              data_timeout = 0.1,
                              flush_timeout = 0.01,
                              check_packets = True,
                              raise_on_issue = False,
                              verbose = 0,
                              autocorr_only = 0,
                              no_accum = 0,
                              n_firmware_frames = 16384,
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

        check_packets : (bool) If True (default), check that the expected number of correlator packets
        were received for each result (compared against the receiver's ``expected_packets``), and
        report any loss.

        raise_on_issue : (bool) If True, raise when correlator packets were dropped. If False
        (default), packet loss is reported but the (possibly incomplete) data is still returned so the
        user can inspect it.

        verbose : (int) Verbosity level. 0: no messages, 1: basic messages, 2: detailed messages

        autocorr_only : (int) Bit to choose between returning all correlated products, or only
        autocorrelations. Only used when the correlator has not yet been configured.
        Default is to send all N(N+1)/2 correlated products.

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
        ``start_correlator`` subtracts 1 internally, so pass the number of frames directly. Only used
        when the correlator has not yet been configured.

        Use the recommended value!

            8192 : minimum
            16384: recommended
            32768: maximum

        function_kwargs : (dict) Arguments for FUNCGEN functions passed to set_channelizers(). Used when data_source is not 'adc'.
        See pychfpga/pychfpga/fpga_firmware/chfpga/f_engine/funcgen.py for functions and their arguments.

        Returns
        -------
        t1 : (float) A rough UNIX timestamp (seconds) of the capture, taken with time.time()
        immediately before reading the correlated data from the receiver. A single value is
        returned regardless of number_of_results.

        d_corr : (np.ndarray) The correlated data, shape ``(number_of_results, self.NBINS, NPROD)``.
        The receiver returns products in upper-triangle order and bins in ascending frequency order.
        The final axis of size NPROD can be indexed using self.prod (see generate_prod_map()).

        count : (np.ndarray) The number of correlator packets integrated for each result, shape
        ``(number_of_results,)``. Values below the receiver's ``expected_packets`` indicate dropped
        packets, which is important to track due to the relationship between integration time and noise.

        sat : (np.ndarray) The number of saturations that occurred in the firmware accumulator, shape
        ``(number_of_results, self.NBINS, NPROD)``.

        info : (dict) Capture-health summary with keys ``packet_loss`` (bool), ``expected_packets``
        (int), ``counts`` (array), and ``percent_captured`` (array).

        """

        # The correlator requires the channelizers in their standard signal path (FFT + scaler
        # enabled). A preceding raw capture (ADC / FFT / scaler) bypasses parts of that path, so we
        # restore the correlator-ready state if needed. For function-generator / bypass testing the
        # caller can request a non-default configuration, which we apply here instead.
        non_default = (data_source != 'adc') or fft_bypass or scaler_bypass or bool(function_kwargs)
        if non_default:
            await self.ca.set_channelizers_async(data_source = data_source,
                                                 fft_bypass = fft_bypass,
                                                 scaler_bypass = scaler_bypass,
                                                 fft_shift = self.FFT_SHIFT,
                                                 scaler_rounding_mode = self.SCALER_ROUNDING_MODE,
                                                 scaler_eight_bit = self.SCALER_EIGHT_BIT,
                                                 symmetric_saturation = self.SCALER_SYMMETRIC_SATURATION,
                                                 offset_binary_encoding = self.OFFSET_BINARY_ENCODING,
                                                 channels = list(range(ADCS_PER_BOARD)),
                                                 sync = True,
                                                 **function_kwargs)
            self._channelizers_corr_ready = False
        elif not self._channelizers_corr_ready:
            await self.set_default_channelizers(sync = True)

        # Configure (and start) the correlator if it has not been done yet.
        if not self.corr_is_configured:
            await self.configure_corr(n_firmware_frames = n_firmware_frames,
                                      autocorr_only = autocorr_only,
                                      no_accum = no_accum,
                                      verbose = verbose
                                      )

        # Clear the sticky correlator OVERRUN flag so it reflects only this capture.
        self.corr.pulse_bit('OVERRUN_RESET')

        t1 = time.time()
        integ_ts, d_corr, count, sat = self.cr.read_corr_frames(soft_integ_period = n_software_frames,
                                                                number_of_results = number_of_results,
                                                                flush = flush,
                                                                data_timeout = data_timeout,
                                                                flush_timeout = flush_timeout,
                                                                verbose = verbose
                                                                )
        t2 = time.time()

        if verbose:
            print(f'Took {t2 - t1} s to capture correlated data')

        # Check for dropped correlator packets. count holds one value per result; the full count is
        # the receiver's expected_packets (which already accounts for the software integration period).
        expected = int(self.cr.expected_packets)
        counts = np.asarray(count)
        percent_captured = 100.0 * counts / expected if expected else np.full(counts.shape, 100.0)
        packet_loss = bool(np.any(counts < expected))

        info = {
            'packet_loss': packet_loss,
            'expected_packets': expected,
            'counts': counts,
            'percent_captured': percent_captured,
        }

        if check_packets:
            if packet_loss:
                print(f'Correlator packet loss detected: counts = {counts.tolist()}, expected '
                      f'{expected} per result (min {percent_captured.min():.2f}% captured).')
            elif verbose:
                print(f'Correlator: no packet loss ({expected} packets per result).')

        if packet_loss and raise_on_issue:
            raise RuntimeError(
                f'Correlator packet loss detected (counts = {counts.tolist()}, expected {expected} '
                f'per result).\n{PACKET_LOSS_SUGGESTIONS}'
            )

        return t1, d_corr, count, sat, info

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

    async def observe(self,
                     data_path = None, # No default data path
                     obs_tag = None,
                     digital_gains_path = None,
                     n_vis_per_file = 256,
                     n_software_frames = 119, # 119 software frames with 16384 firmware frames is approximately 10 s
                     autocorr_only = 0, # Get all products by default
                     no_accum = 0, # accumulate by default
                     n_firmware_frames = 16384,
                     gain_target = 1.5*np.sqrt(2), # borrowed from CHIME
                     n_fft_frames_for_gain_comp = 2*500,
                     capture_adc_bursts = True,
                     n_adc_frames_per_file = 2*5,
                     capture_fft_bursts = True,
                     n_fft_frames_per_file = 2*5
                     ):

        """Compute and store visibility data for a small interferometer.

        Default UCAP mode is 0: ADC/FFT bursts have two frames per input
        channel. Channelizers are configured for live ADC data when this
        method is called.

        Parameters:
            data_path (str): Parent directory for visibility HDF5 files. A
                timestamped subdirectory is created per acquisition.
            obs_tag (str): Suffix on the observation name (for D3A use
                ``'_D3A_rfsoc'`` for compatibility).
            digital_gains_path (str): Directory containing ``gains.hdf5`` to
                load, or None to compute gains with :meth:`compute_gains`
                and store them under ``data_path``.
            n_vis_per_file (int): Visibilities per HDF5 file.
            n_software_frames (int): Host-side integrations of firmware
                frames. Larger values increase total integration time.
            autocorr_only (int): ``0`` all products, ``1`` autocorrelations
                only.
            no_accum (int): ``0`` accumulate in firmware, ``1`` do not.
            n_firmware_frames (int): Frames accumulated in firmware. Too
                low increases data rate; too high may saturate the 18+18-bit
                accumulator. Use the recommended default.
            capture_adc_bursts (bool): Capture ADC snapshots at the start of
                each HDF5 file.
            n_adc_frames_per_file (int): ADC frames per file when capturing
                ADC bursts.
            capture_fft_bursts (bool): Capture FFT snapshots at the start of
                each HDF5 file.
            n_fft_frames_per_file (int): FFT frames per file when capturing
                FFT bursts.
            gain_target (float): Scaler magnitude target forwarded to
                :meth:`compute_gains`.
            n_fft_frames_for_gain_comp (int): FFT frames used for gain
                computation (about 1000 recommended).

        Returns:
            None
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
            await self.compute_gains(target = gain_target,
                                     n_fft_frames = n_fft_frames_for_gain_comp,
                                     data_source = 'adc', # Always read real ADC with the observe method()
                                     channels = list(range(ADCS_PER_BOARD)), # Always read from all ADC channels
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

            with h5py.File(f'{self.digital_gains_path}/gains.hdf5', 'r') as digital_gains_file:
                if 'gain' in digital_gains_file:
                    # New format: a single floating-point gain value per bin.
                    gain = digital_gains_file['gain'][:]
                else:
                    # Backward compatibility: older files stored split linear / logarithmic gains.
                    if 'gain_lin' in digital_gains_file:
                        lin = digital_gains_file['gain_lin'][:]
                        log = digital_gains_file['gain_log'][:]
                    else:
                        lin = digital_gains_file['lin'][:]
                        log = digital_gains_file['log'][:]
                    gain = lin * 2.0**log

            # Write the gains in natural (ascending frequency) order; set_gain_table reorders internally.
            for n in range(self.NPOLS):
                scaler = self.i.chan[n].SCALER
                if scaler.USE_COMPLEX_GAINS != 0:
                    scaler.USE_COMPLEX_GAINS = 0
                if scaler.USE_FLOAT_GAINS != 1:
                    scaler.USE_FLOAT_GAINS = 1
                scaler.set_gain_table(gain[n], bank = 0, gain_timestamp = None, log_gain = None)

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
            await self.configure_corr(n_firmware_frames = n_firmware_frames,
                                      autocorr_only = autocorr_only,
                                      no_accum = no_accum,
                                      )

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

        # Calculate the integration time. firmware_integ_period is the actual number of frames the
        # correlator accumulates (cached by configure_corr from the receiver config).
        s_per_frame = self.M * self.sample_period # seconds per frame
        n_firmware_frames = self.firmware_integ_period
        integration_time = n_firmware_frames * n_software_frames * s_per_frame
        print(f'Integration time: {integration_time} s')

        # The product map self.prod was built by configure_corr().

        try:
            while True:

                # Create vis file
                file_name = f'{self.vis_dir_name}/{file_number}.hdf5'
                print('')
                print('+++++++++++++++++++++++++++++++++')
                print(f'Creating new data file \'{file_name}\'')
                f = h5py.File(file_name, 'w')

                # Create data sets
                dset_vis    = f.create_dataset('vis', (n_vis_per_file, self.NBINS, self.NPROD), dtype = np.complex128) # Create vis dataset
                dset_counts = f.create_dataset('counts', (n_vis_per_file), dtype = np.uint32) # Create counts dataset
                dset_sat    = f.create_dataset('sat', (n_vis_per_file, self.NBINS, self.NPROD), dtype = np.complex64) # Create sat dataset

                # Assign attributes to vis dataset:
                dset_vis.attrs['n_firmware_frames'] = n_firmware_frames # Number of firmware frames per visibility
                dset_vis.attrs['n_software_frames'] = n_software_frames # Number of software frames per visibility
                dset_vis.attrs['expected_packets'] = self.expected_packets # Expected correlator packets per result

                # Make index_map group
                index_map_grp = f.create_group('index_map')
                dset_freq = index_map_grp.create_dataset('freq', (self.NBINS, ), dtype = np.float64)
                dset_prod = index_map_grp.create_dataset('prod', (self.NPROD, 2), dtype = np.int16)
                # 'time' holds the rough UNIX timestamp (time.time(), in seconds) of each capture.
                dset_time = index_map_grp.create_dataset('time', (n_vis_per_file, ), dtype = np.float64)
                dset_good_bin_mask = index_map_grp.create_dataset('good_bin_mask', (self.NBINS, ), dtype = bool)

                dset_freq[:] = self.f
                dset_prod[:] = self.prod
                dset_good_bin_mask[:] = self.good_bin_mask

                # Make numpy buffers for index_map
                t_buf = np.zeros(n_vis_per_file, dtype = np.float64)
                vis_buf = np.zeros((n_vis_per_file, self.NBINS, self.NPROD), dtype = np.complex128)
                counts_buf = np.zeros(n_vis_per_file, dtype = np.uint32)
                sat_buf = np.zeros((n_vis_per_file, self.NBINS, self.NPROD), dtype = np.complex64)

                # Make statistics group
                stats_grp = f.create_group('stats')
                dset_FRAME_COUNT = stats_grp.create_dataset('frame_count', (n_vis_per_file, self.NPOLS), dtype = int)
                dset_ADC_OVERRANGE = stats_grp.create_dataset('adc_overrange', (n_vis_per_file, self.NPOLS), dtype = int)
                dset_ADC_OVERVOLTAGE = stats_grp.create_dataset('adc_overvoltage', (n_vis_per_file, self.NPOLS), dtype = int)
                dset_FFT_OVERFLOWS = stats_grp.create_dataset('fft_overflows', (n_vis_per_file, self.NPOLS), dtype = int) #
                dset_SCALER_OVERFLOWS = stats_grp.create_dataset('scaler_overflows', (n_vis_per_file, self.NPOLS), dtype = int)
                dset_SCALER_STATS_READY = stats_grp.create_dataset('scaler_stats_ready', (n_vis_per_file, self.NPOLS), dtype = int)
                dset_CORR_OVERRUN = stats_grp.create_dataset('corr_overrun', (n_vis_per_file, ), dtype = int)
                dset_UCAP_OVERRUN = stats_grp.create_dataset('ucap_overrun', (n_vis_per_file, ), dtype = int)
                dset_TEMPERATURE = stats_grp.create_dataset('temperature', (n_vis_per_file, ), dtype = np.float64)

                # Make numpy buffers for statistics
                frame_count_buf = np.zeros((n_vis_per_file, self.NPOLS), dtype = int)
                adc_overrange_buf = np.zeros((n_vis_per_file, self.NPOLS), dtype = int)
                adc_overvoltage_buf = np.zeros((n_vis_per_file, self.NPOLS), dtype = int)
                fft_overflows_buf = np.zeros((n_vis_per_file, self.NPOLS), dtype = int)
                scaler_overflows_buf = np.zeros((n_vis_per_file, self.NPOLS), dtype = int)
                scaler_stats_ready_buf = np.zeros((n_vis_per_file, self.NPOLS), dtype = int)
                corr_overrun_buf = np.zeros(n_vis_per_file, dtype = int)
                ucap_overrun_buf = np.zeros(n_vis_per_file, dtype = int)
                temperature_buf = np.zeros(n_vis_per_file, dtype = np.float64)

                # Define burst period if ADC/FFT are requested
                period = 0.2

                if capture_adc_bursts:
                    # Capture ADC data. Note: a single capture for all 8 pols in mode 0 is equal to
                    # 16384 samples per frames * 16 frames per capture * 16 bits per sample / 8 bits per byte / 1e6 B per MB = 0.524288 MB of data.

                    # Create data set
                    dset_adc = f.create_dataset('adc', (self.NPOLS, n_adc_frames_per_file, self.M), dtype = np.int16)

                    # Capture ADC frames
                    print(f'Capturing {n_adc_frames_per_file} ADC frames...')
                    d_adc, _ = await self.read_adc_frames(n_frames = n_adc_frames_per_file,
                                                          data_source = 'adc',
                                                          period = period,
                                                          split = True,
                                                          verbose = 0
                                                          )

                    print('Done!')

                    # Write to file
                    dset_adc[...] = d_adc

                    # Print the RMS power for each ADC
                    rms_power_dBm = self.adc_rms_power_dBm(d_adc)

                    print('')
                    print('ADC RMS power:')
                    for n in range(self.NPOLS):
                        print(f'ADC{n + 1}: {np.round(rms_power_dBm[n], 2)} dBm')

                if capture_fft_bursts:
                    # Capture FFT data. Note: a single capture for all 8 pols in mode 0 is equal to
                    # 16384 samples * 16 frames per capture * (32+32) bit per sample / 8 bits per byte / 1e6 B per MB = 2.097152 MB of data.

                    # Create data set
                    dset_fft = f.create_dataset('fft', (self.NPOLS, n_fft_frames_per_file, self.NBINS), dtype = np.complex128)

                    # Capture bursts
                    print('')
                    print(f'Capturing {n_fft_frames_per_file} FFT frames...')
                    # need to request twice the number of frames to actually get n_fft_frames_per_file with fft_bit_res = '32+32'
                    d_fft, _ = await self.read_fft_frames(n_frames = 2*n_fft_frames_per_file, 
                                                          fft_bit_res = '32+32',
                                                          data_source = 'adc',
                                                          period = period,
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

                    # ADC overrange/overvoltage flag reset (one bit per ADC).
                    self.reset_adc_flags()

                    for m in range(self.NPOLS):

                        # FFT stats reset
                        self.i.chan[m].FFT.OVERFLOW_RESET = 1
                        self.i.chan[m].FFT.OVERFLOW_RESET = 0

                        # Scaler stats reset
                        self.i.chan[m].SCALER.STATS_CAPTURE = 1

                    # Capture data, filling the NumPy buffer on each iteration, flush on first capture
                    flush = True if n == 0 else False

                    try:

                        # Capture correlated data (note that many default arguments are used here).
                        # raise_on_issue is left False so a single result with dropped packets does not
                        # abort the whole observation; packet loss is recorded via counts below.
                        t1, vis, counts, sat, _ = await self.read_corr_frames(data_source = 'adc',
                                                                              fft_bypass = False,
                                                                              scaler_bypass = False,
                                                                              n_software_frames = n_software_frames,
                                                                              number_of_results = 1,
                                                                              flush = flush,
                                                                              raise_on_issue = False,
                                                                              )

                    except RuntimeError as e:
                        print('Caught an error while capturing correlated data')
                        print(f'RuntimeError: {e}')

                        # Try capturing data again, this time flushing the buffer
                        t1, vis, counts, sat, _ = await self.read_corr_frames(data_source = 'adc',
                                                                              fft_bypass = False,
                                                                              scaler_bypass = False,
                                                                              n_software_frames = n_software_frames,
                                                                              number_of_results = 1,
                                                                              flush = True,
                                                                              raise_on_issue = False,
                                                                              )

                    # Each returned array has a leading results axis of length 1 (number_of_results).
                    # t1 is a single float (time.time() taken just before the capture).
                    t_buf[n] = t1
                    vis_buf[n] = vis[0]
                    counts_buf[n] = counts[0]
                    sat_buf[n] = sat[0]

                    # Capture statistics after recording correlator data
                    print('Capturing statistics')

                    # Read the ADC overrange / overvoltage flags (per-ADC, ascending order) using the
                    # shared helper so the decoding is consistent everywhere.
                    adc_or_arr, adc_ov_arr, adc_or_high, adc_ov_high = self.read_adc_flags(verbose = False)
                    adc_overrange_buf[n] = adc_or_arr
                    adc_overvoltage_buf[n] = adc_ov_arr

                    for m in range(self.NPOLS):

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

                    # Correlator and UCAP overrun stats
                    corr_overrun_buf[n] = self.corr.OVERRUN
                    ucap_overrun_buf[n] = self.check_ucap_overrun(raise_on_overrun = False)

                    # FPGA die temperature
                    temperature_buf[n] = self.i.get_temperatures()['FPGA_core']

                    # Print some statistics:
                    if adc_or_high:
                        print('ADC overranges detected:', [f'ADC{m + 1}' for m in range(self.NPOLS) if adc_or_arr[m] != 0])
                    else:
                        print('No ADC overranges detected')
                    if adc_ov_high:
                        print('ADC overvoltages detected:', [f'ADC{m + 1}' for m in range(self.NPOLS) if adc_ov_arr[m] != 0])
                    else:
                        print('No ADC overvoltages detected')
                    print(f'FFT Overflows: {fft_overflows_buf[n]}')
                    print(f'Scaler Overflows: {scaler_overflows_buf[n]}')
                    print(f'Correlator Overruns: {corr_overrun_buf[n]}')
                    print(f'UCAP Overruns: {ucap_overrun_buf[n]}')
                    print(f'FPGA Die Temperature: {temperature_buf[n]} C')

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
                dset_ADC_OVERRANGE[...] = adc_overrange_buf
                dset_ADC_OVERVOLTAGE[...] = adc_overvoltage_buf
                dset_FFT_OVERFLOWS[...] = fft_overflows_buf
                dset_SCALER_OVERFLOWS[...] = scaler_overflows_buf
                dset_SCALER_STATS_READY[...] = scaler_stats_ready_buf
                dset_CORR_OVERRUN[...] = corr_overrun_buf
                dset_UCAP_OVERRUN[...] = ucap_overrun_buf
                dset_TEMPERATURE[...] = temperature_buf

                print('Data written!')

                # Close file
                print(f'Closing file {file_name}')
                f.close()

                # Increment file number
                file_number += 1

        except (KeyboardInterrupt, asyncio.CancelledError):
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
            dset_ADC_OVERRANGE[...] = adc_overrange_buf
            dset_ADC_OVERVOLTAGE[...] = adc_overvoltage_buf
            dset_FFT_OVERFLOWS[...] = fft_overflows_buf
            dset_SCALER_OVERFLOWS[...] = scaler_overflows_buf
            dset_SCALER_STATS_READY[...] = scaler_stats_ready_buf
            dset_CORR_OVERRUN[...] = corr_overrun_buf
            dset_UCAP_OVERRUN[...] = ucap_overrun_buf
            dset_TEMPERATURE[...] = temperature_buf

            print('Done!')
            print('')
            print(f'Ending observation {self.obs_string}.')
            print('')

        finally:
            # Close the open file before ending
            f.close()
            
        return


def parse_args():
    """
    Parse command-line arguments for the pocket correlator CLI.

    Returns
    -------
    argparse.Namespace: the parsed arguments.
    """
    parser = argparse.ArgumentParser(description = 'Capture data from a single t0.CRS board.')

    # ====================
    # Connection arguments:

    parser.add_argument('--serial', type = str, required = True,
                        help = '(str) The serial number of the target hardware. The PC class is designed '
                        'to be used with a single t0.CRS board; therefore, when creating the FPGAArray instance, '
                        'the part \'crs\' is automatically assumed.'
                        )

    parser.add_argument('--stderr_log_level', type = str, default = 'info',
                        help = '(str) The level of logging printed to the screen when controlling the CRS board. Options are: '
                        '\'error\' (only shows errors), '
                        '\'warning\' (quiet, only shows warnings), '
                        '\'info\' (standard, provides key information), '
                        '\'debug\' (lowest-level, provides highest density of information).'
                        )

    parser.add_argument('--prog', type = int, default = 1,
                        help = '(int) Whether to force the FPGA to be reprogrammed when the script is re-run. '
                        'The FPGA will be programmed by default if the CRS board is power cycled. The options for prog are: '
                        '0: do not program the FPGA, '
                        '1: do not force the FPGA to be re-programmed (if, for instance, the script is re-run), '
                        '2: force the FPGA to be programmed.'
                        )

    # ====================
    # Observation arguments:

    parser.add_argument('--observe', action = 'store_true',
                        help = 'If set, run observe() to compute/load gains and record visibilities until Ctrl-C.'
                        )

    parser.add_argument('--data_path', type = str, default = None,
                        help = '(str) Parent directory for visibility HDF5 files. Required with --observe.'
                        )

    parser.add_argument('--obs_tag', type = str, default = '',
                        help = '(str) Suffix appended to the timestamped observation name.'
                        )

    parser.add_argument('--digital_gains_path', type = str, default = None,
                        help = '(str) Directory containing a gains.hdf5 file to load. If omitted, gains are '
                        'computed and saved under the data path.'
                        )

    return parser.parse_args()


async def main():
    """
    Command-line entry point. Constructs a POCKET_CORRELATOR and, if --observe is given, runs an
    observation until interrupted. Everything runs inside a single asyncio event loop; an instance
    created here cannot be reused after the loop closes.

    For interactive use, construct and await the class directly in IPython / Jupyter::

        pc = await POCKET_CORRELATOR('crs 0016', prog = 1)
    """
    args = parse_args()

    pc = await POCKET_CORRELATOR(hwm = f'crs {args.serial}',
                                 stderr_log_level = args.stderr_log_level,
                                 prog = int(args.prog),
                                 )

    if args.observe:
        if args.data_path is None:
            raise ValueError('--data_path is required when --observe is set.')
        await pc.observe(data_path = args.data_path,
                         obs_tag = args.obs_tag,
                         digital_gains_path = args.digital_gains_path,
                         )
    else:
        print('')
        print('Pocket correlator connected. --observe was not set, so no data was captured.')
        print('For interactive use, await the class directly in IPython / Jupyter, e.g.:')
        print("    pc = await POCKET_CORRELATOR('crs {}', prog = 1)".format(args.serial))


if __name__ == '__main__':
    asyncio.run(main())
