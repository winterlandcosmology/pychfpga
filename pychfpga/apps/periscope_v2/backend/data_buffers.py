#!/usr/bin/env python
"""
Data Buffer Management for Periscope Correlator V2
===============================================

Efficient data storage and management for real-time correlator data.
"""
import numpy as np
import time
import threading
from typing import Optional

# Constants
NBINS_TOTAL = 8192
DEFAULT_WATERFALL_LEN = 250

class DataBuffer:
    """
    High-performance data buffer for correlator frame and lag data.
    
    Uses efficient numpy operations and minimal locking for real-time performance.
    """
    
    def __init__(self, waterfall_len: int = DEFAULT_WATERFALL_LEN):
        """Initialize data buffer."""
        self.waterfall_len = waterfall_len
        
        # Current frame data
        self.current_p00 = np.zeros(NBINS_TOTAL, dtype=np.float64)
        self.current_p11 = np.zeros(NBINS_TOTAL, dtype=np.float64)
        self.current_p01_real = np.zeros(NBINS_TOTAL, dtype=np.float64)
        self.current_p01_imag = np.zeros(NBINS_TOTAL, dtype=np.float64)
        
        # Waterfall storage for correlator data
        self.wf_p00 = np.zeros((waterfall_len, NBINS_TOTAL), dtype=np.float64)
        self.wf_p11 = np.zeros((waterfall_len, NBINS_TOTAL), dtype=np.float64)
        self.wf_p01_real = np.zeros((waterfall_len, NBINS_TOTAL), dtype=np.float64)
        self.wf_p01_imag = np.zeros((waterfall_len, NBINS_TOTAL), dtype=np.float64)
        
        # Current lag data
        self.current_lag_p00 = np.zeros(NBINS_TOTAL, dtype=np.float64)
        self.current_lag_p11 = np.zeros(NBINS_TOTAL, dtype=np.float64)
        self.current_lag_p01_mag = np.zeros(NBINS_TOTAL, dtype=np.float64)
        self.current_lag_p01_phase = np.zeros(NBINS_TOTAL, dtype=np.float64)
        
        # Waterfall storage for lag data
        self.lag_wf_p00 = np.zeros((waterfall_len, NBINS_TOTAL), dtype=np.float64)
        self.lag_wf_p11 = np.zeros((waterfall_len, NBINS_TOTAL), dtype=np.float64)
        self.lag_wf_p01_mag = np.zeros((waterfall_len, NBINS_TOTAL), dtype=np.float64)
        self.lag_wf_p01_phase = np.zeros((waterfall_len, NBINS_TOTAL), dtype=np.float64)
        
        # Buffer management
        self.frames_written = 0
        self.lag_frames_written = 0
        self.frame_number = 0
        
        # Thread safety
        self.lock = threading.RLock()
        
        # Performance tracking
        self.total_frames = 0
        self.start_time = time.time()
    
    def update_frame(self, frame_data):
        """Update current frame and waterfall data with O(1) ring buffer."""
        with self.lock:
            # Update current frame
            np.copyto(self.current_p00, frame_data.p00)
            np.copyto(self.current_p11, frame_data.p11)
            np.copyto(self.current_p01_real, frame_data.p01_real)
            np.copyto(self.current_p01_imag, frame_data.p01_imag)
            
            # Use ring buffer approach - O(1) update instead of O(n) array shifting
            # Calculate write position in circular buffer
            write_pos = self.frame_number % self.waterfall_len
            
            # Update waterfall at write position (newest data)
            self.wf_p00[write_pos] = frame_data.p00
            self.wf_p11[write_pos] = frame_data.p11
            self.wf_p01_real[write_pos] = frame_data.p01_real
            self.wf_p01_imag[write_pos] = frame_data.p01_imag
            
            # Update counters
            self.frames_written = min(self.frames_written + 1, self.waterfall_len)
            self.frame_number += 1
            self.total_frames += 1
    
    def update_lag(self, lag_data):
        """Update current lag and lag waterfall data with O(1) ring buffer."""
        with self.lock:
            # Update current lag data
            np.copyto(self.current_lag_p00, lag_data.lag_p00)
            np.copyto(self.current_lag_p11, lag_data.lag_p11)
            np.copyto(self.current_lag_p01_mag, lag_data.lag_p01_mag)
            np.copyto(self.current_lag_p01_phase, lag_data.lag_p01_phase)
            
            # Use ring buffer approach for lag waterfall too - O(1) update
            lag_write_pos = self.lag_frames_written % self.waterfall_len
            
            # Update lag waterfall at write position
            self.lag_wf_p00[lag_write_pos] = lag_data.lag_p00
            self.lag_wf_p11[lag_write_pos] = lag_data.lag_p11
            self.lag_wf_p01_mag[lag_write_pos] = lag_data.lag_p01_mag
            self.lag_wf_p01_phase[lag_write_pos] = lag_data.lag_p01_phase
            
            # Update counter
            self.lag_frames_written += 1
    
    def get_current_frame(self):
        """Get current frame data."""
        from .udp_processor import FrameData  # Import here to avoid circular imports
        
        with self.lock:
            return FrameData(
                self.current_p00.copy(),
                self.current_p11.copy(),
                self.current_p01_real.copy(),
                self.current_p01_imag.copy()
            )
    
    def get_current_lag(self):
        """Get current lag data."""
        from .lag_computer import LagData  # Import here to avoid circular imports
        
        with self.lock:
            return LagData(
                self.current_lag_p00.copy(),
                self.current_lag_p11.copy(),
                self.current_lag_p01_mag.copy(),
                self.current_lag_p01_phase.copy()
            )
    
    def get_waterfall_data(self):
        """Get waterfall data for plotting."""
        with self.lock:
            return {
                'frames_written': self.frames_written,
                'wf_p00': self.wf_p00.copy(),
                'wf_p11': self.wf_p11.copy(),
                'wf_p01_real': self.wf_p01_real.copy(),
                'wf_p01_imag': self.wf_p01_imag.copy(),
            }
    
    def get_lag_waterfall_data(self):
        """Get lag waterfall data for plotting."""
        with self.lock:
            return {
                'lag_frames_written': self.lag_frames_written,
                'lag_wf_p00': self.lag_wf_p00.copy(),
                'lag_wf_p11': self.lag_wf_p11.copy(),
                'lag_wf_p01_mag': self.lag_wf_p01_mag.copy(),
                'lag_wf_p01_phase': self.lag_wf_p01_phase.copy(),
            }
    
    def get_all_data(self):
        """Get all current data for external access."""
        with self.lock:
            return {
                'frame_number': self.frame_number,
                'frames_written': self.frames_written,
                'lag_frames_written': self.lag_frames_written,
                # Current frame data
                'p00': self.current_p00.copy(),
                'p11': self.current_p11.copy(),
                'p01_real': self.current_p01_real.copy(),
                'p01_imag': self.current_p01_imag.copy(),
                # Current lag data
                'lag_p00': self.current_lag_p00.copy(),
                'lag_p11': self.current_lag_p11.copy(),
                'lag_p01_mag': self.current_lag_p01_mag.copy(),
                'lag_p01_phase': self.current_lag_p01_phase.copy(),
                # Waterfall data
                'wf_p00': self.wf_p00.copy(),
                'wf_p11': self.wf_p11.copy(),
                'wf_p01_real': self.wf_p01_real.copy(),
                'wf_p01_imag': self.wf_p01_imag.copy(),
                # Lag waterfall data
                'lag_wf_p00': self.lag_wf_p00.copy(),
                'lag_wf_p11': self.lag_wf_p11.copy(),
                'lag_wf_p01_mag': self.lag_wf_p01_mag.copy(),
                'lag_wf_p01_phase': self.lag_wf_p01_phase.copy(),
            }
    
    def get_performance_stats(self):
        """Get buffer performance statistics."""
        elapsed = time.time() - self.start_time
        fps = self.total_frames / max(elapsed, 1e-6)
        
        return {
            'total_frames': self.total_frames,
            'frames_written': self.frames_written,
            'lag_frames_written': self.lag_frames_written,
            'average_fps': fps,
            'uptime_seconds': elapsed,
            'waterfall_utilization': (self.frames_written / self.waterfall_len) * 100
        }
