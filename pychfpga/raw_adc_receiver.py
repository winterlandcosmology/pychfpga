'''
Raw timestream receiver and hdf5 writer.
Define array of 'ports' assumed to have last 2 digits be the slot number,
and the current host ip to listen to.  Will then listen for and write from all
boards into hdf5 file.  
'''

from Queue import Queue
import SocketServer
import threading
# import logging
# import os
import struct
import numpy as np
import h5py
import time
import datetime
import os


class TimestreamUdpHandler(SocketServer.BaseRequestHandler):
    '''
    Runs the basic receiver part.  Puts data in Queue to be parsed
    and written to hdf5 file.
    '''
    # def __init__(self, request, client_address, server):
    #     #self.logger = logging.getLogger('TimestreamUdpHandler')
    #     #self.logger.debug('__init__')
    #     SocketServer.BaseRequestHandler.__init__(self, request,
    #                                                client_address, server)
    def handle(self):
        self.data = self.request[0] # .strip()
        self.socket = self.request[1]
        (probe_id, stream_id, word_length,
            self.timestamp) = struct.unpack_from('>BHHL', self.data)
        self.ant_channel = probe_id & 0x0F
        self.adc_data = np.fromstring(self.data[9:9+2048], dtype=np.int8)
        if (self.adc_data.shape[0] != 2048):
            print "bad data?"
            print self.adc_data
            print '#######################'
            print len(self.data)
            print '#######################'
            print len(self.request[0])
        else:
            self.server.data_queue.put((self.timestamp,
                                    self.ant_channel, self.adc_data))


class ThreadedUdpServer(SocketServer.ThreadingMixIn, SocketServer.UDPServer):
    def __init__(self, server_address, handler_class, data_queue):
        self.data_queue = data_queue
        SocketServer.UDPServer.__init__(self, server_address, handler_class)


class hdf5TimestreamData(object):
    def __init__(self, filestring):
        self.N_SAMP = 2048
        self.N_ANT = 16
        self.f = h5py.File(filestring, 'w')
        self.timestampDataset = self.f.create_dataset('timestamp',
                  (1, self.N_ANT), dtype=np.int32, maxshape=(None, self.N_ANT))
        self.portDataset = self.f.create_dataset('slot', (1, 1),
                                            dtype=np.int32, maxshape=(None, 1))
        self.timestreamDataset = self.f.create_dataset('timestream',
                        (1, self.N_ANT, self.N_SAMP), dtype=np.int8,
                        maxshape=(None, self.N_ANT, self.N_SAMP))
        self.n_times = 1
        self.n = 0

    def write_singletime(self, timestamp, port, timestream):
        if self.n == self.n_times:
            self.n_times = self.n+1
            self.timestampDataset.resize((self.n_times, self.N_ANT))
            self.portDataset.resize((self.n_times, 1))
            self.timestreamDataset.resize((self.n_times, self.N_ANT, self.N_SAMP))
        elif self.n < self.n_times:
            pass
        else:
            print "ut oh..."
        print self.n_times
        self.timestampDataset[self.n] = timestamp
        self.portDataset[self.n] = port % 100  # assume port gives slot
        self.timestreamDataset[self.n] = timestream
        self.n += 1

    def close(self):
        self.f.close()

class hdf5LiveTimestreamData(object):
    def __init__(self, filestring):
        self.N_SAMP = 2048
        self.N_ANT = 16
        self.f = h5py.File(filestring, 'a')
        self.timestampDataset = self.f.require_dataset('timestamp',
                  (16, self.N_ANT), dtype=np.int32, maxshape=(None, self.N_ANT))
        self.portDataset = self.f.require_dataset('slot', (16, 1),
                                            dtype=np.int32, maxshape=(None, 1))
        self.timestreamDataset = self.f.require_dataset('timestream',
                        (16, self.N_ANT, self.N_SAMP), dtype=np.int8,
                        maxshape=(None, self.N_ANT, self.N_SAMP))


    def init(self, n_times, n):
        self.n_times = n_times
        self.n = n
    

    def write_singletime(self, timestamp, port, timestream):
        if self.n == self.n_times:
            self.n_times = self.n+1
            #self.timestampDataset.resize((self.n_times, self.N_ANT))
            #self.portDataset.resize((self.n_times, 1))
            #self.timestreamDataset.resize((self.n_times, self.N_ANT, self.N_SAMP))
        elif self.n < self.n_times:
            pass
        else:
            print "ut oh..."
        print self.n_times
        self.timestampDataset[self.n] = timestamp
        self.portDataset[self.n] = port % 100  # assume port gives slot
        self.timestreamDataset[self.n] = timestream
        self.n += 1 

    def close(self):
        self.f.close()

