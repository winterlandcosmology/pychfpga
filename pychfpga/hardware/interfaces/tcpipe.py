# Standard Python packages
import asyncio
import logging
import socket
import re
import struct
import time
import textwrap
from contextlib import contextmanager


from .bsb_mmi import BSB_MMI

class _TCPipePerf:
    """Collect and log TCPipe RPC and BSB aggregate timing statistics."""

    def __init__(self, tcpipe, log_interval):
        self.tcpipe = tcpipe
        self.log_interval = log_interval
        self.stats_by_rpc = {}
        self.flush_count = 0
        self.commands_flushed = 0
        self.flush_wall_s = 0.0
        self.max_flush_commands = 0

    def record_rpc(self, rpc_id, tx_len, rx_len, send_s, recv_s, total_s):
        stats = self.stats_by_rpc.setdefault(rpc_id, {
            'count': 0,
            'tx_bytes': 0,
            'rx_bytes': 0,
            'send_s': 0.0,
            'recv_s': 0.0,
            'total_s': 0.0,
            'max_s': 0.0,
        })
        stats['count'] += 1
        stats['tx_bytes'] += tx_len
        stats['rx_bytes'] += rx_len
        stats['send_s'] += send_s
        stats['recv_s'] += recv_s
        stats['total_s'] += total_s
        stats['max_s'] = max(stats['max_s'], total_s)

        if rpc_id in (self.tcpipe.RPC_BSB_WRITE_READ, self.tcpipe.RPC_BSB_BATCH_WRITE):
            count = stats['count']
            if count in (1, 10, 100) or (self.log_interval and count % self.log_interval == 0):
                self.log_rpc_line(rpc_id, stats)

    def log_rpc_line(self, rpc_id, stats):
        count = stats['count']
        name = self.tcpipe.RPC_NAMES.get(rpc_id, f'rpc_{rpc_id}')
        self.tcpipe.log.info(
            '%s: TCPipe RPC perf %s: n=%d avg=%.3f ms send=%.3f ms recv/wait=%.3f ms max=%.3f ms avg_tx=%.1f B avg_rx=%.1f B',
            self.tcpipe,
            name,
            count,
            1000.0 * stats['total_s'] / count,
            1000.0 * stats['send_s'] / count,
            1000.0 * stats['recv_s'] / count,
            1000.0 * stats['max_s'],
            stats['tx_bytes'] / count,
            stats['rx_bytes'] / count)

    def record_aggregate_flush(self, command_count, wall_s):
        self.flush_count += 1
        self.commands_flushed += command_count
        self.flush_wall_s += wall_s
        self.max_flush_commands = max(self.max_flush_commands, command_count)

    def log_rpc_summary(self):
        for rpc_id, stats in sorted(self.stats_by_rpc.items()):
            self.log_rpc_line(rpc_id, stats)

    def log_aggregate_summary(self):
        if not self.flush_count:
            return
        self.tcpipe.log.info(
            '%s: TCPipe BSB aggregate: flushes=%d avg_cmds_per_flush=%.2f '
            'max_cmds_in_flush=%d avg_flush_wall=%.3f ms amortized_per_cmd=%.4f ms',
            self.tcpipe,
            self.flush_count,
            self.commands_flushed / self.flush_count,
            self.max_flush_commands,
            1000.0 * self.flush_wall_s / self.flush_count,
            1000.0 * self.flush_wall_s / max(1, self.commands_flushed))

    def log_summary(self):
        self.log_rpc_summary()
        self.log_aggregate_summary()


