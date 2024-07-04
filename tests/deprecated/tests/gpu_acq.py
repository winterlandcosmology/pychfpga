#!/usr/bin/env python
""" Raw data acquisition REST Server and Client with Python UDP packet receiver
"""
from __future__ import absolute_import, division, print_function

import os
import sys
import argparse
import logging
import socket
import time

import netifaces  # non-standard Python library (pip install netifaces)

import Queue
import SocketServer
import threading
# import logging
# import os
import struct
import numpy as np
import h5py
import datetime
import tornado
import psutil

import log
from rest import AsyncRESTServer, endpoint, AsyncRESTClient, coroutine, coroutine_return, IOLoop, RunSyncWrapper, moment
from pychfpga import NameSpace, Metrics



class RawAcqUDPReceiver(SocketServer.UDPServer):
    class UDPHandler(SocketServer.BaseRequestHandler):
        '''
        Puts the data in the queue. Another process will pull the data from the queue and write it
        to a hdf5 file.

        Example:
            addr = (receiver_ip, receiver_port)
            self.log.info('%.32r: Creating RawAcqUDPreceiver receiver for port %s on (%s:%s)' % (self, port, addr[0], addr[1]))
            receiver = RawAcqUDPReceiver(addr, self.data_queue)

        '''
        def handle(self):

            self.server.packet_counter += 1
            # data, socket = self.request
            # port = self.server.server_address[1]
            # (probe_id, stream_id, ts_high, ts_low) = self.server.unpack_header(data[:9])
            # chan = probe_id & 0x0F
            # timestamp = (ts_high << 32) + ts_low
            # flags = stream_id & 0xF
            # stream_id = (stream_id >> 4) & 0xFFF
            # adc_data = np.fromstring(data[9:2057], dtype=np.int8)
            #print( "Data received on port {0}, channel#{1}, std(data)={2}".format(port, chan, adc_data.std()) )
            #print("0x%03x"% stream_id,end='')
            # try:
            #     self.server.data_queue.put((timestamp, port, chan, stream_id, flags, adc_data), True, 0.8)
            #     self.server.queued_packets += 1# print(".", end='')
            # except Queue.Full:
            #     # print("o", end='')
            #     self.server.queue_overflows += 1
            #     pass
    def __init__(self, server_address, data_queue=None):
        self.data_queue = data_queue
        self.queue_overflows = 0
        self.queued_packets = 0
        self.packet_counter = 0
        self.unpack_header = struct.Struct('>BHHL').unpack_from  # Precompile unpack string for performance
        SocketServer.UDPServer.__init__(self, server_address, self.UDPHandler)  # cannot use super(...): this is an old-style class


if __name__ == '__main__':
    """
    Command-line interface to the raw_acq engine.
        raw_acq server --port 33221 # starts the server on localhost.
        raw_acq client --port 33221 --host localhost # starts a client in variable 'rc' to operate the server at localhost:33221

    Default port is 33221 if not specified.
    """
    pass