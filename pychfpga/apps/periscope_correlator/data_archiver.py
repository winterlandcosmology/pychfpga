#!/usr/bin/env python
"""
Data Archiver for Periscope Correlator
=======================================

Provides HDF5-based data archiving for raw ADC captures and derived products
from Fine Lag, Super-Resolution FFT, and Radar modes.

Author: Periscope Correlator Team
Date: 2025-01-22
"""

import h5py
import numpy as np
from pathlib import Path
from datetime import datetime
from queue import Queue, Empty, Full
from threading import Thread, Event, Lock
from typing import Optional, Dict, Any, Tuple
import time
import logging

# Set up logging
logger = logging.getLogger(__name__)


class DataArchiver:
    """
    Asynchronous HDF5 data archiver for Periscope Correlator.
    
    Features:
    - Non-blocking writes using background thread
    - Automatic file rotation based on size
    - Compression support
    - Metadata tracking
    - Designed for future playback capability
    """
    
    def __init__(self, 
                 base_path: str = "./archives",
                 compression: str = "gzip",
                 compression_level: int = 4,
                 max_file_size_gb: float = 10.0,
                 buffer_size: int = 100):
        """
        Initialize the data archiver.
        
        Args:
            base_path: Directory for archive files
            compression: HDF5 compression type ('gzip', 'lzf', or None)
            compression_level: Compression level (1-9 for gzip)
            max_file_size_gb: Maximum file size before rotation
            buffer_size: Size of write queue buffer
        """
        self.base_path = Path(base_path)
        self.base_path.mkdir(parents=True, exist_ok=True)
        
        # Compression settings
        self.compression = compression
        self.compression_level = compression_level if compression == 'gzip' else None
        
        # File management
        self.max_file_size_bytes = max_file_size_gb * 1024**3
        self.current_file_size = 0
        self.h5file: Optional[h5py.File] = None
        self.session_name: Optional[str] = None
        self.current_mode: Optional[str] = None
        self.file_path: Optional[Path] = None
        
        # Threading and queue
        self.write_queue = Queue(maxsize=buffer_size)
        self.writer_thread: Optional[Thread] = None
        self.stop_event = Event()
        self.write_lock = Lock()
        
        # Counters and statistics
        self.capture_counter: Dict[str, int] = {}
        self.frames_written = 0
        self.frames_dropped = 0
        self.is_recording = False
        self.session_start_time = None
        
        # ADC flag tracking
        self.adc_flags = {
            'total_captures': 0,
            'or_count': 0,
            'ov_count': 0
        }
        
        logger.info(f"DataArchiver initialized with base_path: {self.base_path}")
    
    def start_session(self, mode: str, config: Dict[str, Any]) -> bool:
        """
        Start a new archiving session.
        
        Args:
            mode: Operating mode ('fine_lag', 'super_fft', or 'radar')
            config: Configuration dictionary with mode-specific parameters
            
        Returns:
            True if session started successfully
        """
        if self.is_recording:
            logger.warning("Already recording, stopping current session")
            self.stop_session()
        
        try:
            # Generate session name
            self.session_start_time = time.time()
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.session_name = f"session_{timestamp}_{mode}"
            
            # Create HDF5 file
            self.file_path = self.base_path / f"{self.session_name}.h5"
            self.h5file = h5py.File(self.file_path, 'w')
            self.current_mode = mode
            
            # Initialize file structure
            self._initialize_file_structure(mode, config)
            
            # Reset counters
            self.capture_counter = {mode: 0}
            self.frames_written = 0
            self.frames_dropped = 0
            self.current_file_size = 0
            
            # Start writer thread
            self.stop_event.clear()
            self.writer_thread = Thread(target=self._writer_worker, daemon=True)
            self.writer_thread.start()
            
            self.is_recording = True
            logger.info(f"Started recording session: {self.session_name}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to start session: {e}")
            self.stop_session()
            return False
    
    def stop_session(self):
        """Stop the current archiving session."""
        if not self.is_recording:
            return
        
        self.is_recording = False
        
        # Signal writer thread to stop
        self.stop_event.set()
        
        # Process remaining items in queue
        timeout = time.time() + 5  # 5 second timeout
        while not self.write_queue.empty() and time.time() < timeout:
            time.sleep(0.1)
        
        # Wait for writer thread to finish
        if self.writer_thread and self.writer_thread.is_alive():
            self.writer_thread.join(timeout=2)
        
        # Close HDF5 file
        with self.write_lock:
            if self.h5file:
                # Update final metadata
                if 'metadata' in self.h5file:
                    self.h5file['metadata'].attrs['session_end'] = time.time()
                    self.h5file['metadata'].attrs['frames_written'] = self.frames_written
                    self.h5file['metadata'].attrs['frames_dropped'] = self.frames_dropped
                
                self.h5file.close()
                self.h5file = None
        
        logger.info(f"Stopped recording. Wrote {self.frames_written} frames, "
                   f"dropped {self.frames_dropped} frames")
    
    def archive_fine_lag_frame(self, data: Dict[str, Any]) -> bool:
        """
        Queue fine lag data for archiving.
        
        Args:
            data: Dictionary containing:
                - timestamp: Unix timestamp
                - adc0: Raw ADC0 data (n_frames, 16384)
                - adc1: Raw ADC1 data (n_frames, 16384)
                - lag_p00: ADC0 autocorrelation (8192,)
                - lag_p11: ADC1 autocorrelation (8192,)
                - lag_p01: Cross-correlation (8192,)
                - frames_averaged: Number of frames averaged
                
        Returns:
            True if queued successfully
        """
        if not self.is_recording or self.current_mode != 'fine_lag':
            return False
        
        try:
            self.write_queue.put_nowait(('fine_lag', data))
            return True
        except Full:
            self.frames_dropped += 1
            logger.warning(f"Write queue full, dropped fine_lag frame")
            return False
    
    def archive_super_fft_frame(self, data: Dict[str, Any]) -> bool:
        """
        Queue super FFT data for archiving.
        
        Args:
            data: Dictionary containing:
                - timestamp: Unix timestamp
                - adc0: Raw ADC0 data (262144,)
                - psd: Power spectral density (131073,)
                
        Returns:
            True if queued successfully
        """
        if not self.is_recording or self.current_mode != 'super_fft':
            return False
        
        try:
            self.write_queue.put_nowait(('super_fft', data))
            return True
        except Full:
            self.frames_dropped += 1
            logger.warning(f"Write queue full, dropped super_fft frame")
            return False
    
    def archive_radar_frame(self, data: Dict[str, Any]) -> bool:
        """
        Queue radar data for archiving.
        
        Args:
            data: Dictionary containing:
                - timestamp: Unix timestamp
                - adc0-3: Raw ADC data (n_frames, 16384)
                - lag_adc0-2: Lag spectra (16384,)
                - range_m, azimuth_deg, elevation_deg: Position
                - delays: Dictionary of delays
                - valid: Detection validity flag
                
        Returns:
            True if queued successfully
        """
        if not self.is_recording or self.current_mode != 'radar':
            return False
        
        try:
            self.write_queue.put_nowait(('radar', data))
            return True
        except Full:
            self.frames_dropped += 1
            logger.warning(f"Write queue full, dropped radar frame")
            return False
    
    def update_adc_flags(self, or_count: int, ov_count: int, total: int):
        """Update ADC overrange/overvoltage statistics."""
        self.adc_flags['or_count'] = or_count
        self.adc_flags['ov_count'] = ov_count
        self.adc_flags['total_captures'] = total
    
    def get_status(self) -> Dict[str, Any]:
        """
        Get current archiving status.
        
        Returns:
            Dictionary with status information
        """
        return {
            'recording': self.is_recording,
            'mode': self.current_mode,
            'file_path': str(self.file_path) if self.file_path else '',
            'file_size_mb': self.current_file_size / (1024**2),
            'frames_archived': self.frames_written,
            'frames_dropped': self.frames_dropped,
            'queue_depth': self.write_queue.qsize()
        }
    
    def _initialize_file_structure(self, mode: str, config: Dict[str, Any]):
        """Initialize the HDF5 file structure for the session."""
        # Create metadata group
        meta_group = self.h5file.create_group('metadata')
        meta_group.attrs['session_start'] = self.session_start_time
        meta_group.attrs['mode'] = mode
        meta_group.attrs['fs_hz'] = config.get('fs_hz', 3.2e9)
        meta_group.attrs['frames_to_average'] = config.get('frames_to_average', 1)
        
        # Create ADC flags group
        adc_flags_group = meta_group.create_group('adc_flags')
        adc_flags_group.attrs['total_captures'] = 0
        adc_flags_group.attrs['or_count'] = 0
        adc_flags_group.attrs['ov_count'] = 0
        
        # Create mode-specific structure
        if mode == 'fine_lag':
            self._init_fine_lag_structure(config)
        elif mode == 'super_fft':
            self._init_super_fft_structure(config)
        elif mode == 'radar':
            self._init_radar_structure(config)
    
    def _init_fine_lag_structure(self, config: Dict[str, Any]):
        """Initialize fine lag mode structure."""
        fine_lag_group = self.h5file.create_group('fine_lag')
        fine_lag_group.create_group('captures')
        
        processing_group = fine_lag_group.create_group('processing')
        processing_group.attrs['window_type'] = 'hanning'
        processing_group.attrs['n_frames_averaged'] = config.get('frames_to_average', 1)
    
    def _init_super_fft_structure(self, config: Dict[str, Any]):
        """Initialize super FFT mode structure."""
        super_fft_group = self.h5file.create_group('super_fft')
        super_fft_group.create_group('captures')
        
        processing_group = super_fft_group.create_group('processing')
        processing_group.attrs['window_type'] = 'hanning'
        processing_group.attrs['n_fft_points'] = 262144
    
    def _init_radar_structure(self, config: Dict[str, Any]):
        """Initialize radar mode structure."""
        radar_group = self.h5file.create_group('radar')
        radar_group.create_group('captures')
        
        processing_group = radar_group.create_group('processing')
        processing_group.attrs['antenna_spacing_x01'] = config.get('x01', 1.0)
        processing_group.attrs['antenna_spacing_x02'] = config.get('x02', 1.0)
        processing_group.attrs['threshold'] = config.get('threshold', 0.75)
    
    def _writer_worker(self):
        """Background thread for writing data to HDF5."""
        logger.info("Writer thread started")
        
        while not self.stop_event.is_set() or not self.write_queue.empty():
            try:
                # Get item from queue with timeout
                item = self.write_queue.get(timeout=0.1)
                mode, data = item
                
                # Check file size and rotate if needed
                if self.current_file_size > self.max_file_size_bytes:
                    self._rotate_file()
                
                # Write the frame
                with self.write_lock:
                    if self.h5file:
                        self._write_frame(mode, data)
                
            except Empty:
                continue
            except Exception as e:
                logger.error(f"Error in writer thread: {e}")
        
        logger.info("Writer thread stopped")
    
    def _write_frame(self, mode: str, data: Dict[str, Any]):
        """Write a single frame to the HDF5 file."""
        try:
            if mode == 'fine_lag':
                self._write_fine_lag_frame(data)
            elif mode == 'super_fft':
                self._write_super_fft_frame(data)
            elif mode == 'radar':
                self._write_radar_frame(data)
            
            self.frames_written += 1
            
            # Update file size estimate (rough)
            self.h5file.flush()
            self.current_file_size = self.file_path.stat().st_size
            
        except Exception as e:
            logger.error(f"Error writing {mode} frame: {e}")
    
    def _write_fine_lag_frame(self, data: Dict[str, Any]):
        """Write fine lag mode frame to HDF5."""
        capture_idx = self.capture_counter.get('fine_lag', 0)
        capture_name = f"capture_{capture_idx:06d}"
        
        captures_group = self.h5file['fine_lag']['captures']
        capture_group = captures_group.create_group(capture_name)
        
        # Write timestamp
        capture_group.attrs['timestamp'] = data['timestamp']
        
        # Write raw ADC data
        raw_adc_group = capture_group.create_group('raw_adc')
        
        # Store with compression
        adc0_data = data['adc0'].astype(np.int16)  # Ensure int16 for space efficiency
        adc1_data = data['adc1'].astype(np.int16)
        
        raw_adc_group.create_dataset(
            'adc0', 
            data=adc0_data,
            compression=self.compression,
            compression_opts=self.compression_level,
            chunks=True
        )
        
        raw_adc_group.create_dataset(
            'adc1',
            data=adc1_data,
            compression=self.compression,
            compression_opts=self.compression_level,
            chunks=True
        )
        
        # Write lag spectra
        lag_group = capture_group.create_group('lag_spectra')
        
        for spec_name in ['p00', 'p11', 'p01']:
            lag_group.create_dataset(
                spec_name,
                data=data[f'lag_{spec_name}'],
                compression=self.compression,
                compression_opts=self.compression_level
            )
        
        # Store processing parameters
        capture_group.attrs['frames_averaged'] = data.get('frames_averaged', 1)
        
        self.capture_counter['fine_lag'] = capture_idx + 1
    
    def _write_super_fft_frame(self, data: Dict[str, Any]):
        """Write super FFT mode frame to HDF5."""
        capture_idx = self.capture_counter.get('super_fft', 0)
        capture_name = f"capture_{capture_idx:06d}"
        
        captures_group = self.h5file['super_fft']['captures']
        capture_group = captures_group.create_group(capture_name)
        
        # Write timestamp
        capture_group.attrs['timestamp'] = data['timestamp']
        
        # Write raw ADC data
        raw_adc_group = capture_group.create_group('raw_adc')
        adc0_data = data['adc0'].astype(np.int16)
        
        raw_adc_group.create_dataset(
            'adc0',
            data=adc0_data,
            compression=self.compression,
            compression_opts=self.compression_level,
            chunks=True
        )
        
        # Write PSD
        capture_group.create_dataset(
            'psd',
            data=data['psd'],
            compression=self.compression,
            compression_opts=self.compression_level
        )
        
        self.capture_counter['super_fft'] = capture_idx + 1
    
    def _write_radar_frame(self, data: Dict[str, Any]):
        """Write radar mode frame to HDF5."""
        capture_idx = self.capture_counter.get('radar', 0)
        capture_name = f"capture_{capture_idx:06d}"
        
        captures_group = self.h5file['radar']['captures']
        capture_group = captures_group.create_group(capture_name)
        
        # Write timestamp
        capture_group.attrs['timestamp'] = data['timestamp']
        
        # Write raw ADC data
        raw_adc_group = capture_group.create_group('raw_adc')
        
        for adc_idx in range(4):
            adc_name = f'adc{adc_idx}'
            if adc_name in data:
                adc_data = data[adc_name].astype(np.int16)
                raw_adc_group.create_dataset(
                    adc_name,
                    data=adc_data,
                    compression=self.compression,
                    compression_opts=self.compression_level,
                    chunks=True
                )
        
        # Write lag spectra
        lag_group = capture_group.create_group('lag_spectra')
        
        for adc_idx in range(3):  # Only 3 antennas have lag spectra
            lag_name = f'lag_adc{adc_idx}'
            if lag_name in data:
                lag_group.create_dataset(
                    f'adc{adc_idx}_ref',
                    data=data[lag_name],
                    compression=self.compression,
                    compression_opts=self.compression_level
                )
        
        # Write trilateration results
        trilat_group = capture_group.create_group('trilateration')
        trilat_group.attrs['range_m'] = data.get('range_m', 0.0)
        trilat_group.attrs['azimuth_deg'] = data.get('azimuth_deg', 0.0)
        trilat_group.attrs['elevation_deg'] = data.get('elevation_deg', 0.0)
        trilat_group.attrs['valid'] = data.get('valid', False)
        
        # Write delays
        delays_group = capture_group.create_group('delays')
        delays_dict = data.get('delays', {})
        for i in range(3):
            delays_group.attrs[f'delay_{i}'] = delays_dict.get(i, 0.0)
        
        self.capture_counter['radar'] = capture_idx + 1
    
    def _rotate_file(self):
        """Rotate to a new file when size limit is reached."""
        if not self.is_recording:
            return
        
        logger.info(f"Rotating file at {self.current_file_size / (1024**3):.2f} GB")
        
        # Close current file
        old_file = self.h5file
        if old_file:
            old_file.close()
        
        # Create new file with incremented timestamp
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        new_session_name = f"session_{timestamp}_{self.current_mode}"
        self.file_path = self.base_path / f"{new_session_name}.h5"
        
        # Open new file and reinitialize structure
        self.h5file = h5py.File(self.file_path, 'w')
        config = {
            'fs_hz': 3.2e9,
            'frames_to_average': 1,  # Default values
            'x01': 1.0,
            'x02': 1.0,
            'threshold': 0.75
        }
        self._initialize_file_structure(self.current_mode, config)
        
        # Reset counters for new file
        self.capture_counter[self.current_mode] = 0
        self.current_file_size = 0
        
        logger.info(f"Rotated to new file: {self.file_path}")


