# Standard Python packages
import logging
import socket
import re
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
    }
    RPC_PERF_LOG_INTERVAL = 500

    #: TCPipe processor-side register index for UDP data port (see :meth:`set_local_data_port_number`).
    _UDP_DATA_PORT_REG = 2
    #: Deprecated alias for `_UDP_DATA_PORT_REG` (prefer :meth:`set_local_data_port_number`).
    CORE_REG_UDP_DATA_PORT = 2

    opened_sockets = {}

    def __init__(self, hostname, port=7, timeout=2):
        self.log = logging.getLogger(__name__)
        self.hostname = hostname
        self.port = port
        self.sock = self.opened_sockets.pop((hostname, port), None)
        if self.sock:
            self.sock.close()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect((hostname, self.port))
        self.opened_sockets[(hostname, port)] = self.sock
        self.tx_buf = bytearray(2048)
        self.tx_view = memoryview(self.tx_buf)
        self.rx_buf = bytearray(2048)
        self.rx_view = memoryview(self.rx_buf)
        self.firmware_crc = None
        self.log.debug(f'{self!r}: Opened TCPipe socket from local address{self.sock.getsockname()} to remote address {self.hostname}:{self.port}')
        self.bsb_sent_ctr = 0
        self.perf = _TCPipePerf(self, self.RPC_PERF_LOG_INTERVAL)
        # BSB write aggregation uses the batch-write RPC provided by the RTOS.
        self._bsb_aggregate_ctx_depth = 0
        self._bsb_agg_pending = []
        self._bsb_agg_pending_tx = 0
        self._bsb_aggregate_max_cmds = 512
        self._bsb_aggregate_max_tx_bytes = 64 * 1024

        self.check_tcpipe_version()

    def __repr__(self):
        return f'TCPipe({self.hostname}:{self.port})'

    def close(self):
        try:
            self._bsb_batch_write_flush()
        except (IOError, OSError) as exc:
            self.log.warning('%r: _bsb_batch_write_flush during close failed: %r', self, exc)
        self.perf.log_summary()
        if self.sock is not None:
            print(f'Closing TCPipe socket at {self.sock.getsockname()}')
            self.sock.close()
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
        """
        if self._bsb_agg_pending:
            self._bsb_batch_write_flush()
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
            tx_payload = self.tx_view[:tx_len]
        else:
            tx_payload = bytearray(tx_len)
            tx_payload[0] = self.RPC_PREFIX
            tx_payload[1] = rpc_id
            tx_payload[2] = len(payload) & 0xFF
            tx_payload[3] = len(payload) >> 8
            tx_payload[4:tx_len] = payload

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
            else:
                rx_len = self.sock.recv_into(self.rx_buf)
                recv_done_time = time.perf_counter()
                reply = self.rx_buf[:rx_len]
        finally:
            if timeout:
                self.sock.settimeout(old_timeout)
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

    def bsb_write_read(self, data):
        """ Writes `data` to the FPGA firmware Byte-serial bus and return reply.


        Parameters:

            data (bytes): data to write prior to the read operation


        """
        if self._bsb_agg_pending:
            self._bsb_batch_write_flush()
        reply = self._rpc_write_read(self.RPC_BSB_WRITE_READ, data)
        self.bsb_sent_ctr += 1
        return reply

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
        reply = self._rpc_write_read(self.RPC_BSB_BATCH_WRITE, payload)
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


    def get_rpc_functions(self):
        """ Fetches the list of functions provided by the platform processor.


        Parameters: None


        """
        b = self._rpc_write_read(self.RPC_GET_RPC_FUNCTIONS, read_until=b'\x00\x00\x00')
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
        self.log.info('%s: BSB aggregate flushes will use bsb_batch_write RPC', self)


    def _bsb_aggregate_active(self):
        return self._bsb_aggregate_ctx_depth > 0


    @contextmanager
    def aggregate_commands(self):
        """Coalesce write-only BSB MMI transactions until exit, then flush."""
        self._bsb_aggregate_ctx_depth += 1
        try:
            yield self
        finally:
            self._bsb_batch_write_flush()
            self._bsb_aggregate_ctx_depth -= 1
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
        reply = self._rpc_write_read(self.RPC_IIC_WRITE_READ, payload)
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
            timeout=timeout)
        if reply[0]:
            raise IOError(f'{self!r}: SPI Reply has error code {reply[0]}')
        if len(reply) != 1 + len(data) + read_length:
            raise IOError(f'{self!r}: SPI Received {len(reply)} bytes instead of {1 + len(data) + read_length} bytes')

        return reply[len(reply) - read_length: len(reply)]

    #########################################################
    #  FPGA bitstream programming
    #########################################################

    def set_fpga_bitstream(self, data, crc=0, timeout=40):
        """ Programs the FPGA with the provided bitstream.

        Parameters:

            data (bytes): bitstream, without header, uncompressed. Sould be 34437356 bytes long for the ZU28.
        """

        print(f'TCPipe: Programming FPGA with {len(data)} bytes')

        assert len(data) == 34437356, "ZU27/28/47/48 bitstream should be 34437356 bytes long"

        old_timeout = self.sock.gettimeout()
        try:
            if timeout:
                self.sock.settimeout(timeout)  # Set a longer timeout since we are sending a lot of data
            self.sock.sendall(data)  # sendall will block if there s back pressure on the socket
            self.firmware_crc = crc
        except:
            self.firmware_crc = None
            raise
        finally:
            self.sock.settimeout(old_timeout)
        return

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
    #  ADC synchronization
    #########################################################

    def adc_sync(self):
        """ Synchronizes the ADCs.

        Parameters: None

        """
        return self._rpc_write_read(self.RPC_ADC_SYNC)


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

        addr |= 0x80 if self.current_port else 0

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

            bus_name (str, int, or list of str or int): I2C bus(es) to activate

        """
        if bus_name:
            self.select_bus(bus_name, retry=3)
        try:
            self.write_read(addr, data=[], read_length=1, retry=0)  # dummy I2C acces
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
        reply = self.tcpipe.bsb_write_read(cmd)
        if len(reply) != expected_reply_length + 1:
            raise IOError(f'{self!r}: Unexpected number of reply bytes. Got {len(reply)} bytes ({reply.hex(",")}), expected {expected_reply_length + 1} bytes')
        return reply[1:]

    def close(self):
        # self.tcpipe.close()
        pass