class dataProcessor(object):
    def __init__(self, data_queue, out_queue, port):
        self.data_queue = data_queue
        self.out_queue = out_queue
        self.all_data = np.zeros((16, 2048), dtype=np.int8)
        # Should change to only one timestamp per datset
        self.all_ts = np.zeros(16, dtype=np.int32)
        self.timestamp = 0
        self.old_timestamp = 0
        self.n_ant_rec = 0
        self.N_ANT = 16
        # self.h5file = hdf5TimestreamData()
        self.port = port

    def process(self):
        while True:
            self.timestamp, self.ant, self.adc_data = self.data_queue.get()
            if (self.timestamp == self.old_timestamp):
                self.all_ts[self.ant] = self.timestamp
                self.all_data[self.ant, :] = self.adc_data
                self.n_ant_rec += 1
            elif (self.timestamp != self.old_timestamp) and (self.n_ant_rec == self.N_ANT):
                # Got a full set, write to disk and start over may not need zeroing
                # data, but helps debugging.
                # self.h5file.write_singletime(self.all_ts, self.all_data)
                self.out_queue.put((self.all_ts, self.port, self.all_data))
                self.all_data = np.zeros((16, 2048), dtype=np.int8)
                self.all_ts = np.zeros(16, dtype=np.int32)
                self.old_timestamp = self.timestamp
                self.all_ts[self.ant] = self.timestamp
                self.all_data[self.ant, :] = self.adc_data
                self.n_ant_rec = 1
            elif (self.timestamp != self.old_timestamp) and (self.n_ant_rec < self.N_ANT):
                # Start over
                print "didn't get full set, only received {0} ant. restarting.".format(self.n_ant_rec) # Logging eventually
                # May not need zeroing data.  Helps debugging
                self.all_data = np.zeros((16, 2048), dtype=np.int8)
                self.all_ts = np.zeros(16, dtype=np.int32)
                self.old_timestamp = self.timestamp
                self.all_ts[self.ant] = self.timestamp
                self.all_data[self.ant, :] = self.adc_data
                self.n_ant_rec = 1


class dataWriter(object):
    def __init__(self, out_queue):
        if not isinstance(out_queue, (list, tuple)):
            self.out_queue = [out_queue]
        else:
            self.out_queue = out_queue
        self.n_file = 0
        self.N_TIME_PER_FILE = 64
        self.time_name = datetime.datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')
        self.base_dir = './'+ self.time_name + '_pathfinder_rawadc/'
        self.live_base_dir = './'
        try:
            os.mkdir(self.base_dir)
        except:
            print "couldn't make directory... using current one."
            self.base_dir = './'
        self.h5name = self.base_dir + "{0:06d}.h5".format(self.n_file)
        self.h5file = hdf5TimestreamData(self.h5name)
        self.live_name = self.live_base_dir + "live_adc_data.h5"
        self.live_h5file = hdf5LiveTimestreamData(self.live_name)
        #self.live_h5file.init()
        self.live_h5file.close()

    def write(self):
        while True:
            for i in xrange(self.N_TIME_PER_FILE):
                for j, out_q in enumerate(self.out_queue):
                    self.all_ts, self.port, self.all_data = out_q.get()
                    self.h5file.write_singletime(self.all_ts, self.port, self.all_data)
                    self.live_h5file = hdf5LiveTimestreamData(self.live_name)
                    self.live_h5file.init(n_times=j+1, n=j)
                    self.live_h5file.write_singletime(self.all_ts, self.port, self.all_data)
                    self.live_h5file.close()
            self.h5file.close()
            #self.live_h5file.close()
            self.n_file += 1
            self.h5name = self.base_dir + "{0:06d}.h5".format(self.n_file)
            self.h5file = hdf5TimestreamData(self.h5name)
            #self.live_h5file.init(n_times=1, n=0)
            #self.live_h5file.close()

if __name__ == "__main__":
    HOST = "10.10.10.25"
    #PORTS = [41101, 41102, 41103, 41104, 41105, 41106, 41107, 41108, 41109,
    #           41110, 41111, 41112, 41113, 41114, 41115, 41116]
    # [41102, 41103, 41106, 41114, 41116]
    PORTS = [41101]
    data_queues = []
    out_queues = []
    servers = []
    server_threads = []
    data_processors = []
    data_process_threads = []
    for port in PORTS:
        data_queues.append(Queue())
        out_queues.append(Queue())
        servers.append(ThreadedUdpServer((HOST, port), TimestreamUdpHandler, data_queues[-1]))
        server_threads.append(threading.Thread(target=servers[-1].serve_forever))
        server_threads[-1].setDaemon(True)
        server_threads[-1].start()
        print 'server thread started'
        data_processors.append(dataProcessor(data_queues[-1], out_queues[-1], port))
        data_process_threads.append(threading.Thread(target=data_processors[-1].process))
        data_process_threads[-1].setDaemon(True)
        data_process_threads[-1].start()

    dataWriter = dataWriter(out_queues)
    data_writer_thread = threading.Thread(target=dataWriter.write)
    data_writer_thread.setDaemon(True)
    data_writer_thread.start()
    #
    # Sleep loop.  need if set to Daemon.
    while True:
        try:
            time.sleep(180)
        except KeyboardInterrupt:
            break
    print "done writing for now"
