"""XMODEM and XMODEM-1K transfers over a U-Boot netconsole socket."""

import logging
import socket
import time

import numpy as np

logger = logging.getLogger(__name__)

# XMODEM protocol bytes
_STX = 0x02
_EOT = 0x04
_ACK = 0x06
_NAK = 0x15
_CAN = 0x18
_CRC = 0x43
_BLOCK1K_SIZE = 1024
_PAD = 0x1A

# CCITT CRC-16 table (XMODEM polynomial)
_CRC16_TABLE = np.array([
    0x0000, 0x1021, 0x2042, 0x3063, 0x4084, 0x50a5, 0x60c6, 0x70e7,
    0x8108, 0x9129, 0xa14a, 0xb16b, 0xc18c, 0xd1ad, 0xe1ce, 0xf1ef,
    0x1231, 0x0210, 0x3273, 0x2252, 0x52b5, 0x4294, 0x72f7, 0x62d6,
    0x9339, 0x8318, 0xb37b, 0xa35a, 0xd3bd, 0xc39c, 0xf3ff, 0xe3de,
    0x2462, 0x3443, 0x0420, 0x1401, 0x64e6, 0x74c7, 0x44a4, 0x5485,
    0xa56a, 0xb54b, 0x8528, 0x9509, 0xe5ee, 0xf5cf, 0xc5ac, 0xd58d,
    0x3653, 0x2672, 0x1611, 0x0630, 0x76d7, 0x66f6, 0x5695, 0x46b4,
    0xb75b, 0xa77a, 0x9719, 0x8738, 0xf7df, 0xe7fe, 0xd79d, 0xc7bc,
    0x48c4, 0x58e5, 0x6886, 0x78a7, 0x0840, 0x1861, 0x2802, 0x3823,
    0xc9cc, 0xd9ed, 0xe98e, 0xf9af, 0x8948, 0x9969, 0xa90a, 0xb92b,
    0x5af5, 0x4ad4, 0x7ab7, 0x6a96, 0x1a71, 0x0a50, 0x3a33, 0x2a12,
    0xdbfd, 0xcbdc, 0xfbbf, 0xeb9e, 0x9b79, 0x8b58, 0xbb3b, 0xab1a,
    0x6ca6, 0x7c87, 0x4ce4, 0x5cc5, 0x2c22, 0x3c03, 0x0c60, 0x1c41,
    0xedae, 0xfd8f, 0xcdec, 0xddcd, 0xad2a, 0xbd0b, 0x8d68, 0x9d49,
    0x7e97, 0x6eb6, 0x5ed5, 0x4ef4, 0x3e13, 0x2e32, 0x1e51, 0x0e70,
    0xff9f, 0xefbe, 0xdfdd, 0xcffc, 0xbf1b, 0xaf3a, 0x9f59, 0x8f78,
    0x9188, 0x81a9, 0xb1ca, 0xa1eb, 0xd10c, 0xc12d, 0xf14e, 0xe16f,
    0x1080, 0x00a1, 0x30c2, 0x20e3, 0x5004, 0x4025, 0x7046, 0x6067,
    0x83b9, 0x9398, 0xa3fb, 0xb3da, 0xc33d, 0xd31c, 0xe37f, 0xf35e,
    0x02b1, 0x1290, 0x22f3, 0x32d2, 0x4235, 0x5214, 0x6277, 0x7256,
    0xb5ea, 0xa5cb, 0x95a8, 0x8589, 0xf56e, 0xe54f, 0xd52c, 0xc50d,
    0x34e2, 0x24c3, 0x14a0, 0x0481, 0x7466, 0x6447, 0x5424, 0x4405,
    0xa7db, 0xb7fa, 0x8799, 0x97b8, 0xe75f, 0xf77e, 0xc71d, 0xd73c,
    0x26d3, 0x36f2, 0x0691, 0x16b0, 0x6657, 0x7676, 0x4615, 0x5634,
    0xd94c, 0xc96d, 0xf90e, 0xe92f, 0x99c8, 0x89e9, 0xb98a, 0xa9ab,
    0x5844, 0x4865, 0x7806, 0x6827, 0x18c0, 0x08e1, 0x3882, 0x28a3,
    0xcb7d, 0xdb5c, 0xeb3f, 0xfb1e, 0x8bf9, 0x9bd8, 0xabbb, 0xbb9a,
    0x4a75, 0x5a54, 0x6a37, 0x7a16, 0x0af1, 0x1ad0, 0x2ab3, 0x3a92,
    0xfd2e, 0xed0f, 0xdd6c, 0xcd4d, 0xbdaa, 0xad8b, 0x9de8, 0x8dc9,
    0x7c26, 0x6c07, 0x5c64, 0x4c45, 0x3ca2, 0x2c83, 0x1ce0, 0x0cc1,
    0xef1f, 0xff3e, 0xcf5d, 0xdf7c, 0xaf9b, 0xbfba, 0x8fd9, 0x9ff8,
    0x6e17, 0x7e36, 0x4e55, 0x5e74, 0x2e93, 0x3eb2, 0x0ed1, 0x1ef0,
], dtype=np.uint16)


