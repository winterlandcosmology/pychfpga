#from tornado import httpserver
from tornado import gen
from tornado.ioloop import IOLoop
import tornado.web
'''
Raw timestream receiver and hdf5 writer.
Define array of 'ports' assumed to have last 2 digits be the slot number,
third digit the crate,
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

#Should be in gain.py or something.
class GainCalc(object):
    def __init__(self, gain=None, zero=False):
        self.zero = zero
        self.mask = None
        self.idealRMS = 1.5 * np.sqrt(2)
        self.default_log2_gain = 22
        if gain:
            self.g = np.zeros((16,1024))
            for i in range(16):
                inb = np.array(gain[i][1][0])*2**(gain[i][1][1])
                self.g[i,:] = inb
        else:
           self.g = None  

    def update(self, signal):
        self.signal = np.array(signal)
        mask = np.ma.make_mask_none((len(signal),))
        #The first bin is always bad for some reason
        mask[0] = True
        self.masked = np.ma.array(np.log(signal), mask=mask)

    def convert_gain_format(self):
        '''
        Expects array in. returns (glin, glog)
        '''
        #2**14 is max for linear gain
        #ignore dc component
        #check for nans
        #print g
        bad_values = (self.g > 2**31) | ~np.isfinite(self.g)
        g = np.ma.array(self.g,mask=bad_values)
        glog = (np.ceil(np.log2(np.ma.median(np.abs(g)/2**13,axis=1)))).astype(np.int)
        glin = np.zeros(g.shape, dtype=np.float)
        for i, glog_single in enumerate(glog):
            glin[i] = g[i]/2**glog[i]
        glog.data[glog.mask == True] = np.ma.median(glog)
        glog.mask[glog.mask] = False
        glin[bad_values] = 2**14
        return glin, glog.data

    def noisy_gain_estimate(self, data ):
        outrms = data[:,:,:].std(axis=0)
        outrms[outrms < 0.8] = 0.8
        #rmss.append(outrms.mean())
        print outrms.mean(axis=1)
        if self.g is not None:
            #not sure about format of previous gain yet...
            #needs to be a post of current gain setting I think...
            #glin, glog = self.convert_gain_format(previous_gain):
            glin, glog = self.convert_gain_format()
            # Not sure what g should be here.  
            #g = self.idealRMS*2**(self.default_log2_gain)/outrms
            for j, glog1 in enumerate(glog):
                self.g[j] = self.idealRMS * glin[j] * (2**(glog[j]))/outrms[j] #idealRMS*glin*(2**(glog-4))/outrms
                self.g[j] = (20.0 * self.g[j] + 80.0 * glin[j] * (2**(glog[j])))/100.0
        else:
            #Assumes was set to something simple (glin=1), glog is something.
            self.g = self.idealRMS*2**(self.default_log2_gain)/outrms#idealRMS*2**(default_log2_gain-4)/outrms
        self.glin, self.glog = self.convert_gain_format()
        print self.glog
        bad_gains = self.glin > 2**14
        self.glin[bad_gains] = 2**14
        self.glin = self.glin.astype(np.int).astype(np.float)
        gain = []
        for channel in range(16):
            gain.append([channel,[self.glin[channel].tolist(), self.glog[channel]]])
        return gain

    def fourier_filter(self, signal, num_components):
        '''
        Filters signal with top-hat in fourier space.  Padded with itself on either     side to improve edge behavior.
        Should extend to other windows.
        not assured to maintain signal size
        '''
        signal = np.array(signal)
        signal_length = signal.size
        f_signal = np.fft.fft(np.r_[signal[signal_length/2:0:-1],signal,signal[-1:-signal_length/2:-1]])
        f_signal[num_components:-num_components] = 0
        filtered = np.fft.ifft(f_signal)[signal_length/2:-signal_length/2+1]
        filtered = (filtered.real).astype(np.int).astype(np.float)
        return filtered

    def flag_rfi(self, in_arr, fit, threshold):
        '''
        Identifies RFI in the signal spectrum by finding larger than expected jumps in the signal.
        Returns array of flags for each bin
        '''
        rfmask = abs(in_arr) < abs(fit/threshold)
        in_arr.mask = rfmask|in_arr.mask

    def poly_filter(self, signal, threshold, degree):
        '''
        Filters signal using a polynomial fit. Ignores RFI in calculating the polynomial.
        '''
        x = np.ma.array(np.arange(len(signal)), mask=signal.mask)
        fit = np.polyfit(np.ma.compressed(x), np.ma.compressed(signal), degree)
        #fit = np.polyfit(flagged, x, degree)
        fitarr = np.poly1d(fit)(np.arange(len(signal)))
        self.flag_rfi(signal, fitarr, threshold)
        return fitarr

    def iterative_poly_filter(self, signal):
        mask = np.ma.make_mask_none((len(signal),))
        #The first bin is always bad for some reason
        mask[0] = True
        degree = 1
        threshold = 1.2
        masked = np.ma.array(np.log(signal), mask=mask)
        while threshold > 1.01:
            fitarr = self.poly_filter(masked, threshold, degree)
            threshold = 1 + (threshold - 1)*0.8
            if degree < 15:
                degree += 2
        filtered = np.exp(fitarr)
        filtered = (filtered.real).astype(np.int).astype(np.float)
        return filtered, masked.mask

    def run(self, filtertype='hybrid', num_components = 50):
        if filtertype == 'fourier':
            output = self.fourier_filter(self.signal, num_components)
        elif filtertype == 'poly' or filtertype == 'hybrid':
            output, mask = self.iterative_poly_filter(self.signal)
            self.mask = mask
            if filtertype == 'hybrid':
                in_arr = self.signal.copy()
                in_arr[mask] = output[mask]
                output = self.fourier_filter(in_arr, num_components)
            if self.zero:
                output[mask] = 0
            else:
                output[mask] = self.signal[mask]
        else:
            raise ValueError
        output = (output.real).astype(np.int).astype(np.float)
        return output


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
        self.port = self.server.server_address[1]
        (probe_id, stream_id, word_length,
            self.timestamp) = struct.unpack_from('>BHHL', self.data)
        self.ant_channel = probe_id & 0x0F
        self.adc_data = np.fromstring(self.data[9:2057], dtype=np.int8)
        #if (self.adc_data.shape[0] != 2048):
        #    print "bad data?"
        #    print self.adc_data
        #    print '#######################'
        #    print len(self.data)
        #    print '#######################'
        #    print len(self.request[0])
        print( "{0} {1} {2}".format(self.port, self.ant_channel, self.adc_data.std()) )
        self.server.data_queue.put((self.timestamp, self.port,
                                    self.ant_channel, self.adc_data))


class ThreadedUdpServer(SocketServer.ThreadingUDPServer):
    def __init__(self, server_address, handler_class, data_queue):
        self.data_queue = data_queue
        super(ThreadedUdpServer, self).__init__(self, server_address, handler_class)


class hdf5TimestreamData(object):
    def __init__(self, filestring):
        self.N_SAMP = 2048
        #self.N_ANT = 1
        self.f = h5py.File(filestring, 'w')
        self.f.attrs["file_name"] = filestring
        self.f.attrs["data_type"] = "ADC snapshot data"
        self.f.attrs["version"] = 0.1
        self.f.attrs["timestamping_warning"] = "Done on file write, may be significantly different from snapshot acquistion time"        
        self.compound_dtype = np.dtype([('fpga_count', np.uint64), ('ctime', np.float64)])
        self.timestampDataset = self.f.create_dataset('timestamp',
                    (1, 1), dtype=self.compound_dtype, maxshape=(None, 1))
        self.timestampDataset.attrs['axis'] = ['snapshot', 'time']
        self.slotDataset = self.f.create_dataset('slot', (1, 1),
                                            dtype=np.uint8, maxshape=(None, 1))
        self.slotDataset.attrs['axis'] = ['snapshot', 'slot_number']
        self.crateDataset = self.f.create_dataset('crate', (1, 1),
                                            dtype=np.uint32, maxshape=(None, 1))
        self.crateDataset.attrs['axis'] = [ 'snapshot', 'crate_number']
        self.antDataset = self.f.create_dataset('adc_input', (1, 1),
                                            dtype=np.uint8, maxshape=(None, 1))
        self.antDataset.attrs['axis'] = ['snapshot', 'adc_input_number']
        self.timestreamDataset = self.f.create_dataset('timestream',
                        (1, self.N_SAMP), dtype=np.int8,
                        maxshape=(None, self.N_SAMP))
        self.timestreamDataset.attrs['axis'] = ['snapshot', 'timestream_data']
        self.n_times = 1
        self.n = 0

    def write_singletime(self, timestamp, port, ant, timestream):
        if self.n == self.n_times:
            self.n_times = self.n+1
            self.timestampDataset.resize((self.n_times, 1))
            self.slotDataset.resize((self.n_times, 1))
            self.crateDataset.resize((self.n_times, 1))
            self.antDataset.resize((self.n_times, 1))
            self.timestreamDataset.resize((self.n_times, self.N_SAMP))
        elif self.n < self.n_times:
            pass
        else:
            print "ut oh..."
        print self.n_times
        current_time = time.time()
        self.timestampDataset[self.n] = (timestamp, current_time)
        self.antDataset[self.n] = ant
        self.slotDataset[self.n] = port % 100  # assume port gives slot
        self.crateDataset[self.n] = ((port/100) % 10) - 1
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



class dataWriter(object):
    def __init__(self, data_queue):
        if not isinstance(data_queue, (list, tuple)):
            self.data_queue = [data_queue]
        else:
            self.data_queue = data_queue
        self.n_file = 0
        self.N_ELEMENT_PER_FILE = 2048*64
        self.time_name = datetime.datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')
        self.base_dir = './'+ self.time_name + '_CHIME_pfFirmwareC0_rawadc/'
        #self.live_base_dir = '/mnt/agogo/livedata/'
        try:
            os.mkdir(self.base_dir)
        except:
            print "couldn't make directory... using current one."
            self.base_dir = './'
        self.h5name = self.base_dir + "{0:06d}.h5".format(self.n_file)
        self.h5file = hdf5TimestreamData(self.h5name)
        self.run = True
        #self.live_name = self.live_base_dir + "live_adc_data.h5"
        #self.live_h5file = hdf5LiveTimestreamData(self.live_name)
        #self.live_h5file.init()
        #self.live_h5file.close()

    def write(self):
        while self.run:
            n_elements = 0
            while n_elements < self.N_ELEMENT_PER_FILE:
                for j, out_q in enumerate(self.data_queue):
                    if not out_q.empty():
                        self.all_ts, self.port, self.ant, self.all_data = out_q.get()
                        self.h5file.write_singletime(self.all_ts, self.port, self.ant, self.all_data)
                        n_elements += 1
                        #self.live_h5file = hdf5LiveTimestreamData(self.live_name)
                        #self.live_h5file.init(n_times=j+1, n=j)
                        #self.live_h5file.write_singletime(self.all_ts, self.port, self.all_data)
                        #self.live_h5file.close()
            self.h5file.close()
            #self.live_h5file.close()
            self.n_file += 1
            self.h5name = self.base_dir + "{0:06d}.h5".format(self.n_file)
            self.h5file = hdf5TimestreamData(self.h5name)
            #self.live_h5file.init(n_times=1, n=0)
            #self.live_h5file.close()

class Receiver(object):
    '''
    Interactive receiver object.  To create port threads, and get data out
    from those ports.
    Should probably fix the 'serve forever bits'
    '''
    def __init__(self, ports=[41101], host='10.10.10.25'):
        self.HOST = host
        self.PORTS = ports
        self.dataWriter = None
        self.previousGain = None

    def start(self):
        self.data_queues = []
        self.servers = []
        self.server_threads = []
        self.N_ANT = 16
        self.old_timestamp = 0
        self.n_ant_rec = 0
        self.all_data = []
        self.all_ts = []
        for port in self.PORTS:
            self.data_queues.append(Queue())
            self.servers.append(ThreadedUdpServer((self.HOST, port), TimestreamUdpHandler, self.data_queues[-1]))
            self.server_threads.append(threading.Thread(target=self.servers[-1].serve_forever))
            self.server_threads[-1].setDaemon(True)
            self.server_threads[-1].start()
            print('server thread started')
            self.all_data.append(np.zeros((16, 2048), dtype=np.int8))
            self.all_ts.append(np.zeros(16, dtype=np.int32))
        self.all_data = np.array(self.all_data)
        self.all_ts = np.array(self.all_ts)

    def startHdf5Disk(self):
        self.dataWriter = dataWriter(self.data_queues)
        self.data_writer_thread = threading.Thread(target=self.dataWriter.write)
        self.data_writer_thread.setDaemon(True)
        self.data_writer_thread.start()

    def estimateGains(self):
        ''' Assume setup to send spectrum data.  average a number of
            frames together, and get estimate of new gain settings.'''
        n_avg = 2
        gain_estimates = []
        number_of_frames = 0
        spectrum = np.zeros((len(self.PORTS),n_avg, 16, 1024), dtype=np.complex)
        while number_of_frames < n_avg:
            self.all_ts, self.PORTS, self.all_data = self.read_data()        
            data_unpacked = (np.array(self.all_data).astype(np.int8) ^ np.int8(128)) >> 4
            spectrum[:,number_of_frames,:,:] = data_unpacked[:,:,::2] + 1.0j*data_unpacked[:,:,1::2]
            number_of_frames += 1
        
        for i, port in enumerate(self.PORTS):
            if self.previousGain:
                gain_calc = GainCalc(self.previousGain[i])
                first_run = False
            else:
                gain_calc = GainCalc()
                self.previousGain = []
                first_run = True
            gain = gain_calc.noisy_gain_estimate(spectrum[i])
            for j in range(16):
                gain_calc.update(gain[j][1][0])
                glin_update = gain_calc.run()
                gain[j][1][0] = glin_update.tolist()
            if first_run:
                self.previousGain.append(gain)
            else:
                self.previousGain[i] = gain
        return self.previousGain
                    
        
            

    def read_data(self):
        for j, out_q in enumerate(self.data_queues):
            trying_to_receive = True
            while trying_to_receive:
                self.timestamp, self.port, self.ant, self.adc_data = out_q.get()
                if (self.timestamp == self.old_timestamp) and (self.n_ant_rec < self.N_ANT - 1):
                    self.all_ts[j][self.ant] = self.timestamp
                    self.all_data[j][self.ant, :] = self.adc_data
                    self.n_ant_rec += 1
                elif (self.timestamp == self.old_timestamp) and (self.n_ant_rec == self.N_ANT - 1):
                    self.all_ts[j][self.ant] = self.timestamp
                    self.all_data[j][self.ant, :] = self.adc_data
                    self.n_ant_rec = 0
                    self.old_timestamp = 0
                    trying_to_receive = False
                elif (self.timestamp != self.old_timestamp) and (self.n_ant_rec < self.N_ANT):
                    # Start over, would be new set start as well.
                    #print "didn't get full set, only received {0} ant. restarting.".format(self.n_ant_rec)
                    self.old_timestamp = self.timestamp
                    self.all_ts[j][self.ant] = self.timestamp
                    self.all_data[j][self.ant, :] = self.adc_data
                    self.n_ant_rec = 1
        #SHould use the returned port.  cheating here.
        return self.all_ts, self.PORTS, self.all_data

    def stop(self):
        for i, port in enumerate(self.PORTS):
            self.servers[i].shutdown()
            self.servers[i].server_close()
            print("shutdown servers")
            if not self.data_queues[i].empty():
                self.data_queues[i].queue.clear()
        if self.dataWriter:
            self.dataWriter.run = False
            self.dataWriter = None
        print("done shutting down")

class StartHandler(tornado.web.RequestHandler):
    def initialize(self, rec):
        self.rec = rec

    def get(self):
        self.rec.start()
        self.write("started receiver")

class StartHdf5Handler(tornado.web.RequestHandler):
    def initialize(self, rec):
        self.rec = rec

    def get(self):
        self.rec.startHdf5Disk()
        self.write("started hdf5 writing to disk.")

class StopHandler(tornado.web.RequestHandler):
    def initialize(self, rec):
        self.rec = rec

    def get(self):
        self.rec.stop()
        self.write("stopped receiver")

class PacketHandler(tornado.web.RequestHandler):
    def initialize(self, rec):
        self.rec = rec

    def get(self):
        ts, ports, data = self.rec.read_data()
        print(ts)
        print(ports)
        print(data)
        self.write(dict(ts=ts.tolist(), ports=ports, data=data.tolist()))

class GainHandler(tornado.web.RequestHandler):
    def initialize(self, rec):
        self.rec = rec

    def get(self):
        gains = self.rec.estimateGains()
        self.write(dict( gains=gains))

class Application(tornado.web.Application):
    def __init__(self, rec):
        handlers = [
            (r"/start/?", StartHandler, dict(rec=rec)),
            (r"/start_hdf5/?", StartHdf5Handler, dict(rec=rec)),
            (r"/stop/?", StopHandler, dict(rec=rec)),
            (r"/get_packets/?", PacketHandler, dict(rec=rec)),
            (r"/estimate_gain/?", GainHandler, dict(rec=rec))
        ]
        tornado.web.Application.__init__(self, handlers)

def main():
    rec = Receiver()
    app = Application(rec)
    app.listen(33221)
    print("starting up!, tell me to do something!")
    IOLoop.instance().start()

if __name__ == '__main__':
    main()
