import numpy as np
import select

class RawReceiver(object):

    def __init__(self, socket):
        self.DATA_SIZE = 2048 # number of bytes of data
        self.sock = socket

        self.header_dtype = np.dtype(dict(
            names=['cookie', 'stream_id', 'flags', 'ts'],
            offsets=[0, 1, 3, 2],
            formats=['u1', '>u2', 'u1', '>u8']))

        self.packet_dtype = np.dtype([
            ('header', self.header_dtype, 1),
            ('data', np.int8, self.DATA_SIZE)])
        self.PACKET_SIZE = self.packet_dtype.itemsize
        self.NCHAN = 16


        # Define the packet buffer
        self.BUF_SIZE = self.NCHAN * 100 # size of receive buffer
        if not self.BUF_SIZE:
            self.log.warning('%r: Buffer size is zero! The list of expected STREAM IDs must have been empty!' % self)
        self.buf = np.empty((self.BUF_SIZE, self.PACKET_SIZE), dtype=np.uint8)
        self.n = 0  # number of packets currently stored in the buffer

        # Useful views into the packet buffer
        self.buf_struct = self.buf.view(self.packet_dtype)
        self.buf_stream_id = self.buf_struct['header']['stream_id'][:, 0]
        self.buf_flags = self.buf_struct['header']['flags'][:, 0]
        self.buf_ts = self.buf_struct['header']['ts'][:, 0]
        self.buf_data = self.buf_struct['data'][:, 0]
        self.count=np.zeros(16, dtype=np.int32)

    def flush(self):
        while select.select([self.sock],[],[],0)[0]==[self.sock]:
            self.sock.recv_into(self.buf[0])
            print 'flushing packet'

    def get_data_packet(self, N=1600, P=390625, flush=True):
        if flush:
            self.flush()
        last_ts = 0
        for i in xrange(N):
            self.sock.recv_into(self.buf[i])
            ts = int(self.buf_ts[i]) & 0xFFFFFFFFFFFF
            delta_ts = ts - last_ts
            last_ts = ts
            print 'cap=%0.6f, ts=%i (0x%06X) delta=%i (0x%06X)' % (float(ts) / P, ts, ts, delta_ts, delta_ts)
        print 'Count for each channel:', np.bincount(self.buf_stream_id & 0xFF)
        print 'Count for each timestamp:', [(v,c) for v,c in zip(*np.unique(self.buf_ts & 0xFFFFFFFFFFFF, return_counts=True))]

        return self.buf_struct[:N]


