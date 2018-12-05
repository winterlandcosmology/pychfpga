"""
fpga_mmi.py module

Provides access to the memory-mapped interface of the FPGA through UDP
commands sent directly to the FPGA Ethernet port.

 History:
        2014-03-04 JFC: Created
"""
import logging
import numpy as np
import udp as udp
from ..iceboard_ext import IceBoardExtHandler


class FpgaMmiException(IOError):
    pass


class TimeoutException(IOError):
    pass


class FpgaMmi:
    """
    Base class that defines the memory-mapped interface to the FPGA
    through a direct Ethernet link to the FPGA.

    This is used by Python code that handles the FPGA firmware directly by
    toggling reading and writing to memopry-mapped registers.

    Notes:
       - 140223 JFC: Maybe add methods to allow packing multiple commands in a
         single packet. By default, the command queue is flushed at every
         write command.
    """
    BROADCAST_IP_ADDR = udp.Udp.BROADCAST
    PROTO_UDP = 'UDP'
    PROTO_TCP = 'TCP'
    TimeoutException = TimeoutException

    _BROADCAST_BASE_PORT = IceBoardExtHandler._BROADCAST_BASE_PORT
    _FPGA_IP_SETUP_BASE_ADDR = IceBoardExtHandler._FPGA_IP_SETUP_BASE_ADDR
    _FPGA_SERIAL_NUMBER_ADDR = IceBoardExtHandler._FPGA_SERIAL_NUMBER_ADDR
    _FPGA_TIMESTAMP_ADDR = IceBoardExtHandler._FPGA_TIMESTAMP_ADDR

    # Match those with what is used by Module
    _CONTROL_BASE_ADDR = IceBoardExtHandler._CONTROL_BASE_ADDR
    _STATUS_BASE_ADDR  = IceBoardExtHandler._STATUS_BASE_ADDR
    _RAM_BASE_ADDR     = IceBoardExtHandler._RAM_BASE_ADDR

    OPCODE_WRITE_CONTROL      = 0b100
    OPCODE_WRITE_CONTROL_MASK = 0b101
    OPCODE_NOP                = 0b110
    OPCODE_WRITE_RAM          = 0b111
    OPCODE_READ_CONTROL       = 0b000
    OPCODE_READ_STATUS        = 0b010
    OPCODE_READ_RAM           = 0b011

    def __init__(self,
                 fpga_ip_addr,
                 fpga_port_number,
                 interface_ip_addr=None,
                 local_port_number=0,
                 fpga_serial_number=None,
                 set_fpga_networking_parameters=False,
                 udp_retries=10,
                 timeout=0.5,
                 parent=None):
        """
         'fpga_serial_number' is needed only if we set the FPGA networking using UDP broadcasts (set_fpga_networking_parameters is True)
        """
        self.logger = logging.getLogger(__name__)
        self.fpga_ip_addr = fpga_ip_addr
        self.fpga_port_number = fpga_port_number  # Command listening port on the FPGA
        self.local_port_number = local_port_number  # Command replies destination port (0= selected randomly by OS)
        self.address = (self.fpga_ip_addr, self.local_port_number)
        self.fpga_serial_number = fpga_serial_number  # used to select specific FPGAs during broadcasts
        self.set_fpga_networking_parameters = set_fpga_networking_parameters
        self.timeout = timeout
        self.udp_retries = udp_retries
        self.parent = parent
        self.udp = None
        self.interface_ip_addr = interface_ip_addr
        self.send_counter = 0
        self.recv_counter = 0
        self.error_counter = 0

    def __enter__(self):
            self.open()
            return self

    def __exit__(self, etype, einst, etraceback):
            self.close()

    def __repr__(self):
        # return '%r.%s(FPGA %s:%s, local %s:%s)' % (self.parent, self.__class__.__name__, self.fpga_ip_addr, self.fpga_port_number, self.interface_ip_addr, self.local_port_number)
        return '%r.%s(%s)' % (self.parent, self.__class__.__name__, self.fpga_ip_addr)

    def open(self):
        """
        Open control communication socket to FPGA
        """

        # Set the FPGA communication networking parameters
        if self.set_fpga_networking_parameters and self.fpga_serial_number:
            self.close()  # make sure the current socket is closed
            self._set_fpga_networking_parameters()

        self.udp = udp.Udp(
            remote_ip_addr=self.fpga_ip_addr,
            remote_port_number=self.fpga_port_number, # None or 0: use local port number
            local_port_number=self.local_port_number, # 0 : use random port assigned by OS
            if_ip_addr=self.interface_ip_addr)

        self.udp.set_timeout(self.timeout)
        self.local_port_number = self.udp.local_port_number
        self.fpga_port_number = self.udp.remote_port_number
        # self.logger.info('   Opened control socket on %s:%i through interface %s' % (self.fpga_ip_addr, self.local_port_number, self.interface_ip_addr))

    def close(self):
        """Closes the socket"""
        if self.udp:
            self.udp.close()
        # self.logger.info('Closed control socket')

    def _set_fpga_networking_parameters(
            self, number_of_trials=3, check=True):
        """
        Send UDP broadcasts to set the FPGA firmware in the specified ICEboard to use the specified
        ip address and port. This requires that the FPGA serial number (fpga_serial_number) is known.

        This is not needed if the FPGA networking parameters are set through the ARM SPI link to the FPGA.

        An exception will be raised if the board cannot be found on the
        network of if another board uses the same ip address.

        NOTE:
            - This function is supported only for direct Ethernet connections
              to the FPGA
            - This function cannot be called if the UDP link is already
              established.
            - The broadcast is 'send only': the FPGAs are not asked to reply to
              the broadcast. Consequently, the call will not affect the return
              addresses of these FPGAs.

        """
        import socket  # used for inet_aton()
        import struct

        ip_addr = self.fpga_ip_addr
        port_number = self.local_port_number
        serial_number = self.fpga_serial_number
        broadcast_group = 0
        # interface_ip_addr = self.interface_ip_addr

        logger = logging.getLogger(__name__)
        logger.debug(
            '%r: Broadcasting on port %i to configure FPGA S/N %016X '
            'with address %s:%i' %
            (self, self._BROADCAST_BASE_PORT, serial_number, ip_addr, port_number))

        # Build the array of bytes to fill the network configuration register
        # block
        ip_setup_string = struct.pack(
            '>H4s4sHQ', 0x1234, socket.inet_aton(ip_addr),
            socket.inet_aton(ip_addr), port_number, serial_number)
        trig1 = chr(0x0C | broadcast_group)
        trig2 = chr(0x8C | broadcast_group)

        # Configure the FPGA through a UDP broadcast packet containing the
        # target FPGA serial number
        trial = 0
        while trial < number_of_trials:
            with FpgaMmi(FpgaMmi.BROADCAST_IP_ADDR, FpgaMmi._BROADCAST_BASE_PORT) as mmi:
                mmi.write(self._FPGA_IP_SETUP_BASE_ADDR, ip_setup_string + trig1) # Send string with trigger flag cleared
                mmi.write(self._FPGA_IP_SETUP_BASE_ADDR, ip_setup_string + trig2) # resend with trigger flag set. The 0-to-1 transition will load the desired networking parameters
                mmi.write(self._FPGA_IP_SETUP_BASE_ADDR, [0] * len(ip_setup_string + trig2)) # Write zeros everywhere to make sure we stop latching data
            # logger.debug('FPGA S/N %016X is configured with address %s:%i' % (serial_number, ip_addr, port_number))
            if not check:
                return
            (serial, timestamp) = self.get_fpga_config(ip_addr=ip_addr, port_number=port_number)
            if serial and serial == serial_number:
                return
            else:
                logger.debug('%r: Networking configuration of FPGA S/N %016X with address %s:%i failed.' % (self, serial_number, ip_addr, port_number))
                trial +=1
        logger.debug('%r: Unable to configure FPGA S/N %016X with address %s:%i' % (self, serial_number, ip_addr, port_number))
        raise FpgaMmiException('%r: Unable to configure FPGA S/N %016X with address %s:%i' % (self, serial_number, ip_addr, port_number))

    def get_fpga_config(self, ip_addr, port_number,
                        timeout=0.1, number_of_trials=3):
        """
        Returns basic information allowing to check if we talk to the right
        FPGA with the right firmware.

        Will not cause an exception if the FPGA fails to respond at the
        specified address. Instead, all fields will be None.
        """
        trial = 0
        with FpgaMmi(self.fpga_ip_addr, self.local_port_number) as mmi:
            while trial < number_of_trials:
                try:
                    serial = mmi.read(self._FPGA_SERIAL_NUMBER_ADDR, type=np.dtype('>u8'), timeout=timeout, retry=0)
                    timestamp = mmi.read(self._FPGA_TIMESTAMP_ADDR, type=np.dtype('>u4'), timeout=timeout, retry=0)
                    return (serial, timestamp)
                except mmi.TimeoutException:
                    trial += 1
        return (None, None)

    def flush(self, timeout=0.05):
        """Flushes the socket receive buffer."""
        old_timeout = self.udp.get_timeout()
        self.udp.set_timeout(timeout)
        try:
            while True:
                data = self.udp.recv()
                if len(data) == 0:
                    break
        except self.udp.TimeoutException:
            pass  # do nothing
            # print('Buffer is empty')
        self.udp.set_timeout(old_timeout)

    def set_timeout(self, timeout):
        """
        Sets the socket timeout value in seconds.
        """
        self.udp.set_timeout(timeout)

    def get_timeout(self):
        """
        Returns the current socket timeout value in seconds.
        """
        return self.udp.get_timeout()

    def _send_command(self, cmd, expected_reply_length, retry=None, resync=False, timeout_increase_factor=1):
        """ Send a read or write command to the FPGA and check the reply for the correct
        sequence number and packet length. If unsuccessful, the command will
        be resent ``retry`` times.

        This method is used by the read() and write() methods.

        Parameters:

            cmd (str): command bytes to send

            expected_reply_length (int): Number of bytes we expect in the reply, excluding the
                1-byte header. This is used to validate the reply and retry if necessary.

            retry (int): Number of times the command can be retried before raising an exception

            resync (bool): if `resync` is True, the sequence number will be resynchronized to the
                incoming reply and will not raise an exception.

            timeout_increase_factor (float): factor by which the timeout is increased each time there is a timeout error.

        Returns:

            str: The content of the reply packet without the header.

        Exceptions:

            IOError: Raised if a valid reply cannot be obtained after the retries.

        """
        old_timeout = self.get_timeout()
        if retry is None:
            retry = self.udp_retries
        trial = 1
        while True:
            try:
                error = ''
                self.udp.send(cmd)
                self.send_counter += 1
                data = self.udp.recv()
                self.recv_counter += 1
                # self.logger.warning('read command: Got 0x%02x, expected 0x%02x' % (ord(data[0]), self.send_counter & 0xff))
                if ord(data[0]) != self.send_counter & 0xff:
                    if not resync:
                        error = '%r: Invalid sequence number from a read command. Got 0x%02x, expected 0x%02x.' % (self, ord(data[0]), self.send_counter & 0xff)
                    self.send_counter = ord(data[0])
                elif len(data) != expected_reply_length + 1:
                        error = "%r: FPGA Read command to returned %i bytes (0x%s). %i were expected." % (
                            self,
                            len(data),
                            ' '.join('%02X' % ord(b) for b in data),
                            expected_reply_length + 1)

            except self.udp.TimeoutException:
                self.set_timeout(self.get_timeout() * timeout_increase_factor)
                error = '%r: Timeout during FPGA command.' % self

            if retry < 0:
                return ''

            if not error:
                break

            self.error_counter += 1

            if trial >= retry:
                self.logger.error('%r: %s Raising exception after %i unsuccessful trials' % (self, error, trial))
                raise IOError('%r: %s' % (self, error))
            else:
                self.logger.warning('%r: %s This is trial %i/%i. Trying again' % (self, error, trial, retry))
            trial += 1
        # We now have our data for this chunk
        self.set_timeout(old_timeout)
        return data[1:]

    def read(self, addr, type=np.dtype('>u1'), length=1,
             timeout=None, retry=None, resync=False):
        """
        Reads memory-mapped byte(s) from the FPGA through the Ethernet
        interface.

        'length' values of type 'type' are read. The Reads will be done in the
        minimum number of requests in order to read all bytes.

        ``resync``: If True, the receiver will ignore command sequence number
        mismatches and will resynchronize the local counter with the value
        that was received. This is normally done only once when the system is
        initialized.

        Returns a numpy array of uint8 where the bytes are intrepreted as a series of
        'length' elements of type 'type'.

        2014-02-06 JFC: Now reads multiple bytes at a time to improve
        efficiency by using the length field in the command word.
        """

        if self.udp.is_broadcast():
            raise Exception(
                'standard read cannot be used in broadcast mode as there '
                'might be many returned values. Use broadcast_read() instead.')

        itemsize = np.dtype(type).itemsize  # number of bytes contained in the destinaion vector type
        byte_length = length * itemsize  # total number of bytes to read
        dout = np.zeros(byte_length, np.int8)  # initialize result vector as a byte array
        offset = 0
        # Loop to read all required bytes (the FPGA does not support multi-byte reads (yet))

        if timeout:
            self.set_timeout(timeout)

        if addr & self._RAM_BASE_ADDR:
            opcode = self.OPCODE_READ_RAM
        elif addr & self._STATUS_BASE_ADDR:
            opcode = self.OPCODE_READ_STATUS
        else:
            opcode = self.OPCODE_READ_CONTROL

        while offset < byte_length:
            log2_length = min((byte_length - offset).bit_length() - 1, 3)  # compute the log2 of the number of bytes to read, limited to 3 (i.e. 8 bytes)
            read_length = 1 << log2_length  # number of bytes to read in this iteration
            s = (chr((opcode << 5) | (log2_length << 3) +
                     ((addr >> 16) & 0x07)) +
                 chr((addr >> 8) & 0xFF) +
                 chr(addr & 0xFF))

            data = self._send_command(s, read_length, retry, resync)
            if retry is not None and retry < 0:
                self.logger.warning('%r: FPGA_MMI retry = %i' % (self, retry))
                return
            dout[offset: offset + read_length] = np.fromstring(data, dtype=np.uint8)  # store received byte
            addr += read_length
            offset += read_length

        dout.dtype = np.dtype(type) # change interpretation of the byte array into a 'type' array

        #if we requested a single value (length=1), returns the object, otherwise return a numpy array of objects
        if len(dout) == 1:
            return dout[0]
        else:
            return dout

    def broadcast_read(self, addr, type=np.dtype('>u8'), timeout=.5):
        """
        Reads memory-mapped object from multiple FPGAs through a broadcast
        request.

        Returns a array of type 'type' containing the values that were
        returned by all FPGAs.

        This command can read only a single object that is 1,2,4 or 8 bytes
        wide.
        """

        byte_length = np.dtype(type).itemsize  # number of bytes contained in the destinaion vector type
        log2_length = byte_length.bit_length()-1  # compute the log2 of the number of bytes to read, limited to 3 (i.e. 8 bytes)
        read_length = 1 << log2_length  # number of bytes to read in this iteration
        # dout = np.zeros(byte_length, np.int8) # initialize result vector as a byte array
        dout = []
        # Loop to read all required bytes (the FPGA does not support multi-byte reads (yet))

        if addr & self._RAM_BASE_ADDR:
            opcode = self.OPCODE_READ_RAM
        elif addr & self._STATUS_BASE_ADDR:
            opcode = self.OPCODE_READ_STATUS
        else:
            opcode = self.OPCODE_READ_CONTROL

        s = (chr((opcode << 5) | (log2_length << 3) + ((addr >> 16) & 0x07)) +
             chr((addr >> 8) & 0xFF) +
             chr(addr & 0xFF))

        self.udp.send(s)
        self.send_counter += 1
        self.recv_counter += 1  # We expect only one packet back
        self.udp.set_timeout(timeout)
        while True:
            try:
                data = self.udp.recv()
                if data == s:  # ignore the command packet that was broadcasted back to us
                    continue
            except self.udp.TimeoutException:
                break

            if len(data) != read_length + 1:
                raise FpgaMmiException(
                    "%r: FPGA Read command returned %i bytes. %i were expected." %
                    (self, len(data), read_length + 1))

            dout.append(np.fromstring(data[1:], dtype=type)[0])  # store received byte
        return dout

    def _to_string(self, data):
        if isinstance(data, str):
            return data
        elif isinstance(data, (list, np.ndarray)):
            return ''.join([chr(c) for c in data])
        if isinstance(data, int):
            return chr(data)
        elif isinstance(data, np.uint32):
            return np.array(data, '>u4').tostring()  # store as big endian (most significant byte first)
        elif isinstance(data, np.uint16):
            return np.array(data, '>u2').tostring()  # store as big endian (most significant byte first)
        elif isinstance(data, np.uint8):
            return np.array(data, '>u1').tostring()  # store as big endian (most significant byte first)
        else:
            return chr(data)

    def write(self, addr, data, mask=None, retry=None, resync=False):
        """
        Writes byte(s) to memory-mapped registers in the FPGA through the
        Ethernet interface.

        'data' can be:
            - String
            - list of integers between 0 and 255
            - numpy array of integers between 0 and 255
            - 4 bytes in a numpy uint32. MSB is transmitted first
            - 2 bytes in a numpy uint16. MSB is transmitted first
            - 1 byte in a numpy uint8.
        """


        if addr & self._RAM_BASE_ADDR:
            opcode = self.OPCODE_WRITE_RAM
        elif addr & self._STATUS_BASE_ADDR:
            raise FpgaMmiException(
                'FpgaMmi: Attempt to write to a STATUS register')
        elif mask is None:
            opcode = self.OPCODE_WRITE_CONTROL
        else:
            opcode = self.OPCODE_WRITE_CONTROL_MASK

        command_string = (
            chr((opcode << 5) | ((addr >> 16) & 0x07)) +
            chr((addr >> 8) & 0xFF) +
            chr(addr & 0xFF))

        data_string = self._to_string(data)
        length = len(data_string)

        # If there is a mask, interleave the data with the masks
        if mask is not None:
            mask_string = self._to_string(mask)
            data_string = ''.join(
                [d+m for (d, m) in zip(data_string, mask_string)])
        self._send_command(command_string + data_string, 0, retry, resync)
        return length