class TCPipe:
    """ Interface to the TCP-based command and control protocol to the FPGA board.

    This interface is meant to be very simple and lightweight. It provides :
        - FPGA bitstream programming
        - Access to the ARM and FPGA firmware via AXI memory-mapped read/writes
        - Access to the FPGA firmware using a lightweight Byte-serial Bus protocol
        - Basic I2C read-write commands to access the board's hardware
    """

    RPC_PREFIX = 0xCC
    RPC_BSB_WRITE_READ = 0x00
    # RPC_IIC_WRITE = 0x01
    RPC_GET_RPC_FUNCTIONS = 0x01
    RPC_IIC_WRITE_READ = 0x02
    # RPC_IIC_READ = 0x03
    RPC_SPI_WRITE_READ = 0x04
    RPC_CORE_REG_READ = 0x05
    RPC_CORE_REG_WRITE = 0x06
    RPC_ADC_SYNC = 0x07
    RPC_OLED_DRAW_XBM = 0x08
    RPC_OLED_SET_FIRMWARE_LABEL = 0x09
    RPC_OLED_DRAW_TEXT = 0x0A
    RPC_BSB_BATCH_WRITE = 0x0B
    RPC_SPI_BATCH_WRITE = 0x0C
    RPC_ADC_STARTUP = 0x0D
    RPC_ADC_SHUTDOWN = 0x0E
    RPC_ADC_RESET = 0x0F
    RPC_ADC_WAIT_FOR_STATE = 0x10
    RPC_ADC_GET_IP_STATUS = 0x11
    RPC_ADC_GET_BLOCK_STATUS = 0x12
    RPC_ADC_SETUP_FIFO = 0x13
    RPC_ADC_GET_FIFO_STATUS = 0x14
    RPC_ADC_SET_MIXER_SETTINGS = 0x15
    RPC_ADC_GET_MIXER_SETTINGS = 0x16
    RPC_ADC_RESET_NCO_PHASE = 0x17
    RPC_ADC_UPDATE_EVENT = 0x18
    RPC_ADC_SET_DECIMATION_FACTOR = 0x19
    RPC_ADC_GET_DECIMATION_FACTOR = 0x1A
    RPC_ADC_SET_NYQUIST_ZONE = 0x1B
    RPC_ADC_GET_NYQUIST_ZONE = 0x1C
    RPC_ADC_SET_DSA = 0x1D
    RPC_ADC_GET_DSA = 0x1E
    RPC_ADC_SET_QMC_SETTINGS = 0x1F
    RPC_ADC_GET_QMC_SETTINGS = 0x20
    RPC_ADC_SET_COARSE_DELAY = 0x21
    RPC_ADC_GET_COARSE_DELAY = 0x22
    RPC_ADC_SET_DITHER = 0x23
    RPC_ADC_GET_DITHER = 0x24
    RPC_ADC_GET_CLOCK_SOURCE = 0x25
    RPC_ADC_GET_PLL_LOCK_STATUS = 0x26
    RPC_ADC_GET_PLL_CONFIG = 0x27
    RPC_ADC_DYNAMIC_PLL_CONFIG = 0x28
    RPC_ADC_SET_CALIBRATION_MODE = 0x29
    RPC_ADC_GET_CALIBRATION_MODE = 0x2A
    RPC_ADC_SET_CAL_FREEZE = 0x2B
    RPC_ADC_GET_CAL_FREEZE = 0x2C
    RPC_ADC_SET_CAL_COEFFICIENTS = 0x2D
    RPC_ADC_GET_CAL_COEFFICIENTS = 0x2E
    RPC_ADC_SET_SIGNAL_DETECTOR = 0x2F
    RPC_ADC_GET_SIGNAL_DETECTOR = 0x30
    RPC_ADC_SET_CLK_DISTRIBUTION = 0x31
    RPC_ADC_GET_CLK_DISTRIBUTION = 0x32
    RPC_ADC_SET_THRESHOLD_SETTINGS = 0x33
    RPC_ADC_GET_THRESHOLD_SETTINGS = 0x34
    RPC_ADC_SET_THRESHOLD_CLR_MODE = 0x35
    RPC_ADC_THRESHOLD_STICKY_CLEAR = 0x36
    RPC_ADC_SET_DATAPATH_MODE = 0x37
    RPC_ADC_GET_DATAPATH_MODE = 0x38
    RPC_ADC_SET_IMR_PASS_MODE = 0x39
    RPC_ADC_GET_IMR_PASS_MODE = 0x3A
    RPC_ADC_SET_PWR_MODE = 0x3B
    RPC_ADC_GET_PWR_MODE = 0x3C
    RPC_ADC_GET_SIGNAL_DETECTOR_STATUS = 0x3D
    RPC_GET_MOTHERBOARD_IPMI_INFO = 0x3E
    RPC_GET_BACKPLANE_IPMI_INFO = 0x3F
    RPC_ADC_RERUN_FG_CAL = 0x40

    IPMI_FIELD_WIRE_LEN = 32
    IPMI_RPC_VERSION = 1
    MOTHERBOARD_IPMI_REPLY_LEN = 1 + 5 * IPMI_FIELD_WIRE_LEN
    BACKPLANE_IPMI_REPLY_LEN = 1 + 2 + 1 + 10 * IPMI_FIELD_WIRE_LEN
    BACKPLANE_IPMI_FLAG_PRESENT = 0x01

    ADC_TILE = 0
    DAC_TILE = 1

    # Xilinx XRFdc tile states (xrfdc.h)
    RFDC_STATE_OFF = 0x0
    RFDC_STATE_SHUTDOWN = 0x1
    RFDC_STATE_PWRUP = 0x3
    RFDC_STATE_CLK_DET = 0x6
    RFDC_STATE_CAL = 0xB
    RFDC_STATE_FULL = 0xF

    _RFDC_TILE_STATUS = '<IIBII'
    _RFDC_BLOCK_STATUS = '<dIIBBB'
    _RFDC_MIXER = '<ddIIIBB'
    _RFDC_QMC = '<IIddiI'
    _RFDC_COARSE_DELAY = '<II'
    _RFDC_DSA = '<If'
    _RFDC_PLL = '<IddIIIIQI'
    _RFDC_CAL_FREEZE = '<III'
    _RFDC_CAL_COEFF = '<8I'
    _RFDC_SIGNAL_DETECTOR = '<BBBBHHHB'
    _RFDC_SIGNAL_DETECTOR_STATUS = '<HHHHBBBB'
    _RFDC_THRESHOLD = '<9I'
    _RFDC_PWR_MODE = '<II'
    _RFDC_TILE_CLOCK = 'BBIddBBB'
    _RFDC_DIST_INFO = '6B' + _RFDC_TILE_CLOCK * 8
    _RFDC_DIST = 'II2I2IdI8dI' + _RFDC_DIST_INFO
    _RFDC_DIST_SYS = '<' + _RFDC_DIST * 8

    RPC_NAMES = {
        RPC_BSB_WRITE_READ: 'bsb_write_read',
        RPC_GET_RPC_FUNCTIONS: 'get_rpc_functions',
        RPC_IIC_WRITE_READ: 'iic_write_read',
        RPC_SPI_WRITE_READ: 'spi_write_read',
        RPC_CORE_REG_READ: 'read_reg',
        RPC_CORE_REG_WRITE: 'write_reg',
        RPC_ADC_SYNC: 'adc_sync',
        RPC_OLED_DRAW_XBM: 'oled_draw_xbm',
        RPC_OLED_SET_FIRMWARE_LABEL: 'oled_set_firmware_label',
        RPC_OLED_DRAW_TEXT: 'oled_draw_text',
        RPC_BSB_BATCH_WRITE: 'bsb_batch_write',
        RPC_SPI_BATCH_WRITE: 'spi_batch_write',
        RPC_ADC_STARTUP: 'adc_startup',
        RPC_ADC_RERUN_FG_CAL: 'adc_rerun_fg_cal',
        RPC_ADC_SHUTDOWN: 'adc_shutdown',
        RPC_ADC_RESET: 'adc_reset',
        RPC_ADC_WAIT_FOR_STATE: 'adc_wait_for_state',
        RPC_ADC_GET_IP_STATUS: 'adc_get_ip_status',
        RPC_ADC_GET_BLOCK_STATUS: 'adc_get_block_status',
        RPC_ADC_SETUP_FIFO: 'adc_setup_fifo',
        RPC_ADC_GET_FIFO_STATUS: 'adc_get_fifo_status',
        RPC_ADC_SET_MIXER_SETTINGS: 'adc_set_mixer_settings',
        RPC_ADC_GET_MIXER_SETTINGS: 'adc_get_mixer_settings',
        RPC_ADC_RESET_NCO_PHASE: 'adc_reset_nco_phase',
        RPC_ADC_UPDATE_EVENT: 'adc_update_event',
        RPC_ADC_SET_DECIMATION_FACTOR: 'adc_set_decimation_factor',
        RPC_ADC_GET_DECIMATION_FACTOR: 'adc_get_decimation_factor',
        RPC_ADC_SET_NYQUIST_ZONE: 'adc_set_nyquist_zone',
        RPC_ADC_GET_NYQUIST_ZONE: 'adc_get_nyquist_zone',
        RPC_ADC_SET_DSA: 'adc_set_dsa',
        RPC_ADC_GET_DSA: 'adc_get_dsa',
        RPC_ADC_SET_QMC_SETTINGS: 'adc_set_qmc_settings',
        RPC_ADC_GET_QMC_SETTINGS: 'adc_get_qmc_settings',
        RPC_ADC_SET_COARSE_DELAY: 'adc_set_coarse_delay',
        RPC_ADC_GET_COARSE_DELAY: 'adc_get_coarse_delay',
        RPC_ADC_SET_DITHER: 'adc_set_dither',
        RPC_ADC_GET_DITHER: 'adc_get_dither',
        RPC_ADC_GET_CLOCK_SOURCE: 'adc_get_clock_source',
        RPC_ADC_GET_PLL_LOCK_STATUS: 'adc_get_pll_lock_status',
        RPC_ADC_GET_PLL_CONFIG: 'adc_get_pll_config',
        RPC_ADC_DYNAMIC_PLL_CONFIG: 'adc_dynamic_pll_config',
        RPC_ADC_SET_CALIBRATION_MODE: 'adc_set_calibration_mode',
        RPC_ADC_GET_CALIBRATION_MODE: 'adc_get_calibration_mode',
        RPC_ADC_SET_CAL_FREEZE: 'adc_set_cal_freeze',
        RPC_ADC_GET_CAL_FREEZE: 'adc_get_cal_freeze',
        RPC_ADC_SET_CAL_COEFFICIENTS: 'adc_set_cal_coefficients',
        RPC_ADC_GET_CAL_COEFFICIENTS: 'adc_get_cal_coefficients',
        RPC_ADC_SET_SIGNAL_DETECTOR: 'adc_set_signal_detector',
        RPC_ADC_GET_SIGNAL_DETECTOR: 'adc_get_signal_detector',
        RPC_ADC_GET_SIGNAL_DETECTOR_STATUS: 'adc_get_signal_detector_status',
        RPC_ADC_SET_CLK_DISTRIBUTION: 'adc_set_clk_distribution',
        RPC_ADC_GET_CLK_DISTRIBUTION: 'adc_get_clk_distribution',
        RPC_ADC_SET_THRESHOLD_SETTINGS: 'adc_set_threshold_settings',
        RPC_ADC_GET_THRESHOLD_SETTINGS: 'adc_get_threshold_settings',
        RPC_ADC_SET_THRESHOLD_CLR_MODE: 'adc_set_threshold_clr_mode',
        RPC_ADC_THRESHOLD_STICKY_CLEAR: 'adc_threshold_sticky_clear',
        RPC_ADC_SET_DATAPATH_MODE: 'adc_set_datapath_mode',
        RPC_ADC_GET_DATAPATH_MODE: 'adc_get_datapath_mode',
        RPC_ADC_SET_IMR_PASS_MODE: 'adc_set_imr_pass_mode',
        RPC_ADC_GET_IMR_PASS_MODE: 'adc_get_imr_pass_mode',
        RPC_ADC_SET_PWR_MODE: 'adc_set_pwr_mode',
        RPC_ADC_GET_PWR_MODE: 'adc_get_pwr_mode',
        RPC_GET_MOTHERBOARD_IPMI_INFO: 'get_motherboard_ipmi_info',
        RPC_GET_BACKPLANE_IPMI_INFO: 'get_backplane_ipmi_info',
    }
    RPC_PERF_LOG_INTERVAL = 500

    #: TCPipe processor-side register index for UDP data port (see :meth:`set_local_data_port_number`).
    _UDP_DATA_PORT_REG = 2
    #: Deprecated alias for `_UDP_DATA_PORT_REG` (prefer :meth:`set_local_data_port_number`).
    CORE_REG_UDP_DATA_PORT = 2
    #: Fixed board-side TCP data listen port (matches firmware ``DATA_TCP_DEFAULT_PORT``).
    DEFAULT_TCP_DATA_PORT = 41002
    #: UDP discovery / REBOOT port (matches firmware ``TCPIPE_FWDISCOVER_PORT``).
    FWDISCOVER_PORT = 9875
    REBOOT_PAYLOAD = b'REBOOT'
    #: After an RPC timeout, wait this long with no RX before treating the stream as idle.
    RPC_DRAIN_QUIET_S = 0.2
    #: Cap on post-timeout drain time (late replies plus quiet period).
    RPC_DRAIN_MAX_S = 1.0
    #: Retry TCP connect while the board is still booting (port 7 comes up early).
    CONNECT_RETRY_INTERVAL_S = 1.0
    CONNECT_RETRY_MAX_S = 60.0

    opened_sockets = {}

    def __init__(self, hostname, port=7, timeout=10, log_performance=False):
        self.log = logging.getLogger(__name__)
        self.hostname = hostname
        self.port = port
        self.sock = self.opened_sockets.pop((hostname, port), None)
        if self.sock:
            self.sock.close()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock.settimeout(timeout)
        deadline = time.monotonic() + self.CONNECT_RETRY_MAX_S
        last_err = None
        while True:
            try:
                self.sock.connect((hostname, self.port))
                break
            except (TimeoutError, socket.timeout, ConnectionRefusedError,
                    ConnectionResetError, OSError) as exc:
                last_err = exc
                if time.monotonic() >= deadline:
                    raise
                time.sleep(self.CONNECT_RETRY_INTERVAL_S)
        if last_err is not None:
            self.log.debug('%r: connect succeeded after %r', self, last_err)
        self.opened_sockets[(hostname, port)] = self.sock
        self.tx_buf = bytearray(2048)
        self.tx_view = memoryview(self.tx_buf)
        self.rx_buf = bytearray(2048)
        self.rx_view = memoryview(self.rx_buf)
        self.firmware_crc = None
        self.log.debug(f'{self!r}: Opened TCPipe socket from local address{self.sock.getsockname()} to remote address {self.hostname}:{self.port}')
        self.bsb_sent_ctr = 0
        self.perf = _TCPipePerf(self, self.RPC_PERF_LOG_INTERVAL) if log_performance else None
        self._bsb_aggregate_ctx_depth = 0
        self._bsb_agg_pending = []
        self._bsb_agg_pending_tx = 0
        self._bsb_aggregate_max_cmds = 512
        self._bsb_aggregate_max_tx_bytes = 64 * 1024
        # Serializes async socket use (bitstream send and async RPCs) so the
        # socket is not left non-blocking for a concurrent sync RPC.
        self._sock_lock = asyncio.Lock()

        self.check_tcpipe_version()

    def __repr__(self):
        return f'TCPipe({self.hostname}:{self.port})'

    def close(self):
        try:
            self._bsb_batch_write_flush()
        except (IOError, OSError) as exc:
            self.log.warning('%r: _bsb_batch_write_flush during close failed: %r', self, exc)
        if self.perf:
            self.perf.log_summary()
        if self.sock is None:
            return
        old_sock = self.opened_sockets.get((self.hostname, self.port))
        if old_sock is self.sock:
            self.opened_sockets.pop((self.hostname, self.port), None)
        self.sock.close()
        self.sock = None

    @staticmethod
    def send_reboot(hostname, port=None):
        """Send a UDP ``REBOOT`` datagram to ``hostname``.

        TCPipe listens on the discovery port for a payload whose first token is
        ``REBOOT`` and then issues PSCI SYSTEM_RESET. There is no reply.
        """
        if port is None:
            port = TCPipe.FWDISCOVER_PORT
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto(TCPipe.REBOOT_PAYLOAD, (hostname, port))

    def reboot(self, port=None):
        """Reboot the board by sending the UDP ``REBOOT`` command.

        Drops the TCP session without flushing pending RPCs; the peer is
        resetting and will not complete them.
        """
        if port is None:
            port = self.FWDISCOVER_PORT
        self.log.info('%r: Sending UDP REBOOT to %s:%s', self, self.hostname, port)
        self._bsb_agg_pending.clear()
        self._bsb_agg_pending_tx = 0
        self.send_reboot(self.hostname, port=port)
        if self.sock is None:
            return
        old_sock = self.opened_sockets.get((self.hostname, self.port))
        if old_sock is self.sock:
            self.opened_sockets.pop((self.hostname, self.port), None)
        try:
            self.sock.close()
        except OSError:
            pass
        self.sock = None

    def _rpc_write_read(self, rpc_id, payload=b'', read_length=None, read_until=None, timeout=None):
        """Execute a RPC command and return the reply.


        Parameters:
            rpc_id (int): The RPC ID to write.
            payload (bytes): The payload to write.
            read_length (int): The number of bytes to read.
            read_until (bytes): The bytes to read until.
            timeout (float): The timeout in seconds.

        Returns:
            bytes: The reply from the TCPipe interface.

        Raises:
            ValueError: If the payload length is too long.
            IOError: If the RPC fails.
            TimeoutError, socket.timeout: If the RPC send or reply times out.
                Leftover command-stream bytes are drained before the exception
                is re-raised so the next RPC is not desynchronized.
        """
        if self._bsb_agg_pending:
            self._bsb_batch_write_flush()
        tx_payload, tx_len = self._build_rpc_tx_payload(rpc_id, payload)

        old_timeout = None
        if timeout:
            old_timeout = self.sock.gettimeout()
            self.sock.settimeout(timeout)
        start_time = time.perf_counter()
        send_done_time = start_time
        recv_done_time = start_time
        rx_len = 0
        reply = None
        try:
            try:
                self.sock.sendall(tx_payload)
                send_done_time = time.perf_counter()
                if read_until is not None:
                    reply = bytearray()
                    while True:
                        reply += self.sock.recv(1024)
                        if reply.endswith(read_until):
                            recv_done_time = time.perf_counter()
                            rx_len = len(reply)
                            break
                elif read_length is not None:
                    reply = self._recv_exact(read_length, timeout=timeout)
                    recv_done_time = time.perf_counter()
                    rx_len = len(reply)
                else:
                    rx_len = self.sock.recv_into(self.rx_buf)
                    recv_done_time = time.perf_counter()
                    reply = self.rx_buf[:rx_len]
            except (TimeoutError, socket.timeout):
                discarded = self._drain_socket_buffer(
                    quiet=self.RPC_DRAIN_QUIET_S,
                    max_time=self.RPC_DRAIN_MAX_S)
                self.log.warning(
                    '%r: RPC %s timed out; discarded %d leftover command-stream bytes',
                    self, rpc_id, discarded)
                raise
        finally:
            if timeout:
                self.sock.settimeout(old_timeout)
        if self.perf:
            self.perf.record_rpc(
            rpc_id=rpc_id,
            tx_len=tx_len,
            rx_len=rx_len,
            send_s=send_done_time - start_time,
            recv_s=recv_done_time - send_done_time,
            total_s=recv_done_time - start_time)
        if read_length is not None and rx_len != read_length:
            raise IOError(f'{self!r}: RPC {rpc_id} received {rx_len} bytes instead of {read_length} bytes')
        return reply

    def _build_rpc_tx_payload(self, rpc_id, payload=b''):
        """Build the 4-byte RPC header plus ``payload`` for the command stream."""
        if not isinstance(payload, (bytes, bytearray, memoryview)):
            payload = bytes(payload)
        if len(payload) >= 65536:
            raise ValueError(f'{self!r}: RPC payload length must fit in 16 bits')
        tx_len = 4 + len(payload)
        if tx_len <= len(self.tx_buf):
            self.tx_view[0] = self.RPC_PREFIX
            self.tx_view[1] = rpc_id
            self.tx_view[2] = len(payload) & 0xFF
            self.tx_view[3] = len(payload) >> 8
            self.tx_view[4:tx_len] = payload
            return self.tx_view[:tx_len], tx_len
        tx_payload = bytearray(tx_len)
        tx_payload[0] = self.RPC_PREFIX
        tx_payload[1] = rpc_id
        tx_payload[2] = len(payload) & 0xFF
        tx_payload[3] = len(payload) >> 8
        tx_payload[4:tx_len] = payload
        return tx_payload, tx_len

    async def _recv_exact_locked_async(self, nbytes):
        """Read exactly ``nbytes``. Caller holds ``_sock_lock``; socket non-blocking."""
        if nbytes == 0:
            return b''
        loop = asyncio.get_running_loop()
        buf = bytearray(nbytes)
        view = memoryview(buf)
        got = 0
        while got < nbytes:
            chunk = await loop.sock_recv(self.sock, nbytes - got)
            if not chunk:
                raise ConnectionResetError('TCPipe connection closed by peer')
            view[got:got + len(chunk)] = chunk
            got += len(chunk)
        return bytes(buf)

    async def _drain_socket_buffer_locked_async(self, quiet=0.0, max_time=None):
        """Discard leftover command-stream bytes. Caller holds ``_sock_lock``."""
        if self.sock is None:
            return 0
        discarded = 0
        loop = asyncio.get_running_loop()
        while True:
            try:
                data = self.sock.recv(4096)
            except (BlockingIOError, InterruptedError, TimeoutError, socket.timeout):
                break
            except (ConnectionResetError, ConnectionAbortedError):
                return discarded
            if not data:
                return discarded
            discarded += len(data)
        if not quiet:
            return discarded
        if max_time is None:
            max_time = self.RPC_DRAIN_MAX_S
        deadline = time.perf_counter() + max_time
        while True:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                break
            wait = quiet if remaining > quiet else remaining
            try:
                data = await asyncio.wait_for(
                    loop.sock_recv(self.sock, 4096), timeout=wait)
            except asyncio.TimeoutError:
                break
            except (ConnectionResetError, ConnectionAbortedError):
                break
            if not data:
                break
            discarded += len(data)
        return discarded

    async def _rpc_xfer_locked_async(
            self, tx_payload, read_length=None, read_until=None):
        """Send one RPC and read the reply. Lock held; socket non-blocking."""
        loop = asyncio.get_running_loop()
        start_time = time.perf_counter()
        await loop.sock_sendall(self.sock, tx_payload)
        send_done_time = time.perf_counter()
        if read_until is not None:
            reply = bytearray()
            while True:
                chunk = await loop.sock_recv(self.sock, 1024)
                if not chunk:
                    raise ConnectionResetError('TCPipe connection closed by peer')
                reply += chunk
                if reply.endswith(read_until):
                    recv_done_time = time.perf_counter()
                    return bytes(reply), len(reply), start_time, send_done_time, recv_done_time
        if read_length is not None:
            reply = await self._recv_exact_locked_async(read_length)
            recv_done_time = time.perf_counter()
            return reply, len(reply), start_time, send_done_time, recv_done_time
        chunk = await loop.sock_recv(self.sock, len(self.rx_buf))
        recv_done_time = time.perf_counter()
        return chunk, len(chunk), start_time, send_done_time, recv_done_time

    async def _rpc_write_read_locked_async(
            self, rpc_id, payload=b'', read_length=None, read_until=None,
            timeout=None):
        """Async RPC body. Caller must hold ``_sock_lock``."""
        if self._bsb_agg_pending:
            await self._bsb_batch_write_flush_locked_async()
        tx_payload, tx_len = self._build_rpc_tx_payload(rpc_id, payload)
        old_timeout = self.sock.gettimeout()
        self.sock.setblocking(False)
        try:
            try:
                xfer = self._rpc_xfer_locked_async(
                    tx_payload, read_length=read_length, read_until=read_until)
                if timeout:
                    reply, rx_len, start_time, send_done_time, recv_done_time = (
                        await asyncio.wait_for(xfer, timeout=timeout))
                else:
                    reply, rx_len, start_time, send_done_time, recv_done_time = (
                        await xfer)
            except asyncio.TimeoutError:
                discarded = await self._drain_socket_buffer_locked_async(
                    quiet=self.RPC_DRAIN_QUIET_S,
                    max_time=self.RPC_DRAIN_MAX_S)
                self.log.warning(
                    '%r: RPC %s timed out; discarded %d leftover command-stream bytes',
                    self, rpc_id, discarded)
                raise TimeoutError(
                    f'{self!r}: RPC {rpc_id} timed out after {timeout} s')
        finally:
            self.sock.setblocking(True)
            self.sock.settimeout(old_timeout)
        if self.perf:
            self.perf.record_rpc(
                rpc_id=rpc_id,
                tx_len=tx_len,
                rx_len=rx_len,
                send_s=send_done_time - start_time,
                recv_s=recv_done_time - send_done_time,
                total_s=recv_done_time - start_time)
        if read_length is not None and rx_len != read_length:
            raise IOError(
                f'{self!r}: RPC {rpc_id} received {rx_len} bytes instead of '
                f'{read_length} bytes')
        return reply

    async def _rpc_write_read_async(
            self, rpc_id, payload=b'', read_length=None, read_until=None,
            timeout=None):
        """Async variant of :meth:`_rpc_write_read` using non-blocking sockets.

        Serializes with other async socket users via ``_sock_lock``. Do not
        interleave sync RPCs on the same TCPipe while an async RPC is in
        flight.
        """
        async with self._sock_lock:
            return await self._rpc_write_read_locked_async(
                rpc_id, payload, read_length=read_length,
                read_until=read_until, timeout=timeout)

    def bsb_write_read(self, data, read_length=None):
        """ Writes `data` to the FPGA firmware Byte-serial bus and return reply.


        Parameters:

            data (bytes): data to write prior to the read operation
            read_length (int): expected reply size in bytes, including the
                1-byte BSB sequence header. ``None`` derives it from the
                3-byte BSB command header (reads: ``1 + 2**length_code``;
                writes: ``1``) so a split TCP segment cannot desynchronise
                the command stream.


        """
        if self._bsb_agg_pending:
            self._bsb_batch_write_flush()
        if read_length is None:
            read_length = self._bsb_write_read_reply_len(data)
        reply = self._rpc_write_read(
            self.RPC_BSB_WRITE_READ, data, read_length=read_length)
        self.bsb_sent_ctr += 1
        return reply

    @staticmethod
    def _bsb_write_read_reply_len(data):
        """Bytes the board will send for one ``bsb_write_read`` RPC.

        BSB byte 0: opcode[7:5], length_code[4:3], addr[18:16]. Read opcodes
        (0-3) return 1 sequence byte plus ``2**length_code`` payload bytes.
        Write opcodes return the sequence byte only.
        """
        if not data:
            return 1
        opcode = data[0] >> 5
        if opcode <= 0b011:
            return 1 + (1 << ((data[0] >> 3) & 0x03))
        return 1

    def bsb_batch_write(self, commands):
        """Execute multiple write-only BSB transfers in one ``bsb_batch_write`` RPC.

        Each command is the same payload as :meth:`bsb_write_read`. For write-only commands,
        reply bytes are the single RPC status/sequence byte per command (see ``bsb_write_read``).

        Returns:
            bytes: ``n`` status bytes (equivalent to the first byte of each individual RPC reply).
        """
        for cmd in commands:
            self._bsb_batch_write_enqueue(cmd)
        if self._bsb_aggregate_active():
            return b''
        return self._bsb_batch_write_flush()

    def _bsb_batch_write_now(self, commands):
        """Send write-only BSB commands immediately with the batch-write RPC.

        Parameters:
            commands (list of bytes): the BSB commands to send.

        Returns:
            bytes: the reply from the BSB batch write.

        Raises:
            IOError: if the BSB batch write fails.
        """
        n = len(commands)
        parts = [n.to_bytes(2, 'little')]
        for c in commands:
            if isinstance(c, memoryview):
                c = c.tobytes()
            elif not isinstance(c, (bytes, bytearray)):
                c = bytes(c)
            lc = len(c)
            if lc >= 65536:
                raise ValueError(f'BSB batch pack: command length {lc} must fit in 16 bits')
            parts.append(lc.to_bytes(2, 'little'))
            parts.append(c)
        payload = b''.join(parts)
        reply = self._rpc_write_read(
            self.RPC_BSB_BATCH_WRITE, payload, read_length=1 + n)
        if len(reply) == 1:
            if reply[0] == 0xFF:
                raise IOError(f'{self!r}: BSB batch RPC failed (protocol/parse error on board)')
            raise IOError(
                f'{self!r}: BSB batch RPC failed at command index {reply[0] - 1} '
                f'(1-based status code {reply[0]})')
        if len(reply) != 1 + n:
            raise IOError(
                f'{self!r}: BSB batch RPC expected {1 + n} reply bytes, got {len(reply)}')
        if reply[0] != 0:
            if reply[0] == 0xFF:
                raise IOError(f'{self!r}: BSB batch RPC failed (protocol/parse error on board)')
            raise IOError(
                f'{self!r}: BSB batch RPC failed at command index {reply[0] - 1} '
                f'(1-based status code {reply[0]})')
        self.bsb_sent_ctr += n
        return reply[1:]


    async def bsb_write_read_async(self, data, read_length=None):
        """Async variant of :meth:`bsb_write_read`."""
        if self._bsb_agg_pending:
            await self._bsb_batch_write_flush_async()
        if read_length is None:
            read_length = self._bsb_write_read_reply_len(data)
        reply = await self._rpc_write_read_async(
            self.RPC_BSB_WRITE_READ, data, read_length=read_length)
        self.bsb_sent_ctr += 1
        return reply

    async def bsb_batch_write_async(self, commands):
        """Async variant of :meth:`bsb_batch_write`."""
        for cmd in commands:
            await self._bsb_batch_write_enqueue_async(cmd)
        if self._bsb_aggregate_active():
            return b''
        return await self._bsb_batch_write_flush_async()

    async def _bsb_batch_write_now_locked_async(self, commands):
        """Send write-only BSB commands. Caller holds ``_sock_lock``."""
        n = len(commands)
        parts = [n.to_bytes(2, 'little')]
        for c in commands:
            if isinstance(c, memoryview):
                c = c.tobytes()
            elif not isinstance(c, (bytes, bytearray)):
                c = bytes(c)
            lc = len(c)
            if lc >= 65536:
                raise ValueError(f'BSB batch pack: command length {lc} must fit in 16 bits')
            parts.append(lc.to_bytes(2, 'little'))
            parts.append(c)
        payload = b''.join(parts)
        reply = await self._rpc_write_read_locked_async(
            self.RPC_BSB_BATCH_WRITE, payload, read_length=1 + n)
        if len(reply) == 1:
            if reply[0] == 0xFF:
                raise IOError(f'{self!r}: BSB batch RPC failed (protocol/parse error on board)')
            raise IOError(
                f'{self!r}: BSB batch RPC failed at command index {reply[0] - 1} '
                f'(1-based status code {reply[0]})')
        if len(reply) != 1 + n:
            raise IOError(
                f'{self!r}: BSB batch RPC expected {1 + n} reply bytes, got {len(reply)}')
        if reply[0] != 0:
            if reply[0] == 0xFF:
                raise IOError(f'{self!r}: BSB batch RPC failed (protocol/parse error on board)')
            raise IOError(
                f'{self!r}: BSB batch RPC failed at command index {reply[0] - 1} '
                f'(1-based status code {reply[0]})')
        self.bsb_sent_ctr += n
        return reply[1:]

    async def _bsb_batch_write_flush_locked_async(self):
        """Flush queued BSB writes. Caller holds ``_sock_lock``."""
        if not self._bsb_agg_pending:
            return b''
        cmds = list(self._bsb_agg_pending)
        self._bsb_agg_pending.clear()
        self._bsb_agg_pending_tx = 0
        try:
            t0 = time.perf_counter()
            reply = await self._bsb_batch_write_now_locked_async(cmds)
            if self.perf:
                self.perf.record_aggregate_flush(len(cmds), time.perf_counter() - t0)
            return reply
        except Exception:
            self._bsb_agg_pending = cmds + self._bsb_agg_pending
            self._bsb_agg_pending_tx = sum(4 + len(x) for x in self._bsb_agg_pending)
            raise

    async def _bsb_batch_write_flush_async(self):
        """Async variant of :meth:`_bsb_batch_write_flush`."""
        async with self._sock_lock:
            return await self._bsb_batch_write_flush_locked_async()

    async def _bsb_batch_write_enqueue_async(self, cmd):
        """Enqueue a write-only BSB command, flushing asynchronously if full."""
        if isinstance(cmd, memoryview):
            cmd = cmd.tobytes()
        elif not isinstance(cmd, (bytes, bytearray)):
            cmd = bytes(cmd)
        fs = 4 + len(cmd)
        if fs > self._bsb_aggregate_max_tx_bytes:
            raise ValueError(
                f'{self!r}: BSB command of {len(cmd)} bytes (+ framing) exceeds '
                f'aggregate max tx {self._bsb_aggregate_max_tx_bytes}')
        while self._bsb_agg_pending and (
                len(self._bsb_agg_pending) >= self._bsb_aggregate_max_cmds
                or self._bsb_agg_pending_tx + fs > self._bsb_aggregate_max_tx_bytes):
            await self._bsb_batch_write_flush_async()
        self._bsb_agg_pending.append(bytes(cmd))
        self._bsb_agg_pending_tx += fs

    def _drain_socket_buffer(self, quiet=0.0, max_time=None):
        """Discard leftover bytes on the command TCP stream.

        With ``quiet=0`` (default), only bytes already in the kernel buffer are
        dropped (non-blocking). Used after ``get_rpc_functions``.

        After an RPC timeout, pass a quiet period so a late reply can arrive and
        be discarded before the next command is sent.

        Parameters:
            quiet (float): Seconds with no RX after which the stream is idle.
            max_time (float or None): Cap on total drain time when ``quiet`` is
                non-zero. Defaults to :attr:`RPC_DRAIN_MAX_S`.

        Returns:
            int: Number of bytes discarded.
        """
        if self.sock is None:
            return 0
        discarded = 0
        old_timeout = self.sock.gettimeout()
        try:
            self.sock.settimeout(0)
            while True:
                try:
                    data = self.sock.recv(4096)
                except (BlockingIOError, TimeoutError, socket.timeout):
                    break
                except (ConnectionResetError, ConnectionAbortedError):
                    return discarded
                if not data:
                    return discarded
                discarded += len(data)

            if not quiet:
                return discarded

            if max_time is None:
                max_time = self.RPC_DRAIN_MAX_S
            deadline = time.perf_counter() + max_time
            while True:
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    break
                self.sock.settimeout(quiet if remaining > quiet else remaining)
                try:
                    data = self.sock.recv(4096)
                except (TimeoutError, socket.timeout):
                    break
                except (ConnectionResetError, ConnectionAbortedError):
                    break
                if not data:
                    break
                discarded += len(data)
        finally:
            self.sock.settimeout(old_timeout)
        return discarded

    def _recv_exact(self, nbytes, timeout=None):
        """Read exactly ``nbytes`` from the RPC stream socket."""
        if nbytes == 0:
            return b''
        buf = bytearray(nbytes)
        view = memoryview(buf)
        got = 0
        old_timeout = None
        if timeout is not None:
            old_timeout = self.sock.gettimeout()
            self.sock.settimeout(timeout)
        try:
            while got < nbytes:
                n = self.sock.recv_into(view[got:], nbytes - got)
                if n == 0:
                    raise ConnectionResetError('TCPipe connection closed by peer')
                got += n
        finally:
            if timeout is not None:
                self.sock.settimeout(old_timeout)
        return bytes(buf)

    def get_rpc_functions(self):
        """ Fetches the list of functions provided by the platform processor.


        Parameters: None


        """
        b = self._rpc_write_read(self.RPC_GET_RPC_FUNCTIONS, read_until=b'\x00\x00\x00')
        self._drain_socket_buffer()
        funcs = {}
        # extract function id byte, and the name and docs null-terminated strings
        f=re.findall('(.)([^\x00]*\x00)([^\x00]*\x00)',b.decode())
        for (func_id, name, docs) in f:
            if len(name) > 1: # exclude terminator, which has an empty null-terminated name string
                funcs[ord(func_id)] = dict(
                    name=name.strip('\x00'),
                    docs=re.sub(r'\t(\t*)',r'\n\1',docs.strip('\x00')).strip() #replace first tab of a sequence of tabs by a newline, and remove leading/trailing newlines/empty lines
                    )
        return funcs

    def print_rpc_functions(self):
        for fid,info in self.get_rpc_functions().items():
            print(f'{fid:3d} : {info["name"]}(...)')
            print(textwrap.indent(info['docs'], '\t'))


    def check_tcpipe_version(self):
        """Require RTOS firmware that provides the write-only BSB batch RPC."""
        funcs = self.get_rpc_functions()
        if (
                self.RPC_BSB_BATCH_WRITE not in funcs
                and not any(info.get('name') == 'bsb_batch_write' for info in funcs.values())):
            raise RuntimeError(
                f'{self!r}: RTOS firmware does not advertise required RPC '
                f'bsb_batch_write (id {self.RPC_BSB_BATCH_WRITE}); update the board firmware')
        self.log.debug('%s: BSB aggregate flushes will use bsb_batch_write RPC', self)


    def _bsb_aggregate_active(self):
        return self._bsb_aggregate_ctx_depth > 0


    @contextmanager
    def aggregate_commands(self):
        """Coalesce write-only BSB MMI transactions until exit, then flush.

        Aggregation is off by default so a lone control write (for example
        ``UCAP.USER_RESET = 1``) is sent immediately. Nesting is counted;
        the queued writes flush when the outermost context exits.

        Application code should use ``fpga.mmi.aggregate_commands()`` (see
        :class:`TCPipe_BSB_MMI`) rather than calling this on TCPipe directly.
        """
        self._bsb_aggregate_ctx_depth += 1
        try:
            yield self
        finally:
            self._bsb_batch_write_flush()
            self._bsb_aggregate_ctx_depth -= 1
            if self.perf:
                self.perf.log_aggregate_summary()

    def _bsb_batch_write_flush(self):
        """Send all queued write-only BSB commands with the required batch-write RPC."""
        if not self._bsb_agg_pending:
            return b''
        cmds = list(self._bsb_agg_pending)
        self._bsb_agg_pending.clear()
        self._bsb_agg_pending_tx = 0
        try:
            t0 = time.perf_counter()
            reply = self._bsb_batch_write_now(cmds)
            if self.perf:
                self.perf.record_aggregate_flush(len(cmds), time.perf_counter() - t0)
            return reply
        except Exception:
            # Restore queue so callers can retry or abort explicitly
            self._bsb_agg_pending = cmds + self._bsb_agg_pending
            self._bsb_agg_pending_tx = sum(4 + len(x) for x in self._bsb_agg_pending)
            raise

    def _bsb_batch_write_enqueue(self, cmd):
        """ Enqueue a write-only BSB command for aggregation.

        Parameters:
            cmd (bytes): the BSB command to enqueue.

        Returns:
            None

        Raises:
            ValueError: if the command is too large to aggregate.
        """
        if isinstance(cmd, memoryview):
            cmd = cmd.tobytes()
        elif not isinstance(cmd, (bytes, bytearray)):
            cmd = bytes(cmd)
        fs = 4 + len(cmd)
        if fs > self._bsb_aggregate_max_tx_bytes:
            raise ValueError(
                f'{self!r}: BSB command of {len(cmd)} bytes (+ framing) exceeds aggregate max tx {self._bsb_aggregate_max_tx_bytes}')
        while self._bsb_agg_pending and (
                len(self._bsb_agg_pending) >= self._bsb_aggregate_max_cmds
                or self._bsb_agg_pending_tx + fs > self._bsb_aggregate_max_tx_bytes):
            self._bsb_batch_write_flush()
        self._bsb_agg_pending.append(bytes(cmd))
        self._bsb_agg_pending_tx += fs

    #########################################################
    #  I2C interface
    #########################################################

    def i2c_read(self, addr, read_length, no_error=False):
        return self.i2c_write_read(addr, data=b'', read_length=read_length, no_error=no_error)


    def i2c_polled_read(self, addr, read_length, no_error=False):
        reply = self._rpc_write_read(self.RPC_IIC_READ, bytes((addr, read_length)))
        if reply[0]:
            if no_error:
                return b''
            else:
                raise IOError(f'{self!r}: I2C Reply has {len(reply)} bytes but has error code {reply[0]}')
        if len(reply) < 1+read_length:
            raise IOError(f'{self!r}: I2C Did not receive enough bytes {len(reply)} instead of {1+read_length}')
        return reply[1:read_length + 1]


    # def rpc_call(self, command, length, reply_length=0):
    #     txv = self.tx_view
    #     txv[0] = self.RPC_PREFIX
    #     txv[1] = command
    #     txv[2] = len(header) + len(data)
    #     txv[3] = 0
    #     txv[4:4+len(header)] = header
    #     txv[4+len(header):4+len(header)+len(data)] = data
    I2C_ERROR_CODES = {
        0x01: 'DONE', # should never happen
        0x02: 'Not started',
        0x04: 'Timeout',
        0x08: 'Arb Lost',
        0x10: 'Nack',
        0x20: 'Rx Overflow',
        0x40: 'Tx Overflow',
        0x80: 'Rx Underflow'
    }
    def i2c_write_read(self, addr, data, read_length, no_error=False):
        """ Writes `data` to I2C address `addr`, perform a restart, and read `read_length` from the same device.

        Used for accessing devices such as EEPROMs that require a command or addres be sent in the same transaction before reading from the device.

        Parameters:

            addr (int): bits 6:0 is the I2C address. Bit 7 indiates whether we access IIC bus 0 or 1.

            data (bytes): data to write prior to the read operation

            read_length (int): number of bytes to read (1-255)

        """
        if not isinstance(data, (bytes, bytearray)):
            data = bytes(data)
        if 0 > read_length >= 256:
            raise ValueError('{self!r}: Read length bust be between 0 and 255')
        payload = bytes((addr, read_length)) + data
        reply = self._rpc_write_read(
            self.RPC_IIC_WRITE_READ, payload, read_length=1 + read_length)
        if reply[0]:
            if no_error:
                return b''
            else:
                raise IOError(f'{self!r}: TCPipe I2C: Reply has error code {reply[0]} ({", ".join(e for v,e in self.I2C_ERROR_CODES.items() if reply[0] & v)})')
        if len(reply) != 1 + read_length:
            raise IOError(f'{self!r}: TCPipe I2C: Receive {len(reply)} bytes instead of {1+read_length} bytes (including status byte)')
        return reply[1:read_length + 1]

    def i2c_write(self, addr, data):
        """ Writes `data` to I2C address `addr`.

        Used for accessing devices such as EEPROMs that require a command or addres be sent in the same transaction before reading from the device.

        Parameters:

            addr (int): bits 6:0 is the I2C address. Bit 7 indiates whether we access IIC bus 0 or 1.

            data (bytes): data to write prior to the read operation


        """
        self.i2c_write_read(addr, data, read_length=0)

    async def i2c_read_async(self, addr, read_length, no_error=False):
        """Async variant of :meth:`i2c_read`."""
        return await self.i2c_write_read_async(
            addr, data=b'', read_length=read_length, no_error=no_error)

    async def i2c_polled_read_async(self, addr, read_length, no_error=False):
        """Async variant of :meth:`i2c_polled_read`."""
        reply = await self._rpc_write_read_async(
            self.RPC_IIC_READ, bytes((addr, read_length)))
        if reply[0]:
            if no_error:
                return b''
            raise IOError(
                f'{self!r}: I2C Reply has {len(reply)} bytes but has error '
                f'code {reply[0]}')
        if len(reply) < 1 + read_length:
            raise IOError(
                f'{self!r}: I2C Did not receive enough bytes {len(reply)} '
                f'instead of {1 + read_length}')
        return reply[1:read_length + 1]

    async def i2c_write_read_async(self, addr, data, read_length, no_error=False):
        """Async variant of :meth:`i2c_write_read`."""
        if not isinstance(data, (bytes, bytearray)):
            data = bytes(data)
        if 0 > read_length >= 256:
            raise ValueError('{self!r}: Read length bust be between 0 and 255')
        payload = bytes((addr, read_length)) + data
        reply = await self._rpc_write_read_async(
            self.RPC_IIC_WRITE_READ, payload, read_length=1 + read_length)
        if reply[0]:
            if no_error:
                return b''
            raise IOError(
                f'{self!r}: TCPipe I2C: Reply has error code {reply[0]} '
                f'({", ".join(e for v, e in self.I2C_ERROR_CODES.items() if reply[0] & v)})')
        if len(reply) != 1 + read_length:
            raise IOError(
                f'{self!r}: TCPipe I2C: Receive {len(reply)} bytes instead of '
                f'{1 + read_length} bytes (including status byte)')
        return reply[1:read_length + 1]

    async def i2c_write_async(self, addr, data):
        """Async variant of :meth:`i2c_write`."""
        await self.i2c_write_read_async(addr, data, read_length=0)

    @classmethod
    def _decode_ipmi_wire_fields(cls, blob, n_fields):
        fields = []
        for index in range(n_fields):
            start = index * cls.IPMI_FIELD_WIRE_LEN
            chunk = blob[start:start + cls.IPMI_FIELD_WIRE_LEN]
            fields.append(chunk.split(b'\x00', 1)[0].decode('utf-8', errors='replace'))
        return fields

    def _parse_motherboard_ipmi_reply(self, reply):
        if reply[0]:
            raise IOError(f'{self!r}: get_motherboard_ipmi_info failed with status {reply[0]}')
        body = reply[1:]
        if len(body) < self.MOTHERBOARD_IPMI_REPLY_LEN:
            raise IOError(
                f'{self!r}: get_motherboard_ipmi_info reply too short '
                f'({len(body)} < {self.MOTHERBOARD_IPMI_REPLY_LEN})')
        version = body[0]
        names = ('manufacturer', 'product', 'revision', 'serial', 'manufacturing_date')
        values = self._decode_ipmi_wire_fields(body[1:], len(names))
        return dict(version=version, **dict(zip(names, values)))

    def _parse_backplane_ipmi_reply(self, reply):
        if reply[0]:
            raise IOError(f'{self!r}: get_backplane_ipmi_info failed with status {reply[0]}')
        body = reply[1:]
        if len(body) < self.BACKPLANE_IPMI_REPLY_LEN:
            raise IOError(
                f'{self!r}: get_backplane_ipmi_info reply too short '
                f'({len(body)} < {self.BACKPLANE_IPMI_REPLY_LEN})')
        version = body[0]
        crate_slot = struct.unpack_from('<h', body, 1)[0]
        flags = body[3]
        field_names = (
            'bp_manufacturer', 'bp_part', 'bp_serial', 'bp_revision',
            'crate_manufacturer', 'crate_model', 'crate_revision', 'crate_serial',
            'chassis_part', 'chassis_serial',
        )
        values = self._decode_ipmi_wire_fields(body[4:], len(field_names))
        return dict(
            version=version,
            crate_slot=crate_slot,
            flags=flags,
            backplane_present=bool(flags & self.BACKPLANE_IPMI_FLAG_PRESENT),
            **dict(zip(field_names, values)),
        )

    def get_motherboard_ipmi_info(self):
        """Return motherboard IPMI fields cached by TCPipe at boot (no EEPROM re-read)."""
        reply = self._rpc_write_read(
            self.RPC_GET_MOTHERBOARD_IPMI_INFO,
            b'',
            read_length=1 + self.MOTHERBOARD_IPMI_REPLY_LEN)
        return self._parse_motherboard_ipmi_reply(reply)

    async def get_motherboard_ipmi_info_async(self):
        """Async variant of :meth:`get_motherboard_ipmi_info`."""
        reply = await self._rpc_write_read_async(
            self.RPC_GET_MOTHERBOARD_IPMI_INFO,
            b'',
            read_length=1 + self.MOTHERBOARD_IPMI_REPLY_LEN)
        return self._parse_motherboard_ipmi_reply(reply)

    def get_backplane_ipmi_info(self):
        """Return segment backplane / crate IPMI cached by TCPipe at boot."""
        reply = self._rpc_write_read(
            self.RPC_GET_BACKPLANE_IPMI_INFO,
            b'',
            read_length=1 + self.BACKPLANE_IPMI_REPLY_LEN)
        return self._parse_backplane_ipmi_reply(reply)

    async def get_backplane_ipmi_info_async(self):
        """Async variant of :meth:`get_backplane_ipmi_info`."""
        reply = await self._rpc_write_read_async(
            self.RPC_GET_BACKPLANE_IPMI_INFO,
            b'',
            read_length=1 + self.BACKPLANE_IPMI_REPLY_LEN)
        return self._parse_backplane_ipmi_reply(reply)

    #########################################################
    #  OLED interface
    #########################################################

    OLED_SET_FIRMWARE_LABEL_STATUS = {
        0: 'Success',
        1: 'Argument too long',
    }

    OLED_DRAW_TEXT_STATUS = {
        0: 'Success',
        1: 'Bad arguments',
        2: 'OLED not initialized',
        3: 'I2C/mux error',
    }


    def _check_oled_status(self, command, reply, status_table):
        if len(reply) != 1:
            raise IOError(f'{self!r}: OLED {command} received {len(reply)} status bytes instead of 1')
        status = reply[0]
        if status:
            status_name = status_table.get(status, f'Unknown status {status}')
            raise IOError(f'{self!r}: OLED {command} failed: {status_name}')

    def oled_set_firmware_label(self, label):
        if label is None:
            payload = b''
        else:
            payload = str(label).encode('ascii')
        reply = self._rpc_write_read(self.RPC_OLED_SET_FIRMWARE_LABEL, payload, read_length=1)
        self._check_oled_status('set firmware label', reply, self.OLED_SET_FIRMWARE_LABEL_STATUS)

    def oled_draw_text(self, x, y, text, rotation=0):
        """ Draw text on the RTOS-managed OLED display.

        Coordinates are SSD1306/u8g2 coordinates. ``rotation`` follows u8g2:
        0 = horizontal, 1 = 90 degrees clockwise, 2 = 180 degrees, 3 = 90 degrees counter-clockwise.
        """
        try:
            payload = bytearray(5)
            payload[0:2] = int(x).to_bytes(2, 'little')
            payload[2:4] = int(y).to_bytes(2, 'little')
            payload[4] = int(rotation)
            payload.extend(str(text).encode('ascii'))

            reply = self._rpc_write_read(self.RPC_OLED_DRAW_TEXT, payload, read_length=1)
            self._check_oled_status('draw text', reply, self.OLED_DRAW_TEXT_STATUS)
        except (IOError, OSError, UnicodeEncodeError) as e:
            self.log.warning(f'{self!r}: Failed updating OLED display text: {e!r}')

    async def oled_set_firmware_label_async(self, label):
        """Async variant of :meth:`oled_set_firmware_label`."""
        if label is None:
            payload = b''
        else:
            payload = str(label).encode('ascii')
        reply = await self._rpc_write_read_async(
            self.RPC_OLED_SET_FIRMWARE_LABEL, payload, read_length=1)
        self._check_oled_status(
            'set firmware label', reply, self.OLED_SET_FIRMWARE_LABEL_STATUS)

    async def oled_draw_text_async(self, x, y, text, rotation=0):
        """Async variant of :meth:`oled_draw_text`."""
        try:
            payload = bytearray(5)
            payload[0:2] = int(x).to_bytes(2, 'little')
            payload[2:4] = int(y).to_bytes(2, 'little')
            payload[4] = int(rotation)
            payload.extend(str(text).encode('ascii'))
            reply = await self._rpc_write_read_async(
                self.RPC_OLED_DRAW_TEXT, payload, read_length=1)
            self._check_oled_status(
                'draw text', reply, self.OLED_DRAW_TEXT_STATUS)
        except (IOError, OSError, UnicodeEncodeError) as e:
            self.log.warning(f'{self!r}: Failed updating OLED display text: {e!r}')


    #########################################################
    #  SPI interface
    #########################################################

    def spi_write_read(self, spi_device, data, read_length, timeout=None):
        """ Writes `data` to SPI address `spi_device` while reading the same number of bytes, and return  the last `read_length` bytes transaction.

        Parameters:

            spi_device (int): SPI device to enable (not implemented).

            data (bytes): data to write during the write phase. `read_length` null bytes will be appended for the read phase.

            read_length (int): number of bytes to read (1-255)

            timeout (float): time before timeout

        Returns:

            (bytearray): bytes that have been read
        """
        if not isinstance(data, (bytes, bytearray)):
            data = bytes(data)
        payload = bytes((spi_device, read_length)) + data + b'\x00' * read_length
        reply = self._rpc_write_read(
            self.RPC_SPI_WRITE_READ,
            payload,
            read_length=1 + len(data) + read_length,
            timeout=timeout)
        if reply[0]:
            raise IOError(f'{self!r}: SPI Reply has error code {reply[0]}')
        if len(reply) != 1 + len(data) + read_length:
            raise IOError(f'{self!r}: SPI Received {len(reply)} bytes instead of {1 + len(data) + read_length} bytes')

        return reply[len(reply) - read_length: len(reply)]

    def spi_batch_write(self, spi_device, writes, timeout=None):
        """Run multiple SPI write-only segments in one ``spi_batch_write`` RPC.

        Each element of ``writes`` is clocked out in a separate chip-select
        assertion (CS deasserts between segments), which is required by devices
        such as the HMC7044.

        Parameters:
            spi_device (int): SPI port index on the board processor.
            writes (list of bytes): segments to send, each at most 255 bytes.
            timeout (float): RPC socket timeout in seconds.

        Returns:
            None

        Raises:
            ValueError: if segment count or length limits are exceeded.
            IOError: if the board reports a parse error or a failing segment.
        """
        if not writes:
            raise ValueError(f'{self!r}: spi_batch_write requires at least one segment')
        n = len(writes)
        if n > 255:
            raise ValueError(f'{self!r}: spi_batch_write supports at most 255 segments, got {n}')
        parts = [bytes((spi_device, n))]
        for i, segment in enumerate(writes):
            if isinstance(segment, memoryview):
                segment = segment.tobytes()
            elif not isinstance(segment, (bytes, bytearray)):
                segment = bytes(segment)
            seg_len = len(segment)
            if seg_len > 255:
                raise ValueError(
                    f'{self!r}: spi_batch_write segment {i} length {seg_len} exceeds 255 bytes')
            parts.append(bytes([seg_len]))
            parts.append(segment)
        payload = b''.join(parts)
        reply = self._rpc_write_read(self.RPC_SPI_BATCH_WRITE, payload, read_length=1, timeout=timeout)
        if len(reply) != 1:
            raise IOError(f'{self!r}: spi_batch_write expected 1 reply byte, got {len(reply)}')
        if reply[0] == 0:
            return
        if reply[0] == 0xFF:
            raise IOError(f'{self!r}: spi_batch_write failed (protocol/parse error on board)')
        raise IOError(
            f'{self!r}: spi_batch_write failed at segment index {reply[0] - 1} '
            f'(1-based status code {reply[0]})')

    async def spi_write_read_async(self, spi_device, data, read_length, timeout=None):
        """Async variant of :meth:`spi_write_read`."""
        if not isinstance(data, (bytes, bytearray)):
            data = bytes(data)
        payload = bytes((spi_device, read_length)) + data + b'\x00' * read_length
        reply = await self._rpc_write_read_async(
            self.RPC_SPI_WRITE_READ,
            payload,
            read_length=1 + len(data) + read_length,
            timeout=timeout)
        if reply[0]:
            raise IOError(f'{self!r}: SPI Reply has error code {reply[0]}')
        if len(reply) != 1 + len(data) + read_length:
            raise IOError(
                f'{self!r}: SPI Received {len(reply)} bytes instead of '
                f'{1 + len(data) + read_length} bytes')
        return reply[len(reply) - read_length: len(reply)]

    async def spi_batch_write_async(self, spi_device, writes, timeout=None):
        """Async variant of :meth:`spi_batch_write`."""
        if not writes:
            raise ValueError(f'{self!r}: spi_batch_write requires at least one segment')
        n = len(writes)
        if n > 255:
            raise ValueError(
                f'{self!r}: spi_batch_write supports at most 255 segments, got {n}')
        parts = [bytes((spi_device, n))]
        for i, segment in enumerate(writes):
            if isinstance(segment, memoryview):
                segment = segment.tobytes()
            elif not isinstance(segment, (bytes, bytearray)):
                segment = bytes(segment)
            seg_len = len(segment)
            if seg_len > 255:
                raise ValueError(
                    f'{self!r}: spi_batch_write segment {i} length {seg_len} '
                    f'exceeds 255 bytes')
            parts.append(bytes([seg_len]))
            parts.append(segment)
        payload = b''.join(parts)
        reply = await self._rpc_write_read_async(
            self.RPC_SPI_BATCH_WRITE, payload, read_length=1, timeout=timeout)
        if len(reply) != 1:
            raise IOError(
                f'{self!r}: spi_batch_write expected 1 reply byte, got {len(reply)}')
        if reply[0] == 0:
            return
        if reply[0] == 0xFF:
            raise IOError(
                f'{self!r}: spi_batch_write failed (protocol/parse error on board)')
        raise IOError(
            f'{self!r}: spi_batch_write failed at segment index {reply[0] - 1} '
            f'(1-based status code {reply[0]})')

    #########################################################
    #  FPGA bitstream programming
    #########################################################

    async def set_fpga_bitstream_async(self, data, crc=0, timeout=40):
        """Programs the FPGA with the provided bitstream.

        Uses a non-blocking ``sock_sendall`` so other boards' bitstream
        uploads can run concurrently on the same event loop. The socket is
        restored to blocking mode before return so subsequent RPCs are
        unchanged.

        Parameters:

            data (bytes): bitstream, without header, uncompressed. Should be 34437356 bytes long for the ZU28.
        """

        self.log.info('%r: programming FPGA with %d bytes', self, len(data))

        assert len(data) == 34437356, "ZU27/28/47/48 bitstream should be 34437356 bytes long"

        loop = asyncio.get_running_loop()
        old_timeout = self.sock.gettimeout()
        async with self._sock_lock:
            self.sock.setblocking(False)
            try:
                await asyncio.wait_for(loop.sock_sendall(self.sock, data), timeout=timeout)
                self.firmware_crc = crc
            except Exception:
                self.firmware_crc = None
                raise
            finally:
                self.sock.setblocking(True)
                self.sock.settimeout(old_timeout)

    def get_fpga_bitstream_crc(self):
        return self.firmware_crc

    def is_fpga_programmed(self):
        return True

    #########################################################
    #  TCPipe-backed BSB memory-mapped interface
    #########################################################

    async def open_tcpipe_mmi_async(self):
        """Return the TCPipe-backed BSB memory-mapped interface (``TCPipe_BSB_MMI``) for the FPGA.
        
        Returns:
            TCPipe_BSB_MMI: The TCPipe-backed BSB memory-mapped interface instance.
        """
        return TCPipe_BSB_MMI(self)

    #########################################################
    #  TCPipe processor register access (RPC)
    #########################################################

    def read_reg(self, reg):
        reply = self._rpc_write_read(self.RPC_CORE_REG_READ, bytes((reg,)))
        if reply[0]:
            raise IOError(f'read_reg reply has error code {reply[0]}')
        if len(reply) != 5:
            raise IOError(f'read_reg received {len(reply)} bytes instead of {5} bytes')

        return int.from_bytes(reply[1:5], 'little')

    def write_reg(self, reg, value):
        payload = bytes((reg,)) + value.to_bytes(4, 'little')
        reply = self._rpc_write_read(self.RPC_CORE_REG_WRITE, payload, read_length=1)
        if reply[0]:
            raise IOError(f'write_reg reply has error code {reply[0]}')

    def set_local_data_port_number(self, port):
        """Set the UDP data-channel destination port (TCPipe processor register, not FPGA core map)."""
        self.write_reg(self._UDP_DATA_PORT_REG, port)

    def get_local_data_port_number(self):
        """Return the UDP data-channel destination port (TCPipe processor register, not FPGA core map)."""
        return self.read_reg(self._UDP_DATA_PORT_REG)


    #########################################################
    #  ADC / RFDC
    #########################################################

    def adc_sync(self):
        """ Synchronizes the ADCs.

        Parameters: None

        """
        return self._rpc_write_read(self.RPC_ADC_SYNC)

    def _rfdc_rpc(self, rpc_id, payload=b'', reply_fmt='', timeout=None):
        extra = struct.calcsize(reply_fmt) if reply_fmt else 0
        reply = self._rpc_write_read(rpc_id, payload, read_length=4 + extra, timeout=timeout)
        status = struct.unpack_from('<I', reply, 0)[0]
        if status:
            name = self.RPC_NAMES.get(rpc_id, f'rpc_{rpc_id}')
            raise IOError(f'{self!r}: {name} failed with XRFdc status {status}')
        return reply[4:]

    @staticmethod
    def _rfdc_tile_status(values):
        return {
            'is_enabled': values[0],
            'tile_state': values[1],
            'block_status_mask': values[2],
            'power_up_state': values[3],
            'pll_state': values[4],
        }

    @classmethod
    def _unpack_ip_status(cls, data):
        tile_size = struct.calcsize(cls._RFDC_TILE_STATUS)
        tiles = []
        offset = 0
        for _ in range(8):
            tiles.append(cls._rfdc_tile_status(struct.unpack_from(cls._RFDC_TILE_STATUS, data, offset)))
            offset += tile_size
        state = struct.unpack_from('<I', data, offset)[0]
        return {
            'dac_tile_status': tiles[:4],
            'adc_tile_status': tiles[4:],
            'state': state,
        }

    @classmethod
    def _unpack_block_status(cls, data):
        sampling_freq, analog, digital, clocks, fifo_en, fifo_asserted = struct.unpack(cls._RFDC_BLOCK_STATUS, data)
        return {
            'sampling_freq': sampling_freq,
            'analog_data_path_status': analog,
            'digital_data_path_status': digital,
            'data_path_clocks_status': clocks,
            'is_fifo_flags_enabled': fifo_en,
            'is_fifo_flags_asserted': fifo_asserted,
        }

    @classmethod
    def _pack_mixer(cls, freq, phase_offset, event_source, coarse_mix_freq, mixer_mode,
                    fine_mixer_scale, mixer_type):
        return struct.pack(cls._RFDC_MIXER, freq, phase_offset, event_source, coarse_mix_freq,
                           mixer_mode, fine_mixer_scale, mixer_type)

    @classmethod
    def _unpack_mixer(cls, data):
        freq, phase_offset, event_source, coarse_mix_freq, mixer_mode, fine_mixer_scale, mixer_type = (
            struct.unpack(cls._RFDC_MIXER, data))
        return {
            'freq': freq,
            'phase_offset': phase_offset,
            'event_source': event_source,
            'coarse_mix_freq': coarse_mix_freq,
            'mixer_mode': mixer_mode,
            'fine_mixer_scale': fine_mixer_scale,
            'mixer_type': mixer_type,
        }

    @classmethod
    def _pack_qmc(cls, enable_phase, enable_gain, gain_correction_factor, phase_correction_factor,
                  offset_correction_factor, event_source):
        return struct.pack(cls._RFDC_QMC, enable_phase, enable_gain, gain_correction_factor,
                           phase_correction_factor, offset_correction_factor, event_source)

    @classmethod
    def _unpack_qmc(cls, data):
        enable_phase, enable_gain, gain, phase, offset, event_source = struct.unpack(cls._RFDC_QMC, data)
        return {
            'enable_phase': enable_phase,
            'enable_gain': enable_gain,
            'gain_correction_factor': gain,
            'phase_correction_factor': phase,
            'offset_correction_factor': offset,
            'event_source': event_source,
        }

    @classmethod
    def _unpack_pll(cls, data):
        (enabled, ref_clk_freq, sample_rate, ref_clk_divider, feedback_divider, output_divider,
         fractional_mode, fractional_data, fract_width) = struct.unpack(cls._RFDC_PLL, data)
        return {
            'enabled': enabled,
            'ref_clk_freq': ref_clk_freq,
            'sample_rate': sample_rate,
            'ref_clk_divider': ref_clk_divider,
            'feedback_divider': feedback_divider,
            'output_divider': output_divider,
            'fractional_mode': fractional_mode,
            'fractional_data': fractional_data,
            'fract_width': fract_width,
        }

    @classmethod
    def _pack_signal_detector(cls, mode, time_constant, flush, enable_integrator, threshold,
                              thresh_on_trigger_cnt, thresh_off_trigger_cnt, hysteresis_enable):
        return struct.pack(cls._RFDC_SIGNAL_DETECTOR, mode, time_constant, flush, enable_integrator,
                           threshold, thresh_on_trigger_cnt, thresh_off_trigger_cnt, hysteresis_enable)

    @classmethod
    def _unpack_signal_detector(cls, data):
        (mode, time_constant, flush, enable_integrator, threshold, thresh_on, thresh_off,
         hysteresis_enable) = struct.unpack(cls._RFDC_SIGNAL_DETECTOR, data)
        return {
            'mode': mode,
            'time_constant': time_constant,
            'flush': flush,
            'enable_integrator': enable_integrator,
            'threshold': threshold,
            'thresh_on_trigger_cnt': thresh_on,
            'thresh_off_trigger_cnt': thresh_off,
            'hysteresis_enable': hysteresis_enable,
        }

    @classmethod
    def _pack_threshold(cls, update_threshold, threshold_mode, threshold_avg_val,
                        threshold_under_val, threshold_over_val):
        return struct.pack(
            cls._RFDC_THRESHOLD,
            update_threshold,
            threshold_mode[0], threshold_mode[1],
            threshold_avg_val[0], threshold_avg_val[1],
            threshold_under_val[0], threshold_under_val[1],
            threshold_over_val[0], threshold_over_val[1])

    @classmethod
    def _unpack_threshold(cls, data):
        values = struct.unpack(cls._RFDC_THRESHOLD, data)
        return {
            'update_threshold': values[0],
            'threshold_mode': [values[1], values[2]],
            'threshold_avg_val': [values[3], values[4]],
            'threshold_under_val': [values[5], values[6]],
            'threshold_over_val': [values[7], values[8]],
        }

    @classmethod
    def _pack_tile_clock(cls, settings):
        return struct.pack(
            '<' + cls._RFDC_TILE_CLOCK,
            settings['source_type'],
            settings['source_tile'],
            settings['pll_enable'],
            settings['ref_clk_freq'],
            settings['sample_rate'],
            settings['division_factor'],
            settings['distributed_clock'],
            settings['delay'])

    @classmethod
    def _unpack_tile_clock(cls, data, offset=0):
        values = struct.unpack_from('<' + cls._RFDC_TILE_CLOCK, data, offset)
        return {
            'source_type': values[0],
            'source_tile': values[1],
            'pll_enable': values[2],
            'ref_clk_freq': values[3],
            'sample_rate': values[4],
            'division_factor': values[5],
            'distributed_clock': values[6],
            'delay': values[7],
        }

    @classmethod
    def _pack_clk_distribution(cls, settings):
        info = settings['info']
        payload = struct.pack(
            '<II2I2IdI8dI6B',
            settings['source_tile_id'],
            settings['source_type'],
            settings['edge_tile_ids'][0], settings['edge_tile_ids'][1],
            settings['edge_types'][0], settings['edge_types'][1],
            settings['dist_ref_clk_freq'],
            settings['distributed_clock'],
            *([settings['sample_rates'][t][tile] for t in range(2) for tile in range(4)]),
            settings['shutdown_mode'],
            info['max_delay'], info['min_delay'], info['is_delay_balanced'],
            info['source'], info['upper_bound'], info['lower_bound'])
        clocks = bytearray()
        for t in range(2):
            for tile in range(4):
                clocks.extend(cls._pack_tile_clock(info['clk_settings'][t][tile]))
        return payload + bytes(clocks)

    @classmethod
    def _unpack_clk_distribution(cls, data, offset=0):
        header_fmt = '<II2I2IdI8dI6B'
        header_size = struct.calcsize(header_fmt)
        clock_size = struct.calcsize('<' + cls._RFDC_TILE_CLOCK)
        values = struct.unpack_from(header_fmt, data, offset)
        clk_settings = [[None] * 4 for _ in range(2)]
        clock_offset = offset + header_size
        for t in range(2):
            for tile in range(4):
                clk_settings[t][tile] = cls._unpack_tile_clock(data, clock_offset)
                clock_offset += clock_size
        return {
            'source_tile_id': values[0],
            'source_type': values[1],
            'edge_tile_ids': [values[2], values[3]],
            'edge_types': [values[4], values[5]],
            'dist_ref_clk_freq': values[6],
            'distributed_clock': values[7],
            'sample_rates': [
                [values[8], values[9], values[10], values[11]],
                [values[12], values[13], values[14], values[15]],
            ],
            'shutdown_mode': values[16],
            'info': {
                'max_delay': values[17],
                'min_delay': values[18],
                'is_delay_balanced': values[19],
                'source': values[20],
                'upper_bound': values[21],
                'lower_bound': values[22],
                'clk_settings': clk_settings,
            },
        }

    def adc_startup(self, type=ADC_TILE, tile_id=-1):
        """Start up an RFDC tile. ``tile_id=-1`` selects all tiles."""
        self._rfdc_rpc(self.RPC_ADC_STARTUP, struct.pack('<Bb', type, tile_id))

    def adc_rerun_fg_cal(self, tile_id=-1, start_state=None, end_state=None):
        """Rerun ADC foreground calibration (``XRFdc_CustomStartUp`` CAL → FULL).

        ``tile_id=-1`` runs all ADC tiles 0–3. Default states match Xilinx FG cal rerun.
        """
        if start_state is None:
            start_state = self.RFDC_STATE_CAL
        if end_state is None:
            end_state = self.RFDC_STATE_FULL
        self._rfdc_rpc(
            self.RPC_ADC_RERUN_FG_CAL,
            struct.pack('<bII', tile_id, start_state, end_state),
        )

    def adc_shutdown(self, type=ADC_TILE, tile_id=-1):
        """Shut down an RFDC tile. ``tile_id=-1`` selects all tiles."""
        self._rfdc_rpc(self.RPC_ADC_SHUTDOWN, struct.pack('<Bb', type, tile_id))

    def adc_reset(self, type=ADC_TILE, tile_id=-1):
        """Reset an RFDC tile. ``tile_id=-1`` selects all tiles."""
        self._rfdc_rpc(self.RPC_ADC_RESET, struct.pack('<Bb', type, tile_id))

    def adc_wait_for_state(self, type=ADC_TILE, tile_id=0, state=0, timeout=5):
        """Wait until an RFDC tile reaches ``state``."""
        self._rfdc_rpc(self.RPC_ADC_WAIT_FOR_STATE, struct.pack('<BBI', type, tile_id, state),
                       timeout=timeout)

    def adc_get_ip_status(self):
        """Return RFDC IP and per-tile status."""
        data = self._rfdc_rpc(self.RPC_ADC_GET_IP_STATUS, reply_fmt='<' + 'IIBII' * 8 + 'I')
        return self._unpack_ip_status(data)

    def adc_get_block_status(self, tile_id, block_id, type=ADC_TILE):
        """Return RFDC block status."""
        data = self._rfdc_rpc(self.RPC_ADC_GET_BLOCK_STATUS,
                              struct.pack('<BBB', type, tile_id, block_id),
                              reply_fmt=self._RFDC_BLOCK_STATUS)
        return self._unpack_block_status(data)

    def adc_setup_fifo(self, enable, type=ADC_TILE, tile_id=-1):
        """Enable or disable the RFDC FIFO. ``tile_id=-1`` selects all tiles."""
        self._rfdc_rpc(self.RPC_ADC_SETUP_FIFO, struct.pack('<BbB', type, tile_id, int(bool(enable))))

    def adc_get_fifo_status(self, type=ADC_TILE, tile_id=0):
        """Return whether the RFDC FIFO is enabled."""
        data = self._rfdc_rpc(self.RPC_ADC_GET_FIFO_STATUS, struct.pack('<BB', type, tile_id),
                              reply_fmt='<B')
        return struct.unpack('<B', data)[0]

    def adc_set_mixer_settings(self, tile_id, block_id, freq, phase_offset, event_source,
                               coarse_mix_freq, mixer_mode, fine_mixer_scale, mixer_type,
                               type=ADC_TILE):
        """Set mixer / NCO settings."""
        self._rfdc_rpc(
            self.RPC_ADC_SET_MIXER_SETTINGS,
            struct.pack('<BBB', type, tile_id, block_id) + self._pack_mixer(
                freq, phase_offset, event_source, coarse_mix_freq, mixer_mode,
                fine_mixer_scale, mixer_type))

    def adc_get_mixer_settings(self, tile_id, block_id, type=ADC_TILE):
        """Return mixer / NCO settings."""
        data = self._rfdc_rpc(self.RPC_ADC_GET_MIXER_SETTINGS,
                              struct.pack('<BBB', type, tile_id, block_id),
                              reply_fmt=self._RFDC_MIXER)
        return self._unpack_mixer(data)

    def adc_reset_nco_phase(self, tile_id, block_id, type=ADC_TILE):
        """Reset NCO phase."""
        self._rfdc_rpc(self.RPC_ADC_RESET_NCO_PHASE, struct.pack('<BBB', type, tile_id, block_id))

    def adc_update_event(self, tile_id, block_id, event, type=ADC_TILE):
        """Trigger an RFDC update event."""
        self._rfdc_rpc(self.RPC_ADC_UPDATE_EVENT, struct.pack('<BBBI', type, tile_id, block_id, event))

    def adc_set_decimation_factor(self, tile_id, block_id, factor):
        """Set ADC decimation factor."""
        self._rfdc_rpc(self.RPC_ADC_SET_DECIMATION_FACTOR, struct.pack('<BBI', tile_id, block_id, factor))

    def adc_get_decimation_factor(self, tile_id, block_id):
        """Return ADC decimation factor."""
        data = self._rfdc_rpc(self.RPC_ADC_GET_DECIMATION_FACTOR, struct.pack('<BB', tile_id, block_id),
                              reply_fmt='<I')
        return struct.unpack('<I', data)[0]

    def adc_set_nyquist_zone(self, tile_id, block_id, zone, type=ADC_TILE):
        """Set Nyquist zone."""
        self._rfdc_rpc(self.RPC_ADC_SET_NYQUIST_ZONE,
                       struct.pack('<BBBI', type, tile_id, block_id, zone))

    def adc_get_nyquist_zone(self, tile_id, block_id, type=ADC_TILE):
        """Return Nyquist zone."""
        data = self._rfdc_rpc(self.RPC_ADC_GET_NYQUIST_ZONE,
                              struct.pack('<BBB', type, tile_id, block_id),
                              reply_fmt='<I')
        return struct.unpack('<I', data)[0]

    def adc_set_dsa(self, tile_id, block_id, disable_rts, attenuation):
        """Set ADC DSA attenuation."""
        self._rfdc_rpc(self.RPC_ADC_SET_DSA,
                       struct.pack('<BB', tile_id, block_id) + struct.pack(self._RFDC_DSA, disable_rts, attenuation))

    def adc_get_dsa(self, tile_id, block_id):
        """Return ADC DSA settings."""
        data = self._rfdc_rpc(self.RPC_ADC_GET_DSA, struct.pack('<BB', tile_id, block_id),
                              reply_fmt=self._RFDC_DSA)
        disable_rts, attenuation = struct.unpack(self._RFDC_DSA, data)
        return {'disable_rts': disable_rts, 'attenuation': attenuation}

    def adc_set_qmc_settings(self, tile_id, block_id, enable_phase, enable_gain,
                             gain_correction_factor, phase_correction_factor,
                             offset_correction_factor, event_source, type=ADC_TILE):
        """Set QMC settings."""
        self._rfdc_rpc(
            self.RPC_ADC_SET_QMC_SETTINGS,
            struct.pack('<BBB', type, tile_id, block_id) + self._pack_qmc(
                enable_phase, enable_gain, gain_correction_factor, phase_correction_factor,
                offset_correction_factor, event_source))

    def adc_get_qmc_settings(self, tile_id, block_id, type=ADC_TILE):
        """Return QMC settings."""
        data = self._rfdc_rpc(self.RPC_ADC_GET_QMC_SETTINGS,
                              struct.pack('<BBB', type, tile_id, block_id),
                              reply_fmt=self._RFDC_QMC)
        return self._unpack_qmc(data)

    def adc_set_coarse_delay(self, tile_id, block_id, coarse_delay, event_source, type=ADC_TILE):
        """Set coarse delay."""
        self._rfdc_rpc(
            self.RPC_ADC_SET_COARSE_DELAY,
            struct.pack('<BBB', type, tile_id, block_id) + struct.pack(self._RFDC_COARSE_DELAY, coarse_delay, event_source))

    def adc_get_coarse_delay(self, tile_id, block_id, type=ADC_TILE):
        """Return coarse delay settings."""
        data = self._rfdc_rpc(self.RPC_ADC_GET_COARSE_DELAY,
                              struct.pack('<BBB', type, tile_id, block_id),
                              reply_fmt=self._RFDC_COARSE_DELAY)
        coarse_delay, event_source = struct.unpack(self._RFDC_COARSE_DELAY, data)
        return {'coarse_delay': coarse_delay, 'event_source': event_source}

    def adc_set_dither(self, tile_id, block_id, mode):
        """Set ADC dither mode."""
        self._rfdc_rpc(self.RPC_ADC_SET_DITHER, struct.pack('<BBI', tile_id, block_id, mode))

    def adc_get_dither(self, tile_id, block_id):
        """Return ADC dither mode."""
        data = self._rfdc_rpc(self.RPC_ADC_GET_DITHER, struct.pack('<BB', tile_id, block_id),
                              reply_fmt='<I')
        return struct.unpack('<I', data)[0]

    def adc_get_clock_source(self, type=ADC_TILE, tile_id=0):
        """Return tile clock source."""
        data = self._rfdc_rpc(self.RPC_ADC_GET_CLOCK_SOURCE, struct.pack('<BB', type, tile_id),
                              reply_fmt='<I')
        return struct.unpack('<I', data)[0]

    def adc_get_pll_lock_status(self, type=ADC_TILE, tile_id=0):
        """Return tile PLL lock status."""
        data = self._rfdc_rpc(self.RPC_ADC_GET_PLL_LOCK_STATUS, struct.pack('<BB', type, tile_id),
                              reply_fmt='<I')
        return struct.unpack('<I', data)[0]

    def adc_get_pll_config(self, type=ADC_TILE, tile_id=0):
        """Return tile PLL configuration."""
        data = self._rfdc_rpc(self.RPC_ADC_GET_PLL_CONFIG, struct.pack('<BB', type, tile_id),
                              reply_fmt=self._RFDC_PLL)
        return self._unpack_pll(data)

    def adc_dynamic_pll_config(self, source, ref_clk_freq, sampling_rate, type=ADC_TILE, tile_id=0):
        """Configure a tile PLL dynamically."""
        self._rfdc_rpc(self.RPC_ADC_DYNAMIC_PLL_CONFIG,
                       struct.pack('<BBBdd', type, tile_id, source, ref_clk_freq, sampling_rate))

    def adc_set_calibration_mode(self, tile_id, block_id, mode):
        """Set ADC calibration mode."""
        self._rfdc_rpc(self.RPC_ADC_SET_CALIBRATION_MODE, struct.pack('<BBB', tile_id, block_id, mode))

    def adc_get_calibration_mode(self, tile_id, block_id):
        """Return ADC calibration mode."""
        data = self._rfdc_rpc(self.RPC_ADC_GET_CALIBRATION_MODE, struct.pack('<BB', tile_id, block_id),
                              reply_fmt='<B')
        return struct.unpack('<B', data)[0]

    def adc_set_cal_freeze(self, tile_id, block_id, cal_frozen, disable_freeze_pin, freeze_calibration):
        """Set ADC calibration freeze settings."""
        self._rfdc_rpc(
            self.RPC_ADC_SET_CAL_FREEZE,
            struct.pack('<BB', tile_id, block_id) + struct.pack(
                self._RFDC_CAL_FREEZE, cal_frozen, disable_freeze_pin, freeze_calibration))

    def adc_get_cal_freeze(self, tile_id, block_id):
        """Return ADC calibration freeze settings."""
        data = self._rfdc_rpc(self.RPC_ADC_GET_CAL_FREEZE, struct.pack('<BB', tile_id, block_id),
                              reply_fmt=self._RFDC_CAL_FREEZE)
        cal_frozen, disable_freeze_pin, freeze_calibration = struct.unpack(self._RFDC_CAL_FREEZE, data)
        return {
            'cal_frozen': cal_frozen,
            'disable_freeze_pin': disable_freeze_pin,
            'freeze_calibration': freeze_calibration,
        }

    def adc_set_cal_coefficients(self, tile_id, block_id, calibration_block, coeff):
        """Set ADC calibration coefficients (8 uint32 values)."""
        self._rfdc_rpc(
            self.RPC_ADC_SET_CAL_COEFFICIENTS,
            struct.pack('<BBI', tile_id, block_id, calibration_block) + struct.pack(self._RFDC_CAL_COEFF, *coeff))

    def adc_get_cal_coefficients(self, tile_id, block_id, calibration_block):
        """Return ADC calibration coefficients."""
        data = self._rfdc_rpc(self.RPC_ADC_GET_CAL_COEFFICIENTS,
                              struct.pack('<BBI', tile_id, block_id, calibration_block),
                              reply_fmt=self._RFDC_CAL_COEFF)
        return list(struct.unpack(self._RFDC_CAL_COEFF, data))

    def adc_set_signal_detector(self, tile_id, block_id, mode, time_constant, flush, enable_integrator,
                                threshold, thresh_on_trigger_cnt, thresh_off_trigger_cnt,
                                hysteresis_enable):
        """Set ADC signal detector (Gen3+)."""
        self._rfdc_rpc(
            self.RPC_ADC_SET_SIGNAL_DETECTOR,
            struct.pack('<BB', tile_id, block_id) + self._pack_signal_detector(
                mode, time_constant, flush, enable_integrator, threshold,
                thresh_on_trigger_cnt, thresh_off_trigger_cnt, hysteresis_enable))

    def adc_get_signal_detector(self, tile_id, block_id):
        """Return ADC signal detector settings (Gen3+)."""
        data = self._rfdc_rpc(self.RPC_ADC_GET_SIGNAL_DETECTOR, struct.pack('<BB', tile_id, block_id),
                              reply_fmt=self._RFDC_SIGNAL_DETECTOR)
        return self._unpack_signal_detector(data)

    def adc_get_signal_detector_status(self, tile_id, block_id):
        """Return live ADC signal detector magnitude and threshold compare (Gen3+).

        On Dual/HS ADCs the PL ``signal_detect`` pin is the AND of both interleaved
        slices. ``magnitude`` is this user block; ``pair_magnitude`` is the companion
        slice. ``pl_signal_detect`` matches that AND (instant compare, not debounce).
        """
        data = self._rfdc_rpc(
            self.RPC_ADC_GET_SIGNAL_DETECTOR_STATUS,
            struct.pack('<BB', tile_id, block_id),
            reply_fmt=self._RFDC_SIGNAL_DETECTOR_STATUS)
        (magnitude, pair_magnitude, threshold, pair_threshold, above_threshold,
         pair_above_threshold, pl_signal_detect, high_speed) = struct.unpack(
            self._RFDC_SIGNAL_DETECTOR_STATUS, data)
        return {
            'magnitude': magnitude,
            'pair_magnitude': pair_magnitude,
            'threshold': threshold,
            'pair_threshold': pair_threshold,
            'above_threshold': above_threshold,
            'pair_above_threshold': pair_above_threshold,
            'pl_signal_detect': pl_signal_detect,
            'high_speed': high_speed,
        }

    def adc_set_clk_distribution(self, settings):
        """Set RFDC clock distribution (Gen3+). ``settings`` matches ``adc_get_clk_distribution`` entries."""
        packed = self._pack_clk_distribution(settings)
        if len(packed) != struct.calcsize('<' + self._RFDC_DIST):
            raise ValueError(f'{self!r}: clock distribution payload is {len(packed)} bytes, expected {struct.calcsize("<" + self._RFDC_DIST)}')
        self._rfdc_rpc(self.RPC_ADC_SET_CLK_DISTRIBUTION, packed)

    def adc_get_clk_distribution(self):
        """Return the eight RFDC clock-distribution entries (Gen3+)."""
        data = self._rfdc_rpc(self.RPC_ADC_GET_CLK_DISTRIBUTION, reply_fmt=self._RFDC_DIST_SYS)
        entry_size = struct.calcsize('<' + self._RFDC_DIST)
        return [self._unpack_clk_distribution(data, i * entry_size) for i in range(8)]

    def adc_set_threshold_settings(self, tile_id, block_id, update_threshold, threshold_mode,
                                   threshold_avg_val, threshold_under_val, threshold_over_val):
        """Set ADC threshold settings. Mode/avg/under/over are two-element sequences."""
        self._rfdc_rpc(
            self.RPC_ADC_SET_THRESHOLD_SETTINGS,
            struct.pack('<BB', tile_id, block_id) + self._pack_threshold(
                update_threshold, threshold_mode, threshold_avg_val,
                threshold_under_val, threshold_over_val))

    def adc_get_threshold_settings(self, tile_id, block_id):
        """Return ADC threshold settings."""
        data = self._rfdc_rpc(self.RPC_ADC_GET_THRESHOLD_SETTINGS, struct.pack('<BB', tile_id, block_id),
                              reply_fmt=self._RFDC_THRESHOLD)
        return self._unpack_threshold(data)

    def adc_set_threshold_clr_mode(self, tile_id, block_id, threshold_to_update, clr_mode):
        """Set ADC threshold clear mode."""
        self._rfdc_rpc(self.RPC_ADC_SET_THRESHOLD_CLR_MODE,
                       struct.pack('<BBII', tile_id, block_id, threshold_to_update, clr_mode))

    def adc_threshold_sticky_clear(self, tile_id, block_id, threshold_to_update):
        """Clear a sticky ADC threshold."""
        self._rfdc_rpc(self.RPC_ADC_THRESHOLD_STICKY_CLEAR,
                       struct.pack('<BBI', tile_id, block_id, threshold_to_update))

    def adc_set_datapath_mode(self, tile_id, block_id, mode):
        """Set DAC datapath mode."""
        self._rfdc_rpc(self.RPC_ADC_SET_DATAPATH_MODE, struct.pack('<BBI', tile_id, block_id, mode))

    def adc_get_datapath_mode(self, tile_id, block_id):
        """Return DAC datapath mode."""
        data = self._rfdc_rpc(self.RPC_ADC_GET_DATAPATH_MODE, struct.pack('<BB', tile_id, block_id),
                              reply_fmt='<I')
        return struct.unpack('<I', data)[0]

    def adc_set_imr_pass_mode(self, tile_id, block_id, mode):
        """Set DAC IMR pass mode."""
        self._rfdc_rpc(self.RPC_ADC_SET_IMR_PASS_MODE, struct.pack('<BBI', tile_id, block_id, mode))

    def adc_get_imr_pass_mode(self, tile_id, block_id):
        """Return DAC IMR pass mode."""
        data = self._rfdc_rpc(self.RPC_ADC_GET_IMR_PASS_MODE, struct.pack('<BB', tile_id, block_id),
                              reply_fmt='<I')
        return struct.unpack('<I', data)[0]

    def adc_set_pwr_mode(self, tile_id, block_id, disable_ip_control, pwr_mode, type=ADC_TILE):
        """Set RFDC power mode."""
        self._rfdc_rpc(
            self.RPC_ADC_SET_PWR_MODE,
            struct.pack('<BBB', type, tile_id, block_id) + struct.pack(self._RFDC_PWR_MODE, disable_ip_control, pwr_mode))

    def adc_get_pwr_mode(self, tile_id, block_id, type=ADC_TILE):
        """Return RFDC power mode."""
        data = self._rfdc_rpc(self.RPC_ADC_GET_PWR_MODE, struct.pack('<BBB', type, tile_id, block_id),
                              reply_fmt=self._RFDC_PWR_MODE)
        disable_ip_control, pwr_mode = struct.unpack(self._RFDC_PWR_MODE, data)
        return {'disable_ip_control': disable_ip_control, 'pwr_mode': pwr_mode}


