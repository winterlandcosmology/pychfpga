#!/usr/bin/python
# Disable pylint TAB warnings (W0312) and Line too long (=C0301)
# pylint: disable=W0312,C0301

"""
udp.py module
Provides a class that represents a UDP socket

 History:
        2014-03-04 JFC: Created
"""
import logging
import socket
import __main__  # used to store a list of all opened sockets

class Udp(object):
    """
    Implements basic UDP socket handling.
    """

    BROADCAST = '255.255.255.255'
    BUFFER_LENGTH = 32768

    timeout = socket.timeout
    TimeoutException = socket.timeout

    def __init__(self, remote_ip_addr=None, remote_port_number=None, local_port_number=0, if_ip_addr=None):
        """ Create and bind UDP socket.

        Parameters:

            remote_ip_addr (str): target IP address

            remote_port_number (int or None): target port number. If 0 or None, use the local port number.

            local_port_number (int): local port number. If 0, use OS-assigned port.
        """
        self.logger = logging.getLogger(__name__)
        self.remote_port_number = remote_port_number
        self.remote_ip_addr = remote_ip_addr
        self.address = (remote_ip_addr, remote_port_number)
        self.local_port_number = local_port_number or 0 # make sure None is 0

        if if_ip_addr:
            self.if_ip_addr = if_ip_addr
        elif hasattr(__main__, '_host_interface_ip_addr') and __main__._host_interface_ip_addr:
            self.if_ip_addr = __main__._host_interface_ip_addr
        else:
            raise RuntimeError(
                'An interface IP address is required for UDP communication '
                'with the FPGA')

        self.open()

    def open(self):
        """
        Opens a UDP socket at specified IP address and port over the specified
        interface. If ip_addr is Udp.BROADCAST, a broadcast socket will be
        opened.
        """

        # Make sure there is a list of opened sockets
        if not hasattr(__main__, '__opened_sockets__'):
            __main__.__opened_sockets__ = {}

        # If we want to use a specific local port that was previously reserved, use its socket.
        if self.local_port_number and self.local_port_number in __main__.__opened_sockets__:
            __main__.__opened_sockets__.pop(self.local_port_number).close()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if self.remote_ip_addr == self.BROADCAST:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, True)
        # sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, True)
        #self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1) # don't use REUSEADDR: many sockets get open and we then fail to receive replies

        # Bind the UDP port to the specified interface .
        #
        # By binding the socket, we set the source port and source address (intrface address) of
        # outgoing packets, and we allow the socket to receive packets with in the same
        # interfaceand port number. the desired values. The FPGA will send replies back to
        # this/port
        #
        # We need to specify the interface explicitely because the packet might be sent over the wrong (default) interface (which happened when the 10GbE was connected to the FPGA).
        self.sock.bind((self.if_ip_addr, self.local_port_number))
        # store the socket in the main module so it will live persistently until the Python session is closed.
        __main__.__opened_sockets__[self.local_port_number] = self.sock
        (local_addr, local_port) = self.sock.getsockname()
        self.local_port_number = local_port
        if not self.remote_port_number:
            self.remote_port_number = local_port
        self.address = (self.remote_ip_addr, self.remote_port_number)

        return self.sock

    def close(self):
        """Close the UDP socket"""
        if self.sock:
            self.sock.close()
            # if hasattr(__main__, '__opened_sockets__') and self.local_port_number in __main__.__opened_sockets__:
            #     __main__.__opened_sockets__.discard(self.local_port_number)
            self.sock = None
        # self.logger.debug('   Closed UDP control socket')

    def send(self, data):
        """
        Sends a string to the socket.
        """
        # print 'writing', data, 'to', self.address
        self.sock.sendto(data, self.address)

    def recv(self):
        """
        Reads a string from the socket.
        """
        data = self.sock.recv(self.BUFFER_LENGTH)
        return data


    def flush(self):
        """Flushes the socket receive buffer."""
        old_timeout = self.sock.gettimeout()
        self.sock.settimeout(0.1)
        try:
            while True:
                data = self.sock.recv(self.BUFFER_LENGTH)
                if len(data) == 0:
                    break
        except socket.timeout:
            pass # do nothing
            #print('Buffer is empty')
        self.sock.settimeout(old_timeout)

    def set_timeout(self, timeout):
        """
        Sets the socket timeout value in seconds.
        """
        self.sock.settimeout(timeout)

    def get_timeout(self):
        """
        Returns the current socket timeout value in seconds.
        """
        return self.sock.gettimeout()

    def is_broadcast(self):
        """
        Returns true if the current socket is set-up in broadcast mode.
        """
        return self.remote_ip_addr == self.BROADCAST