class DataReader:
    """
    HDF5 data reader for archived Periscope Correlator data.
    Designed for future playback functionality.
    """
    
    def __init__(self, file_path: str):
        """
        Initialize the data reader.
        
        Args:
            file_path: Path to HDF5 archive file
        """
        self.file_path = Path(file_path)
        self.h5file = h5py.File(file_path, 'r')
        
        # Extract metadata
        self.mode = self.h5file['metadata'].attrs['mode']
        self.session_start = self.h5file['metadata'].attrs['session_start']
        self.fs_hz = self.h5file['metadata'].attrs['fs_hz']
        
        # Get capture count
        captures_group = self.h5file[self.mode]['captures']
        self.capture_count = len(list(captures_group.keys()))
        self.current_index = 0
        
        logger.info(f"Opened archive: {file_path} ({self.mode} mode, {self.capture_count} captures)")
    
    def get_capture_info(self) -> Dict[str, Any]:
        """Get information about the archive."""
        return {
            'mode': self.mode,
            'session_start': self.session_start,
            'fs_hz': self.fs_hz,
            'capture_count': self.capture_count,
            'file_path': str(self.file_path)
        }
    
    def get_capture(self, index: int) -> Optional[Dict[str, Any]]:
        """
        Get a specific capture by index.
        
        Args:
            index: Capture index (0 to capture_count-1)
            
        Returns:
            Dictionary with capture data or None if not found
        """
        if index < 0 or index >= self.capture_count:
            return None
        
        try:
            capture_name = f"capture_{index:06d}"
            captures_group = self.h5file[self.mode]['captures']
            
            if capture_name not in captures_group:
                return None
            
            capture_group = captures_group[capture_name]
            
            # Extract data based on mode
            if self.mode == 'fine_lag':
                return self._extract_fine_lag_capture(capture_group)
            elif self.mode == 'super_fft':
                return self._extract_super_fft_capture(capture_group)
            elif self.mode == 'radar':
                return self._extract_radar_capture(capture_group)
            
        except Exception as e:
            logger.error(f"Error reading capture {index}: {e}")
            return None
    
    def _extract_fine_lag_capture(self, group) -> Dict[str, Any]:
        """Extract fine lag capture data."""
        data = {
            'timestamp': group.attrs['timestamp'],
            'frames_averaged': group.attrs.get('frames_averaged', 1),
            'adc0': np.array(group['raw_adc']['adc0']),
            'adc1': np.array(group['raw_adc']['adc1']),
            'lag_p00': np.array(group['lag_spectra']['p00']),
            'lag_p11': np.array(group['lag_spectra']['p11']),
            'lag_p01': np.array(group['lag_spectra']['p01'])
        }
        return data
    
    def _extract_super_fft_capture(self, group) -> Dict[str, Any]:
        """Extract super FFT capture data."""
        data = {
            'timestamp': group.attrs['timestamp'],
            'adc0': np.array(group['raw_adc']['adc0']),
            'psd': np.array(group['psd'])
        }
        return data
    
    def _extract_radar_capture(self, group) -> Dict[str, Any]:
        """Extract radar capture data."""
        data = {
            'timestamp': group.attrs['timestamp'],
            'range_m': group['trilateration'].attrs['range_m'],
            'azimuth_deg': group['trilateration'].attrs['azimuth_deg'],
            'elevation_deg': group['trilateration'].attrs['elevation_deg'],
            'valid': group['trilateration'].attrs['valid']
        }
        
        # Extract ADC data
        for adc_idx in range(4):
            adc_name = f'adc{adc_idx}'
            if adc_name in group['raw_adc']:
                data[adc_name] = np.array(group['raw_adc'][adc_name])
        
        # Extract lag spectra
        for adc_idx in range(3):
            lag_name = f'adc{adc_idx}_ref'
            if lag_name in group['lag_spectra']:
                data[f'lag_adc{adc_idx}'] = np.array(group['lag_spectra'][lag_name])
        
        # Extract delays
        delays = {}
        for i in range(3):
            delays[i] = group['delays'].attrs[f'delay_{i}']
        data['delays'] = delays
        
        return data
    
    def close(self):
        """Close the HDF5 file."""
        if self.h5file:
            self.h5file.close()
            self.h5file = None
