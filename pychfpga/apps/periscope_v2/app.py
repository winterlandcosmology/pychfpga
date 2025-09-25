#!/usr/bin/env python
"""
Periscope Correlator V2 - Main GUI Application
============================================

High-performance modular correlator with optimized UDP processing
and the familiar PyQt5 interface.
"""
import sys
import time
import numpy as np
from PyQt5 import QtWidgets, QtCore, QtGui
import pyqtgraph as pg
import os
from typing import Optional, Dict
from dataclasses import dataclass

# Add the parent directory to the path for pychfpga imports
sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..'))

# Import from pychfpga package
from pychfpga.pocket_correlator import POCKET_CORRELATOR
from pychfpga.fpga_firmware.chfpga.x_engine.UCORR import UCorrFrameReceiver, NBINS_TOTAL

# Import our optimized backend
from backend import HighPerformanceUDPReceiver, OptimizedLagComputer, DataBuffer
from backend.udp_processor import FrameData
from backend.lag_computer import LagData

# Constants from original
NCHAN = 8
NPROD = NCHAN * (NCHAN + 1) // 2
DEFAULT_SERIAL = "0016"
DEFAULT_GAIN_TARGET = 2.1
DEFAULT_WATERFALL_LEN = 250
DEFAULT_FRAMES_TO_AVERAGE = 1
FS_HZ = 3_200_000_000.0  # 3.2 GHz
M_FACTOR = 2**14

@dataclass
class CorrelatorConfig:
    """Configuration for the correlator system."""
    n_firmware_frames: int = 16384
    gain_factor: Optional[np.ndarray] = None
    idx00: int = 0
    idx11: int = 0
    idx01: int = 0
    waterfall_len: int = DEFAULT_WATERFALL_LEN
    frames_to_average: int = DEFAULT_FRAMES_TO_AVERAGE