class TCPipe_I2C:
    """
    Provides an I2C-over-TCPIPE interface using the standardized I2C object API.

    An instance of this object is passed to the I2C drivers to provide them the methods to access their hardware.
    """

    I2CException = IOError  # Exception object to expect from I2C communication errors

    def __init__(self, tcpipe, verbose=None):
        super().__init__()
        self.tcpipe = tcpipe
        self._logger = logging.getLogger(__name__)
        self.current_port = None;  # I2C port currently in use
        self.current_switch_params = {}  # keep track of switch params so we don't set the switch needlessly

    def __repr__(self):
        return repr(self.tcpipe)

    def select_bus(self, bus_info, retry=1):
        """
        Configure the I2C port and I2C switches so the following communications will access the
        desired I2C bus. 'bus_id' can be a bus name or bus number, or a list of those if multiple
        buses are to be accessed at the same time. An error will be provided if all the buses are
        not accessible through the same FPGA I2C port. This function assumes that each FPGA I2C port
        has an identical I2C switch.

        Parameters:

            bus_info (int, dict, tuple): Describes the port, switch and switch parameter
                It can be either a

                - ``i2c_port`` integer,
                - ``(switch_obj, switch_params)`` tuple, or ``{"port":i2c_port, "switch":switch_obj,
                  "switch_params": switch_params"}`` dict

                where

                - i2c_port (int): I2C port (aka bus number) to use. If not specified, we'll use the I2C port of the specified switch, if any.
                - switch (instance): switch object instance
                - switch_params (int or dict): arguments to pass to the switch instance

            args, kwargs: passed to the bus select function

        Exceptions:

            IOError: Raised by an FPGA-based I2C controller in case of transaction errors

        Example:

            - select_bus(1)  # activate I2C port 1
            - select_bus((some_i2c_switch, 3)) # select bus from specified switch, and enable I2C port 3 of the switch

        """

        if isinstance(bus_info, int):
            switch = switch_params = None
            port = bus_info
        elif isinstance(bus_info, tuple):
            (switch, switch_params) = bus_info
            port = None
        elif isinstance(bus_info, dict):
            port = bus_info.get('port')
            switch = bus_info.get('switch')
            switch_params = bus_info.get('switch_params')
        else:
            raise TypeError('Invalid port/bus info type')

        if port is None and switch:
            port = switch.port

        self.current_port = port

        if not switch:
            return

        if self.current_switch_params.setdefault(switch, None) == switch_params:
            return

        if isinstance(switch_params, dict):
            switch.set_port(**switch_params)
        else:
            switch.set_port(switch_params)
        self.current_switch_params[switch] = switch_params

    # def write_read(self, *args, **kwargs):
    def write_read(self, addr=0, data=[0], read_length=0, verbose=1, noerror=False, retry=1):
        """
        Writes and read to/from I2C device at address `addr`.


        Parameters:

            addr (int): I2C address

            data (list of int): list of bytes to write before the read operation. If ``None``, no write is performed.

            read_length (int): Number of bytes to read. Can be 0-4 after the a
                preceding write operation, or 0-3 without a write operation.

            verbose (int): verbosity level

            noerror (bool): if True, no exception will be raised

            retry (int): Number of times to retry a transfer before raising an exception

        Returns:
            bytearray containing the read bytes

        Exceptions:

            IOError: Raised by an FPGA-based I2C controller in case of transaction errors
            ValueError: Is raised when `addr`, `read>_length` or `write_length` are out of range.

        """
        # self._logger.debug("Accessing I2C bus...")

        # Encode PS I2C port in bit 7 of the TCPipe address byte.
        # Do not use truthiness on current_port: port 0 is valid and must stay
        # distinct from "never selected" (None).
        if self.current_port is None:
            raise RuntimeError(
                f'{self!r}: I2C port not selected; call select_bus() before write_read/is_present')
        addr = (addr & 0x7F) | ((int(self.current_port) & 1) << 7)

        if not data:  # if we have no data to write, just read
            return list(self.tcpipe.i2c_read(addr,read_length)) # list for backwards compatibility
        elif not read_length:  # if we have data to write but nothing to read
            return self.tcpipe.i2c_write(addr, data)
        else:  # if we both write and read
            return list(self.tcpipe.i2c_write_read(addr, data, read_length))

    def is_present(self, addr, bus_name=None):
        """ Test the presence of an I2C device at the specified address.

        Parameters:

            addr (int): I2C address of the device to query

            bus_name (str, int, or list of str or int): I2C bus(es) to activate.
                Required unless ``select_bus()`` was already called. Pass ``0``
                for PS I2C0 (truthy check would skip it).

        """
        if bus_name is not None:
            self.select_bus(bus_name, retry=3)
        try:
            self.write_read(addr, data=[], read_length=0, retry=0)  # dummy I2C access
        except (IOError, OSError):
            return False
        return True


