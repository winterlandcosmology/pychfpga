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

# Import data archiver
try:
    import h5py
    try:
        # Try relative import first (when run as package)
        from .data_archiver import DataArchiver
        DATA_ARCHIVER_AVAILABLE = True
        print(f"DataArchiver available with h5py version {h5py.__version__}")
    except ImportError:
        try:
            # Try absolute import (when run directly)
            from data_archiver import DataArchiver
            DATA_ARCHIVER_AVAILABLE = True
            print(f"DataArchiver available with h5py version {h5py.__version__}")
        except ImportError as e:
            print(f"Warning: DataArchiver import failed: {e}")
            DATA_ARCHIVER_AVAILABLE = False
except ImportError:
    print("Warning: h5py not installed. Please install h5py to enable archiving features.")
    print("Install with: pip install h5py")
    DATA_ARCHIVER_AVAILABLE = False

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
    # Frequency range for lag computation (in Hz)
    lag_freq_start_hz: float = 0.0  # Default to full band
    lag_freq_end_hz: float = FS_HZ / 2  # Default to full band (0 to 1600 MHz)

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
        
        # Pre-compute Hann window to match fine lag mode processing
        self.window = np.hanning(NBINS_TOTAL)
        
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
                self.vis_p00_buffer[:] = np.abs(self.shared_buffers.p00)  # Use magnitude for autocorrelations
                self.vis_p11_buffer[:] = np.abs(self.shared_buffers.p11)  # Use magnitude for autocorrelations
                self.vis_p01_complex_buffer[:] = self.shared_buffers.p01  # Complex directly
            
            # Remove DC offset to match fine lag mode processing
            self.vis_p00_buffer -= np.mean(self.vis_p00_buffer)
            self.vis_p11_buffer -= np.mean(self.vis_p11_buffer)
            self.vis_p01_complex_buffer -= np.mean(self.vis_p01_complex_buffer)
            
            # Apply Hann window to reduce spectral leakage (matches fine lag mode)
            self.vis_p00_buffer = self.vis_p00_buffer * self.window
            self.vis_p11_buffer = self.vis_p11_buffer * self.window
            self.vis_p01_complex_buffer = self.vis_p01_complex_buffer * self.window
            
            # Apply frequency range selection if different from full band
            # Get frequency range from config (will be set by GUI)
            # This is passed through shared_buffers since LagSpectraComputer doesn't have direct access to config
            if hasattr(self.shared_buffers, 'gui_instance') and self.shared_buffers.gui_instance:
                gui = self.shared_buffers.gui_instance
                freq_start = gui.config.lag_freq_start_hz
                freq_end = gui.config.lag_freq_end_hz
                nyquist_zone = self.shared_buffers.current_nyquist_zone
                
                # Check if we're using a frequency subset
                is_full_band = (freq_start == 0 and freq_end == FS_HZ / 2)
                
                if not is_full_band:
                    # Convert frequency range to correlator bin indices
                    start_bin = gui._freq_to_correlator_bin_index(freq_start, nyquist_zone)
                    end_bin = gui._freq_to_correlator_bin_index(freq_end, nyquist_zone)
                    
                    # Ensure start < end (accounting for Nyquist zone reversal)
                    if start_bin > end_bin:
                        start_bin, end_bin = end_bin, start_bin
                    
                    # Create masked visibility buffers (zero out excluded bins)
                    # For correlator data, we work with all 8192 bins
                    vis_p00_masked = np.zeros_like(self.vis_p00_buffer)
                    vis_p11_masked = np.zeros_like(self.vis_p11_buffer)
                    vis_p01_masked = np.zeros_like(self.vis_p01_complex_buffer)
                    
                    # Copy only the selected frequency range
                    vis_p00_masked[start_bin:end_bin] = self.vis_p00_buffer[start_bin:end_bin]
                    vis_p11_masked[start_bin:end_bin] = self.vis_p11_buffer[start_bin:end_bin]
                    vis_p01_masked[start_bin:end_bin] = self.vis_p01_complex_buffer[start_bin:end_bin]
                    
                    # Use masked buffers for computation
                    self.vis_p00_buffer = vis_p00_masked
                    self.vis_p11_buffer = vis_p11_masked
                    self.vis_p01_complex_buffer = vis_p01_masked
                
            # # Apply windowing to reduce edge artifacts and spectral leakage
            # # This is critical for proper lag spectrum computation from channelized data
            # window = np.hanning(len(self.vis_p00_buffer))
            
            # # Apply window to all visibility data
            # self.vis_p00_buffer = self.vis_p00_buffer * window
            # self.vis_p11_buffer = self.vis_p11_buffer * window
            # self.vis_p01_complex_buffer = self.vis_p01_complex_buffer * window
            
            # # Optional: Apply PFB correction
            # # The PFB channelizer has a sinc-like response that should be corrected
            # # This is a simplified correction - you may need to calibrate this
            # pfb_correction = np.ones(NBINS_TOTAL)
            # # Create a simple correction for PFB rolloff at band edges
            # # Channels near the edges have reduced response
            # edge_width = NBINS_TOTAL // 16  # Adjust based on your PFB design
            # for i in range(edge_width):
            #     # Gradual correction at edges
            #     correction = 1.0 / (0.5 + 0.5 * (i / edge_width))
            #     pfb_correction[i] = correction
            #     pfb_correction[-(i+1)] = correction
            
            # # Apply PFB correction
            # self.vis_p00_buffer = self.vis_p00_buffer * pfb_correction
            # self.vis_p11_buffer = self.vis_p11_buffer * pfb_correction
            # self.vis_p01_complex_buffer = self.vis_p01_complex_buffer * pfb_correction
            
            # Add diagnostic logging every 100 frames for phase coherence
            if self.frames_processed % 100 == 0:
                # Check phase coherence
                phase = np.angle(self.vis_p01_complex_buffer)
                phase_gradient = np.gradient(phase)
                phase_coherence = np.std(phase_gradient)
                
                # Check if we have meaningful complex data
                imag_power = np.mean(np.abs(self.shared_buffers.p01.imag)**2)
                real_power = np.mean(np.abs(self.shared_buffers.p01.real)**2)
                power_ratio = real_power / imag_power if imag_power > 0 else np.inf
                
                # Check phase statistics
                phase_range = np.max(phase) - np.min(phase)
                phase_std = np.std(phase)
                
                print(f"Lag computer: Phase gradient std: {phase_coherence:.3f} (should be ~constant for coherent signal)")
                print(f"Lag computer: Real/Imag power ratio: {power_ratio:.2f} (should be ~1 for complex data)")
                print(f"Lag computer: Phase range: {phase_range:.2f} rad, std: {phase_std:.3f}")
            
            # Compute inverse FFTs (no lock needed)
            # For autocorrelations, take real part since input is real-valued in frequency domain
            lag_p00_complex = np.fft.ifft(self.vis_p00_buffer)
            lag_p11_complex = np.fft.ifft(self.vis_p11_buffer)
            
            # For autocorrelations, compute delay spectra (power of lag)
            # This is |IFFT|^2 which gives us the power at each delay
            self.lag_p00_buffer = np.abs(lag_p00_complex)**2
            self.lag_p11_buffer = np.abs(lag_p11_complex)**2
            
            # Store zero-lag values before normalization for cross-correlation
            lag_p00_zero = self.lag_p00_buffer[0]  # Zero lag is at index 0 before fftshift
            lag_p11_zero = self.lag_p11_buffer[0]
            
            # Normalize autocorrelations by their zero-lag values (like fine lag mode)
            # This brings values into 0-1 range instead of e+21!
            self.lag_p00_buffer = self.lag_p00_buffer / lag_p00_zero if lag_p00_zero > 0 else self.lag_p00_buffer
            self.lag_p11_buffer = self.lag_p11_buffer / lag_p11_zero if lag_p11_zero > 0 else self.lag_p11_buffer
            
            # For cross-correlation, compute IFFT of complex visibility
            lag_p01_complex = np.fft.ifft(self.vis_p01_complex_buffer)
            lag_p01_magnitude = np.abs(lag_p01_complex)**2  # Power spectrum
            
            # Normalize cross-correlation by sqrt of zero-lag autocorrelations (like fine lag mode)
            norm_factor = np.sqrt(lag_p00_zero * lag_p11_zero)
            if norm_factor > 0:
                lag_p01_magnitude = lag_p01_magnitude / norm_factor
            
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
                
                # Update history buffers for running difference mode
                # Store in circular buffer at current position
                hist_pos = self.shared_buffers.lag_history_pos
                self.shared_buffers.lag_history_p00[hist_pos] = self.lag_p00_buffer
                self.shared_buffers.lag_history_p11[hist_pos] = self.lag_p11_buffer
                self.shared_buffers.lag_history_p01[hist_pos] = lag_p01_magnitude
                
                # Update history position and count
                self.shared_buffers.lag_history_pos = (hist_pos + 1) % self.shared_buffers.max_diff_window
                self.shared_buffers.lag_history_count = min(
                    self.shared_buffers.lag_history_count + 1,
                    self.shared_buffers.max_diff_window
                )
                
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
                
                # Calculate and store running difference if enabled
                if self.shared_buffers.running_diff_enabled:
                    window_size = self.shared_buffers.running_diff_window_size
                    history_count = self.shared_buffers.lag_history_count
                    
                    if history_count >= window_size:
                        # Calculate moving average from the last window_size frames
                        history_pos = self.shared_buffers.lag_history_pos
                        
                        # Get indices for the last window_size frames
                        indices = []
                        for i in range(window_size):
                            idx = (history_pos - 1 - i) % self.shared_buffers.max_diff_window
                            if idx < history_count:  # Only use valid history entries
                                indices.append(idx)
                        
                        if len(indices) > 0:
                            # Calculate moving averages
                            moving_avg_p00 = np.mean(self.shared_buffers.lag_history_p00[indices], axis=0)
                            moving_avg_p11 = np.mean(self.shared_buffers.lag_history_p11[indices], axis=0)
                            moving_avg_p01 = np.mean(self.shared_buffers.lag_history_p01[indices], axis=0)
                            
                            # Calculate differences and update diff waterfall buffers
                            # Shift existing diff data down and insert new at top
                            self.shared_buffers.lag_diff_wf_p00[1:] = self.shared_buffers.lag_diff_wf_p00[:-1]
                            self.shared_buffers.lag_diff_wf_p11[1:] = self.shared_buffers.lag_diff_wf_p11[:-1]
                            self.shared_buffers.lag_diff_wf_p01[1:] = self.shared_buffers.lag_diff_wf_p01[:-1]
                            
                            # Write new difference data at position 0 (top)
                            self.shared_buffers.lag_diff_wf_p00[0] = self.lag_p00_buffer - moving_avg_p00
                            self.shared_buffers.lag_diff_wf_p11[0] = self.lag_p11_buffer - moving_avg_p11
                            self.shared_buffers.lag_diff_wf_p01[0] = lag_p01_magnitude - moving_avg_p01
                
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


# --- Fine Lag Plot Options ---
# Build combo box options for the 8-channel fine lag mode
FINE_LAG_PLOT_OPTIONS = []
for ch in range(8):
    FINE_LAG_PLOT_OPTIONS.append(f"TOD: ADC {ch}")
for ch in range(8):
    FINE_LAG_PLOT_OPTIONS.append(f"FFT: ADC {ch}")
for i in range(8):
    for j in range(i, 8):
        label = f"Lag: p{i}{j} (auto)" if i == j else f"Lag: p{i}{j}"
        FINE_LAG_PLOT_OPTIONS.append(label)

FINE_LAG_WATERFALL_OPTIONS = []
for ch in range(8):
    FINE_LAG_WATERFALL_OPTIONS.append(f"FFT: ADC {ch}")
for i in range(8):
    for j in range(i, 8):
        label = f"Lag: p{i}{j} (auto)" if i == j else f"Lag: p{i}{j}"
        FINE_LAG_WATERFALL_OPTIONS.append(label)

FINE_LAG_SPECTRA_DEFAULTS = [
    "TOD: ADC 0", "TOD: ADC 1", "TOD: ADC 2", "TOD: ADC 3",
    "TOD: ADC 4", "TOD: ADC 5", "TOD: ADC 6", "TOD: ADC 7",
]
FINE_LAG_WATERFALL_DEFAULTS = ["FFT: ADC 0", "FFT: ADC 1", "Lag: p00 (auto)", "Lag: p01"]


@dataclass
class FineLagConfig:
    """Configuration for 8-channel fine lag mode."""
    # Per-ADC settings (length 8)
    nyquist_zones: list = field(default_factory=lambda: [1]*8)
    freq_mask_low_mhz: list = field(default_factory=lambda: [0.0]*8)
    freq_mask_high_mhz: list = field(default_factory=lambda: [1600.0]*8)

    # Global settings
    frames_to_average: int = 1

    # Fusion (placeholder)
    fusion_enabled: bool = False
    fusion_groups: list = field(default_factory=list)

    # Derived constants
    FS_HZ: float = 3.2e9
    ZONE_BANDWIDTH_HZ: float = 1.6e9

    def freq_axis_hz(self, adc_index: int) -> np.ndarray:
        """Return frequency axis for a given ADC based on its Nyquist zone."""
        zone = self.nyquist_zones[adc_index]
        base = (zone - 1) * self.ZONE_BANDWIDTH_HZ
        freqs = np.linspace(0, self.ZONE_BANDWIDTH_HZ, 16384, endpoint=False)
        if zone % 2 == 0:
            freqs = base + self.ZONE_BANDWIDTH_HZ - freqs
        else:
            freqs = base + freqs
        return freqs


