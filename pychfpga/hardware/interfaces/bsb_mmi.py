"""
Generate read/write command to access the FPGA's memory-mapped registers through its byte-serial bus (BSB) protocol.
Is meant to be be inherited by an interface class that provides a _send_command() method that does the actual data transfer.
"""

import numpy as np
from contextlib import contextmanager

class FpgaMmiException(IOError):
    pass


class BSB_MMI:
    """
    Base class that defines the memory-mapped interface to the FPGA.

    Read/write commands are transmitted as a sequence of bytes, where the
    first 3 bytes contain the 3-bit command type, 2-bit read length and 19-bit
    target address. The command is then followed by the data to be written, if
    applicable.

    Read/writes operations can be performed in 3 different targets that share the same address space:
        Control registers (read/write)
        Status registers (read only)
        RAM or DRP (read/write)

    The read/write methods determine the target and select the appropriate operation code based on the upper bits of the address.

    This class shall be inherited by a class tha tprovides the _send_command() method to send/receive the packets created and decoded by this class.
    """

    # Define address ranges of various targets. That will be used to map to the proper command.
    _CONTROL_BASE_ADDR = 0x000000  # Read/write control registers
    _STATUS_BASE_ADDR = 0x080000  # read-only status registers
    _RAM_BASE_ADDR = 0x100000  # RAM or DRP access (depends on firmware implementation)

    # MMI operations codes
    OPCODE_READ_CONTROL       = 0b000
    OPCODE_READ_NOP           = 0b001
    OPCODE_READ_STATUS        = 0b010
    OPCODE_READ_RAM           = 0b011
    OPCODE_WRITE_CONTROL      = 0b100
    OPCODE_WRITE_CONTROL_MASK = 0b101
    OPCODE_WRITE_NOP          = 0b110
    OPCODE_WRITE_RAM          = 0b111

    # Maximum packet lengths, limited by the size of the FIFOs
    # This has to be defined by the subclasses
    MAX_BSB_COMMAND_PACKET_LENGTH = 0
    MAX_BSB_REPLY_PACKET_LENGTH = 0

    # List command opcodes indexed by addr[20:19]
    READ_OPCODES = (OPCODE_READ_CONTROL, OPCODE_READ_STATUS, OPCODE_READ_RAM, None)
    WRITE_OPCODES = (OPCODE_WRITE_CONTROL, None, OPCODE_WRITE_RAM, None)
    MASKED_WRITE_OPCODES = (OPCODE_WRITE_CONTROL_MASK, None, None, None)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # pre-allocated buffers
        self.tx_buf = memoryview(bytearray(512+3)); # use memoryview so we can use zero-copy slices
        self.rx_buf = memoryview(bytearray(256)); # use memoryview so we can use zero-copy slices
        self.rx_command = bytearray(3); # for read commands
    
        self.error_counter = 0

    @contextmanager
    def aggregate_commands(self):
        """Coalesce write-only BSB transactions until exit, then flush.

        Default is a no-op (each write is sent immediately). Transports that
        support batching, such as :class:`TCPipe_BSB_MMI`, override this.
        """
        yield self

    def _send_command(self, cmd, expected_reply_length, **kwargs):
        """ Send a read or write command to the FPGA and check the reply for the correct
        sequence number and packet length. If unsuccessful, the command will
        be resent ``retry`` times.

        This method is used by the read() and write() methods.

        Parameters:

            cmd (bytes): command bytes to send

            expected_reply_length (int): Number of bytes we expect in the reply, excluding the
                1-byte header. This is used to validate the reply and retry if necessary.

        Returns:

            bytes: The content of the reply packet without the header.

        Exceptions:

            IOError: Raised if a valid reply cannot be obtained after the retries.

        """
        raise NotImplementedError('Subclass must define the send method')

    def read(self, addr, type, length,
             timeout=None, retry=None, resync=True):
        """
        Reads memory-mapped byte(s) from the FPGA through the Ethernet
        interface.

        Parameters:


            addr (int): Address from which to read within the BSB address
                space. The control/status/RAM pages are determined from the high
                bits of the address.

            type (``int``, str or dtype): Selects the type of returned object

                - ``int`` object: a single integer of `length` bytes is read as a **big endian** and is returned.
                - ``bytes`` or ``bytearray`` object: A ``bytearray`` is returned.
                - dtype or string representing a dtype: Returns a numpy array of `length` numpy objects of dtype `type`.


            length (int): Indicates the number of bytes (if type==int) or the number of numpy elements of dtype==type to read.
                Multiple transactions will be performed if the requested number of bytes cannot be obtained in a single one.

            timeout (float): If not None, sets the timeout period for the read transaction.

            retry (int): Number times the read is retried. If None, the default is used.

            resync (bool): If True, the receiver will ignore command sequence number
                mismatches and will resynchronize the local counter with the value
                that was received. This is normally done only once when the system is
                initialized.

        Returns:

            - If `type` = ``int``, returns an integer. Otherwise, returns a numpy array.

        2014-02-06 JFC: Now reads multiple bytes at a time to improve
            efficiency by using the length field in the command word.
        """

        byte_length = abs(length) * (1 if type is int else np.dtype(type).itemsize) # total number of bytes to read
        # use preallocated destination buffer unless we need more bytes, in which case a new one is allocated
        rx_buf = self.rx_buf[:byte_length] if byte_length <= len(self.rx_buf) else memoryview(bytearray(byte_length))

        if timeout:
            self.set_timeout(timeout)

        # Determine the command opcode based on the page encoded in the upper bits of the address
        opcode = self.READ_OPCODES[addr >> 19]
        if opcode is None:
            raise RuntimeError(f'Invalid opcode for address page {addr >> 19}')
        # Loop to read all required bytes (the FPGA does not support multi-byte reads (yet))
        offset = 0
        while offset < byte_length:
            # compute the log2 of the number of bytes to read, limited to 3 (i.e. 8 bytes)
            log2_length = min((byte_length - offset).bit_length() - 1, 3)
            read_length = 1 << log2_length  # number of bytes to read in this iteration
            self.rx_command[0] = (opcode << 5) | (log2_length << 3) + ((addr >> 16) & 0x07)  # Byte 0: opcode, length, MSB of address
            self.rx_command[1] = (addr >> 8) & 0xFF  # Byte 1: address
            self.rx_command[2] = addr & 0xFF  # Byte 2: LSB of address

            # Check command length against platform capability
            # if len(command_bytes) > self.MAX_BSB_COMMAND_PACKET_LENGTH:
            #     raise RuntimeError('BSB read command packet is too large for this platform')
            rx_buf[offset: offset + read_length] = self._send_command(self.rx_command, read_length, retry, resync)
            if retry is not None and retry < 0:
                self.logger.warning(f'{self!r}: FPGA_MMI retry = {retry}')
                return
            if offset + read_length > byte_length:
                raise IOError(f'{self!r}: mmi.read(): Received too many bytes')
            # dout[offset: offset + read_length] = np.frombuffer(data, dtype=np.uint8)  # store received byte
            addr += read_length
            offset += read_length

        if type is int:
            return int.from_bytes(rx_buf, 'big', signed=length < 0)
        elif type is bytes:
            return bytes(rx_buf)
        elif type is bytearray:
            return bytearray(rx_buf) # make sure we make a copy of the array
        else:
            dout = np.frombuffer(rx_buf, dtype=np.dtype(type)).copy()  # make a copy so we don't just return a pointer to the buffer, which changes on every call
            return dout
            # # If we requested a single value (length=1), returns the object,
            # # otherwise return a numpy array of objects

            # if len(dout) == 1:
            #     # print(f'mmi read dout={dout} -> {dout[0]}')
            #     return dout[0]
            # else:
            #     # print(f'mmi read dout={dout} )')
            #     return dout


    def _to_bytes(self, data):
        """ Returns a byte-like view of `data`

        - A bytestring, bytearray or memoryview is returned as is
        - A numpy array or numpy integer is viewed as an array of bytes according to its intrinsic endianness.
        - A list or tuple is converted in a bytestring (each element representing a byte value)
        - An integer is interpreted as the value of a single byte
        - Any other type is passed to bytes() and returned
        """
        # print(f'to_bytes data = {data}')
        if isinstance(data, (bytes, bytearray, memoryview)):
            return data
        elif isinstance(data, np.ndarray):
            return data.view('u1')
        elif isinstance(data, (list, tuple)):
            return bytes(data)
        elif isinstance(data, np.integer):
            self.log.warning(f'{self!r: BSB_MMI: converting a numpy integer to a byte array with intristic byte order. We recommend you convert to bytes with an explicit byte order to avoid any incompatibilities.}')
            return data.tobytes()
        elif isinstance(data, int):
            return bytes((data,))
        else:
            return bytes(data)

    def write(self, addr, data, mask=None, retry=None, resync=True):
        """
        Writes byte(s) to memory-mapped registers in the FPGA.

        'data' can be:
            - String
            - list of integers between 0 and 255
            - numpy array of integers between 0 and 255
            - 4 bytes in a numpy uint32. MSB is transmitted first
            - 2 bytes in a numpy uint16. MSB is transmitted first
            - 1 byte in a numpy uint8.
        """


        opcode = self.WRITE_OPCODES[addr >> 19] if mask is None else self.MASKED_WRITE_OPCODES[addr >> 19]
        if opcode is None:
            raise RuntimeError(f'Invalid opcode for address page {addr >> 19}')

        data_bytes = self._to_bytes(data)
        length = len(data_bytes) if mask is None else 2 * len(data_bytes)

        # use preallocated destination buffer unless we need more bytes, in which case a new one is allocated
        tx_buf = self.tx_buf[:length + 3] if length <= len(self.tx_buf) else memoryview(bytearray(length + 3))

        tx_buf[0] = (opcode << 5) | ((addr >> 16) & 0x07)  # Byte 0: opcode, length, MSB of address
        tx_buf[1] = (addr >> 8) & 0xFF  # Byte 1: address
        tx_buf[2] = addr & 0xFF  # Byte 2: LSB of address


        # If there is a mask, interleave the data with the masks
        if mask is not None:
            tx_buf[3::2] = data_bytes
            tx_buf[4::2] = self._to_bytes(mask)
        else:
            tx_buf[3:] = data_bytes

        # Check command length against platform capability
        if len(tx_buf) > self.MAX_BSB_COMMAND_PACKET_LENGTH:
            raise RuntimeError('BSB write Command packet is too large for this platform')

        self._send_command(tx_buf, 0, retry, resync)
        return length

    def flush(self, timeout=0.05):
        """ Flushes the reply channel of any remaining data.
        This is typically  used to ensure future replies will be synchronized with their commands
        """
        pass