def discover_fpgas(interface_ip_addr=None, source_subarrays=[0], timeout=0.1):
    """
    Get the serial numbers of all FPGA directly connected on the network (i.e.
    not accessed through the ARM processor)

    NOTE:
        - This function should not be called when the MMI interface is opened.
        - This function is supported only for direct Ethernet connections to
          the FPGA
        - /!\ Calling this function will disrupt operations of all FPGAs in
          the network as reading from them cause them to redirect their
          outputs to this machine on the broadcast port.
    """
    logger = logging.getLogger(__name__)

    if isinstance(source_subarrays, int):
        source_subarrays = [source_subarrays]

    serial_list = []
    #  for if_addr in interface_ip:
    for subarray in source_subarrays:
        logger.debug(
            'Searching ICEBoards on subarray %i through interface %s' %
            (subarray, interface_ip_addr))

        with FpgaMmi(
                FpgaMmi.BROADCAST_IP_ADDR,
                FpgaMmi._BROADCAST_BASE_PORT + subarray,
                interface_ip_addr=interface_ip_addr) as mmi:
            mmi.flush()
            serials = mmi.broadcast_read(
                FpgaMmi._FPGA_SERIAL_NUMBER_ADDR,
                type=np.dtype('>u8'),
                timeout=timeout)
        serial_list += serials

    return serial_list
