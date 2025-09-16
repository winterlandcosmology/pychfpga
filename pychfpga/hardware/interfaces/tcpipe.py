# Standard Python packages
import logging
import socket
import re
import textwrap


from .bsb_mmi import BSB_MMI

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

    def __repr__(self):
        return f'TCPipe({self.hostname}:{self.port})'

    def close(self):
        print(f'Closing TCPipe socket at {self.sock.getsockname()}')
        self.sock.close()
        self.sock = None

    def get_rpc_functions(self):
        """ Fetches the list of functions provided by the platform processor.


        Parameters: None


        """
        tx_len = 4 # RPC header, I2C address, read length,  excluding data
        self.tx_view[0] = self.RPC_PREFIX
        self.tx_view[1] = 1
        self.tx_view[2] = 0   # I2C addr, read_length & data
        self.tx_view[3] = 0

        # print(f'Sending {bytes(self.tx_view[:tx_len_data]).hex(":")}')
        self.sock.sendall(self.tx_view[:tx_len])

        b = bytearray()
        funcs = {}
        # read TCP stream until we encounter 3 consecutive nulls indicting the end of the list
        while True:
            b += self.sock.recv(1024)
            if b.endswith(b'\x00\x00\x00'):
                break
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


    def i2c_read(self, addr, read_length, no_error=False):
        return self.i2c_write_read(addr, data=b'', read_length=read_length, no_error=no_error)


    def i2c_polled_read(self, addr, read_length, no_error=False):
        tx_len = 6  # prefix, cmd, len_lsb, len_msb, addr, read_length
        self.tx_view[:tx_len] = bytes((self.RPC_PREFIX, self.RPC_IIC_READ, 2, 0, addr, read_length))
        self.sock.send(self.tx_view[:tx_len])
        rx_len = self.sock.recv_into(self.rx_buf)
        if self.rx_buf[0]:
            if no_error:
                return b''
            else:
                raise IOError(f'{self!r}: I2C Reply has {rx_len} bytes but has error code {self.rx_buf[0]}')
        if rx_len < 1+read_length:
            raise IOError(f'{self!r}: I2C Did not receive enough bytes {rx_len} instead of {1+read_length}')
        return self.rx_buf[1:read_length + 1]


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
        tx_len = 6 # RPC header, I2C address, read length,  excluding data
        tx_len_data = tx_len + len(data)
        rpc_len = 2 + len(data)
        self.tx_view[0] = self.RPC_PREFIX
        self.tx_view[1] = self.RPC_IIC_WRITE_READ
        self.tx_view[2] = rpc_len & 0xFF  # I2C addr, read_length & data
        self.tx_view[3] = rpc_len >> 8
        self.tx_view[4] = addr # 7-bit I2C address; bit 7 is the port number.
        self.tx_view[5] = read_length # number of bytes to read after the write

        self.tx_view[tx_len:tx_len_data] = data

        # print(f'Sending {bytes(self.tx_view[:tx_len_data]).hex(":")}')
        self.sock.sendall(self.tx_view[:tx_len_data])
        rx_len = self.sock.recv_into(self.rx_buf)
        if self.rx_buf[0]:
            if no_error:
                return b''
            else:
                raise IOError(f'{self!r}: TCPipe I2C: Reply has error code {self.rx_buf[0]} ({", ".join(e for v,e in self.I2C_ERROR_CODES.items() if self.rx_buf[0] & v)})')
        if rx_len != 1 + read_length:
            raise IOError(f'{self!r}: TCPipe I2C: Receive {rx_len} bytes instead of {1+read_length} bytes (including status byte)')
        return self.rx_buf[1:read_length + 1]

    def i2c_write(self, addr, data):
        """ Writes `data` to I2C address `addr`.

        Used for accessing devices such as EEPROMs that require a command or addres be sent in the same transaction before reading from the device.

        Parameters:

            addr (int): bits 6:0 is the I2C address. Bit 7 indiates whether we access IIC bus 0 or 1.

            data (bytes): data to write prior to the read operation


        """
        self.i2c_write_read(addr, data, read_length=0)

    def bsb_write_read(self, data):
        """ Writes `data` to the FPGA firmware Byte-serial bus and return reply.


        Parameters:

            data (bytes): data to write prior to the read operation


        """
        tx_len = 4 # excluding data
        tx_len_data = tx_len + len(data)
        # self.tx_view[:tx_len] = bytes((self.RPC_PREFIX, self.RPC_BSB_WRITE_READ, len(data) & 0xFF, len(data) >> 8))
        self.tx_view[0] = self.RPC_PREFIX
        self.tx_view[1] = self.RPC_BSB_WRITE_READ
        self.tx_view[2] = len(data) & 0xFF # lsb of data length
        self.tx_view[3] = len(data) >> 8 # msb of dat alength
        self.tx_view[tx_len:tx_len_data] = data
        self.sock.sendall(self.tx_view[:tx_len_data])
        self.bsb_sent_ctr += 1
        # print(self.bsb_sent_ctr)
        rx_len = self.sock.recv_into(self.rx_buf)
        return self.rx_buf[:rx_len]

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
        tx_len = 6
        self.tx_view[0] = self.RPC_PREFIX
        self.tx_view[1] = self.RPC_SPI_WRITE_READ
        self.tx_view[2] = 2 + len(data) + read_length  # SPI port, read_length & data length
        self.tx_view[3] = 0 # data length assumed to be < 256-2
        self.tx_view[4] = spi_device # SPI port
        self.tx_view[5] = read_length

        self.tx_view[tx_len: tx_len + len(data)] = data
        self.tx_view[tx_len + len(data): tx_len + len(data) + read_length] = b'\x00' * read_length

        # print(f'TCPIPE SPI: Sending {self.tx_buf[:tx_len + len(data) + read_length]}')
        self.sock.sendall(self.tx_view[:tx_len + len(data) + read_length])
        if timeout:
            old_timeout = self.sock.gettimeout()
            self.sock.settimeout(timeout)
        try:
            rx_len = self.sock.recv_into(self.rx_buf)
        finally:
            if timeout:
                self.sock.settimeout(old_timeout)
        if self.rx_buf[0]:
            raise IOError(f'{self!r}: SPI Reply has error code {self.rx_buf[0]}')
        if rx_len != 1 + len(data) + read_length:
            raise IOError(f'{self!r}: SPI Received {rx_len} bytes instead of {1 + len(data) + read_length} bytes')
        # print(f'received {self.rx_buf[:rx_len]}, returning {self.rx_buf[rx_len-read_length:rx_len]}')

        return self.rx_buf[rx_len - read_length: rx_len]

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

    def core_reg_read(self, reg):
        tx_len = 5
        self.tx_view[0] = self.RPC_PREFIX
        self.tx_view[1] = self.RPC_CORE_REG_READ
        self.tx_view[2] = 1
        self.tx_view[3] = 0
        self.tx_view[4] = reg # register number

        # print(f'Sending {self.tx_buf[:tx_len + len(data) + read_length]}')
        self.sock.sendall(self.tx_view[:tx_len])
        rx_len = self.sock.recv_into(self.rx_buf)
        if self.rx_buf[0]:
            raise IOError(f'core_reg_read reply has error code {self.rx_buf[0]}')
        if rx_len != 5:
            raise IOError(f'core_reg_read received {rx_len} bytes instead of {5} bytes')
        # print(f'received {self.rx_buf[:rx_len]}, returning {self.rx_buf[rx_len-read_length:rx_len]}')

        return int.from_bytes(self.rx_buf[1:5], 'little')

    def core_reg_write(self, reg, value):
        tx_len = 4+1+4
        self.tx_view[0] = self.RPC_PREFIX
        self.tx_view[1] = self.RPC_CORE_REG_WRITE
        self.tx_view[2] = 5 # register + value word
        self.tx_view[3] = 0
        self.tx_view[4] = reg # register number
        self.tx_view[5: 9] = value.to_bytes(4, 'little');

        # print(f'Sending {self.tx_buf[:tx_len + len(data) + read_length]}')
        self.sock.sendall(self.tx_view[:tx_len])
        rx_len = self.sock.recv_into(self.rx_buf)
        if self.rx_buf[0]:
            raise IOError(f'core_reg_read reply has error code {self.rx_buf[0]}')
        if rx_len != 1:
            raise IOError(f'core_reg_read received {rx_len} bytes instead of {1} byte')
        # print(f'received {self.rx_buf[:rx_len]}, returning {self.rx_buf[rx_len-read_length:rx_len]}')

    def adc_sync(self):
        """ Synchronizes the ADCs.

        Parameters: None

        """
        tx_len = 4 # RPC header, I2C address, read length,  excluding data
        self.tx_view[0] = self.RPC_PREFIX
        self.tx_view[1] = self.RPC_ADC_SYNC
        self.tx_view[2] = 0   # I2C addr, read_length & data
        self.tx_view[3] = 0

        # print(f'Sending {bytes(self.tx_view[:tx_len_data]).hex(":")}')
        self.sock.sendall(self.tx_view[:tx_len])

        b = self.sock.recv(1024)
        return b

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
        reply = self.tcpipe.bsb_write_read(cmd)
        if len(reply) != expected_reply_length + 1:
            raise IOError(f'{self!r}: Unexpected number of reply bytes. Got {len(reply)} bytes ({reply.hex(",")}), expected {expected_reply_length + 1} bytes')
        return reply[1:]

    def close(self):
        # self.tcpipe.close()
        pass