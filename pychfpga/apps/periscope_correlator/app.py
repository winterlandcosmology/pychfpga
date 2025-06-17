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
        
        # Skip frames if falling behind
        self.skip_counter = 0
        self.skip_rate = 1  # Process every Nth frame
        
    def set_compute_enabled(self, enabled: bool):
        """Enable or disable lag computation."""
        if enabled:
            self.compute_enabled.set()
        else:
            self.compute_enabled.clear()
            
    def set_skip_rate(self, rate: int):
        """Set frame skip rate (1 = process every frame, 2 = every other, etc.)."""
        self.skip_rate = max(1, rate)
    
    def run(self):
        """Main thread loop for computing lag spectra."""
        while self.running:
            # Wait for new data signal
            with self.shared_buffers.new_data_condition:
                self.shared_buffers.new_data_condition.wait(timeout=0.1)
                
            # Only compute if enabled and it's time (based on skip rate)
            if self.compute_enabled.is_set():
                self.skip_counter += 1
                if self.skip_counter >= self.skip_rate:
                    self.skip_counter = 0
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
            
    def stop(self):
        """Stop the thread."""
        self.running = False


class FineLagComputer(threading.Thread):
    """
    A thread that captures raw ADC data and computes fine-resolution lag spectra.
    Uses direct pocket_correlator calls to get raw ADC frames.
    """
    def __init__(self, pocket_correlator: POCKET_CORRELATOR, shared_buffers: 'SharedDataBuffers'):
        super().__init__(daemon=True)
        self.pc = pocket_correlator
        self.shared_buffers = shared_buffers
        self.running = True
        self.enabled = threading.Event()
        
        # Performance tracking
        self.frames_processed = 0
        self.last_fps_time = time.time()
        self.fps = 0
        
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
        n_frames_to_average = 20
        d_adc = self.pc.read_adc_frames(n_frames=n_frames_to_average, split=True, verbose=0)
        
        while self.running:
            if not self.enabled.is_set():
                time.sleep(0.1)
                continue
                
            try:
                # For subsequent frames, use read_raw_frames directly
                # In mode 0, we get 2 frames per channel per capture
                # Since we want 20 frames total, we need 10 captures
                ncap = n_frames_to_average // 2  # 20 frames / 2 frames per capture = 10 captures
                
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
                
                # Shift to 14 bits (same as in read_adc_frames)
                d_adc = d_adc >> 2
                # d_adc shape: (8, 10, 16384)
                
                # # Debug: Check if we got data
                # if self.frames_processed == 0:
                #     print(f"Fine lag: Got ADC data shape: {d_adc.shape}")
                #     print(f"Fine lag: ADC0 range: [{np.min(d_adc[0]):.1f}, {np.max(d_adc[0]):.1f}]")
                #     print(f"Fine lag: ADC1 range: [{np.min(d_adc[1]):.1f}, {np.max(d_adc[1]):.1f}]")
                
                # Process each frame separately and average for better SNR
                n_samples = 16384  # Samples per frame
                n_fft = n_samples  # Use same size - no zero padding
                n_lags = n_samples // 2  # Only positive lags (0 to 8191)
                
                # Initialize accumulators for averaging
                lag_p00_sum = np.zeros(n_lags)
                lag_p11_sum = np.zeros(n_lags)
                lag_p01_sum = np.zeros(n_lags, dtype=np.complex128)  # Complex for cross-correlation
                
                # Process each of the frames for averaging
                for frame_idx in range(n_frames_to_average):
                    # Get data for this frame
                    adc0_frame = d_adc[0, frame_idx, :].astype(np.float64)  # 16384 samples
                    adc1_frame = d_adc[1, frame_idx, :].astype(np.float64)  # 16384 samples
                    
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
                    
                    # Compute cross-correlation
                    cross_spectrum = adc0_fft * np.conj(adc1_fft)
                    lag_p01_complex = np.fft.ifft(cross_spectrum)[:n_lags]
                    
                    # Add to accumulator
                    lag_p00_sum += lag_p00_frame
                    lag_p11_sum += lag_p11_frame
                    lag_p01_sum += lag_p01_complex  # Keep complex for averaging
                
                # Average the results
                lag_p00 = lag_p00_sum / n_frames_to_average
                lag_p11 = lag_p11_sum / n_frames_to_average
                lag_p01_complex_avg = lag_p01_sum / n_frames_to_average
                
                # Take magnitude of cross-correlation
                lag_p01 = np.abs(lag_p01_complex_avg)
                
                # Normalize by the zero-lag value for autocorrelations
                lag_p00 = lag_p00 / lag_p00[0]
                lag_p11 = lag_p11 / lag_p11[0]
                
                # For cross-correlation, normalize by sqrt of auto-correlation zero-lags
                # This gives correlation coefficient
                norm_factor = np.sqrt(lag_p00_sum[0] * lag_p11_sum[0]) / n_frames_to_average
                if norm_factor > 0:
                    lag_p01 = lag_p01 / norm_factor
                
                # Update shared buffers with lag data
                with self.shared_buffers.fine_lag_lock:
                    self.shared_buffers.fine_lag_p00[:] = lag_p00
                    self.shared_buffers.fine_lag_p11[:] = lag_p11
                    self.shared_buffers.fine_lag_p01[:] = lag_p01  # Cross-correlation magnitude
                    
                    # Store raw time domain data (concatenate first 2 frames for display)
                    # This is just for visualization - the lag computation uses all frames
                    adc0_display = np.concatenate([d_adc[0, 0, :], d_adc[0, 1, :]])
                    adc1_display = np.concatenate([d_adc[1, 0, :], d_adc[1, 1, :]])
                    self.shared_buffers.fine_time_adc0[:] = adc0_display
                    self.shared_buffers.fine_time_adc1[:] = adc1_display
                    
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
                
                # For subsequent frames, use read_raw_frames directly
                # Use minimal flush to ensure we're synchronized
                t, d_adc, c = self.pc.u_receiver.read_raw_frames(
                    format='16',
                    ncap=1,  # In mode 3, we get 16 frames per capture
                    split=True,
                    verbose=0,
                    flush=False,  # Minimal flush to ensure timestamp sync
                    flush_timeout=0.0001  # Very short flush
                )
                
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
                
    def stop(self):
        """Stop the thread."""
        self.running = False