class PeriscopeCorrelatorV2(QtWidgets.QMainWindow):
    """
    Main application window for Periscope Correlator V2.
    
    Combines the familiar original interface with the high-performance
    optimized backend for maximum speed and usability.
    """
    
    # Signal emitted when new data is available
    data_ready = QtCore.pyqtSignal()
    
    def __init__(self, serial_number: str = DEFAULT_SERIAL):
        super().__init__()
        self.serial_number = serial_number
        self.setWindowTitle("Periscope Correlator V2 - Not Connected")
        self.setGeometry(100, 100, 1400, 800)

        # Core components
        self.pocket_correlator: Optional[POCKET_CORRELATOR] = None
        self.udp_receiver: Optional[HighPerformanceUDPReceiver] = None
        self.lag_computer: Optional[OptimizedLagComputer] = None
        self.data_buffer: Optional[DataBuffer] = None
        
        self.config = CorrelatorConfig()
        self.computed_gains: Optional[np.ndarray] = None
        
        # Initialize frequency and lag axes
        self.freq_axis = self._calculate_frequency_axis(1)  # Zone 1 default
        self.lag_axis = self._calculate_lag_axis()
        
        # Product map
        self.prod_map = self._generate_prod_map()
        self._precalculate_indices()
        
        # Build UI
        self._build_ui()
        self._setup_status_bar()
        
        # Connect data ready signal
        self.data_ready.connect(self._update_plots)
        
        self.show()

    def _generate_prod_map(self) -> np.ndarray:
        """Generate the product map for the correlator."""
        prod_map = [(i, (j + i) % NCHAN) if i < NCHAN - j else (((j + i) % NCHAN), i - 1) 
                    for i in range(NCHAN + 1) for j in range(NCHAN // 2)]
        return np.array(prod_map)

    def _precalculate_indices(self) -> None:
        """Pre-calculate the indices for the products to be plotted."""
        try:
            self.config.idx00 = np.where((self.prod_map[:, 0] == 0) & (self.prod_map[:, 1] == 0))[0][0]
            self.config.idx11 = np.where((self.prod_map[:, 0] == 1) & (self.prod_map[:, 1] == 1))[0][0]
            self.config.idx01 = np.where((self.prod_map[:, 0] == 0) & (self.prod_map[:, 1] == 1))[0][0]
        except IndexError:
            print("Warning: Could not find product indices for plots.")
    
    def _populate_product_selectors(self) -> None:
        """Populate product selector dropdowns with all 36 available products."""
        # Generate product labels for all 36 products
        product_labels = []
        for idx, (p0, p1) in enumerate(self.prod_map):
            if p0 == p1:
                # Autocorrelation
                product_labels.append(f"ADC{p0} Auto ({p0},{p1}) [idx:{idx}]")
            else:
                # Cross-correlation
                product_labels.append(f"ADC{p0}/ADC{p1} Cross ({p0},{p1}) [idx:{idx}]")
        
        # Populate all selectors with same options
        for selector in [self.p00_selector, self.p11_selector, self.p01_selector]:
            selector.addItems(product_labels)
        
        # Set default selections
        self.p00_selector.setCurrentIndex(self.config.idx00)  # ADC0 Auto
        self.p11_selector.setCurrentIndex(self.config.idx11)  # ADC1 Auto  
        self.p01_selector.setCurrentIndex(self.config.idx01)  # ADC0/1 Cross
        
        # Connect change signals
        self.p00_selector.currentIndexChanged.connect(self._on_product_selection_changed)
        self.p11_selector.currentIndexChanged.connect(self._on_product_selection_changed)
        self.p01_selector.currentIndexChanged.connect(self._on_product_selection_changed)
        
        print(f"Product selectors populated with {len(product_labels)} available products")
    
    def _on_product_selection_changed(self):
        """Handle product selection changes."""
        if self.udp_receiver:
            # Get currently selected products
            selected_products = [
                self.p00_selector.currentIndex(),
                self.p11_selector.currentIndex(), 
                self.p01_selector.currentIndex()
            ]
            
            # Update decoder with new selection
            self.udp_receiver.decoder.selected_products = selected_products
            
            # Update efficiency display
            unique_products = len(set(selected_products))
            self.efficiency_label.setText(f"Efficiency: {unique_products}/{NPROD} products")
            
            print(f"Updated product selection: {selected_products}")
            print(f"Processing {unique_products} unique products out of {NPROD} total")

    def _calculate_frequency_axis(self, zone: int) -> np.ndarray:
        """Calculate frequency axis for the specified Nyquist zone."""
        base_freq_axis = (FS_HZ / M_FACTOR) * np.arange(NBINS_TOTAL)
        
        if zone == 1:
            return base_freq_axis
        elif zone == 2:
            reversed_axis = base_freq_axis[::-1]
            return reversed_axis + FS_HZ / 2
        elif zone == 3:
            return base_freq_axis + FS_HZ
        else:
            return base_freq_axis

    def _calculate_lag_axis(self) -> np.ndarray:
        """Calculate lag axis in meters."""
        lag_bins = np.arange(NBINS_TOTAL) - NBINS_TOTAL // 2
        lag_time_s = lag_bins / FS_HZ
        c_m_per_s = 299792458.0
        return lag_time_s * c_m_per_s / 2

    def _build_ui(self) -> None:
        """Build the main user interface."""
        self.central_widget = QtWidgets.QWidget()
        self.setCentralWidget(self.central_widget)
        self.main_layout = QtWidgets.QVBoxLayout(self.central_widget)
        
        self._build_toolbar()
        self._build_plot_area()

    def _build_toolbar(self) -> None:
        """Build the toolbar with connection and control widgets."""
        toolbar_layout = QtWidgets.QHBoxLayout()
        
        # Connection controls
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
        
        # Configuration controls
        self.gain_target_spinner = QtWidgets.QDoubleSpinBox()
        self.gain_target_spinner.setRange(0.1, 10.0)
        self.gain_target_spinner.setValue(DEFAULT_GAIN_TARGET)
        self.gain_target_spinner.setDecimals(2)

        self.waterfall_len_spinner = QtWidgets.QSpinBox()
        self.waterfall_len_spinner.setRange(10, 2000)
        self.waterfall_len_spinner.setValue(DEFAULT_WATERFALL_LEN)
        
        # Lag computation controls
        self.enable_lag_checkbox = QtWidgets.QCheckBox("Enable Lag Computation")
        self.enable_lag_checkbox.setToolTip("Enable optimized lag spectrum computation")
        self.enable_lag_checkbox.setChecked(True)
        self.enable_lag_checkbox.stateChanged.connect(self._toggle_lag_computation)
        
        # Nyquist zone selector
        self.nyquist_zone_combo = QtWidgets.QComboBox()
        self.nyquist_zone_combo.addItems(["Zone 1 (0-1600 MHz)", "Zone 2 (1600-3200 MHz)", "Zone 3 (3200-4800 MHz)"])
        self.nyquist_zone_combo.setCurrentIndex(0)
        self.nyquist_zone_combo.currentIndexChanged.connect(self._on_nyquist_zone_changed)
        
        # Waterfall enhancement controls
        self.running_diff_checkbox = QtWidgets.QCheckBox("Running Difference")
        self.running_diff_checkbox.setToolTip("Subtract moving average to highlight changes")
        self.running_diff_checkbox.setChecked(False)
        
        self.diff_window_spinner = QtWidgets.QSpinBox()
        self.diff_window_spinner.setRange(2, 100)
        self.diff_window_spinner.setValue(10)
        self.diff_window_spinner.setPrefix("Window: ")
        self.diff_window_spinner.setSuffix(" frames")
        self.diff_window_spinner.setEnabled(False)
        
        self.fix_color_scale_checkbox = QtWidgets.QCheckBox("Fix Color Scale")
        self.fix_color_scale_checkbox.setToolTip("Lock current color scale")
        self.fix_color_scale_checkbox.setChecked(False)
        
        self.viewbox_mode_checkbox = QtWidgets.QCheckBox("ViewBox Mode")
        self.viewbox_mode_checkbox.setToolTip("Enable box zoom (drag to zoom)")
        self.viewbox_mode_checkbox.setChecked(False)
        self.viewbox_mode_checkbox.stateChanged.connect(self._toggle_viewbox_mode)
        
        # Connect running difference controls
        self.running_diff_checkbox.stateChanged.connect(self._on_running_diff_changed)
        self.diff_window_spinner.valueChanged.connect(self._on_diff_window_changed)
        
        # Product selection controls - allow user to select which products to process/plot
        self.product_group = QtWidgets.QGroupBox("Product Selection")
        product_layout = QtWidgets.QGridLayout(self.product_group)
        
        # Create dropdown for each plot type
        self.p00_selector = QtWidgets.QComboBox()
        self.p11_selector = QtWidgets.QComboBox()
        self.p01_selector = QtWidgets.QComboBox()
        
        # Populate with all available products
        self._populate_product_selectors()
        
        product_layout.addWidget(QtWidgets.QLabel("ADC0 Auto:"), 0, 0)
        product_layout.addWidget(self.p00_selector, 0, 1)
        product_layout.addWidget(QtWidgets.QLabel("ADC1 Auto:"), 0, 2)
        product_layout.addWidget(self.p11_selector, 0, 3)
        product_layout.addWidget(QtWidgets.QLabel("Cross-Corr:"), 0, 4)
        product_layout.addWidget(self.p01_selector, 0, 5)
        
        # Add efficiency label
        self.efficiency_label = QtWidgets.QLabel(f"Efficiency: 3/{NPROD} products")
        self.efficiency_label.setToolTip("Fewer products = faster processing")
        product_layout.addWidget(self.efficiency_label, 0, 6)
        
        # Add widgets to toolbar
        toolbar_layout.addWidget(QtWidgets.QLabel("Serial:"))
        toolbar_layout.addWidget(self.serial_input)
        toolbar_layout.addWidget(self.connect_button)
        toolbar_layout.addSpacing(20)
        toolbar_layout.addWidget(self.calibrate_button)
        toolbar_layout.addWidget(QtWidgets.QLabel("Gain Target:"))
        toolbar_layout.addWidget(self.gain_target_spinner)
        toolbar_layout.addSpacing(20)
        toolbar_layout.addWidget(self.configure_button)
        toolbar_layout.addSpacing(20)
        toolbar_layout.addWidget(QtWidgets.QLabel("Waterfall Length:"))
        toolbar_layout.addWidget(self.waterfall_len_spinner)
        toolbar_layout.addSpacing(20)
        toolbar_layout.addWidget(self.enable_lag_checkbox)
        toolbar_layout.addSpacing(20)
        toolbar_layout.addWidget(QtWidgets.QLabel("Nyquist Zone:"))
        toolbar_layout.addWidget(self.nyquist_zone_combo)
        toolbar_layout.addSpacing(20)
        toolbar_layout.addWidget(self.running_diff_checkbox)
        toolbar_layout.addWidget(self.diff_window_spinner)
        toolbar_layout.addWidget(self.fix_color_scale_checkbox)
        toolbar_layout.addWidget(self.viewbox_mode_checkbox)
        toolbar_layout.addStretch(1)
        
        self.main_layout.addLayout(toolbar_layout)
        
        # Add product selection group as separate row
        self.main_layout.addWidget(self.product_group)

    def _build_plot_area(self) -> None:
        """Build the tabbed widget for plots."""
        self.tabs = QtWidgets.QTabWidget()
        self.main_layout.addWidget(self.tabs)

        # Create tabs
        self.spectra_tab = QtWidgets.QWidget()
        self.waterfall_tab = QtWidgets.QWidget()
        self.lag_spectra_tab = QtWidgets.QWidget()
        self.lag_waterfall_tab = QtWidgets.QWidget()

        self.tabs.addTab(self.spectra_tab, "Spectra")
        self.tabs.addTab(self.waterfall_tab, "Waterfall")
        self.tabs.addTab(self.lag_spectra_tab, "Lag Spectra")
        self.tabs.addTab(self.lag_waterfall_tab, "Lag Waterfall")

        # Setup plots
        self._setup_spectra_plots()
        self._setup_waterfall_plots()
        self._setup_lag_spectra_plots()
        self._setup_lag_waterfall_plots()

    def _setup_spectra_plots(self) -> None:
        """Set up the plots for the Spectra tab."""
        layout = QtWidgets.QGridLayout(self.spectra_tab)
        
        self.plots = {}
        colors = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728']  # Tab10 colors

        # P00 Plot (ADC0 Auto)
        p00_plot = pg.PlotWidget()
        p00_plot.setLabel('bottom', 'Frequency', units='Hz')
        p00_plot.setLabel('left', 'Power')
        p00_plot.showGrid(x=True, y=True)
        self.plots['p00'] = p00_plot.plot(pen=colors[0], name='ADC0 Auto')
        p00_plot.addLegend()
        layout.addWidget(p00_plot, 0, 0)

        # P11 Plot (ADC1 Auto)
        p11_plot = pg.PlotWidget()
        p11_plot.setLabel('bottom', 'Frequency', units='Hz')
        p11_plot.setLabel('left', 'Power')
        p11_plot.showGrid(x=True, y=True)
        self.plots['p11'] = p11_plot.plot(pen=colors[1], name='ADC1 Auto')
        p11_plot.addLegend()
        layout.addWidget(p11_plot, 0, 1)

        # P01 Magnitude Plot
        p01_abs_plot = pg.PlotWidget()
        p01_abs_plot.setLabel('bottom', 'Frequency', units='Hz')
        p01_abs_plot.setLabel('left', 'Cross-Correlation Magnitude')
        p01_abs_plot.showGrid(x=True, y=True)
        self.plots['p01_abs'] = p01_abs_plot.plot(pen=colors[2], name='ADC0/1 Cross |Amp|')
        p01_abs_plot.addLegend()
        layout.addWidget(p01_abs_plot, 1, 0)

        # P01 Real Plot
        p01_real_plot = pg.PlotWidget()
        p01_real_plot.setLabel('bottom', 'Frequency', units='Hz')
        p01_real_plot.setLabel('left', 'Cross-Correlation Real Part')
        p01_real_plot.showGrid(x=True, y=True)
        self.plots['p01_real'] = p01_real_plot.plot(pen=colors[3], name='ADC0/1 Cross Real')
        p01_real_plot.addLegend()
        layout.addWidget(p01_real_plot, 1, 1)

    def _setup_waterfall_plots(self) -> None:
        """Set up the waterfall plots."""
        layout = QtWidgets.QGridLayout(self.waterfall_tab)
        self.waterfall_plots = {}
        self.waterfall_images = {}
        
        viridis_cm = pg.colormap.get('viridis')
        
        titles = ["ADC0 Auto", "ADC1 Auto", "Cross-Corr Real", "Cross-Corr Imag"]
        keys = ['p00', 'p11', 'p01_real', 'p01_imag']
        
        for i, (title, key) in enumerate(zip(titles, keys)):
            row, col = divmod(i, 2)
            
            plot = pg.PlotWidget()
            plot.setLabel('left', 'Time (frames ago)')
            plot.setLabel('bottom', 'Frequency', units='Hz')
            plot.invertY(True)  # Newest data at top
            
            img_item = pg.ImageItem()
            plot.addItem(img_item)
            img_item.setLookupTable(viridis_cm.getLookupTable())
            
            self.waterfall_plots[key] = plot
            self.waterfall_images[key] = img_item
            
            # Add title
            title_label = QtWidgets.QLabel(title)
            title_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            
            container = QtWidgets.QWidget()
            container_layout = QtWidgets.QVBoxLayout(container)
            container_layout.addWidget(title_label)
            container_layout.addWidget(plot)
            
            layout.addWidget(container, row, col)

    def _setup_lag_spectra_plots(self) -> None:
        """Set up the lag spectra plots."""
        layout = QtWidgets.QGridLayout(self.lag_spectra_tab)
        
        self.lag_plots = {}
        colors = ['#1f77b4', '#ff7f0e', '#2ca02c']

        # Lag P00 Plot
        lag_p00_plot = pg.PlotWidget()
        lag_p00_plot.setLabel('bottom', 'Distance', units='m')
        lag_p00_plot.setLabel('left', 'Power')
        lag_p00_plot.showGrid(x=True, y=True)
        self.lag_plots['p00'] = lag_p00_plot.plot(pen=colors[0], name='ADC0 Auto Lag')
        lag_p00_plot.addLegend()
        layout.addWidget(lag_p00_plot, 0, 0)

        # Lag P11 Plot
        lag_p11_plot = pg.PlotWidget()
        lag_p11_plot.setLabel('bottom', 'Distance', units='m')
        lag_p11_plot.setLabel('left', 'Power')
        lag_p11_plot.showGrid(x=True, y=True)
        self.lag_plots['p11'] = lag_p11_plot.plot(pen=colors[1], name='ADC1 Auto Lag')
        lag_p11_plot.addLegend()
        layout.addWidget(lag_p11_plot, 0, 1)

        # Lag P01 Magnitude Plot
        lag_p01_plot = pg.PlotWidget()
        lag_p01_plot.setLabel('bottom', 'Distance', units='m')
        lag_p01_plot.setLabel('left', 'Cross-Correlation Power')
        lag_p01_plot.showGrid(x=True, y=True)
        self.lag_plots['p01_mag'] = lag_p01_plot.plot(pen=colors[2], name='ADC0/1 Cross Lag')
        lag_p01_plot.addLegend()
        layout.addWidget(lag_p01_plot, 1, 0, 1, 2)  # Span both columns

    def _setup_lag_waterfall_plots(self) -> None:
        """Set up the lag waterfall plots."""
        layout = QtWidgets.QGridLayout(self.lag_waterfall_tab)
        self.lag_waterfall_plots = {}
        self.lag_waterfall_images = {}
        
        viridis_cm = pg.colormap.get('viridis')
        
        titles = ["ADC0 Auto Lag", "ADC1 Auto Lag", "Cross-Corr Lag"]
        keys = ['lag_p00', 'lag_p11', 'lag_p01_mag']
        
        for i, (title, key) in enumerate(zip(titles, keys)):
            if i < 2:
                row, col = 0, i
            else:
                row, col = 1, 0
            
            plot = pg.PlotWidget()
            plot.setLabel('left', 'Time (frames ago)')
            plot.setLabel('bottom', 'Distance', units='m')
            plot.invertY(True)
            
            img_item = pg.ImageItem()
            plot.addItem(img_item)
            img_item.setLookupTable(viridis_cm.getLookupTable())
            
            self.lag_waterfall_plots[key] = plot
            self.lag_waterfall_images[key] = img_item
            
            # Add title
            title_label = QtWidgets.QLabel(title)
            title_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            
            container = QtWidgets.QWidget()
            container_layout = QtWidgets.QVBoxLayout(container)
            container_layout.addWidget(title_label)
            container_layout.addWidget(plot)
            
            layout.addWidget(container, row, col)

    def _setup_status_bar(self) -> None:
        """Setup the status bar."""
        self.statusBar = QtWidgets.QStatusBar()
        self.setStatusBar(self.statusBar)
        self.status_label = QtWidgets.QLabel("Ready. Please connect to a board.")
        self.metrics_label = QtWidgets.QLabel("")
        self.statusBar.addWidget(self.status_label, 1)
        self.statusBar.addWidget(self.metrics_label)

    def connect_to_board(self) -> None:
        """Connect to the FPGA board using the original pattern."""
        try:
            self.serial_number = self.serial_input.text()
            self.status_label.setText(f"Connecting to CRS board with serial: {self.serial_number}...")
            QtWidgets.QApplication.processEvents()
            
            # Use the original connection pattern
            hardware_map = f'crs {self.serial_number}'
            self.pocket_correlator = POCKET_CORRELATOR(hwm=hardware_map, prog=2, stderr_log_level='debug')
            
            self.setWindowTitle(f"Periscope Correlator V2 - Connected to {self.serial_number}")
            self.status_label.setText("Connection successful. Ready to calibrate and configure.")
            
            # Update UI
            self.connect_button.setText("Disconnect")
            self.connect_button.clicked.disconnect()
            self.connect_button.clicked.connect(self.disconnect_from_board)
            self.calibrate_button.setEnabled(True)
            self.configure_button.setEnabled(True)
            self.serial_input.setEnabled(False)
            
            print("Connection successful.")
            
        except Exception as e:
            print(f"Failed to connect: {e}")
            self.status_label.setText(f"Failed to connect: {e}")
            QtWidgets.QMessageBox.critical(self, "Connection Error", str(e))

    def calibrate_gains(self) -> None:
        """Perform gain calibration using the original method."""
        if not self.pocket_correlator: 
            return
            
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
        """Configure the correlator and start streaming with optimized backend."""
        if not self.pocket_correlator: 
            return
            
        try:
            self.status_label.setText("Configuring correlator...")
            QtWidgets.QApplication.processEvents()
            
            # Configure using original method
            self.pocket_correlator.configure_corr(
                autocorr_only=0, 
                no_accum=0, 
                n_firmware_frames=self.config.n_firmware_frames - 1
            )
            
            if self.pocket_correlator.ucap.OUTPUT_SOURCE_SEL != 1:
                print("Setting UCAP output source to Correlator (1)...")
                self.pocket_correlator.ucap.OUTPUT_SOURCE_SEL = 1
            
            # Get the socket from the correlator (original pattern)
            sock = self.pocket_correlator.corr_receiver.socket
            sock.settimeout(0.1)
            
            # Update configuration
            self.config.waterfall_len = self.waterfall_len_spinner.value()
            
            # Create our optimized components
            self.data_buffer = DataBuffer(waterfall_len=self.config.waterfall_len)
            
            # Get the packet dtype for CRS compatibility
            from pychfpga.fpga_firmware.chfpga.x_engine.UCORR import UCorrFrameReceiver
            ucorr_receiver = UCorrFrameReceiver(None, 8, Nbins=NBINS_TOTAL, NCORR=NPROD)
            packet_dtype = ucorr_receiver.packet_dtype
            
            # Create optimized UDP receiver with proper config and CRS packet format
            udp_config = {
                'gain_factor': self.config.gain_factor,
                'n_firmware_frames': self.config.n_firmware_frames
            }
            
            self.udp_receiver = HighPerformanceUDPReceiver(sock, udp_config, self.data_buffer, packet_dtype)
            self.udp_receiver.set_frame_callback(self._on_frame_received)
            
            # Create optimized lag computer if enabled
            if self.enable_lag_checkbox.isChecked():
                self.lag_computer = OptimizedLagComputer(self.data_buffer)
                self.lag_computer.set_lag_callback(self._on_lag_computed)
                self.lag_computer.set_enabled(True)
                self.lag_computer.start()
            
            # Start UDP receiver
            self.udp_receiver.start()
            
            # Update UI
            self.configure_button.setText("Stop Streaming")
            self.configure_button.clicked.disconnect()
            self.configure_button.clicked.connect(self.stop_streaming)
            self.calibrate_button.setEnabled(False)
            
            self.status_label.setText("Correlator V2 configured. High-performance streaming active...")
            print("Correlator V2 configured and streaming with optimized backend.")
            
        except Exception as e:
            print(f"Correlator configuration failed: {e}")
            self.status_label.setText(f"Configuration failed: {e}")
            QtWidgets.QMessageBox.critical(self, "Configuration Error", str(e))

    def stop_streaming(self) -> None:
        """Stop the data stream."""
        self.status_label.setText("Stopping data stream...")
        QtWidgets.QApplication.processEvents()
        
        # Stop components
        if self.udp_receiver:
            self.udp_receiver.stop()
            if self.udp_receiver.is_alive():
                self.udp_receiver.join(timeout=2)
        
        if self.lag_computer:
            self.lag_computer.stop()
            if self.lag_computer.is_alive():
                self.lag_computer.join(timeout=2)
        
        # Reset UI
        self.configure_button.setText("Configure Correlator")
        self.configure_button.clicked.disconnect()
        self.configure_button.clicked.connect(self.configure_correlator)
        self.calibrate_button.setEnabled(True)
        
        self.status_label.setText("Data stream stopped. Ready to reconfigure.")
        print("Data stream stopped.")

    def disconnect_from_board(self) -> None:
        """Disconnect from the board."""
        self.stop_streaming()
        
        if self.pocket_correlator: 
            self.pocket_correlator = None
        
        self.setWindowTitle("Periscope Correlator V2 - Not Connected")
        self.connect_button.setText("Connect")
        self.connect_button.clicked.disconnect()
        self.connect_button.clicked.connect(self.connect_to_board)
        
        self.serial_input.setEnabled(True)
        self.calibrate_button.setEnabled(False)
        self.configure_button.setEnabled(False)
        self.status_label.setText("Disconnected. Ready to connect.")
        print("Disconnected.")

    def _update_gain_factor(self) -> None:
        """Calculate gain normalization factor from computed gains."""
        if self.computed_gains is None:
            self.config.gain_factor = None
            return
        
        print("Pre-calculating gain normalization factor...")
        gains = self.computed_gains.copy()
        gains[gains == 0] = 1e-9
        
        p0_indices = self.prod_map[:, 0]
        p1_indices = self.prod_map[:, 1]
        
        gains_p0 = gains[p0_indices]
        gains_p1 = gains[p1_indices]
        gain_product = gains_p0 * gains_p1
        
        # Use logarithmic computation for numerical stability
        log_scale_factor = 2 * 44 * np.log(2)
        log_gain_product = np.log(gain_product)
        log_gain_factor = log_scale_factor - log_gain_product
        
        self.config.gain_factor = np.exp(log_gain_factor).astype(np.complex128).T
        print("Gain normalization factor updated.")

    def _toggle_lag_computation(self, state: int) -> None:
        """Toggle lag computation on/off."""
        enabled = (state == QtCore.Qt.CheckState.Checked)
        if self.lag_computer:
            self.lag_computer.set_enabled(enabled)
            print(f"Lag computation {'enabled' if enabled else 'disabled'}")

    def _on_nyquist_zone_changed(self, index: int) -> None:
        """Handle Nyquist zone change."""
        zone = index + 1
        self.freq_axis = self._calculate_frequency_axis(zone)
        print(f"Switched to Nyquist Zone {zone}")
        self._update_plots()

    def _on_frame_received(self, frame: FrameData) -> None:
        """Callback for when a frame is received."""
        # Notify lag computer of new data
        if self.lag_computer and self.lag_computer.enabled.is_set():
            self.lag_computer.notify_new_data()
        
        # Emit signal to update plots
        self.data_ready.emit()

    def _on_lag_computed(self, lag_data: LagData) -> None:
        """Callback for when lag computation completes."""
        # This will trigger plot updates via the data buffer
        pass

    def _toggle_viewbox_mode(self, state: int) -> None:
        """Toggle between pan mode and zoom mode."""
        is_checked = (state == QtCore.Qt.CheckState.Checked)
        
        # Define the mouse mode
        if is_checked:
            mode = pg.ViewBox.RectMode  # Box zoom mode
        else:
            mode = pg.ViewBox.PanMode   # Pan mode (default)
        
        # Apply to all plot widgets
        for plot in self.waterfall_plots.values():
            viewbox = plot.getViewBox()
            viewbox.setMouseMode(mode)
        
        for plot in self.lag_waterfall_plots.values():
            viewbox = plot.getViewBox()
            viewbox.setMouseMode(mode)
        
        print(f"ViewBox mode: {'Zoom' if is_checked else 'Pan'}")

    def _on_running_diff_changed(self, state: int) -> None:
        """Handle running difference mode toggle."""
        enabled = (state == QtCore.Qt.CheckState.Checked)
        self.diff_window_spinner.setEnabled(enabled)
        
        print(f"Running difference mode {'enabled' if enabled else 'disabled'}")

    def _on_diff_window_changed(self, value: int) -> None:
        """Handle running difference window size change."""
        print(f"Running difference window size: {value} frames")

    def _update_plots(self) -> None:
        """Update plots with smart tab-based updates (only update visible tab)."""
        if not self.data_buffer:
            return

        current_tab = self.tabs.currentIndex()
        
        # Smart update: only update currently visible tab for better performance
        if current_tab == 0:  # Spectra tab
            self._update_spectra_plots()
        elif current_tab == 1:  # Waterfall tab
            self._update_waterfall_plots()
        elif current_tab == 2:  # Lag Spectra tab
            self._update_lag_spectra_plots()
        elif current_tab == 3:  # Lag Waterfall tab
            self._update_lag_waterfall_plots()
        
        # Always update performance metrics
        self._update_performance_display()

    def _update_spectra_plots(self) -> None:
        """Update spectra line plots."""
        if not self.data_buffer:
            return
            
        data = self.data_buffer.get_all_data()
        
        # Update plots
        self.plots['p00'].setData(x=self.freq_axis, y=data['p00'])
        self.plots['p11'].setData(x=self.freq_axis, y=data['p11'])
        
        # Cross-correlation magnitude
        p01_complex = data['p01_real'] + 1j * data['p01_imag']
        p01_abs = np.abs(p01_complex)
        self.plots['p01_abs'].setData(x=self.freq_axis, y=p01_abs)
        self.plots['p01_real'].setData(x=self.freq_axis, y=data['p01_real'])

    def _update_waterfall_plots(self) -> None:
        """Update waterfall plots."""
        if not self.data_buffer:
            return
            
        wf_data = self.data_buffer.get_waterfall_data()
        
        if wf_data['frames_written'] == 0:
            return
        
        # Calculate frequency range
        freq_min = self.freq_axis[0]
        freq_max = self.freq_axis[-1]
        
        # Update each waterfall
        for key in ['p00', 'p11', 'p01_real', 'p01_imag']:
            img = self.waterfall_images[key]
            data = wf_data[f'wf_{key}']
            
            img.setImage(data.T, autoLevels=True)
            img.setRect(QtCore.QRectF(freq_min, 0, freq_max - freq_min, self.config.waterfall_len))

    def _update_lag_spectra_plots(self) -> None:
        """Update lag spectra plots."""
        if not self.data_buffer:
            return
            
        data = self.data_buffer.get_all_data()
        
        # Only show positive lags for autocorrelations
        positive_mask = self.lag_axis >= 0
        positive_lags = self.lag_axis[positive_mask]
        
        # Update lag plots
        self.lag_plots['p00'].setData(x=positive_lags, y=data['lag_p00'][positive_mask])
        self.lag_plots['p11'].setData(x=positive_lags, y=data['lag_p11'][positive_mask])
        self.lag_plots['p01_mag'].setData(x=self.lag_axis, y=data['lag_p01_mag'])

    def _update_lag_waterfall_plots(self) -> None:
        """Update lag waterfall plots."""
        if not self.data_buffer:
            return
            
        lag_wf_data = self.data_buffer.get_lag_waterfall_data()
        
        if lag_wf_data['lag_frames_written'] == 0:
            return
        
        # Calculate lag range
        lag_min = self.lag_axis[0]
        lag_max = self.lag_axis[-1]
        
        # Update lag waterfalls
        lag_keys = {
            'lag_p00': 'lag_wf_p00',
            'lag_p11': 'lag_wf_p11', 
            'lag_p01_mag': 'lag_wf_p01_mag'
        }
        
        for key in ['lag_p00', 'lag_p11', 'lag_p01_mag']:
            img = self.lag_waterfall_images[key]
            # Use correct key from data buffer
            data_key = lag_keys[key]
            if data_key not in lag_wf_data:
                continue  # Skip if no data yet
            data = lag_wf_data[data_key]
            
            if key in ['lag_p00', 'lag_p11']:
                # For autocorrelations, show only positive lags
                positive_start = NBINS_TOTAL // 2
                plot_data = data[:, positive_start:]
                # Set image first, then rectangle
                img.setImage(plot_data.T, autoLevels=True)
                img.setRect(QtCore.QRectF(0, 0, lag_max, self.config.waterfall_len))
            else:
                # For cross-correlation, show full range
                plot_data = data
                # Set image first, then rectangle
                img.setImage(plot_data.T, autoLevels=True)
                img.setRect(QtCore.QRectF(lag_min, 0, lag_max - lag_min, self.config.waterfall_len))

    def _update_performance_display(self) -> None:
        """Update performance metrics display."""
        if not self.udp_receiver:
            return
        
        udp_stats = self.udp_receiver.get_stats()
        metrics_text = f"Frames: {udp_stats['frames_received']}, FPS: {udp_stats['fps']:.2f}"
        
        if self.lag_computer:
            lag_stats = self.lag_computer.get_stats()
            metrics_text += f" | Lag: {lag_stats['computations']}, Rate: {lag_stats['fps']:.1f}"
        
        self.metrics_label.setText(metrics_text)

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        """Handle window close event."""
        self.stop_streaming()
        self.disconnect_from_board()
        event.accept()

def main():
    """Main function to run the application."""
    app = QtWidgets.QApplication(sys.argv)
    
    # Apply dark theme
    app.setStyleSheet("""
        QMainWindow { background-color: #2E2E2E; color: #D0D0D0; }
        QLabel, QCheckBox { color: #D0D0D0; font-size: 14px; }
        QPushButton { background-color: #555555; color: #D0D0D0; border: 1px solid #777777; padding: 5px; min-width: 80px; }
        QPushButton:hover { background-color: #666666; }
        QPushButton:pressed { background-color: #444444; }
        QLineEdit { background-color: #444444; color: #D0D0D0; border: 1px solid #777777; padding: 5px; }
        QDoubleSpinBox, QSpinBox { background-color: #444444; color: #D0D0D0; border: 1px solid #777777; padding: 3px; }
        QTabWidget::pane { border: 1px solid #777777; }
        QTabBar::tab { background: #555555; color: #D0D0D0; padding: 8px; }
        QTabBar::tab:selected { background: #666666; }
    """)
    
    win = PeriscopeCorrelatorV2()
    sys.exit(app.exec_())

if __name__ == '__main__':
    main()
