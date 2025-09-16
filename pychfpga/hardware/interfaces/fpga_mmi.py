"""
fpga_mmi.py module

Provides access to the memory-mapped interface of the FPGA through UDP
commands sent directly to the FPGA Ethernet port.

 History:
        2014-03-04 JFC: Created
"""
import logging
import numpy as np

from .udp import Udp as UDP
from .bsb_mmi import BSB_MMI, FpgaMmiException
# pychfpga.fpga_firmware.chfpga.chFPGA:  imported at runtime to prevent circular imports (chFPGA imports fpga_mmi)

class TimeoutException(IOError):
    pass

class FPGAMmi(BSB_MMI):
    """
    Provides access to the FPGA's Byte-serial-bus BSB memory mapped registers of the FPGA over UDP Ethernet packets.


    Notes:
       - 140223 JFC: Maybe add methods to allow packing multiple commands in a
         single packet. By default, the command queue is flushed at every
         write command.
    """
    BROADCAST_IP_ADDR = UDP.BROADCAST
    PROTO_UDP = 'UDP'
    PROTO_TCP = 'TCP'
    TimeoutException = TimeoutException



    # Maximum BSB packet lengths, limited by the size of the FIFOs
    # These are approximale. Have to lookup the UDP buffer sizes.
    # We assume the networking allows Jumbo frames.
    MAX_BSB_COMMAND_PACKET_LENGTH = 2048
    MAX_BSB_REPLY_PACKET_LENGTH = 2048

    def __init__(
            self,
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
         'fpga_serial_number' is needed only if we set the FPGA networking
         using UDP broadcasts (set_fpga_networking_parameters is True)
        """
        super().__init__()



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
        # return '%r.%s(FPGA %s:%s, local %s:%s)' % (
        # self.parent, self.__class__.__name__, self.fpga_ip_addr,
        # self.fpga_port_number, self.interface_ip_addr, self.local_port_number)
        return '%r.%s(%s)' % (self.parent, self.__class__.__name__, self.fpga_ip_addr)

    def open(self):
        """
        Open control communication socket to FPGA
        """

        # Set the FPGA communication networking parameters
        if self.set_fpga_networking_parameters and self.fpga_serial_number:
            self.close()  # make sure the current socket is closed
            self._set_fpga_networking_parameters()

        self.udp = UDP(
            remote_ip_addr=self.fpga_ip_addr,
            remote_port_number=self.fpga_port_number,  # None or 0: use local port number
            local_port_number=self.local_port_number,  # 0 : use random port assigned by OS
            if_ip_addr=self.interface_ip_addr)

        self.udp.set_timeout(self.timeout)
        self.local_port_number = self.udp.local_port_number
        self.fpga_port_number = self.udp.remote_port_number
        # self.logger.info('   Opened control socket on %s:%i through interface %s'
        #                  % (self.fpga_ip_addr, self.local_port_number, self.interface_ip_addr))

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
        from pychfpga.fpga_firmware.chfpga import chFPGA

        ip_addr = self.fpga_ip_addr
        port_number = self.local_port_number
        serial_number = self.fpga_serial_number
        broadcast_group = 0
        # interface_ip_addr = self.interface_ip_addr

        logger = logging.getLogger(__name__)
        logger.debug(
            '%r: Broadcasting on port %i to configure FPGA S/N %016X '
            'with address %s:%i' %
            (self, chFPGA._BROADCAST_BASE_PORT, serial_number, ip_addr, port_number))

        # Build the array of bytes to fill the network configuration register
        # block
        ip_setup_string = struct.pack(
            '>H4s4sHQ', 0x1234, socket.inet_aton(ip_addr),
            socket.inet_aton(ip_addr), port_number, serial_number)
        trig1 = bytes([0x0C | broadcast_group])
        trig2 = bytes([0x8C | broadcast_group])

        # Configure the FPGA through a UDP broadcast packet containing the
        # target FPGA serial number
        trial = 0
        while trial < number_of_trials:
            with FPGAMmi(FPGAMmi.BROADCAST_IP_ADDR, FPGAMmi._BROADCAST_BASE_PORT) as mmi:
                # Send string with trigger flag cleared
                mmi.write(chFPGA._FPGA_IP_SETUP_BASE_ADDR, ip_setup_string + trig1)
                # resend with trigger flag set. The 0-to-1 transition will load the desired networking parameters
                mmi.write(chFPGA._FPGA_IP_SETUP_BASE_ADDR, ip_setup_string + trig2)
                # Write zeros everywhere to make sure we stop latching data
                mmi.write(chFPGA._FPGA_IP_SETUP_BASE_ADDR, [0] * len(ip_setup_string + trig2))
            # logger.debug('FPGA S/N %016X is configured with address %s:%i' % (serial_number, ip_addr, port_number))
            if not check:
                return
            (serial, timestamp) = self.get_fpga_config(ip_addr=ip_addr, port_number=port_number)
            if serial and serial == serial_number:
                return
            else:
                logger.debug(
                    '%r: Networking configuration of FPGA S/N %016X with address %s:%i failed.'
                    % (self, serial_number, ip_addr, port_number))
                trial += 1
        logger.debug(
            '%r: Unable to configure FPGA S/N %016X with address %s:%i'
            % (self, serial_number, ip_addr, port_number))
        raise FPGAMmiException(
            '%r: Unable to configure FPGA S/N %016X with address %s:%i'
            % (self, serial_number, ip_addr, port_number))

    def get_fpga_config(self, ip_addr, port_number,
                        timeout=0.1, number_of_trials=3):
        """
        Returns basic information allowing to check if we talk to the right
        FPGA with the right firmware.

        Will not cause an exception if the FPGA fails to respond at the
        specified address. Instead, all fields will be None.
        """
        trial = 0
        from pychfpga.fpga_firmware.chfpga import chFPGA

        with FPGAMmi(self.fpga_ip_addr, self.local_port_number) as mmi:
            while trial < number_of_trials:
                try:
                    serial = mmi.read(chFPGA._FPGA_SERIAL_NUMBER_ADDR, type=np.dtype('>u8'), timeout=timeout, retry=0)
                    timestamp = mmi.read(chFPGA._FPGA_TIMESTAMP_ADDR, type=np.dtype('>u4'), timeout=timeout, retry=0)
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

    def send(self, data):
        self.udp.send(data)

    def recv(self):
        return self.udp.recv()

    def _send_command(self, cmd, expected_reply_length, retry=None, resync=True, timeout_increase_factor=1):
        """ Send a read or write command to the FPGA and check the reply for the correct
        sequence number and packet length. If unsuccessful, the command will
        be resent ``retry`` times.

        This method is used by the read() and write() methods.

        Parameters:

            cmd (bytes): command bytes to send

            expected_reply_length (int): Number of bytes we expect in the reply, excluding the
                1-byte header. This is used to validate the reply and retry if necessary.

            retry (int): Number of times the command can be retried before raising an exception

            resync (bool): if `resync` is True, the sequence number will be resynchronized to the
                incoming reply and will not raise an exception.

            timeout_increase_factor (float): factor by which the timeout is
                increased each time there is a timeout error.

        Returns:

            bytes: The content of the reply packet without the header.

        Exceptions:

            IOError: Raised if a valid reply cannot be obtained after the retries.

        """
        old_timeout = self.get_timeout()
        if retry is None:
            retry = self.udp_retries
        trial = 1
        has_timed_out = False
        while True:
            try:
                error = ''
                self.udp.send(cmd)
                self.send_counter += 1
                data = self.udp.recv()
                self.recv_counter += 1
                # self.logger.warning(
                #   'read command: Got 0x%02x, expected 0x%02x'
                #   % (ord(data[0]), self.send_counter & 0xff))

                # Check if the sequence number returned by the FPGA
                # corresponds to ours so we know we got the answer to the
                # right command.
                seq = data[0]  # received sequence number
                if seq != self.send_counter & 0xff:
                    error = ('Invalid sequence number from a read command. '
                             'Got 0x%02x, expected 0x%02x.'
                             % (seq, self.send_counter & 0xff))
                    if resync:
                        self.send_counter = seq
                        error += ' Resynchronizing.'
                    self.flush()
                    # if we had a timeout, it is either because the command
                    # did not reach the FPGA or the reply didn't make it back.
                    # In the later case, our command counters are still in
                    # sync, so the next retry will work. In the first case, we
                    # advanced our counter when the FPGA didn't, so we'll resync if the
                    # FPGA is one count behind.
                    # elif has_timed_out and seq == (self.send_counter - 1) & 0xff:
                    #     self.send_counter = seq
                    #     error = "Command sequence number was offset by one following a timeout. " \
                    #             "The previous command probably didn't reach the FPGA. " \
                    #             "Resynchronizing and retrying to make sure."
                    # else:
                elif len(data) != expected_reply_length + 1:
                    error = "FPGA Read command returned %i bytes (0x%s) while %i were expected." % (
                        len(data),
                        ' '.join('%02X' % b for b in data),
                        expected_reply_length + 1)

                has_timed_out = False
            except self.udp.TimeoutException:
                self.set_timeout(self.get_timeout() * timeout_increase_factor)
                error = '%r: Timeout during FPGA command.' % self
                has_timed_out = True
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


    def broadcast_read(self, addr, type=np.dtype('>u8'), timeout=.5):
        """
        Reads memory-mapped object from multiple FPGAs through a broadcast
        request.

        Returns a array of type 'type' containing the values that were
        returned by all FPGAs.

        This command can read only a single object that is 1,2,4 or 8 bytes
        wide.
        """
        # number of bytes contained in the destination vector type
        byte_length = np.dtype(type).itemsize
        # compute the log2 of the number of bytes to read, limited to 3 (i.e. 8 bytes)
        log2_length = byte_length.bit_length()-1
        # number of bytes to read in this iteration
        read_length = 1 << log2_length
        # dout = np.zeros(byte_length, np.int8) # initialize result vector as a byte array
        dout = []
        # Loop to read all required bytes (the FPGA does not support multi-byte reads (yet))

        if addr & self._RAM_BASE_ADDR:
            opcode = self.OPCODE_READ_RAM
        elif addr & self._STATUS_BASE_ADDR:
            opcode = self.OPCODE_READ_STATUS
        else:
            opcode = self.OPCODE_READ_CONTROL

        command_bytes = bytes([
             (opcode << 5) | (log2_length << 3) + ((addr >> 16) & 0x07),
             (addr >> 8) & 0xFF,
             addr & 0xFF])

        self.udp.send(command_bytes)
        self.send_counter += 1
        self.recv_counter += 1  # We expect only one packet back
        self.udp.set_timeout(timeout)
        while True:
            try:
                data = self.udp.recv()
                if data == command_bytes:  # ignore the command packet that was broadcasted back to us
                    continue
            except self.udp.TimeoutException:
                break

            if len(data) != read_length + 1:
                raise FPGAMmiException(
                    "%r: FPGA Read command returned %i bytes. %i were expected." %
                    (self, len(data), read_length + 1))

            dout.append(np.frombuffer(data[1:], dtype=type)[0])  # store received byte
        return dout



def discover_fpgas(interface_ip_addr=None, source_subarrays=[0], timeout=0.1):
    """
    Get the serial numbers of all FPGA directly connected on the network (i.e.
    not accessed through the ARM processor)

    NOTE:
        - This function should not be called when the MMI interface is opened.
        - This function is supported only for direct Ethernet connections to
          the FPGA
        - //!\\ Calling this function will disrupt operations of all FPGAs in
          the network as reading from them cause them to redirect their
          outputs to this machine on the broadcast port.
    """
    from pychfpga.fpga_firmware.chfpga import chFPGA

    logger = logging.getLogger(__name__)

    if isinstance(source_subarrays, int):
        source_subarrays = [source_subarrays]

    serial_list = []
    #  for if_addr in interface_ip:
    for subarray in source_subarrays:
        logger.debug(
            'Searching ICEBoards on subarray %i through interface %s' %
            (subarray, interface_ip_addr))

        with FPGAMmi(
                FPGAMmi.BROADCAST_IP_ADDR,
                FPGAMmi._BROADCAST_BASE_PORT + subarray,
                interface_ip_addr=interface_ip_addr) as mmi:
            mmi.flush()
            serials = mmi.broadcast_read(
                chFPGA._FPGA_SERIAL_NUMBER_ADDR,
                type=np.dtype('>u8'),
                timeout=timeout)
        serial_list += serials

    return serial_list