class XMODEM:
    """XMODEM-1K sender over a connected netconsole UDP socket."""

    def __init__(self, sock, log=None):
        self.sock = sock
        self.log = log or logger

    @staticmethod
    def crc16_ccitt(data):
        """CCITT CRC-16 over a uint8 numpy array (XMODEM polynomial)."""
        crc = np.uint16(0)
        table = _CRC16_TABLE
        for byte in data:
            idx = ((crc >> 8) ^ byte) & 0xFF
            crc = (table[idx] ^ (crc << 8)) & np.uint16(0xFFFF)
        return int(crc)

    @staticmethod
    def block_1k(seq, chunk, pad=_PAD):
        """Build one XMODEM-1K (STX) frame from up to 1024 bytes of payload."""
        payload = np.full(_BLOCK1K_SIZE, pad, dtype=np.uint8)
        n = min(int(chunk.size), _BLOCK1K_SIZE)
        if n:
            payload[:n] = chunk[:n]
        seq_b = seq & 0xFF
        crc = XMODEM.crc16_ccitt(payload)
        return (
            bytes([_STX, seq_b, (~seq_b) & 0xFF])
            + payload.tobytes()
            + bytes([crc >> 8, crc & 0xFF])
        )

    def _recv_response(self, timeout=5.0):
        """Read inbound bytes until ACK, NAK, or cancel is observed."""
        deadline = time.monotonic() + timeout
        self.sock.settimeout(0.1)
        cancel_count = 0
        while time.monotonic() < deadline:
            try:
                data = self.sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                continue
            for byte in data:
                if byte == _ACK:
                    return _ACK
                if byte == _NAK:
                    return _NAK
                if byte == _CAN:
                    cancel_count += 1
                    if cancel_count >= 2:
                        return _CAN
                elif byte != _CRC:
                    cancel_count = 0
        return None

    def _wait_for_crc_mode(self, timeout=60):
        """Wait for the receiver to request CRC mode (``C``)."""
        deadline = time.monotonic() + timeout
        self.sock.settimeout(0.25)
        cancel_count = 0
        while time.monotonic() < deadline:
            try:
                data = self.sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                continue
            for byte in data:
                if byte == _CRC:
                    return True
                if byte == _CAN:
                    cancel_count += 1
                    if cancel_count >= 2:
                        return False
                elif byte != _EOT:
                    cancel_count = 0
        return False

    def _send_eot(self, timeout=60):
        self.sock.sendall(bytes([_EOT]))
        response = self._recv_response(timeout=timeout)
        if response == _ACK:
            return True
        if response == _NAK:
            self.sock.sendall(bytes([_EOT]))
            return self._recv_response(timeout=timeout) == _ACK
        return False

    def send(self, filename, timeout=60, pad=_PAD):
        """Standard XMODEM-1K: build, send, and ACK-sync each block in turn."""
        if not self._wait_for_crc_mode(timeout=timeout):
            return False

        data = np.fromfile(filename, dtype=np.uint8)
        sequence = 1
        offset = 0
        size = int(data.size)
        while offset < size:
            chunk = data[offset:offset + _BLOCK1K_SIZE]
            block = self.block_1k(sequence, chunk, pad=pad)
            self.sock.sendall(block)
            if self._recv_response(timeout=timeout) != _ACK:
                return False
            sequence = (sequence + 1) & 0xFF
            offset += _BLOCK1K_SIZE

        return self._send_eot(timeout=timeout)

    def pipe_send(self, filename, pipe_depth=8, timeout=60, pad=_PAD):
        """Pipelined XMODEM-1K: pre-build batches of blocks, send, then collect ACKs.

        Up to ``pipe_depth`` frames are transmitted back-to-back before waiting for
        their ACKs.  This is faster on low-latency links but can overrun U-Boot's
        512-byte netconsole input buffer; use :meth:`send` on high-RTT paths.
        """
        if pipe_depth < 1:
            raise ValueError("pipe_depth must be >= 1")
        if not self._wait_for_crc_mode(timeout=timeout):
            return False

        data = np.fromfile(filename, dtype=np.uint8)
        sequence = 1
        offset = 0
        size = int(data.size)
        while offset < size:
            blocks = []
            batch_offset = offset
            while batch_offset < size and len(blocks) < pipe_depth:
                chunk = data[batch_offset:batch_offset + _BLOCK1K_SIZE]
                blocks.append(self.block_1k(sequence, chunk, pad=pad))
                sequence = (sequence + 1) & 0xFF
                batch_offset += _BLOCK1K_SIZE

            for block in blocks:
                self.sock.sendall(block)
            for _ in blocks:
                if self._recv_response(timeout=timeout) != _ACK:
                    return False
            offset = batch_offset

        return self._send_eot(timeout=timeout)


def xmodem1k_send(sock, filename, timeout=60, log=None):
    """Send a file with standard (per-block ACK) XMODEM-1K over netconsole."""
    return XMODEM(sock, log=log).send(filename, timeout=timeout)


def xmodem1k_pipe_send(sock, filename, pipe_depth=8, timeout=60, log=None):
    """Send a file with batched/pipelined XMODEM-1K over netconsole."""
    return XMODEM(sock, log=log).pipe_send(
        filename, pipe_depth=pipe_depth, timeout=timeout)
