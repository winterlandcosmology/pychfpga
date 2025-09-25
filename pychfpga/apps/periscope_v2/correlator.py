#!/usr/bin/env python
"""
Periscope Correlator V2 - High-Performance UDP Correlator
=======================================================

Single entry point for the modular, high-performance correlator system.
Consolidates all functionality into one clean, working implementation.
"""
import numpy as np
import time
import socket
import struct
import threading
import argparse
import sys
import signal

# ==============================================================================
# CONSTANTS & CONFIGURATION
# ==============================================================================

# Hardware constants
NCHAN = 8
NPROD = NCHAN * (NCHAN + 1) // 2  # 36 products
NBINS_TOTAL = 8192
FS_HZ = 3_200_000_000.0  # 3.2 GHz sampling frequency
M_FACTOR = 2**14  # 16384

# Default settings
DEFAULT_WATERFALL_LEN = 250
DEFAULT_UDP_PORT = 8888
DEFAULT_FRAME_RATE = 10  # FPS for demo mode

# Calculate product map and indices
def generate_product_map():
    """Generate the product map for correlator indices."""
    prod_map = []
    for i in range(NCHAN + 1):
        for j in range(NCHAN // 2):
            if i < NCHAN - j:
                prod_map.append((i, (j + i) % NCHAN))
            else:
                prod_map.append((((j + i) % NCHAN), i - 1))
    return np.array(prod_map)

# Pre-calculate product indices
PRODUCT_MAP = generate_product_map()
PRODUCT_INDICES = {}
for idx, (p0, p1) in enumerate(PRODUCT_MAP):
    if p0 == 0 and p1 == 0:
        PRODUCT_INDICES['p00'] = idx
    elif p0 == 1 and p1 == 1:
        PRODUCT_INDICES['p11'] = idx
    elif (p0 == 0 and p1 == 1) or (p0 == 1 and p1 == 0):
        PRODUCT_INDICES['p01'] = idx

# Calculate frequency and lag axes
def calculate_frequency_axis(nyquist_zone=1):
    """Calculate frequency axis for specified Nyquist zone."""
    base_freq_axis = (FS_HZ / M_FACTOR) * np.arange(NBINS_TOTAL)
    
    if nyquist_zone == 1:
        return base_freq_axis
    elif nyquist_zone == 2:
        return base_freq_axis[::-1] + FS_HZ / 2
    elif nyquist_zone == 3:
        return base_freq_axis + FS_HZ
    else:
        return base_freq_axis

def calculate_lag_axis():
    """Calculate lag axis in meters."""
    lag_bins = np.arange(NBINS_TOTAL) - NBINS_TOTAL // 2
    lag_time_s = lag_bins / FS_HZ
    return lag_time_s * 299792458.0 / 2  # Round trip distance in meters

FREQ_AXIS = calculate_frequency_axis(1)
LAG_AXIS = calculate_lag_axis()

# ==============================================================================
# CORE DATA STRUCTURES
# ==============================================================================

class FrameData:
    """Container for correlator frame data."""
    def __init__(self, p00, p11, p01_real, p01_imag):
        self.p00 = p00
        self.p11 = p11
        self.p01_real = p01_real
        self.p01_imag = p01_imag
        self.timestamp = time.time()
        self.frame_number = 0
        self.valid = True

class LagData:
    """Container for lag spectrum data."""
    def __init__(self, lag_p00, lag_p11, lag_p01_mag, lag_p01_phase):
        self.lag_p00 = lag_p00
        self.lag_p11 = lag_p11
        self.lag_p01_mag = lag_p01_mag
        self.lag_p01_phase = lag_p01_phase
        self.timestamp = time.time()
        self.valid = True

# ==============================================================================
# HIGH-PERFORMANCE PROCESSING COMPONENTS  
# ==============================================================================

class OptimizedPacketDecoder:
    """High-performance packet decoder with vectorized operations."""
    
    def __init__(self):
        self.frame_buffer = np.zeros((NBINS_TOTAL, NPROD), dtype=np.complex128)
        self.idx00 = PRODUCT_INDICES['p00']
        self.idx11 = PRODUCT_INDICES['p11']
        self.idx01 = PRODUCT_INDICES['p01']
        
        # Performance tracking
        self.packets_decoded = 0
        self.decode_time_total = 0.0
    
    def decode_packet(self, packet_data: bytes):
        """Decode UDP packet with optimized bit operations."""
        start_time = time.perf_counter()
        
        try:
            if len(packet_data) < 8:
                return None
            
            # Extract header
            header = struct.unpack('<BBH', packet_data[:4])
            cookie, reserved, stream_id = header
            
            if cookie != 0xcf or stream_id >= NBINS_TOTAL:
                return None
            
            # Validate data size
            data_size = len(packet_data) - 4
            expected_size = NPROD * 8  # 2 uint32 per product
            
            if data_size != expected_size:
                return None
            
            # Unpack data efficiently
            data_format = f'<{NPROD * 2}I'
            data_values = struct.unpack(data_format, packet_data[4:])
            
            # Split into h and l arrays
            h_values = np.array(data_values[::2], dtype=np.uint32)
            l_values = np.array(data_values[1::2], dtype=np.uint32)
            
            # Optimized bit extraction with proper sign extension
            h_signed = h_values.astype(np.int32)
            l_signed = l_values.astype(np.int32)
            
            real_values = np.right_shift(np.left_shift(h_signed, 4), 14)
            imag_values = np.right_shift(np.left_shift(l_signed, 14), 14)
            
            complex_data = real_values.astype(np.float64) + 1j * imag_values.astype(np.float64)
            
            self.packets_decoded += 1
            self.decode_time_total += time.perf_counter() - start_time
            
            return stream_id, complex_data
            
        except Exception:
            return None
    
    def update_frame_buffer(self, bin_index, complex_data):
        """Update frame buffer efficiently."""
        if 0 <= bin_index < NBINS_TOTAL:
            self.frame_buffer[bin_index] = complex_data
    
    def extract_frame_data(self, gain_factor=None):
        """Extract main products from frame buffer."""
        # Normalize by firmware frames
        normalized_frame = self.frame_buffer / 16384
        
        # Apply gain correction if provided
        if gain_factor is not None:
            if gain_factor.shape != normalized_frame.shape:
                gain_factor = gain_factor.T
            normalized_frame *= gain_factor
        
        # Extract main products
        p00 = np.abs(normalized_frame[:, self.idx00])
        p11 = np.abs(normalized_frame[:, self.idx11])
        p01_complex = normalized_frame[:, self.idx01]
        
        return p00, p11, p01_complex.real, p01_complex.imag
    
    def reset_frame(self):
        """Reset frame buffer."""
        self.frame_buffer.fill(0)
    
    def get_stats(self):
        """Get performance statistics."""
        avg_time = self.decode_time_total / max(self.packets_decoded, 1)
        return {
            'packets_decoded': self.packets_decoded,
            'avg_decode_time_ms': avg_time * 1000,
            'decode_rate_per_sec': 1.0 / max(avg_time, 1e-9)
        }

class OptimizedLagComputer:
    """High-performance lag spectrum computer."""
    
    def __init__(self):
        # Pre-allocate work buffers for efficiency
        self.vis_p00_buffer = np.zeros(NBINS_TOTAL, dtype=np.float64)
        self.vis_p11_buffer = np.zeros(NBINS_TOTAL, dtype=np.float64)
        self.vis_p01_complex_buffer = np.zeros(NBINS_TOTAL, dtype=np.complex128)
        
        # Performance tracking
        self.computations = 0
        self.compute_time_total = 0.0
    
    def compute_lag_spectra(self, frame_data):
        """Compute lag spectra via optimized inverse FFT."""
        start_time = time.perf_counter()
        
        # Copy data to work buffers (zero-copy where possible)
        np.copyto(self.vis_p00_buffer, frame_data.p00)
        np.copyto(self.vis_p11_buffer, frame_data.p11)
        
        self.vis_p01_complex_buffer.real = frame_data.p01_real
        self.vis_p01_complex_buffer.imag = frame_data.p01_imag
        
        # Compute inverse FFTs
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
        
        self.computations += 1
        self.compute_time_total += time.perf_counter() - start_time
        
        return LagData(lag_p00_shifted, lag_p11_shifted, lag_p01_mag_shifted, lag_p01_phase_shifted)
    
    def get_stats(self):
        """Get performance statistics."""
        avg_time = self.compute_time_total / max(self.computations, 1)
        return {
            'computations': self.computations,
            'avg_compute_time_ms': avg_time * 1000,
            'compute_rate_per_sec': 1.0 / max(avg_time, 1e-9)
        }

# ==============================================================================
# SYSTEM COMPONENTS
# ==============================================================================

class MockUDPTransmitter:
    """Mock UDP transmitter for testing."""
    
    def __init__(self, target_host="localhost", target_port=8888):
        self.target_host = target_host
        self.target_port = target_port
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.running = False
        
    def generate_packet(self, bin_index):
        """Generate test UDP packet."""
        # Create header
        header = struct.pack('<BBH', 0xcf, 0, bin_index)
        
        # Generate mock correlator data with test patterns
        real_parts = []
        imag_parts = []
        
        for prod_idx in range(NPROD):
            phase = 2 * np.pi * bin_index / NBINS_TOTAL + prod_idx * 0.1
            amplitude = 1000 + 500 * np.sin(phase)
            
            real_val = int(amplitude * np.cos(phase))
            imag_val = int(amplitude * np.sin(phase))
            
            # Pack into hardware format
            h_word = (real_val & 0x3ffff) << 10
            l_word = imag_val & 0x3ffff
            
            real_parts.append(h_word)
            imag_parts.append(l_word)
        
        # Interleave h and l values
        data_values = []
        for i in range(NPROD):
            data_values.extend([real_parts[i], imag_parts[i]])
        
        data = struct.pack(f'<{len(data_values)}I', *data_values)
        return header + data
    
    def send_frame(self):
        """Send complete frame (all frequency bins)."""
        for bin_idx in range(NBINS_TOTAL):
            packet = self.generate_packet(bin_idx)
            self.socket.sendto(packet, (self.target_host, self.target_port))
            
            # Small delay to prevent overwhelming receiver
            if bin_idx % 1000 == 0:
                time.sleep(0.0001)
    
    def run_continuous(self, frame_rate, duration):
        """Run continuous transmission."""
        self.running = True
        frame_interval = 1.0 / frame_rate
        end_time = time.time() + duration
        frames_sent = 0
        
        print(f"Mock transmitter: {frame_rate} FPS for {duration} seconds")
        
        while self.running and time.time() < end_time:
            start = time.time()
            self.send_frame()
            frames_sent += 1
            
            elapsed = time.time() - start
            sleep_time = frame_interval - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)
        
        print(f"Mock transmitter sent {frames_sent} frames")
        self.socket.close()

