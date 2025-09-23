"""
Configuration and Constants for Periscope Correlator
==================================================

This module contains all constants, configuration classes, and utility functions
used throughout the periscope correlator application.
"""

import numpy as np
from dataclasses import dataclass, field
from typing import Optional

# --- Core Constants ---
NCHAN = 8
NPROD = NCHAN * (NCHAN + 1) // 2
DEFAULT_SERIAL = "0016"
DEFAULT_GAIN_TARGET = 2.1
DEFAULT_WATERFALL_LEN = 250
DEFAULT_FRAMES_TO_AVERAGE = 1
FS_HZ = 3_200_000_000.0  # 3.2 GHz
M_FACTOR = 2**14

# --- UI Constants ---
WINDOW_TITLE = "Periscope Correlator"
DEFAULT_WINDOW_WIDTH = 1200
DEFAULT_WINDOW_HEIGHT = 800

# --- Performance Constants ---
MAX_FPS_DISPLAY_RATE = 30  # Hz
STATS_UPDATE_INTERVAL = 1.0  # seconds
DEFAULT_TIMEOUT = 0.1  # seconds


@dataclass
class CorrelatorConfig:
    """Configuration for the data processing workers."""
    n_firmware_frames: int = 16384
    gain_factor: Optional[np.ndarray] = None
    idx00: int = 0
    idx11: int = 0
    idx01: int = 0
    waterfall_len: int = DEFAULT_WATERFALL_LEN
    frames_to_average: int = DEFAULT_FRAMES_TO_AVERAGE


@dataclass
class NetworkConfig:
    """Configuration for network operations."""
    udp_port: int = 8888
    buffer_size: int = 65536
    timeout: float = 1.0
    max_packets_per_read: int = 100


@dataclass
class PlotConfig:
    """Configuration for plot display settings."""
    refresh_rate_ms: int = 33  # ~30 FPS
    waterfall_history: int = DEFAULT_WATERFALL_LEN
    colormap: str = 'viridis'
    log_scale: bool = True
    auto_range: bool = True


def generate_product_map(n_channels: int = NCHAN) -> np.ndarray:
    """
    Generate the product mapping for correlation data.
    
    Parameters
    ----------
    n_channels : int
        Number of input channels
        
    Returns
    -------
    np.ndarray
        Array of (i, j) pairs for correlation products
    """
    prod_map = []
    for i in range(n_channels):
        for j in range(i, n_channels):
            prod_map.append((i, j))
    return np.array(prod_map)


def get_frequency_axis(n_bins: int = None, fs_hz: float = FS_HZ, m_factor: int = M_FACTOR) -> np.ndarray:
    """
    Generate frequency axis for spectral plots.
    
    Parameters
    ----------
    n_bins : int, optional
        Number of frequency bins. If None, uses M_FACTOR
    fs_hz : float
        Sampling frequency in Hz
    m_factor : int
        Number of samples per frame
        
    Returns
    -------
    np.ndarray
        Frequency axis in Hz
    """
    if n_bins is None:
        n_bins = m_factor // 2
    
    return np.fft.fftfreq(n_bins * 2, 1/fs_hz)[:n_bins]


def get_lag_axis(n_bins: int = None, fs_hz: float = FS_HZ, m_factor: int = M_FACTOR) -> np.ndarray:
    """
    Generate lag axis for lag plots.
    
    Parameters
    ----------
    n_bins : int, optional
        Number of lag bins. If None, uses M_FACTOR
    fs_hz : float
        Sampling frequency in Hz
    m_factor : int
        Number of samples per frame
        
    Returns
    -------
    np.ndarray
        Lag axis in seconds
    """
    if n_bins is None:
        n_bins = m_factor
    
    # Create lag axis centered at zero
    dt = 1.0 / fs_hz
    max_lag = (n_bins // 2) * dt
    return np.linspace(-max_lag, max_lag, n_bins)