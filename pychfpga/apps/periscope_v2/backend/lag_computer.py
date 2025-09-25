#!/usr/bin/env python
"""
Optimized Lag Computer for Periscope Correlator V2
===============================================

High-performance lag spectrum computation using cached FFTs and optimized operations.
"""
import numpy as np
import time
import threading
from typing import Optional, Callable

# Try to import pyFFTW for maximum performance
try:
    import pyfftw
    PYFFTW_AVAILABLE = True
    print("pyFFTW available - using cached FFT plans for maximum performance")
except ImportError:
    PYFFTW_AVAILABLE = False
    print("pyFFTW not available - using NumPy FFT")

# Constants
NBINS_TOTAL = 8192
FS_HZ = 3_200_000_000.0

class LagData:
    """Container for lag spectrum data."""
    def __init__(self, lag_p00, lag_p11, lag_p01_mag, lag_p01_phase):
        self.lag_p00 = lag_p00
        self.lag_p11 = lag_p11
        self.lag_p01_mag = lag_p01_mag
        self.lag_p01_phase = lag_p01_phase
        self.timestamp = time.time()
        self.valid = True

class OptimizedLagComputer(threading.Thread):
    """High-performance lag spectrum computer with cached FFT plans."""
    
    def __init__(self, data_buffer):
        """Initialize optimized lag computer."""
        super().__init__(daemon=True, name="LagComputer")
        
        self.data_buffer = data_buffer
        
        # Pre-allocate work buffers for efficiency
        self.vis_p00_buffer = np.zeros(NBINS_TOTAL, dtype=np.float64)
        self.vis_p11_buffer = np.zeros(NBINS_TOTAL, dtype=np.float64)
        self.vis_p01_complex_buffer = np.zeros(NBINS_TOTAL, dtype=np.complex128)
        
        # Pre-allocate output buffers
        self.lag_p00_complex_buffer = np.zeros(NBINS_TOTAL, dtype=np.complex128)
        self.lag_p11_complex_buffer = np.zeros(NBINS_TOTAL, dtype=np.complex128)
        self.lag_p01_complex_output = np.zeros(NBINS_TOTAL, dtype=np.complex128)
        
        # Setup FFT engines for maximum performance
        self._setup_fft_engines()
        
        # Performance tracking
        self.computations = 0
        self.compute_time_total = 0.0
        self.fps = 0.0
        self.start_time = time.time()
        self.last_fps_time = time.time()
        
        # Control
        self.running = False
        self.enabled = threading.Event()
        self.compute_interval = 1  # Compute every N frames
        self.frames_since_compute = 0
        
        # Callbacks
        self.lag_callback: Optional[Callable] = None
        
        # Input synchronization
        self.new_data_condition = threading.Condition()
        
    def _setup_fft_engines(self):
        """Setup optimized FFT engines with caching."""
        if PYFFTW_AVAILABLE:
            self._setup_pyfftw_engines()
        else:
            self._setup_numpy_fft()
    
    def _setup_pyfftw_engines(self):
        """Setup pyFFTW engines for maximum performance with safe error handling."""
        try:
            # Configure pyFFTW for optimal performance
            pyfftw.config.NUM_THREADS = 1  # Single thread for deterministic performance
            pyfftw.config.PLANNER_EFFORT = 'FFTW_MEASURE'  # Good balance of planning time vs speed
            
            print("Setting up pyFFTW cached FFT plans...")
            
            # Convert all input buffers to complex to avoid type mismatch issues
            self.vis_p00_complex = self.vis_p00_buffer.astype(np.complex128)
            self.vis_p11_complex = self.vis_p11_buffer.astype(np.complex128)
            
            # Create IFFT plans (complex input -> complex output for consistency)
            self.ifft_p00 = pyfftw.FFTW(
                self.vis_p00_complex, 
                self.lag_p00_complex_buffer,
                direction='FFTW_BACKWARD',
                flags=('FFTW_MEASURE',),
                threads=1
            )
            
            self.ifft_p11 = pyfftw.FFTW(
                self.vis_p11_complex,
                self.lag_p11_complex_buffer, 
                direction='FFTW_BACKWARD',
                flags=('FFTW_MEASURE',),
                threads=1
            )
            
            # IFFT for cross-correlation (complex input -> complex output)
            self.ifft_p01 = pyfftw.FFTW(
                self.vis_p01_complex_buffer,
                self.lag_p01_complex_output,
                direction='FFTW_BACKWARD',
                flags=('FFTW_MEASURE',),
                threads=1
            )
            
            print("pyFFTW cached FFT plans initialized successfully")
            
        except Exception as e:
            print(f"pyFFTW setup failed: {e}")
            print("Falling back to NumPy FFT")
            # Set global flag to disable pyFFTW for this instance
            global PYFFTW_AVAILABLE
            PYFFTW_AVAILABLE = False
            self._setup_numpy_fft()
    
    def _setup_numpy_fft(self):
        """Setup NumPy FFT as fallback."""
        # No special setup needed for NumPy FFT
        self.ifft_p00 = None
        self.ifft_p11 = None  
        self.ifft_p01 = None
        print("Using NumPy FFT (fallback)")
    
    def set_enabled(self, enabled: bool):
        """Enable or disable lag computation."""
        if enabled:
            self.enabled.set()
        else:
            self.enabled.clear()
    
    def set_lag_callback(self, callback: Callable):
        """Set callback for completed lag data."""
        self.lag_callback = callback
    
    def set_compute_interval(self, interval: int):
        """Set computation interval (compute every N frames)."""
        self.compute_interval = max(1, interval)
    
    def notify_new_data(self):
        """Notify that new frame data is available."""
        with self.new_data_condition:
            self.new_data_condition.notify()
    
    def run(self):
        """Main computation loop."""
        self.running = True
        print("Optimized lag computer started with cached FFT plans")
        
        while self.running:
            # Wait for enable signal
            if not self.enabled.wait(timeout=0.1):
                continue
            
            # Wait for new data
            with self.new_data_condition:
                self.new_data_condition.wait(timeout=0.1)
            
            # Check if we should compute
            self.frames_since_compute += 1
            if self.frames_since_compute >= self.compute_interval:
                self._compute_lag_spectra()
                self.frames_since_compute = 0
        
        print("Lag computer stopped")
    
    def _compute_lag_spectra(self):
        """Compute lag spectra using cached FFT plans for maximum speed."""
        start_time = time.perf_counter()
        
        try:
            # Get current frame data from buffer
            current_frame = self.data_buffer.get_current_frame()
            if not current_frame or not current_frame.valid:
                return
            
            # Copy visibility data to work buffers
            np.copyto(self.vis_p00_buffer, current_frame.p00)
            np.copyto(self.vis_p11_buffer, current_frame.p11)
            
            self.vis_p01_complex_buffer.real = current_frame.p01_real
            self.vis_p01_complex_buffer.imag = current_frame.p01_imag
            
            # Compute inverse FFTs using cached plans or NumPy fallback
            if PYFFTW_AVAILABLE and hasattr(self, 'ifft_p00') and self.ifft_p00 is not None:
                # Use pre-computed FFT plans for maximum speed
                # Copy real data to complex buffers for pyFFTW
                np.copyto(self.vis_p00_complex, self.vis_p00_buffer)
                np.copyto(self.vis_p11_complex, self.vis_p11_buffer)
                
                self.ifft_p00()  # Execute cached plan
                self.ifft_p11()  # Execute cached plan
                self.ifft_p01()  # Execute cached plan
                
                # Extract results
                lag_p00_complex = self.lag_p00_complex_buffer
                lag_p11_complex = self.lag_p11_complex_buffer
                lag_p01_complex = self.lag_p01_complex_output
            else:
                # Fallback to NumPy FFT
                lag_p00_complex = np.fft.ifft(self.vis_p00_buffer)
                lag_p11_complex = np.fft.ifft(self.vis_p11_buffer)
                lag_p01_complex = np.fft.ifft(self.vis_p01_complex_buffer)
            
            # Power spectrum for autocorrelations
            lag_p00 = np.abs(lag_p00_complex)**2
            lag_p11 = np.abs(lag_p11_complex)**2
            
            # Magnitude and phase for cross-correlation
            lag_p01_mag = np.abs(lag_p01_complex)
            lag_p01_phase = np.angle(lag_p01_complex)
            
            # FFT shift to center zero lag
            lag_p00_shifted = np.fft.fftshift(lag_p00)
            lag_p11_shifted = np.fft.fftshift(lag_p11)
            lag_p01_mag_shifted = np.fft.fftshift(lag_p01_mag)
            lag_p01_phase_shifted = np.fft.fftshift(lag_p01_phase)
            
            # Create lag data object
            lag_data = LagData(lag_p00_shifted, lag_p11_shifted, lag_p01_mag_shifted, lag_p01_phase_shifted)
            
            # Update data buffer
            self.data_buffer.update_lag(lag_data)
            
            # Call callback if set
            if self.lag_callback:
                self.lag_callback(lag_data)
            
            # Update performance metrics
            self.computations += 1
            self.compute_time_total += time.perf_counter() - start_time
            
            current_time = time.time()
            if current_time - self.last_fps_time >= 1.0:
                self.fps = self.computations / (current_time - self.start_time)
                self.last_fps_time = current_time
            
        except Exception as e:
            print(f"Error in lag computation: {e}")
    
    def stop(self):
        """Stop the lag computer."""
        self.running = False
        self.enabled.set()  # Wake up thread
        with self.new_data_condition:
            self.new_data_condition.notify()
    
    def get_stats(self):
        """Get performance statistics."""
        avg_time = self.compute_time_total / max(self.computations, 1)
        return {
            'computations': self.computations,
            'fps': self.fps,
            'avg_compute_time_ms': avg_time * 1000,
            'compute_rate_per_sec': 1.0 / max(avg_time, 1e-9),
            'using_pyfftw': PYFFTW_AVAILABLE
        }
