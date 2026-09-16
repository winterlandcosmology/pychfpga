#!/usr/bin/env python
"""
Periscope Correlator
====================

A real-time visualization tool for the N^2 FX correlator data products
streamed from a t0.CRS board. This application uses PyQt and pyqtgraph
to display live data, including auto and cross-correlations between
up to 8 antennas.
"""
import sys
import time
import numpy as np
from PyQt5 import QtWidgets, QtCore, QtGui
import pyqtgraph as pg
import queue
import socket
import threading
from dataclasses import dataclass, field
from typing import Optional, Dict, Callable
import os
from multiprocessing import shared_memory
try:
    from numba import jit, prange
    NUMBA_AVAILABLE = True
except ImportError:
    NUMBA_AVAILABLE = False
    print("Numba not available, using pure NumPy optimizations")
try:
    from scipy import signal
    from scipy.optimize import least_squares
    SCIPY_AVAILABLE = True
except ImportError:
    SCIPY_AVAILABLE = False
    print("Warning: scipy not found. Radar mode will not work.")

# For matplotlib 3D visualization
try:
    import matplotlib
    matplotlib.use('Qt5Agg')
    from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
    from matplotlib.figure import Figure
    from mpl_toolkits.mplot3d import Axes3D
    MATPLOTLIB_3D_AVAILABLE = True
except ImportError:
    MATPLOTLIB_3D_AVAILABLE = False
    print("Warning: matplotlib 3D not available. 3D radar visualization will be disabled.")

# Add the parent directory to the path to allow for package imports
sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..'))

from pychfpga.pocket_correlator import POCKET_CORRELATOR
from pychfpga.fpga_firmware.chfpga.x_engine.UCORR import UCorrFrameReceiver, NBINS_TOTAL

# --- Constants ---
NCHAN = 8
NPROD = NCHAN * (NCHAN + 1) // 2
DEFAULT_SERIAL = "0016"
DEFAULT_GAIN_TARGET = 2.1
DEFAULT_WATERFALL_LEN = 250
DEFAULT_FRAMES_TO_AVERAGE = 1
FS_HZ = 3_200_000_000.0  # 3.2 GHz
M_FACTOR = 2**14


class SymLogNorm:
    """
    Symmetric logarithmic normalization for data with positive and negative values.
    Linear in the range -C to C, logarithmic outside.
    """
    def __init__(self, vmin, vmax, C, base=10):
        """
        Parameters:
        - vmin, vmax: data range
        - C: crossover point (linear below, log above)
        - base: log base (default 10)
        """
        self.vmin = float(vmin)
        self.vmax = float(vmax)
        self.C = float(max(C, 1e-10))  # Prevent zero
        self.base = base
        self.log_base = np.log(base)
        
        # Calculate bounds in symlog space
        self.y_min = self._forward_scalar(self.vmin)
        self.y_max = self._forward_scalar(self.vmax)
        self.y_range = self.y_max - self.y_min
        
    def _forward_scalar(self, x):
        """Apply symlog transformation to a scalar."""
        return np.sign(x) * np.log(np.abs(x)/self.C + 1) / self.log_base
    
    def forward(self, x):
        """Transform data to normalized [0,1] range."""
        # Apply symlog transformation
        y = np.sign(x) * np.log(np.abs(x)/self.C + 1) / self.log_base
        
        # Map to [0,1] with proper handling of the range
        if self.y_range > 0:
            y_normalized = (y - self.y_min) / self.y_range
        else:
            y_normalized = np.ones_like(y) * 0.5
        
        return np.clip(y_normalized, 0, 1)
    
    def inverse(self, y_norm):
        """Transform normalized values back to data space."""
        # Unmap from [0,1]
        y = y_norm * self.y_range + self.y_min
        
        # Apply inverse symlog
        x = np.sign(y) * self.C * (np.power(self.base, np.abs(y)) - 1)
        return x


@dataclass
class CorrelatorConfig:
    """Configuration for the data processing worker."""
    n_firmware_frames: int = 16384
    gain_factor: Optional[np.ndarray] = None
    idx00: int = 0
    idx11: int = 0
    idx01: int = 0
    waterfall_len: int = DEFAULT_WATERFALL_LEN
    frames_to_average: int = DEFAULT_FRAMES_TO_AVERAGE

class LagSpectraComputer(threading.Thread):
    """
    A thread that computes lag spectra from visibility data using inverse FFT.
    Runs independently from the UDP receiver to avoid blocking data reception.
    """
    def __init__(self, shared_buffers: 'SharedDataBuffers'):
        super().__init__(daemon=True)
        self.shared_buffers = shared_buffers
        self.running = True
        self.compute_enabled = threading.Event()
        
        # Pre-allocate work buffers
        self.vis_p00_buffer = np.zeros(NBINS_TOTAL, dtype=np.float64)
        self.vis_p11_buffer = np.zeros(NBINS_TOTAL, dtype=np.float64)
        self.vis_p01_complex_buffer = np.zeros(NBINS_TOTAL, dtype=np.complex128)
        
        # IFFT output buffers
        self.lag_p00_buffer = np.zeros(NBINS_TOTAL, dtype=np.float64)
        self.lag_p11_buffer = np.zeros(NBINS_TOTAL, dtype=np.float64)
        self.lag_p01_complex_buffer = np.zeros(NBINS_TOTAL, dtype=np.complex128)
        
        # Performance tracking
        self.frames_processed = 0
        self.last_fps_time = time.time()
        self.fps = 0
        
        # Frame validation statistics
        self.total_frames_captured = 0
        self.valid_frames_processed = 0
        self.frames_skipped = 0
        self.last_stats_print_time = time.time()
        
    def set_compute_enabled(self, enabled: bool):
        """Enable or disable lag computation."""
        if enabled:
            self.compute_enabled.set()
        else:
            self.compute_enabled.clear()
    
    def run(self):
        """Main thread loop for computing lag spectra."""
        while self.running:
            # Wait for new data signal
            with self.shared_buffers.new_data_condition:
                self.shared_buffers.new_data_condition.wait(timeout=0.1)
                
            # Only compute if enabled
            if self.compute_enabled.is_set():
                self._compute_lag_spectra()
                    
    def _compute_lag_spectra(self):
        """Compute lag spectra from visibility data."""
        try:
            # Copy visibility data with minimal lock time
            with self.shared_buffers.lock:
                self.vis_p00_buffer[:] = self.shared_buffers.p00
                self.vis_p11_buffer[:] = self.shared_buffers.p11
                # Create complex visibility
                self.vis_p01_complex_buffer.real = self.shared_buffers.p01_real
                self.vis_p01_complex_buffer.imag = self.shared_buffers.p01_imag
                
            # Compute inverse FFTs (no lock needed)
            # For autocorrelations, take real part since input is real-valued in frequency domain
            lag_p00_complex = np.fft.ifft(self.vis_p00_buffer)
            lag_p11_complex = np.fft.ifft(self.vis_p11_buffer)
            
            # For autocorrelations, compute delay spectra (power of lag)
            # This is |IFFT|^2 which gives us the power at each delay
            self.lag_p00_buffer = np.abs(lag_p00_complex)**2
            self.lag_p11_buffer = np.abs(lag_p11_complex)**2
            
            # For cross-correlation, compute magnitude of complex lag
            self.lag_p01_complex_buffer = np.fft.ifft(self.vis_p01_complex_buffer)
            lag_p01_magnitude = np.abs(self.lag_p01_complex_buffer)**2  # Power spectrum
            
            # FFT shift to center zero lag
            self.lag_p00_buffer = np.fft.fftshift(self.lag_p00_buffer)
            self.lag_p11_buffer = np.fft.fftshift(self.lag_p11_buffer)
            lag_p01_magnitude = np.fft.fftshift(lag_p01_magnitude)
            
            # Update shared lag buffers
            with self.shared_buffers.lag_lock:
                self.shared_buffers.lag_p00[:] = self.lag_p00_buffer
                self.shared_buffers.lag_p11[:] = self.lag_p11_buffer
                self.shared_buffers.lag_p01_mag[:] = lag_p01_magnitude
                # Phase is not meaningful for delay spectra
                self.shared_buffers.lag_p01_phase[:] = 0  # Not used
                
                # Update lag waterfall - shift existing data down and insert new at top
                # Roll the arrays down by 1 to make room at position 0
                self.shared_buffers.lag_wf_p00[1:] = self.shared_buffers.lag_wf_p00[:-1]
                self.shared_buffers.lag_wf_p11[1:] = self.shared_buffers.lag_wf_p11[:-1]
                self.shared_buffers.lag_wf_p01_mag[1:] = self.shared_buffers.lag_wf_p01_mag[:-1]
                self.shared_buffers.lag_wf_p01_phase[1:] = self.shared_buffers.lag_wf_p01_phase[:-1]
                
                # Write new data at position 0 (top)
                self.shared_buffers.lag_wf_p00[0] = self.lag_p00_buffer
                self.shared_buffers.lag_wf_p11[0] = self.lag_p11_buffer
                self.shared_buffers.lag_wf_p01_mag[0] = lag_p01_magnitude
                self.shared_buffers.lag_wf_p01_phase[0] = 0  # Not used
                
                # Update frames written counter
                self.shared_buffers.lag_frames_written = min(
                    self.shared_buffers.lag_frames_written + 1, 
                    self.shared_buffers.waterfall_len
                )
                
            # Update performance metrics
            self.frames_processed += 1
            current_time = time.time()
            time_delta = current_time - self.last_fps_time
            if time_delta >= 1.0:
                self.fps = self.frames_processed / time_delta
                self.last_fps_time = current_time
                self.frames_processed = 0
                
        except Exception as e:
            print(f"Error in lag computation: {e}")
            
    def _reset_adc_flags(self):
        """Reset ADC overrange and overvoltage flags before data capture.""" 
        try:
            # Reset flags for next capture 
            self.pc.i.GPIO.ADC_OR_CLEAR = 2**8 - 1  # 255, i.e., 11111111 for all 8 pols
            self.pc.i.GPIO.ADC_OR_CLEAR = 0
            self.pc.i.GPIO.ADC_OV_CLEAR = 2**8 - 1  # 255, i.e., 11111111 for all 8 pols
            self.pc.i.GPIO.ADC_OV_CLEAR = 0
        except Exception as e:
            print(f"Error resetting ADC flags: {e}")

    def _check_adc_flags(self):
        """Check ADC overrange and overvoltage flags and update shared buffers."""
        
        # Helper array for reading individual bits of ADC_OR and ADC_OV flags
        bitmask = 2**np.arange(8) # 8-bit bitmask for isolating values of individual bits
        bitshifts = np.arange(0, 8)[::-1] # bitshifts to read values of bits as 1 or 0
                            
        try:
            # Check the ADC overrange and overvoltage flags 
            adc_or = self.pc.i.GPIO.ADC_OR
            adc_ov = self.pc.i.GPIO.ADC_OV

            # Create arrays to print the overrange/overflow status of individual ADCs,
            # reversing the array so that the ADCs are indexed in ascending order from ADC1 to ADC8
            adc_or_arr = np.array([(adc_or & bitmask[m]) >> m for m in bitshifts])[::-1]
            adc_ov_arr = np.array([(adc_ov & bitmask[m]) >> m for m in bitshifts])[::-1]

            # Update shared buffer counters
            self.shared_buffers.total_captures += 1
            
            # Count any overrange or overvoltage flags
            if adc_or != 0:
                print(f'ADC overranges detected:', [f'ADC{n + 1}' for n in range(8) if adc_or_arr[n] != 0])
                self.shared_buffers.adc_or_count += 1
            if adc_ov != 0:
                print(f'ADC overranges detected:', [f'ADC{n + 1}' for n in range(8) if adc_ov_arr[n] != 0])
                self.shared_buffers.adc_ov_count += 1
                
        except Exception as e:
            print(f"Error checking ADC flags: {e}")

    def stop(self):
        """Stop the thread."""
        self.running = False


