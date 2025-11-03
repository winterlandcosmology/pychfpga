import logging
import numpy as np
import socket
import time

class UDPPacketReceiver:
    """ Receives and stores UDP packets until the buffer is full or a timout has occured.

        Parameters:

            sock (socket.socket): Socket to use for receiving UDP packets

            n_packets (int): Size of the packet buffer in number of packets

            max_packet_size (int): Maximum expected UDP payload length
    """

    def __init__(self, socket, n_packets=2048, max_packet_size=9000, verbose=1):

        self.socket = socket
        self.n_packets = n_packets
        self.max_packet_size = max_packet_size;
        self.verbose = verbose

        self.buf = np.zeros((n_packets, max_packet_size), dtype=np.uint8)
        self.pkt_len = np.zeros((n_packets,), dtype=np.uint32) # packet length. 0= unused slot.
        self.n = 0  # number of packets currently stored in the buffer

    def settimeout(self, timeout):
        """ Sets the timeout on the socket

        Parameter:

            timeout (float): timeout in seconds

        """
        self.socket.settimeout(timeout)

    def gettimeout(self):
        """ Returns the current timeout on the socket

        Returns:

            float: current timeout in seconds

        """
        return self.socket.gettimeout()

    def get_packets(self, verbose=0):
        """ Receive some packets into the buffer until there is a timeout or the buffer is full
        """
        verbose = max(verbose, self.verbose)
        # n = self.n # current packet number
        n = 0
        # last_ts = None
        while n < self.n_packets:
            try:
                s = self.pkt_len[n] = self.socket.recv_into(self.buf[n])
                if verbose >= 3:
                    print(f"get_packets: got raw packet {n:03d}, len={s}: {self.buf[n,:16].tobytes().hex(':')}")
                n += 1
            except socket.timeout:  # we have a timeout, so we probably have time to process data
                if not n:
                    continue
                if verbose >= 3:
                    print(f'get_packets: timeout')
                break
        # we get here if there is a timeout or if the buffer is full
        if verbose >= 2:
            print(f'get_packets: Returning {n} packets')
        self.n = n
        return n



    def flush(self, timeout=0.001, max_flush_time=2, verbose=0):
        """ Flush the UDP buffer (read packets until timeout).
        Note: Partial frame data may start to fill the UDP buffer while we start to flush it. There is still likely a partial frame in the buffer after this operation.

        """
        flushed_packets = 0
        t0 = time.time()
        # Read packets until timeout
        self.socket.settimeout(timeout)
        # self.socket.setblocking(1)
        if verbose >=2:
            print('Flushing UDP buffer')
        while time.time() - t0 < max_flush_time:  # give up after some time
            try:
                s = self.socket.recv_into(self.buf[0])
                flushed_packets += 1
                if verbose >=2:
                    print(f"flushing packet Starting with {self.buf[0][:10].tobytes().hex(':')}")

            except socket.timeout:
                break
        else:
            print(f'Stopped flushing after {max_flush_time} s: packets are arriving faster that the specified timeout period of {timeout} s')
        if verbose:
            print(f'Flushed {flushed_packets} packets')
        return flushed_packets