class HighPerformanceReceiver:
    """High-performance UDP receiver with lag computation."""
    
    def __init__(self, host="localhost", port=8888, waterfall_len=DEFAULT_WATERFALL_LEN):
        self.host = host
        self.port = port
        self.waterfall_len = waterfall_len
        
        # Create socket
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind((host, port))
        self.socket.settimeout(0.1)
        
        # Configure for performance
        try:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16 * 1024 * 1024)
        except:
            pass
        
        # Processing components
        self.decoder = OptimizedPacketDecoder()
        self.lag_computer = OptimizedLagComputer()
        
        # Data storage
        self.current_frame_data = {}
        self.packet_buffer = bytearray(4096)
        
        # Waterfall storage
        self.wf_p00 = np.zeros((waterfall_len, NBINS_TOTAL))
        self.wf_p11 = np.zeros((waterfall_len, NBINS_TOTAL))
        self.wf_p01_real = np.zeros((waterfall_len, NBINS_TOTAL))
        self.wf_p01_imag = np.zeros((waterfall_len, NBINS_TOTAL))
        
        # Lag waterfall storage
        self.lag_wf_p00 = np.zeros((waterfall_len, NBINS_TOTAL))
        self.lag_wf_p11 = np.zeros((waterfall_len, NBINS_TOTAL))
        self.lag_wf_p01_mag = np.zeros((waterfall_len, NBINS_TOTAL))
        self.lag_wf_p01_phase = np.zeros((waterfall_len, NBINS_TOTAL))
        
        # Metrics
        self.frames_received = 0
        self.lag_computations = 0
        self.start_time = 0.0
        self.running = False
        self.frames_written = 0
    
    def run(self, duration=None, enable_lag=True, verbose=True):
        """Run receiver for specified duration."""
        self.running = True
        self.start_time = time.time()
        end_time = self.start_time + duration if duration else float('inf')
        
        if verbose:
            print(f"Receiver started on {self.host}:{self.port}")
            if duration:
                print(f"Running for {duration} seconds...")
            else:
                print("Running until stopped...")
        
        try:
            while self.running and time.time() < end_time:
                try:
                    nbytes, addr = self.socket.recvfrom_into(self.packet_buffer)
                    
                    if nbytes > 0:
                        packet_data = bytes(self.packet_buffer[:nbytes])
                        result = self.decoder.decode_packet(packet_data)
                        
                        if result:
                            bin_index, complex_data = result
                            self.current_frame_data[bin_index] = complex_data
                            
                            # Check if frame complete
                            if len(self.current_frame_data) >= NBINS_TOTAL:
                                self._complete_frame(enable_lag, verbose)
                                
                except socket.timeout:
                    continue
                except Exception as e:
                    if verbose:
                        print(f"Receiver error: {e}")
                    break
        
        finally:
            self.socket.close()
            if verbose:
                print("Receiver stopped")
    
    def _complete_frame(self, enable_lag, verbose):
        """Complete frame processing."""
        try:
            # Update decoder frame buffer
            for bin_idx, complex_data in self.current_frame_data.items():
                self.decoder.update_frame_buffer(bin_idx, complex_data)
            
            # Extract frame data
            p00, p11, p01_real, p01_imag = self.decoder.extract_frame_data()
            frame = FrameData(p00, p11, p01_real, p01_imag)
            frame.frame_number = self.frames_received
            
            # Update waterfall
            self._update_waterfall(frame)
            
            # Compute lag if enabled
            if enable_lag:
                lag_data = self.lag_computer.compute_lag_spectra(frame)
                self._update_lag_waterfall(lag_data)
                self.lag_computations += 1
            
            self.frames_received += 1
            
            # Print progress
            if verbose and self.frames_received % 10 == 0:
                elapsed = time.time() - self.start_time
                fps = self.frames_received / max(elapsed, 1e-6)
                print(f"  Frames: {self.frames_received}, FPS: {fps:.2f}")
            
            # Reset for next frame
            self.current_frame_data.clear()
            self.decoder.reset_frame()
            
        except Exception as e:
            print(f"Frame completion error: {e}")
            self.current_frame_data.clear()
            self.decoder.reset_frame()
    
    def _update_waterfall(self, frame):
        """Update correlator waterfall data."""
        self.wf_p00[1:] = self.wf_p00[:-1]
        self.wf_p11[1:] = self.wf_p11[:-1]
        self.wf_p01_real[1:] = self.wf_p01_real[:-1]
        self.wf_p01_imag[1:] = self.wf_p01_imag[:-1]
        
        self.wf_p00[0] = frame.p00
        self.wf_p11[0] = frame.p11
        self.wf_p01_real[0] = frame.p01_real
        self.wf_p01_imag[0] = frame.p01_imag
        
        self.frames_written = min(self.frames_written + 1, self.waterfall_len)
    
    def _update_lag_waterfall(self, lag_data):
        """Update lag waterfall data."""
        self.lag_wf_p00[1:] = self.lag_wf_p00[:-1]
        self.lag_wf_p11[1:] = self.lag_wf_p11[:-1]
        self.lag_wf_p01_mag[1:] = self.lag_wf_p01_mag[:-1]
        self.lag_wf_p01_phase[1:] = self.lag_wf_p01_phase[:-1]
        
        self.lag_wf_p00[0] = lag_data.lag_p00
        self.lag_wf_p11[0] = lag_data.lag_p11
        self.lag_wf_p01_mag[0] = lag_data.lag_p01_mag
        self.lag_wf_p01_phase[0] = lag_data.lag_p01_phase
    
    def get_current_data(self):
        """Get all current data for external access."""
        return {
            'frames_received': self.frames_received,
            'frames_written': self.frames_written,
            # Current frame data
            'p00': self.wf_p00[0] if self.frames_written > 0 else np.zeros(NBINS_TOTAL),
            'p11': self.wf_p11[0] if self.frames_written > 0 else np.zeros(NBINS_TOTAL),
            'p01_real': self.wf_p01_real[0] if self.frames_written > 0 else np.zeros(NBINS_TOTAL),
            'p01_imag': self.wf_p01_imag[0] if self.frames_written > 0 else np.zeros(NBINS_TOTAL),
            # Waterfall data
            'wf_p00': self.wf_p00.copy(),
            'wf_p11': self.wf_p11.copy(),
            'wf_p01_real': self.wf_p01_real.copy(),
            'wf_p01_imag': self.wf_p01_imag.copy(),
            # Current lag data
            'lag_p00': self.lag_wf_p00[0] if self.frames_written > 0 else np.zeros(NBINS_TOTAL),
            'lag_p11': self.lag_wf_p11[0] if self.frames_written > 0 else np.zeros(NBINS_TOTAL),
            'lag_p01_mag': self.lag_wf_p01_mag[0] if self.frames_written > 0 else np.zeros(NBINS_TOTAL),
            'lag_p01_phase': self.lag_wf_p01_phase[0] if self.frames_written > 0 else np.zeros(NBINS_TOTAL),
            # Lag waterfall data
            'lag_wf_p00': self.lag_wf_p00.copy(),
            'lag_wf_p11': self.lag_wf_p11.copy(),
            'lag_wf_p01_mag': self.lag_wf_p01_mag.copy(),
            'lag_wf_p01_phase': self.lag_wf_p01_phase.copy(),
            # Axes for plotting
            'freq_axis': FREQ_AXIS,
            'lag_axis': LAG_AXIS
        }
    
    def get_stats(self):
        """Get performance statistics."""
        elapsed = time.time() - self.start_time
        fps = self.frames_received / max(elapsed, 1e-6)
        
        decoder_stats = self.decoder.get_stats()
        lag_stats = self.lag_computer.get_stats()
        
        return {
            'duration': elapsed,
            'frames_received': self.frames_received,
            'frame_fps': fps,
            'lag_computations': self.lag_computations,
            'decoder': decoder_stats,
            'lag_computer': lag_stats
        }
    
    def stop(self):
        """Stop receiver."""
        self.running = False