class TCPipe_SPI:
    """
    Provides an SPI-over-TCPIPE interface using the standardized SPI object API.

    An instance of this object is passed to the SPI devices to provide them the methods to access their hardware.
    """

    SPIException = IOError  # Exception object to expect from SPI communication errors

    def __init__(self, tcpipe, verbose=None):
        super().__init__()
        self.tcpipe = tcpipe
        self._logger = logging.getLogger(__name__)
        self.current_port = None;  # I2C port currently in use

    def __repr__(self):
        return repr(self.tcpipe)

    # def select_bus(self, bus_info, retry=1):
    #     """
    #     Configure the I2C port and I2C switches so the following
    #     communications will access the desired I2C bus. 'bus_id'
    #     can be a bus name or bus number, or a list of those if
    #     multiple buses are to be accessed at the same time. An
    #     error will be provided if all the buses are not accessible
    #     through the same FPGA I2C port. This function assumes that
    #     each FPGA I2C port has an identical I2C switch.

    #     Parameters:

    #         bus_info (int, dict, tuple): Describes the port, switch and switch parameter
    #             It can be either a
    #                 - ``i2c_port`` integer,
    #                 - ``(switch_obj, switch_params)`` tuple, or
    #                 ``{"port":i2c_port, "switch":switch_obj, "switch_params": switch_params"} dict

    #             where

    #                 - i2c_port (int): I2C port (aka bus number) to use. If not specified, we'll use the I2C port of the specified switch, if any.
    #                 - switch (instance): switch object instance
    #                 - switch_params (int or dict): arguments to pass to the switch instance

    #         args, kwargs: passed to the bus select function

    #     Exceptions:

    #         IOError: Raised by an FPGA-based I2C controller in case of transaction errors

    #     Example:

    #         select_bus(1)  # activate I2C port 1
    #         select_bus((some_i2c_switch, 3)) # select bus from specified switch, and enable I2C port 3 of the switch

    #     """

    #     if isinstance(bus_info, int):
    #         switch = switch_params = None
    #         port = bus_info
    #     elif isinstance(bus_info, tuple):
    #         (switch, switch_params) = bus_info
    #         port = None
    #     elif isinstance(bus_info, dict):
    #         port = bus_info.get('port')
    #         switch = bus_info.get('switch')
    #         switch_params = bus_info.get('switch_params')
    #     else:
    #         raise TypeError('Invalid port/bus info type')

    #     if port is None and switch:
    #         port = switch.port

    #     self.current_port = port

    #     if not switch:
    #         return

    #     if self.current_switch_params.setdefault(switch, None) == switch_params:
    #         return

    #     if isinstance(switch_params, dict):
    #         switch.set_port(**switch_params)
    #     else:
    #         switch.set_port(switch_params)
    #     self.current_switch_params[switch] = switch_params

    # def write_read(self, *args, **kwargs):
    def write_read(self, spi_device=0, data=[], read_length=0, verbose=1, noerror=False, retry=1, timeout=None):
        """
        Writes and read to/from SPI device `spi_device`.


        Parameters:

            spi_device (int): Number of the SPI device to access, which corresponde to the index of the chip select line to activate.


            data (bytes or list of int): list of bytes to write before the read operation. If ``None``, no write is performed.

            read_length (int): Number of bytes to read.

            verbose (int): verbosity level

            noerror (bool): if True, no exception will be raised

            retry (int): Number of times to retry a transfer before raising an exception

            timeout (float): Time to wait (in seconds) before timeout exception

        Returns:
            bytearray containing the read bytes

        Exceptions:

            IOError: Raised by an FPGA-based I2C controller in case of transaction errors
            ValueError: Is raised when `addr`, `read>_length` or `write_length` are out of range.

        """
        # self._logger.debug("Accessing I2C bus...")

        return self.tcpipe.spi_write_read(spi_device, data, read_length, timeout=timeout)

    def batch_write(self, spi_device, writes, timeout=None):
        """Write multiple SPI segments with CS deasserted between each.

        Parameters:
            spi_device (int): SPI port index.
            writes (list of bytes): one segment per list element (max 255 bytes each).
            timeout (float): RPC socket timeout in seconds.

        Returns:
            None
        """
        return self.tcpipe.spi_batch_write(spi_device, writes, timeout=timeout)

    # def is_present(self, addr, bus_name=None):
    #     """ Test the presence of an I2C device at the specified address.

    #     Parameters:

    #         addr (int): I2C address of the device to query

    #         bus_name (str, int, or list of str or int): I2C bus(es) to activate

    #     """
    #     if bus_name:
    #         self.select_bus(bus_name, retry=3)
    #     try:
    #         self.write_read(addr, data=[], read_length=1, retry=0)  # dummy I2C acces
    #     except (IOError, OSError):
    #         return False
    #     return True