class SharedDataBuffers:
    """Manages shared memory buffers for zero-copy data transfer."""
    def __init__(self, waterfall_len: int):
        self.waterfall_len = waterfall_len
        
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
        n_time_samples = 32768  # Raw time domain samples
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
        
    def cleanup(self):
        """Clean up shared memory."""
        for shm in [self.shm_p00, self.shm_p11, self.shm_p01_real, self.shm_p01_imag,
                    self.shm_wf_p00, self.shm_wf_p11, self.shm_wf_p01_real, self.shm_wf_p01_imag,
                    self.shm_lag_p00, self.shm_lag_p11, self.shm_lag_p01_mag, self.shm_lag_p01_phase,
                    self.shm_lag_wf_p00, self.shm_lag_wf_p11, self.shm_lag_wf_p01_mag, self.shm_lag_wf_p01_phase,
                    self.shm_fine_lag_p00, self.shm_fine_lag_p11, self.shm_fine_lag_p01,
                    self.shm_fine_lag_wf_p00, self.shm_fine_lag_wf_p11, self.shm_fine_lag_wf_p01,
                    self.shm_fine_time_adc0, self.shm_fine_time_adc1,
                    self.shm_super_fft_psd, self.shm_super_fft_wf_psd, self.shm_super_fft_time]:
            shm.close()
            shm.unlink()