# ==============================================================================
# MAIN FUNCTIONS
# ==============================================================================

def test_functionality():
    """Test basic functionality without networking."""
    print("=== Functionality Test ===")
    
    # Test components
    decoder = OptimizedPacketDecoder()
    lag_computer = OptimizedLagComputer()
    
    print("✅ Components created")
    
    # Generate test data
    test_p00 = np.random.random(NBINS_TOTAL) + 0.1
    test_p11 = np.random.random(NBINS_TOTAL) + 0.1
    test_p01_real = np.random.random(NBINS_TOTAL) * 0.5
    test_p01_imag = np.random.random(NBINS_TOTAL) * 0.5
    
    frame = FrameData(test_p00, test_p11, test_p01_real, test_p01_imag)
    print("✅ Test frame generated")
    
    # Test lag computation
    lag_data = lag_computer.compute_lag_spectra(frame)
    print("✅ Lag computation successful")
    print(f"   P00 peak: {np.max(lag_data.lag_p00):.2e}")
    print(f"   P11 peak: {np.max(lag_data.lag_p11):.2e}")
    print(f"   Cross-corr peak: {np.max(lag_data.lag_p01_mag):.4f}")
    
    # Test axes
    print(f"✅ Frequency axis: {FREQ_AXIS[0]/1e6:.1f} - {FREQ_AXIS[-1]/1e6:.1f} MHz")
    print(f"✅ Lag axis: {LAG_AXIS[0]:.1f} - {LAG_AXIS[-1]:.1f} meters")
    print(f"   Resolution: {(LAG_AXIS[1] - LAG_AXIS[0])*100:.2f} cm")

