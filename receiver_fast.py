#!/usr/bin/python

'''
Fast python timestream receiver.  Simplified to just save timestreams fast!
'''

import struct, socket
import numpy as np

all_data = []
all_data_channels = []
timestamps = []

port = 41001

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(('',port))
print "Receiving at port %d" % (port)

BUF_SIZE=65536
RAW_DATA=2048+9
FRAME_HEADER_LENGTH = 9
data = bytearray(BUF_SIZE)
data_buf = buffer(data)
data_block = np.zeros((256,8,2048), dtype=np.int8)
channels = np.zeros((256,8), dtype=np.int)
timestamps = np.zeros((256,8), dtype=np.int)

def get_frames(nframes = 256):
    first = True
    last_timestamp=0
    channels = []
    for frame in xrange(nframes):
        nbytes = sock.recv_into(data)
        #data = sock.recv(BUF_SIZE)
        (probe_id, stream_id, word_length, timestamp) = struct.unpack_from('>BHHL', data_buf)
        frame_id = probe_id & 0xF0
        #channel = probe_id & 0x0F
        #all_data_channels.append(channel)
        if (frame_id == 0xA0):
            channel = probe_id & 0x0F
            if first:
                last_timestamp = timestamp
                first = False
                print 'in first loop'
            #print channel, timestamp, last_timestamp
            if (timestamp !=last_timestamp) & (~first) :
                all_data.append(data_block[channels])
                all_data_channels.append(channels)
                timestamps.append(last_timestamp)
                last_timestamp = timestamp
                channels = []
            data_block[channel, :] = data[FRAME_HEADER_LENGTH:RAW_DATA]                    
            channels.append(channel)
            #print timestamp
    return timestamps, all_data_channels, all_data

if __name__ == '__main__':
    #ts, adcs, ad = get_frames()
    adcs = get_frames()

