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

    def __init__(self, sock, buf_size=2048, max_packet_size=9000, verbose=1, report_pkt_len_me: int = 0):

        self.sock = sock
        self.n_packets = buf_size
        self.max_packet_size = max_packet_size
        self.verbose = verbose
        self.report_pkt_len_me = report_pkt_len_me

        self.buf = np.zeros((self.n_packets, self.max_packet_size), dtype=np.uint8)
        self.pkt_len = np.zeros((self.n_packets,), dtype=np.uint32) # packet length. 0= unused slot.
        self.n_stored = 0  # number of packets currently stored in the buffer


    def settimeout(self, timeout):
        """ Sets the timeout on the socket

        Parameter:

            timeout (float): timeout in seconds

        """
        self.sock.settimeout(timeout)

    def gettimeout(self):
        """ Returns the current timeout on the socket

        Returns:

            float: current timeout in seconds

        """
        return self.sock.gettimeout()

    def receive_packets(self, verbose=0):
        """ Receive some packets into the buffer until there is a timeout or the buffer is full
        """
        verbose = max(verbose, self.verbose)
        n = 0
        # last_ts = None
        while n < self.n_packets:
            try:
                nbytes, addr = self.sock.recvfrom_into(self.buf[n])
                self.pkt_len[n] = nbytes
                if verbose >= 3 and self.pkt_len[n] > self.report_pkt_len_me:
                    print(f"receive_packets: got raw packet {n:03d} from {addr}, len={nbytes}: {self.buf[n,:26].tobytes().hex(':')}")
                n += 1
            except socket.timeout:  # we have a timeout, so we probably have time to process data
                if not n:
                    continue
                if verbose >= 3:
                    print(f'receive_packets: timeout')
                break
        # we get here if there is a timeout or if the buffer is full
        if verbose >= 2:
            print(f'receive_packets: Returning {n} packets')
        self.n_stored = n
        return n



    def flush(self, timeout=0.001, max_flush_time=2, verbose=0):
        """ Flush the UDP buffer (read packets until timeout).
        Note: Partial frame data may start to fill the UDP buffer while we start to flush it. There is still likely a partial frame in the buffer after this operation.

        """
        flushed_packets = 0
        t0 = time.time()
        # Read packets until timeout
        self.sock.settimeout(timeout)
        # self.socket.setblocking(1)
        if verbose >=2:
            print('Flushing UDP buffer')
        while time.time() - t0 < max_flush_time:  # give up after some time
            try:
                s = self.sock.recv_into(self.buf[0])
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