class UDPReceiver(threading.Thread):
    """
    A thread that continuously receives UDP packets and processes them directly.
    Uses shared memory buffers for zero-copy data transfer to the GUI.
    """
    def __init__(self, sock: socket.socket, shared_buffers: SharedDataBuffers, 
                 packet_dtype: np.dtype, config: CorrelatorConfig):
        super().__init__(daemon=True)
        self.socket = sock
        self.shared_buffers = shared_buffers
        self.packet_dtype = packet_dtype
        self.config = config
        self.running = True
        
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
        packet_buffer = bytearray(4096)
        
        while self.running:
            try:
                nbytes, _ = self.socket.recvfrom_into(packet_buffer)
                if nbytes > 0:
                    self._process_packet(packet_buffer[:nbytes])
            except socket.timeout:
                continue
            except Exception as e:
                if self.running:
                    print(f"UDPReceiver Error: {e}")
                break

    def _process_packet(self, packet_data: bytes):
        """Process a single raw UDP packet."""
        packet_array = np.frombuffer(packet_data, dtype=self.packet_dtype, count=1)
        header = packet_array['header'][0]
        if header['cookie'] != 0xcf: 
            return
        
        bin_idx = header['stream_id']
        if bin_idx >= NBINS_TOTAL: 
            return
        
        data = packet_array['data'][0]
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
        self.shared_buffers: Optional[SharedDataBuffers] = None
        
        self.config = CorrelatorConfig()
        self.computed_gains: Optional[np.ndarray] = None
        
        ucorr_receiver = UCorrFrameReceiver(None, 8, Nbins=NBINS_TOTAL, NCORR=NPROD)
        self.packet_dtype = ucorr_receiver.packet_dtype

        self.freq_axis = (FS_HZ / M_FACTOR) * np.arange(NBINS_TOTAL)
        
        # Lag axis in meters (distance = c * time / 2 for radar applications)
        lag_bins = np.arange(NBINS_TOTAL) - NBINS_TOTAL // 2
        # Time between lag bins = M_FACTOR / FS_HZ seconds
        lag_time_s = lag_bins * (M_FACTOR / FS_HZ)  # Time in seconds
        c_m_per_s = 299792458.0  # Speed of light in meters per second
        self.lag_axis = lag_time_s * c_m_per_s / 2  # Convert to distance in meters (round trip)
        
        # Fine lag axis in meters (for raw ADC data)
        # Only positive lags (half of FFT output)
        fine_lag_samples = np.arange(8192)  # 0 to 8191 samples
        fine_lag_time_s = fine_lag_samples / FS_HZ  # Time in seconds
        self.fine_lag_axis = fine_lag_time_s * c_m_per_s / 2  # Distance in meters (round trip)
        # Range: 0 to ~383 meters with 4.68 cm resolution
        
        # Time axis for raw ADC samples (microseconds)
        time_samples = np.arange(32768)  # 32768 samples
        self.time_axis_us = time_samples / FS_HZ * 1e6  # Convert to microseconds
        
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
        
        self.lag_skip_rate_spinner = QtWidgets.QSpinBox()
        self.lag_skip_rate_spinner.setRange(1, 10)
        self.lag_skip_rate_spinner.setValue(1)
        self.lag_skip_rate_spinner.setPrefix("Lag Update: 1/")
        self.lag_skip_rate_spinner.setSuffix(" frames")
        self.lag_skip_rate_spinner.setToolTip("Process every Nth frame for lag computation")
        self.lag_skip_rate_spinner.valueChanged.connect(self._update_lag_skip_rate)
        
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
        toolbar_row2.addWidget(self.enable_lag_checkbox)
        toolbar_row2.addWidget(self.lag_skip_rate_spinner)
        toolbar_row2.addSpacing(20)
        toolbar_row2.addWidget(self.fine_lag_mode_button)
        toolbar_row2.addWidget(self.super_fft_mode_button)
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

        self.tabs.addTab(self.spectra_tab, "Spectra")
        self.tabs.addTab(self.waterfall_tab, "Waterfall")
        self.tabs.addTab(self.lag_spectra_tab, "Lag Spectra")
        self.tabs.addTab(self.lag_waterfall_tab, "Lag Waterfall")
        self.tabs.addTab(self.fine_lag_spectra_tab, "Fine Lag Spectra")
        self.tabs.addTab(self.fine_lag_waterfall_tab, "Fine Lag Waterfall")
        self.tabs.addTab(self.super_fft_spectra_tab, "Super FFT Spectra")
        self.tabs.addTab(self.super_fft_waterfall_tab, "Super FFT Waterfall")

        self.plot_checkboxes: Dict[str, QtWidgets.QCheckBox] = {}
        self._setup_spectra_plots()
        self._setup_waterfall_plots()
        self._setup_lag_spectra_plots()
        self._setup_lag_waterfall_plots()
        self._setup_fine_lag_spectra_plots()
        self._setup_fine_lag_waterfall_plots()
        self._setup_super_fft_spectra_plots()
        self._setup_super_fft_waterfall_plots()

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
        p00_cb.stateChanged.connect(lambda state: p00_widget.setVisible(state == QtCore.Qt.Checked))

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
        p01_abs_cb.stateChanged.connect(lambda state: p01_abs_widget.setVisible(state == QtCore.Qt.Checked))

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
        p01_real_cb.stateChanged.connect(lambda state: p01_real_widget.setVisible(state == QtCore.Qt.Checked))

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
        p11_cb.stateChanged.connect(lambda state: p11_widget.setVisible(state == QtCore.Qt.Checked))

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
            cb.stateChanged.connect(lambda state, w=container: w.setVisible(state == QtCore.Qt.Checked))

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
        lag_p00_cb.stateChanged.connect(lambda state: lag_p00_widget.setVisible(state == QtCore.Qt.Checked))

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
        lag_p11_cb.stateChanged.connect(lambda state: lag_p11_widget.setVisible(state == QtCore.Qt.Checked))

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
        lag_p01_mag_cb.stateChanged.connect(lambda state: lag_p01_mag_widget.setVisible(state == QtCore.Qt.Checked))

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
        fine_lag_p00_cb.stateChanged.connect(lambda state: fine_lag_p00_widget.setVisible(state == QtCore.Qt.Checked))

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
        fine_lag_p11_cb.stateChanged.connect(lambda state: fine_lag_p11_widget.setVisible(state == QtCore.Qt.Checked))
        
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
        fine_lag_p01_cb.stateChanged.connect(lambda state: fine_lag_p01_widget.setVisible(state == QtCore.Qt.Checked))
        
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
        time_adc0_cb.stateChanged.connect(lambda state: time_adc0_widget.setVisible(state == QtCore.Qt.Checked))
        
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
        time_adc1_cb.stateChanged.connect(lambda state: time_adc1_widget.setVisible(state == QtCore.Qt.Checked))

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
        super_fft_cb.stateChanged.connect(lambda state: super_fft_widget.setVisible(state == QtCore.Qt.Checked))
        
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
        time_domain_cb.stateChanged.connect(lambda state: time_domain_widget.setVisible(state == QtCore.Qt.Checked))
        
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
        cb.stateChanged.connect(lambda state: container.setVisible(state == QtCore.Qt.Checked))

    def _setup_status_bar(self) -> None:
        """Initialize the status bar at the bottom of the window."""
        self.statusBar = QtWidgets.QStatusBar()
        self.setStatusBar(self.statusBar)
        self.status_label = QtWidgets.QLabel("Ready. Please connect to a board.")
        self.metrics_label = QtWidgets.QLabel("")
        self.statusBar.addWidget(self.status_label, 1)
        self.statusBar.addWidget(self.metrics_label)

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
            self.receiver_thread = UDPReceiver(sock, self.shared_buffers, self.packet_dtype, self.config)
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
        gains = self.computed_gains.copy()  # Shape: (NPOLS, NBINS)
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
        is_checked = (state == QtCore.Qt.Checked)
        
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
            enabled = (state == QtCore.Qt.Checked)
            self.lag_computer_thread.set_compute_enabled(enabled)
            status = "enabled" if enabled else "disabled"
            print(f"Lag computation {status}")
            
    def _update_lag_skip_rate(self, value: int) -> None:
        """Update the frame skip rate for lag computation."""
        if self.lag_computer_thread:
            self.lag_computer_thread.set_skip_rate(value)
            print(f"Lag skip rate set to: process every {value} frame(s)")
    
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
                self.fine_lag_computer_thread = FineLagComputer(self.pocket_correlator, self.shared_buffers)
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
                    
                    self.receiver_thread = UDPReceiver(sock, self.shared_buffers, self.packet_dtype, self.config)
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
                    
                    self.receiver_thread = UDPReceiver(sock, self.shared_buffers, self.packet_dtype, self.config)
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

    def _update_plots(self) -> None:
        """Update all plots with new data from the shared buffers."""
        if not self.shared_buffers:
            return
            
        # Check which tab is active
        current_tab_index = self.tabs.currentIndex()
        
        # Check if in super FFT mode
        if self.super_fft_mode_button.isChecked() and current_tab_index >= 6:
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

    def _update_spectra_plots(self) -> None:
        """Update the line plots in the Spectra tab."""
        use_log_scale = self.log_scale_checkbox.isChecked()
        
        # Update plot log modes based on checkbox
        self.plot_widgets[(0,0)].setLogMode(y=use_log_scale)
        self.plot_widgets[(1,1)].setLogMode(y=use_log_scale)
        # For cross-correlation abs, use log scale if enabled
        self.plot_widgets[(0,1,'abs')].setLogMode(y=use_log_scale)
        # Real plot can use log scale if enabled (for symmetry log)
        self.plot_widgets[(0,1,'real')].setLogMode(y=False)
        
        # Only update plots that are checked
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
            
            # Set the scale to show frequency on x-axis and time on y-axis (only need to do once)
            # Check if we need to set the scale by checking if we have a flag
            if not hasattr(img, '_scale_set'):
                # Set the image rectangle to map bins to frequency and time
                # Y-axis: 0 at top (newest), waterfall_len at bottom (oldest)
                img.setRect(QtCore.QRectF(freq_min, 0, freq_max - freq_min, self.shared_buffers.waterfall_len))
                img._scale_set = True  # Mark that scale has been set
                
                # Update plot labels
                plot = self.waterfall_plots[key]
                plot.setLabel('bottom', 'Frequency', units='Hz')
                plot.setLabel('left', 'Time (frames ago)')
        
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
            # Plot raw ADC0 time domain data
            self.fine_time_plots[0].setData(x=self.time_axis_us, y=self.shared_buffers.fine_time_adc0)
            
        if self.plot_checkboxes["fine_time_adc1"].isChecked():
            # Plot raw ADC1 time domain data
            self.fine_time_plots[1].setData(x=self.time_axis_us, y=self.shared_buffers.fine_time_adc1)

    def _update_fine_lag_waterfall_plots(self) -> None:
        """Update the fine lag waterfall plots."""
        # Get the current write position to properly display circular buffer
        pos = self.shared_buffers.fine_lag_wf_write_pos
        
        # Create properly ordered views of the circular buffers
        def get_ordered_waterfall(data, pos):
            """Get waterfall data in correct time order."""
            return np.vstack([data[pos:], data[:pos]])
        
        # Calculate fine lag axis range (in meters)
        fine_lag_min = self.fine_lag_axis[0]
        fine_lag_max = self.fine_lag_axis[-1]
        
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
                # Calculate robust levels using percentiles
                valid_data = wf_data[np.isfinite(wf_data)]
                
                # For normalized autocorrelation data (0 to 1 range)
                if len(valid_data) > 0:
                    vmin = np.percentile(valid_data, 5)
                    vmax = np.percentile(valid_data, 95)
                    # Ensure we capture the full range
                    vmin = min(vmin, 0)
                    vmax = max(vmax, 1)
                else:
                    vmin, vmax = 0, 1
                
                self.fine_lag_waterfall_levels[key] = (vmin, vmax)
                self.fine_lag_waterfall_level_update_counter[key] = 0
            
            # Set image with fixed levels
            img = self.fine_lag_waterfall_images[key]
            use_log_scale = self.log_scale_checkbox.isChecked()
            
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
        
        # Set the scale to show frequency on x-axis (only need to do once)
        if not hasattr(img, '_scale_set'):
            # Set the image rectangle to map bins to frequency
            # Note: we need to handle the fact that we have many more bins (131k vs 8k)
            # The waterfall should show the full frequency range
            img.setRect(QtCore.QRectF(freq_min, 0, freq_max - freq_min, self.shared_buffers.waterfall_len))
            img._scale_set = True
            
            # Update plot labels
            plot = self.super_fft_waterfall_plots[key]
            plot.setLabel('bottom', 'Frequency', units='Hz')
            plot.setLabel('left', 'Time (frames ago)')

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