class FineLagComputer(threading.Thread):
    """
    A thread that captures raw ADC data and computes fine-resolution lag spectra.
    Uses direct pocket_correlator calls to get raw ADC frames.
    """
    def __init__(self, pocket_correlator: POCKET_CORRELATOR, shared_buffers: 'SharedDataBuffers', config: CorrelatorConfig):
        super().__init__(daemon=True)
        self.pc = pocket_correlator
        self.shared_buffers = shared_buffers
        self.config = config
        self.running = True
        self.enabled = threading.Event()
        
        # Performance tracking
        self.frames_processed = 0
        self.last_fps_time = time.time()
        self.fps = 0
        
        # Frame validation statistics
        self.total_frames_captured = 0
        self.valid_frames_processed = 0
        self.frames_skipped = 0
        self.last_stats_print_time = time.time()
        
    def set_enabled(self, enabled: bool):
        """Enable or disable fine lag computation."""
        if enabled:
            self.enabled.set()
        else:
            self.enabled.clear()
    
    def run(self):
        """Main thread loop for computing fine lag spectra from raw ADC data."""
        # Initial setup - do this once before the loop
        self.pc.set_ucap_mode(mode=0)  # 8-channel capture (2 frames per channel)
        
        # Do initial capture with flush=True to set everything up
        # Always capture at least 2 frames (minimum for mode 0)
        n_frames_to_capture = max(2, self.config.frames_to_average)
        # Round up to nearest multiple of 2
        n_frames_to_capture = ((n_frames_to_capture + 1) // 2) * 2
        d_adc = self.pc.read_adc_frames(n_frames=n_frames_to_capture, split=True, verbose=0)
        
        # Debug: check which channels have data on first capture
        debug_printed = False
        
        while self.running:
            if not self.enabled.is_set():
                time.sleep(0.1)
                continue
                
            try:
                # Reset ADC flags before data capture
                self._reset_adc_flags()
                
                # Get current frames to average from config (dynamic update)
                n_frames_to_capture = max(2, self.config.frames_to_average)
                # Round up to nearest multiple of 2
                n_frames_to_capture = ((n_frames_to_capture + 1) // 2) * 2
                
                ncap = n_frames_to_capture // 2  # Number of captures needed
                
                # Use a small flush to ensure we're synchronized to a new timestamp
                # This is lighter than the full flush in read_adc_frames
                t, d_adc, c = self.pc.u_receiver.read_raw_frames(
                    format='16',
                    ncap=ncap,
                    split=True,
                    verbose=0,
                    flush=True,  # Light flush to ensure timestamp sync
                    flush_timeout=0.0001  # Very short flush, just to sync
                )
                
                # Check ADC flags after data capture and skip if flags detected
                if self._check_adc_flags():
                    # ADC flags detected, skip this data and continue to next iteration
                    continue
                
                # Shift to 14 bits (same as in read_adc_frames)
                d_adc = d_adc >> 2
                # d_adc shape: (8, n_frames, 16384)
                
                # Increment total frames captured counter
                self.total_frames_captured += 1
                
                # Try to find ADC1 at a different channel if channel 1 is empty
                adc1_ch_idx = 1  # Default assumption
                
                # Debug: Check which channels have data
                if not debug_printed:
                    print(f"Fine lag: Got ADC data shape: {d_adc.shape}")
                    for ch in range(d_adc.shape[0]):
                        ch_min = np.min(d_adc[ch])
                        ch_max = np.max(d_adc[ch])
                        ch_mean = np.mean(d_adc[ch])
                        print(f"  Channel {ch}: range=[{ch_min:.1f}, {ch_max:.1f}], mean={ch_mean:.1f}")
                    debug_printed = True
                
                # Process each frame separately and average for better SNR
                n_samples = 16384  # Samples per frame
                n_fft = n_samples  # Use same size - no zero padding
                n_lags = n_samples // 2  # Only positive lags (0 to 8191) for autocorrelations
                n_lags_cross = n_samples // 2  # For cross-correlation, we'll use fftshift to center around zero lag
                
                # Initialize accumulators for averaging
                lag_p00_sum = np.zeros(n_lags)
                lag_p11_sum = np.zeros(n_lags)
                lag_p01_sum = np.zeros(n_lags_cross, dtype=np.complex128)  # Complex for cross-correlation
                
                # Process only the requested number of frames for averaging
                # (we may have captured more to meet the 2-frame requirement)
                frames_to_process = min(self.config.frames_to_average, d_adc.shape[1])
                
                # Check for missing data within frames
                missing_data_detected = False
                frames_actually_processed = 0  # Track how many frames passed validation
                valid_frame_indices = []  # Track which frame indices passed validation
                
                for frame_idx in range(frames_to_process):
                    # Get data for this frame
                    adc0_frame = d_adc[0, frame_idx, :].astype(np.float64)  # 16384 samples
                    adc1_frame = d_adc[adc1_ch_idx, frame_idx, :].astype(np.float64)  # Use the correct channel for ADC1
                    
                    # Check for gaps/missing data (consecutive zeros)
                    # Look for runs of zeros that indicate missing data
                    zero_threshold = 1.0  # Values below this are considered missing
                    adc0_zeros = np.abs(adc0_frame) < zero_threshold
                    adc1_zeros = np.abs(adc1_frame) < zero_threshold
                    
                    # Count consecutive zeros with more sophisticated detection
                    def find_max_consecutive_zeros(zero_mask):
                        """Find the maximum length of consecutive zeros in the data."""
                        if not np.any(zero_mask):
                            return 0, []
                        # Find changes from non-zero to zero and vice versa
                        changes = np.diff(np.concatenate(([False], zero_mask, [False])).astype(int))
                        starts = np.where(changes == 1)[0]
                        ends = np.where(changes == -1)[0]
                        
                        if len(starts) > 0 and len(ends) > 0:
                            lengths = ends - starts
                            max_length = np.max(lengths)
                            # Also return all significant run lengths for debugging
                            significant_runs = lengths[lengths > 100]
                            return max_length, significant_runs
                        return 0, []
                    
                    adc0_max_zeros, adc0_runs = find_max_consecutive_zeros(adc0_zeros)
                    adc1_max_zeros, adc1_runs = find_max_consecutive_zeros(adc1_zeros)
                    
                    # New validation: reject frame if we find runs of zeros > 4000
                    # These are likely blocks of missing data that are multiples of 4096
                    critical_threshold = 4000
                    
                    if adc0_max_zeros > critical_threshold or adc1_max_zeros > critical_threshold:
                        # Reject this entire frame for both ADCs
                        self.frames_skipped += 1
                        
                        # Print detailed info
                        percent_zeros_adc0 = np.sum(adc0_zeros) / len(adc0_frame) * 100
                        percent_zeros_adc1 = np.sum(adc1_zeros) / len(adc1_frame) * 100
                        
                        print(f"Fine lag: REJECTING frame {self.total_frames_captured} due to large zero blocks:")
                        print(f"  ADC0: max run = {adc0_max_zeros} zeros ({percent_zeros_adc0:.1f}% zeros total)")
                        print(f"  ADC1: max run = {adc1_max_zeros} zeros ({percent_zeros_adc1:.1f}% zeros total)")
                        
                        # Show distribution of run lengths if there are multiple significant runs
                        if len(adc0_runs) > 0:
                            print(f"  ADC0 significant runs: {adc0_runs}")
                        if len(adc1_runs) > 0:
                            print(f"  ADC1 significant runs: {adc1_runs}")
                        
                        # Skip processing this frame entirely
                        continue
                    
                    # If we have smaller gaps (more than 100 consecutive zeros), just flag it
                    gap_threshold = 100
                    if adc0_max_zeros > gap_threshold or adc1_max_zeros > gap_threshold:
                        missing_data_detected = True
                        if self.frames_processed < 10 or self.frames_processed % 100 == 0:
                            percent_zeros_adc0 = np.sum(adc0_zeros) / len(adc0_frame) * 100
                            percent_zeros_adc1 = np.sum(adc1_zeros) / len(adc1_frame) * 100
                            print(f"Fine lag: Minor gaps in frame {self.total_frames_captured}, idx {frame_idx}:")
                            print(f"  ADC0: {adc0_max_zeros} consecutive zeros ({percent_zeros_adc0:.1f}% zeros total)")
                            print(f"  ADC1: {adc1_max_zeros} consecutive zeros ({percent_zeros_adc1:.1f}% zeros total)")
                    
                    # Remove DC offset to reduce artifacts
                    adc0_frame -= np.mean(adc0_frame)
                    adc1_frame -= np.mean(adc1_frame)
                    
                    # Apply Hann window to reduce spectral leakage
                    window = np.hanning(n_samples)
                    adc0_windowed = adc0_frame * window
                    adc1_windowed = adc1_frame * window
                    
                    # Compute FFTs
                    adc0_fft = np.fft.fft(adc0_windowed, n=n_fft)
                    adc1_fft = np.fft.fft(adc1_windowed, n=n_fft)
                    
                    # Compute power spectral density (PSD)
                    # This is the proper way to compute autocorrelation in frequency domain
                    psd0 = adc0_fft * np.conj(adc0_fft)
                    psd1 = adc1_fft * np.conj(adc1_fft)
                    
                    # Take IFFT to get autocorrelation in lag domain
                    # Only keep positive lags since autocorrelation is symmetric
                    lag_p00_complex = np.fft.ifft(psd0)[:n_lags]
                    lag_p11_complex = np.fft.ifft(psd1)[:n_lags]
                    
                    # Take real part (should be real for autocorrelation) and ensure positive
                    lag_p00_frame = np.abs(lag_p00_complex.real)
                    lag_p11_frame = np.abs(lag_p11_complex.real)
                    
                    # Compute cross-correlation - include both positive and negative lags
                    cross_spectrum = adc0_fft * np.conj(adc1_fft)
                    lag_p01_complex_full = np.fft.ifft(cross_spectrum)
                    # Apply fftshift to center zero lag and take middle n_lags_cross points
                    lag_p01_complex_shifted = np.fft.fftshift(lag_p01_complex_full)
                    # Take the middle portion centered around zero lag
                    start_idx = (n_samples - n_lags_cross) // 2
                    lag_p01_complex = lag_p01_complex_shifted[start_idx:start_idx + n_lags_cross]
                    
                    # Add to accumulator
                    lag_p00_sum += lag_p00_frame
                    lag_p11_sum += lag_p11_frame
                    lag_p01_sum += lag_p01_complex  # Keep complex for averaging
                    
                    # Increment counter of successfully processed frames
                    frames_actually_processed += 1
                    valid_frame_indices.append(frame_idx)  # Track this as a valid frame
                
                # Check if any frames were successfully processed
                if frames_actually_processed == 0:
                    # No valid frames in this capture, skip it entirely
                    if self.frames_processed < 10 or self.frames_processed % 50 == 0:
                        print(f"Fine lag: Skipping entire capture {self.total_frames_captured} - all frames rejected")
                    time.sleep(0.05)  # Small delay before trying next capture
                    continue
                
                # Average the results (using actual number of frames processed)
                lag_p00 = lag_p00_sum / frames_actually_processed
                lag_p11 = lag_p11_sum / frames_actually_processed
                lag_p01_complex_avg = lag_p01_sum / frames_actually_processed
                
                # Take magnitude of cross-correlation
                lag_p01 = np.abs(lag_p01_complex_avg)
                
                # Normalize by the zero-lag value for autocorrelations
                lag_p00 = lag_p00 / lag_p00[0]
                lag_p11 = lag_p11 / lag_p11[0]
                
                # For cross-correlation, normalize by sqrt of auto-correlation zero-lags
                # This gives correlation coefficient
                norm_factor = np.sqrt(lag_p00_sum[0] * lag_p11_sum[0]) / frames_to_process
                if norm_factor > 0:
                    lag_p01 = lag_p01 / norm_factor
                
                # Update shared buffers with lag data
                with self.shared_buffers.fine_lag_lock:
                    self.shared_buffers.fine_lag_p00[:] = lag_p00
                    self.shared_buffers.fine_lag_p11[:] = lag_p11
                    self.shared_buffers.fine_lag_p01[:] = lag_p01  # Cross-correlation magnitude
                    
                    # Store raw time domain data (concatenate frames for display)
                    # Limit to max frames that fit in the buffer
                    max_frames_for_display = 10  # Buffer is sized for 10 frames max
                    
                    # Only concatenate VALID frames that passed validation
                    valid_frames_to_display = valid_frame_indices[:max_frames_for_display]
                    n_valid_frames_to_display = len(valid_frames_to_display)
                    
                    if n_valid_frames_to_display > 0:
                        # Only concatenate the valid frames
                        adc0_display = np.concatenate([d_adc[0, idx, :] for idx in valid_frames_to_display])
                    else:
                        # No valid frames to display
                        adc0_display = np.zeros(16384)  # Show at least one frame of zeros
                        n_valid_frames_to_display = 1
                    
                    # Try different channel indices for ADC1
                    # Mode 0 might have ADC1 at a different index
                    adc1_ch_idx = 1  # Default assumption
                    
                    # Concatenate frames for ADC1 display (only valid frames)
                    if n_valid_frames_to_display > 0 and len(valid_frames_to_display) > 0:
                        adc1_display = np.concatenate([d_adc[adc1_ch_idx, idx, :] for idx in valid_frames_to_display])
                    else:
                        adc1_display = np.zeros(16384)  # Show at least one frame of zeros
                    
                    # Only copy as much data as we have from valid frames
                    n_samples_total = n_valid_frames_to_display * 16384
                    self.shared_buffers.fine_time_adc0[:n_samples_total] = adc0_display
                    self.shared_buffers.fine_time_adc1[:n_samples_total] = adc1_display
                    
                    # Store the actual number of samples for plotting
                    self.shared_buffers.fine_time_n_samples = n_samples_total
                    
                    # # Debug info about what frames are being displayed
                    # if self.frames_processed < 5 or self.frames_processed % 100 == 0:
                    #     print(f"Fine lag: Displaying {n_valid_frames_to_display} valid frames out of {frames_to_process} processed")
                    
                    # # Debug info about what's being displayed
                    # if self.frames_processed == 0:
                    #     print(f"Fine lag: Displaying all {n_frames_captured} captured frames ({n_samples_total} samples) in time domain")
                    
                    # Update waterfall - shift existing data down and insert new at top
                    # Roll the arrays down by 1 to make room at position 0
                    self.shared_buffers.fine_lag_wf_p00[1:] = self.shared_buffers.fine_lag_wf_p00[:-1]
                    self.shared_buffers.fine_lag_wf_p11[1:] = self.shared_buffers.fine_lag_wf_p11[:-1]
                    self.shared_buffers.fine_lag_wf_p01[1:] = self.shared_buffers.fine_lag_wf_p01[:-1]
                    
                    # Write new data at position 0 (top)
                    self.shared_buffers.fine_lag_wf_p00[0] = lag_p00
                    self.shared_buffers.fine_lag_wf_p11[0] = lag_p11
                    self.shared_buffers.fine_lag_wf_p01[0] = lag_p01  # Cross-correlation waterfall
                    
                    # Update frames written counter
                    self.shared_buffers.fine_lag_frames_written = min(
                        self.shared_buffers.fine_lag_frames_written + 1,
                        self.shared_buffers.waterfall_len
                    )
                
                # # Debug output
                # if self.frames_processed % 20 == 0:  # Print every 20 frames
                #     print(f"Fine lag: ADC0 max={np.max(lag_p00):.3f}, ADC1 max={np.max(lag_p11):.3f}, FPS={self.fps:.1f}")
                    
                # Update performance metrics
                self.frames_processed += 1
                current_time = time.time()
                time_delta = current_time - self.last_fps_time
                if time_delta >= 1.0:
                    self.fps = self.frames_processed / time_delta
                    self.last_fps_time = current_time
                    self.frames_processed = 0
                    
                # Small delay to avoid saturating (20 Hz update rate)
                time.sleep(0.05)
                
            except Exception as e:
                print(f"Error in fine lag computation: {e}")
                time.sleep(0.1)  # Wait a bit before retrying
                
    def _reset_adc_flags(self):
        """Reset ADC overrange and overvoltage flags before data capture."""
        try:
            # Reset flags for next capture 
            self.pc.i.GPIO.ADC_OR_CLEAR = 2**8 - 1  # 255, i.e., 11111111 for all 8 pols
            self.pc.i.GPIO.ADC_OR_CLEAR = 0
            self.pc.i.GPIO.ADC_OV_CLEAR = 2**8 - 1  # 255, i.e., 11111111 for all 8 pols
            self.pc.i.GPIO.ADC_OV_CLEAR = 0
        except Exception as e:
            print(f"Error resetting ADC flags: {e}")

    def _check_adc_flags(self):
        """Check ADC overrange and overvoltage flags and update shared buffers."""

        # Helper array for reading individual bits of ADC_OR and ADC_OV flags
        bitmask = 2**np.arange(8) # 8-bit bitmask for isolating values of individual bits
        bitshifts = np.arange(0, 8)[::-1] # bitshifts to read values of bits as 1 or 0
                            
        try:
            # Check the ADC overrange and overvoltage flags 
            adc_or = self.pc.i.GPIO.ADC_OR
            adc_ov = self.pc.i.GPIO.ADC_OV

            # Create arrays to print the overrange/overflow status of individual ADCs,
            # reversing the array so that the ADCs are indexed in ascending order from ADC1 to ADC8
            adc_or_arr = np.array([(adc_or & bitmask[m]) >> m for m in bitshifts])[::-1]
            adc_ov_arr = np.array([(adc_ov & bitmask[m]) >> m for m in bitshifts])[::-1]

            # Update shared buffer counters
            self.shared_buffers.total_captures += 1
            
            # Count any overrange or overvoltage flags
            if adc_or != 0:
                print(f'ADC overranges detected:', [f'ADC{n + 1}' for n in range(8) if adc_or_arr[n] != 0])
                self.shared_buffers.adc_or_count += 1
            if adc_ov != 0:
                print(f'ADC overranges detected:', [f'ADC{n + 1}' for n in range(8) if adc_ov_arr[n] != 0])
                self.shared_buffers.adc_ov_count += 1
                
        except Exception as e:
            print(f"Error checking ADC flags: {e}")

    def stop(self):
        """Stop the thread."""
        self.running = False


class RadarComputer(threading.Thread):
    """
    A thread that captures raw ADC data from 4 channels and performs 3D radar localization.
    Uses ucap_mode=0 for 4-channel capture.
    """
    def __init__(self, pocket_correlator: POCKET_CORRELATOR, shared_buffers: 'SharedDataBuffers', config: CorrelatorConfig):
        super().__init__(daemon=True)
        self.pc = pocket_correlator
        self.shared_buffers = shared_buffers
        self.config = config
        self.running = True
        self.enabled = threading.Event()
        
        # Radar parameters
        self.c = 3e8  # Speed of light
        self.x01 = 1.0  # Antenna spacing ADC0-ADC1 (meters)
        self.x02 = 1.0  # Antenna spacing ADC0-ADC2 (meters)
        self.significance_threshold = 0.75  # Default threshold
        
        # Antenna positions (L-shaped array)
        self.ant_pos = np.array([
            [0, 0, 0],      # ADC0 at origin
            [self.x01, 0, 0],    # ADC1 along x-axis
            [0, self.x02, 0]     # ADC2 along y-axis
        ])
        
        # Performance tracking
        self.frames_processed = 0
        self.last_fps_time = time.time()
        self.fps = 0
        
    def set_enabled(self, enabled: bool):
        """Enable or disable radar computation."""
        if enabled:
            self.enabled.set()
        else:
            self.enabled.clear()
    
    def set_threshold(self, threshold: float):
        """Update significance threshold."""
        self.significance_threshold = threshold
    
    # Radar processing functions extracted from radar_test.py
    def compute_lag_spectrum(self, sig1, sig2, fs):
        """Compute cross-correlation lag spectrum between two signals."""
        X1 = np.fft.fft(sig1)
        X2 = np.fft.fft(sig2)
        cross_spectrum = np.conj(X1) * X2
        lag_spectrum = np.fft.ifft(cross_spectrum)
        lag_spectrum = np.fft.fftshift(lag_spectrum)
        n = len(sig1)
        lags = np.arange(-n//2, n//2) / fs
        return lags, lag_spectrum

    def extract_delay_with_phase(self, lags, lag_spectrum, peak_idx):
        """
        Extract precise delay using centroid method around the peak.
        Returns the calculated delay, and the start/end indices of the centroid window.
        """
        coarse_delay = lags[peak_idx]
        magnitude = np.abs(lag_spectrum)
        
        # Use a fixed window size around the peak (e.g., ±15 samples)
        window_half_size = 15
        start = max(0, peak_idx - window_half_size)
        end = min(len(magnitude), peak_idx + window_half_size + 1)
        
        # Extract windowed data
        lags_window = lags[start:end]
        mag_window = magnitude[start:end]
        
        # Calculate centroid using all points in the window
        weighted_sum = np.sum(lags_window * mag_window)
        weight_total = np.sum(mag_window)
        
        if weight_total > 0:
            delay = weighted_sum / weight_total
        else:
            delay = coarse_delay
            
        return delay, start, end

    def find_single_peak(self, lags, lag_spectrum):
        """
        Find the strongest peak in lag spectrum.
        Returns the delay, and centroid window start/end.
        """
        magnitude = np.abs(lag_spectrum)
        
        # Optional: Apply simple smoothing with moving average (window size 5)
        window_size = 5
        if len(magnitude) > window_size:
            # Simple moving average
            smoothed = np.convolve(magnitude, np.ones(window_size)/window_size, mode='same')
            # Use smoothed data for peak finding
            magnitude_for_peak = smoothed
        else:
            magnitude_for_peak = magnitude
        
        # Simply find the index of the maximum value
        peak_idx = np.argmax(magnitude_for_peak)
        
        # Extract precise delay and centroid window
        delay, centroid_start, centroid_end = self.extract_delay_with_phase(lags, lag_spectrum, peak_idx)
        return delay, centroid_start, centroid_end

    def trilaterate_3d(self, ant_positions, ranges):
        """Find 3D position given ranges from three antennas."""
        if not SCIPY_AVAILABLE:
            # Simple geometric solution without scipy
            # This is a simplified approach - not as accurate as least squares
            x1, y1, z1 = ant_positions[0]
            x2, y2, z2 = ant_positions[1]
            x3, y3, z3 = ant_positions[2]
            r1, r2, r3 = ranges
            
            # Simplified calculation (assuming z=0 for now)
            A = 2*x2 - 2*x1
            B = 2*y2 - 2*y1
            C = r1**2 - r2**2 - x1**2 + x2**2 - y1**2 + y2**2
            
            D = 2*x3 - 2*x1
            E = 2*y3 - 2*y1
            F = r1**2 - r3**2 - x1**2 + x3**2 - y1**2 + y3**2
            
            try:
                x = (C*E - F*B) / (E*A - B*D)
                y = (C*D - A*F) / (B*D - A*E)
                z = np.sqrt(max(0, r1**2 - (x-x1)**2 - (y-y1)**2))
                return np.array([x, y, z])
            except:
                return np.array([5.0, 0.0, 5.0])  # Default position
        else:
            # Use scipy least squares for accurate solution
            def residuals(p):
                x, y, z = p
                res = []
                for i in range(3):
                    predicted_range = np.sqrt(
                        (x - ant_positions[i][0])**2 + 
                        (y - ant_positions[i][1])**2 + 
                        (z - ant_positions[i][2])**2
                    )
                    res.append(predicted_range - ranges[i])
                return res
            
            # Initial guess
            x0 = [np.mean([p[0] for p in ant_positions]),
                  np.mean([p[1] for p in ant_positions]),
                  5.0]
            
            # Solve
            result = least_squares(residuals, x0)
            
            if result.success and result.cost < 0.1:
                return result.x
            else:
                # Try with negative z
                x0[2] = -5.0
                result2 = least_squares(residuals, x0)
                if result2.success and result2.cost < result.cost:
                    return result2.x
                return result.x
    
    def process_radar_frame(self, adc0, adc1, adc2, adc3, fs):
        """
        Process one frame of radar data to find emitter position.
        Only updates shared buffers on a successful detection.
        """
        # Step 1: Compute lag spectra and find peaks for all correlations
        # Store results locally before committing to shared memory
        local_delays = {}
        local_lag_spectra = {}
        local_centroid_starts = {}
        local_centroid_ends = {}
        
        # Emitter (ADC3) to antenna correlations (for absolute ranging)
        for i, adc in enumerate([adc0, adc1, adc2]):
            lags, lag_spectrum = self.compute_lag_spectrum(adc3, adc, fs)
            
            # Store magnitude of lag spectrum locally
            local_lag_spectra[i] = np.abs(lag_spectrum)
            
            # Find peak and get delay and centroid info
            delay, centroid_start, centroid_end = self.find_single_peak(lags, lag_spectrum)
            
            if delay is not None:
                local_delays[('emitter', i)] = delay
                local_centroid_starts[i] = centroid_start
                local_centroid_ends[i] = centroid_end
        
        # Check if we have enough detections
        if len(local_delays) < 3:
            return None  # Need at least 3 for trilateration
        
        # Step 2: Extract delays for trilateration
        emitter_delays = {}
        for i in range(3):
            key = ('emitter', i)
            if key in local_delays:
                emitter_delays[i] = local_delays[key]
        
        if len(emitter_delays) != 3:
            return None  # Need exactly 3 measurements
        
        # Step 3: Convert to ranges and trilaterate
        ranges = [self.c * abs(delay) for delay in emitter_delays.values()]
        
        try:
            pos = self.trilaterate_3d(self.ant_pos, ranges)
            x, y, z = pos
            r = np.sqrt(x**2 + y**2 + z**2)
            
            if r > 0 and r < 100:  # Reasonable range check
                azimuth = np.degrees(np.arctan2(y, x))
                elevation = np.degrees(np.arcsin(z / r))
                
                # --- SUCCESS: Update shared buffers with all data ---
                with self.shared_buffers.radar_lock:
                    # Update lag spectra for debug plots
                    if 0 in local_lag_spectra:
                        self.shared_buffers.radar_lag_adc0[:] = local_lag_spectra[0]
                    if 1 in local_lag_spectra:
                        self.shared_buffers.radar_lag_adc1[:] = local_lag_spectra[1]
                    if 2 in local_lag_spectra:
                        self.shared_buffers.radar_lag_adc2[:] = local_lag_spectra[2]
                        
                    # Update delays and centroid windows
                    for i in range(3):
                        self.shared_buffers.radar_delays[i] = emitter_delays.get(i, 0)
                        self.shared_buffers.radar_centroid_starts[i] = local_centroid_starts.get(i, 0)
                        self.shared_buffers.radar_centroid_ends[i] = local_centroid_ends.get(i, 0)
                
                return {
                    'range_m': r,
                    'azimuth_deg': azimuth,
                    'elevation_deg': elevation,
                    'x': x,
                    'y': y,
                    'z': z
                }
        except Exception as e:
            print(f"Radar trilateration failed: {e}")
        
        return None
    
    def run(self):
        """Main thread loop for radar processing."""
        # Initial setup
        self.pc.set_ucap_mode(mode=0)  # 4-channel capture
        
        # Do initial capture
        n_frames = max(2, self.config.frames_to_average)
        n_frames = ((n_frames + 1) // 2) * 2  # Round to multiple of 2
        d_adc = self.pc.read_adc_frames(n_frames=n_frames, split=True, verbose=0)
        
        while self.running:
            if not self.enabled.is_set():
                time.sleep(0.1)
                continue
                
            try:
                # Reset ADC flags before data capture
                self._reset_adc_flags()
                
                # Capture ADC data
                n_frames = max(2, self.config.frames_to_average)
                n_frames = ((n_frames + 1) // 2) * 2
                ncap = n_frames // 2
                
                t, d_adc, c = self.pc.u_receiver.read_raw_frames(
                    format='16',
                    ncap=ncap,
                    split=True,
                    verbose=0,
                    flush=True,
                    flush_timeout=0.0001
                )
                
                # Check ADC flags after data capture and skip if flags detected
                if self._check_adc_flags():
                    # ADC flags detected, skip this data and continue to next iteration
                    continue
                
                # Shift to 14 bits
                d_adc = d_adc >> 2
                # d_adc shape: (8, n_frames, 16384)
                # We need channels 0,1,2,3 for ADC0,1,2,3
                
                # Process each frame and average results
                detections = []
                
                for frame_idx in range(min(self.config.frames_to_average, d_adc.shape[1])):
                    # Extract data for this frame
                    adc0_frame = d_adc[0, frame_idx, :].astype(np.float64)
                    adc1_frame = d_adc[1, frame_idx, :].astype(np.float64)
                    adc2_frame = d_adc[2, frame_idx, :].astype(np.float64)
                    adc3_frame = d_adc[3, frame_idx, :].astype(np.float64)
                    
                    # Remove DC offset
                    adc0_frame -= np.mean(adc0_frame)
                    adc1_frame -= np.mean(adc1_frame)
                    adc2_frame -= np.mean(adc2_frame)
                    adc3_frame -= np.mean(adc3_frame)
                    
                    # Process frame
                    result = self.process_radar_frame(adc0_frame, adc1_frame, adc2_frame, adc3_frame, FS_HZ)
                    if result:
                        detections.append(result)
                
                # Average detections if we have multiple
                if detections:
                    avg_range = np.mean([d['range_m'] for d in detections])
                    avg_azimuth = np.mean([d['azimuth_deg'] for d in detections])
                    avg_elevation = np.mean([d['elevation_deg'] for d in detections])
                    
                    # Update shared buffers - only update position values on valid detection
                    with self.shared_buffers.radar_lock:
                        # Update position values only when we have a valid detection
                        self.shared_buffers.radar_range[0] = avg_range
                        self.shared_buffers.radar_azimuth[0] = avg_azimuth
                        self.shared_buffers.radar_elevation[0] = avg_elevation
                        self.shared_buffers.radar_detection_valid[0] = True
                        
                        # Update history (shift and add new)
                        self.shared_buffers.radar_history_range[1:] = self.shared_buffers.radar_history_range[:-1]
                        self.shared_buffers.radar_history_azimuth[1:] = self.shared_buffers.radar_history_azimuth[:-1]
                        self.shared_buffers.radar_history_elevation[1:] = self.shared_buffers.radar_history_elevation[:-1]
                        
                        self.shared_buffers.radar_history_range[0] = avg_range
                        self.shared_buffers.radar_history_azimuth[0] = avg_azimuth
                        self.shared_buffers.radar_history_elevation[0] = avg_elevation
                        
                        self.shared_buffers.radar_history_count = min(
                            self.shared_buffers.radar_history_count + 1,
                            self.shared_buffers.radar_history_len
                        )
                else:
                    # No detection - only update validity flag, keep last valid position values
                    with self.shared_buffers.radar_lock:
                        # Do NOT update radar_range/azimuth/elevation - hold last valid values
                        self.shared_buffers.radar_detection_valid[0] = False
                        # Debug: print that we're holding last valid values
                        if self.frames_processed % 50 == 0:  # Print occasionally
                            print(f"Radar: No detection, holding last valid values: "
                                  f"Range={self.shared_buffers.radar_range[0]:.2f}m, "
                                  f"Az={self.shared_buffers.radar_azimuth[0]:.1f}°, "
                                  f"El={self.shared_buffers.radar_elevation[0]:.1f}°")
                
                # Update performance metrics
                self.frames_processed += 1
                current_time = time.time()
                time_delta = current_time - self.last_fps_time
                if time_delta >= 1.0:
                    self.fps = self.frames_processed / time_delta
                    self.last_fps_time = current_time
                    self.frames_processed = 0
                    
                # Small delay (20 Hz update rate)
                time.sleep(0.05)
                
            except Exception as e:
                print(f"Error in radar computation: {e}")
                time.sleep(0.1)

    def _reset_adc_flags(self):
        """Reset ADC overrange and overvoltage flags before data capture."""
        try:
            # Reset flags for next capture 
            self.pc.i.GPIO.ADC_OR_CLEAR = 2**8 - 1  # 255, i.e., 11111111 for all 8 pols
            self.pc.i.GPIO.ADC_OR_CLEAR = 0
            self.pc.i.GPIO.ADC_OV_CLEAR = 2**8 - 1  # 255, i.e., 11111111 for all 8 pols
            self.pc.i.GPIO.ADC_OV_CLEAR = 0
        except Exception as e:
            print(f"Error resetting ADC flags: {e}")

    def _check_adc_flags(self):
        """Check ADC overrange and overvoltage flags and update shared buffers."""
        
        # Helper array for reading individual bits of ADC_OR and ADC_OV flags
        bitmask = 2**np.arange(8) # 8-bit bitmask for isolating values of individual bits
        bitshifts = np.arange(0, 8)[::-1] # bitshifts to read values of bits as 1 or 0
                            
        try:
            # Check the ADC overrange and overvoltage flags 
            adc_or = self.pc.i.GPIO.ADC_OR
            adc_ov = self.pc.i.GPIO.ADC_OV

            # Create arrays to print the overrange/overflow status of individual ADCs,
            # reversing the array so that the ADCs are indexed in ascending order from ADC1 to ADC8
            adc_or_arr = np.array([(adc_or & bitmask[m]) >> m for m in bitshifts])[::-1]
            adc_ov_arr = np.array([(adc_ov & bitmask[m]) >> m for m in bitshifts])[::-1]

            # Update shared buffer counters
            self.shared_buffers.total_captures += 1
            
            # Count any overrange or overvoltage flags
            if adc_or != 0:
                print(f'ADC overranges detected:', [f'ADC{n + 1}' for n in range(8) if adc_or_arr[n] != 0])
                self.shared_buffers.adc_or_count += 1
            if adc_ov != 0:
                print(f'ADC overranges detected:', [f'ADC{n + 1}' for n in range(8) if adc_ov_arr[n] != 0])
                self.shared_buffers.adc_ov_count += 1
                
        except Exception as e:
            print(f"Error checking ADC flags: {e}")
                
    def stop(self):
        """Stop the thread."""
        self.running = False


class SuperFFTComputer(threading.Thread):
    """
    A thread that captures 16 contiguous frames from ADC0 and computes super-resolution FFT.
    Uses ucap_mode=3 for high-resolution spectral analysis.
    """
    def __init__(self, pocket_correlator: POCKET_CORRELATOR, shared_buffers: 'SharedDataBuffers'):
        super().__init__(daemon=True)
        self.pc = pocket_correlator
        self.shared_buffers = shared_buffers
        self.running = True
        self.enabled = threading.Event()
        
        # FFT parameters
        self.n_frames = 16
        self.samples_per_frame = 16384
        self.total_samples = self.n_frames * self.samples_per_frame  # 262,144
        self.n_fft_bins = self.total_samples // 2 + 1  # 131,073 for rfft
        
        # Pre-compute Hanning window
        self.window = np.hanning(self.total_samples)
        
        # Performance tracking
        self.frames_processed = 0
        self.last_fps_time = time.time()
        self.fps = 0
        
    def set_enabled(self, enabled: bool):
        """Enable or disable super FFT computation."""
        if enabled:
            self.enabled.set()
        else:
            self.enabled.clear()
    
    def run(self):
        """Main thread loop for computing super-resolution FFT from raw ADC data."""
        # Initial setup - do this once before the loop
        self.pc.set_ucap_mode(mode=3)
        
        # Do initial capture with flush=True to set everything up
        d_adc = self.pc.read_adc_frames(self.n_frames)
        
        # Track packet loss statistics
        self.total_captures = 0
        self.successful_captures = 0
        self.packet_loss_events = 0
        
        while self.running:
            if not self.enabled.is_set():
                time.sleep(0.1)
                continue
                
            try:
                # Small delay to avoid overwhelming the receiver
                #time.sleep(0.02)  # 20ms between captures to reduce packet loss

                # Reset ADC flags before data capture
                self._reset_adc_flags()
                
                # For subsequent frames, use read_raw_frames directly
                # Use minimal flush to ensure we're synchronized
                t, d_adc, c = self.pc.u_receiver.read_raw_frames(
                    format='16',
                    ncap=1,  # In mode 3, we get 16 frames per capture
                    split=True,
                    verbose=0,
                    flush=True,  # Minimal flush to ensure timestamp sync
                    flush_timeout=0.0001  # Very short flush
                )

                # Check ADC flags after data capture and skip if flags detected
                if self._check_adc_flags():
                    # ADC flags detected, skip this data and continue to next iteration
                    continue
                
                self.total_captures += 1
                
                # Check if we got all the expected packets
                # In mode 3, we expect 16 frames * 4 packets/frame = 64 packets total
                expected_packets = 16 * 4  # 64 packets for mode 3
                if c[0] < expected_packets:
                    self.packet_loss_events += 1
                    missing = expected_packets - c[0]
                    loss_rate = (self.packet_loss_events / self.total_captures) * 100
                    print(f"SuperFFT: Missing {missing} packets! Got {c[0]}/{expected_packets} (Loss rate: {loss_rate:.1f}%)")
                    # Skip this frame if we're missing packets
                    #time.sleep(0.1)  # Wait longer after packet loss
                    continue
                
                self.successful_captures += 1
                
                # Shift to 14 bits (same as in read_adc_frames)
                d_adc = d_adc >> 2
                
                # d_adc shape should be (1, 16, 16384) - only ADC0
                
                # Concatenate all frames into one long time series
                adc0_data = d_adc[0].flatten().astype(np.float64)  # 262,144 samples
                
                # Remove DC offset
                adc0_data -= np.mean(adc0_data)
                
                # Store raw time domain data for visualization
                with self.shared_buffers.super_fft_lock:
                    self.shared_buffers.super_fft_time[:] = adc0_data
                
                # Apply Hanning window to reduce spectral leakage
                windowed_data = adc0_data * self.window
                
                # Compute FFT (real-valued input, so use rfft)
                fft_data = np.fft.rfft(windowed_data)
                
                # Compute power spectral density
                psd = np.abs(fft_data)**2
                
                # Normalize by window power for proper scaling
                window_power = np.sum(self.window**2)
                psd = psd / window_power
                
                # Update shared buffers with PSD data
                with self.shared_buffers.super_fft_lock:
                    self.shared_buffers.super_fft_psd[:] = psd
                    
                    # Update waterfall - shift existing data down and insert new at top
                    # Roll the arrays down by 1 to make room at position 0
                    self.shared_buffers.super_fft_wf_psd[1:] = self.shared_buffers.super_fft_wf_psd[:-1]
                    
                    # Write new data at position 0 (top)
                    self.shared_buffers.super_fft_wf_psd[0] = psd
                    
                    # Update frames written counter
                    self.shared_buffers.super_fft_frames_written = min(
                        self.shared_buffers.super_fft_frames_written + 1,
                        self.shared_buffers.waterfall_len
                    )
                
                # Update performance metrics
                self.frames_processed += 1
                current_time = time.time()
                time_delta = current_time - self.last_fps_time
                if time_delta >= 1.0:
                    self.fps = self.frames_processed / time_delta
                    self.last_fps_time = current_time
                    self.frames_processed = 0
                    
                # Small delay to avoid saturating (10 Hz update rate)
                time.sleep(0.1)
                
            except Exception as e:
                print(f"Error in super FFT computation: {e}")
                time.sleep(0.1)  # Wait a bit before retrying

    def _reset_adc_flags(self):
        """Reset ADC overrange and overvoltage flags before data capture."""
        try:
            # Reset flags for next capture 
            self.pc.i.GPIO.ADC_OR_CLEAR = 2**8 - 1  # 255, i.e., 11111111 for all 8 pols
            self.pc.i.GPIO.ADC_OR_CLEAR = 0
            self.pc.i.GPIO.ADC_OV_CLEAR = 2**8 - 1  # 255, i.e., 11111111 for all 8 pols
            self.pc.i.GPIO.ADC_OV_CLEAR = 0
        except Exception as e:
            print(f"Error resetting ADC flags: {e}")

    def _check_adc_flags(self):
        """Check ADC overrange and overvoltage flags and update shared buffers."""
        
        # Helper array for reading individual bits of ADC_OR and ADC_OV flags
        bitmask = 2**np.arange(8) # 8-bit bitmask for isolating values of individual bits
        bitshifts = np.arange(0, 8)[::-1] # bitshifts to read values of bits as 1 or 0
                            
        try:
            # Check the ADC overrange and overvoltage flags 
            adc_or = self.pc.i.GPIO.ADC_OR
            adc_ov = self.pc.i.GPIO.ADC_OV

            # Create arrays to print the overrange/overflow status of individual ADCs,
            # reversing the array so that the ADCs are indexed in ascending order from ADC1 to ADC8
            adc_or_arr = np.array([(adc_or & bitmask[m]) >> m for m in bitshifts])[::-1]
            adc_ov_arr = np.array([(adc_ov & bitmask[m]) >> m for m in bitshifts])[::-1]

            # Update shared buffer counters
            self.shared_buffers.total_captures += 1
            
            # Count any overrange or overvoltage flags
            if adc_or != 0:
                print(f'ADC overranges detected:', [f'ADC{n + 1}' for n in range(8) if adc_or_arr[n] != 0])
                self.shared_buffers.adc_or_count += 1
            if adc_ov != 0:
                print(f'ADC overranges detected:', [f'ADC{n + 1}' for n in range(8) if adc_ov_arr[n] != 0])
                self.shared_buffers.adc_ov_count += 1
                
        except Exception as e:
            print(f"Error checking ADC flags: {e}")
                
    def stop(self):
        """Stop the thread."""
        self.running = False


class SharedDataBuffers:
    """Manages shared memory buffers for zero-copy data transfer."""
    def __init__(self, waterfall_len: int):
        self.waterfall_len = waterfall_len
        
        # ADC flag tracking
        self.total_captures = 0
        self.adc_or_count = 0
        self.adc_ov_count = 0
        
        # Create shared memory for current frame data
        self.shm_p00 = shared_memory.SharedMemory(create=True, size=NBINS_TOTAL * 8)
        self.shm_p11 = shared_memory.SharedMemory(create=True, size=NBINS_TOTAL * 8)
        self.shm_p01_real = shared_memory.SharedMemory(create=True, size=NBINS_TOTAL * 8)
        self.shm_p01_imag = shared_memory.SharedMemory(create=True, size=NBINS_TOTAL * 8)
        
        # Create shared memory for waterfall data
        wf_size = waterfall_len * NBINS_TOTAL * 8
        self.shm_wf_p00 = shared_memory.SharedMemory(create=True, size=wf_size)
        self.shm_wf_p11 = shared_memory.SharedMemory(create=True, size=wf_size)
        self.shm_wf_p01_real = shared_memory.SharedMemory(create=True, size=wf_size)
        self.shm_wf_p01_imag = shared_memory.SharedMemory(create=True, size=wf_size)
        
        # Create shared memory for lag spectra data
        self.shm_lag_p00 = shared_memory.SharedMemory(create=True, size=NBINS_TOTAL * 8)
        self.shm_lag_p11 = shared_memory.SharedMemory(create=True, size=NBINS_TOTAL * 8)
        self.shm_lag_p01_mag = shared_memory.SharedMemory(create=True, size=NBINS_TOTAL * 8)
        self.shm_lag_p01_phase = shared_memory.SharedMemory(create=True, size=NBINS_TOTAL * 8)
        
        # Create shared memory for lag waterfall data
        self.shm_lag_wf_p00 = shared_memory.SharedMemory(create=True, size=wf_size)
        self.shm_lag_wf_p11 = shared_memory.SharedMemory(create=True, size=wf_size)
        self.shm_lag_wf_p01_mag = shared_memory.SharedMemory(create=True, size=wf_size)
        self.shm_lag_wf_p01_phase = shared_memory.SharedMemory(create=True, size=wf_size)
        
        # Create NumPy arrays backed by shared memory
        self.p00 = np.ndarray((NBINS_TOTAL,), dtype=np.float64, buffer=self.shm_p00.buf)
        self.p11 = np.ndarray((NBINS_TOTAL,), dtype=np.float64, buffer=self.shm_p11.buf)
        self.p01_real = np.ndarray((NBINS_TOTAL,), dtype=np.float64, buffer=self.shm_p01_real.buf)
        self.p01_imag = np.ndarray((NBINS_TOTAL,), dtype=np.float64, buffer=self.shm_p01_imag.buf)
        
        self.wf_p00 = np.ndarray((waterfall_len, NBINS_TOTAL), dtype=np.float64, buffer=self.shm_wf_p00.buf)
        self.wf_p11 = np.ndarray((waterfall_len, NBINS_TOTAL), dtype=np.float64, buffer=self.shm_wf_p11.buf)
        self.wf_p01_real = np.ndarray((waterfall_len, NBINS_TOTAL), dtype=np.float64, buffer=self.shm_wf_p01_real.buf)
        self.wf_p01_imag = np.ndarray((waterfall_len, NBINS_TOTAL), dtype=np.float64, buffer=self.shm_wf_p01_imag.buf)
        
        # Create NumPy arrays for lag spectra
        self.lag_p00 = np.ndarray((NBINS_TOTAL,), dtype=np.float64, buffer=self.shm_lag_p00.buf)
        self.lag_p11 = np.ndarray((NBINS_TOTAL,), dtype=np.float64, buffer=self.shm_lag_p11.buf)
        self.lag_p01_mag = np.ndarray((NBINS_TOTAL,), dtype=np.float64, buffer=self.shm_lag_p01_mag.buf)
        self.lag_p01_phase = np.ndarray((NBINS_TOTAL,), dtype=np.float64, buffer=self.shm_lag_p01_phase.buf)
        
        self.lag_wf_p00 = np.ndarray((waterfall_len, NBINS_TOTAL), dtype=np.float64, buffer=self.shm_lag_wf_p00.buf)
        self.lag_wf_p11 = np.ndarray((waterfall_len, NBINS_TOTAL), dtype=np.float64, buffer=self.shm_lag_wf_p11.buf)
        self.lag_wf_p01_mag = np.ndarray((waterfall_len, NBINS_TOTAL), dtype=np.float64, buffer=self.shm_lag_wf_p01_mag.buf)
        self.lag_wf_p01_phase = np.ndarray((waterfall_len, NBINS_TOTAL), dtype=np.float64, buffer=self.shm_lag_wf_p01_phase.buf)
        
        # Initialize arrays
        self.p00.fill(0)
        self.p11.fill(0)
        self.p01_real.fill(0)
        self.p01_imag.fill(0)
        self.wf_p00.fill(0)
        self.wf_p11.fill(0)
        self.wf_p01_real.fill(0)
        self.wf_p01_imag.fill(0)
        
        self.lag_p00.fill(0)
        self.lag_p11.fill(0)
        self.lag_p01_mag.fill(0)
        self.lag_p01_phase.fill(0)
        self.lag_wf_p00.fill(0)
        self.lag_wf_p11.fill(0)
        self.lag_wf_p01_mag.fill(0)
        self.lag_wf_p01_phase.fill(0)
        
        # Waterfall write position (circular buffer)
        self.wf_write_pos = 0
        self.frames_written = 0  # Track how many frames have been written
        self.lock = threading.Lock()
        
        # Lag waterfall write position
        self.lag_wf_write_pos = 0
        self.lag_frames_written = 0
        self.lag_lock = threading.Lock()
        
        # Condition variable for lag computation
        self.new_data_condition = threading.Condition(self.lock)
        
        # Fine lag data (8192 positive lag bins - half of 16384)
        n_fine_lags = 8192
        # Time domain samples - allocate for max frames we might average
        # Maximum 200 frames * 16384 samples/frame = 3,276,800 samples
        max_frames_for_display = 10
        n_time_samples = max_frames_for_display * 16384
        self.shm_fine_lag_p00 = shared_memory.SharedMemory(create=True, size=n_fine_lags * 8)
        self.shm_fine_lag_p11 = shared_memory.SharedMemory(create=True, size=n_fine_lags * 8)
        self.shm_fine_lag_p01 = shared_memory.SharedMemory(create=True, size=n_fine_lags * 8)  # Cross-correlation
        self.shm_fine_lag_wf_p00 = shared_memory.SharedMemory(create=True, size=waterfall_len * n_fine_lags * 8)
        self.shm_fine_lag_wf_p11 = shared_memory.SharedMemory(create=True, size=waterfall_len * n_fine_lags * 8)
        self.shm_fine_lag_wf_p01 = shared_memory.SharedMemory(create=True, size=waterfall_len * n_fine_lags * 8)  # Cross-correlation waterfall
        
        # Raw time domain data for fine lag
        self.shm_fine_time_adc0 = shared_memory.SharedMemory(create=True, size=n_time_samples * 8)
        self.shm_fine_time_adc1 = shared_memory.SharedMemory(create=True, size=n_time_samples * 8)
        
        self.fine_lag_p00 = np.ndarray((n_fine_lags,), dtype=np.float64, buffer=self.shm_fine_lag_p00.buf)
        self.fine_lag_p11 = np.ndarray((n_fine_lags,), dtype=np.float64, buffer=self.shm_fine_lag_p11.buf)
        self.fine_lag_p01 = np.ndarray((n_fine_lags,), dtype=np.float64, buffer=self.shm_fine_lag_p01.buf)  # Cross-correlation
        self.fine_lag_wf_p00 = np.ndarray((waterfall_len, n_fine_lags), dtype=np.float64, buffer=self.shm_fine_lag_wf_p00.buf)
        self.fine_lag_wf_p11 = np.ndarray((waterfall_len, n_fine_lags), dtype=np.float64, buffer=self.shm_fine_lag_wf_p11.buf)
        self.fine_lag_wf_p01 = np.ndarray((waterfall_len, n_fine_lags), dtype=np.float64, buffer=self.shm_fine_lag_wf_p01.buf)  # Cross-correlation waterfall
        
        # Time domain arrays
        self.fine_time_adc0 = np.ndarray((n_time_samples,), dtype=np.float64, buffer=self.shm_fine_time_adc0.buf)
        self.fine_time_adc1 = np.ndarray((n_time_samples,), dtype=np.float64, buffer=self.shm_fine_time_adc1.buf)
        
        # Initialize fine lag arrays
        self.fine_lag_p00.fill(0)
        self.fine_lag_p11.fill(0)
        self.fine_lag_p01.fill(0)  # Cross-correlation
        self.fine_lag_wf_p00.fill(0)
        self.fine_lag_wf_p11.fill(0)
        self.fine_lag_wf_p01.fill(0)  # Cross-correlation waterfall
        self.fine_time_adc0.fill(0)
        self.fine_time_adc1.fill(0)
        
        # Fine lag waterfall position and lock
        self.fine_lag_wf_write_pos = 0
        self.fine_lag_frames_written = 0
        self.fine_lag_lock = threading.Lock()
        
        # Track actual number of time domain samples being used
        self.fine_time_n_samples = 32768  # Default to 2 frames
        
        # Super FFT data (131,073 frequency bins for 262,144 sample FFT)
        n_super_fft_bins = 131073  # (16 * 16384) // 2 + 1
        n_super_fft_samples = 262144  # 16 * 16384
        self.shm_super_fft_psd = shared_memory.SharedMemory(create=True, size=n_super_fft_bins * 8)
        self.shm_super_fft_wf_psd = shared_memory.SharedMemory(create=True, size=waterfall_len * n_super_fft_bins * 8)
        self.shm_super_fft_time = shared_memory.SharedMemory(create=True, size=n_super_fft_samples * 8)
        
        self.super_fft_psd = np.ndarray((n_super_fft_bins,), dtype=np.float64, buffer=self.shm_super_fft_psd.buf)
        self.super_fft_wf_psd = np.ndarray((waterfall_len, n_super_fft_bins), dtype=np.float64, buffer=self.shm_super_fft_wf_psd.buf)
        self.super_fft_time = np.ndarray((n_super_fft_samples,), dtype=np.float64, buffer=self.shm_super_fft_time.buf)
        
        # Initialize super FFT arrays
        self.super_fft_psd.fill(0)
        self.super_fft_wf_psd.fill(0)
        self.super_fft_time.fill(0)
        
        # Super FFT waterfall position and lock
        self.super_fft_wf_write_pos = 0
        self.super_fft_frames_written = 0
        self.super_fft_lock = threading.Lock()
        
        # Radar data
        self.radar_history_len = 100  # Keep history of last 100 detections
        self.shm_radar_range = shared_memory.SharedMemory(create=True, size=8)
        self.shm_radar_azimuth = shared_memory.SharedMemory(create=True, size=8)
        self.shm_radar_elevation = shared_memory.SharedMemory(create=True, size=8)
        self.shm_radar_x = shared_memory.SharedMemory(create=True, size=8)
        self.shm_radar_y = shared_memory.SharedMemory(create=True, size=8)
        self.shm_radar_z = shared_memory.SharedMemory(create=True, size=8)
        self.shm_radar_detection_valid = shared_memory.SharedMemory(create=True, size=1)
        
        # Radar history buffers
        self.shm_radar_history_range = shared_memory.SharedMemory(create=True, size=self.radar_history_len * 8)
        self.shm_radar_history_azimuth = shared_memory.SharedMemory(create=True, size=self.radar_history_len * 8)
        self.shm_radar_history_elevation = shared_memory.SharedMemory(create=True, size=self.radar_history_len * 8)
        
        # Radar debug data - antenna-to-reference lag spectra
        self.shm_radar_lag_adc0 = shared_memory.SharedMemory(create=True, size=16384 * 8)  # Full lag spectrum
        self.shm_radar_lag_adc1 = shared_memory.SharedMemory(create=True, size=16384 * 8)
        self.shm_radar_lag_adc2 = shared_memory.SharedMemory(create=True, size=16384 * 8)
        
        # Radar detected delays and centroid windows
        self.shm_radar_delays = shared_memory.SharedMemory(create=True, size=3 * 8)  # 3 delays (one per antenna)
        self.shm_radar_centroid_starts = shared_memory.SharedMemory(create=True, size=3 * 4)  # 3 start indices
        self.shm_radar_centroid_ends = shared_memory.SharedMemory(create=True, size=3 * 4)  # 3 end indices
        
        # Create numpy arrays for radar data
        self.radar_range = np.ndarray((1,), dtype=np.float64, buffer=self.shm_radar_range.buf)
        self.radar_azimuth = np.ndarray((1,), dtype=np.float64, buffer=self.shm_radar_azimuth.buf)
        self.radar_elevation = np.ndarray((1,), dtype=np.float64, buffer=self.shm_radar_elevation.buf)
        self.radar_detection_valid = np.ndarray((1,), dtype=np.bool_, buffer=self.shm_radar_detection_valid.buf)
        
        self.radar_history_range = np.ndarray((self.radar_history_len,), dtype=np.float64, buffer=self.shm_radar_history_range.buf)
        self.radar_history_azimuth = np.ndarray((self.radar_history_len,), dtype=np.float64, buffer=self.shm_radar_history_azimuth.buf)
        self.radar_history_elevation = np.ndarray((self.radar_history_len,), dtype=np.float64, buffer=self.shm_radar_history_elevation.buf)
        
        # Radar debug arrays
        self.radar_lag_adc0 = np.ndarray((16384,), dtype=np.float64, buffer=self.shm_radar_lag_adc0.buf)
        self.radar_lag_adc1 = np.ndarray((16384,), dtype=np.float64, buffer=self.shm_radar_lag_adc1.buf)
        self.radar_lag_adc2 = np.ndarray((16384,), dtype=np.float64, buffer=self.shm_radar_lag_adc2.buf)
        
        # Radar delay and centroid window arrays
        self.radar_delays = np.ndarray((3,), dtype=np.float64, buffer=self.shm_radar_delays.buf)
        self.radar_centroid_starts = np.ndarray((3,), dtype=np.int32, buffer=self.shm_radar_centroid_starts.buf)
        self.radar_centroid_ends = np.ndarray((3,), dtype=np.int32, buffer=self.shm_radar_centroid_ends.buf)
        
        # Initialize radar arrays
        self.radar_range[0] = 0.0
        self.radar_azimuth[0] = 0.0
        self.radar_elevation[0] = 0.0
        self.radar_detection_valid[0] = False
        self.radar_history_range.fill(0)
        self.radar_history_azimuth.fill(0)
        self.radar_history_elevation.fill(0)
        self.radar_lag_adc0.fill(0)
        self.radar_lag_adc1.fill(0)
        self.radar_lag_adc2.fill(0)
        self.radar_delays.fill(0)
        self.radar_centroid_starts.fill(0)
        self.radar_centroid_ends.fill(0)
        
        # Radar lock and counter
        self.radar_lock = threading.Lock()
        self.radar_history_count = 0
        
    def cleanup(self):
        """Clean up shared memory."""
        for shm in [self.shm_p00, self.shm_p11, self.shm_p01_real, self.shm_p01_imag,
                    self.shm_wf_p00, self.shm_wf_p11, self.shm_wf_p01_real, self.shm_wf_p01_imag,
                    self.shm_lag_p00, self.shm_lag_p11, self.shm_lag_p01_mag, self.shm_lag_p01_phase,
                    self.shm_lag_wf_p00, self.shm_lag_wf_p11, self.shm_lag_wf_p01_mag, self.shm_lag_wf_p01_phase,
                    self.shm_fine_lag_p00, self.shm_fine_lag_p11, self.shm_fine_lag_p01,
                    self.shm_fine_lag_wf_p00, self.shm_fine_lag_wf_p11, self.shm_fine_lag_wf_p01,
                    self.shm_fine_time_adc0, self.shm_fine_time_adc1,
                    self.shm_super_fft_psd, self.shm_super_fft_wf_psd, self.shm_super_fft_time,
                    self.shm_radar_range, self.shm_radar_azimuth, self.shm_radar_elevation,
                    self.shm_radar_detection_valid, self.shm_radar_history_range,
                    self.shm_radar_history_azimuth, self.shm_radar_history_elevation]:
            shm.close()
            shm.unlink()

class UDPReceiver(threading.Thread):
    """
    A thread that continuously receives UDP packets and processes them directly.
    Uses shared memory buffers for zero-copy data transfer to the GUI.
    """
    def __init__(self, sock: socket.socket, shared_buffers: SharedDataBuffers, 
                 packet_dtype: np.dtype, config: CorrelatorConfig, pocket_correlator=None):
        super().__init__(daemon=True)
        self.socket = sock
        self.shared_buffers = shared_buffers
        self.packet_dtype = packet_dtype
        self.config = config
        self.running = True
        self.pc = pocket_correlator  # Reference to pocket correlator for ADC flag access
        
        # Pre-allocate buffers for packet processing
        self.raw_frame = np.zeros((NBINS_TOTAL, NPROD), dtype=np.complex128)
        self.temp32_real = np.empty((NPROD,), dtype=np.int32)
        self.temp32_imag = np.empty((NPROD,), dtype=np.int32)
        
        # Batch processing buffers
        self.batch_size = 64  # Process packets in batches
        self.packet_batch = bytearray(4096 * self.batch_size)
        self.packet_sizes = np.zeros(self.batch_size, dtype=np.int32)
        self.batch_count = 0
        
        self.frame_count = 0
        self.last_fps_time = time.time()
        self.fps = 0
        
        # Signal for new data available
        self.data_ready_callback: Optional[Callable[[], None]] = None
        
        # Pre-compile optimized functions if Numba is available
        if NUMBA_AVAILABLE:
            self._decode_packets_batch = self._make_numba_decoder()
        else:
            self._decode_packets_batch = self._numpy_decode_packets_batch

    def _make_numba_decoder(self):
        """Create Numba-optimized decoder function."""
        @jit(nopython=True, cache=True)
        def decode_single_packet(h_values, l_values):
            """Decode a single packet's data using Numba."""
            # Pre-allocate output arrays
            real_values = np.empty_like(h_values, dtype=np.int32)
            imag_values = np.empty_like(l_values, dtype=np.int32)
            
            # Process each value - ensuring proper sign extension
            for i in range(len(h_values)):
                # Extract real part (bits 27:10)
                real_values[i] = (h_values[i] << 4) >> 14
                # Extract imaginary part (bits 17:0)
                imag_values[i] = (l_values[i] << 14) >> 14
            
            return real_values, imag_values
        
        return decode_single_packet
    
    def _numpy_decode_packets_batch(self, h_values, l_values):
        """Decode packet data using optimized NumPy operations."""
        # Ensure we're working with signed 32-bit integers for proper sign extension
        if h_values.dtype != np.int32:
            h_values = h_values.astype(np.int32)
        if l_values.dtype != np.int32:
            l_values = l_values.astype(np.int32)
        
        # Extract real part (bits 27:10) using vectorized operations
        real_values = np.right_shift(np.left_shift(h_values, 4), 14)
        # Extract imaginary part (bits 17:0)
        imag_values = np.right_shift(np.left_shift(l_values, 14), 14)
        return real_values, imag_values

    def run(self) -> None:
        """Continuously listen for UDP packets and process them."""
        # Aggregated datagrams pack several records (up to 8*660 = 5280 B); size the
        # receive buffer for a full jumbo frame so nothing is truncated.
        packet_buffer = bytearray(9000)
        
        while self.running:
            try:
                # Reset ADC flags for next frame capture
                self._reset_adc_flags()

                nbytes, _ = self.socket.recvfrom_into(packet_buffer)

                # Check ADC flags after completing frame capture
                self._check_adc_flags()
                
                if nbytes > 0:
                    self._process_packet(packet_buffer[:nbytes])
            except socket.timeout:
                continue
            except Exception as e:
                if self.running:
                    print(f"UDPReceiver Error: {e}")
                break

    def _process_packet(self, packet_data: bytes):
        """Process a raw UDP datagram, which may pack several aggregated records.

        Firmware now aggregates multiple UCORR bins (each a full header+products
        record) into one datagram by gating TLAST, so parse every record present
        (``nbytes // itemsize``) instead of only the first. A short final group is
        handled naturally by the integer division.
        """
        itemsize = self.packet_dtype.itemsize
        count = len(packet_data) // itemsize
        if count == 0:
            return
        packet_array = np.frombuffer(packet_data, dtype=self.packet_dtype, count=count)
        for rec in range(count):
            header = packet_array['header'][rec]
            if header['cookie'] != 0xcf:
                continue

            bin_idx = header['stream_id']
            if bin_idx >= NBINS_TOTAL:
                continue

            data = packet_array['data'][rec]
            self._decode_frame_data(bin_idx, data)

            if bin_idx == NBINS_TOTAL - 1:
                self._process_complete_frame()

    def _decode_frame_data(self, bin_idx: int, data: np.ndarray):
        """Decode the complex data from the raw packet format."""
        # For now, always use the working path that properly handles sign extension
        # The Numba optimization has issues with sign extension on bit shifts
        
        # Ensure proper sign extension by viewing as signed int32
        h_signed = data['h'].view(np.int32)
        l_signed = data['l'].view(np.int32)
        
        # Perform bit extraction with proper sign extension
        # Extract real part (bits 27:10)
        self.temp32_real[:] = (h_signed << 4) >> 14
        # Extract imaginary part (bits 17:0)
        self.temp32_imag[:] = (l_signed << 14) >> 14
        
        # Combine into complex values
        self.raw_frame[bin_idx] = self.temp32_real + 1j * self.temp32_imag

    def _process_complete_frame(self):
        """Process completed frame and update shared buffers."""
        if self.config.n_firmware_frames == 0: 
            return
        
        # Normalize the frame
        normalized_frame = self.raw_frame / self.config.n_firmware_frames
        if self.config.gain_factor is not None:
            normalized_frame *= self.config.gain_factor

        # Extract the data for current frame (outside the lock)
        p00_data = np.abs(normalized_frame[:, self.config.idx00])
        p11_data = np.abs(normalized_frame[:, self.config.idx11])
        p01_real_data = normalized_frame[:, self.config.idx01].real
        p01_imag_data = normalized_frame[:, self.config.idx01].imag

        # Update shared memory buffers with minimal lock time
        with self.shared_buffers.lock:
            # Update current frame data
            self.shared_buffers.p00[:] = p00_data
            self.shared_buffers.p11[:] = p11_data
            self.shared_buffers.p01_real[:] = p01_real_data
            self.shared_buffers.p01_imag[:] = p01_imag_data
            
            # Update waterfall - shift existing data down and insert new at top
            # Roll the arrays down by 1 to make room at position 0
            self.shared_buffers.wf_p00[1:] = self.shared_buffers.wf_p00[:-1]
            self.shared_buffers.wf_p11[1:] = self.shared_buffers.wf_p11[:-1]
            self.shared_buffers.wf_p01_real[1:] = self.shared_buffers.wf_p01_real[:-1]
            self.shared_buffers.wf_p01_imag[1:] = self.shared_buffers.wf_p01_imag[:-1]
            
            # Write new data at position 0 (top)
            self.shared_buffers.wf_p00[0] = p00_data
            self.shared_buffers.wf_p11[0] = p11_data
            self.shared_buffers.wf_p01_real[0] = p01_real_data
            self.shared_buffers.wf_p01_imag[0] = p01_imag_data
            
            # Update frames written counter
            self.shared_buffers.frames_written = min(self.shared_buffers.frames_written + 1, self.shared_buffers.waterfall_len)
            
            # Signal that new data is available for lag computation
            self.shared_buffers.new_data_condition.notify()

        # Calculate FPS
        self.frame_count += 1
        current_time = time.time()
        time_delta = current_time - self.last_fps_time
        if time_delta >= 1.0:
            self.fps = self.frame_count / time_delta
            self.last_fps_time = current_time
            self.frame_count = 0
        
        # Signal that new data is available
        if self.data_ready_callback:
            self.data_ready_callback()

    def _reset_adc_flags(self):
        """Reset ADC overrange and overvoltage flags before data capture."""
        try:
            # Reset flags for next capture 
            self.pc.i.GPIO.ADC_OR_CLEAR = 2**8 - 1  # 255, i.e., 11111111 for all 8 pols
            self.pc.i.GPIO.ADC_OR_CLEAR = 0
            self.pc.i.GPIO.ADC_OV_CLEAR = 2**8 - 1  # 255, i.e., 11111111 for all 8 pols
            self.pc.i.GPIO.ADC_OV_CLEAR = 0
        except Exception as e:
            print(f"Error resetting ADC flags: {e}")

    def _check_adc_flags(self):
        """Check ADC overrange and overvoltage flags and update shared buffers."""
        
        # Helper array for reading individual bits of ADC_OR and ADC_OV flags
        bitmask = 2**np.arange(8) # 8-bit bitmask for isolating values of individual bits
        bitshifts = np.arange(0, 8)[::-1] # bitshifts to read values of bits as 1 or 0
                            
        try:
            # Check the ADC overrange and overvoltage flags 
            adc_or = self.pc.i.GPIO.ADC_OR
            adc_ov = self.pc.i.GPIO.ADC_OV

            # Create arrays to print the overrange/overflow status of individual ADCs,
            # reversing the array so that the ADCs are indexed in ascending order from ADC1 to ADC8
            adc_or_arr = np.array([(adc_or & bitmask[m]) >> m for m in bitshifts])[::-1]
            adc_ov_arr = np.array([(adc_ov & bitmask[m]) >> m for m in bitshifts])[::-1]

            # Update shared buffer counters
            self.shared_buffers.total_captures += 1
            
            # Count any overrange or overvoltage flags
            if adc_or != 0:
                print(f'ADC overranges detected:', [f'ADC{n + 1}' for n in range(8) if adc_or_arr[n] != 0])
                self.shared_buffers.adc_or_count += 1
            if adc_ov != 0:
                print(f'ADC overranges detected:', [f'ADC{n + 1}' for n in range(8) if adc_ov_arr[n] != 0])
                self.shared_buffers.adc_ov_count += 1
                
        except Exception as e:
            print(f"Error checking ADC flags: {e}")

    def stop(self) -> None:
        """Stop the receiver thread."""
        self.running = False

class PeriscopeCorrelator(QtWidgets.QMainWindow):
    """
    Main application window for the Periscope Correlator.
    Handles the GUI, user interactions, and orchestration of data processing.
    """
    
    # Signal emitted when new data is available
    data_ready = QtCore.pyqtSignal()
    
    def __init__(self, serial_number: str = DEFAULT_SERIAL):
        super().__init__()
        self.serial_number = serial_number
        self.setWindowTitle("Periscope Correlator - Not Connected")
        self.setGeometry(100, 100, 1600, 900)

        self.pocket_correlator: Optional[POCKET_CORRELATOR] = None
        self.receiver_thread: Optional[UDPReceiver] = None
        self.lag_computer_thread: Optional[LagSpectraComputer] = None
        self.fine_lag_computer_thread: Optional[FineLagComputer] = None
        self.super_fft_computer_thread: Optional[SuperFFTComputer] = None
        self.radar_computer_thread: Optional[RadarComputer] = None
        self.shared_buffers: Optional[SharedDataBuffers] = None
        
        self.config = CorrelatorConfig()
        self.computed_gains: Optional[np.ndarray] = None
        
        ucorr_receiver = UCorrFrameReceiver(None, 8, Nbins=NBINS_TOTAL, NCORR=NPROD)
        self.packet_dtype = ucorr_receiver.packet_dtype

        # Initialize Nyquist zone tracking
        self.current_nyquist_zone = 1  # Default to Zone 1
        
        # Initialize frequency axes for Zone 1
        self._update_all_frequency_axes(1)
        
        # Lag axis in meters (distance = c * time / 2 for radar applications)
        lag_bins = np.arange(NBINS_TOTAL) - NBINS_TOTAL // 2
        # Time between lag bins = M_FACTOR / FS_HZ seconds
        lag_time_s = lag_bins * (M_FACTOR / FS_HZ)  # Time in seconds
        c_m_per_s = 299792458.0  # Speed of light in meters per second
        self.lag_axis = lag_time_s * c_m_per_s / 2  # Convert to distance in meters (round trip)
        
        # Fine lag axis in meters (for raw ADC data)
        # For cross-correlations, we now have both positive and negative lags centered around zero
        fine_lag_samples = np.arange(8192) - 8192 // 2  # -4096 to 4095 samples (centered around zero)
        fine_lag_time_s = fine_lag_samples / FS_HZ  # Time in seconds
        self.fine_lag_axis = fine_lag_time_s * c_m_per_s / 2  # Distance in meters (round trip)
        # Range: ~-192 to +192 meters with 4.68 cm resolution (centered around zero lag)
        
        # Time axis for raw ADC samples (microseconds)
        # Will be dynamically adjusted based on actual frames captured
        max_time_samples = 10 * 16384  # Maximum samples we might display (10 frames)
        time_samples = np.arange(max_time_samples)
        self.time_axis_us_full = time_samples / FS_HZ * 1e6  # Convert to microseconds
        
        self.prod_map = self._generate_prod_map()
        self._precalculate_indices()
        
        self._build_ui()
        self._setup_status_bar()
        
        # Connect data ready signal to update plots
        self.data_ready.connect(self._update_plots)
        
        self.show()

    def _generate_prod_map(self) -> np.ndarray:
        """Generate the product map for the correlator."""
        prod_map = [(i, (j + i) % NCHAN) if i < NCHAN - j else (((j + i) % NCHAN), i - 1) 
                    for i in range(NCHAN + 1) for j in range(NCHAN // 2)]
        return np.array(prod_map)

    def _build_ui(self) -> None:
        """Construct the main user interface."""
        self.central_widget = QtWidgets.QWidget()
        self.setCentralWidget(self.central_widget)
        self.main_layout = QtWidgets.QVBoxLayout(self.central_widget)
        
        self._build_toolbar()
        self._build_plot_area()

    def _build_toolbar(self) -> None:
        """Build the top toolbar with connection and control widgets."""
        # Main toolbar container with two rows
        toolbar_container = QtWidgets.QVBoxLayout()
        
        # First row - Connection and configuration controls
        toolbar_row1 = QtWidgets.QHBoxLayout()
        
        self.connect_button = QtWidgets.QPushButton("Connect")
        self.connect_button.clicked.connect(self.connect_to_board)
        self.serial_input = QtWidgets.QLineEdit(self.serial_number)
        self.serial_input.setFixedWidth(80)
        
        self.calibrate_button = QtWidgets.QPushButton("Calibrate Gains")
        self.calibrate_button.clicked.connect(self.calibrate_gains)
        self.calibrate_button.setEnabled(False)
        
        self.configure_button = QtWidgets.QPushButton("Configure Correlator")
        self.configure_button.clicked.connect(self.configure_correlator)
        self.configure_button.setEnabled(False)
        
        self.gain_target_spinner = QtWidgets.QDoubleSpinBox()
        self.gain_target_spinner.setRange(0.1, 10.0)
        self.gain_target_spinner.setSingleStep(0.1)
        self.gain_target_spinner.setValue(DEFAULT_GAIN_TARGET)
        self.gain_target_spinner.setDecimals(2)

        self.waterfall_len_spinner = QtWidgets.QSpinBox()
        self.waterfall_len_spinner.setRange(10, 8000)
        self.waterfall_len_spinner.setSingleStep(100)
        self.waterfall_len_spinner.setValue(DEFAULT_WATERFALL_LEN)
        self.waterfall_len_spinner.valueChanged.connect(self._update_waterfall_length)
        
        # Add apply button for waterfall length
        self.apply_waterfall_len_button = QtWidgets.QPushButton("Apply")
        self.apply_waterfall_len_button.setToolTip("Apply new waterfall length to running stream")
        self.apply_waterfall_len_button.clicked.connect(self._apply_waterfall_length)
        self.apply_waterfall_len_button.setEnabled(False)  # Disabled until streaming
        
        # Add widgets to first row
        toolbar_row1.addWidget(QtWidgets.QLabel("Serial:"))
        toolbar_row1.addWidget(self.serial_input)
        toolbar_row1.addWidget(self.connect_button)
        toolbar_row1.addSpacing(20)
        toolbar_row1.addWidget(self.calibrate_button)
        toolbar_row1.addWidget(QtWidgets.QLabel("Gain Target:"))
        toolbar_row1.addWidget(self.gain_target_spinner)
        toolbar_row1.addSpacing(20)
        toolbar_row1.addWidget(self.configure_button)
        toolbar_row1.addSpacing(20)
        toolbar_row1.addWidget(QtWidgets.QLabel("Waterfall Length:"))
        toolbar_row1.addWidget(self.waterfall_len_spinner)
        toolbar_row1.addWidget(self.apply_waterfall_len_button)
        toolbar_row1.addStretch(1)
        
        # Second row - Display and computation controls
        toolbar_row2 = QtWidgets.QHBoxLayout()
        
        self.scale_update_spinner = QtWidgets.QSpinBox()
        self.scale_update_spinner.setRange(10, 1000)
        self.scale_update_spinner.setSingleStep(10)
        self.scale_update_spinner.setValue(10)
        self.scale_update_spinner.setPrefix("Scale Update: ")
        self.scale_update_spinner.setSuffix(" frames")
        self.scale_update_spinner.setToolTip("Update waterfall color scale every N frames")
        
        self.mean_subtract_checkbox = QtWidgets.QCheckBox("Mean Subtract")
        self.mean_subtract_checkbox.setToolTip("Subtract mean from each frequency bin in waterfall plots")
        self.mean_subtract_checkbox.setChecked(False)
        
        self.log_scale_checkbox = QtWidgets.QCheckBox("Log Scale")
        self.log_scale_checkbox.setToolTip("Use log scale for autocorrelations and symmetric log for cross-correlations")
        self.log_scale_checkbox.setChecked(True)
        
        self.symlog_crossover_spinner = QtWidgets.QDoubleSpinBox()
        self.symlog_crossover_spinner.setRange(0.001, 1000000000000000.0)
        self.symlog_crossover_spinner.setSingleStep(1000)
        self.symlog_crossover_spinner.setValue(10000000)
        self.symlog_crossover_spinner.setDecimals(1)
        self.symlog_crossover_spinner.setPrefix("Symlog: ")
        self.symlog_crossover_spinner.setToolTip("Crossover point for symmetric log scale (linear below C, log above C)")
        
        self.viewbox_mode_checkbox = QtWidgets.QCheckBox("ViewBox Mode")
        self.viewbox_mode_checkbox.setToolTip("Enable matplotlib-style box zoom (click and drag to zoom)")
        self.viewbox_mode_checkbox.setChecked(False)
        self.viewbox_mode_checkbox.stateChanged.connect(self._toggle_viewbox_mode)
        
        # Tab computation controls
        self.enable_spectra_checkbox = QtWidgets.QCheckBox("Enable Spectra")
        self.enable_spectra_checkbox.setToolTip("Enable computation of frequency spectra")
        self.enable_spectra_checkbox.setChecked(True)
        
        self.enable_waterfall_checkbox = QtWidgets.QCheckBox("Enable Waterfall")
        self.enable_waterfall_checkbox.setToolTip("Enable computation of waterfall plots")
        self.enable_waterfall_checkbox.setChecked(True)
        
        self.enable_lag_checkbox = QtWidgets.QCheckBox("Enable Lag Computation")
        self.enable_lag_checkbox.setToolTip("Enable computation of lag spectra (uses additional CPU)")
        self.enable_lag_checkbox.setChecked(False)
        self.enable_lag_checkbox.stateChanged.connect(self._toggle_lag_computation)
        
        # Frames to average spinner (applies to all modes)
        self.frames_to_average_spinner = QtWidgets.QSpinBox()
        self.frames_to_average_spinner.setRange(1, 2000)
        self.frames_to_average_spinner.setValue(DEFAULT_FRAMES_TO_AVERAGE)
        self.frames_to_average_spinner.setPrefix("Frames to Average: ")
        self.frames_to_average_spinner.setToolTip("Number of frames to average (applies to all modes)")
        self.frames_to_average_spinner.valueChanged.connect(self._update_frames_to_average)
        
        # Fine lag mode controls
        self.fine_lag_mode_button = QtWidgets.QPushButton("Enable Fine Lag Mode")
        self.fine_lag_mode_button.setCheckable(True)
        self.fine_lag_mode_button.setToolTip("Switch to fine lag mode using raw ADC data (cm resolution)")
        self.fine_lag_mode_button.clicked.connect(self._toggle_fine_lag_mode)
        self.fine_lag_mode_button.setEnabled(False)  # Disabled until connected
        
        # Super-resolution FFT controls
        self.super_fft_mode_button = QtWidgets.QPushButton("Enable Super-Resolution FFT")
        self.super_fft_mode_button.setCheckable(True)
        self.super_fft_mode_button.setToolTip("Switch to super-resolution FFT mode (12.2 kHz resolution)")
        self.super_fft_mode_button.clicked.connect(self._toggle_super_fft_mode)
        self.super_fft_mode_button.setEnabled(False)  # Disabled until connected
        
        # Radar mode controls
        self.radar_mode_button = QtWidgets.QPushButton("Enable Radar Mode")
        self.radar_mode_button.setCheckable(True)
        self.radar_mode_button.setToolTip("Switch to 3D radar localization mode")
        self.radar_mode_button.clicked.connect(self._toggle_radar_mode)
        self.radar_mode_button.setEnabled(False)  # Disabled until connected
        
        self.radar_threshold_spinner = QtWidgets.QDoubleSpinBox()
        self.radar_threshold_spinner.setRange(0.0, 1.0)
        self.radar_threshold_spinner.setSingleStep(0.05)
        self.radar_threshold_spinner.setValue(0.75)
        self.radar_threshold_spinner.setDecimals(2)
        self.radar_threshold_spinner.setPrefix("Threshold: ")
        self.radar_threshold_spinner.setToolTip("Significance threshold for radar peak detection")
        self.radar_threshold_spinner.valueChanged.connect(self._update_radar_threshold)
        self.radar_threshold_spinner.setEnabled(False)  # Disabled until radar mode active
        
        # Nyquist zone selector
        self.nyquist_zone_combo = QtWidgets.QComboBox()
        self.nyquist_zone_combo.addItems(["Zone 1 (0-1600 MHz)", "Zone 2 (1600-3200 MHz)", "Zone 3 (3200-4800 MHz)"])
        self.nyquist_zone_combo.setCurrentIndex(0)  # Default to Zone 1
        self.nyquist_zone_combo.setToolTip("Select Nyquist zone for frequency axis display")
        self.nyquist_zone_combo.currentIndexChanged.connect(self._on_nyquist_zone_changed)

        # Restore all plots button
        self.restore_plots_button = QtWidgets.QPushButton("Restore All Plots")
        self.restore_plots_button.setToolTip("Show all hidden plots")
        self.restore_plots_button.clicked.connect(self._restore_all_plots)

        # Add widgets to second row
        toolbar_row2.addWidget(self.scale_update_spinner)
        toolbar_row2.addWidget(self.mean_subtract_checkbox)
        toolbar_row2.addWidget(self.log_scale_checkbox)
        toolbar_row2.addWidget(self.symlog_crossover_spinner)
        toolbar_row2.addWidget(self.viewbox_mode_checkbox)
        toolbar_row2.addSpacing(20)
        toolbar_row2.addWidget(QtWidgets.QLabel("Nyquist Zone:"))
        toolbar_row2.addWidget(self.nyquist_zone_combo)
        toolbar_row2.addSpacing(20)
        toolbar_row2.addWidget(self.enable_lag_checkbox)
        toolbar_row2.addWidget(self.frames_to_average_spinner)
        toolbar_row2.addSpacing(20)
        toolbar_row2.addWidget(self.fine_lag_mode_button)
        toolbar_row2.addWidget(self.super_fft_mode_button)
        toolbar_row2.addWidget(self.radar_mode_button)
        toolbar_row2.addWidget(self.radar_threshold_spinner)
        toolbar_row2.addSpacing(20)
        toolbar_row2.addWidget(self.restore_plots_button)
        toolbar_row2.addStretch(1)
        
        # Add both rows to the toolbar container
        toolbar_container.addLayout(toolbar_row1)
        toolbar_container.addLayout(toolbar_row2)
        
        # Add a separator line between toolbar and plots
        separator = QtWidgets.QFrame()
        separator.setFrameShape(QtWidgets.QFrame.HLine)
        separator.setFrameShadow(QtWidgets.QFrame.Sunken)
        toolbar_container.addWidget(separator)
        
        self.main_layout.addLayout(toolbar_container)

    def _build_plot_area(self) -> None:
        """Build the tabbed widget for plots."""
        self.tabs = QtWidgets.QTabWidget()
        self.main_layout.addWidget(self.tabs)

        self.spectra_tab = QtWidgets.QWidget()
        self.waterfall_tab = QtWidgets.QWidget()
        self.lag_spectra_tab = QtWidgets.QWidget()
        self.lag_waterfall_tab = QtWidgets.QWidget()
        self.fine_lag_spectra_tab = QtWidgets.QWidget()
        self.fine_lag_waterfall_tab = QtWidgets.QWidget()
        self.super_fft_spectra_tab = QtWidgets.QWidget()
        self.super_fft_waterfall_tab = QtWidgets.QWidget()
        self.radar_azel_tab = QtWidgets.QWidget()
        self.radar_azrange_tab = QtWidgets.QWidget()
        self.radar_xyz_tab = QtWidgets.QWidget()
        self.radar_debug_tab = QtWidgets.QWidget()

        self.tabs.addTab(self.spectra_tab, "Spectra")
        self.tabs.addTab(self.waterfall_tab, "Waterfall")
        self.tabs.addTab(self.lag_spectra_tab, "Lag Spectra")
        self.tabs.addTab(self.lag_waterfall_tab, "Lag Waterfall")
        self.tabs.addTab(self.fine_lag_spectra_tab, "Fine Lag Spectra")
        self.tabs.addTab(self.fine_lag_waterfall_tab, "Fine Lag Waterfall")
        self.tabs.addTab(self.super_fft_spectra_tab, "Super FFT Spectra")
        self.tabs.addTab(self.super_fft_waterfall_tab, "Super FFT Waterfall")
        self.tabs.addTab(self.radar_azel_tab, "Radar Az/El")
        self.tabs.addTab(self.radar_azrange_tab, "Radar Az/Range")
        self.tabs.addTab(self.radar_xyz_tab, "Radar 3D")
        self.tabs.addTab(self.radar_debug_tab, "Radar Debug")

        self.plot_checkboxes: Dict[str, QtWidgets.QCheckBox] = {}
        self._setup_spectra_plots()
        self._setup_waterfall_plots()
        self._setup_lag_spectra_plots()
        self._setup_lag_waterfall_plots()
        self._setup_fine_lag_spectra_plots()
        self._setup_fine_lag_waterfall_plots()
        self._setup_super_fft_spectra_plots()
        self._setup_super_fft_waterfall_plots()
        self._setup_radar_azel_plots()
        self._setup_radar_azrange_plots()
        self._setup_radar_xyz_plots()
        self._setup_radar_debug_plots()

    def _setup_spectra_plots(self) -> None:
        """Set up the plots for the Spectra tab."""
        layout = QtWidgets.QGridLayout(self.spectra_tab)
        
        self.plots = {}
        self.plot_widgets = {}  # Store the plot widgets for log mode control
        colors = pg.colormap.get("tab10", "matplotlib").getColors()

        # --- P00 Plot ---
        p00_widget = QtWidgets.QWidget()
        p00_layout = QtWidgets.QVBoxLayout(p00_widget)
        p00_cb = QtWidgets.QCheckBox("ADC0 Auto")
        p00_cb.setChecked(True)
        self.plot_checkboxes["s_p00"] = p00_cb
        p00_plot = pg.PlotWidget()
        p00_layout.addWidget(p00_cb)
        p00_layout.addWidget(p00_plot)
        self.plots[(0,0)] = p00_plot.plot(pen=colors[0], name='|amp|')
        self.plot_widgets[(0,0)] = p00_plot  # Store the widget
        p00_plot.showGrid(x=True, y=True)
        p00_plot.setLabel('bottom', 'Frequency', units='Hz')
        p00_plot.addLegend()
        layout.addWidget(p00_widget, 0, 0)
        # Connect checkbox to hide/show the widget
        p00_cb.stateChanged.connect(lambda state, w=p00_widget: w.setVisible(state == QtCore.Qt.CheckState.Checked))

        # --- P01 Abs Plot ---
        p01_abs_widget = QtWidgets.QWidget()
        p01_abs_layout = QtWidgets.QVBoxLayout(p01_abs_widget)
        p01_abs_cb = QtWidgets.QCheckBox("ADC0/1 Cross |Amp|")
        p01_abs_cb.setChecked(True)
        self.plot_checkboxes["s_p01_abs"] = p01_abs_cb
        p01_abs_plot = pg.PlotWidget()
        p01_abs_layout.addWidget(p01_abs_cb)
        p01_abs_layout.addWidget(p01_abs_plot)
        self.plots[(0,1,'abs')] = p01_abs_plot.plot(pen=colors[1], name='|amp|')
        self.plot_widgets[(0,1,'abs')] = p01_abs_plot  # Store the widget
        p01_abs_plot.addLegend()
        p01_abs_plot.showGrid(x=True, y=True)
        p01_abs_plot.setLabel('bottom', 'Frequency', units='Hz')
        layout.addWidget(p01_abs_widget, 1, 0)
        # Connect checkbox to hide/show the widget
        p01_abs_cb.stateChanged.connect(lambda state, w=p01_abs_widget: w.setVisible(state == QtCore.Qt.CheckState.Checked))

        # --- P01 Real Plot ---
        p01_real_widget = QtWidgets.QWidget()
        p01_real_layout = QtWidgets.QVBoxLayout(p01_real_widget)
        p01_real_cb = QtWidgets.QCheckBox("ADC0/1 Cross Real")
        p01_real_cb.setChecked(True)
        self.plot_checkboxes["s_p01_real"] = p01_real_cb
        p01_real_plot = pg.PlotWidget()
        p01_real_layout.addWidget(p01_real_cb)
        p01_real_layout.addWidget(p01_real_plot)
        self.plots[(0,1,'real')] = p01_real_plot.plot(pen=colors[2], name='real')
        self.plot_widgets[(0,1,'real')] = p01_real_plot  # Store the widget
        p01_real_plot.addLegend()
        p01_real_plot.showGrid(x=True, y=True)
        p01_real_plot.setLabel('bottom', 'Frequency', units='Hz')
        p01_real_plot.setLabel('left', 'Real Part')
        layout.addWidget(p01_real_widget, 1, 1)
        # Connect checkbox to hide/show the widget
        p01_real_cb.stateChanged.connect(lambda state, w=p01_real_widget: w.setVisible(state == QtCore.Qt.CheckState.Checked))

        # --- P11 Plot ---
        p11_widget = QtWidgets.QWidget()
        p11_layout = QtWidgets.QVBoxLayout(p11_widget)
        p11_cb = QtWidgets.QCheckBox("ADC1 Auto")
        p11_cb.setChecked(True)
        self.plot_checkboxes["s_p11"] = p11_cb
        p11_plot = pg.PlotWidget()
        p11_layout.addWidget(p11_cb)
        p11_layout.addWidget(p11_plot)
        self.plots[(1,1)] = p11_plot.plot(pen=colors[3], name='|amp|')
        self.plot_widgets[(1,1)] = p11_plot  # Store the widget
        p11_plot.showGrid(x=True, y=True)
        p11_plot.setLabel('bottom', 'Frequency', units='Hz')
        p11_plot.addLegend()
        layout.addWidget(p11_widget, 0, 1)
        # Connect checkbox to hide/show the widget
        p11_cb.stateChanged.connect(lambda state, w=p11_widget: w.setVisible(state == QtCore.Qt.CheckState.Checked))

    def _setup_waterfall_plots(self) -> None:
        """Set up the image views for the Waterfall tab."""
        layout = QtWidgets.QGridLayout(self.waterfall_tab)
        self.waterfall_plots = {}
        self.waterfall_images = {}  # Store the image items for efficient updates
        self.waterfall_levels = {}  # Store color levels for each plot
        self.waterfall_level_update_counter = {}  # Track when to update levels
        viridis_cm = pg.colormap.get('viridis')
        # Use diverging colormap for cross-correlations
        coolwarm_cm = viridis_cm #pg.colormap.get('CET-D1')  # Blue-white-red diverging

        titles = ["ADC0 Auto", "ADC1 Auto", "Cross-Corr Real", "Cross-Corr Phase"]
        keys = ['p00', 'p11', 'p01_real', 'p01_phase']
        
        for i, (title, key) in enumerate(zip(titles, keys)):
            row, col = divmod(i, 2)
            
            container = QtWidgets.QWidget()
            container_layout = QtWidgets.QVBoxLayout(container)
            
            cb = QtWidgets.QCheckBox(title)
            cb.setChecked(True)
            self.plot_checkboxes[f"wf_{key}"] = cb
            
            # Use PlotWidget instead of ImageView for better performance
            plot = pg.PlotWidget()
            plot.setLabel('left', 'Time')
            plot.setLabel('bottom', 'Frequency Bin')
            # Invert Y axis so 0 is at top (newest data)
            plot.invertY(True)
            
            # Create an ImageItem
            img_item = pg.ImageItem()
            plot.addItem(img_item)
            # Use diverging colormap for cross-correlations
            if 'p01' in key:
                img_item.setLookupTable(coolwarm_cm.getLookupTable())
            else:
                img_item.setLookupTable(viridis_cm.getLookupTable())
            
            self.waterfall_plots[key] = plot
            self.waterfall_images[key] = img_item
            self.waterfall_levels[key] = None  # Will be set on first data
            self.waterfall_level_update_counter[key] = 0
            
            container_layout.addWidget(cb)
            container_layout.addWidget(plot)
            
            layout.addWidget(container, row, col)
            
            # Connect checkbox to hide/show the container
            cb.stateChanged.connect(lambda state, w=container: w.setVisible(state == QtCore.Qt.CheckState.Checked))


    def _setup_lag_spectra_plots(self) -> None:
        """Set up the plots for the Lag Spectra tab."""
        layout = QtWidgets.QGridLayout(self.lag_spectra_tab)
        
        self.lag_plots = {}
        self.lag_plot_widgets = {}
        colors = pg.colormap.get("tab10", "matplotlib").getColors()

        # --- Lag P00 Plot ---
        lag_p00_widget = QtWidgets.QWidget()
        lag_p00_layout = QtWidgets.QVBoxLayout(lag_p00_widget)
        lag_p00_cb = QtWidgets.QCheckBox("ADC0 Auto Delay Spectrum")
        lag_p00_cb.setChecked(True)
        self.plot_checkboxes["lag_s_p00"] = lag_p00_cb
        lag_p00_plot = pg.PlotWidget()
        lag_p00_layout.addWidget(lag_p00_cb)
        lag_p00_layout.addWidget(lag_p00_plot)
        self.lag_plots[(0,0)] = lag_p00_plot.plot(pen=colors[0], name='power')
        self.lag_plot_widgets[(0,0)] = lag_p00_plot
        lag_p00_plot.showGrid(x=True, y=True)
        lag_p00_plot.setLabel('bottom', 'Distance', units='m')
        lag_p00_plot.setLabel('left', 'Power')
        lag_p00_plot.addLegend()
        layout.addWidget(lag_p00_widget, 0, 0)
        # Connect checkbox to hide/show the widget
        lag_p00_cb.stateChanged.connect(lambda state, w=lag_p00_widget: w.setVisible(state == QtCore.Qt.CheckState.Checked))

        # --- Lag P11 Plot ---
        lag_p11_widget = QtWidgets.QWidget()
        lag_p11_layout = QtWidgets.QVBoxLayout(lag_p11_widget)
        lag_p11_cb = QtWidgets.QCheckBox("ADC1 Auto Delay Spectrum")
        lag_p11_cb.setChecked(True)
        self.plot_checkboxes["lag_s_p11"] = lag_p11_cb
        lag_p11_plot = pg.PlotWidget()
        lag_p11_layout.addWidget(lag_p11_cb)
        lag_p11_layout.addWidget(lag_p11_plot)
        self.lag_plots[(1,1)] = lag_p11_plot.plot(pen=colors[3], name='power')
        self.lag_plot_widgets[(1,1)] = lag_p11_plot
        lag_p11_plot.showGrid(x=True, y=True)
        lag_p11_plot.setLabel('bottom', 'Distance', units='m')
        lag_p11_plot.setLabel('left', 'Power')
        lag_p11_plot.addLegend()
        layout.addWidget(lag_p11_widget, 0, 1)
        # Connect checkbox to hide/show the widget
        lag_p11_cb.stateChanged.connect(lambda state, w=lag_p11_widget: w.setVisible(state == QtCore.Qt.CheckState.Checked))

        # --- Lag P01 Magnitude Plot ---
        lag_p01_mag_widget = QtWidgets.QWidget()
        lag_p01_mag_layout = QtWidgets.QVBoxLayout(lag_p01_mag_widget)
        lag_p01_mag_cb = QtWidgets.QCheckBox("ADC0/1 Cross Delay Spectrum")
        lag_p01_mag_cb.setChecked(True)
        self.plot_checkboxes["lag_s_p01_mag"] = lag_p01_mag_cb
        lag_p01_mag_plot = pg.PlotWidget()
        lag_p01_mag_layout.addWidget(lag_p01_mag_cb)
        lag_p01_mag_layout.addWidget(lag_p01_mag_plot)
        self.lag_plots[(0,1,'mag')] = lag_p01_mag_plot.plot(pen=colors[1], name='power')
        self.lag_plot_widgets[(0,1,'mag')] = lag_p01_mag_plot
        lag_p01_mag_plot.addLegend()
        lag_p01_mag_plot.showGrid(x=True, y=True)
        lag_p01_mag_plot.setLabel('bottom', 'Distance', units='m')
        lag_p01_mag_plot.setLabel('left', 'Power')
        layout.addWidget(lag_p01_mag_widget, 1, 0)
        # Connect checkbox to hide/show the widget
        lag_p01_mag_cb.stateChanged.connect(lambda state, w=lag_p01_mag_widget: w.setVisible(state == QtCore.Qt.CheckState.Checked))

    def _setup_lag_waterfall_plots(self) -> None:
        """Set up the image views for the Lag Waterfall tab."""
        layout = QtWidgets.QGridLayout(self.lag_waterfall_tab)
        self.lag_waterfall_plots = {}
        self.lag_waterfall_images = {}
        self.lag_waterfall_levels = {}
        self.lag_waterfall_level_update_counter = {}
        
        viridis_cm = pg.colormap.get('viridis')

        # Only 3 plots now - no phase
        titles = ["ADC0 Auto Delay", "ADC1 Auto Delay", "Cross-Corr Delay"]
        keys = ['lag_p00', 'lag_p11', 'lag_p01_mag']
        
        for i, (title, key) in enumerate(zip(titles, keys)):
            if i < 2:
                row, col = 0, i
            else:
                row, col = 1, 0
            
            container = QtWidgets.QWidget()
            container_layout = QtWidgets.QVBoxLayout(container)
            
            cb = QtWidgets.QCheckBox(title)
            cb.setChecked(True)
            self.plot_checkboxes[f"lag_wf_{key}"] = cb
            cb.stateChanged.connect(lambda state, w=container: w.setVisible(state == QtCore.Qt.CheckState.Checked))

            
            plot = pg.PlotWidget()
            plot.setLabel('left', 'Time')
            plot.setLabel('bottom', 'Distance', units='m')
            # Invert Y axis so 0 is at top (newest data)
            plot.invertY(True)
            
            img_item = pg.ImageItem()
            plot.addItem(img_item)
            img_item.setLookupTable(viridis_cm.getLookupTable())
            
            self.lag_waterfall_plots[key] = plot
            self.lag_waterfall_images[key] = img_item
            self.lag_waterfall_levels[key] = None
            self.lag_waterfall_level_update_counter[key] = 0
            
            container_layout.addWidget(cb)
            container_layout.addWidget(plot)
            
            layout.addWidget(container, row, col)

    def _setup_fine_lag_spectra_plots(self) -> None:
        """Set up the plots for the Fine Lag Spectra tab."""
        layout = QtWidgets.QGridLayout(self.fine_lag_spectra_tab)
        
        self.fine_lag_plots = {}
        self.fine_lag_plot_widgets = {}
        self.fine_time_plots = {}  # Store time domain plots
        self.fine_time_plot_widgets = {}  # Store time domain plot widgets
        colors = pg.colormap.get("tab10", "matplotlib").getColors()

        # --- Fine Lag P00 Plot ---
        fine_lag_p00_widget = QtWidgets.QWidget()
        fine_lag_p00_layout = QtWidgets.QVBoxLayout(fine_lag_p00_widget)
        fine_lag_p00_cb = QtWidgets.QCheckBox("ADC0 Fine Lag (Raw)")
        fine_lag_p00_cb.setChecked(True)
        self.plot_checkboxes["fine_lag_s_p00"] = fine_lag_p00_cb
        fine_lag_p00_plot = pg.PlotWidget()
        fine_lag_p00_layout.addWidget(fine_lag_p00_cb)
        fine_lag_p00_layout.addWidget(fine_lag_p00_plot)
        self.fine_lag_plots[(0,0)] = fine_lag_p00_plot.plot(pen=colors[0], name='autocorr')
        self.fine_lag_plot_widgets[(0,0)] = fine_lag_p00_plot
        fine_lag_p00_plot.showGrid(x=True, y=True)
        fine_lag_p00_plot.setLabel('bottom', 'Distance', units='m')
        fine_lag_p00_plot.setLabel('left', 'Normalized Autocorrelation')
        fine_lag_p00_plot.addLegend()
        layout.addWidget(fine_lag_p00_widget, 0, 0)
        # Connect checkbox to hide/show the widget
        fine_lag_p00_cb.stateChanged.connect(lambda state, w=fine_lag_p00_widget: w.setVisible(state == QtCore.Qt.CheckState.Checked))

        # --- Fine Lag P11 Plot ---
        fine_lag_p11_widget = QtWidgets.QWidget()
        fine_lag_p11_layout = QtWidgets.QVBoxLayout(fine_lag_p11_widget)
        fine_lag_p11_cb = QtWidgets.QCheckBox("ADC1 Fine Lag (Raw)")
        fine_lag_p11_cb.setChecked(True)
        self.plot_checkboxes["fine_lag_s_p11"] = fine_lag_p11_cb
        fine_lag_p11_plot = pg.PlotWidget()
        fine_lag_p11_layout.addWidget(fine_lag_p11_cb)
        fine_lag_p11_layout.addWidget(fine_lag_p11_plot)
        self.fine_lag_plots[(1,1)] = fine_lag_p11_plot.plot(pen=colors[3], name='autocorr')
        self.fine_lag_plot_widgets[(1,1)] = fine_lag_p11_plot
        fine_lag_p11_plot.showGrid(x=True, y=True)
        fine_lag_p11_plot.setLabel('bottom', 'Distance', units='m')
        fine_lag_p11_plot.setLabel('left', 'Normalized Autocorrelation')
        fine_lag_p11_plot.addLegend()
        layout.addWidget(fine_lag_p11_widget, 0, 1)
        # Connect checkbox to hide/show the widget
        fine_lag_p11_cb.stateChanged.connect(lambda state, w=fine_lag_p11_widget: w.setVisible(state == QtCore.Qt.CheckState.Checked))
        
        # --- Fine Lag P01 Plot (Cross-correlation) ---
        fine_lag_p01_widget = QtWidgets.QWidget()
        fine_lag_p01_layout = QtWidgets.QVBoxLayout(fine_lag_p01_widget)
        fine_lag_p01_cb = QtWidgets.QCheckBox("ADC0/1 Cross-correlation")
        fine_lag_p01_cb.setChecked(True)
        self.plot_checkboxes["fine_lag_s_p01"] = fine_lag_p01_cb
        fine_lag_p01_plot = pg.PlotWidget()
        fine_lag_p01_layout.addWidget(fine_lag_p01_cb)
        fine_lag_p01_layout.addWidget(fine_lag_p01_plot)
        self.fine_lag_plots[(0,1)] = fine_lag_p01_plot.plot(pen=colors[1], name='cross-corr')
        self.fine_lag_plot_widgets[(0,1)] = fine_lag_p01_plot
        fine_lag_p01_plot.showGrid(x=True, y=True)
        fine_lag_p01_plot.setLabel('bottom', 'Distance', units='m')
        fine_lag_p01_plot.setLabel('left', 'Normalized Cross-correlation')
        fine_lag_p01_plot.addLegend()
        layout.addWidget(fine_lag_p01_widget, 1, 0, 1, 2)  # Span both columns
        # Connect checkbox to hide/show the widget
        fine_lag_p01_cb.stateChanged.connect(lambda state, w=fine_lag_p01_widget: w.setVisible(state == QtCore.Qt.CheckState.Checked))
        
        # --- Time Domain ADC0 Plot ---
        time_adc0_widget = QtWidgets.QWidget()
        time_adc0_layout = QtWidgets.QVBoxLayout(time_adc0_widget)
        time_adc0_cb = QtWidgets.QCheckBox("ADC0 Time Domain")
        time_adc0_cb.setChecked(True)
        self.plot_checkboxes["fine_time_adc0"] = time_adc0_cb
        time_adc0_plot = pg.PlotWidget()
        time_adc0_layout.addWidget(time_adc0_cb)
        time_adc0_layout.addWidget(time_adc0_plot)
        self.fine_time_plots[0] = time_adc0_plot.plot(pen=colors[0], name='ADC0')
        self.fine_time_plot_widgets[0] = time_adc0_plot
        time_adc0_plot.showGrid(x=True, y=True)
        time_adc0_plot.setLabel('bottom', 'Time', units='μs')
        time_adc0_plot.setLabel('left', 'Amplitude')
        time_adc0_plot.addLegend()
        layout.addWidget(time_adc0_widget, 2, 0)
        # Connect checkbox to hide/show the widget
        time_adc0_cb.stateChanged.connect(lambda state, w=time_adc0_widget: w.setVisible(state == QtCore.Qt.CheckState.Checked))
        
        # --- Time Domain ADC1 Plot ---
        time_adc1_widget = QtWidgets.QWidget()
        time_adc1_layout = QtWidgets.QVBoxLayout(time_adc1_widget)
        time_adc1_cb = QtWidgets.QCheckBox("ADC1 Time Domain")
        time_adc1_cb.setChecked(True)
        self.plot_checkboxes["fine_time_adc1"] = time_adc1_cb
        time_adc1_plot = pg.PlotWidget()
        time_adc1_layout.addWidget(time_adc1_cb)
        time_adc1_layout.addWidget(time_adc1_plot)
        self.fine_time_plots[1] = time_adc1_plot.plot(pen=colors[3], name='ADC1')
        self.fine_time_plot_widgets[1] = time_adc1_plot
        time_adc1_plot.showGrid(x=True, y=True)
        time_adc1_plot.setLabel('bottom', 'Time', units='μs')
        time_adc1_plot.setLabel('left', 'Amplitude')
        time_adc1_plot.addLegend()
        layout.addWidget(time_adc1_widget, 2, 1)
        # Connect checkbox to hide/show the widget
        time_adc1_cb.stateChanged.connect(lambda state, w=time_adc1_widget: w.setVisible(state == QtCore.Qt.CheckState.Checked))

    def _setup_fine_lag_waterfall_plots(self) -> None:
        """Set up the image views for the Fine Lag Waterfall tab."""
        layout = QtWidgets.QGridLayout(self.fine_lag_waterfall_tab)
        self.fine_lag_waterfall_plots = {}
        self.fine_lag_waterfall_images = {}
        self.fine_lag_waterfall_levels = {}
        self.fine_lag_waterfall_level_update_counter = {}
        
        viridis_cm = pg.colormap.get('viridis')

        titles = ["ADC0 Fine Lag", "ADC1 Fine Lag", "Cross-Correlation"]
        keys = ['fine_lag_p00', 'fine_lag_p11', 'fine_lag_p01']
        
        for i, (title, key) in enumerate(zip(titles, keys)):
            if i < 2:
                row, col = 0, i
            else:
                row, col = 1, 0  # Third plot on second row
            
            container = QtWidgets.QWidget()
            container_layout = QtWidgets.QVBoxLayout(container)
            
            cb = QtWidgets.QCheckBox(title)
            cb.setChecked(True)
            cb.stateChanged.connect(lambda state, w=container: w.setVisible(state == QtCore.Qt.CheckState.Checked))
            self.plot_checkboxes[f"fine_lag_wf_{key}"] = cb
            
            plot = pg.PlotWidget()
            plot.setLabel('left', 'Time')
            plot.setLabel('bottom', 'Distance', units='m')
            # Invert Y axis so 0 is at top (newest data)
            plot.invertY(True)
            
            img_item = pg.ImageItem()
            plot.addItem(img_item)
            img_item.setLookupTable(viridis_cm.getLookupTable())
            
            self.fine_lag_waterfall_plots[key] = plot
            self.fine_lag_waterfall_images[key] = img_item
            self.fine_lag_waterfall_levels[key] = None
            self.fine_lag_waterfall_level_update_counter[key] = 0
            
            container_layout.addWidget(cb)
            container_layout.addWidget(plot)
            
            layout.addWidget(container, row, col)

    def _setup_super_fft_spectra_plots(self) -> None:
        """Set up the plots for the Super FFT Spectra tab."""
        layout = QtWidgets.QGridLayout(self.super_fft_spectra_tab)
        
        self.super_fft_plots = {}
        self.super_fft_plot_widgets = {}
        colors = pg.colormap.get("tab10", "matplotlib").getColors()

        # --- Super FFT PSD Plot ---
        super_fft_widget = QtWidgets.QWidget()
        super_fft_layout = QtWidgets.QVBoxLayout(super_fft_widget)
        super_fft_cb = QtWidgets.QCheckBox("ADC0 Super-Resolution FFT")
        super_fft_cb.setChecked(True)
        self.plot_checkboxes["super_fft_psd"] = super_fft_cb
        super_fft_plot = pg.PlotWidget()
        super_fft_layout.addWidget(super_fft_cb)
        super_fft_layout.addWidget(super_fft_plot)
        self.super_fft_plots['psd'] = super_fft_plot.plot(pen=colors[0], name='PSD')
        self.super_fft_plot_widgets['psd'] = super_fft_plot
        super_fft_plot.showGrid(x=True, y=True)
        super_fft_plot.setLabel('bottom', 'Frequency', units='Hz')
        super_fft_plot.setLabel('left', 'Power Spectral Density')
        super_fft_plot.addLegend()
        layout.addWidget(super_fft_widget, 0, 0, 1, 2)  # Span both columns
        # Connect checkbox to hide/show the widget
        super_fft_cb.stateChanged.connect(lambda state, w=super_fft_widget: w.setVisible(state == QtCore.Qt.CheckState.Checked))
        
        # --- Time Domain Plot ---
        time_domain_widget = QtWidgets.QWidget()
        time_domain_layout = QtWidgets.QVBoxLayout(time_domain_widget)
        time_domain_cb = QtWidgets.QCheckBox("ADC0 Time Domain (16 Frames)")
        time_domain_cb.setChecked(True)
        self.plot_checkboxes["super_fft_time"] = time_domain_cb
        time_domain_plot = pg.PlotWidget()
        time_domain_layout.addWidget(time_domain_cb)
        time_domain_layout.addWidget(time_domain_plot)
        self.super_fft_plots['time'] = time_domain_plot.plot(pen=colors[2], name='ADC0')
        self.super_fft_plot_widgets['time'] = time_domain_plot
        time_domain_plot.showGrid(x=True, y=True)
        time_domain_plot.setLabel('bottom', 'Time', units='μs')
        time_domain_plot.setLabel('left', 'Amplitude')
        time_domain_plot.addLegend()
        layout.addWidget(time_domain_widget, 1, 0, 1, 2)  # Span both columns
        # Connect checkbox to hide/show the widget
        time_domain_cb.stateChanged.connect(lambda state, w=time_domain_widget: w.setVisible(state == QtCore.Qt.CheckState.Checked))
        
        # Calculate frequency axis for super FFT
        n_super_fft_bins = 131073
        self.super_fft_freq_axis = np.linspace(0, FS_HZ/2, n_super_fft_bins)
        
        # Calculate time axis for 16 frames (262,144 samples)
        n_super_fft_samples = 262144
        time_samples = np.arange(n_super_fft_samples)
        self.super_fft_time_axis_us = time_samples / FS_HZ * 1e6  # Convert to microseconds

    def _setup_super_fft_waterfall_plots(self) -> None:
        """Set up the image views for the Super FFT Waterfall tab."""
        layout = QtWidgets.QVBoxLayout(self.super_fft_waterfall_tab)
        self.super_fft_waterfall_plots = {}
        self.super_fft_waterfall_images = {}
        self.super_fft_waterfall_levels = {}
        self.super_fft_waterfall_level_update_counter = {}
        
        viridis_cm = pg.colormap.get('viridis')

        # Super FFT waterfall plot
        container = QtWidgets.QWidget()
        container_layout = QtWidgets.QVBoxLayout(container)
        
        cb = QtWidgets.QCheckBox("ADC0 Super-Resolution FFT")
        cb.setChecked(True)
        self.plot_checkboxes["super_fft_wf_psd"] = cb
        
        plot = pg.PlotWidget()
        plot.setLabel('left', 'Time')
        plot.setLabel('bottom', 'Frequency', units='Hz')
        # Invert Y axis so 0 is at top (newest data)
        plot.invertY(True)
        
        img_item = pg.ImageItem()
        plot.addItem(img_item)
        img_item.setLookupTable(viridis_cm.getLookupTable())
        
        self.super_fft_waterfall_plots['psd'] = plot
        self.super_fft_waterfall_images['psd'] = img_item
        self.super_fft_waterfall_levels['psd'] = None
        self.super_fft_waterfall_level_update_counter['psd'] = 0
        
        container_layout.addWidget(cb)
        container_layout.addWidget(plot)
        
        layout.addWidget(container)
        
        # Connect checkbox to hide/show the container
        cb.stateChanged.connect(lambda state: container.setVisible(state == QtCore.Qt.CheckState.Checked))
    
    def _setup_radar_azel_plots(self) -> None:
        """Set up the plots for the Radar Az/El tab."""
        layout = QtWidgets.QVBoxLayout(self.radar_azel_tab)
        
        self.radar_plots = {}
        self.radar_plot_widgets = {}
        colors = pg.colormap.get("tab10", "matplotlib").getColors()
        
        # Create a container widget with checkbox
        container = QtWidgets.QWidget()
        container_layout = QtWidgets.QVBoxLayout(container)
        
        cb = QtWidgets.QCheckBox("Radar Azimuth/Elevation View")
        cb.setChecked(True)
        self.plot_checkboxes["radar_azel"] = cb
        
        # Create the scatter plot
        plot = pg.PlotWidget()
        plot.setLabel('bottom', 'Azimuth', units='degrees')
        plot.setLabel('left', 'Elevation', units='degrees')
        plot.showGrid(x=True, y=True)
        plot.setXRange(-90, 90)
        plot.setYRange(0, 90)
        plot.setAspectLocked(False)
        
        # Create scatter plot item for current position
        self.radar_azel_scatter = pg.ScatterPlotItem(
            size=15, 
            pen=pg.mkPen(colors[0], width=2),
            brush=pg.mkBrush(colors[0]),
            symbol='o'
        )
        plot.addItem(self.radar_azel_scatter)
        
        # Create line plot for history trail
        self.radar_azel_trail = pg.PlotDataItem(
            pen=pg.mkPen(colors[0], width=1, style=QtCore.Qt.PenStyle.DashLine)
        )
        plot.addItem(self.radar_azel_trail)
        
        # Add text item for "No Detection"
        self.radar_azel_text = pg.TextItem(
            text="No Detection",
            color=(200, 200, 200),
            anchor=(0.5, 0.5)
        )
        self.radar_azel_text.setPos(0, 45)
        plot.addItem(self.radar_azel_text)
        
        self.radar_plots['azel'] = plot
        self.radar_plot_widgets['azel'] = plot
        
        container_layout.addWidget(cb)
        container_layout.addWidget(plot)
        
        layout.addWidget(container)
        
        # Connect checkbox
        cb.stateChanged.connect(lambda state, w=container: w.setVisible(state == QtCore.Qt.CheckState.Checked))
    
    def _setup_radar_debug_plots(self) -> None:
        """Set up the plots for the Radar Debug tab showing antenna-to-reference lag spectra."""
        layout = QtWidgets.QGridLayout(self.radar_debug_tab)
        
        self.radar_lag_plots = {}
        self.radar_lag_plot_widgets = {}
        colors = pg.colormap.get("tab10", "matplotlib").getColors()
        
        # Create lag axis for radar (16384 samples centered around zero)
        lag_samples = np.arange(16384) - 16384 // 2
        lag_time_s = lag_samples / FS_HZ
        c_m_per_s = 299792458.0
        self.radar_lag_axis = lag_time_s * c_m_per_s  # Convert to distance in meters (one-way)
        
        # --- ADC0-to-Reference Lag Plot ---
        adc0_widget = QtWidgets.QWidget()
        adc0_layout = QtWidgets.QVBoxLayout(adc0_widget)
        adc0_cb = QtWidgets.QCheckBox("ADC0-to-Reference Lag Spectrum")
        adc0_cb.setChecked(True)
        self.plot_checkboxes["radar_lag_adc0"] = adc0_cb
        adc0_plot = pg.PlotWidget()
        adc0_layout.addWidget(adc0_cb)
        adc0_layout.addWidget(adc0_plot)
        self.radar_lag_plots[0] = adc0_plot.plot(pen=colors[0], name='ADC0-Ref')
        self.radar_lag_plot_widgets[0] = adc0_plot
        adc0_plot.showGrid(x=True, y=True)
        adc0_plot.setLabel('bottom', 'Distance', units='m')
        adc0_plot.setLabel('left', 'Correlation Magnitude')
        adc0_plot.addLegend()
        layout.addWidget(adc0_widget, 0, 0)
        adc0_cb.stateChanged.connect(lambda state, w=adc0_widget: w.setVisible(state == QtCore.Qt.CheckState.Checked))
        
        # --- ADC1-to-Reference Lag Plot ---
        adc1_widget = QtWidgets.QWidget()
        adc1_layout = QtWidgets.QVBoxLayout(adc1_widget)
        adc1_cb = QtWidgets.QCheckBox("ADC1-to-Reference Lag Spectrum")
        adc1_cb.setChecked(True)
        self.plot_checkboxes["radar_lag_adc1"] = adc1_cb
        adc1_plot = pg.PlotWidget()
        adc1_layout.addWidget(adc1_cb)
        adc1_layout.addWidget(adc1_plot)
        self.radar_lag_plots[1] = adc1_plot.plot(pen=colors[1], name='ADC1-Ref')
        self.radar_lag_plot_widgets[1] = adc1_plot
        adc1_plot.showGrid(x=True, y=True)
        adc1_plot.setLabel('bottom', 'Distance', units='m')
        adc1_plot.setLabel('left', 'Correlation Magnitude')
        adc1_plot.addLegend()
        layout.addWidget(adc1_widget, 0, 1)
        adc1_cb.stateChanged.connect(lambda state, w=adc1_widget: w.setVisible(state == QtCore.Qt.CheckState.Checked))
        
        # --- ADC2-to-Reference Lag Plot ---
        adc2_widget = QtWidgets.QWidget()
        adc2_layout = QtWidgets.QVBoxLayout(adc2_widget)
        adc2_cb = QtWidgets.QCheckBox("ADC2-to-Reference Lag Spectrum")
        adc2_cb.setChecked(True)
        self.plot_checkboxes["radar_lag_adc2"] = adc2_cb
        adc2_plot = pg.PlotWidget()
        adc2_layout.addWidget(adc2_cb)
        adc2_layout.addWidget(adc2_plot)
        self.radar_lag_plots[2] = adc2_plot.plot(pen=colors[2], name='ADC2-Ref')
        self.radar_lag_plot_widgets[2] = adc2_plot
        adc2_plot.showGrid(x=True, y=True)
        adc2_plot.setLabel('bottom', 'Distance', units='m')
        adc2_plot.setLabel('left', 'Correlation Magnitude')
        adc2_plot.addLegend()
        layout.addWidget(adc2_widget, 1, 0, 1, 2)  # Span both columns
        adc2_cb.stateChanged.connect(lambda state, w=adc2_widget: w.setVisible(state == QtCore.Qt.CheckState.Checked))
    
    def _setup_radar_azrange_plots(self) -> None:
        """Set up the plots for the Radar Az/Range tab."""
        layout = QtWidgets.QVBoxLayout(self.radar_azrange_tab)
        
        colors = pg.colormap.get("tab10", "matplotlib").getColors()
        
        # Create a container widget with checkbox
        container = QtWidgets.QWidget()
        container_layout = QtWidgets.QVBoxLayout(container)
        
        cb = QtWidgets.QCheckBox("Radar Azimuth/Range View")
        cb.setChecked(True)
        self.plot_checkboxes["radar_azrange"] = cb
        
        # Create the scatter plot
        plot = pg.PlotWidget()
        plot.setLabel('bottom', 'Azimuth', units='degrees')
        plot.setLabel('left', 'Range', units='meters')
        plot.showGrid(x=True, y=True)
        plot.setXRange(-90, 90)
        plot.setYRange(0, 50)
        plot.setAspectLocked(False)
        
        # Create scatter plot item for current position
        self.radar_azrange_scatter = pg.ScatterPlotItem(
            size=15,
            pen=pg.mkPen(colors[1], width=2),
            brush=pg.mkBrush(colors[1]),
            symbol='o'
        )
        plot.addItem(self.radar_azrange_scatter)
        
        # Create line plot for history trail
        self.radar_azrange_trail = pg.PlotDataItem(
            pen=pg.mkPen(colors[1], width=1, style=QtCore.Qt.PenStyle.DashLine)
        )
        plot.addItem(self.radar_azrange_trail)
        
        # Add text item for "No Detection"
        self.radar_azrange_text = pg.TextItem(
            text="No Detection",
            color=(200, 200, 200),
            anchor=(0.5, 0.5)
        )
        self.radar_azrange_text.setPos(0, 25)
        plot.addItem(self.radar_azrange_text)
        
        self.radar_plots['azrange'] = plot
        self.radar_plot_widgets['azrange'] = plot
        
        container_layout.addWidget(cb)
        container_layout.addWidget(plot)
        
        layout.addWidget(container)
        
        # Connect checkbox
        cb.stateChanged.connect(lambda state, w=container: w.setVisible(state == QtCore.Qt.CheckState.Checked))
    
    def _setup_radar_xyz_plots(self) -> None:
        """Set up the 3D plot for the Radar 3D Position tab using matplotlib."""
        layout = QtWidgets.QVBoxLayout(self.radar_xyz_tab)
        
        # Create a container widget with checkbox
        container = QtWidgets.QWidget()
        container_layout = QtWidgets.QVBoxLayout(container)
        
        cb = QtWidgets.QCheckBox("Radar 3D Position View")
        cb.setChecked(True)
        self.plot_checkboxes["radar_xyz"] = cb
        
        if not MATPLOTLIB_3D_AVAILABLE:
            # Create fallback message
            fallback_label = QtWidgets.QLabel(
                "3D visualization unavailable.\n"
                "Matplotlib 3D is not installed.\n"
                "Please check Az/El and Az/Range tabs for 2D views."
            )
            fallback_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            fallback_label.setStyleSheet("font-size: 16px; padding: 50px;")
            
            self.radar_xyz_info_label = QtWidgets.QLabel("No Detection")
            self.radar_xyz_info_label.setStyleSheet("font-size: 16px; padding: 5px;")
            
            container_layout.addWidget(cb)
            container_layout.addWidget(self.radar_xyz_info_label)
            container_layout.addWidget(fallback_label)
            
            layout.addWidget(container)
            
            # Connect checkbox
            cb.stateChanged.connect(lambda state, w=container: w.setVisible(state == QtCore.Qt.CheckState.Checked))
            self.radar_xyz_view = None
            return
        
        # Create matplotlib figure and canvas
        self.radar_xyz_figure = Figure()
        self.radar_xyz_canvas = FigureCanvas(self.radar_xyz_figure)
        self.radar_xyz_canvas.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        self.radar_xyz_ax = self.radar_xyz_figure.add_subplot(111, projection='3d')
        
        # Set up the 3D axes
        self.radar_xyz_ax.set_xlabel('X (m)')
        self.radar_xyz_ax.set_ylabel('Y (m)')
        self.radar_xyz_ax.set_zlabel('Z (m)')
        self.radar_xyz_ax.set_xlim(-25, 25)
        self.radar_xyz_ax.set_ylim(-25, 25)
        self.radar_xyz_ax.set_zlim(-25, 25)
        
        # Add grid
        self.radar_xyz_ax.grid(True)
        
        # Initialize plot elements
        colors = pg.colormap.get("tab10", "matplotlib").getColors()
        
        # Debug: print color info
        print(f"Radar 3D: Color at index 2: {colors[2]}")
        print(f"Radar 3D: Color type: {type(colors[2])}, length: {len(colors[2])}")
        
        # Normalize colors to 0-1 range for matplotlib
        # PyQtGraph colors can be either tuples or arrays, and may have 3 or 4 components
        raw_color = colors[2]
        if len(raw_color) >= 3:
            # Take first 3 components (RGB)
            rgb = [float(raw_color[0]), float(raw_color[1]), float(raw_color[2])]
            # Check if in 0-255 range
            if any(c > 1.0 for c in rgb):
                color_normalized = [c/255.0 for c in rgb]
            else:
                color_normalized = rgb
        else:
            # Fallback color
            color_normalized = [0.2, 0.6, 0.2]  # Green
        
        print(f"Radar 3D: Normalized color: {color_normalized}")
        
        # Create scatter plot for current position
        self.radar_xyz_scatter = self.radar_xyz_ax.scatter(
            [0], [0], [0], 
            c=[color_normalized], 
            s=100, 
            marker='o',
            edgecolors='black',
            linewidths=2
        )
        
        # Create line plot for history trail
        self.radar_xyz_trail, = self.radar_xyz_ax.plot(
            [0], [0], [0], 
            color=color_normalized, 
            alpha=0.5, 
            linewidth=2,
            linestyle='--'
        )
        
        # Add antenna position markers
        antenna_positions = np.array([
            [0, 0, 0],      # ADC0 at origin
            [1, 0, 0],      # ADC1 along x-axis
            [0, 1, 0]       # ADC2 along y-axis
        ])
        
        self.antenna_markers = self.radar_xyz_ax.scatter(
            antenna_positions[:, 0],
            antenna_positions[:, 1],
            antenna_positions[:, 2],
            c='gray',
            s=80,
            marker='^',
            edgecolors='black',
            linewidths=1
        )
        
        # Add antenna labels
        for i, pos in enumerate(antenna_positions):
            self.radar_xyz_ax.text(
                pos[0], pos[1], pos[2] + 0.5,
                f'ADC{i}',
                fontsize=10,
                ha='center'
            )
        
        # Add labels for info display
        info_widget = QtWidgets.QWidget()
        info_layout = QtWidgets.QHBoxLayout(info_widget)
        
        self.radar_xyz_info_label = QtWidgets.QLabel("No Detection")
        self.radar_xyz_info_label.setStyleSheet("font-size: 16px; padding: 5px;")
        info_layout.addWidget(self.radar_xyz_info_label)
        info_layout.addStretch()
        
        self.radar_plots['xyz'] = self.radar_xyz_ax
        self.radar_plot_widgets['xyz'] = self.radar_xyz_canvas
        self.radar_xyz_view = self.radar_xyz_canvas  # For compatibility
        
        container_layout.addWidget(cb)
        container_layout.addWidget(info_widget)
        container_layout.addWidget(self.radar_xyz_canvas)
        
        layout.addWidget(container)
        
        # Connect checkbox
        cb.stateChanged.connect(lambda state, w=container: w.setVisible(state == QtCore.Qt.CheckState.Checked))
        
        # Set initial view angle
        self.radar_xyz_ax.view_init(elev=20, azim=45)
        
        # Draw the initial canvas
        self.radar_xyz_canvas.draw()

    def _setup_status_bar(self) -> None:
        """Initialize the status bar at the bottom of the window."""
        self.statusBar = QtWidgets.QStatusBar()
        self.setStatusBar(self.statusBar)
        self.status_label = QtWidgets.QLabel("Ready. Please connect to a board.")
        self.metrics_label = QtWidgets.QLabel("")
        self.adc_flags_label = QtWidgets.QLabel("ADC: OK")
        self.adc_flags_label.setStyleSheet("color: #90EE90;")  # Light green
        self.statusBar.addWidget(self.status_label, 1)  # Takes up remaining space
        self.statusBar.addWidget(self.metrics_label)    # Middle-right
        self.statusBar.addWidget(self.adc_flags_label)  # Bottom-right

    def connect_to_board(self) -> None:
        """Establish connection to the FPGA board."""
        try:
            self.serial_number = self.serial_input.text()
            self.status_label.setText(f"Connecting to CRS board with serial: {self.serial_number}...")
            QtWidgets.QApplication.processEvents()
            
            hardware_map = f'crs {self.serial_number}'
            self.pocket_correlator = POCKET_CORRELATOR(hwm=hardware_map, prog=2, stderr_log_level='debug')
            
            self.setWindowTitle(f"Periscope Correlator - Connected to {self.serial_number}")
            self.status_label.setText("Connection successful. Ready to calibrate and configure.")
            self.connect_button.setText("Disconnect")
            self.connect_button.clicked.disconnect()
            self.connect_button.clicked.connect(self.disconnect_from_board)
            self.calibrate_button.setEnabled(True)
            self.configure_button.setEnabled(True)
            self.fine_lag_mode_button.setEnabled(True)  # Enable fine lag mode button after connection
            self.super_fft_mode_button.setEnabled(True)  # Enable super FFT mode button after connection
            self.radar_mode_button.setEnabled(True)  # Enable radar mode button after connection
            self.serial_input.setEnabled(False)
            print("Connection successful.")
        except Exception as e:
            print(f"Failed to connect: {e}")
            self.status_label.setText(f"Failed to connect: {e}")
            QtWidgets.QMessageBox.critical(self, "Connection Error", str(e))

    def calibrate_gains(self) -> None:
        """Perform gain calibration on the connected board."""
        if not self.pocket_correlator: return
        try:
            self.status_label.setText("Calibrating gains...")
            QtWidgets.QApplication.processEvents()
            
            target = self.gain_target_spinner.value()
            print(f"Computing gains with target: {target:.2f}")
            _, _, gain_lin, gain_log, _ = self.pocket_correlator.compute_gains(
                target=target, n_fft_frames=100, bank=0, return_scaler_frames=True, save_gains=False)
            
            self.computed_gains = gain_lin * (2**gain_log)
            self._update_gain_factor()
            self.status_label.setText("Gain calibration successful.")
            print("Gains computed successfully.")
        except Exception as e:
            print(f"Gain calibration failed: {e}")
            self.status_label.setText(f"Gain calibration failed: {e}")
            QtWidgets.QMessageBox.critical(self, "Calibration Error", str(e))

    def configure_correlator(self) -> None:
        """Configure the correlator and start the data stream."""
        if not self.pocket_correlator: return
        try:
            self.status_label.setText("Configuring correlator...")
            QtWidgets.QApplication.processEvents()
            
            # Update configuration
            self.config.waterfall_len = self.waterfall_len_spinner.value()
            
            self.pocket_correlator.configure_corr(autocorr_only=0, no_accum=0, n_firmware_frames=self.config.n_firmware_frames - 1)
            if self.pocket_correlator.ucap.OUTPUT_SOURCE_SEL != 1:
                print("Setting UCAP output source to Correlator (1)...")
                self.pocket_correlator.ucap.OUTPUT_SOURCE_SEL = 1
            
            sock = self.pocket_correlator.corr_receiver.socket
            sock.settimeout(0.1)
            
            # Create shared memory buffers
            self.shared_buffers = SharedDataBuffers(waterfall_len=self.config.waterfall_len)
            
            # Create and start receiver thread
            self.receiver_thread = UDPReceiver(sock, self.shared_buffers, self.packet_dtype, self.config, self.pocket_correlator)
            self.receiver_thread.data_ready_callback = lambda: self.data_ready.emit()
            self.receiver_thread.start()
            
            # Create and start lag computer thread if checkbox is enabled
            self.lag_computer_thread = LagSpectraComputer(self.shared_buffers)
            self.lag_computer_thread.start()
            if self.enable_lag_checkbox.isChecked():
                self.lag_computer_thread.set_compute_enabled(True)
            
            # Change button to "Stop Streaming"
            self.configure_button.setText("Stop Streaming")
            self.configure_button.clicked.disconnect()
            self.configure_button.clicked.connect(self.stop_streaming)
            # Disable calibrate button while streaming
            self.calibrate_button.setEnabled(False)
            # Enable apply waterfall length button while streaming
            self.apply_waterfall_len_button.setEnabled(True)
            self.status_label.setText("Correlator configured. Streaming data...")
            print("Correlator configured and streaming.")
        except Exception as e:
            print(f"Correlator configuration failed: {e}")
            self.status_label.setText(f"Correlator configuration failed: {e}")
            QtWidgets.QMessageBox.critical(self, "Configuration Error", str(e))
    
    def stop_streaming(self) -> None:
        """Stop the data stream and re-enable configuration."""
        self.status_label.setText("Stopping data stream...")
        QtWidgets.QApplication.processEvents()
        
        # Stop all workers
        self.stop_workers()
        
        # Change button back to "Configure Correlator"
        self.configure_button.setText("Configure Correlator")
        self.configure_button.clicked.disconnect()
        self.configure_button.clicked.connect(self.configure_correlator)
        # Re-enable calibrate button
        self.calibrate_button.setEnabled(True)
        
        self.status_label.setText("Data stream stopped. Ready to reconfigure.")
        print("Data stream stopped.")

    def stop_workers(self) -> None:
        """Stop all background threads."""
        print("Stopping workers...")
        if self.receiver_thread and self.receiver_thread.is_alive():
            self.receiver_thread.stop()
            self.receiver_thread.join(timeout=2)
        
        if self.lag_computer_thread and self.lag_computer_thread.is_alive():
            self.lag_computer_thread.stop()
            self.lag_computer_thread.join(timeout=2)
        
        if self.shared_buffers:
            self.shared_buffers.cleanup()
            self.shared_buffers = None
        print("Workers stopped.")

    def disconnect_from_board(self) -> None:
        """Disconnect from the board and reset the UI."""
        self.stop_workers()
        if self.pocket_correlator: 
            self.pocket_correlator = None
        
        self.setWindowTitle("Periscope Correlator - Not Connected")
        self.connect_button.setText("Connect")
        if not self.connect_button.signalsBlocked():
             self.connect_button.clicked.disconnect()
        self.connect_button.clicked.connect(self.connect_to_board)
        
        self.serial_input.setEnabled(True)
        self.calibrate_button.setEnabled(False)
        self.configure_button.setEnabled(False)
        self.status_label.setText("Disconnected. Ready to connect.")
        self.metrics_label.setText("")
        print("Disconnected.")

    def _calculate_frequency_axis(self, zone: int) -> np.ndarray:
        """Calculate frequency axis for the specified Nyquist zone.
        
        Args:
            zone: Nyquist zone number (1, 2, or 3)
            
        Returns:
            Frequency axis array in Hz
        """
        # Base frequency axis (0 to FS_HZ/2)
        base_freq_axis = (FS_HZ / M_FACTOR) * np.arange(NBINS_TOTAL)
        
        if zone == 1:
            # Zone 1: 0 to 1600 MHz
            return base_freq_axis
        elif zone == 2:
            # Zone 2: 1600 to 3200 MHz (reversed due to aliasing)
            # In the second Nyquist zone, frequencies are aliased and appear reversed
            reversed_axis = base_freq_axis[::-1]  # Reverse the frequency order
            return reversed_axis + FS_HZ / 2
        elif zone == 3:
            # Zone 3: 3200 to 4800 MHz
            return base_freq_axis + FS_HZ
        else:
            print(f"Warning: Invalid Nyquist zone {zone}, defaulting to Zone 1")
            return base_freq_axis
    
    def _update_all_frequency_axes(self, zone: int) -> None:
        """Update all frequency axes for the specified Nyquist zone."""
        self.current_nyquist_zone = zone
        self.freq_axis = self._calculate_frequency_axis(zone)
        
        # Update super FFT frequency axis
        n_super_fft_bins = 131073
        base_super_fft_freq_axis = np.linspace(0, FS_HZ/2, n_super_fft_bins)
        
        if zone == 1:
            self.super_fft_freq_axis = base_super_fft_freq_axis
        elif zone == 2:
            # Zone 2: 1600 to 3200 MHz (reversed due to aliasing)
            reversed_super_fft_axis = base_super_fft_freq_axis[::-1]  # Reverse the frequency order
            self.super_fft_freq_axis = reversed_super_fft_axis + FS_HZ / 2
        elif zone == 3:
            self.super_fft_freq_axis = base_super_fft_freq_axis + FS_HZ
        else:
            self.super_fft_freq_axis = base_super_fft_freq_axis
    
    def _on_nyquist_zone_changed(self, index: int) -> None:
        """Handle Nyquist zone selection change."""
        zone = index + 1  # Convert 0-based index to 1-based zone number
        print(f"Switching to Nyquist Zone {zone}")
        
        # Update frequency axes
        self._update_all_frequency_axes(zone)
        
        # Update status
        zone_ranges = {
            1: "0-1600 MHz",
            2: "1600-3200 MHz", 
            3: "3200-4800 MHz"
        }
        self.status_label.setText(f"Nyquist Zone {zone} selected ({zone_ranges[zone]})")
        
        # Force plot updates by triggering a refresh
        self._update_plots()

    def _precalculate_indices(self) -> None:
        """Pre-calculate the indices for the products to be plotted."""
        try:
            self.config.idx00 = np.where((self.prod_map[:, 0] == 0) & (self.prod_map[:, 1] == 0))[0][0]
            self.config.idx11 = np.where((self.prod_map[:, 0] == 1) & (self.prod_map[:, 1] == 1))[0][0]
            self.config.idx01 = np.where((self.prod_map[:, 0] == 0) & (self.prod_map[:, 1] == 1))[0][0]
        except IndexError:
            print("Warning: Could not find product indices for initial plots.")

    def _update_gain_factor(self) -> None:
        """Calculate the gain normalization factor from the computed gains."""
        if self.computed_gains is None:
            self.config.gain_factor = None
            return
        
        print("Pre-calculating gain normalization factor...")
        gains = self.computed_gains.copy()
        gains[gains == 0] = 1e-9
        
        p0_indices = self.prod_map[:, 0]
        p1_indices = self.prod_map[:, 1]
        
        # Extract gains for each product - this gives shape (NPROD, NBINS)
        gains_p0 = gains[p0_indices]  # Shape: (36, 8192)
        gains_p1 = gains[p1_indices]  # Shape: (36, 8192)
        
        gain_product = gains_p0 * gains_p1  # Shape: (36, 8192)
        
        # Use logarithmic computation to avoid numerical issues with large powers
        # Instead of (2**44)**2 / gain_product, compute exp(log(2**44)**2 - log(gain_product))
        # This is numerically more stable than dealing with 2**88 directly
        log_scale_factor = 2 * 44 * np.log(2)  # log((2**44)**2)
        log_gain_product = np.log(gain_product)
        log_gain_factor = log_scale_factor - log_gain_product
        
        # Convert back from log space
        # Transpose to shape (NBINS, NPROD) to match normalized_frame shape
        self.config.gain_factor = np.exp(log_gain_factor).astype(np.complex128).T
        
        # Debug output to check values
        print(f"Gain factor shape: {self.config.gain_factor.shape}")
        print(f"Gain factor range: {np.min(np.abs(self.config.gain_factor)):.2e} to {np.max(np.abs(self.config.gain_factor)):.2e}")
        print("Gain normalization factor updated.")

    def _update_waterfall_length(self, value: int) -> None:
        """Handle changes to the waterfall length spinner."""
        self.config.waterfall_len = value
        # Enable apply button if streaming in any mode
        is_streaming = False
        if self.receiver_thread and self.receiver_thread.is_alive():
            is_streaming = True
        elif self.fine_lag_mode_button.isChecked() and self.fine_lag_computer_thread and self.fine_lag_computer_thread.is_alive():
            is_streaming = True
        elif self.super_fft_mode_button.isChecked() and self.super_fft_computer_thread and self.super_fft_computer_thread.is_alive():
            is_streaming = True
            
        if is_streaming and self.shared_buffers and value != self.shared_buffers.waterfall_len:
            self.apply_waterfall_len_button.setEnabled(True)
            print(f"Waterfall length set to {value}. Click Apply to update.")
        else:
            self.apply_waterfall_len_button.setEnabled(False)
            if self.shared_buffers:
                print(f"Waterfall length set to {value}. Already at this length or not streaming.")
    
    def _apply_waterfall_length(self) -> None:
        """Apply new waterfall length to running stream."""
        if not self.shared_buffers:
            return
            
        new_len = self.waterfall_len_spinner.value()
        if new_len == self.shared_buffers.waterfall_len:
            print("Waterfall length unchanged")
            return
            
        print(f"Applying new waterfall length: {new_len}")
        self.status_label.setText(f"Updating waterfall length to {new_len}...")
        
        # Temporarily pause the appropriate thread based on current mode
        old_receiver_running = False
        old_fine_lag_enabled = False
        old_super_fft_enabled = False
        
        # Handle normal mode
        if self.receiver_thread and self.receiver_thread.is_alive():
            old_receiver_running = self.receiver_thread.running
            self.receiver_thread.running = False
            
        # Handle fine lag mode
        if self.fine_lag_computer_thread and self.fine_lag_mode_button.isChecked():
            old_fine_lag_enabled = self.fine_lag_computer_thread.enabled.is_set()
            self.fine_lag_computer_thread.set_enabled(False)
            
        # Handle super FFT mode
        if self.super_fft_computer_thread and self.super_fft_mode_button.isChecked():
            old_super_fft_enabled = self.super_fft_computer_thread.enabled.is_set()
            self.super_fft_computer_thread.set_enabled(False)
            
        # Wait for operations to complete
        time.sleep(0.1)
            
        # Create new shared buffers with the new waterfall length
        old_buffers = self.shared_buffers
        self.shared_buffers = SharedDataBuffers(waterfall_len=new_len)
        
        # Copy current frame data to new buffers (only if in normal mode)
        if old_receiver_running:
            with old_buffers.lock:
                self.shared_buffers.p00[:] = old_buffers.p00
                self.shared_buffers.p11[:] = old_buffers.p11
                self.shared_buffers.p01_real[:] = old_buffers.p01_real
                self.shared_buffers.p01_imag[:] = old_buffers.p01_imag
            
        # Update all threads with new buffers
        if self.receiver_thread:
            self.receiver_thread.shared_buffers = self.shared_buffers
            self.receiver_thread.running = old_receiver_running
            
        if self.lag_computer_thread:
            self.lag_computer_thread.shared_buffers = self.shared_buffers
            
        if self.fine_lag_computer_thread:
            self.fine_lag_computer_thread.shared_buffers = self.shared_buffers
            if old_fine_lag_enabled:
                self.fine_lag_computer_thread.set_enabled(True)
            
        if self.super_fft_computer_thread:
            self.super_fft_computer_thread.shared_buffers = self.shared_buffers
            if old_super_fft_enabled:
                self.super_fft_computer_thread.set_enabled(True)
            
        # Clean up old buffers
        old_buffers.cleanup()
        
        # Update config
        self.config.waterfall_len = new_len
        
        # Disable apply button until next change
        self.apply_waterfall_len_button.setEnabled(False)
        
        self.status_label.setText(f"Waterfall length updated to {new_len}")
        print(f"Waterfall length successfully updated to {new_len}")
    
    def _restore_all_plots(self) -> None:
        """Restore all hidden plots by checking all plot checkboxes."""
        # Check all plot checkboxes to make all plots visible
        for checkbox in self.plot_checkboxes.values():
            checkbox.setChecked(True)
        
        self.status_label.setText("All plots restored")
    
    def _toggle_viewbox_mode(self, state: int) -> None:
        """Toggle between normal pan mode and rect zoom mode."""
        is_checked = (state == QtCore.Qt.CheckState.Checked)
        
        # Define the mouse mode for each plot widget
        if is_checked:
            # RectMode allows drawing a rectangle to zoom
            mode = pg.ViewBox.RectMode
        else:
            # PanMode is the default - drag to pan
            mode = pg.ViewBox.PanMode
        
        # Apply to all plot widgets in the spectra tab
        for plot_widget in self.plot_widgets.values():
            viewbox = plot_widget.getViewBox()
            viewbox.setMouseMode(mode)
            
        # Apply to all waterfall plots
        for plot in self.waterfall_plots.values():
            viewbox = plot.getViewBox()
            viewbox.setMouseMode(mode)
            
        # Apply to lag plot widgets
        if hasattr(self, 'lag_plot_widgets'):
            for plot_widget in self.lag_plot_widgets.values():
                viewbox = plot_widget.getViewBox()
                viewbox.setMouseMode(mode)
                
            # Apply to lag waterfall plots
            if hasattr(self, 'lag_waterfall_plots'):
                for plot in self.lag_waterfall_plots.values():
                    viewbox = plot.getViewBox()
                    viewbox.setMouseMode(mode)
                
        # Apply to fine lag plot widgets
        if hasattr(self, 'fine_lag_plot_widgets'):
            for plot_widget in self.fine_lag_plot_widgets.values():
                viewbox = plot_widget.getViewBox()
                viewbox.setMouseMode(mode)
                
        # Apply to fine lag waterfall plots
        if hasattr(self, 'fine_lag_waterfall_plots'):
            for plot in self.fine_lag_waterfall_plots.values():
                viewbox = plot.getViewBox()
                viewbox.setMouseMode(mode)
                
        # Apply to fine time plot widgets (time domain plots)
        if hasattr(self, 'fine_time_plot_widgets'):
            for plot_widget in self.fine_time_plot_widgets.values():
                viewbox = plot_widget.getViewBox()
                viewbox.setMouseMode(mode)
        
        # Apply to super FFT plot widgets
        if hasattr(self, 'super_fft_plot_widgets'):
            for plot_widget in self.super_fft_plot_widgets.values():
                viewbox = plot_widget.getViewBox()
                viewbox.setMouseMode(mode)
                
        # Apply to super FFT waterfall plots
        if hasattr(self, 'super_fft_waterfall_plots'):
            for plot in self.super_fft_waterfall_plots.values():
                viewbox = plot.getViewBox()
                viewbox.setMouseMode(mode)
            
        status_text = "ViewBox mode enabled - click and drag to zoom" if is_checked else "ViewBox mode disabled - click and drag to pan"
        self.status_label.setText(status_text)
    
    def _toggle_lag_computation(self, state: int) -> None:
        """Toggle lag computation on/off."""
        if self.lag_computer_thread:
            enabled = (state == QtCore.Qt.CheckState.Checked)
            self.lag_computer_thread.set_compute_enabled(enabled)
            status = "enabled" if enabled else "disabled"
            print(f"Lag computation {status}")
            
    def _update_frames_to_average(self, value: int) -> None:
        """Update the number of frames to average."""
        if hasattr(self, 'config'):
            self.config.frames_to_average = value
            # TODO: Apply this to running threads
            print(f"Frames to average set to: {value}")
    
    def _toggle_fine_lag_mode(self, checked: bool) -> None:
        """Toggle between normal mode and fine lag mode."""
        if not self.pocket_correlator:
            self.fine_lag_mode_button.setChecked(False)
            return
            
        if checked:
            print("Switching to Fine Lag Mode...")
            self.status_label.setText("Switching to Fine Lag Mode...")
            
            # Stop normal threads
            if self.receiver_thread and self.receiver_thread.is_alive():
                self.receiver_thread.stop()
                self.receiver_thread.join(timeout=2)
            
            if self.lag_computer_thread and self.lag_computer_thread.is_alive():
                self.lag_computer_thread.set_compute_enabled(False)
            
            # Create shared buffers if they don't exist
            if not self.shared_buffers:
                waterfall_len = self.waterfall_len_spinner.value()
                self.shared_buffers = SharedDataBuffers(waterfall_len=waterfall_len)
            
            # Create and start fine lag computer if needed
            if not self.fine_lag_computer_thread:
                self.fine_lag_computer_thread = FineLagComputer(self.pocket_correlator, self.shared_buffers, self.config)
                self.fine_lag_computer_thread.start()
            
            self.fine_lag_computer_thread.set_enabled(True)
            
            # Update UI
            self.fine_lag_mode_button.setText("Disable Fine Lag Mode")
            self.status_label.setText("Fine Lag Mode Active - cm resolution")
            
            # Disable normal operation controls
            self.calibrate_button.setEnabled(False)
            self.configure_button.setEnabled(False)
            self.enable_lag_checkbox.setEnabled(False)
            
            # Switch to fine lag tab
            self.tabs.setCurrentIndex(4)  # Fine Lag Spectra tab
            
            # Start a timer to trigger plot updates for fine lag mode
            if not hasattr(self, 'fine_lag_timer'):
                self.fine_lag_timer = QtCore.QTimer()
                self.fine_lag_timer.timeout.connect(self._update_plots)
            self.fine_lag_timer.start(50)  # Update at 20 Hz
            
        else:
            print("Switching back to Normal Mode...")
            self.status_label.setText("Switching back to Normal Mode...")
            
            # Stop the fine lag timer
            if hasattr(self, 'fine_lag_timer'):
                self.fine_lag_timer.stop()
            
            # Stop fine lag thread
            if self.fine_lag_computer_thread:
                self.fine_lag_computer_thread.set_enabled(False)
            
            # Restart normal operation if it was configured
            if hasattr(self, 'config') and self.config and self.shared_buffers and hasattr(self.pocket_correlator, 'corr_receiver'):
                try:
                    # Restart receiver thread
                    sock = self.pocket_correlator.corr_receiver.socket
                    sock.settimeout(0.1)
                    
                    self.receiver_thread = UDPReceiver(sock, self.shared_buffers, self.packet_dtype, self.config, self.pocket_correlator)
                    self.receiver_thread.data_ready_callback = lambda: self.data_ready.emit()
                    self.receiver_thread.start()
                    
                    # Re-enable lag computation if it was enabled
                    if self.enable_lag_checkbox.isChecked() and self.lag_computer_thread:
                        self.lag_computer_thread.set_compute_enabled(True)
                except Exception as e:
                    print(f"Could not restart normal mode receiver: {e}")
            
            # Update UI
            self.fine_lag_mode_button.setText("Enable Fine Lag Mode")
            self.status_label.setText("Normal Mode Active")
            
            # Re-enable normal controls
            self.calibrate_button.setEnabled(True)
            self.configure_button.setEnabled(True)  # Re-enable configure button
            self.enable_lag_checkbox.setEnabled(True)
            
            # Switch back to spectra tab
            self.tabs.setCurrentIndex(0)

    def _toggle_super_fft_mode(self, checked: bool) -> None:
        """Toggle between normal mode and super-resolution FFT mode."""
        if not self.pocket_correlator:
            self.super_fft_mode_button.setChecked(False)
            return
            
        if checked:
            print("Switching to Super-Resolution FFT Mode...")
            self.status_label.setText("Switching to Super-Resolution FFT Mode...")
            
            # Make sure fine lag mode is off
            if self.fine_lag_mode_button.isChecked():
                self.fine_lag_mode_button.setChecked(False)
                self._toggle_fine_lag_mode(False)
            
            # Stop normal threads
            if self.receiver_thread and self.receiver_thread.is_alive():
                self.receiver_thread.stop()
                self.receiver_thread.join(timeout=2)
            
            if self.lag_computer_thread and self.lag_computer_thread.is_alive():
                self.lag_computer_thread.set_compute_enabled(False)
            
            # Create shared buffers if they don't exist
            if not self.shared_buffers:
                waterfall_len = self.waterfall_len_spinner.value()
                self.shared_buffers = SharedDataBuffers(waterfall_len=waterfall_len)
            
            # Create and start super FFT computer if needed
            if not self.super_fft_computer_thread:
                self.super_fft_computer_thread = SuperFFTComputer(self.pocket_correlator, self.shared_buffers)
                self.super_fft_computer_thread.start()
            
            self.super_fft_computer_thread.set_enabled(True)
            
            # Update UI
            self.super_fft_mode_button.setText("Disable Super-Resolution FFT")
            self.status_label.setText("Super-Resolution FFT Mode Active - 12.2 kHz resolution")
            
            # Disable normal operation controls and fine lag mode
            self.calibrate_button.setEnabled(False)
            self.configure_button.setEnabled(False)
            self.enable_lag_checkbox.setEnabled(False)
            self.fine_lag_mode_button.setEnabled(False)
            
            # Switch to super FFT tab
            self.tabs.setCurrentIndex(6)  # Super FFT Spectra tab
            
            # Start a timer to trigger plot updates for super FFT mode
            if not hasattr(self, 'super_fft_timer'):
                self.super_fft_timer = QtCore.QTimer()
                self.super_fft_timer.timeout.connect(self._update_plots)
            self.super_fft_timer.start(100)  # Update at 10 Hz
            
        else:
            print("Switching back to Normal Mode...")
            self.status_label.setText("Switching back to Normal Mode...")
            
            # Stop the super FFT timer
            if hasattr(self, 'super_fft_timer'):
                self.super_fft_timer.stop()
            
            # Stop super FFT thread
            if self.super_fft_computer_thread:
                self.super_fft_computer_thread.set_enabled(False)
            
            # Restart normal operation if it was configured
            if hasattr(self, 'config') and self.config and self.shared_buffers and hasattr(self.pocket_correlator, 'corr_receiver'):
                try:
                    # Restart receiver thread
                    sock = self.pocket_correlator.corr_receiver.socket
                    sock.settimeout(0.1)
                    
                    self.receiver_thread = UDPReceiver(sock, self.shared_buffers, self.packet_dtype, self.config, self.pocket_correlator)
                    self.receiver_thread.data_ready_callback = lambda: self.data_ready.emit()
                    self.receiver_thread.start()
                    
                    # Re-enable lag computation if it was enabled
                    if self.enable_lag_checkbox.isChecked() and self.lag_computer_thread:
                        self.lag_computer_thread.set_compute_enabled(True)
                except Exception as e:
                    print(f"Could not restart normal mode receiver: {e}")
            
            # Update UI
            self.super_fft_mode_button.setText("Enable Super-Resolution FFT")
            self.status_label.setText("Normal Mode Active")
            
            # Re-enable normal controls
            self.calibrate_button.setEnabled(True)
            self.configure_button.setEnabled(True)
            self.enable_lag_checkbox.setEnabled(True)
            self.fine_lag_mode_button.setEnabled(True)
            
            # Switch back to spectra tab
            self.tabs.setCurrentIndex(0)

    def _toggle_radar_mode(self, checked: bool) -> None:
        """Toggle between normal mode and 3D radar localization mode."""
        if not self.pocket_correlator:
            self.radar_mode_button.setChecked(False)
            return
            
        if checked:
            print("Switching to Radar Mode...")
            self.status_label.setText("Switching to 3D Radar Localization Mode...")
            
            # Make sure other modes are off
            if self.fine_lag_mode_button.isChecked():
                self.fine_lag_mode_button.setChecked(False)
                self._toggle_fine_lag_mode(False)
            if self.super_fft_mode_button.isChecked():
                self.super_fft_mode_button.setChecked(False)
                self._toggle_super_fft_mode(False)
            
            # Stop normal threads
            if self.receiver_thread and self.receiver_thread.is_alive():
                self.receiver_thread.stop()
                self.receiver_thread.join(timeout=2)
            
            if self.lag_computer_thread and self.lag_computer_thread.is_alive():
                self.lag_computer_thread.set_compute_enabled(False)
            
            # Create shared buffers if they don't exist
            if not self.shared_buffers:
                waterfall_len = self.waterfall_len_spinner.value()
                self.shared_buffers = SharedDataBuffers(waterfall_len=waterfall_len)
            
            # Create and start radar computer if needed
            if not self.radar_computer_thread:
                self.radar_computer_thread = RadarComputer(self.pocket_correlator, self.shared_buffers, self.config)
                self.radar_computer_thread.start()
            
            self.radar_computer_thread.set_enabled(True)
            
            # Update UI
            self.radar_mode_button.setText("Disable Radar Mode")
            self.status_label.setText("Radar Mode Active - 3D Localization")
            
            # Disable normal operation controls and other modes
            self.calibrate_button.setEnabled(False)
            self.configure_button.setEnabled(False)
            self.enable_lag_checkbox.setEnabled(False)
            self.fine_lag_mode_button.setEnabled(False)
            self.super_fft_mode_button.setEnabled(False)
            
            # Enable radar threshold spinner
            self.radar_threshold_spinner.setEnabled(True)
            
            # Switch to radar Az/El tab
            self.tabs.setCurrentIndex(8)  # Radar Az/El tab
            
            # Start a timer to trigger plot updates for radar mode
            if not hasattr(self, 'radar_timer'):
                self.radar_timer = QtCore.QTimer()
                self.radar_timer.timeout.connect(self._update_plots)
            self.radar_timer.start(50)  # Update at 20 Hz
            
        else:
            print("Switching back to Normal Mode...")
            self.status_label.setText("Switching back to Normal Mode...")
            
            # Stop the radar timer
            if hasattr(self, 'radar_timer'):
                self.radar_timer.stop()
            
            # Stop radar thread
            if self.radar_computer_thread:
                self.radar_computer_thread.set_enabled(False)
            
            # Restart normal operation if it was configured
            if hasattr(self, 'config') and self.config and self.shared_buffers and hasattr(self.pocket_correlator, 'corr_receiver'):
                try:
                    # Restart receiver thread
                    sock = self.pocket_correlator.corr_receiver.socket
                    sock.settimeout(0.1)
                    
                    self.receiver_thread = UDPReceiver(sock, self.shared_buffers, self.packet_dtype, self.config)
                    self.receiver_thread.data_ready_callback = lambda: self.data_ready.emit()
                    self.receiver_thread.start()
                    
                    # Re-enable lag computation if it was enabled
                    if self.enable_lag_checkbox.isChecked() and self.lag_computer_thread:
                        self.lag_computer_thread.set_compute_enabled(True)
                except Exception as e:
                    print(f"Could not restart normal mode receiver: {e}")
            
            # Update UI
            self.radar_mode_button.setText("Enable Radar Mode")
            self.status_label.setText("Normal Mode Active")
            
            # Re-enable normal controls
            self.calibrate_button.setEnabled(True)
            self.configure_button.setEnabled(True)
            self.enable_lag_checkbox.setEnabled(True)
            self.fine_lag_mode_button.setEnabled(True)
            self.super_fft_mode_button.setEnabled(True)
            
            # Disable radar threshold spinner
            self.radar_threshold_spinner.setEnabled(False)
            
            # Switch back to spectra tab
            self.tabs.setCurrentIndex(0)
    
    def _update_radar_threshold(self, value: float) -> None:
        """Update the radar peak detection threshold."""
        if self.radar_computer_thread:
            self.radar_computer_thread.set_threshold(value)
            print(f"Radar threshold set to: {value:.2f}")

    def _update_adc_status(self) -> None:
        """Update the ADC status display in the status bar."""
        if not self.shared_buffers:
            return
            
        total_captures = self.shared_buffers.total_captures
        adc_or_count = self.shared_buffers.adc_or_count
        adc_ov_count = self.shared_buffers.adc_ov_count
        
        if total_captures == 0:
            # No captures yet
            self.adc_flags_label.setText("ADC: OK")
            self.adc_flags_label.setStyleSheet("color: #90EE90;")  # Light green
        elif adc_or_count == 0 and adc_ov_count == 0:
            # No issues detected
            self.adc_flags_label.setText(f"ADC: OK ({total_captures})")
            self.adc_flags_label.setStyleSheet("color: #90EE90;")  # Light green
        else:
            # Issues detected
            or_rate = (adc_or_count / total_captures) * 100 if total_captures > 0 else 0
            ov_rate = (adc_ov_count / total_captures) * 100 if total_captures > 0 else 0
            
            status_parts = []
            if adc_or_count > 0:
                status_parts.append(f"OR: {or_rate:.1f}%")
            if adc_ov_count > 0:
                status_parts.append(f"OV: {ov_rate:.1f}%")
                
            status_text = f"ADC: {', '.join(status_parts)}"
            self.adc_flags_label.setText(status_text)
            
            # Use red for any overranges or overvoltages
            self.adc_flags_label.setStyleSheet("color: #FF6B6B;")  # Light red

    def _update_plots(self) -> None:
        """Update all plots with new data from the shared buffers."""
        if not self.shared_buffers:
            return

        # Check which tab is active
        current_tab_index = self.tabs.currentIndex()
        
        # Check if in radar mode
        if self.radar_mode_button.isChecked() and current_tab_index >= 8:
            with self.shared_buffers.radar_lock:
                if current_tab_index == 8:  # Radar Az/El Tab
                    self._update_radar_azel_plot()
                elif current_tab_index == 9:  # Radar Az/Range Tab
                    self._update_radar_azrange_plot()
                elif current_tab_index == 10:  # Radar 3D Tab
                    self._update_radar_xyz_plot()
                elif current_tab_index == 11:  # Radar Debug Tab
                    self._update_radar_debug_plot()
                    
            # Update FPS for radar mode
            if self.radar_computer_thread:
                fps = self.radar_computer_thread.fps
                self.metrics_label.setText(f"Radar FPS: {fps:.2f}")
        # Check if in super FFT mode
        elif self.super_fft_mode_button.isChecked() and current_tab_index >= 6:
            with self.shared_buffers.super_fft_lock:
                if current_tab_index == 6:  # Super FFT Spectra Tab
                    self._update_super_fft_spectra_plots()
                elif current_tab_index == 7:  # Super FFT Waterfall Tab
                    self._update_super_fft_waterfall_plots()
                    
            # Update FPS for super FFT mode
            if self.super_fft_computer_thread:
                fps = self.super_fft_computer_thread.fps
                self.metrics_label.setText(f"Super FFT FPS: {fps:.2f}")
        # Check if in fine lag mode
        elif self.fine_lag_mode_button.isChecked() and current_tab_index >= 4:
            with self.shared_buffers.fine_lag_lock:
                if current_tab_index == 4:  # Fine Lag Spectra Tab
                    self._update_fine_lag_spectra_plots()
                elif current_tab_index == 5:  # Fine Lag Waterfall Tab
                    self._update_fine_lag_waterfall_plots()
                    
            # Update FPS for fine lag mode
            if self.fine_lag_computer_thread:
                fps = self.fine_lag_computer_thread.fps
                self.metrics_label.setText(f"Fine Lag FPS: {fps:.2f}")
        else:
            # Normal mode
            if not self.receiver_thread:
                return
                
            with self.shared_buffers.lock:
                if current_tab_index == 0:  # Spectra Tab
                    self._update_spectra_plots()
                elif current_tab_index == 1:  # Waterfall Tab
                    self._update_waterfall_plots()
            
            # For lag tabs, use lag_lock instead
            if current_tab_index >= 2 and current_tab_index < 4 and self.lag_computer_thread and self.lag_computer_thread.compute_enabled.is_set():
                with self.shared_buffers.lag_lock:
                    if current_tab_index == 2:  # Lag Spectra Tab
                        self._update_lag_spectra_plots()
                    elif current_tab_index == 3:  # Lag Waterfall Tab
                        self._update_lag_waterfall_plots()
                
            # Update FPS and lag FPS if available
            if self.receiver_thread:
                fps = self.receiver_thread.fps
                metrics_text = f"Processing FPS: {fps:.2f}"
                
                if self.lag_computer_thread and self.lag_computer_thread.compute_enabled.is_set():
                    lag_fps = self.lag_computer_thread.fps
                    metrics_text += f" | Lag FPS: {lag_fps:.2f}"
                    
                self.metrics_label.setText(metrics_text)
                
                # Update ADC flag status
                self._update_adc_status()

    def _update_spectra_plots(self) -> None:
        """Update the line plots in the Spectra tab."""
        if not self.shared_buffers:
            return
        use_log_scale = self.log_scale_checkbox.isChecked()
        
        # Update plot log modes based on checkbox
        self.plot_widgets[(0,0)].setLogMode(y=use_log_scale)
        self.plot_widgets[(1,1)].setLogMode(y=use_log_scale)
        # For cross-correlation abs, use log scale if enabled
        self.plot_widgets[(0,1,'abs')].setLogMode(y=use_log_scale)
        # Real plot can use log scale if enabled (for symmetry log)
        self.plot_widgets[(0,1,'real')].setLogMode(y=False)
        
        # Only update plots that are checked (using zone-adjusted frequency axis)
        if self.plot_checkboxes["s_p00"].isChecked():
            self.plots[(0,0)].setData(x=self.freq_axis, y=self.shared_buffers.p00)
            
        if self.plot_checkboxes["s_p11"].isChecked():
            self.plots[(1,1)].setData(x=self.freq_axis, y=self.shared_buffers.p11)
            
        # Calculate complex visibility from real and imaginary parts
        complex_vis = self.shared_buffers.p01_real + 1j * self.shared_buffers.p01_imag
        
        if self.plot_checkboxes["s_p01_abs"].isChecked():
            # Calculate magnitude
            abs_data = np.abs(complex_vis)
            self.plots[(0,1,'abs')].setData(x=self.freq_axis, y=abs_data)
            
        if self.plot_checkboxes["s_p01_real"].isChecked():
            # Show real part
            real_data = self.shared_buffers.p01_real
            
            # Apply symmetric log transformation if log scale is enabled
            if use_log_scale:
                # Simple symmetric log transformation: sign * log(1 + |x|/C)
                C = self.symlog_crossover_spinner.value()
                sign_data = np.sign(real_data)
                abs_data = np.abs(real_data)
                # Apply symlog transformation
                symlog_data = sign_data * np.log10(1 + abs_data / C)
                self.plots[(0,1,'real')].setData(x=self.freq_axis, y=symlog_data)
                # Update y-axis label to indicate symlog scale
                self.plot_widgets[(0,1,'real')].setLabel('left', 'Real Part (symlog)')
            else:
                self.plots[(0,1,'real')].setData(x=self.freq_axis, y=real_data)
                # Reset y-axis label
                self.plot_widgets[(0,1,'real')].setLabel('left', 'Real Part')

    def _update_waterfall_plots(self) -> None:
        """Update the waterfall plots with new data."""
        if not self.shared_buffers:
            return
        # Data is already in correct order with newest at top (index 0)
        
        # Calculate frequency axis
        freq_min = self.freq_axis[0]
        freq_max = self.freq_axis[-1]
        
        # Helper function to update a waterfall plot
        def update_waterfall(key, data, is_magnitude=True):
            """Update a single waterfall plot with proper scaling and levels."""
            if not self.plot_checkboxes[f"wf_{key}"].isChecked():
                return
                
            # Data is already ordered with newest at top
            wf_data = data
            
            # Apply mean subtraction if enabled
            if self.mean_subtract_checkbox.isChecked():
                # Only use populated frames for mean calculation
                frames_written = self.shared_buffers.frames_written
                if frames_written > 0:
                    # Only calculate mean over the populated part of the buffer
                    populated_data = wf_data[-frames_written:] if frames_written < self.shared_buffers.waterfall_len else wf_data
                    bin_means = np.mean(populated_data, axis=0, keepdims=True)
                    wf_data = wf_data - bin_means
            
            # Update level counter
            self.waterfall_level_update_counter[key] += 1
            
            # Get the scale update interval from the spinner
            scale_update_interval = self.scale_update_spinner.value()
            
            # Update levels every N frames or if not set
            if self.waterfall_levels[key] is None or self.waterfall_level_update_counter[key] >= scale_update_interval:
                # Calculate robust levels using percentiles
                # Filter out zeros and invalid data for better scaling
                valid_data = wf_data[np.isfinite(wf_data)]
                
                if is_magnitude:
                    # For magnitude data (autocorrelations)
                    valid_data = valid_data[valid_data > 0]
                    if len(valid_data) > 0:
                        # Use log scale for better dynamic range
                        log_data = np.log10(valid_data + 1e-10)
                        vmin_log = np.percentile(log_data, 5)
                        vmax_log = np.percentile(log_data, 99)
                        vmin = 10**vmin_log
                        vmax = 10**vmax_log
                    else:
                        vmin, vmax = 0, 1
                else:
                    # For real/imaginary data (cross-correlations)
                    if len(valid_data) > 0:
                        # Use more robust percentiles for better dynamic range
                        percentiles = np.percentile(valid_data, [5, 95])
                        vmin, vmax = percentiles
                        
                        # For symlog scale, we want to capture the full dynamic range
                        # Don't force perfect symmetry, but ensure we include zero
                        if vmin > 0:
                            vmin = -vmax * 0.1  # Ensure some negative range
                        elif vmax < 0:
                            vmax = -vmin * 0.1  # Ensure some positive range
                        
                        # Add headroom to prevent saturation
                        # For symlog, we want to see the full range
                        vmin *= 1.5
                        vmax *= 1.5
                    else:
                        vmin, vmax = -1, 1
                
                self.waterfall_levels[key] = (vmin, vmax)
                self.waterfall_level_update_counter[key] = 0
            
            # Set image with fixed levels
            img = self.waterfall_images[key]
            use_log_scale = self.log_scale_checkbox.isChecked()
            
            if use_log_scale:
                if is_magnitude:
                    # For power data, display on log scale
                    # Add small epsilon to avoid log(0)
                    log_wf_data = np.log10(wf_data + 1e-10)
                    vmin, vmax = self.waterfall_levels[key]
                    log_levels = (np.log10(vmin + 1e-10), np.log10(vmax + 1e-10))
                    img.setImage(log_wf_data.T, autoLevels=False, levels=log_levels)
                else:
                    # For real/imaginary data (cross-correlations), use proper symmetric log scale
                    C = self.symlog_crossover_spinner.value()
                    vmin, vmax = self.waterfall_levels[key]
                    
                    # Create symlog normalizer
                    norm = SymLogNorm(vmin, vmax, C)
                    
                    # Transform data to [0,1] range using proper symlog normalization
                    normalized_data = norm.forward(wf_data)
                    
                    # Map to colormap range (using full range for better color distribution)
                    # PyQtGraph expects levels between 0 and 1 for normalized data
                    img.setImage(normalized_data.T, autoLevels=False, levels=(0, 1))
            else:
                # Linear scale
                img.setImage(wf_data.T, autoLevels=False, levels=self.waterfall_levels[key])
            
            # Set the scale to show frequency on x-axis and time on y-axis
            # Update every time in case Nyquist zone changed
            img.setRect(QtCore.QRectF(freq_min, 0, freq_max - freq_min, self.shared_buffers.waterfall_len))
            
            # Update plot labels (only need to do once)
            if not hasattr(img, '_labels_set'):
                plot = self.waterfall_plots[key]
                plot.setLabel('bottom', 'Frequency', units='Hz')
                plot.setLabel('left', 'Time (frames ago)')
                img._labels_set = True  # Mark that labels have been set
        
        # Update each waterfall plot
        update_waterfall('p00', self.shared_buffers.wf_p00, is_magnitude=True)
        update_waterfall('p11', self.shared_buffers.wf_p11, is_magnitude=True)
        update_waterfall('p01_real', self.shared_buffers.wf_p01_real, is_magnitude=False)
        
        # For phase waterfall, calculate phase from real and imaginary parts
        if self.plot_checkboxes["wf_p01_phase"].isChecked():
            # Data is already ordered with newest at top
            wf_real = self.shared_buffers.wf_p01_real
            wf_imag = self.shared_buffers.wf_p01_imag
            
            # Calculate phase from complex values
            complex_wf = wf_real + 1j * wf_imag
            phase_wf = np.angle(complex_wf)
            
            # Create a custom update for phase data
            key = 'p01_phase'
            wf_data = phase_wf
            
            # Apply mean subtraction if enabled
            if self.mean_subtract_checkbox.isChecked():
                frames_written = self.shared_buffers.frames_written
                if frames_written > 0:
                    populated_data = wf_data[-frames_written:] if frames_written < self.shared_buffers.waterfall_len else wf_data
                    bin_means = np.mean(populated_data, axis=0, keepdims=True)
                    wf_data = wf_data - bin_means
            
            # Update level counter
            self.waterfall_level_update_counter[key] += 1
            
            # Get the scale update interval
            scale_update_interval = self.scale_update_spinner.value()
            
            # Update levels for phase data  
            if self.waterfall_levels[key] is None or self.waterfall_level_update_counter[key] >= scale_update_interval:
                valid_data = wf_data[np.isfinite(wf_data)]
                if len(valid_data) > 0:
                    # Phase data ranges from -pi to pi
                    vmin, vmax = -np.pi, np.pi
                else:
                    vmin, vmax = -np.pi, np.pi
                    
                self.waterfall_levels[key] = (vmin, vmax)
                self.waterfall_level_update_counter[key] = 0
            
            # Set image with fixed levels
            img = self.waterfall_images[key]
            
            # Phase data should use linear scale
            img.setImage(wf_data.T, autoLevels=False, levels=self.waterfall_levels[key])
            
            # Set the scale to show frequency on x-axis (only need to do once)
            if not hasattr(img, '_scale_set'):
                img.setRect(QtCore.QRectF(freq_min, 0, freq_max - freq_min, self.shared_buffers.waterfall_len))
                img._scale_set = True
                
                plot = self.waterfall_plots[key]
                plot.setLabel('bottom', 'Frequency', units='Hz')

    def _update_lag_spectra_plots(self) -> None:
        """Update the lag spectra line plots."""
        if not self.shared_buffers:
            return
        use_log_scale = self.log_scale_checkbox.isChecked()
        
        # Update plot log modes based on checkbox
        self.lag_plot_widgets[(0,0)].setLogMode(y=use_log_scale)
        self.lag_plot_widgets[(1,1)].setLogMode(y=use_log_scale)
        self.lag_plot_widgets[(0,1,'mag')].setLogMode(y=use_log_scale)
        
        # For autocorrelations, show only positive lags (symmetric function)
        positive_mask = self.lag_axis >= 0
        positive_lags = self.lag_axis[positive_mask]
        
        # Only update plots that are checked
        if self.plot_checkboxes["lag_s_p00"].isChecked():
            y_data = self.shared_buffers.lag_p00[positive_mask]
            self.lag_plots[(0,0)].setData(x=positive_lags, y=y_data)
            
        if self.plot_checkboxes["lag_s_p11"].isChecked():
            y_data = self.shared_buffers.lag_p11[positive_mask]
            self.lag_plots[(1,1)].setData(x=positive_lags, y=y_data)
            
        if self.plot_checkboxes["lag_s_p01_mag"].isChecked():
            # For cross-correlation, show full range
            y_data = self.shared_buffers.lag_p01_mag
            self.lag_plots[(0,1,'mag')].setData(x=self.lag_axis, y=y_data)
    
    def _update_lag_waterfall_plots(self) -> None:
        """Update the lag waterfall plots."""
        if not self.shared_buffers:
            return
        # Get the current write position to properly display circular buffer
        pos = self.shared_buffers.lag_wf_write_pos
        
        # Create properly ordered views of the circular buffers
        def get_ordered_waterfall(data, pos):
            """Get waterfall data in correct time order."""
            return np.vstack([data[pos:], data[:pos]])
        
        # Calculate lag axis range
        lag_min = self.lag_axis[0]
        lag_max = self.lag_axis[-1]
        
        # Helper function to update a lag waterfall plot
        def update_lag_waterfall(key, data, is_magnitude=True, positive_only=False):
            """Update a single lag waterfall plot with proper scaling and levels."""
            plot_key = f"lag_wf_{key}"
            if not self.plot_checkboxes[plot_key].isChecked():
                return
                
            wf_data = get_ordered_waterfall(data, pos)
            
            # Apply mean subtraction if enabled
            if self.mean_subtract_checkbox.isChecked():
                frames_written = self.shared_buffers.lag_frames_written
                if frames_written > 0:
                    populated_data = wf_data[-frames_written:] if frames_written < self.shared_buffers.waterfall_len else wf_data
                    bin_means = np.mean(populated_data, axis=0, keepdims=True)
                    wf_data = wf_data - bin_means
            
            # Update level counter
            self.lag_waterfall_level_update_counter[key] += 1
            
            # Get the scale update interval from the spinner
            scale_update_interval = self.scale_update_spinner.value()
            
            # Update levels every N frames or if not set
            if self.lag_waterfall_levels[key] is None or self.lag_waterfall_level_update_counter[key] >= scale_update_interval:
                # Calculate robust levels using percentiles
                valid_data = wf_data[np.isfinite(wf_data)]
                
                if is_magnitude:
                    # For magnitude data
                    if len(valid_data) > 0:
                        vmin = np.percentile(valid_data, 5)
                        vmax = np.percentile(valid_data, 95)
                    else:
                        vmin, vmax = 0, 1
                else:
                    # For phase data
                    if len(valid_data) > 0:
                        # Phase data ranges from -pi to pi
                        vmin, vmax = -np.pi, np.pi
                    else:
                        vmin, vmax = -np.pi, np.pi
                
                self.lag_waterfall_levels[key] = (vmin, vmax)
                self.lag_waterfall_level_update_counter[key] = 0
            
            # Set image with fixed levels
            img = self.lag_waterfall_images[key]
            use_log_scale = self.log_scale_checkbox.isChecked()
            
            if use_log_scale and is_magnitude:
                # For power data, display on log scale
                # When mean subtraction is on, data can be negative, so clip to positive values
                epsilon = 1e-10
                clipped_data = np.maximum(wf_data, epsilon)
                log_wf_data = np.log10(clipped_data)
                vmin, vmax = self.lag_waterfall_levels[key]
                # Also clip the levels to avoid log of negative values
                vmin_clipped = max(vmin, epsilon)
                vmax_clipped = max(vmax, epsilon)
                log_levels = (np.log10(vmin_clipped), np.log10(vmax_clipped))
                img.setImage(log_wf_data.T, autoLevels=False, levels=log_levels)
            else:
                # Linear scale
                img.setImage(wf_data.T, autoLevels=False, levels=self.lag_waterfall_levels[key])
            
            # Set the scale to show lag on x-axis (only need to do once)
            if not hasattr(img, '_scale_set'):
                # Set the image rectangle to map bins to lag time
                if positive_only:
                    # For autocorrelations, only show positive distances
                    x_min = 0
                    x_max = lag_max
                else:
                    # For cross-correlations, show full range
                    x_min = lag_min
                    x_max = lag_max
                img.setRect(QtCore.QRectF(x_min, 0, x_max - x_min, self.shared_buffers.waterfall_len))
                img._scale_set = True
                
                # Update plot labels
                plot = self.lag_waterfall_plots[key]
                plot.setLabel('bottom', 'Distance', units='m')
                plot.setLabel('left', 'Time (frames ago)')
        
        # For autocorrelations, only show positive lags
        positive_start = NBINS_TOTAL // 2  # Start at zero lag
        
        # Update autocorrelation waterfalls with only positive lags
        update_lag_waterfall('lag_p00', self.shared_buffers.lag_wf_p00[:, positive_start:], is_magnitude=True, positive_only=True)
        update_lag_waterfall('lag_p11', self.shared_buffers.lag_wf_p11[:, positive_start:], is_magnitude=True, positive_only=True)
        
        # For cross-correlation, show full range
        update_lag_waterfall('lag_p01_mag', self.shared_buffers.lag_wf_p01_mag, is_magnitude=True, positive_only=False)
        # Phase is not used for delay spectra

    def _update_fine_lag_spectra_plots(self) -> None:
        """Update the fine lag spectra line plots."""
        if not self.shared_buffers:
            return
        use_log_scale = self.log_scale_checkbox.isChecked()
        
        # Update plot log modes based on checkbox
        # For normalized autocorrelation (0-1 range), log scale might not be ideal but respect user preference
        self.fine_lag_plot_widgets[(0,0)].setLogMode(y=use_log_scale)
        self.fine_lag_plot_widgets[(1,1)].setLogMode(y=use_log_scale)
        self.fine_lag_plot_widgets[(0,1)].setLogMode(y=use_log_scale)
        
        # Only update plots that are checked
        if self.plot_checkboxes["fine_lag_s_p00"].isChecked():
            self.fine_lag_plots[(0,0)].setData(x=self.fine_lag_axis, y=self.shared_buffers.fine_lag_p00)
            
        if self.plot_checkboxes["fine_lag_s_p11"].isChecked():
            self.fine_lag_plots[(1,1)].setData(x=self.fine_lag_axis, y=self.shared_buffers.fine_lag_p11)
            
        if self.plot_checkboxes["fine_lag_s_p01"].isChecked():
            self.fine_lag_plots[(0,1)].setData(x=self.fine_lag_axis, y=self.shared_buffers.fine_lag_p01)
        
        # Update time domain plots
        if self.plot_checkboxes["fine_time_adc0"].isChecked():
            # Plot raw ADC0 time domain data - only the actual samples captured
            n_samples = self.shared_buffers.fine_time_n_samples
            time_axis = self.time_axis_us_full[:n_samples]
            adc0_data = self.shared_buffers.fine_time_adc0[:n_samples]
            self.fine_time_plots[0].setData(x=time_axis, y=adc0_data)
            
        if self.plot_checkboxes["fine_time_adc1"].isChecked():
            # Plot raw ADC1 time domain data - only the actual samples captured
            n_samples = self.shared_buffers.fine_time_n_samples
            time_axis = self.time_axis_us_full[:n_samples]
            adc1_data = self.shared_buffers.fine_time_adc1[:n_samples]
            self.fine_time_plots[1].setData(x=time_axis, y=adc1_data)

    def _update_fine_lag_waterfall_plots(self) -> None:
        """Update the fine lag waterfall plots."""
        if not self.shared_buffers:
            return
        # Get the current write position to properly display circular buffer
        pos = self.shared_buffers.fine_lag_wf_write_pos
        
        # Create properly ordered views of the circular buffers
        def get_ordered_waterfall(data, pos):
            """Get waterfall data in correct time order."""
            return np.vstack([data[pos:], data[:pos]])
        
        # Calculate fine lag axis range (in meters)
        fine_lag_min = self.fine_lag_axis[0]
        fine_lag_max = self.fine_lag_axis[-1]
        
        # Get log scale setting
        use_log_scale = self.log_scale_checkbox.isChecked()
        
        # Helper function to update a fine lag waterfall plot
        def update_fine_lag_waterfall(key, data):
            """Update a single fine lag waterfall plot with proper scaling and levels."""
            plot_key = f"fine_lag_wf_{key}"
            if not self.plot_checkboxes[plot_key].isChecked():
                return
                
            wf_data = get_ordered_waterfall(data, pos)
            
            # Apply mean subtraction if enabled
            if self.mean_subtract_checkbox.isChecked():
                frames_written = self.shared_buffers.fine_lag_frames_written
                if frames_written > 0:
                    populated_data = wf_data[-frames_written:] if frames_written < self.shared_buffers.waterfall_len else wf_data
                    bin_means = np.mean(populated_data, axis=0, keepdims=True)
                    wf_data = wf_data - bin_means
            
            # Update level counter
            self.fine_lag_waterfall_level_update_counter[key] += 1
            
            # Get the scale update interval from the spinner
            scale_update_interval = self.scale_update_spinner.value()
            
            # Update levels every N frames or if not set
            if self.fine_lag_waterfall_levels[key] is None or self.fine_lag_waterfall_level_update_counter[key] >= scale_update_interval:
                self.fine_lag_waterfall_level_update_counter[key] = 0
                # Calculate robust levels using percentiles
                valid_data = wf_data[np.isfinite(wf_data)]
                
                # For normalized autocorrelation data
                if len(valid_data) > 0:
                    # Use percentiles for more robust scaling (like other waterfalls)
                    vmin = min(valid_data) #np.percentile(valid_data, 5)
                    vmax = max(valid_data) #np.percentile(valid_data, 95)
                    
                    # For normalized data, ensure we don't go below 0 for log scale
                    if use_log_scale:
                        vmin = max(vmin, 1e-6)  # Prevent log of negative/zero
                        vmax = max(vmax, vmin * 10)  # Ensure reasonable dynamic range
                else:
                    vmin, vmax = 1e-6, 1
                
                self.fine_lag_waterfall_levels[key] = (vmin, vmax)
                self.fine_lag_waterfall_level_update_counter[key] = 0
            
            # Set image with fixed levels
            img = self.fine_lag_waterfall_images[key]
            
            if use_log_scale:
                # For normalized autocorrelation data, use log scale to better visualize small values
                # When mean subtraction is on, data can be negative, so clip to positive values
                epsilon = 1e-10
                clipped_data = np.maximum(wf_data, epsilon)
                log_wf_data = np.log10(clipped_data)
                vmin, vmax = self.fine_lag_waterfall_levels[key]
                # Also clip the levels to avoid log of negative values
                vmin_clipped = max(vmin, epsilon)
                vmax_clipped = max(vmax, epsilon)
                log_levels = (np.log10(vmin_clipped), np.log10(vmax_clipped))
                img.setImage(log_wf_data.T, autoLevels=False, levels=log_levels)
            else:
                # Linear scale
                img.setImage(wf_data.T, autoLevels=False, levels=self.fine_lag_waterfall_levels[key])
            
            # Set the scale to show distance on x-axis (only need to do once)
            if not hasattr(img, '_scale_set'):
                # Set the image rectangle to map bins to distance
                img.setRect(QtCore.QRectF(fine_lag_min, 0, fine_lag_max - fine_lag_min, self.shared_buffers.waterfall_len))
                img._scale_set = True
                
                # Update plot labels
                plot = self.fine_lag_waterfall_plots[key]
                plot.setLabel('bottom', 'Distance', units='m')
                plot.setLabel('left', 'Time (frames ago)')
        
        # Update fine lag waterfall plots
        update_fine_lag_waterfall('fine_lag_p00', self.shared_buffers.fine_lag_wf_p00)
        update_fine_lag_waterfall('fine_lag_p11', self.shared_buffers.fine_lag_wf_p11)
        update_fine_lag_waterfall('fine_lag_p01', self.shared_buffers.fine_lag_wf_p01)

    def _update_super_fft_spectra_plots(self) -> None:
        """Update the super FFT spectra line plots."""
        if not self.shared_buffers:
            return
        use_log_scale = self.log_scale_checkbox.isChecked()
        
        # Update plot log mode based on checkbox
        self.super_fft_plot_widgets['psd'].setLogMode(y=use_log_scale)
        
        # Only update plot if it's checked
        if self.plot_checkboxes["super_fft_psd"].isChecked():
            self.super_fft_plots['psd'].setData(x=self.super_fft_freq_axis, y=self.shared_buffers.super_fft_psd)
            
        # Update time domain plot
        if self.plot_checkboxes["super_fft_time"].isChecked():
            self.super_fft_plots['time'].setData(x=self.super_fft_time_axis_us, y=self.shared_buffers.super_fft_time)

    def _update_super_fft_waterfall_plots(self) -> None:
        """Update the super FFT waterfall plots."""
        if not self.shared_buffers:
            return
        # Get the current write position to properly display circular buffer
        pos = self.shared_buffers.super_fft_wf_write_pos
        
        # Create properly ordered views of the circular buffer
        def get_ordered_waterfall(data, pos):
            """Get waterfall data in correct time order."""
            return np.vstack([data[pos:], data[:pos]])
        
        # Calculate frequency axis range
        freq_min = self.super_fft_freq_axis[0]
        freq_max = self.super_fft_freq_axis[-1]
        
        # Update super FFT waterfall
        key = 'psd'
        if not self.plot_checkboxes["super_fft_wf_psd"].isChecked():
            return
            
        wf_data = get_ordered_waterfall(self.shared_buffers.super_fft_wf_psd, pos)
        
        # Apply mean subtraction if enabled
        if self.mean_subtract_checkbox.isChecked():
            frames_written = self.shared_buffers.super_fft_frames_written
            if frames_written > 0:
                populated_data = wf_data[-frames_written:] if frames_written < self.shared_buffers.waterfall_len else wf_data
                bin_means = np.mean(populated_data, axis=0, keepdims=True)
                wf_data = wf_data - bin_means
        
        # Update level counter
        self.super_fft_waterfall_level_update_counter[key] += 1
        
        # Get the scale update interval from the spinner
        scale_update_interval = self.scale_update_spinner.value()
        
        # Update levels every N frames or if not set
        if self.super_fft_waterfall_levels[key] is None or self.super_fft_waterfall_level_update_counter[key] >= scale_update_interval:
            # Calculate robust levels using percentiles
            valid_data = wf_data[np.isfinite(wf_data) & (wf_data > 0)]
            
            if len(valid_data) > 0:
                # Use log scale for better dynamic range
                log_data = np.log10(valid_data + 1e-10)
                vmin_log = np.percentile(log_data, 5)
                vmax_log = np.percentile(log_data, 99)
                vmin = 10**vmin_log
                vmax = 10**vmax_log
            else:
                vmin, vmax = 1e-10, 1
            
            self.super_fft_waterfall_levels[key] = (vmin, vmax)
            self.super_fft_waterfall_level_update_counter[key] = 0
        
        # Set image with fixed levels
        img = self.super_fft_waterfall_images[key]
        use_log_scale = self.log_scale_checkbox.isChecked()
        
        if use_log_scale:
            # For power data, display on log scale
            epsilon = 1e-10
            clipped_data = np.maximum(wf_data, epsilon)
            log_wf_data = np.log10(clipped_data)
            vmin, vmax = self.super_fft_waterfall_levels[key]
            vmin_clipped = max(vmin, epsilon)
            vmax_clipped = max(vmax, epsilon)
            log_levels = (np.log10(vmin_clipped), np.log10(vmax_clipped))
            img.setImage(log_wf_data.T, autoLevels=False, levels=log_levels)
        else:
            # Linear scale
            img.setImage(wf_data.T, autoLevels=False, levels=self.super_fft_waterfall_levels[key])
        
        # Set the scale to show frequency on x-axis (update every time in case Nyquist zone changed)
        # Set the image rectangle to map bins to frequency
        # Note: we need to handle the fact that we have many more bins (131k vs 8k)
        # The waterfall should show the full frequency range
        img.setRect(QtCore.QRectF(freq_min, 0, freq_max - freq_min, self.shared_buffers.waterfall_len))
        
        # Update plot labels
        plot = self.super_fft_waterfall_plots[key]
        plot.setLabel('bottom', 'Frequency', units='Hz')
        plot.setLabel('left', 'Time (frames ago)')
    
    def _update_radar_azel_plot(self) -> None:
        """Update the radar azimuth/elevation plot."""
        if not self.shared_buffers or not self.plot_checkboxes["radar_azel"].isChecked():
            return
            
        # Get current position (always available, even if stale)
        azimuth = self.shared_buffers.radar_azimuth[0]
        elevation = self.shared_buffers.radar_elevation[0]
        
        # Check if we have a valid detection
        if self.shared_buffers.radar_detection_valid[0]:
            # Valid detection - show with normal style
            colors = pg.colormap.get("tab10", "matplotlib").getColors()
            self.radar_azel_scatter.setData(
                [azimuth], [elevation],
                pen=pg.mkPen(colors[0], width=2),
                brush=pg.mkBrush(colors[0])
            )
            
            # Update trail with history
            history_count = self.shared_buffers.radar_history_count
            if history_count > 0:
                # Get historical positions (newest first)
                az_history = self.shared_buffers.radar_history_azimuth[:history_count]
                el_history = self.shared_buffers.radar_history_elevation[:history_count]
                
                # Reverse to show oldest to newest
                az_trail = az_history[::-1]
                el_trail = el_history[::-1]
                
                # Update trail line
                self.radar_azel_trail.setData(az_trail, el_trail)
            
            # Hide "No Detection" text
            self.radar_azel_text.setVisible(False)
        else:
            # No valid detection but we have last known position
            if azimuth != 0 or elevation != 0:  # Check if we have any position data
                # Show last position with different style (hollow/gray)
                self.radar_azel_scatter.setData(
                    [azimuth], [elevation],
                    pen=pg.mkPen((150, 150, 150), width=2),  # Gray outline
                    brush=pg.mkBrush(None)  # Hollow
                )
                # Keep showing the trail
                history_count = self.shared_buffers.radar_history_count
                if history_count > 0:
                    az_history = self.shared_buffers.radar_history_azimuth[:history_count]
                    el_history = self.shared_buffers.radar_history_elevation[:history_count]
                    az_trail = az_history[::-1]
                    el_trail = el_history[::-1]
                    self.radar_azel_trail.setData(az_trail, el_trail)
                
                # Update text to show stale data
                self.radar_azel_text.setText("Last Known Position (Stale)")
                self.radar_azel_text.setVisible(True)
            else:
                # No data at all
                self.radar_azel_scatter.setData([], [])
                self.radar_azel_trail.setData([], [])
                self.radar_azel_text.setText("No Detection")
                self.radar_azel_text.setVisible(True)
    
    def _update_radar_xyz_plot(self) -> None:
        """Update the radar 3D position plot."""
        if not self.shared_buffers or not self.plot_checkboxes["radar_xyz"].isChecked():
            return
            
        # Skip if matplotlib 3D is not available
        if not MATPLOTLIB_3D_AVAILABLE or not hasattr(self, 'radar_xyz_scatter'):
            return
            
        # Get current position (always available, even if stale)
        range_m = self.shared_buffers.radar_range[0]
        azimuth = self.shared_buffers.radar_azimuth[0]
        elevation = self.shared_buffers.radar_elevation[0]
        
        # Check if we have a valid detection
        if self.shared_buffers.radar_detection_valid[0]:
            # Valid detection - convert spherical to Cartesian coordinates
            azimuth_rad = np.radians(azimuth)
            elevation_rad = np.radians(elevation)
            
            x = range_m * np.cos(elevation_rad) * np.cos(azimuth_rad)
            y = range_m * np.cos(elevation_rad) * np.sin(azimuth_rad)
            z = range_m * np.sin(elevation_rad)
            
            # Update scatter plot with current position (matplotlib way)
            self.radar_xyz_scatter._offsets3d = ([x], [y], [z])
            
            # Update color to show valid detection
            colors = pg.colormap.get("tab10", "matplotlib").getColors()
            raw_color = colors[2]
            if len(raw_color) >= 3:
                rgb = [float(raw_color[0]), float(raw_color[1]), float(raw_color[2])]
                if any(c > 1.0 for c in rgb):
                    color_normalized = [c/255.0 for c in rgb]
                else:
                    color_normalized = rgb
            else:
                color_normalized = [0.2, 0.6, 0.2]
            
            self.radar_xyz_scatter.set_color([color_normalized])
            self.radar_xyz_scatter.set_alpha(1.0)  # Full opacity
            
            # Update trail with history
            history_count = self.shared_buffers.radar_history_count
            if history_count > 0:
                # Get historical positions and convert to Cartesian
                range_history = self.shared_buffers.radar_history_range[:history_count]
                az_history = self.shared_buffers.radar_history_azimuth[:history_count]
                el_history = self.shared_buffers.radar_history_elevation[:history_count]
                
                # Convert to Cartesian coordinates
                az_rad = np.radians(az_history)
                el_rad = np.radians(el_history)
                
                x_trail = range_history * np.cos(el_rad) * np.cos(az_rad)
                y_trail = range_history * np.cos(el_rad) * np.sin(az_rad)
                z_trail = range_history * np.sin(el_rad)
                
                # Reverse to show oldest to newest
                x_trail = x_trail[::-1]
                y_trail = y_trail[::-1]
                z_trail = z_trail[::-1]
                
                # Update trail line (matplotlib way)
                self.radar_xyz_trail.set_data_3d(x_trail, y_trail, z_trail)
            else:
                # No history yet, just show a point
                self.radar_xyz_trail.set_data_3d([x], [y], [z])
            
            # Update info label
            self.radar_xyz_info_label.setText(
                f"Range: {range_m:.2f}m | Az: {azimuth:.1f}° | El: {elevation:.1f}°"
            )
        else:
            # No valid detection but we have last known position
            if range_m != 0 or azimuth != 0 or elevation != 0:  # Check if we have any position data
                # Convert last known position to Cartesian
                azimuth_rad = np.radians(azimuth)
                elevation_rad = np.radians(elevation)
                
                x = range_m * np.cos(elevation_rad) * np.cos(azimuth_rad)
                y = range_m * np.cos(elevation_rad) * np.sin(azimuth_rad)
                z = range_m * np.sin(elevation_rad)
                
                # Update scatter plot with last position (matplotlib way)
                self.radar_xyz_scatter._offsets3d = ([x], [y], [z])
                
                # Update color to gray/transparent to show stale data
                self.radar_xyz_scatter.set_color([[0.6, 0.6, 0.6]])  # Gray
                self.radar_xyz_scatter.set_alpha(0.5)  # Semi-transparent
                
                # Keep showing the trail
                history_count = self.shared_buffers.radar_history_count
                if history_count > 0:
                    range_history = self.shared_buffers.radar_history_range[:history_count]
                    az_history = self.shared_buffers.radar_history_azimuth[:history_count]
                    el_history = self.shared_buffers.radar_history_elevation[:history_count]
                    
                    az_rad = np.radians(az_history)
                    el_rad = np.radians(el_history)
                    
                    x_trail = range_history * np.cos(el_rad) * np.cos(az_rad)
                    y_trail = range_history * np.cos(el_rad) * np.sin(az_rad)
                    z_trail = range_history * np.sin(el_rad)
                    
                    x_trail = x_trail[::-1]
                    y_trail = y_trail[::-1]
                    z_trail = z_trail[::-1]
                    
                    self.radar_xyz_trail.set_data_3d(x_trail, y_trail, z_trail)
                
                # Update info label to show stale data
                self.radar_xyz_info_label.setText(
                    f"Range: {range_m:.2f}m | Az: {azimuth:.1f}° | El: {elevation:.1f}° (Last Known)"
                )
            else:
                # No data at all - clear plots
                self.radar_xyz_scatter._offsets3d = ([], [], [])
                self.radar_xyz_trail.set_data_3d([], [], [])
                self.radar_xyz_info_label.setText("No Detection")
        
        # Redraw the canvas
        self.radar_xyz_canvas.draw_idle()
    
    def _update_radar_azrange_plot(self) -> None:
        """Update the radar azimuth/range plot."""
        if not self.shared_buffers or not self.plot_checkboxes["radar_azrange"].isChecked():
            return
            
        # Get current position (always available, even if stale)
        azimuth = self.shared_buffers.radar_azimuth[0]
        range_m = self.shared_buffers.radar_range[0]
        
        # Check if we have a valid detection
        if self.shared_buffers.radar_detection_valid[0]:
            # Valid detection - show with normal style
            colors = pg.colormap.get("tab10", "matplotlib").getColors()
            self.radar_azrange_scatter.setData(
                [azimuth], [range_m],
                pen=pg.mkPen(colors[1], width=2),
                brush=pg.mkBrush(colors[1])
            )
            
            # Update trail with history
            history_count = self.shared_buffers.radar_history_count
            if history_count > 0:
                # Get historical positions (newest first)
                az_history = self.shared_buffers.radar_history_azimuth[:history_count]
                range_history = self.shared_buffers.radar_history_range[:history_count]
                
                # Reverse to show oldest to newest
                az_trail = az_history[::-1]
                range_trail = range_history[::-1]
                
                # Update trail line
                self.radar_azrange_trail.setData(az_trail, range_trail)
            
            # Hide "No Detection" text
            self.radar_azrange_text.setVisible(False)
        else:
            # No valid detection but we have last known position
            if azimuth != 0 or range_m != 0:  # Check if we have any position data
                # Show last position with different style (hollow/gray)
                self.radar_azrange_scatter.setData(
                    [azimuth], [range_m],
                    pen=pg.mkPen((150, 150, 150), width=2),  # Gray outline
                    brush=pg.mkBrush(None)  # Hollow
                )
                # Keep showing the trail
                history_count = self.shared_buffers.radar_history_count
                if history_count > 0:
                    az_history = self.shared_buffers.radar_history_azimuth[:history_count]
                    range_history = self.shared_buffers.radar_history_range[:history_count]
                    az_trail = az_history[::-1]
                    range_trail = range_history[::-1]
                    self.radar_azrange_trail.setData(az_trail, range_trail)
                
                # Update text to show stale data
                self.radar_azrange_text.setText("Last Known Position (Stale)")
                self.radar_azrange_text.setVisible(True)
            else:
                # No data at all
                self.radar_azrange_scatter.setData([], [])
                self.radar_azrange_trail.setData([], [])
                self.radar_azrange_text.setText("No Detection")
                self.radar_azrange_text.setVisible(True)
    
    def _update_radar_debug_plot(self) -> None:
        """Update the radar debug plots showing antenna-to-reference lag spectra."""
        if not self.shared_buffers:
            return
            
        # Colors for the peak markers and centroid windows
        colors = pg.colormap.get("tab10", "matplotlib").getColors()
        
        # Update each ADC plot
        for adc_idx in range(3):
            checkbox_key = f"radar_lag_adc{adc_idx}"
            if not self.plot_checkboxes[checkbox_key].isChecked():
                continue
                
            # Get the lag spectrum data
            if adc_idx == 0:
                lag_data = self.shared_buffers.radar_lag_adc0
            elif adc_idx == 1:
                lag_data = self.shared_buffers.radar_lag_adc1
            else:
                lag_data = self.shared_buffers.radar_lag_adc2
            
            # Update the main plot
            self.radar_lag_plots[adc_idx].setData(
                x=self.radar_lag_axis, 
                y=lag_data
            )
            
            # Get the plot widget
            plot_widget = self.radar_lag_plot_widgets[adc_idx]
            
            # Remove any existing vertical lines and fill items
            # We'll store references in the plot widget for cleanup
            if hasattr(plot_widget, '_peak_line'):
                plot_widget.removeItem(plot_widget._peak_line)
            if hasattr(plot_widget, '_centroid_fill'):
                plot_widget.removeItem(plot_widget._centroid_fill)
            if hasattr(plot_widget, '_peak_text'):
                plot_widget.removeItem(plot_widget._peak_text)
            
            # Get the detected delay for this antenna
            delay_seconds = self.shared_buffers.radar_delays[adc_idx]
            delay_meters = delay_seconds * 299792458.0  # Convert to meters
            
            # Check if we have valid data or last known values
            has_delay_data = delay_seconds != 0
            is_current_detection = self.shared_buffers.radar_detection_valid[0]
            
            if has_delay_data:
                # Determine style based on whether detection is current or stale
                if is_current_detection:
                    # Current detection - use normal colors
                    line_color = colors[adc_idx]
                    line_style = QtCore.Qt.PenStyle.DashLine
                    fill_transparency = 50
                    text_suffix = ""
                    text_color = colors[adc_idx]
                else:
                    # Stale detection - use gray/muted colors
                    line_color = (150, 150, 150)  # Gray
                    line_style = QtCore.Qt.PenStyle.DotLine  # Dotted line for stale
                    fill_transparency = 25  # More transparent
                    text_suffix = " (Last Known)"
                    text_color = (150, 150, 150)
                
                # Add vertical line at detected peak
                peak_line = pg.InfiniteLine(
                    pos=delay_meters,
                    angle=90,
                    pen=pg.mkPen(line_color, width=2, style=line_style),
                    label=f'{delay_meters:.2f}m{text_suffix}',
                    labelOpts={'position': 0.95, 'color': line_color, 'movable': False}
                )
                plot_widget.addItem(peak_line)
                plot_widget._peak_line = peak_line  # Store reference for cleanup
                
                # Get centroid window boundaries
                start_idx = self.shared_buffers.radar_centroid_starts[adc_idx]
                end_idx = self.shared_buffers.radar_centroid_ends[adc_idx]
                
                # Convert indices to lag axis values
                if 0 <= start_idx < len(self.radar_lag_axis) and 0 <= end_idx < len(self.radar_lag_axis):
                    start_meters = self.radar_lag_axis[start_idx]
                    end_meters = self.radar_lag_axis[end_idx]
                    
                    # Create shaded region for centroid window
                    if is_current_detection:
                        brush_color = colors[adc_idx] + (fill_transparency,)
                    else:
                        brush_color = (150, 150, 150, fill_transparency)
                    
                    centroid_region = pg.LinearRegionItem(
                        values=(start_meters, end_meters),
                        orientation='vertical',
                        brush=pg.mkBrush(brush_color),
                        movable=False
                    )
                    plot_widget.addItem(centroid_region)
                    plot_widget._centroid_fill = centroid_region  # Store reference for cleanup
                
                # Add text label with range value
                range_m = self.shared_buffers.radar_range[0]
                peak_magnitude = lag_data[np.argmin(np.abs(self.radar_lag_axis - delay_meters))]
                
                text_item = pg.TextItem(
                    text=f'Range: {range_m:.2f}m{text_suffix}',
                    color=text_color,
                    anchor=(0, 1)
                )
                text_item.setPos(delay_meters, peak_magnitude * 1.1)
                plot_widget.addItem(text_item)
                plot_widget._peak_text = text_item  # Store reference for cleanup

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        """Handle the main window close event."""
        self.stop_workers()
        self.disconnect_from_board()
        event.accept()


def main():
    """Main function to run the application."""
    app = QtWidgets.QApplication(sys.argv)
    app.setStyleSheet("""
        QMainWindow { background-color: #2E2E2E; color: #D0D0D0; }
        QLabel, QCheckBox { color: #D0D0D0; font-size: 14px; }
        QPushButton { background-color: #555555; color: #D0D0D0; border: 1px solid #777777; padding: 5px; min-width: 80px; }
        QPushButton:hover { background-color: #666666; }
        QPushButton:pressed { background-color: #444444; }
        QLineEdit { background-color: #444444; color: #D0D0D0; border: 1px solid #777777; padding: 5px; }
        QDoubleSpinBox, QSpinBox { background-color: #444444; color: #D0D0D0; border: 1px solid #777777; padding: 3px; }
        QGroupBox { color: #D0D0D0; border: 1px solid #777777; margin-top: 10px; }
        QGroupBox::title { subcontrol-origin: margin; subcontrol-position: top center; padding: 0 3px; }
        QTabWidget::pane { border: 1px solid #777777; }
        QTabBar::tab { background: #555555; color: #D0D0D0; padding: 8px; }
        QTabBar::tab:selected { background: #666666; }
    """)
    win = PeriscopeCorrelator()
    sys.exit(app.exec_())


if __name__ == '__main__':
    main()