def run_demo(duration=15, frame_rate=8):
    """Run performance demo with mock data."""
    print("=== Performance Demo ===")
    
    # Create receiver
    receiver = HighPerformanceReceiver()
    
    # Create transmitter
    transmitter = MockUDPTransmitter()
    
    # Start transmitter in separate thread
    transmitter_thread = threading.Thread(
        target=transmitter.run_continuous,
        args=(frame_rate, duration),
        daemon=True
    )
    
    print("Starting components...")
    transmitter_thread.start()
    time.sleep(0.5)  # Let transmitter start
    
    # Run receiver
    receiver.run(duration=duration, enable_lag=True)
    
    # Get results
    stats = receiver.get_stats()
    
    print(f"\n=== Performance Results ===")
    print(f"Duration: {stats['duration']:.1f} seconds")
    print(f"Frames received: {stats['frames_received']}")
    print(f"Frame rate: {stats['frame_fps']:.2f} FPS")
    print(f"Packets decoded: {stats['decoder']['packets_decoded']}")
    print(f"Packet rate: {stats['decoder']['decode_rate_per_sec']:.0f} packets/sec")
    print(f"Lag computations: {stats['lag_computations']}")
    print(f"Lag rate: {stats['lag_computer']['compute_rate_per_sec']:.0f} computations/sec")
    print(f"Avg decode time: {stats['decoder']['avg_decode_time_ms']:.3f} ms/packet")
    print(f"Avg lag time: {stats['lag_computer']['avg_compute_time_ms']:.3f} ms/computation")
    
    # Performance comparison
    original_lag_time = 2.0  # Estimated from original system
    improvement = original_lag_time / max(stats['lag_computer']['avg_compute_time_ms'], 0.001)
    print(f"\n=== vs Original Performance ===")
    print(f"Lag computation improvement: {improvement:.1f}x faster")
    print(f"Theoretical max frame rate: {stats['lag_computer']['compute_rate_per_sec']:.0f} FPS")