class TCPipe_BSB_MMI(BSB_MMI):
    """
    Provides read/write functions for accessing the FPGA's memory-map registers through a
    Byte-serial Bus interface through the TCPipe interface.

    Parameters:
        tcpipe (TCPipe): The TCPipe object, which will be used to access the FPGA core registers.
    """
    # Maximum packet lengths, limited by the size of the FIFOs
    MAX_BSB_COMMAND_PACKET_LENGTH = 4096
    MAX_BSB_REPLY_PACKET_LENGTH = 16384
    def __init__(self, tcpipe):

        super().__init__()
        self.tcpipe = tcpipe
        self.send_counter = 0
        self.recv_counter = 0

    def __repr__(self):
        return repr(self.tcpipe)

    @contextmanager
    def aggregate_commands(self):
        """Coalesce write-only BSB MMI transactions via the TCPipe batch RPC."""
        with self.tcpipe.aggregate_commands():
            yield self

    def _send_command(self, cmd, expected_reply_length, retry=1, resync=False, **kwargs):
        """ Send a BSB command and return the reply.

        Parameters:
            cmd (bytes): the BSB command to send.
            expected_reply_length (int): the expected length of the reply.
            retry (int): (unused)
            resync (bool): (unused)
            **kwargs: (unused)

        Returns:
            bytes: the reply from the BSB command. b'' for a write-only command

        Raises:
            IOError: if the reply is not the expected length.
        """
        if expected_reply_length == 0:
            self.tcpipe.bsb_batch_write((cmd,))
            return b''
        reply = self.tcpipe.bsb_write_read(
            cmd, read_length=expected_reply_length + 1)
        if len(reply) != expected_reply_length + 1:
            raise IOError(f'{self!r}: Unexpected number of reply bytes. Got {len(reply)} bytes ({reply.hex(",")}), expected {expected_reply_length + 1} bytes')
        return reply[1:]

    def close(self):
        # self.tcpipe.close()
        pass