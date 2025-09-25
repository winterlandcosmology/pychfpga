#!/usr/bin/env python
"""
High-Performance UDP Processor for Periscope Correlator V2
========================================================

Optimized UDP packet reception and decoding with vectorized operations.
"""
import numpy as np
import time
import socket
import struct
import threading
from typing import Optional, Callable

# Constants from parent config
NCHAN = 8
NPROD = NCHAN * (NCHAN + 1) // 2  # 36 products
NBINS_TOTAL = 8192
FS_HZ = 3_200_000_000.0
M_FACTOR = 2**14

# Generate product indices
def _generate_product_indices():
    """Generate product map and find key indices."""
    prod_map = []
    for i in range(NCHAN + 1):
        for j in range(NCHAN // 2):
            if i < NCHAN - j:
                prod_map.append((i, (j + i) % NCHAN))
            else:
                prod_map.append((((j + i) % NCHAN), i - 1))
    
    product_map = np.array(prod_map)
    indices = {}
    for idx, (p0, p1) in enumerate(product_map):
        if p0 == 0 and p1 == 0:
            indices['p00'] = idx
        elif p0 == 1 and p1 == 1:
            indices['p11'] = idx
        elif (p0 == 0 and p1 == 1) or (p0 == 1 and p1 == 0):
            indices['p01'] = idx
    return indices

PRODUCT_INDICES = _generate_product_indices()

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

class OptimizedPacketDecoder:
    """High-performance packet decoder with selective product processing."""
    
    def __init__(self, selected_products=None):
        """Initialize with optional selective product processing."""
        self.frame_buffer = np.zeros((NBINS_TOTAL, NPROD), dtype=np.complex128)
        
        # Default products or user selection
        if selected_products is None:
            self.selected_products = [PRODUCT_INDICES['p00'], PRODUCT_INDICES['p11'], PRODUCT_INDICES['p01']]
        else:
            self.selected_products = selected_products
        
        # Pre-calculate indices for fast lookup
        self.idx00 = PRODUCT_INDICES['p00']
        self.idx11 = PRODUCT_INDICES['p11']
        self.idx01 = PRODUCT_INDICES['p01']
        
        # Performance tracking
        self.packets_decoded = 0
        self.decode_time_total = 0.0
        
        print(f"Decoder initialized with {len(self.selected_products)} selected products (vs {NPROD} total)")
    
    def decode_packet(self, packet_data: bytes):
        """Decode UDP packet with selective product processing for maximum speed."""
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
            
            # Unpack data efficiently - but only for selected products!
            data_format = f'<{NPROD * 2}I'
            data_values = struct.unpack(data_format, packet_data[4:])
            
            # Create sparse complex data array (only process selected products)
            complex_data = np.zeros(NPROD, dtype=np.complex128)
            
            # Only process selected products for massive speedup
            for prod_idx in self.selected_products:
                if prod_idx < NPROD:
                    # Get h and l values for this product
                    h_val = np.uint32(data_values[prod_idx * 2])
                    l_val = np.uint32(data_values[prod_idx * 2 + 1])
                    
                    # Optimized bit extraction
                    h_signed = np.int32(h_val)
                    l_signed = np.int32(l_val)
                    
                    real_val = (h_signed << 4) >> 14  # Proper sign extension
                    imag_val = (l_signed << 14) >> 14
                    
                    complex_data[prod_idx] = np.float64(real_val) + 1j * np.float64(imag_val)
            
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

class HighPerformanceUDPReceiver(threading.Thread):
    """High-performance UDP receiver using original CRS packet format."""
    
    def __init__(self, socket_obj, config, data_buffer, packet_dtype):
        """Initialize high-performance UDP receiver with CRS compatibility."""
        super().__init__(daemon=True, name="UDPReceiver")
        
        self.socket = socket_obj
        self.config = config
        self.data_buffer = data_buffer
        self.packet_dtype = packet_dtype  # Use original UCorrFrameReceiver format
        
        # Configure socket for performance
        try:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16 * 1024 * 1024)
        except:
            pass
        
        # Frame assembly using original format
        self.raw_frame = np.zeros((NBINS_TOTAL, NPROD), dtype=np.complex128)
        self.temp32_real = np.empty((NPROD,), dtype=np.int32)
        self.temp32_imag = np.empty((NPROD,), dtype=np.int32)
        
        # Frame assembly
        self.packet_buffer = bytearray(4096)
        
        # Performance tracking
        self.frames_received = 0
        self.start_time = time.time()
        self.last_fps_time = time.time()
        self.fps = 0.0
        
        # Control
        self.running = False
        self.frame_callback: Optional[Callable[[FrameData], None]] = None
        
    def set_frame_callback(self, callback: Callable[[FrameData], None]):
        """Set callback for completed frames."""
        self.frame_callback = callback
    
    def run(self):
        """Main receiver loop using original CRS packet format."""
        self.running = True
        print("High-performance UDP receiver started with CRS packet compatibility")
        
        while self.running:
            try:
                nbytes, _ = self.socket.recvfrom_into(self.packet_buffer)
                
                if nbytes > 0:
                    self._process_packet(self.packet_buffer[:nbytes])
                            
            except socket.timeout:
                continue
            except Exception as e:
                if self.running:
                    print(f"UDP Receiver error: {e}")
        
        print("UDP receiver stopped")
    
    def _process_packet(self, packet_data: bytes):
        """Process single packet using original CRS format."""
        try:
            # Parse using original packet format
            packet_array = np.frombuffer(packet_data, dtype=self.packet_dtype, count=1)
            header = packet_array['header'][0]
            
            if header['cookie'] != 0xcf:
                return
            
            bin_idx = header['stream_id']
            if bin_idx >= NBINS_TOTAL:
                return
            
            data = packet_array['data'][0]
            self._decode_frame_data(bin_idx, data)
            
            # Check if frame is complete (when we get the last bin)
            if bin_idx == NBINS_TOTAL - 1:
                self._complete_frame()
                
        except Exception as e:
            print(f"Packet processing error: {e}")
    
    def _decode_frame_data(self, bin_idx: int, data: np.ndarray):
        """Decode frame data using original CRS format."""
        try:
            # Use original decoding logic from the working app
            h_signed = data['h'].view(np.int32)
            l_signed = data['l'].view(np.int32)
            
            # Extract real and imaginary parts with proper sign extension
            self.temp32_real[:] = (h_signed << 4) >> 14
            self.temp32_imag[:] = (l_signed << 14) >> 14
            
            # Store in frame buffer
            self.raw_frame[bin_idx] = self.temp32_real + 1j * self.temp32_imag
            
        except Exception as e:
            print(f"Frame decode error at bin {bin_idx}: {e}")
    
    def _complete_frame(self):
        """Process completed frame using original logic."""
        try:
            # Normalize frame (use firmware frames config)
            n_firmware_frames = self.config.get('n_firmware_frames', 16384)
            if n_firmware_frames == 0:
                return
                
            normalized_frame = self.raw_frame / n_firmware_frames
            
            # Apply gain factor if available
            gain_factor = self.config.get('gain_factor')
            if gain_factor is not None:
                normalized_frame *= gain_factor
            
            # Extract products using original indices
            idx00 = PRODUCT_INDICES['p00']
            idx11 = PRODUCT_INDICES['p11'] 
            idx01 = PRODUCT_INDICES['p01']
            
            p00_data = np.abs(normalized_frame[:, idx00])
            p11_data = np.abs(normalized_frame[:, idx11])
            p01_complex = normalized_frame[:, idx01]
            p01_real_data = p01_complex.real
            p01_imag_data = p01_complex.imag
            
            # Create frame data
            frame = FrameData(p00_data, p11_data, p01_real_data, p01_imag_data)
            frame.frame_number = self.frames_received
            
            # Update data buffer
            self.data_buffer.update_frame(frame)
            
            # Call frame callback
            if self.frame_callback:
                self.frame_callback(frame)
            
            # Update performance metrics
            self.frames_received += 1
            current_time = time.time()
            
            if current_time - self.last_fps_time >= 1.0:
                self.fps = self.frames_received / (current_time - self.start_time)
                self.last_fps_time = current_time
                
                # Debug info
                if self.frames_received % 10 == 0:
                    print(f"Frames received: {self.frames_received}, FPS: {self.fps:.2f}")
            
            # Reset frame buffer for next frame
            self.raw_frame.fill(0)
            
        except Exception as e:
            print(f"Frame completion error: {e}")
            self.raw_frame.fill(0)
    
    def stop(self):
        """Stop the receiver."""
        self.running = False
    
    def get_stats(self):
        """Get performance statistics."""
        return {
            'frames_received': self.frames_received,
            'fps': self.fps,
            'packets_processed': self.frames_received * NBINS_TOTAL  # Estimate
        }