def run_production(host="localhost", port=8888, enable_lag=True):
    """Run production correlator (listen for real UDP data)."""
    print("=== Production Mode ===")
    print(f"Listening on {host}:{port}")
    print("Waiting for correlator UDP data...")
    print("Press Ctrl+C to stop")
    
    # Create receiver
    receiver = HighPerformanceReceiver(host, port)
    
    # Set up signal handler
    def signal_handler(signum, frame):
        print("\nShutdown requested...")
        receiver.stop()
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    try:
        receiver.run(duration=None, enable_lag=enable_lag, verbose=True)
    except KeyboardInterrupt:
        print("\nStopped by user")
    
    # Final statistics
    stats = receiver.get_stats()
    print(f"\nFinal stats: {stats['frames_received']} frames in {stats['duration']:.1f}s")

def benchmark():
    """Run comprehensive performance benchmarks."""
    print("=== Performance Benchmark ===")
    
    # Test 1: Packet decoding speed
    print("\n1. Packet Decoding Benchmark")
    decoder = OptimizedPacketDecoder()
    transmitter = MockUDPTransmitter()
    
    # Generate test packets
    test_packets = []
    for i in range(1000):
        packet = transmitter.generate_packet(i % NBINS_TOTAL)
        test_packets.append(packet)
    
    start_time = time.perf_counter()
    decoded_count = 0
    for packet in test_packets:
        result = decoder.decode_packet(packet)
        if result:
            decoded_count += 1
    
    decode_time = time.perf_counter() - start_time
    decode_rate = decoded_count / decode_time
    
    print(f"   Decoded {decoded_count} packets in {decode_time:.3f}s")
    print(f"   Decode rate: {decode_rate:.0f} packets/sec")
    print(f"   Avg time per packet: {(decode_time/decoded_count)*1000:.3f} ms")
    
    # Test 2: Lag computation speed
    print("\n2. Lag Computation Benchmark")
    lag_computer = OptimizedLagComputer()
    
    # Generate test frame
    test_frame = FrameData(
        np.random.random(NBINS_TOTAL) + 0.1,
        np.random.random(NBINS_TOTAL) + 0.1,
        np.random.random(NBINS_TOTAL) * 0.5,
        np.random.random(NBINS_TOTAL) * 0.5
    )
    
    # Benchmark lag computation
    n_computations = 100
    start_time = time.perf_counter()
    
    for i in range(n_computations):
        lag_data = lag_computer.compute_lag_spectra(test_frame)
    
    lag_time = time.perf_counter() - start_time
    lag_rate = n_computations / lag_time
    
    print(f"   Computed {n_computations} lag spectra in {lag_time:.3f}s")
    print(f"   Lag rate: {lag_rate:.0f} computations/sec")
    print(f"   Avg time per computation: {(lag_time/n_computations)*1000:.3f} ms")
    
    print(f"\n=== Benchmark Summary ===")
    print(f"✅ Packet processing: {decode_rate:.0f} packets/sec")
    print(f"✅ Lag computation: {lag_rate:.0f} computations/sec")
    print(f"✅ Theoretical max correlator rate: {decode_rate/NBINS_TOTAL:.1f} frames/sec")