class FineLagConfigDialog(QtWidgets.QDialog):
    """Configuration dialog for 8-channel fine lag mode."""

    def __init__(self, config: FineLagConfig, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Fine Lag Configuration")
        self.setMinimumWidth(600)
        self._config = config

        main_layout = QtWidgets.QVBoxLayout(self)

        # --- ADC Channel Settings ---
        adc_group = QtWidgets.QGroupBox("ADC Channel Settings")
        adc_layout = QtWidgets.QGridLayout(adc_group)
        adc_layout.addWidget(QtWidgets.QLabel("ADC"), 0, 0)
        adc_layout.addWidget(QtWidgets.QLabel("Nyquist Zone"), 0, 1)
        adc_layout.addWidget(QtWidgets.QLabel("Mask Low (MHz)"), 0, 2)
        adc_layout.addWidget(QtWidgets.QLabel("Mask High (MHz)"), 0, 3)

        self.zone_combos = []
        self.mask_low_spins = []
        self.mask_high_spins = []

        zone_options = ["Zone 1 (0-1600 MHz)", "Zone 2 (1600-3200 MHz)", "Zone 3 (3200-4800 MHz)", "Zone 4 (4800-6400 MHz)"]
        zone_ranges = {1: (0.0, 1600.0), 2: (1600.0, 3200.0), 3: (3200.0, 4800.0), 4: (4800.0, 6400.0)}

        for ch in range(8):
            row = ch + 1
            adc_layout.addWidget(QtWidgets.QLabel(f"ADC {ch}"), row, 0)

            zone_combo = QtWidgets.QComboBox()
            zone_combo.addItems(zone_options)
            zone_combo.setCurrentIndex(config.nyquist_zones[ch] - 1)
            zone_combo.currentIndexChanged.connect(lambda idx, c=ch: self._on_zone_changed(c, idx))
            self.zone_combos.append(zone_combo)
            adc_layout.addWidget(zone_combo, row, 1)

            z = config.nyquist_zones[ch]
            lo, hi = zone_ranges[z]

            mask_low = QtWidgets.QDoubleSpinBox()
            mask_low.setRange(lo, hi)
            mask_low.setSingleStep(10.0)
            mask_low.setDecimals(1)
            mask_low.setValue(config.freq_mask_low_mhz[ch])
            self.mask_low_spins.append(mask_low)
            adc_layout.addWidget(mask_low, row, 2)

            mask_high = QtWidgets.QDoubleSpinBox()
            mask_high.setRange(lo, hi)
            mask_high.setSingleStep(10.0)
            mask_high.setDecimals(1)
            mask_high.setValue(config.freq_mask_high_mhz[ch])
            self.mask_high_spins.append(mask_high)
            adc_layout.addWidget(mask_high, row, 3)

        main_layout.addWidget(adc_group)

        # --- Processing ---
        proc_group = QtWidgets.QGroupBox("Processing")
        proc_layout = QtWidgets.QHBoxLayout(proc_group)
        proc_layout.addWidget(QtWidgets.QLabel("Frames to average:"))
        self.avg_spin = QtWidgets.QSpinBox()
        self.avg_spin.setRange(1, 100)
        self.avg_spin.setValue(config.frames_to_average)
        proc_layout.addWidget(self.avg_spin)
        proc_layout.addStretch()
        main_layout.addWidget(proc_group)

        # --- Fusion placeholder ---
        fusion_group = QtWidgets.QGroupBox("Bandwidth Fusion (future)")
        fusion_layout = QtWidgets.QHBoxLayout(fusion_group)
        fusion_cb = QtWidgets.QCheckBox("Enable fusion mode")
        fusion_cb.setChecked(False)
        fusion_cb.setEnabled(False)
        fusion_layout.addWidget(fusion_cb)
        fusion_layout.addStretch()
        main_layout.addWidget(fusion_group)

        # --- Buttons ---
        btn_layout = QtWidgets.QHBoxLayout()
        btn_layout.addStretch()
        cancel_btn = QtWidgets.QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        apply_btn = QtWidgets.QPushButton("Apply")
        apply_btn.clicked.connect(self.accept)
        btn_layout.addWidget(cancel_btn)
        btn_layout.addWidget(apply_btn)
        main_layout.addLayout(btn_layout)

    def _on_zone_changed(self, ch: int, zone_idx: int):
        """Update mask spin box ranges when zone changes."""
        zone = zone_idx + 1
        zone_ranges = {1: (0.0, 1600.0), 2: (1600.0, 3200.0), 3: (3200.0, 4800.0), 4: (4800.0, 6400.0)}
        lo, hi = zone_ranges[zone]
        self.mask_low_spins[ch].setRange(lo, hi)
        self.mask_low_spins[ch].setValue(lo)
        self.mask_high_spins[ch].setRange(lo, hi)
        self.mask_high_spins[ch].setValue(hi)

    def get_config(self) -> FineLagConfig:
        """Read widget values and return updated config."""
        return FineLagConfig(
            nyquist_zones=[c.currentIndex() + 1 for c in self.zone_combos],
            freq_mask_low_mhz=[s.value() for s in self.mask_low_spins],
            freq_mask_high_mhz=[s.value() for s in self.mask_high_spins],
            frames_to_average=self.avg_spin.value(),
            fusion_enabled=False,
            fusion_groups=[],
        )


class FineLagComputer(threading.Thread):
    """
    8-channel fine lag computer.  Uses UCAP mode 0 (all 8 ADC channels,
    2 frames per channel) and computes:
      - 8 TODs  (raw time-domain per channel)
      - 8 FFTs  (power spectra per channel, with per-channel freq masking)
      - 36 lag products  (8 autos + 28 cross-correlations)
    Results are stored in ``shared_buffers.fine_lag_result`` as a dict
    *and* written to the legacy p00/p11/p01 shared-memory arrays so the
    existing GUI keeps working during the Phase-3/4/5 transition.
    """

    N_CHANNELS = 8
    N_SAMPLES = 16384       # samples per frame
    N_LAGS = 8192           # positive-only lags (half of N_SAMPLES)

    def __init__(self, pocket_correlator: POCKET_CORRELATOR,
                 shared_buffers: 'SharedDataBuffers',
                 config: CorrelatorConfig,
                 fine_lag_config: 'FineLagConfig' = None):
        super().__init__(daemon=True)
        self.pc = pocket_correlator
        self.shared_buffers = shared_buffers
        self.config = config
        self.fine_lag_config = fine_lag_config or FineLagConfig()
        self.running = True
        self.enabled = threading.Event()

        # UCAP mode — always 0 for 8-channel capture
        self.ucap_mode = 0
        self.max_frames_per_channel = 2

        # Performance tracking
        self.frames_processed = 0
        self.last_fps_time = time.time()
        self.fps = 0.0

        # Frame validation stats
        self.total_frames_captured = 0
        self.frames_skipped = 0

        # Pre-compute Hann window (reused every frame)
        self._window = np.hanning(self.N_SAMPLES)

    # ------------------------------------------------------------------
    # public helpers
    # ------------------------------------------------------------------
    def set_enabled(self, enabled: bool):
        if enabled:
            self.enabled.set()
        else:
            self.enabled.clear()

    def set_frames_to_average(self, n: int):
        """Not directly relevant for mode-0 (always 2 frames) but kept for API compat."""
        pass  # mode 0 gives exactly 2 frames per channel per burst

    def update_fine_lag_config(self, cfg: 'FineLagConfig'):
        """Hot-swap the per-ADC config (thread-safe: the dict is read atomically)."""
        self.fine_lag_config = cfg

    # ------------------------------------------------------------------
    # main loop
    # ------------------------------------------------------------------
    def run(self):
        # Set UCAP mode 0 — 8 channels, 2 frames each
        self.pc.set_ucap_mode(mode=0)
        print("Fine lag: Using ucap_mode=0 (8 channels, 2 frames per channel)")

        # Initial capture to prime the hardware
        d_adc = self.pc.read_adc_frames(n_frames=2, split=True, verbose=0)

        debug_printed = False

        while self.running:
            if not self.enabled.is_set():
                time.sleep(0.1)
                continue

            try:
                # --- reset / capture / check ADC flags ---
                self._reset_adc_flags()

                t, d_adc, c = self.pc.u_receiver.read_raw_frames(
                    format='16', ncap=1, split=True, verbose=0,
                    flush=True, flush_timeout=0.0001,
                )

                if self._check_adc_flags():
                    continue  # ADC flags detected — skip

                d_adc = d_adc >> 2  # shift to 14-bit
                # d_adc shape: (8, n_frames_per_ch, 16384)
                self.total_frames_captured += 1

                if not debug_printed:
                    print(f"Fine lag 8ch: ADC data shape = {d_adc.shape}")
                    for ch in range(min(d_adc.shape[0], 8)):
                        print(f"  CH{ch}: range=[{np.min(d_adc[ch]):.0f}, {np.max(d_adc[ch]):.0f}]")
                    debug_printed = True

                cfg = self.fine_lag_config
                n_avg_bursts = max(1, cfg.frames_to_average)

                # --- accumulate multiple bursts if requested ---
                all_bursts = [d_adc]
                for _ in range(n_avg_bursts - 1):
                    self._reset_adc_flags()
                    _, extra, _ = self.pc.u_receiver.read_raw_frames(
                        format='16', ncap=1, split=True, verbose=0,
                        flush=True, flush_timeout=0.0001,
                    )
                    if not self._check_adc_flags():
                        all_bursts.append(extra >> 2)

                # --- build per-channel concatenated time series ---
                adc_data = {}  # ch -> (n_frames_total, 16384)
                for ch in range(self.N_CHANNELS):
                    adc_data[ch] = np.concatenate(
                        [burst[ch] for burst in all_bursts], axis=0
                    ).astype(np.float64)

                # --- compute per-channel FFTs with masking ---
                fft_complex = {}   # ch -> complex FFT array
                fft_power = {}     # ch -> real power spectrum
                n_fft = self.N_SAMPLES

                for ch in range(self.N_CHANNELS):
                    ts = adc_data[ch].ravel()
                    ts = ts - np.mean(ts)  # DC removal
                    windowed = ts[:n_fft] * self._window if len(ts) >= n_fft else ts * self._window[:len(ts)]
                    F = np.fft.fft(windowed, n=n_fft)

                    # per-channel frequency mask
                    mask_lo_hz = cfg.freq_mask_low_mhz[ch] * 1e6
                    mask_hi_hz = cfg.freq_mask_high_mhz[ch] * 1e6
                    zone = cfg.nyquist_zones[ch]
                    # Map physical freq to baseband bin index
                    zone_base = (zone - 1) * cfg.ZONE_BANDWIDTH_HZ
                    bb_lo = mask_lo_hz - zone_base
                    bb_hi = mask_hi_hz - zone_base
                    if zone % 2 == 0:
                        # even zone is mirrored
                        bb_lo, bb_hi = cfg.ZONE_BANDWIDTH_HZ - bb_hi, cfg.ZONE_BANDWIDTH_HZ - bb_lo
                    bin_lo = max(0, int(bb_lo / FS_HZ * n_fft))
                    bin_hi = min(n_fft // 2, int(bb_hi / FS_HZ * n_fft))

                    # Check if mask is the full band
                    is_full_band = (bin_lo <= 0 and bin_hi >= n_fft // 2)
                    if not is_full_band:
                        mask = np.zeros(n_fft, dtype=np.float64)
                        mask[bin_lo:bin_hi] = 1.0
                        # mirror for negative freqs
                        mask[n_fft - bin_hi:n_fft - bin_lo] = 1.0
                        F = F * mask

                    fft_complex[ch] = F
                    fft_power[ch] = np.abs(F[:n_fft // 2 + 1]) ** 2

                # --- compute all 36 lag products ---
                lags = {}
                for i in range(self.N_CHANNELS):
                    for j in range(i, self.N_CHANNELS):
                        cross_spec = fft_complex[i] * np.conj(fft_complex[j])
                        lag_full = np.fft.ifft(cross_spec)
                        if i == j:
                            # auto: positive lags only, real-valued, normalized
                            lag_pos = np.abs(lag_full[:self.N_LAGS].real)
                            zero_lag = lag_pos[0] if lag_pos[0] > 0 else 1.0
                            lags[(i, j)] = lag_pos / zero_lag
                        else:
                            # cross: center around zero lag
                            shifted = np.fft.fftshift(lag_full)
                            start = (self.N_SAMPLES - self.N_LAGS) // 2
                            lag_cross = np.abs(shifted[start:start + self.N_LAGS])
                            # normalize by geometric mean of zero-lag autos
                            zi = np.abs(np.fft.ifft(fft_complex[i] * np.conj(fft_complex[i]))[0].real)
                            zj = np.abs(np.fft.ifft(fft_complex[j] * np.conj(fft_complex[j]))[0].real)
                            norm = np.sqrt(zi * zj) if (zi > 0 and zj > 0) else 1.0
                            lags[(i, j)] = lag_cross / norm

                # --- package result dict ---
                result = {
                    'tods': adc_data,
                    'ffts': fft_power,
                    'fft_complex': fft_complex,
                    'lags': lags,
                    'timestamp': time.time(),
                    'config': cfg,
                    'n_bursts_averaged': len(all_bursts),
                }

                # --- update shared buffers ---
                with self.shared_buffers.fine_lag_lock:
                    # New-style: store full result dict
                    self.shared_buffers.fine_lag_result = result

                    # Legacy compat: write p00, p11, p01 to old shared-memory arrays
                    if (0, 0) in lags and len(self.shared_buffers.fine_lag_p00) == self.N_LAGS:
                        self.shared_buffers.fine_lag_p00[:] = lags[(0, 0)]
                    if (1, 1) in lags and len(self.shared_buffers.fine_lag_p11) == self.N_LAGS:
                        self.shared_buffers.fine_lag_p11[:] = lags[(1, 1)]
                    if (0, 1) in lags and len(self.shared_buffers.fine_lag_p01) == self.N_LAGS:
                        self.shared_buffers.fine_lag_p01[:] = lags[(0, 1)]

                    # Legacy: time-domain display (ch0, ch1)
                    n_disp = min(adc_data[0].size, len(self.shared_buffers.fine_time_adc0))
                    self.shared_buffers.fine_time_adc0[:n_disp] = adc_data[0].ravel()[:n_disp]
                    self.shared_buffers.fine_time_adc1[:n_disp] = adc_data[1].ravel()[:n_disp]
                    self.shared_buffers.fine_time_n_samples = n_disp

                    # Legacy: frequency domain display (ch0, ch1)
                    n_freq = min(len(fft_complex[0][:8193]), len(self.shared_buffers.fine_freq_adc0))
                    self.shared_buffers.fine_freq_adc0[:n_freq] = fft_complex[0][:n_freq]
                    self.shared_buffers.fine_freq_adc1[:n_freq] = fft_complex[1][:n_freq]
                    cross01 = fft_complex[0][:n_freq] * np.conj(fft_complex[1][:n_freq])
                    self.shared_buffers.fine_freq_cross[:n_freq] = cross01

                    # Legacy: waterfall (p00, p11, p01)
                    self.shared_buffers.fine_lag_wf_p00[1:] = self.shared_buffers.fine_lag_wf_p00[:-1]
                    self.shared_buffers.fine_lag_wf_p11[1:] = self.shared_buffers.fine_lag_wf_p11[:-1]
                    self.shared_buffers.fine_lag_wf_p01[1:] = self.shared_buffers.fine_lag_wf_p01[:-1]
                    self.shared_buffers.fine_lag_wf_p00[0] = lags.get((0, 0), np.zeros(self.N_LAGS))
                    self.shared_buffers.fine_lag_wf_p11[0] = lags.get((1, 1), np.zeros(self.N_LAGS))
                    self.shared_buffers.fine_lag_wf_p01[0] = lags.get((0, 1), np.zeros(self.N_LAGS))

                    # Legacy: running-difference history
                    hp = self.shared_buffers.fine_lag_history_pos
                    self.shared_buffers.fine_lag_history_p00[hp] = lags.get((0, 0), np.zeros(self.N_LAGS))
                    self.shared_buffers.fine_lag_history_p11[hp] = lags.get((1, 1), np.zeros(self.N_LAGS))
                    self.shared_buffers.fine_lag_history_p01[hp] = lags.get((0, 1), np.zeros(self.N_LAGS))
                    self.shared_buffers.fine_lag_history_pos = (hp + 1) % self.shared_buffers.max_diff_window
                    self.shared_buffers.fine_lag_history_count = min(
                        self.shared_buffers.fine_lag_history_count + 1,
                        self.shared_buffers.max_diff_window,
                    )

                    # Legacy: running-difference waterfall
                    if self.shared_buffers.running_diff_enabled:
                        ws = self.shared_buffers.running_diff_window_size
                        hc = self.shared_buffers.fine_lag_history_count
                        if hc >= ws:
                            hp2 = self.shared_buffers.fine_lag_history_pos
                            idxs = [(hp2 - 1 - k) % self.shared_buffers.max_diff_window for k in range(ws)]
                            avg00 = np.mean(self.shared_buffers.fine_lag_history_p00[idxs], axis=0)
                            avg11 = np.mean(self.shared_buffers.fine_lag_history_p11[idxs], axis=0)
                            avg01 = np.mean(self.shared_buffers.fine_lag_history_p01[idxs], axis=0)
                            self.shared_buffers.fine_lag_diff_wf_p00[1:] = self.shared_buffers.fine_lag_diff_wf_p00[:-1]
                            self.shared_buffers.fine_lag_diff_wf_p11[1:] = self.shared_buffers.fine_lag_diff_wf_p11[:-1]
                            self.shared_buffers.fine_lag_diff_wf_p01[1:] = self.shared_buffers.fine_lag_diff_wf_p01[:-1]
                            self.shared_buffers.fine_lag_diff_wf_p00[0] = lags.get((0, 0), np.zeros(self.N_LAGS)) - avg00
                            self.shared_buffers.fine_lag_diff_wf_p11[0] = lags.get((1, 1), np.zeros(self.N_LAGS)) - avg11
                            self.shared_buffers.fine_lag_diff_wf_p01[0] = lags.get((0, 1), np.zeros(self.N_LAGS)) - avg01

                    self.shared_buffers.fine_lag_frames_written = min(
                        self.shared_buffers.fine_lag_frames_written + 1,
                        self.shared_buffers.waterfall_len,
                    )

                # --- archive (8-channel format) ---
                if self.shared_buffers.archiving_enabled and self.shared_buffers.archiver:
                    archive_data = {
                        'timestamp': result['timestamp'],
                        'tods': adc_data,
                        'ffts': fft_power,
                        'lags': lags,
                        'config': cfg,
                        'frames_averaged': len(all_bursts) * 2,
                    }
                    success = self.shared_buffers.archiver.archive_fine_lag_frame(archive_data)
                    if success:
                        st = self.shared_buffers.archiver.get_status()
                        if st['frames_archived'] % 50 == 0:
                            print(f"FineLag 8ch: Archived frame {st['frames_archived']}")

                # --- FPS ---
                self.frames_processed += 1
                now = time.time()
                dt = now - self.last_fps_time
                if dt >= 1.0:
                    self.fps = self.frames_processed / dt
                    self.last_fps_time = now
                    self.frames_processed = 0

                time.sleep(0.01)

            except Exception as e:
                import traceback
                print(f"Error in fine lag 8ch computation: {e}")
                traceback.print_exc()
                time.sleep(0.1)

    # ------------------------------------------------------------------
    # ADC flag helpers (unchanged)
    # ------------------------------------------------------------------
    def _reset_adc_flags(self):
        try:
            self.pc.i.GPIO.ADC_OR_CLEAR = 2**8 - 1
            self.pc.i.GPIO.ADC_OR_CLEAR = 0
            self.pc.i.GPIO.ADC_OV_CLEAR = 2**8 - 1
            self.pc.i.GPIO.ADC_OV_CLEAR = 0
        except Exception as e:
            print(f"Error resetting ADC flags: {e}")

    def _check_adc_flags(self):
        bitmask = 2 ** np.arange(8)
        bitshifts = np.arange(0, 8)[::-1]
        try:
            adc_or = self.pc.i.GPIO.ADC_OR
            adc_ov = self.pc.i.GPIO.ADC_OV
            adc_or_arr = np.array([(adc_or & bitmask[m]) >> m for m in bitshifts])[::-1]
            adc_ov_arr = np.array([(adc_ov & bitmask[m]) >> m for m in bitshifts])[::-1]
            self.shared_buffers.total_captures += 1
            if adc_or != 0:
                print('ADC overranges:', [f'ADC{n+1}' for n in range(8) if adc_or_arr[n]])
                self.shared_buffers.adc_or_count += 1
            if adc_ov != 0:
                print('ADC overvoltages:', [f'ADC{n+1}' for n in range(8) if adc_ov_arr[n]])
                self.shared_buffers.adc_ov_count += 1
        except Exception as e:
            print(f"Error checking ADC flags: {e}")

    def stop(self):
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
                    
                    # Archive successful detection
                    if self.shared_buffers.archiving_enabled and self.shared_buffers.archiver:
                        # Get lag spectra from shared buffers
                        archive_data = {
                            'timestamp': time.time(),
                            'adc0': d_adc[0],
                            'adc1': d_adc[1], 
                            'adc2': d_adc[2],
                            'adc3': d_adc[3],
                            'lag_adc0': self.shared_buffers.radar_lag_adc0.copy(),
                            'lag_adc1': self.shared_buffers.radar_lag_adc1.copy(),
                            'lag_adc2': self.shared_buffers.radar_lag_adc2.copy(),
                            'range_m': avg_range,
                            'azimuth_deg': avg_azimuth,
                            'elevation_deg': avg_elevation,
                            'delays': {i: self.shared_buffers.radar_delays[i] for i in range(3)},
                            'valid': True
                        }
                        success = self.shared_buffers.archiver.archive_radar_frame(archive_data)
                        if success:
                            archiver_status = self.shared_buffers.archiver.get_status()
                            frames_archived = archiver_status['frames_archived']
                            if frames_archived % 20 == 0:  # Print every 20 successful archives
                                print(f"Radar: Archived valid detection {frames_archived} to file")
                            
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
                    
                    # Archive invalid detection (optional - for data completeness)
                    if self.shared_buffers.archiving_enabled and self.shared_buffers.archiver:
                        archive_data = {
                            'timestamp': time.time(),
                            'adc0': d_adc[0],
                            'adc1': d_adc[1],
                            'adc2': d_adc[2], 
                            'adc3': d_adc[3],
                            'lag_adc0': self.shared_buffers.radar_lag_adc0.copy(),
                            'lag_adc1': self.shared_buffers.radar_lag_adc1.copy(),
                            'lag_adc2': self.shared_buffers.radar_lag_adc2.copy(),
                            'range_m': self.shared_buffers.radar_range[0],  # Last valid position
                            'azimuth_deg': self.shared_buffers.radar_azimuth[0],
                            'elevation_deg': self.shared_buffers.radar_elevation[0], 
                            'delays': {i: self.shared_buffers.radar_delays[i] for i in range(3)},
                            'valid': False
                        }
                        success = self.shared_buffers.archiver.archive_radar_frame(archive_data)
                        if success:
                            archiver_status = self.shared_buffers.archiver.get_status()
                            frames_archived = archiver_status['frames_archived']
                            if frames_archived % 100 == 0:  # Print every 100 successful archives
                                print(f"Radar: Archived invalid detection {frames_archived} to file")
                
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
                
                # Archive the frame if enabled
                if self.shared_buffers.archiving_enabled and self.shared_buffers.archiver:
                    archive_data = {
                        'timestamp': time.time(),
                        'adc0': adc0_data,  # 262,144 samples
                        'psd': psd,  # 131,073 bins
                    }
                    success = self.shared_buffers.archiver.archive_super_fft_frame(archive_data)
                    if success:
                        archiver_status = self.shared_buffers.archiver.get_status()
                        frames_archived = archiver_status['frames_archived']
                        if frames_archived % 50 == 0:  # Print every 50 successful archives
                            print(f"SuperFFT: Archived frame {frames_archived} to file")
                
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
        
        # Archiving support
        self.archiving_enabled = False
        self.archiver = None  # Will be set to DataArchiver instance
        self.archive_path = "./archives"
        
        # GUI reference for frequency conversion
        self.gui_instance = None  # Will be set to PeriscopeCorrelator instance
        self.current_nyquist_zone = 1  # Default to Zone 1
        
        # Create shared memory for current frame data (all as complex128)
        self.shm_p00 = shared_memory.SharedMemory(create=True, size=NBINS_TOTAL * 16)  # complex128 = 16 bytes
        self.shm_p11 = shared_memory.SharedMemory(create=True, size=NBINS_TOTAL * 16)
        self.shm_p01 = shared_memory.SharedMemory(create=True, size=NBINS_TOTAL * 16)
        
        # Create shared memory for waterfall data (all as complex128)
        wf_size = waterfall_len * NBINS_TOTAL * 16  # complex128 = 16 bytes
        self.shm_wf_p00 = shared_memory.SharedMemory(create=True, size=wf_size)
        self.shm_wf_p11 = shared_memory.SharedMemory(create=True, size=wf_size)
        self.shm_wf_p01 = shared_memory.SharedMemory(create=True, size=wf_size)
        
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
        
        # Create NumPy arrays backed by shared memory (all as complex128)
        self.p00 = np.ndarray((NBINS_TOTAL,), dtype=np.complex128, buffer=self.shm_p00.buf)
        self.p11 = np.ndarray((NBINS_TOTAL,), dtype=np.complex128, buffer=self.shm_p11.buf)
        self.p01 = np.ndarray((NBINS_TOTAL,), dtype=np.complex128, buffer=self.shm_p01.buf)
        
        self.wf_p00 = np.ndarray((waterfall_len, NBINS_TOTAL), dtype=np.complex128, buffer=self.shm_wf_p00.buf)
        self.wf_p11 = np.ndarray((waterfall_len, NBINS_TOTAL), dtype=np.complex128, buffer=self.shm_wf_p11.buf)
        self.wf_p01 = np.ndarray((waterfall_len, NBINS_TOTAL), dtype=np.complex128, buffer=self.shm_wf_p01.buf)
        
        # Create NumPy arrays for lag spectra
        self.lag_p00 = np.ndarray((NBINS_TOTAL,), dtype=np.float64, buffer=self.shm_lag_p00.buf)
        self.lag_p11 = np.ndarray((NBINS_TOTAL,), dtype=np.float64, buffer=self.shm_lag_p11.buf)
        self.lag_p01_mag = np.ndarray((NBINS_TOTAL,), dtype=np.float64, buffer=self.shm_lag_p01_mag.buf)
        self.lag_p01_phase = np.ndarray((NBINS_TOTAL,), dtype=np.float64, buffer=self.shm_lag_p01_phase.buf)
        
        self.lag_wf_p00 = np.ndarray((waterfall_len, NBINS_TOTAL), dtype=np.float64, buffer=self.shm_lag_wf_p00.buf)
        self.lag_wf_p11 = np.ndarray((waterfall_len, NBINS_TOTAL), dtype=np.float64, buffer=self.shm_lag_wf_p11.buf)
        self.lag_wf_p01_mag = np.ndarray((waterfall_len, NBINS_TOTAL), dtype=np.float64, buffer=self.shm_lag_wf_p01_mag.buf)
        self.lag_wf_p01_phase = np.ndarray((waterfall_len, NBINS_TOTAL), dtype=np.float64, buffer=self.shm_lag_wf_p01_phase.buf)
        
        # Initialize arrays (all complex)
        self.p00.fill(0)
        self.p11.fill(0)
        self.p01.fill(0)
        self.wf_p00.fill(0)
        self.wf_p11.fill(0)
        self.wf_p01.fill(0)
        
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
        
        # Running difference mode history buffers for lag spectra
        self.max_diff_window = 100  # Maximum window size for running difference
        self.shm_lag_history_p00 = shared_memory.SharedMemory(
            create=True, size=self.max_diff_window * NBINS_TOTAL * 8)
        self.shm_lag_history_p11 = shared_memory.SharedMemory(
            create=True, size=self.max_diff_window * NBINS_TOTAL * 8)
        self.shm_lag_history_p01 = shared_memory.SharedMemory(
            create=True, size=self.max_diff_window * NBINS_TOTAL * 8)
        
        self.lag_history_p00 = np.ndarray(
            (self.max_diff_window, NBINS_TOTAL), dtype=np.float64, 
            buffer=self.shm_lag_history_p00.buf)
        self.lag_history_p11 = np.ndarray(
            (self.max_diff_window, NBINS_TOTAL), dtype=np.float64, 
            buffer=self.shm_lag_history_p11.buf)
        self.lag_history_p01 = np.ndarray(
            (self.max_diff_window, NBINS_TOTAL), dtype=np.float64, 
            buffer=self.shm_lag_history_p01.buf)
        
        # Initialize history buffers
        self.lag_history_p00.fill(0)
        self.lag_history_p11.fill(0)
        self.lag_history_p01.fill(0)
        
        # History position and count
        self.lag_history_pos = 0
        self.lag_history_count = 0
        
        # Running difference waterfall buffers (separate from raw data)
        self.shm_lag_diff_wf_p00 = shared_memory.SharedMemory(
            create=True, size=wf_size)
        self.shm_lag_diff_wf_p11 = shared_memory.SharedMemory(
            create=True, size=wf_size)
        self.shm_lag_diff_wf_p01 = shared_memory.SharedMemory(
            create=True, size=wf_size)
        
        self.lag_diff_wf_p00 = np.ndarray(
            (waterfall_len, NBINS_TOTAL), dtype=np.float64, 
            buffer=self.shm_lag_diff_wf_p00.buf)
        self.lag_diff_wf_p11 = np.ndarray(
            (waterfall_len, NBINS_TOTAL), dtype=np.float64, 
            buffer=self.shm_lag_diff_wf_p11.buf)
        self.lag_diff_wf_p01 = np.ndarray(
            (waterfall_len, NBINS_TOTAL), dtype=np.float64, 
            buffer=self.shm_lag_diff_wf_p01.buf)
        
        # Initialize running difference arrays
        self.lag_diff_wf_p00.fill(0)
        self.lag_diff_wf_p11.fill(0)
        self.lag_diff_wf_p01.fill(0)
        
        # Fine lag data (8192 positive lag bins - half of 16384)
        n_fine_lags = 8192
        # Time domain samples - allocate for max frames we might average
        # Maximum 200 frames * 16384 samples/frame = 3,276,800 samples
        max_frames_for_display = 10
        n_time_samples = max_frames_for_display * 16384
        # Frequency domain data (8193 frequency bins for real FFT - 0 to Nyquist)
        n_fine_freq = 8193  # (16384 // 2) + 1 for rfft of real signals
        
        self.shm_fine_lag_p00 = shared_memory.SharedMemory(create=True, size=n_fine_lags * 8)
        self.shm_fine_lag_p11 = shared_memory.SharedMemory(create=True, size=n_fine_lags * 8)
        self.shm_fine_lag_p01 = shared_memory.SharedMemory(create=True, size=n_fine_lags * 8)  # Cross-correlation
        self.shm_fine_lag_wf_p00 = shared_memory.SharedMemory(create=True, size=waterfall_len * n_fine_lags * 8)
        self.shm_fine_lag_wf_p11 = shared_memory.SharedMemory(create=True, size=waterfall_len * n_fine_lags * 8)
        self.shm_fine_lag_wf_p01 = shared_memory.SharedMemory(create=True, size=waterfall_len * n_fine_lags * 8)  # Cross-correlation waterfall
        
        # Frequency domain data for intermediate products
        self.shm_fine_freq_adc0 = shared_memory.SharedMemory(create=True, size=n_fine_freq * 16)  # Complex128 = 16 bytes
        self.shm_fine_freq_adc1 = shared_memory.SharedMemory(create=True, size=n_fine_freq * 16)  # Complex128 = 16 bytes
        self.shm_fine_freq_cross = shared_memory.SharedMemory(create=True, size=n_fine_freq * 16)  # Complex128 = 16 bytes
        
        # Running difference waterfall buffers (separate from raw data)
        self.shm_fine_lag_diff_wf_p00 = shared_memory.SharedMemory(create=True, size=waterfall_len * n_fine_lags * 8)
        self.shm_fine_lag_diff_wf_p11 = shared_memory.SharedMemory(create=True, size=waterfall_len * n_fine_lags * 8)
        self.shm_fine_lag_diff_wf_p01 = shared_memory.SharedMemory(create=True, size=waterfall_len * n_fine_lags * 8)
        
        # Raw time domain data for fine lag
        self.shm_fine_time_adc0 = shared_memory.SharedMemory(create=True, size=n_time_samples * 8)
        self.shm_fine_time_adc1 = shared_memory.SharedMemory(create=True, size=n_time_samples * 8)
        
        # Frequency domain data for intermediate products
        self.shm_fine_freq_adc0 = shared_memory.SharedMemory(create=True, size=n_fine_freq * 16)  # Complex128 = 16 bytes
        self.shm_fine_freq_adc1 = shared_memory.SharedMemory(create=True, size=n_fine_freq * 16)  # Complex128 = 16 bytes
        self.shm_fine_freq_cross = shared_memory.SharedMemory(create=True, size=n_fine_freq * 16)  # Complex128 = 16 bytes
        
        self.fine_lag_p00 = np.ndarray((n_fine_lags,), dtype=np.float64, buffer=self.shm_fine_lag_p00.buf)
        self.fine_lag_p11 = np.ndarray((n_fine_lags,), dtype=np.float64, buffer=self.shm_fine_lag_p11.buf)
        self.fine_lag_p01 = np.ndarray((n_fine_lags,), dtype=np.float64, buffer=self.shm_fine_lag_p01.buf)  # Cross-correlation
        self.fine_lag_wf_p00 = np.ndarray((waterfall_len, n_fine_lags), dtype=np.float64, buffer=self.shm_fine_lag_wf_p00.buf)
        self.fine_lag_wf_p11 = np.ndarray((waterfall_len, n_fine_lags), dtype=np.float64, buffer=self.shm_fine_lag_wf_p11.buf)
        self.fine_lag_wf_p01 = np.ndarray((waterfall_len, n_fine_lags), dtype=np.float64, buffer=self.shm_fine_lag_wf_p01.buf)  # Cross-correlation waterfall
        
        # Running difference waterfall arrays
        self.fine_lag_diff_wf_p00 = np.ndarray((waterfall_len, n_fine_lags), dtype=np.float64, buffer=self.shm_fine_lag_diff_wf_p00.buf)
        self.fine_lag_diff_wf_p11 = np.ndarray((waterfall_len, n_fine_lags), dtype=np.float64, buffer=self.shm_fine_lag_diff_wf_p11.buf)
        self.fine_lag_diff_wf_p01 = np.ndarray((waterfall_len, n_fine_lags), dtype=np.float64, buffer=self.shm_fine_lag_diff_wf_p01.buf)
        
        # Time domain arrays
        self.fine_time_adc0 = np.ndarray((n_time_samples,), dtype=np.float64, buffer=self.shm_fine_time_adc0.buf)
        self.fine_time_adc1 = np.ndarray((n_time_samples,), dtype=np.float64, buffer=self.shm_fine_time_adc1.buf)
        
        # Frequency domain arrays for intermediate products
        self.fine_freq_adc0 = np.ndarray((n_fine_freq,), dtype=np.complex128, buffer=self.shm_fine_freq_adc0.buf)
        self.fine_freq_adc1 = np.ndarray((n_fine_freq,), dtype=np.complex128, buffer=self.shm_fine_freq_adc1.buf)
        self.fine_freq_cross = np.ndarray((n_fine_freq,), dtype=np.complex128, buffer=self.shm_fine_freq_cross.buf)
        
        # Initialize fine lag arrays
        self.fine_lag_p00.fill(0)
        self.fine_lag_p11.fill(0)
        self.fine_lag_p01.fill(0)  # Cross-correlation
        self.fine_lag_wf_p00.fill(0)
        self.fine_lag_wf_p11.fill(0)
        self.fine_lag_wf_p01.fill(0)  # Cross-correlation waterfall
        self.fine_time_adc0.fill(0)
        self.fine_time_adc1.fill(0)
        
        # Initialize frequency domain arrays
        self.fine_freq_adc0.fill(0)
        self.fine_freq_adc1.fill(0)
        self.fine_freq_cross.fill(0)
        
        # Initialize running difference arrays
        self.fine_lag_diff_wf_p00.fill(0)
        self.fine_lag_diff_wf_p11.fill(0)
        self.fine_lag_diff_wf_p01.fill(0)
        
        # Fine lag waterfall position and lock
        self.fine_lag_wf_write_pos = 0
        self.fine_lag_frames_written = 0
        self.fine_lag_lock = threading.Lock()
        
        # 8-channel fine lag result dict (new-style, written by FineLagComputer)
        # Contains: tods, ffts, fft_complex, lags, timestamp, config, n_bursts_averaged
        self.fine_lag_result = None  # Will be set to a dict by FineLagComputer
        
        # Track actual number of time domain samples being used
        self.fine_time_n_samples = 32768  # Default to 2 frames
        
        # Control flags for running difference mode (set by UI thread)
        self.running_diff_enabled = False
        self.running_diff_window_size = 10
        
        # Running difference mode history buffers for fine lag
        self.max_diff_window = 100  # Maximum window size for running difference
        self.shm_fine_lag_history_p00 = shared_memory.SharedMemory(
            create=True, size=self.max_diff_window * n_fine_lags * 8)
        self.shm_fine_lag_history_p11 = shared_memory.SharedMemory(
            create=True, size=self.max_diff_window * n_fine_lags * 8)
        self.shm_fine_lag_history_p01 = shared_memory.SharedMemory(
            create=True, size=self.max_diff_window * n_fine_lags * 8)
        
        self.fine_lag_history_p00 = np.ndarray(
            (self.max_diff_window, n_fine_lags), dtype=np.float64, 
            buffer=self.shm_fine_lag_history_p00.buf)
        self.fine_lag_history_p11 = np.ndarray(
            (self.max_diff_window, n_fine_lags), dtype=np.float64, 
            buffer=self.shm_fine_lag_history_p11.buf)
        self.fine_lag_history_p01 = np.ndarray(
            (self.max_diff_window, n_fine_lags), dtype=np.float64, 
            buffer=self.shm_fine_lag_history_p01.buf)
        
        # Initialize history buffers
        self.fine_lag_history_p00.fill(0)
        self.fine_lag_history_p11.fill(0)
        self.fine_lag_history_p01.fill(0)
        
        # History position and count
        self.fine_lag_history_pos = 0
        self.fine_lag_history_count = 0
        
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
        # Add all shared memory buffers to cleanup including lag history and diff buffers
        for shm in [self.shm_p00, self.shm_p11, self.shm_p01,
                    self.shm_wf_p00, self.shm_wf_p11, self.shm_wf_p01,
                    self.shm_lag_p00, self.shm_lag_p11, self.shm_lag_p01_mag, self.shm_lag_p01_phase,
                    self.shm_lag_wf_p00, self.shm_lag_wf_p11, self.shm_lag_wf_p01_mag, self.shm_lag_wf_p01_phase,
                    self.shm_lag_history_p00, self.shm_lag_history_p11, self.shm_lag_history_p01,
                    self.shm_lag_diff_wf_p00, self.shm_lag_diff_wf_p11, self.shm_lag_diff_wf_p01,
                    self.shm_fine_lag_p00, self.shm_fine_lag_p11, self.shm_fine_lag_p01,
                    self.shm_fine_lag_wf_p00, self.shm_fine_lag_wf_p11, self.shm_fine_lag_wf_p01,
                    self.shm_fine_lag_diff_wf_p00, self.shm_fine_lag_diff_wf_p11, self.shm_fine_lag_diff_wf_p01,
                    self.shm_fine_time_adc0, self.shm_fine_time_adc1,
                    self.shm_fine_freq_adc0, self.shm_fine_freq_adc1, self.shm_fine_freq_cross,
                    self.shm_fine_lag_history_p00, self.shm_fine_lag_history_p11, self.shm_fine_lag_history_p01,
                    self.shm_super_fft_psd, self.shm_super_fft_wf_psd, self.shm_super_fft_time,
                    self.shm_radar_range, self.shm_radar_azimuth, self.shm_radar_elevation,
                    self.shm_radar_detection_valid, self.shm_radar_history_range,
                    self.shm_radar_history_azimuth, self.shm_radar_history_elevation,
                    self.shm_radar_lag_adc0, self.shm_radar_lag_adc1, self.shm_radar_lag_adc2,
                    self.shm_radar_delays, self.shm_radar_centroid_starts, self.shm_radar_centroid_ends]:
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
        
        # Frame averaging buffers
        self.frames_accumulated = 0
        self.p00_accumulator = np.zeros(NBINS_TOTAL, dtype=np.complex128)
        self.p11_accumulator = np.zeros(NBINS_TOTAL, dtype=np.complex128)
        self.p01_accumulator = np.zeros(NBINS_TOTAL, dtype=np.complex128)
        
        # Pre-calculate FFT bin remapping (matches reference code)
        # The correlator outputs data in 4 lanes of 2048 bins each
        # fft_bins[lane, clk] gives the correct index in the 8192-element output array
        self.fft_bins = np.array([
            [2048*((lane - (clk & 0b11)) & 0b11) + clk 
             for clk in range(2048)] 
            for lane in range(4)
        ])
        
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
                ## Reset ADC flags for next frame capture
                #self._reset_adc_flags()

                nbytes, _ = self.socket.recvfrom_into(packet_buffer)

                ## Check ADC flags after completing frame capture
                #self._check_adc_flags()
                
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
        """Decode the complex data from the raw packet format and apply bin remapping."""
        # Ensure proper sign extension by viewing as signed int32
        h_signed = data['h'].view(np.int32)
        l_signed = data['l'].view(np.int32)
        
        # Perform bit extraction with proper sign extension
        # Extract real part (bits 27:10)
        self.temp32_real[:] = (h_signed << 4) >> 14
        # Extract imaginary part (bits 17:0)
        self.temp32_imag[:] = (l_signed << 14) >> 14
        
        # Decode stream_id to lane and clock
        lane = bin_idx // 2048  # Which lane (0-3)
        clk = bin_idx % 2048     # Position within lane (0-2047)
        
        # Apply the FFT bin remapping
        remapped_idx = self.fft_bins[lane, clk]
        
        # Store at the remapped index
        self.raw_frame[remapped_idx] = self.temp32_real + 1j * self.temp32_imag

    def _process_complete_frame(self):
        """Process completed frame and update shared buffers."""
        if self.config.n_firmware_frames == 0: 
            return
        
        # Normalize the frame
        normalized_frame = self.raw_frame / self.config.n_firmware_frames
        if self.config.gain_factor is not None:
            normalized_frame *= self.config.gain_factor

        # Extract complex data directly
        p00_data = normalized_frame[:, self.config.idx00]
        p11_data = normalized_frame[:, self.config.idx11]
        p01_data = normalized_frame[:, self.config.idx01]

        # Update shared memory buffers with minimal lock time
        with self.shared_buffers.lock:
            # Store complex values directly
            self.shared_buffers.p00[:] = p00_data
            self.shared_buffers.p11[:] = p11_data
            self.shared_buffers.p01[:] = p01_data
            
            # Update waterfall - shift existing data down and insert new at top
            # Roll the arrays down by 1 to make room at position 0
            self.shared_buffers.wf_p00[1:] = self.shared_buffers.wf_p00[:-1]
            self.shared_buffers.wf_p11[1:] = self.shared_buffers.wf_p11[:-1]
            self.shared_buffers.wf_p01[1:] = self.shared_buffers.wf_p01[:-1]
            
            # Write new data at position 0 (top)
            self.shared_buffers.wf_p00[0] = p00_data
            self.shared_buffers.wf_p11[0] = p11_data
            self.shared_buffers.wf_p01[0] = p01_data
            
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
        # Time between lag bins = 1 / FS_HZ seconds (NO M_FACTOR - correlator visibilities are not decimated)
        lag_time_s = lag_bins / FS_HZ  # Time in seconds
        c_m_per_s = 299792458.0  # Speed of light in meters per second
        self.lag_axis = lag_time_s * c_m_per_s / 2  # Convert to distance in meters (round trip)
        # Range: ~-192 to +192 meters with 4.68 cm resolution (same as fine lag mode)
        
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
        # Main toolbar container with three rows
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
        
        # Second row - Display and visualization controls
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
        
        # Running difference mode controls
        self.running_diff_checkbox = QtWidgets.QCheckBox("Running Difference")
        self.running_diff_checkbox.setToolTip("Subtract moving average of previous N frames to highlight changes")
        self.running_diff_checkbox.setChecked(False)
        
        self.diff_window_spinner = QtWidgets.QSpinBox()
        self.diff_window_spinner.setRange(2, 100)
        self.diff_window_spinner.setValue(10)
        self.diff_window_spinner.setPrefix("Window: ")
        self.diff_window_spinner.setSuffix(" frames")
        self.diff_window_spinner.setToolTip("Number of frames for moving average in running difference mode")
        self.diff_window_spinner.setEnabled(False)  # Disabled until running diff is checked
        
        # Connect running diff checkbox to enable/disable spinner and update shared buffer flags
        self.running_diff_checkbox.stateChanged.connect(self._on_running_diff_changed)
        self.diff_window_spinner.valueChanged.connect(self._on_diff_window_changed)
        
        self.fix_color_scale_checkbox = QtWidgets.QCheckBox("Fix Color Scale")
        self.fix_color_scale_checkbox.setToolTip("Lock the current color scale to see relative changes over time")
        self.fix_color_scale_checkbox.setChecked(False)
        
        self.dark_mode_colors_checkbox = QtWidgets.QCheckBox("Dark Mode Colors")
        self.dark_mode_colors_checkbox.setToolTip("Use dark diverging colormap (black at zero) for difference modes")
        self.dark_mode_colors_checkbox.setChecked(False)
        
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
        
        # Phase unwrapping checkbox for correlator plots
        self.unwrap_correlator_phase_checkbox = QtWidgets.QCheckBox("Unwrap Correlator Phase")
        self.unwrap_correlator_phase_checkbox.setToolTip("Apply phase unwrapping to correlator phase plots")
        self.unwrap_correlator_phase_checkbox.setChecked(False)
        
        # Nyquist zone selector
        self.nyquist_zone_combo = QtWidgets.QComboBox()
        self.nyquist_zone_combo.addItems(["Zone 1 (0-1600 MHz)", "Zone 2 (1600-3200 MHz)", "Zone 3 (3200-4800 MHz)", "Zone 4 (4800-6400 MHz)"])
        self.nyquist_zone_combo.setCurrentIndex(0)  # Default to Zone 1
        self.nyquist_zone_combo.setToolTip("Select Nyquist zone for frequency axis display")
        self.nyquist_zone_combo.currentIndexChanged.connect(self._on_nyquist_zone_changed)
        
        # Lag frequency range controls
        self.lag_freq_start_spinner = QtWidgets.QDoubleSpinBox()
        self.lag_freq_start_spinner.setRange(0.0, 1600.0)  # Will be updated based on Nyquist zone
        self.lag_freq_start_spinner.setSingleStep(10.0)
        self.lag_freq_start_spinner.setValue(0.0)
        self.lag_freq_start_spinner.setDecimals(1)
        self.lag_freq_start_spinner.setSuffix(" MHz")
        self.lag_freq_start_spinner.setPrefix("Lag Start: ")
        self.lag_freq_start_spinner.setToolTip("Start frequency for lag computation (excludes lower frequencies)")
        self.lag_freq_start_spinner.valueChanged.connect(self._on_lag_freq_range_changed)
        
        self.lag_freq_end_spinner = QtWidgets.QDoubleSpinBox()
        self.lag_freq_end_spinner.setRange(0.0, 1600.0)  # Will be updated based on Nyquist zone
        self.lag_freq_end_spinner.setSingleStep(10.0)
        self.lag_freq_end_spinner.setValue(1600.0)
        self.lag_freq_end_spinner.setDecimals(1)
        self.lag_freq_end_spinner.setSuffix(" MHz")
        self.lag_freq_end_spinner.setPrefix("End: ")
        self.lag_freq_end_spinner.setToolTip("End frequency for lag computation (excludes higher frequencies)")
        self.lag_freq_end_spinner.valueChanged.connect(self._on_lag_freq_range_changed)
        
        self.lag_freq_reset_button = QtWidgets.QPushButton("Reset Lag Range")
        self.lag_freq_reset_button.setToolTip("Reset lag frequency range to full band for current Nyquist zone")
        self.lag_freq_reset_button.clicked.connect(self._reset_lag_freq_range)
        
        # Add widgets to second row
        toolbar_row2.addWidget(self.scale_update_spinner)
        toolbar_row2.addWidget(self.mean_subtract_checkbox)
        toolbar_row2.addWidget(self.running_diff_checkbox)
        toolbar_row2.addWidget(self.diff_window_spinner)
        toolbar_row2.addWidget(self.fix_color_scale_checkbox)
        toolbar_row2.addWidget(self.dark_mode_colors_checkbox)
        toolbar_row2.addWidget(self.log_scale_checkbox)
        toolbar_row2.addWidget(self.symlog_crossover_spinner)
        toolbar_row2.addWidget(self.viewbox_mode_checkbox)
        toolbar_row2.addWidget(self.unwrap_correlator_phase_checkbox)
        toolbar_row2.addSpacing(20)
        toolbar_row2.addWidget(QtWidgets.QLabel("Nyquist Zone:"))
        toolbar_row2.addWidget(self.nyquist_zone_combo)
        toolbar_row2.addSpacing(10)
        toolbar_row2.addWidget(self.lag_freq_start_spinner)
        toolbar_row2.addWidget(self.lag_freq_end_spinner)
        toolbar_row2.addWidget(self.lag_freq_reset_button)
        toolbar_row2.addStretch(1)
        
        # Third row - Mode controls and advanced features
        toolbar_row3 = QtWidgets.QHBoxLayout()
        
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
        
        # Fine lag frames to average spinner
        self.fine_lag_avg_spinner = QtWidgets.QSpinBox()
        self.fine_lag_avg_spinner.setRange(1, 4)  # Will be updated based on ucap_mode
        self.fine_lag_avg_spinner.setValue(4)  # Default to max for best SNR
        self.fine_lag_avg_spinner.setPrefix("Fine Lag Avg: ")
        self.fine_lag_avg_spinner.setSuffix(" frames")
        self.fine_lag_avg_spinner.setToolTip("Number of frames to average in fine lag mode (more = better SNR)")
        self.fine_lag_avg_spinner.valueChanged.connect(self._update_fine_lag_averaging)
        self.fine_lag_avg_spinner.setEnabled(False)  # Disabled until fine lag mode active
        
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
        
        # Archive controls
        self.archive_checkbox = QtWidgets.QCheckBox("Enable Archiving")
        self.archive_checkbox.setToolTip("Archive data to HDF5 file")
        self.archive_checkbox.stateChanged.connect(self._toggle_archiving)
        self.archive_checkbox.setEnabled(DATA_ARCHIVER_AVAILABLE)

        self.archive_path_button = QtWidgets.QPushButton("Archive Path...")
        self.archive_path_button.setToolTip("Select archive directory")
        self.archive_path_button.clicked.connect(self._select_archive_path)
        self.archive_path_button.setEnabled(DATA_ARCHIVER_AVAILABLE)

        self.archive_status_label = QtWidgets.QLabel("Not Recording")
        self.archive_status_label.setStyleSheet("color: gray;")
        
        # Restore all plots button
        self.restore_plots_button = QtWidgets.QPushButton("Restore All Plots")
        self.restore_plots_button.setToolTip("Show all hidden plots")
        self.restore_plots_button.clicked.connect(self._restore_all_plots)

        # Add widgets to third row
        toolbar_row3.addWidget(self.enable_lag_checkbox)
        toolbar_row3.addWidget(self.frames_to_average_spinner)
        toolbar_row3.addSpacing(20)
        toolbar_row3.addWidget(self.fine_lag_mode_button)
        toolbar_row3.addWidget(self.fine_lag_avg_spinner)
        toolbar_row3.addWidget(self.super_fft_mode_button)
        toolbar_row3.addSpacing(20)
        toolbar_row3.addWidget(self.radar_mode_button)
        toolbar_row3.addWidget(self.radar_threshold_spinner)
        toolbar_row3.addSpacing(20)
        toolbar_row3.addWidget(self.archive_checkbox)
        toolbar_row3.addWidget(self.archive_path_button)
        toolbar_row3.addWidget(self.archive_status_label)
        toolbar_row3.addSpacing(20)
        toolbar_row3.addWidget(self.restore_plots_button)
        toolbar_row3.addStretch(1)
        
        # Add all three rows to the toolbar container
        # Add all three rows to the toolbar container
        toolbar_container.addLayout(toolbar_row1)
        toolbar_container.addLayout(toolbar_row2)
        toolbar_container.addLayout(toolbar_row3)
        
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
        self.plot_selectors = {}  # Store the product selectors
        colors = pg.colormap.get("tab10", "matplotlib").getColors()

        # Generate product options for selectors
        product_options = []
        for i in range(NCHAN):
            for j in range(i, NCHAN):
                if i == j:
                    product_options.append(f"ADC{i} Auto")
                else:
                    product_options.append(f"ADC{i}/{j} Cross")

        # --- Top Left Plot (Default P00) ---
        p00_widget = QtWidgets.QWidget()
        p00_layout = QtWidgets.QVBoxLayout(p00_widget)
        
        # Control bar with checkbox and selectors
        p00_control = QtWidgets.QHBoxLayout()
        p00_cb = QtWidgets.QCheckBox("Plot 1")
        p00_cb.setChecked(True)
        self.plot_checkboxes["s_p00"] = p00_cb
        
        p00_selector = QtWidgets.QComboBox()
        p00_selector.addItems(product_options)
        p00_selector.setCurrentText("ADC0 Auto")
        p00_selector.currentTextChanged.connect(lambda text: self._update_plot_product(0, text))
        self.plot_selectors[0] = p00_selector
        
        p00_component = QtWidgets.QComboBox()
        p00_component.addItems(["Magnitude", "Real", "Imaginary", "Phase"])
        p00_component.setCurrentText("Magnitude")
        p00_component.currentTextChanged.connect(lambda text: self._update_plot_component(0, text))
        self.plot_selectors['0_component'] = p00_component
        
        p00_control.addWidget(p00_cb)
        p00_control.addWidget(p00_selector)
        p00_control.addWidget(p00_component)
        p00_control.addStretch()
        
        p00_plot = pg.PlotWidget()
        p00_layout.addLayout(p00_control)
        p00_layout.addWidget(p00_plot)
        self.plots[(0,0)] = p00_plot.plot(pen=colors[0], name='|amp|')
        self.plot_widgets[(0,0)] = p00_plot  # Store the widget
        p00_plot.showGrid(x=True, y=True)
        p00_plot.setLabel('bottom', 'Frequency', units='Hz')
        p00_plot.addLegend()
        layout.addWidget(p00_widget, 0, 0)
        # Connect checkbox to hide/show the widget
        p00_cb.stateChanged.connect(lambda state, w=p00_widget: w.setVisible(state == QtCore.Qt.CheckState.Checked))

        # --- Bottom Left Plot (Default P01 Abs) ---
        p01_abs_widget = QtWidgets.QWidget()
        p01_abs_layout = QtWidgets.QVBoxLayout(p01_abs_widget)
        
        # Control bar
        p01_abs_control = QtWidgets.QHBoxLayout()
        p01_abs_cb = QtWidgets.QCheckBox("Plot 2")
        p01_abs_cb.setChecked(True)
        self.plot_checkboxes["s_p01_abs"] = p01_abs_cb
        
        p01_abs_selector = QtWidgets.QComboBox()
        p01_abs_selector.addItems(product_options)
        p01_abs_selector.setCurrentText("ADC0/1 Cross")
        p01_abs_selector.currentTextChanged.connect(lambda text: self._update_plot_product(1, text))
        self.plot_selectors[1] = p01_abs_selector
        
        p01_abs_component = QtWidgets.QComboBox()
        p01_abs_component.addItems(["Magnitude", "Real", "Imaginary", "Phase"])
        p01_abs_component.setCurrentText("Magnitude")
        p01_abs_component.currentTextChanged.connect(lambda text: self._update_plot_component(1, text))
        self.plot_selectors['1_component'] = p01_abs_component
        
        p01_abs_control.addWidget(p01_abs_cb)
        p01_abs_control.addWidget(p01_abs_selector)
        p01_abs_control.addWidget(p01_abs_component)
        p01_abs_control.addStretch()
        
        p01_abs_plot = pg.PlotWidget()
        p01_abs_layout.addLayout(p01_abs_control)
        p01_abs_layout.addWidget(p01_abs_plot)
        self.plots[(0,1,'abs')] = p01_abs_plot.plot(pen=colors[1], name='|amp|')
        self.plot_widgets[(0,1,'abs')] = p01_abs_plot  # Store the widget
        p01_abs_plot.addLegend()
        p01_abs_plot.showGrid(x=True, y=True)
        p01_abs_plot.setLabel('bottom', 'Frequency', units='Hz')
        layout.addWidget(p01_abs_widget, 1, 0)
        # Connect checkbox to hide/show the widget
        p01_abs_cb.stateChanged.connect(lambda state, w=p01_abs_widget: w.setVisible(state == QtCore.Qt.CheckState.Checked))

        # --- Bottom Right Plot (Default P01 Real) ---
        p01_real_widget = QtWidgets.QWidget()
        p01_real_layout = QtWidgets.QVBoxLayout(p01_real_widget)
        
        # Control bar
        p01_real_control = QtWidgets.QHBoxLayout()
        p01_real_cb = QtWidgets.QCheckBox("Plot 3")
        p01_real_cb.setChecked(True)
        self.plot_checkboxes["s_p01_real"] = p01_real_cb
        
        p01_real_selector = QtWidgets.QComboBox()
        p01_real_selector.addItems(product_options)
        p01_real_selector.setCurrentText("ADC0/1 Cross")
        p01_real_selector.currentTextChanged.connect(lambda text: self._update_plot_product(2, text))
        self.plot_selectors[2] = p01_real_selector
        
        p01_real_component = QtWidgets.QComboBox()
        p01_real_component.addItems(["Real", "Imaginary", "Magnitude", "Phase"])
        p01_real_component.setCurrentText("Real")
        p01_real_component.currentTextChanged.connect(lambda text: self._update_plot_component(2, text))
        self.plot_selectors['2_component'] = p01_real_component
        
        p01_real_control.addWidget(p01_real_cb)
        p01_real_control.addWidget(p01_real_selector)
        p01_real_control.addWidget(p01_real_component)
        p01_real_control.addStretch()
        
        p01_real_plot = pg.PlotWidget()
        p01_real_layout.addLayout(p01_real_control)
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

        # --- Top Right Plot (Default P11) ---
        p11_widget = QtWidgets.QWidget()
        p11_layout = QtWidgets.QVBoxLayout(p11_widget)
        
        # Control bar with checkbox and selectors
        p11_control = QtWidgets.QHBoxLayout()
        p11_cb = QtWidgets.QCheckBox("Plot 4")
        p11_cb.setChecked(True)
        self.plot_checkboxes["s_p11"] = p11_cb
        
        p11_selector = QtWidgets.QComboBox()
        p11_selector.addItems(product_options)
        p11_selector.setCurrentText("ADC1 Auto")
        p11_selector.currentTextChanged.connect(lambda text: self._update_plot_product(3, text))
        self.plot_selectors[3] = p11_selector
        
        p11_component = QtWidgets.QComboBox()
        p11_component.addItems(["Magnitude", "Real", "Imaginary", "Phase"])
        p11_component.setCurrentText("Magnitude")
        p11_component.currentTextChanged.connect(lambda text: self._update_plot_component(3, text))
        self.plot_selectors['3_component'] = p11_component
        
        p11_control.addWidget(p11_cb)
        p11_control.addWidget(p11_selector)
        p11_control.addWidget(p11_component)
        p11_control.addStretch()
        
        p11_plot = pg.PlotWidget()
        p11_layout.addLayout(p11_control)
        p11_layout.addWidget(p11_plot)
        self.plots[(1,1)] = p11_plot.plot(pen=colors[3], name='|amp|')
        self.plot_widgets[(1,1)] = p11_plot  # Store the widget
        p11_plot.showGrid(x=True, y=True)
        p11_plot.setLabel('bottom', 'Frequency', units='Hz')
        p11_plot.addLegend()
        layout.addWidget(p11_widget, 0, 1)
        # Connect checkbox to hide/show the widget
        p11_cb.stateChanged.connect(lambda state, w=p11_widget: w.setVisible(state == QtCore.Qt.CheckState.Checked))
        
        # Store current plot configurations
        self.plot_configs = {
            0: {'product': (0, 0), 'component': 'magnitude'},
            1: {'product': (0, 1), 'component': 'magnitude'},
            2: {'product': (0, 1), 'component': 'real'},
            3: {'product': (1, 1), 'component': 'magnitude'}
        }
    
    def _update_plot_product(self, plot_idx: int, product_text: str) -> None:
        """Update which product a plot displays based on selector text."""
        # Parse the product text (e.g., "ADC0 Auto" or "ADC0/1 Cross")
        if "Auto" in product_text:
            # Auto-correlation (e.g., "ADC0 Auto")
            adc_num = int(product_text.split("ADC")[1].split()[0])
            product = (adc_num, adc_num)
        else:
            # Cross-correlation (e.g., "ADC0/1 Cross")
            parts = product_text.split("/")
            adc1 = int(parts[0].split("ADC")[1])
            adc2 = int(parts[1].split()[0])
            product = (adc1, adc2)
        
        # Update configuration
        self.plot_configs[plot_idx]['product'] = product
        
        # Update the product indices in config if this is one of the default plots
        if plot_idx == 0:
            # Find the index for this product in prod_map
            idx = np.where((self.prod_map[:, 0] == product[0]) & (self.prod_map[:, 1] == product[1]))[0]
            if len(idx) > 0:
                self.config.idx00 = idx[0]
        elif plot_idx == 3:
            idx = np.where((self.prod_map[:, 0] == product[0]) & (self.prod_map[:, 1] == product[1]))[0]
            if len(idx) > 0:
                self.config.idx11 = idx[0]
        elif plot_idx in [1, 2]:
            idx = np.where((self.prod_map[:, 0] == product[0]) & (self.prod_map[:, 1] == product[1]))[0]
            if len(idx) > 0:
                self.config.idx01 = idx[0]
        
        print(f"Plot {plot_idx} now showing product {product}")
    
    def _update_plot_component(self, plot_idx: int, component_text: str) -> None:
        """Update which component a plot displays (magnitude, real, imaginary, phase)."""
        # Map friendly names to internal names
        component_map = {
            "Magnitude": "magnitude",
            "Real": "real",
            "Imaginary": "imaginary",
            "Phase": "phase"
        }
        
        component = component_map.get(component_text, "magnitude")
        self.plot_configs[plot_idx]['component'] = component
        
        print(f"Plot {plot_idx} now showing {component}")

    def _update_waterfall_product(self, waterfall_idx: int, product_text: str) -> None:
        """Update which product a waterfall displays based on selector text."""
        # Parse the product text (e.g., "ADC0 Auto" or "ADC0/1 Cross")
        self.waterfall_configs[waterfall_idx]['product_text'] = product_text
        print(f"Waterfall {waterfall_idx} now showing product {product_text}")
    
    def _update_waterfall_component(self, waterfall_idx: int, component_text: str) -> None:
        """Update which component a waterfall displays (magnitude, real, imaginary, phase)."""
        component = component_text.lower()
        self.waterfall_configs[waterfall_idx]['component'] = component
        print(f"Waterfall {waterfall_idx} now showing {component}")

    def _update_fine_lag_plot_channel(self, plot_idx: int, channel_text: str) -> None:
        """Update which channel a fine lag plot displays."""
        self.fine_lag_plot_configs[plot_idx]['channel'] = channel_text
        print(f"Fine lag plot {plot_idx} now showing channel {channel_text}")
    
    def _update_fine_lag_plot_type(self, plot_idx: int, plot_type_text: str) -> None:
        """Update the plot type for a fine lag plot."""
        self.fine_lag_plot_configs[plot_idx]['plot_type'] = plot_type_text
        print(f"Fine lag plot {plot_idx} now showing {plot_type_text}")
    
    def _update_fine_lag_plot_component(self, plot_idx: int, component_text: str) -> None:
        """Update which component a fine lag plot displays."""
        self.fine_lag_plot_configs[plot_idx]['component'] = component_text
        print(f"Fine lag plot {plot_idx} now showing component {component_text}")

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

        # Generate product options for waterfall selectors
        product_options = []
        for i in range(NCHAN):
            for j in range(i, NCHAN):
                if i == j:
                    product_options.append(f"ADC{i} Auto")
                else:
                    product_options.append(f"ADC{i}/{j} Cross")

        # Default configurations for waterfall plots
        default_configs = [
            {"product": "ADC0 Auto", "component": "Magnitude", "title": "Waterfall 1"},
            {"product": "ADC1 Auto", "component": "Magnitude", "title": "Waterfall 2"},
            {"product": "ADC0/1 Cross", "component": "Real", "title": "Waterfall 3"},
            {"product": "ADC0/1 Cross", "component": "Phase", "title": "Waterfall 4"}
        ]

        # Store waterfall configurations
        self.waterfall_configs = {}
        
        for i, config in enumerate(default_configs):
            row, col = divmod(i, 2)
            
            container = QtWidgets.QWidget()
            container_layout = QtWidgets.QVBoxLayout(container)
            
            # Control bar with checkbox and selectors
            control_layout = QtWidgets.QHBoxLayout()
            
            cb = QtWidgets.QCheckBox(config["title"])
            cb.setChecked(True)
            plot_key = f"wf_{i}"
            self.plot_checkboxes[plot_key] = cb
            
            # Product selector
            product_selector = QtWidgets.QComboBox()
            product_selector.addItems(product_options)
            product_selector.setCurrentText(config["product"])
            product_selector.currentTextChanged.connect(lambda text, idx=i: self._update_waterfall_product(idx, text))
            
            # Component selector
            component_selector = QtWidgets.QComboBox()
            component_selector.addItems(["Magnitude", "Real", "Imaginary", "Phase"])
            component_selector.setCurrentText(config["component"])
            component_selector.currentTextChanged.connect(lambda text, idx=i: self._update_waterfall_component(idx, text))
            
            control_layout.addWidget(cb)
            control_layout.addWidget(product_selector)
            control_layout.addWidget(component_selector)
            control_layout.addStretch()
            
            # Use PlotWidget instead of ImageView for better performance
            plot = pg.PlotWidget()
            plot.setLabel('left', 'Time')
            plot.setLabel('bottom', 'Frequency Bin')
            # Invert Y axis so 0 is at top (newest data)
            plot.invertY(True)
            
            # Create an ImageItem
            img_item = pg.ImageItem()
            plot.addItem(img_item)
            # Use diverging colormap for cross-correlations and phases
            if config["component"] in ["Real", "Imaginary", "Phase"]:
                img_item.setLookupTable(coolwarm_cm.getLookupTable())
            else:
                img_item.setLookupTable(viridis_cm.getLookupTable())
            
            # Use index-based keys for waterfall plots
            key = f"wf_{i}"
            self.waterfall_plots[key] = plot
            self.waterfall_images[key] = img_item
            self.waterfall_levels[key] = None  # Will be set on first data
            self.waterfall_level_update_counter[key] = 0
            
            # Store configuration
            self.waterfall_configs[i] = {
                'product_text': config["product"],
                'component': config["component"].lower(),
                'product_selector': product_selector,
                'component_selector': component_selector
            }
            
            container_layout.addLayout(control_layout)
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
        self.lag_waterfall_colorbars = {}  # Store colorbars
        
        # Get colormaps - viridis for normal, diverging for differences
        viridis_cm = pg.colormap.get('viridis')
        # Light mode: CET-D1 (red-white-blue) for differences 
        rdbu_light_cm = pg.colormap.get('CET-D1')  # Diverging colormap (white at zero)
        # Dark mode: CET-D4 (red-black-blue) for differences
        rdbu_dark_cm = pg.colormap.get('CET-D4')   # Dark diverging colormap (black at zero)

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

            # Create a GraphicsLayoutWidget to hold both plot and colorbar
            graphics_layout = pg.GraphicsLayoutWidget()
            
            # Add the plot
            plot = graphics_layout.addPlot(row=0, col=0)
            plot.setLabel('left', 'Time')
            plot.setLabel('bottom', 'Distance', units='m')
            # Invert Y axis so 0 is at top (newest data)
            plot.invertY(True)
            
            img_item = pg.ImageItem()
            plot.addItem(img_item)
            
            # Store all colormaps - we'll switch between them based on mode
            img_item._viridis_lut = viridis_cm.getLookupTable()
            img_item._rdbu_light_lut = rdbu_light_cm.getLookupTable()
            img_item._rdbu_dark_lut = rdbu_dark_cm.getLookupTable()
            img_item.setLookupTable(viridis_cm.getLookupTable())  # Start with viridis
            
            # Add colorbar
            colorbar = pg.ColorBarItem(
                values=(0, 1),  # Will be updated with actual data range
                colorMap=viridis_cm,
                width=20,
                interactive=False
            )
            colorbar.setImageItem(img_item)
            graphics_layout.addItem(colorbar, row=0, col=1)
            
            self.lag_waterfall_plots[key] = plot
            self.lag_waterfall_images[key] = img_item
            self.lag_waterfall_colorbars[key] = colorbar
            self.lag_waterfall_levels[key] = None
            self.lag_waterfall_level_update_counter[key] = 0
            
            container_layout.addWidget(cb)
            container_layout.addWidget(graphics_layout)
            
            layout.addWidget(container, row, col)

    def _setup_fine_lag_spectra_plots(self) -> None:
        """Set up 2×2 grid of fine lag spectra plots with combo box selectors."""
        layout = QtWidgets.QGridLayout(self.fine_lag_spectra_tab)

        self.fine_lag_plots = {}
        self.fine_lag_plot_widgets = {}
        self.fine_lag_combos = {}
        self.fine_lag_plot_configs = {}
        colors = pg.colormap.get("tab10", "matplotlib").getColors()

        # Top control bar
        ctrl = QtWidgets.QHBoxLayout()
        self.fine_lag_unwrap_phase_checkbox = QtWidgets.QCheckBox("Unwrap Phase")
        self.fine_lag_unwrap_phase_checkbox.setChecked(False)

        self.fine_lag_config_button = QtWidgets.QPushButton("⚙ Configure")
        self.fine_lag_config_button.setToolTip("Open Fine Lag configuration dialog")
        self.fine_lag_config_button.clicked.connect(self._open_fine_lag_config)

        ctrl.addWidget(self.fine_lag_config_button)
        ctrl.addWidget(self.fine_lag_unwrap_phase_checkbox)
        ctrl.addStretch()
        layout.addLayout(ctrl, 0, 0, 1, 4)  # span all 4 columns

        n_spectra_plots = len(FINE_LAG_SPECTRA_DEFAULTS)  # 8 plots
        for idx in range(n_spectra_plots):
            row = idx // 4 + 1  # 4 columns, offset for control bar
            col = idx % 4

            container = QtWidgets.QWidget()
            clayout = QtWidgets.QVBoxLayout(container)

            combo = QtWidgets.QComboBox()
            combo.addItems(FINE_LAG_PLOT_OPTIONS)
            combo.setCurrentText(FINE_LAG_SPECTRA_DEFAULTS[idx])
            self.fine_lag_combos[idx] = combo

            plot = pg.PlotWidget()
            plot.showGrid(x=True, y=True)

            plot_line = plot.plot(pen=colors[idx % len(colors)])
            self.fine_lag_plots[idx] = plot_line
            self.fine_lag_plot_widgets[idx] = plot

            cb_key = f"fine_lag_plot_{idx}"
            cb = QtWidgets.QCheckBox(f"Plot {idx+1}")
            cb.setChecked(True)
            self.plot_checkboxes[cb_key] = cb
            cb.stateChanged.connect(lambda st, w=container: w.setVisible(st == QtCore.Qt.CheckState.Checked))

            h = QtWidgets.QHBoxLayout()
            h.addWidget(cb)
            h.addWidget(combo)
            h.addStretch()
            clayout.addLayout(h)
            clayout.addWidget(plot)

            layout.addWidget(container, row, col)

    # helper opened by the ⚙ button
    def _open_fine_lag_config(self):
        cfg = getattr(self, '_fine_lag_config', FineLagConfig())
        dlg = FineLagConfigDialog(cfg, parent=self)
        if dlg.exec_() == QtWidgets.QDialog.Accepted:
            self._fine_lag_config = dlg.get_config()
            if self.fine_lag_computer_thread:
                self.fine_lag_computer_thread.update_fine_lag_config(self._fine_lag_config)
            print(f"Fine lag config updated: zones={self._fine_lag_config.nyquist_zones}")

    def _setup_fine_lag_waterfall_plots(self) -> None:
        """Set up 2×2 grid of fine lag waterfall plots with combo box selectors."""
        layout = QtWidgets.QGridLayout(self.fine_lag_waterfall_tab)

        self.fine_lag_waterfall_plots = {}
        self.fine_lag_waterfall_images = {}
        self.fine_lag_waterfall_levels = {}
        self.fine_lag_waterfall_level_update_counter = {}
        self.fine_lag_waterfall_colorbars = {}
        self.fine_lag_waterfall_combos = {}
        self.fine_lag_waterfall_data = {}
        self.fine_lag_waterfall_prev = {}

        viridis_cm = pg.colormap.get('viridis')
        n_lags = 8192

        for idx in range(4):
            row, col = divmod(idx, 2)

            container = QtWidgets.QWidget()
            clayout = QtWidgets.QVBoxLayout(container)

            combo = QtWidgets.QComboBox()
            combo.addItems(FINE_LAG_WATERFALL_OPTIONS)
            combo.setCurrentText(FINE_LAG_WATERFALL_DEFAULTS[idx])
            # Reset waterfall history when selection changes
            combo.currentTextChanged.connect(lambda text, i=idx: self._reset_fine_lag_waterfall(i))
            self.fine_lag_waterfall_combos[idx] = combo

            graphics_layout = pg.GraphicsLayoutWidget()
            plot = graphics_layout.addPlot(row=0, col=0)
            plot.setLabel('left', 'Time')
            plot.setLabel('bottom', 'Distance', units='m')
            plot.invertY(True)

            img_item = pg.ImageItem()
            plot.addItem(img_item)
            img_item.setLookupTable(viridis_cm.getLookupTable())

            colorbar = pg.ColorBarItem(values=(0, 1), colorMap=viridis_cm, width=20, interactive=False)
            colorbar.setImageItem(img_item)
            graphics_layout.addItem(colorbar, row=0, col=1)

            key = f"fine_lag_wf_{idx}"
            self.fine_lag_waterfall_plots[key] = plot
            self.fine_lag_waterfall_images[key] = img_item
            self.fine_lag_waterfall_colorbars[key] = colorbar
            self.fine_lag_waterfall_levels[key] = None
            self.fine_lag_waterfall_level_update_counter[key] = 0

            # Per-slot waterfall history (reset on combo change)
            wf_len = getattr(self, 'config', CorrelatorConfig()).waterfall_len
            self.fine_lag_waterfall_data[idx] = np.zeros((wf_len, n_lags))
            self.fine_lag_waterfall_prev[idx] = np.zeros(n_lags)

            cb_key = f"fine_lag_wf_{idx}"
            cb = QtWidgets.QCheckBox(f"Waterfall {idx+1}")
            cb.setChecked(True)
            self.plot_checkboxes[cb_key] = cb
            cb.stateChanged.connect(lambda st, w=container: w.setVisible(st == QtCore.Qt.CheckState.Checked))

            h = QtWidgets.QHBoxLayout()
            h.addWidget(cb)
            h.addWidget(combo)
            h.addStretch()
            clayout.addLayout(h)
            clayout.addWidget(graphics_layout)

            layout.addWidget(container, row, col)

    def _reset_fine_lag_waterfall(self, slot_idx: int):
        """Clear waterfall history when combo box selection changes."""
        n_lags = 8192
        wf_len = self.shared_buffers.waterfall_len if self.shared_buffers else 250
        self.fine_lag_waterfall_data[slot_idx] = np.zeros((wf_len, n_lags))
        self.fine_lag_waterfall_prev[slot_idx] = np.zeros(n_lags)
        key = f"fine_lag_wf_{slot_idx}"
        self.fine_lag_waterfall_levels[key] = None

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
            import traceback
            tb_str = traceback.format_exc()
            print(f"Gain calibration failed: {e}")
            print("Full traceback:")
            print(tb_str)
            self.status_label.setText(f"Gain calibration failed: {e}")
            QtWidgets.QMessageBox.critical(self, "Calibration Error", f"{str(e)}\n\nSee console for full traceback")

    def configure_correlator(self) -> None:
        """Configure the correlator and start the data stream."""
        if not self.pocket_correlator: return
        try:
            self.status_label.setText("Configuring correlator...")
            QtWidgets.QApplication.processEvents()
            
            # Update configuration
            self.config.waterfall_len = self.waterfall_len_spinner.value()
            
            self.pocket_correlator.configure_corr(autocorr_only=0, no_accum=0, n_firmware_frames=self.config.n_firmware_frames - 1)
            
            # Configure the channelizer for correlator streaming
            # After calibrate_gains(), the SCALER is left bypassed, so we need to re-enable it
            print("Configuring channelizer for correlator streaming...")
            for n in range(8):  # All 8 channels
                # Ensure FFT is enabled (should already be from calibrate)
                if self.pocket_correlator.i.chan[n].FFT.BYPASS:
                    self.pocket_correlator.i.chan[n].FFT.BYPASS = 0
                
                # Enable SCALER (was bypassed during gain calibration)
                if self.pocket_correlator.i.chan[n].SCALER.BYPASS:
                    self.pocket_correlator.i.chan[n].SCALER.BYPASS = 0
                
                # Set SCALER to send 4-bit values to correlator
                self.pocket_correlator.i.chan[n].SCALER.DATA_TYPE = 0
                
                # Ensure data source is ADC (should already be set)
                self.pocket_correlator.i.chan[n].FUNCGEN.set_data_source('adc')
            
            if self.pocket_correlator.ucap.OUTPUT_SOURCE_SEL != 1:
                print("Setting UCAP output source to Correlator (1)...")
                self.pocket_correlator.ucap.OUTPUT_SOURCE_SEL = 1
            
            sock = self.pocket_correlator.corr_receiver.socket
            sock.settimeout(0.1)
            
            # Create shared memory buffers
            self.shared_buffers = SharedDataBuffers(waterfall_len=self.config.waterfall_len)
            
            # Set GUI reference and Nyquist zone in shared buffers
            self.shared_buffers.gui_instance = self
            self.shared_buffers.current_nyquist_zone = self.current_nyquist_zone
            
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
        elif zone == 4:
            # Zone 4: 4800 to 6400 MHz (reversed due to aliasing, like Zone 2)
            reversed_axis = base_freq_axis[::-1]
            return reversed_axis + 3 * FS_HZ / 2
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
            reversed_super_fft_axis = base_super_fft_freq_axis[::-1]
            self.super_fft_freq_axis = reversed_super_fft_axis + FS_HZ / 2
        elif zone == 3:
            self.super_fft_freq_axis = base_super_fft_freq_axis + FS_HZ
        elif zone == 4:
            reversed_super_fft_axis = base_super_fft_freq_axis[::-1]
            self.super_fft_freq_axis = reversed_super_fft_axis + 3 * FS_HZ / 2
        else:
            self.super_fft_freq_axis = base_super_fft_freq_axis
            
        # Update fine lag frequency axis with Nyquist zone
        n_fine_freq_bins = 8193
        base_fine_freq_axis = np.linspace(0, FS_HZ/2, n_fine_freq_bins, endpoint=True)
        
        if zone == 1:
            self.fine_freq_axis = base_fine_freq_axis
        elif zone == 2:
            reversed_fine_freq_axis = base_fine_freq_axis[::-1]
            self.fine_freq_axis = reversed_fine_freq_axis + FS_HZ / 2
        elif zone == 3:
            self.fine_freq_axis = base_fine_freq_axis + FS_HZ
        elif zone == 4:
            reversed_fine_freq_axis = base_fine_freq_axis[::-1]
            self.fine_freq_axis = reversed_fine_freq_axis + 3 * FS_HZ / 2
        else:
            self.fine_freq_axis = base_fine_freq_axis
    
    def _on_nyquist_zone_changed(self, index: int) -> None:
        """Handle Nyquist zone selection change."""
        zone = index + 1  # Convert 0-based index to 1-based zone number
        print(f"Switching to Nyquist Zone {zone}")
        
        # Update frequency axes
        self._update_all_frequency_axes(zone)
        
        # Update Nyquist zone in shared buffers if they exist
        if self.shared_buffers:
            self.shared_buffers.current_nyquist_zone = zone
        
        # Update lag frequency range spinner limits based on Nyquist zone
        zone_ranges = {
            1: (0.0, 1600.0),
            2: (1600.0, 3200.0),
            3: (3200.0, 4800.0),
            4: (4800.0, 6400.0),
        }
        min_freq, max_freq = zone_ranges[zone]
        
        # Block signals while updating to avoid triggering callbacks
        self.lag_freq_start_spinner.blockSignals(True)
        self.lag_freq_end_spinner.blockSignals(True)
        
        self.lag_freq_start_spinner.setRange(min_freq, max_freq)
        self.lag_freq_end_spinner.setRange(min_freq, max_freq)
        
        # Reset to full range for new zone
        self.lag_freq_start_spinner.setValue(min_freq)
        self.lag_freq_end_spinner.setValue(max_freq)
        
        # Unblock signals
        self.lag_freq_start_spinner.blockSignals(False)
        self.lag_freq_end_spinner.blockSignals(False)
        
        # Update config
        self.config.lag_freq_start_hz = min_freq * 1e6  # Convert MHz to Hz
        self.config.lag_freq_end_hz = max_freq * 1e6  # Convert MHz to Hz
        
        # Update status
        zone_range_text = {
            1: "0-1600 MHz",
            2: "1600-3200 MHz", 
            3: "3200-4800 MHz",
            4: "4800-6400 MHz",
        }
        self.status_label.setText(f"Nyquist Zone {zone} selected ({zone_range_text[zone]})")
        
        # Force plot updates by triggering a refresh
        self._update_plots()
    
    def _on_lag_freq_range_changed(self, value: float) -> None:
        """Handle lag frequency range spinner changes."""
        # Get current values in MHz
        start_mhz = self.lag_freq_start_spinner.value()
        end_mhz = self.lag_freq_end_spinner.value()
        
        # Validate that start < end
        if start_mhz >= end_mhz:
            # Adjust the other spinner to maintain valid range
            sender = self.sender()
            if sender == self.lag_freq_start_spinner:
                # User changed start, adjust end to be at least 10 MHz higher
                new_end = start_mhz + 10.0
                # Get the maximum allowed value
                max_allowed = self.lag_freq_end_spinner.maximum()
                if new_end <= max_allowed:
                    self.lag_freq_end_spinner.blockSignals(True)
                    self.lag_freq_end_spinner.setValue(new_end)
                    self.lag_freq_end_spinner.blockSignals(False)
                    end_mhz = new_end
                else:
                    # Can't adjust end, revert start
                    self.lag_freq_start_spinner.blockSignals(True)
                    self.lag_freq_start_spinner.setValue(end_mhz - 10.0)
                    self.lag_freq_start_spinner.blockSignals(False)
                    return
            else:
                # User changed end, adjust start to be at least 10 MHz lower
                new_start = end_mhz - 10.0
                # Get the minimum allowed value
                min_allowed = self.lag_freq_start_spinner.minimum()
                if new_start >= min_allowed:
                    self.lag_freq_start_spinner.blockSignals(True)
                    self.lag_freq_start_spinner.setValue(new_start)
                    self.lag_freq_start_spinner.blockSignals(False)
                    start_mhz = new_start
                else:
                    # Can't adjust start, revert end
                    self.lag_freq_end_spinner.blockSignals(True)
                    self.lag_freq_end_spinner.setValue(start_mhz + 10.0)
                    self.lag_freq_end_spinner.blockSignals(False)
                    return
        
        # Update config (convert MHz to Hz)
        self.config.lag_freq_start_hz = start_mhz * 1e6
        self.config.lag_freq_end_hz = end_mhz * 1e6
        
        # Calculate bandwidth and resolution
        bandwidth_mhz = end_mhz - start_mhz
        print(f"Lag frequency range set to: {start_mhz:.1f} - {end_mhz:.1f} MHz (BW: {bandwidth_mhz:.1f} MHz)")
    
    def _reset_lag_freq_range(self) -> None:
        """Reset lag frequency range to full band for current Nyquist zone."""
        zone = self.current_nyquist_zone
        zone_ranges = {
            1: (0.0, 1600.0),
            2: (1600.0, 3200.0),
            3: (3200.0, 4800.0),
            4: (4800.0, 6400.0),
        }
        min_freq, max_freq = zone_ranges[zone]
        
        # Block signals to avoid unnecessary callbacks
        self.lag_freq_start_spinner.blockSignals(True)
        self.lag_freq_end_spinner.blockSignals(True)
        
        self.lag_freq_start_spinner.setValue(min_freq)
        self.lag_freq_end_spinner.setValue(max_freq)
        
        self.lag_freq_start_spinner.blockSignals(False)
        self.lag_freq_end_spinner.blockSignals(False)
        
        # Update config
        self.config.lag_freq_start_hz = min_freq * 1e6
        self.config.lag_freq_end_hz = max_freq * 1e6
        
        print(f"Lag frequency range reset to full band: {min_freq:.1f} - {max_freq:.1f} MHz")
        self.status_label.setText("Lag range reset to full band")
    
    def _freq_to_correlator_bin_index(self, freq_hz: float, nyquist_zone: int) -> int:
        """Convert frequency in Hz to correlator bin index, accounting for Nyquist zone.
        
        Args:
            freq_hz: Frequency in Hz
            nyquist_zone: Nyquist zone number (1, 2, or 3)
            
        Returns:
            Bin index (0 to NBINS_TOTAL-1)
        """
        # For the correlator, bins are distributed across the Nyquist zones differently
        # The channelizer decimates by M_FACTOR, so each bin represents a narrower freq range
        freq_bin_width = FS_HZ / M_FACTOR  # ~195.3125 kHz per bin
        
        if nyquist_zone == 1:
            # Zone 1: 0 to 1600 MHz maps directly to bins 0-8191
            # freq_hz should be in range [0, FS_HZ/2]
            bin_idx = int((freq_hz / freq_bin_width))
        elif nyquist_zone == 2:
            # Zone 2: 1600 to 3200 MHz maps to bins 8191-0 (reversed due to aliasing)
            # First normalize to 0-1600 MHz range
            freq_offset = freq_hz - FS_HZ / 2
            # Then map to bins in reverse order
            bin_idx = NBINS_TOTAL - 1 - int((freq_offset / freq_bin_width))
        elif nyquist_zone == 3:
            # Zone 3: 3200 to 4800 MHz maps to bins 0-8191
            freq_offset = freq_hz - FS_HZ
            bin_idx = int((freq_offset / freq_bin_width))
        elif nyquist_zone == 4:
            freq_offset = freq_hz - 3 * FS_HZ / 2
            bin_idx = NBINS_TOTAL - 1 - int((freq_offset / freq_bin_width))
        else:
            bin_idx = int((freq_hz / freq_bin_width))
        
        # Clamp to valid range
        return np.clip(bin_idx, 0, NBINS_TOTAL - 1)
    
    def _freq_to_fine_lag_bin_index(self, freq_hz: float, nyquist_zone: int, n_fft_bins: int = 16384) -> int:
        """Convert frequency in Hz to fine lag FFT bin index, accounting for Nyquist zone.
        
        Args:
            freq_hz: Frequency in Hz  
            nyquist_zone: Nyquist zone number (1, 2, or 3)
            n_fft_bins: Number of FFT bins (16384 for fine lag mode)
            
        Returns:
            Bin index (0 to n_fft_bins-1)
        """
        # For fine lag mode (raw ADC), bins span full Nyquist bandwidth
        freq_bin_width = FS_HZ / n_fft_bins  # ~195.3125 kHz per bin for 16384-point FFT
        
        if nyquist_zone == 1:
            # Zone 1: 0 to 1600 MHz maps directly to bins 0-8191 (first half)
            bin_idx = int((freq_hz / freq_bin_width))
        elif nyquist_zone == 2:
            # Zone 2: 1600 to 3200 MHz maps to bins 8191-0 (reversed)
            freq_offset = freq_hz - FS_HZ / 2
            # Map to second half of FFT bins, reversed
            bin_idx = n_fft_bins - 1 - int((freq_offset / freq_bin_width))
        elif nyquist_zone == 3:
            # Zone 3: 3200 to 4800 MHz maps to bins 0-8191 (first half again)
            freq_offset = freq_hz - FS_HZ
            bin_idx = int((freq_offset / freq_bin_width))
        elif nyquist_zone == 4:
            freq_offset = freq_hz - 3 * FS_HZ / 2
            bin_idx = n_fft_bins - 1 - int((freq_offset / freq_bin_width))
        else:
            bin_idx = int((freq_hz / freq_bin_width))
        
        # Clamp to valid range
        return np.clip(bin_idx, 0, n_fft_bins - 1)

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
                self.shared_buffers.p01[:] = old_buffers.p01
            
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
    
    def _update_fine_lag_averaging(self, value: int) -> None:
        """Update the number of frames to average in fine lag mode."""
        if self.fine_lag_computer_thread:
            self.fine_lag_computer_thread.set_frames_to_average(value)
            print(f"Fine lag frames to average set to: {value}")
    
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
                # Set GUI reference and Nyquist zone
                self.shared_buffers.gui_instance = self
                self.shared_buffers.current_nyquist_zone = self.current_nyquist_zone
            
            # Create and start fine lag computer if needed
            if not self.fine_lag_computer_thread:
                self.fine_lag_computer_thread = FineLagComputer(self.pocket_correlator, self.shared_buffers, self.config)
                self.fine_lag_computer_thread.start()
                
                # Update spinner range based on ucap_mode
                max_frames = self.fine_lag_computer_thread.max_frames_per_channel
                self.fine_lag_avg_spinner.setRange(1, max_frames)
                self.fine_lag_avg_spinner.setValue(max_frames)  # Default to max for best SNR
                
                # Apply initial averaging setting
                self.fine_lag_computer_thread.set_frames_to_average(self.fine_lag_avg_spinner.value())
            
            self.fine_lag_computer_thread.set_enabled(True)
            
            # Enable the averaging spinner
            self.fine_lag_avg_spinner.setEnabled(True)
            
            # Update UI
            self.fine_lag_mode_button.setText("Disable Fine Lag Mode")
            ucap_mode = self.fine_lag_computer_thread.ucap_mode if self.fine_lag_computer_thread else 0
            mode_desc = {0: "2 frames", 1: "4 frames", 2: "8 frames"}
            self.status_label.setText(f"Fine Lag Mode Active - cm resolution, {mode_desc.get(ucap_mode, 'unknown')} max")
            
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
            
            # Disable the averaging spinner
            self.fine_lag_avg_spinner.setEnabled(False)
            
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
    
    def _on_running_diff_changed(self, state: int) -> None:
        """Handle running difference checkbox state change."""
        enabled = (state == QtCore.Qt.CheckState.Checked)
        self.diff_window_spinner.setEnabled(enabled)
        
        # Update shared buffer flag if it exists
        if self.shared_buffers:
            self.shared_buffers.running_diff_enabled = enabled
            print(f"Running difference mode {'enabled' if enabled else 'disabled'}")
    
    def _on_diff_window_changed(self, value: int) -> None:
        """Handle difference window size change."""
        # Update shared buffer flag if it exists
        if self.shared_buffers:
            self.shared_buffers.running_diff_window_size = value
            print(f"Running difference window size set to: {value}")

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
        """Update the line plots in the Spectra tab using dynamic product/component selection."""
        if not self.shared_buffers:
            return
        use_log_scale = self.log_scale_checkbox.isChecked()
        
        # Build a dictionary of all available products from shared buffers
        # This would need to be extended for full N^2 correlator support
        # For now, we only have specific products available: p00, p11, p01
        
        # Helper function to get data for a specific product and component
        def get_product_data(product, component):
            """Get the data for a specific product and component."""
            # For now, map to the available data
            # In a full implementation, this would index into the complete correlation matrix
            
            if product == (0, 0):
                # ADC0 auto-correlation
                data = self.shared_buffers.p00
                if component == 'magnitude':
                    return np.abs(data)
                elif component == 'real':
                    return data  # Auto-correlations are real
                elif component == 'imaginary':
                    return np.zeros_like(data)  # Auto-correlations have no imaginary part
                elif component == 'phase':
                    return np.zeros_like(data)  # Auto-correlations have zero phase
                    
            elif product == (1, 1):
                # ADC1 auto-correlation  
                data = self.shared_buffers.p11
                if component == 'magnitude':
                    return np.abs(data)
                elif component == 'real':
                    return data  # Auto-correlations are real
                elif component == 'imaginary':
                    return np.zeros_like(data)
                elif component == 'phase':
                    return np.zeros_like(data)
                    
            elif product == (0, 1) or product == (1, 0):
                # Cross-correlation - now stored as complex
                complex_data = self.shared_buffers.p01
                if component == 'magnitude':
                    return np.abs(complex_data)
                elif component == 'real':
                    return complex_data.real
                elif component == 'imaginary':
                    return complex_data.imag
                elif component == 'phase':
                    phase_data = np.angle(complex_data)
                    # Apply unwrapping if checkbox is checked
                    if self.unwrap_correlator_phase_checkbox.isChecked():
                        return np.unwrap(phase_data)
                    else:
                        return phase_data
                    
            else:
                # Product not available in current data
                return np.zeros(len(self.freq_axis))
        
        # Update each plot based on its configuration
        for plot_idx in range(4):
            # Get checkbox key based on plot index
            if plot_idx == 0:
                checkbox_key = "s_p00"
                plot_key = (0, 0)
            elif plot_idx == 1:
                checkbox_key = "s_p01_abs"
                plot_key = (0, 1, 'abs')
            elif plot_idx == 2:
                checkbox_key = "s_p01_real"
                plot_key = (0, 1, 'real')
            else:  # plot_idx == 3
                checkbox_key = "s_p11"
                plot_key = (1, 1)
            
            if not self.plot_checkboxes[checkbox_key].isChecked():
                continue
                
            # Get configuration for this plot
            config = self.plot_configs[plot_idx]
            product = config['product']
            component = config['component']
            
            # Get the data
            data = get_product_data(product, component)
            
            # Get the plot widget
            if plot_idx == 0:
                plot_widget = self.plot_widgets[(0, 0)]
                plot_line = self.plots[(0, 0)]
            elif plot_idx == 1:
                plot_widget = self.plot_widgets[(0, 1, 'abs')]
                plot_line = self.plots[(0, 1, 'abs')]
            elif plot_idx == 2:
                plot_widget = self.plot_widgets[(0, 1, 'real')]
                plot_line = self.plots[(0, 1, 'real')]
            else:  # plot_idx == 3
                plot_widget = self.plot_widgets[(1, 1)]
                plot_line = self.plots[(1, 1)]
            
            # Apply log scale based on data type
            is_auto_corr = (product[0] == product[1])
            
            if is_auto_corr or component == 'magnitude':
                # Auto-correlations and magnitudes use regular log scale
                plot_widget.setLogMode(y=use_log_scale)
                plot_line.setData(x=self.freq_axis, y=data)
                
                # Update y-axis label
                if component == 'magnitude':
                    plot_widget.setLabel('left', 'Magnitude')
                elif component == 'real':
                    plot_widget.setLabel('left', 'Power')
                    
            elif component in ['real', 'imaginary']:
                # Real/imaginary parts can use symlog
                plot_widget.setLogMode(y=False)  # Symlog is applied to data, not axes
                
                if use_log_scale:
                    # Apply symmetric log transformation
                    C = self.symlog_crossover_spinner.value()
                    sign_data = np.sign(data)
                    abs_data = np.abs(data)
                    symlog_data = sign_data * np.log10(1 + abs_data / C)
                    plot_line.setData(x=self.freq_axis, y=symlog_data)
                    plot_widget.setLabel('left', f'{component.capitalize()} (symlog)')
                else:
                    plot_line.setData(x=self.freq_axis, y=data)
                    plot_widget.setLabel('left', component.capitalize())
                    
            elif component == 'phase':
                # Phase doesn't use log scale
                plot_widget.setLogMode(y=False)
                plot_line.setData(x=self.freq_axis, y=data)
                # Update label based on unwrap status
                if self.unwrap_correlator_phase_checkbox.isChecked():
                    plot_widget.setLabel('left', 'Unwrapped Phase (radians)')
                else:
                    plot_widget.setLabel('left', 'Phase (radians)')

    def _update_waterfall_plots(self) -> None:
        """Update the waterfall plots with new data using dynamic product/component selection."""
        if not self.shared_buffers:
            return
        
        # Calculate frequency axis
        freq_min = self.freq_axis[0]
        freq_max = self.freq_axis[-1]
        
        # Helper function to get waterfall data for a specific product and component
        def get_waterfall_data(product_text, component):
            """Get the waterfall data for a specific product and component."""
            # Parse the product text to get the actual product
            if "Auto" in product_text:
                # Auto-correlation (e.g., "ADC0 Auto")
                adc_num = int(product_text.split("ADC")[1].split()[0])
                if adc_num == 0:
                    base_data = self.shared_buffers.wf_p00
                elif adc_num == 1:
                    base_data = self.shared_buffers.wf_p11
                else:
                    # For other ADCs, return zeros (not currently available)
                    return np.zeros((self.shared_buffers.waterfall_len, NBINS_TOTAL))
                    
                # Auto-correlations are real-valued
                if component == 'magnitude':
                    return np.abs(base_data)
                elif component == 'real':
                    return base_data
                elif component == 'imaginary':
                    return np.zeros_like(base_data)
                elif component == 'phase':
                    return np.zeros_like(base_data)
            else:
                # Cross-correlation (e.g., "ADC0/1 Cross")
                parts = product_text.split("/")
                adc1 = int(parts[0].split("ADC")[1])
                adc2 = int(parts[1].split()[0])
                
                # For now, only ADC0/1 cross-correlation is available
                if (adc1, adc2) == (0, 1) or (adc1, adc2) == (1, 0):
                    complex_wf = self.shared_buffers.wf_p01
                    if component == 'magnitude':
                        return np.abs(complex_wf)
                    elif component == 'real':
                        return complex_wf.real
                    elif component == 'imaginary':
                        return complex_wf.imag
                    elif component == 'phase':
                        # Apply unwrapping per time slice if checkbox is checked
                        phase_wf = np.angle(complex_wf)
                        if self.unwrap_correlator_phase_checkbox.isChecked():
                            # Unwrap each row (time slice) independently
                            unwrapped_phase_wf = np.zeros_like(phase_wf)
                            for i in range(phase_wf.shape[0]):
                                unwrapped_phase_wf[i] = np.unwrap(phase_wf[i])
                            return unwrapped_phase_wf
                        else:
                            return phase_wf
                else:
                    # Other cross-correlations not available yet
                    return np.zeros((self.shared_buffers.waterfall_len, NBINS_TOTAL))
            
            # Fallback
            return np.zeros((self.shared_buffers.waterfall_len, NBINS_TOTAL))
        
        # Update each waterfall plot based on its configuration
        for waterfall_idx in range(4):
            key = f"wf_{waterfall_idx}"
            
            if not self.plot_checkboxes[key].isChecked():
                continue
                
            # Get configuration for this waterfall
            config = self.waterfall_configs[waterfall_idx]
            product_text = config['product_text']
            component = config['component']
            
            # Get the data
            wf_data = get_waterfall_data(product_text, component)
            
            # Determine if this is magnitude data for scaling purposes
            is_magnitude = (component == 'magnitude')
            
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
            
            # Update levels every N frames or if not set, but only if color scale is not fixed
            update_levels = (
                self.waterfall_levels[key] is None or 
                (not self.fix_color_scale_checkbox.isChecked() and 
                 self.waterfall_level_update_counter[key] >= scale_update_interval)
            )
            
            if update_levels:
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
                elif component == 'phase':
                    # Phase data ranges from -pi to pi
                    vmin, vmax = -np.pi, np.pi
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
            
            if use_log_scale and component != 'phase':
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
                # Linear scale (or phase data which should always use linear scale)
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
        def update_lag_waterfall(key, raw_data, is_magnitude=True, positive_only=False):
            """Update a single lag waterfall plot with proper scaling and levels."""
            plot_key = f"lag_wf_{key}"
            if not self.plot_checkboxes[plot_key].isChecked():
                return
            
            # Choose which buffer to display based on running difference mode (like fine lag)
            if self.running_diff_checkbox.isChecked():
                # Use the pre-calculated difference buffer
                if key == 'lag_p00':
                    data = self.shared_buffers.lag_diff_wf_p00
                elif key == 'lag_p11':
                    data = self.shared_buffers.lag_diff_wf_p11
                else:  # lag_p01_mag
                    data = self.shared_buffers.lag_diff_wf_p01
            else:
                # Use the raw data buffer
                data = raw_data
                
            wf_data = get_ordered_waterfall(data, pos)
            
            # Apply mean subtraction only if enabled AND not in running difference mode
            if self.mean_subtract_checkbox.isChecked() and not self.running_diff_checkbox.isChecked():
                # Original mean subtraction mode
                frames_written = self.shared_buffers.lag_frames_written
                if frames_written > 0:
                    populated_data = wf_data[-frames_written:] if frames_written < self.shared_buffers.waterfall_len else wf_data
                    bin_means = np.mean(populated_data, axis=0, keepdims=True)
                    wf_data = wf_data - bin_means
            
            # Update level counter
            self.lag_waterfall_level_update_counter[key] += 1
            
            # Get the scale update interval from the spinner
            scale_update_interval = self.scale_update_spinner.value()
            
            # Update levels every N frames or if not set, but only if color scale is not fixed
            update_levels = (
                self.lag_waterfall_levels[key] is None or 
                (not self.fix_color_scale_checkbox.isChecked() and 
                 self.lag_waterfall_level_update_counter[key] >= scale_update_interval)
            )
            
            if update_levels:
                # Calculate robust levels using percentiles
                valid_data = wf_data[np.isfinite(wf_data)]
                use_log_scale = self.log_scale_checkbox.isChecked()  # Define the variable
                
                if is_magnitude:
                    # For magnitude data - use full range like fine lag waterfall for sharp peaks
                    if len(valid_data) > 0:
                        vmin = min(valid_data)  # Use full range instead of percentiles
                        vmax = max(valid_data)  # This preserves sharp peak definition
                        
                        # For normalized data, ensure we don't go below 0 for log scale
                        if use_log_scale:
                            vmin = max(vmin, 1e-6)  # Prevent log of negative/zero
                            vmax = max(vmax, vmin * 10)  # Ensure reasonable dynamic range
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
            colorbar = self.lag_waterfall_colorbars[key]
            use_log_scale = self.log_scale_checkbox.isChecked()
            
            # Switch colormap based on mode
            if self.running_diff_checkbox.isChecked() or self.mean_subtract_checkbox.isChecked():
                # Use diverging colormap for difference data
                if self.dark_mode_colors_checkbox.isChecked():
                    # Dark mode: use CET-D4 (black at zero)
                    rdbu_cm = pg.colormap.get('CET-D4')
                else:
                    # Light mode: use CET-D1 (white at zero)
                    rdbu_cm = pg.colormap.get('CET-D1')
                
                img.setLookupTable(rdbu_cm.getLookupTable())
                colorbar.setColorMap(rdbu_cm)
            else:
                # Use viridis for normal data
                viridis_cm = pg.colormap.get('viridis')
                img.setLookupTable(viridis_cm.getLookupTable())
                colorbar.setColorMap(viridis_cm)
            
            if use_log_scale and is_magnitude and not (self.running_diff_checkbox.isChecked() or self.mean_subtract_checkbox.isChecked()):
                # For power data, display on log scale
                # Don't use log scale for difference data as it can be negative
                epsilon = 1e-10
                clipped_data = np.maximum(wf_data, epsilon)
                log_wf_data = np.log10(clipped_data)
                vmin, vmax = self.lag_waterfall_levels[key]
                # Also clip the levels to avoid log of negative values
                vmin_clipped = max(vmin, epsilon)
                vmax_clipped = max(vmax, epsilon)
                log_levels = (np.log10(vmin_clipped), np.log10(vmax_clipped))
                img.setImage(log_wf_data.T, autoLevels=False, levels=log_levels)
                # Update colorbar with log scale values
                colorbar.setLevels((np.log10(vmin_clipped), np.log10(vmax_clipped)))
            else:
                # Linear scale (or difference mode which should always be linear)
                img.setImage(wf_data.T, autoLevels=False, levels=self.lag_waterfall_levels[key])
                # Update colorbar with actual data range
                vmin, vmax = self.lag_waterfall_levels[key]
                colorbar.setLevels((vmin, vmax))
            
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

    def _get_fine_lag_product(self, result: dict, selection: str):
        """Parse combo box text and return the corresponding data array from the result dict."""
        if result is None:
            return None
        try:
            if selection.startswith("TOD: ADC "):
                ch = int(selection.split()[-1])
                return result['tods'][ch].ravel()
            elif selection.startswith("FFT: ADC "):
                ch = int(selection.split()[-1])
                return result['ffts'][ch]
            elif selection.startswith("Lag: p"):
                pstr = selection.split()[1]  # "p01" or "p00"
                i, j = int(pstr[1]), int(pstr[2])
                return result['lags'][(i, j)]
        except (KeyError, IndexError, ValueError):
            pass
        return None

    def _update_fine_lag_spectra_plots(self) -> None:
        """Update the 2×2 fine lag spectra plots from the new result dict."""
        if not self.shared_buffers:
            return
        result = self.shared_buffers.fine_lag_result
        if result is None:
            return

        n_plots = len(self.fine_lag_combos)
        for idx in range(n_plots):
            cb_key = f"fine_lag_plot_{idx}"
            if cb_key not in self.plot_checkboxes or not self.plot_checkboxes[cb_key].isChecked():
                continue

            selection = self.fine_lag_combos[idx].currentText()
            data = self._get_fine_lag_product(result, selection)
            if data is None:
                continue

            plot_widget = self.fine_lag_plot_widgets[idx]
            plot_line = self.fine_lag_plots[idx]

            # Choose x-axis based on product type
            if selection.startswith("TOD:"):
                x = self.time_axis_us_full[:len(data)]
                plot_widget.setLabel('bottom', 'Time', units='μs')
                plot_widget.setLabel('left', 'Amplitude')
                plot_widget.setLogMode(y=False)
            elif selection.startswith("FFT:"):
                n = len(data)
                x = np.linspace(0, FS_HZ / 2, n)
                plot_widget.setLabel('bottom', 'Frequency', units='Hz')
                plot_widget.setLabel('left', 'Power')
                plot_widget.setLogMode(y=self.log_scale_checkbox.isChecked())
            elif selection.startswith("Lag:"):
                x = self.fine_lag_axis if len(data) == len(self.fine_lag_axis) else np.arange(len(data))
                plot_widget.setLabel('bottom', 'Distance', units='m')
                plot_widget.setLabel('left', 'Normalized Correlation')
                plot_widget.setLogMode(y=self.log_scale_checkbox.isChecked())
            else:
                x = np.arange(len(data))

            plot_line.setData(x=x, y=data)

    def _update_fine_lag_waterfall_plots(self) -> None:
        """Update the 2×2 fine lag waterfall plots from the new result dict."""
        if not self.shared_buffers:
            return
        result = self.shared_buffers.fine_lag_result
        if result is None:
            return

        fine_lag_min = self.fine_lag_axis[0]
        fine_lag_max = self.fine_lag_axis[-1]
        use_log = self.log_scale_checkbox.isChecked()
        is_diff = self.running_diff_checkbox.isChecked()
        viridis_cm = pg.colormap.get('viridis')

        for idx in range(4):
            cb_key = f"fine_lag_wf_{idx}"
            if not self.plot_checkboxes[cb_key].isChecked():
                continue

            selection = self.fine_lag_waterfall_combos[idx].currentText()
            data = self._get_fine_lag_product(result, selection)
            if data is None:
                continue

            # Shift waterfall and append new row (truncate/pad to waterfall width)
            wf = self.fine_lag_waterfall_data[idx]
            n_wf_bins = wf.shape[1]
            row = data[:n_wf_bins] if len(data) >= n_wf_bins else np.pad(data, (0, n_wf_bins - len(data)))
            wf[1:] = wf[:-1]
            if is_diff:
                prev = self.fine_lag_waterfall_prev[idx]
                wf[0] = row - prev[:n_wf_bins]
                self.fine_lag_waterfall_prev[idx][:n_wf_bins] = row
            else:
                wf[0] = row

            key = f"fine_lag_wf_{idx}"
            img = self.fine_lag_waterfall_images[key]
            colorbar = self.fine_lag_waterfall_colorbars[key]

            # Update levels periodically
            self.fine_lag_waterfall_level_update_counter[key] = self.fine_lag_waterfall_level_update_counter.get(key, 0) + 1
            scale_interval = self.scale_update_spinner.value()
            update_lvl = (
                self.fine_lag_waterfall_levels[key] is None or
                (not self.fix_color_scale_checkbox.isChecked() and
                 self.fine_lag_waterfall_level_update_counter[key] >= scale_interval)
            )
            if update_lvl:
                self.fine_lag_waterfall_level_update_counter[key] = 0
                valid = wf[np.isfinite(wf)]
                if len(valid) > 0:
                    vmin, vmax = float(np.min(valid)), float(np.max(valid))
                    if use_log and not is_diff:
                        vmin = max(vmin, 1e-6)
                        vmax = max(vmax, vmin * 10)
                else:
                    vmin, vmax = 1e-6, 1.0
                self.fine_lag_waterfall_levels[key] = (vmin, vmax)

            vmin, vmax = self.fine_lag_waterfall_levels[key]

            if use_log and not is_diff:
                eps = 1e-10
                log_wf = np.log10(np.maximum(wf, eps))
                log_lvl = (np.log10(max(vmin, eps)), np.log10(max(vmax, eps)))
                img.setImage(log_wf.T, autoLevels=False, levels=log_lvl)
                colorbar.setLevels(log_lvl)
            else:
                img.setImage(wf.T, autoLevels=False, levels=(vmin, vmax))
                colorbar.setLevels((vmin, vmax))

            # Set axis rect once
            if not hasattr(img, '_scale_set'):
                n_bins = wf.shape[1]
                wf_len = wf.shape[0]
                # For FFT products, use freq axis; for lag products, use lag axis
                if selection.startswith("FFT:"):
                    x_min, x_max = 0, FS_HZ / 2
                    plot = self.fine_lag_waterfall_plots[key]
                    plot.setLabel('bottom', 'Frequency', units='Hz')
                else:
                    x_min, x_max = fine_lag_min, fine_lag_max
                    plot = self.fine_lag_waterfall_plots[key]
                    plot.setLabel('bottom', 'Distance', units='m')
                img.setRect(QtCore.QRectF(x_min, 0, x_max - x_min, wf_len))
                plot.setLabel('left', 'Time (frames ago)')
                img._scale_set = True

            # Colormap switching for diff mode
            if is_diff:
                if self.dark_mode_colors_checkbox.isChecked():
                    cm = pg.colormap.get('CET-D4')
                else:
                    cm = pg.colormap.get('CET-D1')
                img.setLookupTable(cm.getLookupTable())
                colorbar.setColorMap(cm)
            else:
                img.setLookupTable(viridis_cm.getLookupTable())
                colorbar.setColorMap(viridis_cm)

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
        
        # Update levels every N frames or if not set, but only if color scale is not fixed
        update_levels = (
            self.super_fft_waterfall_levels[key] is None or 
            (not self.fix_color_scale_checkbox.isChecked() and 
             self.super_fft_waterfall_level_update_counter[key] >= scale_update_interval)
        )
        
        if update_levels:
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

    def _toggle_archiving(self, state: int) -> None:
        """Toggle archiving on/off."""
        if not DATA_ARCHIVER_AVAILABLE:
            self.archive_checkbox.setChecked(False)
            QtWidgets.QMessageBox.warning(self, "Archiving Not Available", 
                "h5py is not installed. Please install h5py to enable archiving.")
            return
            
        enabled = (state == QtCore.Qt.CheckState.Checked)
        
        if enabled:
            # Start archiving
            current_mode = self._get_current_mode()
            if current_mode:
                if not self.shared_buffers.archiver:
                    self.shared_buffers.archiver = DataArchiver(
                        base_path=self.shared_buffers.archive_path
                    )
                
                config = {
                    'fs_hz': FS_HZ,
                    'frames_to_average': self.config.frames_to_average,
                    'x01': 1.0,  # Default antenna spacing
                    'x02': 1.0,
                    'threshold': self.radar_threshold_spinner.value()
                }
                
                if self.shared_buffers.archiver.start_session(current_mode, config):
                    self.shared_buffers.archiving_enabled = True
                    self.archive_status_label.setText("Recording")
                    self.archive_status_label.setStyleSheet("color: #FF6B6B;")  # Red
                    print(f"Started archiving in {current_mode} mode")
                else:
                    self.archive_checkbox.setChecked(False)
                    QtWidgets.QMessageBox.critical(self, "Archiving Error", 
                        "Failed to start archiving session.")
            else:
                self.archive_checkbox.setChecked(False)
                QtWidgets.QMessageBox.warning(self, "No Active Mode", 
                    "Please enable a capture mode (Fine Lag, Super FFT, or Radar) before archiving.")
        else:
            # Stop archiving
            if self.shared_buffers and self.shared_buffers.archiver:
                self.shared_buffers.archiver.stop_session()
                self.shared_buffers.archiving_enabled = False
                self.archive_status_label.setText("Not Recording")
                self.archive_status_label.setStyleSheet("color: gray;")
                print("Stopped archiving")

    def _select_archive_path(self) -> None:
        """Open dialog to select archive directory."""
        current_path = self.shared_buffers.archive_path if self.shared_buffers else "./archives"
        
        directory = QtWidgets.QFileDialog.getExistingDirectory(
            self, 
            "Select Archive Directory",
            current_path
        )
        
        if directory:
            if self.shared_buffers:
                self.shared_buffers.archive_path = directory
            self.archive_path_button.setText(f"...{directory[-20:]}")  # Show last 20 chars
            print(f"Archive path set to: {directory}")

    def _get_current_mode(self) -> Optional[str]:
        """Get the currently active mode for archiving."""
        if self.fine_lag_mode_button.isChecked():
            return 'fine_lag'
        elif self.super_fft_mode_button.isChecked():
            return 'super_fft'
        elif self.radar_mode_button.isChecked():
            return 'radar'
        else:
            return None

    def _update_archive_status(self) -> None:
        """Update archive status display."""
        if not self.shared_buffers or not self.shared_buffers.archiver:
            return
            
        status = self.shared_buffers.archiver.get_status()
        
        if status['recording']:
            self.archive_status_label.setText(
                f"Recording: {status['file_size_mb']:.1f} MB | "
                f"{status['frames_archived']} frames"
            )
            self.archive_status_label.setStyleSheet("color: #FF6B6B;")  # Red
        else:
            self.archive_status_label.setText("Archive Ready")
            self.archive_status_label.setStyleSheet("color: #90EE90;")  # Green

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        """Handle the main window close event."""
        # Stop archiving if active
        if self.shared_buffers and self.shared_buffers.archiver:
            self.shared_buffers.archiver.stop_session()
        
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
