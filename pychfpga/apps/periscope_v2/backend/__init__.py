#!/usr/bin/env python
"""
Periscope Correlator V2 Backend Package
====================================

High-performance modular backend for the periscope correlator.
Provides optimized UDP processing, lag computation, and data management.
"""

# Import key classes for easy access
from .udp_processor import HighPerformanceUDPReceiver, OptimizedPacketDecoder
from .lag_computer import OptimizedLagComputer, LagData
from .data_buffers import DataBuffer

# Package version and info
__version__ = "2.0.0"
__author__ = "Periscope Team"
__description__ = "High-performance modular correlator backend"

# Export main classes
__all__ = [
    'HighPerformanceUDPReceiver',
    'OptimizedPacketDecoder', 
    'OptimizedLagComputer',
    'DataBuffer',
    'FrameData',
    'LagData'
]
