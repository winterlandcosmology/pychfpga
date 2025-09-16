#!/usr/bin/python
# Disable pylint TAB warnings (W0312) and Line too long (=C0301)
# pylint: disable=W0312,C0301

"""
socketIO.py module. Implements socket communications to chFPGA. Used by chFPGA_receiver.py. *To be obsoleted*.


History:
    2011-08-14 JFC : Created from the code in chFPGA.py
    2011-09-18 JFC: Added socket timout variable
    2012-03-31 JFC: Removed manual ARP entry now that the firmware supports ARP protocol. Was a problem with Win 7 (running non-admin) and with a router.
    2012-05-18 JFC: Let the code automatically determine the host computer IP address on which to open a listening port
    2014-04-13 JMP: Removed unused class ControlSocket_base. Fixed get_host_addr() so now also works on linux. Small fix on DataSocket_base.__init__ regarding host_ip handling.
"""

import socket
import logging
import numpy as np
import __main__

timeout = socket.timeout  #110918 JFC
TimeoutException = socket.timeout

# The following exception name exists elsewhere. Removed to prevent collision in docs. Does not seem to be used.
# class FPGAException(Exception):
#     logger = logging.getLogger('FPGAException')
#     def __init__(self, message):
#         super().__init__(message)
#         self.logger.exception(message)


class DataSocket_base(object):
    """Creates an object that represents the control socket communication link to the chFPGA."""

    BUFFER_LENGTH = 32768

    def __init__(self, ip_address, port_number=0, netmask='255.255.0.0', host_ip=None):

        # Defines basic variables
        self.logger = logging.getLogger(__name__)
        self.netmask = netmask # network mask used to find the host address that is on the same subnet as the target IP. This does not affect the network adapter settings.
        self.ip_address = ip_address # IP of the chFPGA board. Used to determine the host address
        self.port_number = port_number # Data port on the host (Control port +1), to receive frame data
        self.sock = None
        self.host_ip = host_ip
        if host_ip:
            self.host_ip = host_ip
        elif hasattr(__main__, '_host_interface_ip_addr') and __main__._host_interface_ip_addr:
            self.host_ip = __main__._host_interface_ip_addr
        else:
            self.host_ip = get_host_addr(dest_addr=self.ip_address, netmask=self.netmask)


        self.open()

    def open(self):
        """
        Open data communication socket communications to chFPGA. This is a listen-only socket.
        """
        if not hasattr(__main__, '__opened_sockets__'):
            __main__.__opened_sockets__ = {}

        # if self.port_number in __main__.__opened_sockets__:
        #     self.sock = __main__.__opened_sockets__[self.port_number]
        #     self.logger.debug('%r: reusing existing socket %i' % (self, self.port_number))
        # else:
        self.logger.debug('%r: Creating new socket %s:%i' % (self, self.host_ip, self.port_number))
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((self.host_ip, self.port_number))

        if not self.port_number:
            (_, self.port_number) = self.sock.getsockname()

        self.sock.settimeout(2)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, self.BUFFER_LENGTH)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        self.logger.debug('%r: Using host address %s' % (self, self.host_ip))
        self.logger.debug('%r: Opened data UDP Socket' % self)
        self.logger.debug('%r:     Data port:    listening on %s:%i ' % (self, self.host_ip, self.port_number))

    def __repr__(self):
        return 'RecvSocketIO(%s)' % self.ip_address

    def close(self):
        """Closes the communication socket"""
        if self.port_number not in __main__.__opened_sockets__:
            self.sock.close()
            self.logger.debug('%r: Closed UDP data socket' % self)
        else:
            self.logger.debug('%r: Closed UDP link, socket left open for future use' % self)

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

    def read(self, timeout_delay=None): #110918 JFC: Added timeout_delay
        """
        Reads a string from the control socket.
        """
        if timeout_delay is not None:
            self.sock.settimeout(timeout_delay)
        else:
            self.sock.settimeout(0.1)

        data = self.sock.recv(self.BUFFER_LENGTH)
        return data


def get_host_addr(dest_addr, netmask='255.255.0.0', only_one=True):
    """
    Returns the IP of the host adapter that is on the same subnet as the specified destination IP given the net mask
    """
    host_data = socket.gethostbyname_ex(socket.gethostname()) # get the list of IP addresses associated with this computer
    try:
        import netifaces
        host_addr_list = [netifaces.ifaddresses(iface)[netifaces.AF_INET][0]['addr'] for iface in netifaces.interfaces()] # This method to get all the host ip addresses works both on Linux and Windows
    except: # If netifaces not installed, try the old way (Works on Windows but may not work on Linux)
        host_addr_list = host_data[2] # get the list of IP addresses associated with this computer
    dest_addr_vect = np.array(map(ord, socket.inet_aton(dest_addr))) # convert the target IP into a vector
    netmask_vect = np.array(map(ord, socket.inet_aton(netmask))) # convert the net mask into a vector

    matched_addr = []
    for host_addr in host_addr_list:
        host_addr_vect = np.array(map(ord, socket.inet_aton(host_addr))) # convert the host address into a vector
        if all((host_addr_vect & netmask_vect) == (dest_addr_vect & netmask_vect)):
            matched_addr.append(host_addr)
    if only_one and len(matched_addr) != 1:
        raise SystemError('Could not determine the host address. Found %i possible matches for %s/%s on the following adapters for %s : %s' % (len(matched_addr), dest_addr, netmask, host_data[0], ', '.join(host_addr_list)))
    return matched_addr[0]