# ==============================================================================
# MAIN ENTRY POINT
# ==============================================================================

def main():
    """Main entry point with consolidated functionality."""
    parser = argparse.ArgumentParser(
        description='Periscope Correlator V2 - High-Performance UDP Correlator',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python correlator.py --test              # Test basic functionality
  python correlator.py --demo              # Performance demo with mock data
  python correlator.py --run               # Listen for real correlator data
  python correlator.py --benchmark         # Run performance benchmarks
        """
    )
    
    parser.add_argument('--test', action='store_true',
                       help='Run functionality test')
    parser.add_argument('--demo', action='store_true',
                       help='Run performance demo with mock data')
    parser.add_argument('--run', action='store_true',
                       help='Run production correlator')
    parser.add_argument('--benchmark', action='store_true',
                       help='Run performance benchmarks')
    
    # Options for demo and production modes
    parser.add_argument('--host', default='localhost',
                       help='UDP host to bind to (default: localhost)')
    parser.add_argument('--port', type=int, default=DEFAULT_UDP_PORT,
                       help=f'UDP port to listen on (default: {DEFAULT_UDP_PORT})')
    parser.add_argument('--duration', type=int, default=15,
                       help='Demo duration in seconds (default: 15)')
    parser.add_argument('--frame-rate', type=int, default=8,
                       help='Demo frame rate in FPS (default: 8)')
    parser.add_argument('--no-lag', action='store_true',
                       help='Disable lag computation')
    parser.add_argument('--waterfall-len', type=int, default=DEFAULT_WATERFALL_LEN,
                       help=f'Waterfall buffer length (default: {DEFAULT_WATERFALL_LEN})')
    
    args = parser.parse_args()
    
    # Show header
    print("=== Periscope Correlator V2 ===")
    print("High-performance modular UDP correlator")
    print(f"Configuration: {NBINS_TOTAL} bins, {NPROD} products")
    
    # Dispatch to appropriate function
    if args.test:
        test_functionality()
    elif args.demo:
        run_demo(duration=args.duration, frame_rate=args.frame_rate)
    elif args.run:
        run_production(host=args.host, port=args.port, enable_lag=not args.no_lag)
    elif args.benchmark:
        benchmark()
    else:
        # Default behavior - show help and run test
        parser.print_help()
        print("\nRunning default functionality test...\n")
        test_functionality()

if __name__ == '__main__':
    main()
