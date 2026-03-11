#!/usr/bin/env python
"""
HDF5 Inspector for Periscope Correlator Archives
================================================

A tool to inspect, visualize, and analyze archived data from the Periscope Correlator.
Supports all three capture modes: Fine Lag, Super FFT, and Radar.

Usage:
    python hdf5_inspector.py --file archives/session_xyz.h5 --list
    python hdf5_inspector.py --file archives/session_xyz.h5 --plot-capture 5
    python hdf5_inspector.py --file archives/session_xyz.h5 --interactive
    
Interactive Usage:
    from hdf5_inspector import ArchiveInspector
    inspector = ArchiveInspector('archives/session_xyz.h5')
    inspector.interactive_browse()
"""

import argparse
import sys
import os
import time
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.colors import LogNorm
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, List, Any, Tuple

# Try to import the data_archiver
try:
    from .data_archiver import DataReader
except ImportError:
    try:
        from data_archiver import DataReader
    except ImportError:
        print("Error: Cannot import DataReader. Make sure data_archiver.py is in the same directory.")
        sys.exit(1)


class ArchiveInspector:
    """
    Inspector and visualizer for Periscope Correlator HDF5 archives.
    """
    
    def __init__(self, file_path: str):
        """
        Initialize the archive inspector.
        
        Args:
            file_path: Path to the HDF5 archive file
        """
        self.file_path = Path(file_path)
        if not self.file_path.exists():
            raise FileNotFoundError(f"Archive file not found: {file_path}")
        
        self.reader = DataReader(str(file_path))
        self.info = self.reader.get_capture_info()
        
        # Constants from the original app
        self.FS_HZ = 3.2e9  # 3.2 GHz sampling frequency
        self.c_m_per_s = 299792458.0  # Speed of light
        
        print(f"✓ Loaded archive: {self.file_path.name}")
        print(f"  Mode: {self.info['mode']}")
        print(f"  Captures: {self.info['capture_count']}")
        print(f"  Session start: {datetime.fromtimestamp(self.info['session_start'])}")
    
    def list_info(self):
        """Print detailed information about the archive."""
        print(f"\n{'='*60}")
        print(f"Archive Information: {self.file_path.name}")
        print(f"{'='*60}")
        print(f"File path:        {self.file_path}")
        print(f"File size:        {self.file_path.stat().st_size / (1024**2):.2f} MB")
        print(f"Mode:             {self.info['mode']}")
        print(f"Sampling rate:    {self.info['fs_hz']:.2e} Hz")
        print(f"Capture count:    {self.info['capture_count']}")
        print(f"Session start:    {datetime.fromtimestamp(self.info['session_start'])}")
        
        if self.info['capture_count'] > 0:
            # Get first and last capture info
            first_capture = self.reader.get_capture(0)
            last_capture = self.reader.get_capture(self.info['capture_count'] - 1)
            
            if first_capture and last_capture:
                duration = last_capture['timestamp'] - first_capture['timestamp']
                print(f"Duration:         {duration:.2f} seconds")
                print(f"First capture:    {datetime.fromtimestamp(first_capture['timestamp'])}")
                print(f"Last capture:     {datetime.fromtimestamp(last_capture['timestamp'])}")
                
                # Mode-specific info
                if self.info['mode'] == 'fine_lag':
                    n_ch = first_capture.get('n_channels', 2)
                    print(f"Channels:         {n_ch}")
                    print(f"UCAP mode:        {first_capture.get('ucap_mode', 'unknown')}")
                    print(f"Frames averaged:  {first_capture.get('frames_averaged', 'unknown')}")
                    if 'adc0' in first_capture:
                        print(f"ADC data shape:   {first_capture['adc0'].shape}")
                    if 'lags' in first_capture:
                        n_lags_products = len(first_capture['lags'])
                        auto_keys = [k for k in first_capture['lags'] if isinstance(k, tuple) and k[0] == k[1]]
                        cross_keys = [k for k in first_capture['lags'] if isinstance(k, tuple) and k[0] != k[1]]
                        print(f"Lag products:     {n_lags_products} ({len(auto_keys)} auto + {len(cross_keys)} cross)")
                        sample_lag = next(iter(first_capture['lags'].values()))
                        print(f"Lag points:       {len(sample_lag)}")
                    elif 'lag_p00' in first_capture:
                        print(f"Lag points:       {len(first_capture['lag_p00'])}")
                    if 'ffts' in first_capture:
                        print(f"FFT channels:     {len(first_capture['ffts'])}")
                        sample_fft = next(iter(first_capture['ffts'].values()))
                        print(f"FFT bins:         {len(sample_fft)}")
                    if 'config' in first_capture:
                        cfg = first_capture['config']
                        print(f"Nyquist zones:    {cfg['nyquist_zones']}")
                    
                elif self.info['mode'] == 'super_fft':
                    print(f"ADC data shape:   {first_capture['adc0'].shape}")
                    print(f"FFT bins:         {len(first_capture['psd'])}")
                    
                elif self.info['mode'] == 'radar':
                    print(f"ADC data shape:   {first_capture['adc0'].shape}")
                    valid_count = 0
                    for i in range(self.info['capture_count']):
                        capture = self.reader.get_capture(i)
                        if capture and capture.get('valid', False):
                            valid_count += 1
                    print(f"Valid detections: {valid_count}")
            else:
                print("Warning: Could not load capture data for detailed information")
    
    def plot_capture(self, capture_index: int, save_path: Optional[str] = None, show: bool = True):
        """
        Plot a specific capture based on the mode.
        
        Args:
            capture_index: Index of the capture to plot
            save_path: Optional path to save the plot
            show: Whether to display the plot
        """
        if capture_index >= self.info['capture_count']:
            print(f"Error: Capture index {capture_index} exceeds available captures ({self.info['capture_count']})")
            return
        
        capture = self.reader.get_capture(capture_index)
        if not capture:
            print(f"Error: Could not load capture {capture_index}")
            return
        
        capture_time = datetime.fromtimestamp(capture['timestamp'])
        
        if self.info['mode'] == 'fine_lag':
            self._plot_fine_lag_capture(capture, capture_index, capture_time, save_path, show)
        elif self.info['mode'] == 'super_fft':
            self._plot_super_fft_capture(capture, capture_index, capture_time, save_path, show)
        elif self.info['mode'] == 'radar':
            self._plot_radar_capture(capture, capture_index, capture_time, save_path, show)
    
    def _plot_fine_lag_capture(self, capture: Dict[str, Any], index: int, timestamp: datetime, 
                              save_path: Optional[str], show: bool):
        """Plot Fine Lag mode capture — all 8 channels TOD + FFT + lag spectra."""
        n_ch = capture.get('n_channels', 2)
        has_ffts = 'ffts' in capture
        has_lags = 'lags' in capture

        # --- Page 1: 8-channel TOD (4x2) ---
        fig1, axes1 = plt.subplots(2, 4, figsize=(20, 8))
        fig1.suptitle(f'Fine Lag — Time Domain — Capture {index} — {timestamp.strftime("%Y-%m-%d %H:%M:%S")}')
        colors = plt.cm.tab10(np.linspace(0, 1, 10))
        for ch in range(min(n_ch, 8)):
            ax = axes1[ch // 4, ch % 4]
            key = f'adc{ch}'
            if key in capture:
                adc_flat = capture[key].flatten()
                t_us = np.arange(len(adc_flat)) / self.FS_HZ * 1e6
                ax.plot(t_us, adc_flat, color=colors[ch], alpha=0.7, linewidth=0.5)
            ax.set_title(f'ADC {ch}')
            ax.set_xlabel('Time (μs)')
            ax.set_ylabel('Amplitude')
            ax.grid(True, alpha=0.3)
        fig1.tight_layout()

        # --- Page 2: 8-channel FFT (4x2) ---
        fig2 = None
        if has_ffts:
            fig2, axes2 = plt.subplots(2, 4, figsize=(20, 8))
            fig2.suptitle(f'Fine Lag — FFT Power — Capture {index} — {timestamp.strftime("%Y-%m-%d %H:%M:%S")}')
            for ch in range(min(n_ch, 8)):
                ax = axes2[ch // 4, ch % 4]
                if ch in capture['ffts']:
                    fft_data = capture['ffts'][ch]
                    freq_mhz = np.linspace(0, self.FS_HZ / 2, len(fft_data)) / 1e6
                    ax.plot(freq_mhz, fft_data, color=colors[ch], linewidth=0.8)
                    ax.set_yscale('log')
                ax.set_title(f'ADC {ch} FFT')
                ax.set_xlabel('Frequency (MHz)')
                ax.set_ylabel('Power')
                ax.grid(True, alpha=0.3)
            fig2.tight_layout()

        # Page 3 intentionally omitted — lag spectra are shown in waterfalls
        fig3 = None

        if save_path:
            plt.savefig(save_path, dpi=150, bbox_inches='tight')
            print(f"✓ Saved plot to {save_path}")

        if show:
            plt.show()
        else:
            # Return list of figures for PDF
            figs = [fig1]
            if fig2: figs.append(fig2)
            if fig3: figs.append(fig3)
            return figs
    
    def _plot_super_fft_capture(self, capture: Dict[str, Any], index: int, timestamp: datetime,
                               save_path: Optional[str], show: bool):
        """Plot Super FFT mode capture."""
        fig, axes = plt.subplots(2, 2, figsize=(15, 10))
        fig.suptitle(f'Super FFT Mode - Capture {index} - {timestamp.strftime("%Y-%m-%d %H:%M:%S")}')
        
        # Calculate axes
        n_samples = len(capture['adc0'])
        time_axis_us = np.arange(n_samples) / self.FS_HZ * 1e6
        
        # Frequency axis for PSD
        n_fft_bins = len(capture['psd'])
        freq_axis_hz = np.linspace(0, self.FS_HZ/2, n_fft_bins)
        
        # Plot raw time domain (16 concatenated frames)
        axes[0, 0].plot(time_axis_us, capture['adc0'], 'b-', alpha=0.7, linewidth=0.5)
        axes[0, 0].set_title('ADC0 Time Domain (16 Frames)')
        axes[0, 0].set_xlabel('Time (μs)')
        axes[0, 0].set_ylabel('Amplitude')
        axes[0, 0].grid(True, alpha=0.3)
        
        # Plot PSD
        axes[0, 1].plot(freq_axis_hz / 1e6, capture['psd'], 'b-', linewidth=1)
        axes[0, 1].set_title('Power Spectral Density')
        axes[0, 1].set_xlabel('Frequency (MHz)')
        axes[0, 1].set_ylabel('PSD')
        axes[0, 1].set_yscale('log')
        axes[0, 1].grid(True, alpha=0.3)
        
        # Plot PSD zoomed to first 100 MHz
        mask_zoom = freq_axis_hz <= 100e6
        axes[1, 0].plot(freq_axis_hz[mask_zoom] / 1e6, capture['psd'][mask_zoom], 'b-', linewidth=1)
        axes[1, 0].set_title('PSD (0-100 MHz Zoom)')
        axes[1, 0].set_xlabel('Frequency (MHz)')
        axes[1, 0].set_ylabel('PSD')
        axes[1, 0].set_yscale('log')
        axes[1, 0].grid(True, alpha=0.3)
        
        # Add metadata and statistics
        freq_resolution = (freq_axis_hz[1] - freq_axis_hz[0]) / 1e3  # kHz
        time_duration = time_axis_us[-1] - time_axis_us[0]  # μs
        
        metadata_text = f"Frequency resolution: {freq_resolution:.2f} kHz\n"
        metadata_text += f"Time duration: {time_duration:.2f} μs\n"
        metadata_text += f"Total samples: {n_samples:,}\n"
        metadata_text += f"FFT bins: {n_fft_bins:,}\n"
        metadata_text += f"Peak PSD: {np.max(capture['psd']):.2e}\n"
        metadata_text += f"Mean PSD: {np.mean(capture['psd']):.2e}"
        
        axes[1, 1].text(0.1, 0.5, metadata_text, transform=axes[1, 1].transAxes,
                        fontsize=11, verticalalignment='center',
                        bbox=dict(boxstyle="round,pad=0.3", facecolor="lightgray", alpha=0.5))
        axes[1, 1].set_title('Metadata & Statistics')
        axes[1, 1].axis('off')
        
        plt.tight_layout()
        
        if save_path:
            plt.savefig(save_path, dpi=150, bbox_inches='tight')
            print(f"✓ Saved plot to {save_path}")
        
        if show:
            plt.show()
        else:
            plt.close(fig)
    
    def _plot_radar_capture(self, capture: Dict[str, Any], index: int, timestamp: datetime,
                           save_path: Optional[str], show: bool):
        """Plot Radar mode capture."""
        fig = plt.figure(figsize=(16, 12))
        fig.suptitle(f'Radar Mode - Capture {index} - {timestamp.strftime("%Y-%m-%d %H:%M:%S")}')
        
        # Create subplots
        gs = fig.add_gridspec(3, 4, hspace=0.3, wspace=0.3)
        
        # Plot raw ADC data from all 4 channels
        adc_colors = ['blue', 'red', 'green', 'orange']
        for ch in range(4):
            ax = fig.add_subplot(gs[0, ch])
            adc_key = f'adc{ch}'
            if adc_key in capture:
                adc_data = capture[adc_key].flatten()
                n_samples = len(adc_data)
                time_axis_us = np.arange(n_samples) / self.FS_HZ * 1e6
                
                ax.plot(time_axis_us, adc_data, color=adc_colors[ch], alpha=0.7, linewidth=0.5)
                ax.set_title(f'ADC{ch} Time Domain' + (' (Emitter)' if ch == 3 else ' (Antenna)'))
                ax.set_xlabel('Time (μs)')
                ax.set_ylabel('Amplitude')
                ax.grid(True, alpha=0.3)
        
        # Plot lag spectra for antennas 0, 1, 2 (correlation with emitter ADC3)
        lag_samples = np.arange(16384) - 16384 // 2
        lag_time_s = lag_samples / self.FS_HZ
        lag_axis_m = lag_time_s * self.c_m_per_s  # One-way distance
        
        for ch in range(3):
            ax = fig.add_subplot(gs[1, ch])
            lag_key = f'lag_adc{ch}'
            if lag_key in capture:
                ax.plot(lag_axis_m, capture[lag_key], color=adc_colors[ch], linewidth=1.5)
                ax.set_title(f'ADC{ch}-to-Emitter Lag Spectrum')
                ax.set_xlabel('Distance (m)')
                ax.set_ylabel('Correlation Magnitude')
                ax.grid(True, alpha=0.3)
                ax.set_yscale('log')
        
        # Position information
        position_ax = fig.add_subplot(gs[1, 3])
        if capture['valid']:
            range_m = capture['range_m']
            azimuth_deg = capture['azimuth_deg']
            elevation_deg = capture['elevation_deg']
            
            # Convert to Cartesian for display
            azimuth_rad = np.radians(azimuth_deg)
            elevation_rad = np.radians(elevation_deg)
            x = range_m * np.cos(elevation_rad) * np.cos(azimuth_rad)
            y = range_m * np.cos(elevation_rad) * np.sin(azimuth_rad)
            z = range_m * np.sin(elevation_rad)
            
            position_text = f"VALID DETECTION\n\n"
            position_text += f"Range:     {range_m:.2f} m\n"
            position_text += f"Azimuth:   {azimuth_deg:.1f}°\n"
            position_text += f"Elevation: {elevation_deg:.1f}°\n\n"
            position_text += f"Cartesian:\n"
            position_text += f"X: {x:.2f} m\n"
            position_text += f"Y: {y:.2f} m\n"
            position_text += f"Z: {z:.2f} m\n\n"
            
            # Add delay information
            delays = capture.get('delays', {})
            position_text += f"Delays:\n"
            for i in range(3):
                delay_s = delays.get(i, 0)
                delay_m = delay_s * self.c_m_per_s
                position_text += f"Ch{i}: {delay_s*1e9:.2f} ns ({delay_m:.2f} m)\n"
            
            position_ax.text(0.1, 0.5, position_text, transform=position_ax.transAxes,
                            fontsize=10, verticalalignment='center',
                            bbox=dict(boxstyle="round,pad=0.3", facecolor="lightgreen", alpha=0.7))
        else:
            position_ax.text(0.5, 0.5, "NO VALID DETECTION", transform=position_ax.transAxes,
                            fontsize=16, horizontalalignment='center', verticalalignment='center',
                            bbox=dict(boxstyle="round,pad=0.3", facecolor="lightcoral", alpha=0.7))
        
        position_ax.set_title('Position Results')
        position_ax.axis('off')
        
        # Plot time domain overview
        overview_ax = fig.add_subplot(gs[2, :])
        
        frame_length = 16384
        colors = ['blue', 'red', 'green', 'orange']
        labels = ['ADC0 (Ant)', 'ADC1 (Ant)', 'ADC2 (Ant)', 'ADC3 (Emit)']
        
        time_frame_us = np.arange(frame_length) / self.FS_HZ * 1e6
        
        for ch in range(4):
            adc_key = f'adc{ch}'
            if adc_key in capture:
                # Plot first frame only for clarity
                first_frame = capture[adc_key][0, :] if capture[adc_key].ndim > 1 else capture[adc_key][:frame_length]
                overview_ax.plot(time_frame_us, first_frame, color=colors[ch], 
                               alpha=0.8, linewidth=1, label=labels[ch])
        
        overview_ax.set_title('Time Domain Comparison (First Frame)')
        overview_ax.set_xlabel('Time (μs)')
        overview_ax.set_ylabel('Amplitude')
        overview_ax.legend()
        overview_ax.grid(True, alpha=0.3)
        
        if save_path:
            plt.savefig(save_path, dpi=150, bbox_inches='tight')
            print(f"✓ Saved plot to {save_path}")
        
        if show:
            plt.show()
        else:
            plt.close(fig)
    
    def plot_all_captures(self, output_dir: str = "plots", format: str = "png"):
        """
        Generate plots for all captures in the archive.
        
        Args:
            output_dir: Directory to save plots
            format: Image format (png, pdf, svg)
        """
        output_path = Path(output_dir)
        output_path.mkdir(exist_ok=True)
        
        print(f"Generating plots for {self.info['capture_count']} captures...")
        
        for i in range(self.info['capture_count']):
            if i % 10 == 0:
                print(f"  Processing capture {i}/{self.info['capture_count']}")
            
            save_file = output_path / f"capture_{i:06d}.{format}"
            self.plot_capture(i, save_path=str(save_file), show=False)
        
        print(f"✓ All plots saved to {output_path}")
    
    def create_summary_report(self, save_path: Optional[str] = None):
        """Create a comprehensive summary report with key plots."""
        if self.info['capture_count'] == 0:
            print("No captures to summarize")
            return
        
        # Select representative captures
        indices = []
        if self.info['capture_count'] == 1:
            indices = [0]
        elif self.info['capture_count'] <= 5:
            indices = list(range(self.info['capture_count']))
        else:
            # Select first, middle, and last captures, plus a couple more
            indices = [
                0,  # First
                self.info['capture_count'] // 4,  # Quarter
                self.info['capture_count'] // 2,  # Middle  
                3 * self.info['capture_count'] // 4,  # Three quarters
                self.info['capture_count'] - 1  # Last
            ]
        
        if save_path:
            # Create PDF report
            with PdfPages(save_path) as pdf:
                # --- Title page ---
                fig, ax = plt.subplots(figsize=(11, 8.5))
                title_text = f"Periscope Correlator Archive Report\n\n"
                title_text += f"File: {self.file_path.name}\n"
                title_text += f"Mode: {self.info['mode'].upper()}\n"
                title_text += f"Captures: {self.info['capture_count']}\n"
                title_text += f"Session: {datetime.fromtimestamp(self.info['session_start'])}\n"
                title_text += f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                ax.text(0.5, 0.5, title_text, transform=ax.transAxes,
                       fontsize=16, horizontalalignment='center', verticalalignment='center',
                       bbox=dict(boxstyle="round,pad=0.5", facecolor="lightblue", alpha=0.8))
                ax.axis('off')
                ax.set_title('Archive Summary Report', fontsize=20, pad=20)
                pdf.savefig(fig, bbox_inches='tight')
                plt.close(fig)

                # --- Individual capture pages ---
                for i in indices:
                    result = self.plot_capture(i, save_path=None, show=False)
                    # Handle multi-figure returns from 8-channel mode
                    if isinstance(result, list):
                        for fig_item in result:
                            pdf.savefig(fig_item, bbox_inches='tight')
                            plt.close(fig_item)
                    else:
                        pdf.savefig(bbox_inches='tight')
                        plt.close('all')

                # --- Waterfall pages (for fine_lag 8-channel mode) ---
                if self.info['mode'] == 'fine_lag':
                    n_cap = min(self.info['capture_count'], 1000)
                    first = self.reader.get_capture(0)
                    has_ffts = first and 'ffts' in first
                    has_lags = first and 'lags' in first

                    if has_ffts or has_lags:
                        print(f"  Building waterfalls from {n_cap} captures...")
                        # Load all captures once
                        all_captures = []
                        for ci in range(n_cap):
                            cap = self.reader.get_capture(ci)
                            if cap:
                                all_captures.append(cap)
                        nc = len(all_captures)

                    # --- FFT waterfalls (4x2 = 8 channels) ---
                    if has_ffts and nc > 0:
                        n_fft_bins = len(next(iter(first['ffts'].values())))
                        freq_mhz = np.linspace(0, self.FS_HZ / 2, n_fft_bins) / 1e6
                        fig_fft, axes_fft = plt.subplots(2, 4, figsize=(24, 10))
                        fig_fft.suptitle(f'FFT Power Waterfalls — All 8 Channels ({nc} captures)', fontsize=14)
                        for ch in range(8):
                            wf = np.zeros((nc, n_fft_bins))
                            for ci, cap in enumerate(all_captures):
                                if ch in cap.get('ffts', {}):
                                    wf[ci] = cap['ffts'][ch]
                            ax = axes_fft[ch // 4, ch % 4]
                            wf_log = np.log10(np.maximum(wf, 1e-10))
                            ax.imshow(wf_log, aspect='auto', origin='lower',
                                     extent=[freq_mhz[0], freq_mhz[-1], 0, nc],
                                     cmap='viridis')
                            ax.set_title(f'ADC {ch}')
                            ax.set_xlabel('Freq (MHz)')
                            ax.set_ylabel('Capture')
                        fig_fft.tight_layout()
                        pdf.savefig(fig_fft, bbox_inches='tight')
                        plt.close(fig_fft)

                    # --- Lag auto-correlation waterfalls (4x2 = 8 autos) ---
                    if has_lags and nc > 0:
                        n_lags = 8192
                        fine_lag_m = (np.arange(n_lags) - n_lags // 2) / self.FS_HZ * self.c_m_per_s / 2
                        fig_auto, axes_auto = plt.subplots(2, 4, figsize=(24, 10))
                        fig_auto.suptitle(f'Auto-Correlation Lag Waterfalls ({nc} captures)', fontsize=14)
                        for ch in range(8):
                            wf = np.zeros((nc, n_lags))
                            for ci, cap in enumerate(all_captures):
                                key = (ch, ch)
                                if key in cap.get('lags', {}):
                                    wf[ci] = cap['lags'][key]
                            ax = axes_auto[ch // 4, ch % 4]
                            wf_log = np.log10(np.maximum(wf, 1e-10))
                            ax.imshow(wf_log, aspect='auto', origin='lower',
                                     extent=[fine_lag_m[0], fine_lag_m[-1], 0, nc],
                                     cmap='viridis')
                            ax.set_title(f'p{ch}{ch} (auto)')
                            ax.set_xlabel('Distance (m)')
                            ax.set_ylabel('Capture')
                        fig_auto.tight_layout()
                        pdf.savefig(fig_auto, bbox_inches='tight')
                        plt.close(fig_auto)

                    # --- Selected cross-correlation waterfalls (p01..p07) ---
                    if has_lags and nc > 0:
                        cross_keys = [(0, j) for j in range(1, 8)]  # p01 through p07
                        fig_cross, axes_cross = plt.subplots(2, 4, figsize=(24, 10))
                        fig_cross.suptitle(f'Cross-Correlation Lag Waterfalls — ADC0 × ADC1-7 ({nc} captures)', fontsize=14)
                        for idx, (i_ch, j_ch) in enumerate(cross_keys):
                            if idx >= 8:
                                break
                            wf = np.zeros((nc, n_lags))
                            for ci, cap in enumerate(all_captures):
                                key = (i_ch, j_ch)
                                if key in cap.get('lags', {}):
                                    wf[ci] = cap['lags'][key]
                            ax = axes_cross[idx // 4, idx % 4]
                            wf_log = np.log10(np.maximum(wf, 1e-10))
                            ax.imshow(wf_log, aspect='auto', origin='lower',
                                     extent=[fine_lag_m[0], fine_lag_m[-1], 0, nc],
                                     cmap='viridis')
                            ax.set_title(f'p{i_ch}{j_ch}')
                            ax.set_xlabel('Distance (m)')
                            ax.set_ylabel('Capture')
                        # Hide unused subplot
                        axes_cross[1, 3].axis('off')
                        fig_cross.tight_layout()
                        pdf.savefig(fig_cross, bbox_inches='tight')
                        plt.close(fig_cross)

            print(f"✓ Summary report saved to {save_path}")
        else:
            # Just show the plots
            for i in indices:
                print(f"\nShowing capture {i}...")
                self.plot_capture(i, show=True)
    
    def plot_waterfall(self, start_index: int = 0, end_index: Optional[int] = None,
                      log_scale: bool = False, show_timestamps: bool = False,
                      colormap: str = 'viridis', save_path: Optional[str] = None,
                      show: bool = True, max_captures: int = 500):
        """
        Plot waterfall diagram showing data evolution over time.
        
        Args:
            start_index: Starting capture index
            end_index: Ending capture index (None for all remaining)
            log_scale: Use logarithmic scale for color mapping
            show_timestamps: Show timestamps instead of indices on y-axis
            colormap: Matplotlib colormap name
            save_path: Optional path to save the plot
            show: Whether to display the plot
            max_captures: Maximum number of captures to plot (for performance)
        """
        # Determine capture range
        if end_index is None:
            end_index = min(start_index + max_captures, self.info['capture_count'])
        else:
            end_index = min(end_index, self.info['capture_count'])
            
        if end_index - start_index > max_captures:
            print(f"Warning: Limiting to {max_captures} captures for performance.")
            end_index = start_index + max_captures
        
        n_captures = end_index - start_index
        if n_captures <= 0:
            print(f"Error: Invalid capture range {start_index}-{end_index}")
            return
        
        print(f"Generating waterfall plot for captures {start_index}-{end_index-1}...")
        
        # Call mode-specific waterfall plotting
        if self.info['mode'] == 'fine_lag':
            return self._plot_fine_lag_waterfall(start_index, end_index, log_scale, 
                                                show_timestamps, colormap, save_path, show)
        elif self.info['mode'] == 'super_fft':
            return self._plot_super_fft_waterfall(start_index, end_index, log_scale,
                                                 show_timestamps, colormap, save_path, show)
        elif self.info['mode'] == 'radar':
            return self._plot_radar_waterfall(start_index, end_index, log_scale,
                                             show_timestamps, colormap, save_path, show)
    
    def _plot_fine_lag_waterfall(self, start_index: int, end_index: int,
                                log_scale: bool, show_timestamps: bool,
                                colormap: str, save_path: Optional[str], show: bool):
        """Plot waterfall for Fine Lag mode."""
        n_captures = end_index - start_index
        
        # Get first capture to determine dimensions
        first_capture = self.reader.get_capture(start_index)
        if not first_capture:
            print(f"Error: Could not load capture {start_index}")
            return
        
        # Initialize data arrays
        n_lag_points = len(first_capture['lag_p00'])
        waterfall_p00 = np.zeros((n_captures, n_lag_points))
        waterfall_p11 = np.zeros((n_captures, n_lag_points))
        waterfall_p01 = np.zeros((n_captures, n_lag_points))
        timestamps = []
        
        # Collect data
        for i in range(n_captures):
            capture = self.reader.get_capture(start_index + i)
            if capture:
                waterfall_p00[i, :] = capture['lag_p00']
                waterfall_p11[i, :] = capture['lag_p11']
                waterfall_p01[i, :] = capture['lag_p01']
                timestamps.append(capture['timestamp'])
        
        # Calculate axes
        fine_lag_samples = np.arange(n_lag_points) - n_lag_points // 2
        fine_lag_axis_m = fine_lag_samples / self.FS_HZ * self.c_m_per_s / 2
        
        # Create figure
        fig, axes = plt.subplots(1, 3, figsize=(18, 8))
        fig.suptitle(f'Fine Lag Waterfall - Captures {start_index} to {end_index-1}', fontsize=14)
        
        # Prepare y-axis
        if show_timestamps:
            y_labels = [datetime.fromtimestamp(ts).strftime('%H:%M:%S') for ts in timestamps]
            y_axis = np.arange(n_captures)
        else:
            y_axis = np.arange(start_index, end_index)
        
        # Determine color normalization
        norm = LogNorm() if log_scale else None
        
        # Plot waterfalls
        im0 = axes[0].imshow(waterfall_p00, aspect='auto', origin='lower',
                           extent=[fine_lag_axis_m[0], fine_lag_axis_m[-1], 
                                  y_axis[0], y_axis[-1]],
                           cmap=colormap, norm=norm)
        axes[0].set_title('ADC0 Autocorrelation')
        axes[0].set_xlabel('Distance (m)')
        axes[0].set_ylabel('Capture Index' if not show_timestamps else 'Time')
        plt.colorbar(im0, ax=axes[0], label='Correlation')
        
        im1 = axes[1].imshow(waterfall_p11, aspect='auto', origin='lower',
                           extent=[fine_lag_axis_m[0], fine_lag_axis_m[-1],
                                  y_axis[0], y_axis[-1]],
                           cmap=colormap, norm=norm)
        axes[1].set_title('ADC1 Autocorrelation')
        axes[1].set_xlabel('Distance (m)')
        axes[1].set_ylabel('Capture Index' if not show_timestamps else 'Time')
        plt.colorbar(im1, ax=axes[1], label='Correlation')
        
        im2 = axes[2].imshow(waterfall_p01, aspect='auto', origin='lower',
                           extent=[fine_lag_axis_m[0], fine_lag_axis_m[-1],
                                  y_axis[0], y_axis[-1]],
                           cmap=colormap, norm=norm)
        axes[2].set_title('ADC0/1 Cross-correlation')
        axes[2].set_xlabel('Distance (m)')
        axes[2].set_ylabel('Capture Index' if not show_timestamps else 'Time')
        plt.colorbar(im2, ax=axes[2], label='Correlation')
        
        # Add timestamp labels if requested
        if show_timestamps and n_captures <= 20:
            for ax in axes:
                ax.set_yticks(y_axis[::max(1, n_captures//10)])
                ax.set_yticklabels(y_labels[::max(1, n_captures//10)], rotation=45, ha='right')
        
        plt.tight_layout()
        
        if save_path:
            plt.savefig(save_path, dpi=150, bbox_inches='tight')
            print(f"✓ Saved waterfall plot to {save_path}")
        
        if show:
            plt.show()
        else:
            plt.close(fig)
            
        return fig
    
    def _plot_super_fft_waterfall(self, start_index: int, end_index: int,
                                 log_scale: bool, show_timestamps: bool,
                                 colormap: str, save_path: Optional[str], show: bool):
        """Plot waterfall for Super FFT mode."""
        n_captures = end_index - start_index
        
        # Get first capture to determine dimensions
        first_capture = self.reader.get_capture(start_index)
        if not first_capture:
            print(f"Error: Could not load capture {start_index}")
            return
        
        # Initialize data arrays
        n_fft_bins = len(first_capture['psd'])
        waterfall_psd = np.zeros((n_captures, n_fft_bins))
        timestamps = []
        
        # Collect data
        print("Loading capture data...")
        for i in range(n_captures):
            if i % 50 == 0:
                print(f"  Loading capture {i}/{n_captures}")
            capture = self.reader.get_capture(start_index + i)
            if capture:
                waterfall_psd[i, :] = capture['psd']
                timestamps.append(capture['timestamp'])
        
        # Calculate frequency axis
        freq_axis_mhz = np.linspace(0, self.FS_HZ/2, n_fft_bins) / 1e6
        
        # Create figure
        fig, axes = plt.subplots(1, 2, figsize=(16, 8))
        fig.suptitle(f'Super FFT Waterfall - Captures {start_index} to {end_index-1}', fontsize=14)
        
        # Prepare y-axis
        if show_timestamps:
            y_labels = [datetime.fromtimestamp(ts).strftime('%H:%M:%S') for ts in timestamps]
            y_axis = np.arange(n_captures)
        else:
            y_axis = np.arange(start_index, end_index)
        
        # Determine color normalization
        norm = LogNorm() if log_scale else None
        
        # Plot full spectrum waterfall
        im0 = axes[0].imshow(waterfall_psd, aspect='auto', origin='lower',
                           extent=[freq_axis_mhz[0], freq_axis_mhz[-1],
                                  y_axis[0], y_axis[-1]],
                           cmap=colormap, norm=norm)
        axes[0].set_title('PSD Waterfall - Full Spectrum')
        axes[0].set_xlabel('Frequency (MHz)')
        axes[0].set_ylabel('Capture Index' if not show_timestamps else 'Time')
        plt.colorbar(im0, ax=axes[0], label='PSD (log)' if log_scale else 'PSD')
        
        # Plot zoomed waterfall (0-100 MHz)
        zoom_mask = freq_axis_mhz <= 100
        im1 = axes[1].imshow(waterfall_psd[:, zoom_mask], aspect='auto', origin='lower',
                           extent=[0, 100, y_axis[0], y_axis[-1]],
                           cmap=colormap, norm=norm)
        axes[1].set_title('PSD Waterfall - 0-100 MHz Zoom')
        axes[1].set_xlabel('Frequency (MHz)')
        axes[1].set_ylabel('Capture Index' if not show_timestamps else 'Time')
        plt.colorbar(im1, ax=axes[1], label='PSD (log)' if log_scale else 'PSD')
        
        # Add timestamp labels if requested
        if show_timestamps and n_captures <= 20:
            for ax in axes:
                ax.set_yticks(y_axis[::max(1, n_captures//10)])
                ax.set_yticklabels(y_labels[::max(1, n_captures//10)], rotation=45, ha='right')
        
        plt.tight_layout()
        
        if save_path:
            plt.savefig(save_path, dpi=150, bbox_inches='tight')
            print(f"✓ Saved waterfall plot to {save_path}")
        
        if show:
            plt.show()
        else:
            plt.close(fig)
            
        return fig
    
    def _plot_radar_waterfall(self, start_index: int, end_index: int,
                             log_scale: bool, show_timestamps: bool,
                             colormap: str, save_path: Optional[str], show: bool):
        """Plot waterfall for Radar mode."""
        n_captures = end_index - start_index
        
        # Get first capture to determine dimensions
        first_capture = self.reader.get_capture(start_index)
        if not first_capture:
            print(f"Error: Could not load capture {start_index}")
            return
        
        # Initialize data arrays
        n_lag_points = len(first_capture.get('lag_adc0', []))
        if n_lag_points == 0:
            print("Error: No lag data found in radar captures")
            return
            
        waterfall_adc0 = np.zeros((n_captures, n_lag_points))
        waterfall_adc1 = np.zeros((n_captures, n_lag_points))
        waterfall_adc2 = np.zeros((n_captures, n_lag_points))
        valid_detections = []
        timestamps = []
        
        # Collect data
        for i in range(n_captures):
            capture = self.reader.get_capture(start_index + i)
            if capture:
                waterfall_adc0[i, :] = capture.get('lag_adc0', np.zeros(n_lag_points))
                waterfall_adc1[i, :] = capture.get('lag_adc1', np.zeros(n_lag_points))
                waterfall_adc2[i, :] = capture.get('lag_adc2', np.zeros(n_lag_points))
                valid_detections.append(capture.get('valid', False))
                timestamps.append(capture['timestamp'])
        
        # Calculate lag axis
        lag_samples = np.arange(n_lag_points) - n_lag_points // 2
        lag_axis_m = lag_samples / self.FS_HZ * self.c_m_per_s
        
        # Create figure
        fig, axes = plt.subplots(1, 3, figsize=(18, 8))
        fig.suptitle(f'Radar Mode Waterfall - Captures {start_index} to {end_index-1}', fontsize=14)
        
        # Prepare y-axis
        if show_timestamps:
            y_labels = [datetime.fromtimestamp(ts).strftime('%H:%M:%S') for ts in timestamps]
            y_axis = np.arange(n_captures)
        else:
            y_axis = np.arange(start_index, end_index)
        
        # Determine color normalization
        norm = LogNorm() if log_scale else None
        
        # Plot waterfalls for each antenna
        titles = ['ADC0-to-Emitter', 'ADC1-to-Emitter', 'ADC2-to-Emitter']
        waterfalls = [waterfall_adc0, waterfall_adc1, waterfall_adc2]
        
        for idx, (ax, data, title) in enumerate(zip(axes, waterfalls, titles)):
            im = ax.imshow(data, aspect='auto', origin='lower',
                          extent=[lag_axis_m[0], lag_axis_m[-1],
                                 y_axis[0], y_axis[-1]],
                          cmap=colormap, norm=norm)
            ax.set_title(title)
            ax.set_xlabel('Distance (m)')
            ax.set_ylabel('Capture Index' if not show_timestamps else 'Time')
            plt.colorbar(im, ax=ax, label='Correlation (log)' if log_scale else 'Correlation')
            
            # Mark valid detections
            valid_indices = [i for i, valid in enumerate(valid_detections) if valid]
            if valid_indices:
                y_valid = [y_axis[i] for i in valid_indices]
                ax.scatter([0] * len(y_valid), y_valid, c='red', marker='>', s=50,
                          label='Valid Detection', alpha=0.7)
                if idx == 0:
                    ax.legend(loc='upper right')
        
        # Add timestamp labels if requested
        if show_timestamps and n_captures <= 20:
            for ax in axes:
                ax.set_yticks(y_axis[::max(1, n_captures//10)])
                ax.set_yticklabels(y_labels[::max(1, n_captures//10)], rotation=45, ha='right')
        
        plt.tight_layout()
        
        if save_path:
            plt.savefig(save_path, dpi=150, bbox_inches='tight')
            print(f"✓ Saved waterfall plot to {save_path}")
        
        if show:
            plt.show()
        else:
            plt.close(fig)
            
        return fig
    
    def export_data(self, capture_index: int, output_file: str, format: str = "csv"):
        """
        Export capture data to various formats.
        
        Args:
            capture_index: Index of capture to export
            output_file: Output file path
            format: Export format ('csv', 'npz')
        """
        capture = self.reader.get_capture(capture_index)
        if not capture:
            print(f"Error: Could not load capture {capture_index}")
            return
        
        if format.lower() == 'csv':
            self._export_to_csv(capture, capture_index, output_file)
        elif format.lower() == 'npz':
            self._export_to_npz(capture, capture_index, output_file)
        else:
            print(f"Error: Unsupported format '{format}'. Use 'csv' or 'npz'")
    
    def _export_to_csv(self, capture: Dict[str, Any], capture_index: int, output_file: str):
        """Export data to CSV format."""
        output_path = Path(output_file)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        if self.info['mode'] == 'fine_lag':
            # Create CSV with lag spectra
            fine_lag_samples = np.arange(8192) - 8192 // 2
            fine_lag_axis_m = fine_lag_samples / self.FS_HZ * self.c_m_per_s / 2
            
            data = np.column_stack([
                fine_lag_axis_m,
                capture['lag_p00'],
                capture['lag_p11'], 
                capture['lag_p01']
            ])
            
            header = "distance_m,lag_p00,lag_p11,lag_p01"
            np.savetxt(output_file, data, delimiter=',', header=header, comments='')
            
        elif self.info['mode'] == 'super_fft':
            # Create CSV with PSD
            n_fft_bins = len(capture['psd'])
            freq_axis_hz = np.linspace(0, self.FS_HZ/2, n_fft_bins)
            
            data = np.column_stack([freq_axis_hz, capture['psd']])
            header = "frequency_hz,psd"
            np.savetxt(output_file, data, delimiter=',', header=header, comments='')
            
        elif self.info['mode'] == 'radar':
            # Create CSV with position data
            if capture['valid']:
                data = [[
                    capture['range_m'],
                    capture['azimuth_deg'],
                    capture['elevation_deg'],
                    1 if capture['valid'] else 0
                ]]
                header = "range_m,azimuth_deg,elevation_deg,valid"
                np.savetxt(output_file, data, delimiter=',', header=header, comments='')
            else:
                print("No valid detection data to export")
                return
        
        print(f"✓ Exported data to {output_file}")
    
    def _export_to_npz(self, capture: Dict[str, Any], capture_index: int, output_file: str):
        """Export data to NumPy .npz format."""
        # Add axes information to the capture data
        export_data = capture.copy()
        
        if self.info['mode'] == 'fine_lag':
            fine_lag_samples = np.arange(8192) - 8192 // 2
            export_data['fine_lag_axis_m'] = fine_lag_samples / self.FS_HZ * self.c_m_per_s / 2
            
        elif self.info['mode'] == 'super_fft':
            n_fft_bins = len(capture['psd'])
            export_data['freq_axis_hz'] = np.linspace(0, self.FS_HZ/2, n_fft_bins)
            
        elif self.info['mode'] == 'radar':
            lag_samples = np.arange(16384) - 16384 // 2
            export_data['lag_axis_m'] = lag_samples / self.FS_HZ * self.c_m_per_s
        
        # Add metadata
        export_data['mode'] = self.info['mode']
        export_data['fs_hz'] = self.info['fs_hz']
        export_data['capture_index'] = capture_index
        
        np.savez_compressed(output_file, **export_data)
        print(f"✓ Exported data to {output_file}")
    
    def interactive_browse(self):
        """
        Interactive command-line interface for browsing the archive.
        """
        print(f"\n{'='*60}")
        print("Interactive Archive Browser")
        print(f"{'='*60}")
        print("Commands:")
        print("  info                          - Show archive information")
        print("  list                          - List all captures")
        print("  plot <index>                  - Plot specific capture")
        print("  plot <start>-<end>            - Plot range of captures")
        print("  waterfall <start> <end> [options] - Plot waterfall diagram")
        print("    Options: --log --timestamps --colormap <cmap>")
        print("  wf <start> <end> [options]    - Shorthand for waterfall")
        print("  export <index> <file>         - Export capture data")
        print("  summary [filename.pdf]        - Create summary report")
        print("  quit/exit                     - Exit browser")
        print()
        
        while True:
            try:
                cmd = input(f"[{self.file_path.name}] > ").strip().split()
                
                if not cmd:
                    continue
                
                if cmd[0].lower() in ['quit', 'exit', 'q']:
                    break
                    
                elif cmd[0].lower() == 'info':
                    self.list_info()
                    
                elif cmd[0].lower() == 'list':
                    print(f"\nCaptures in {self.file_path.name}:")
                    for i in range(min(20, self.info['capture_count'])):
                        capture = self.reader.get_capture(i)
                        if capture:
                            timestamp = datetime.fromtimestamp(capture['timestamp'])
                            status = ""
                            if self.info['mode'] == 'radar':
                                status = " ✓" if capture.get('valid', False) else " ✗"
                            print(f"  {i:3d}: {timestamp.strftime('%H:%M:%S.%f')[:-3]}{status}")
                    
                    if self.info['capture_count'] > 20:
                        print(f"  ... and {self.info['capture_count'] - 20} more")
                    
                elif cmd[0].lower() == 'plot':
                    if len(cmd) < 2:
                        print("Error: plot command requires an index or range")
                        continue
                    
                    try:
                        if '-' in cmd[1]:
                            # Range plotting
                            start, end = map(int, cmd[1].split('-'))
                            for i in range(start, min(end + 1, self.info['capture_count'])):
                                print(f"Plotting capture {i}...")
                                self.plot_capture(i, show=True)
                        else:
                            # Single capture
                            index = int(cmd[1])
                            self.plot_capture(index, show=True)
                    except ValueError:
                        print("Error: Invalid capture index or range")
                    except Exception as e:
                        print(f"Error plotting: {e}")
                
                elif cmd[0].lower() in ['waterfall', 'wf']:
                    if len(cmd) < 3:
                        print("Error: waterfall command requires start and end indices")
                        print("Usage: waterfall <start> <end> [--log] [--timestamps] [--colormap <cmap>]")
                        continue
                    
                    try:
                        start = int(cmd[1])
                        end = int(cmd[2])
                        
                        # Parse options
                        log_scale = '--log' in cmd
                        show_timestamps = '--timestamps' in cmd
                        
                        # Find colormap option
                        colormap = 'viridis'
                        if '--colormap' in cmd:
                            cmap_idx = cmd.index('--colormap')
                            if cmap_idx + 1 < len(cmd):
                                colormap = cmd[cmap_idx + 1]
                        
                        self.plot_waterfall(start, end, log_scale=log_scale,
                                          show_timestamps=show_timestamps,
                                          colormap=colormap, show=True)
                    except ValueError:
                        print("Error: Invalid capture indices")
                    except Exception as e:
                        print(f"Error creating waterfall: {e}")
                
                elif cmd[0].lower() == 'export':
                    if len(cmd) < 3:
                        print("Error: export command requires capture index and filename")
                        continue
                    
                    try:
                        index = int(cmd[1])
                        filename = cmd[2]
                        format_ext = Path(filename).suffix.lower()
                        
                        if format_ext == '.csv':
                            self.export_data(index, filename, 'csv')
                        elif format_ext == '.npz':
                            self.export_data(index, filename, 'npz')
                        else:
                            print("Error: Unsupported format. Use .csv or .npz extension")
                    except ValueError:
                        print("Error: Invalid capture index")
                    except Exception as e:
                        print(f"Error exporting: {e}")
                
                elif cmd[0].lower() == 'summary':
                    try:
                        if len(cmd) > 1:
                            filename = cmd[1]
                            if not filename.endswith('.pdf'):
                                filename += '.pdf'
                            self.create_summary_report(filename)
                        else:
                            self.create_summary_report()
                    except Exception as e:
                        print(f"Error creating summary: {e}")
                        
                else:
                    print(f"Unknown command: {cmd[0]}")
                    print("Type 'info' for archive information or 'quit' to exit")
                    
            except KeyboardInterrupt:
                print("\nExiting...")
                break
            except EOFError:
                print("\nExiting...")
                break
            except Exception as e:
                print(f"Error: {e}")
        
        self.reader.close()
        print("Archive browser closed.")


def main():
    """Command-line interface for the HDF5 inspector."""
    parser = argparse.ArgumentParser(description="Inspect and visualize Periscope Correlator HDF5 archives")
    parser.add_argument('--file', '-f', required=True, help="Path to HDF5 archive file")
    parser.add_argument('--list', '-l', action='store_true', help="List archive information")
    parser.add_argument('--plot-capture', '-p', type=int, help="Plot specific capture by index")
    parser.add_argument('--plot-all', action='store_true', help="Generate plots for all captures")
    parser.add_argument('--waterfall', '-w', nargs=2, type=int, metavar=('START', 'END'),
                       help="Generate waterfall plot for capture range")
    parser.add_argument('--log-scale', action='store_true', help="Use logarithmic scale for waterfall data")
    parser.add_argument('--show-timestamps', action='store_true', 
                       help="Show timestamps instead of indices on waterfall y-axis")
    parser.add_argument('--colormap', default='viridis', help="Colormap for waterfall plot")
    parser.add_argument('--interactive', '-i', action='store_true', help="Start interactive browser")
    parser.add_argument('--summary', '-s', help="Create summary report (PDF filename)")
    parser.add_argument('--export', '-e', nargs=2, metavar=('INDEX', 'FILE'),
                       help="Export capture data (index and output file)")
    parser.add_argument('--output-dir', '-o', default='plots', help="Output directory for plots")
    parser.add_argument('--format', default='png', choices=['png', 'pdf', 'svg'],
                       help="Output format for plots")
    
    args = parser.parse_args()
    
    try:
        inspector = ArchiveInspector(args.file)
        
        if args.list:
            inspector.list_info()
        
        if args.plot_capture is not None:
            inspector.plot_capture(args.plot_capture)
        
        if args.plot_all:
            inspector.plot_all_captures(args.output_dir, args.format)
        
        if args.waterfall:
            start, end = args.waterfall
            save_path = None
            if args.output_dir and args.format:
                save_path = f"{args.output_dir}/waterfall_{start}_{end}.{args.format}"
            inspector.plot_waterfall(start, end, 
                                   log_scale=args.log_scale,
                                   show_timestamps=args.show_timestamps,
                                   colormap=args.colormap,
                                   save_path=save_path,
                                   show=True)
        
        if args.summary:
            inspector.create_summary_report(args.summary)
        
        if args.export:
            index, filename = args.export
            inspector.export_data(int(index), filename)
        
        if args.interactive:
            inspector.interactive_browse()
            
        # If no specific action was requested, show info and enter interactive mode
        if not any([args.list, args.plot_capture is not None, args.plot_all, args.waterfall,
                   args.summary, args.export, args.interactive]):
            inspector.list_info()
            print("\nNo specific action requested. Starting interactive mode...")
            inspector.interactive_browse()
            
    except FileNotFoundError as e:
        print(f"Error: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()
