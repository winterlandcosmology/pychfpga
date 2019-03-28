#!/usr/bin/python

import pychfpga.fpga_array as FA 
import socket
import time
import argparse
import numpy as np
import sys
import os
import socket
import struct


def save_frames(Nframe_pairs=10, cadence=1.00, setup='interhut', filename='/home/chime/ch_acq/frame_pair_test.npz'):
    #create array
    print 'CREATING FPGA ARRAY'
    board_sn_to_ip = {'0343':'10.10.10.243', '0338':'10.10.10.244', '0336':'10.10.10.245'}

    sma_to_adc = [12, 13, 14, 15, 8, 9, 10, 11, 4, 5, 6, 7, 0, 1, 2, 3]

    if setup == 'mcgill_dist':
        channels = {'0343': [0, 8]}
        clk = {'0343':True}

    elif setup == 'mcgill_dist_board':
        channels = {'0343': [0], '0338': [0]}
        clk = {'0343':True, '0338': False}

    elif setup == 'mcgill_dist_board_bp':
        channels = {'0343': [0], '0336': [0]}
        clk = {'0343':True, '0336': False}

    else:
        ValueError("Do not recognize setup %s" % setup)

    board_sn = sorted(channels.keys())
    ips = list(set([board_sn_to_ip[sn] for sn in board_sn]))

    ca = FA.FPGAArray(iceboards=ips, open = 1, prog = 1, ignore_missing_boards=True)
    #configure array
    print 'CONFIGURING BOARDS'
    
    for iceboard in ca.ib:
        print iceboard.serial
        if clk[iceboard.serial]:
            for mezz in iceboard.mezzanine:
                mezz.set_refclk_source('sma')

    ca.set_sync_method()
    
    for brd in ca.ib:
        print brd.serial, brd.get_irigb_time()
    
    ca.sync()
    ca.ib.set_adc_mode('data')
    ca.ib.set_fft_bypass(True)
    #Get board info
    base_port=42100
    ib_port_id = [0, 4]

    ib = [iceboard for iceboard in ca.ib]
    ib_id = []
    for iceboard in ib:
        for chan in channels[iceboard.serial]:
            ib_id.append( "%s-%d" % (iceboard.serial, chan))

    print 'STARTING DATA TRANSMISSION'
    for i, iceboard in enumerate(ib):
        iceboard.set_local_data_port_number(base_port+ib_port_id[i])
        iceboard.start_data_capture(burst_period_in_seconds=cadence, source='adc', frames_per_burst=2, channels=channels[iceboard.serial])

    time.sleep(5)

    print 'SYNCING BOARDS'
    ca.sync()
    time.sleep(5)

    print 'CREATING DATA RECEIVERS'
    conf = ib[0].get_config()
    host_ip = conf.system_interface_ip_address
    rec = []
    for i, iceboard in enumerate(ib):
        rec.append(raw_adc_receiver(host_ip, base_port+ib_port_id[i]))

    print 'STARTING FRAME CAPTURE'
    data = {ib_id[0]: np.zeros((2*Nframe_pairs, 2048), dtype=np.int8)*np.nan,
            ib_id[1]: np.zeros((2*Nframe_pairs, 2048), dtype=np.int8)*np.nan,
            'timestamp': np.zeros(2*Nframe_pairs, dtype=np.uint64)*np.nan}

    for pair in range(Nframe_pairs):
        # Get pair
        d = [r.get_frame_pair(channel=chan) for i, r in enumerate(rec) for chan in channels[ib[i].serial]]
        # Compare timestamps of the two boards
        if d[0][1][0]==d[1][1][0]:
            # Saving frames
            data[ib_id[0]][2*pair:2*(pair+1)] = d[0][0]
            data[ib_id[1]][2*pair:2*(pair+1)] = d[1][0]
            data['timestamp'][2*pair:2*(pair+1)] = d[0][1] 
            print 'SAVED FRAME PAIR {0}'.format(pair)
        else: 
            print 'timestamp for {0}: {1}'.format(ib_id[0], d[0][1][0])
            print 'timestamp for {0}: {1}'.format(ib_id[1], d[1][1][0])            
            raise('THE TIMESTAMP FOR THE TWO BOARDS ARE DIFFERENT')
    print 'SAVING DATA TO {0}'.format(filename)
    np.savez(filename, **data)
    print 'DONE'


class raw_adc_receiver(object):
    """raw adc receiver class
    """

    def __init__(self, host_ip, port_number):
        """ Create socket object. ib_config is the iceboard configuration as obtained from ib.get_config()
        """
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((host_ip, port_number))
        self.sock.settimeout(2)
        self.host_ip = host_ip
        self.port_number = port_number

    def get_frame_pair(self, n_attemps=6, verbose=True, channel=0):
        """ Get 2 consecutive raw adc frames from a single input. n_attempts is the number of attempts to get two consecutive frames
        """
        self.frame = np.zeros((2, 2048), dtype=np.int8)*np.nan
        self.frame_timestamp = np.zeros(2, dtype=np.uint64)
        self.n_attemps = n_attemps
        self.channel = channel
        attempts = 0
        self.frame_ready = False
        self.incomplete_frame = False
        while attempts < self.n_attemps: # Stays here unless frame ready or oncomplete frame flag
            data = self.sock.recv(2099)
            (probe_id, stream_id, word_length, timestamp) = struct.unpack_from('>BHHL', data)
            ant_channel = probe_id & 0x0F
            adc_data = np.fromstring(data[9:9+2048], dtype=np.int8)
            if (ant_channel==self.channel) and (timestamp==self.frame_timestamp[0]+1): # Got two consecutive frames. Done!
                self.frame_timestamp[1] = timestamp
                self.frame[1] = adc_data
                break
            else:
                self.frame_timestamp[0] = timestamp
                self.frame[0] = adc_data
                attempts += 1
        if (attempts >= self.n_attemps) and verbose:
            print 'Incomplete frame'
        return self.frame, self.frame_timestamp

    def close(self):
        """ close socket
        """
        self.sock.close()

    def flush(self):
        """ flush socket
        """
        while True:
            try:
                data = self.sock.recv(2099)
            except:
                break

if __name__ == '__main__':

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    
    parser.add_argument('-n','--nframe',  help='Number of frames to collect.', type=int, default=2000)
    parser.add_argument('-t','--time',  help='Cadence in seconds.', type=float, default=0.01)
    parser.add_argument('-s', '--setup', help='Describes a setup to read data out.', 
                                         type=str, default='mcgill_dist_board')
    parser.add_argument('-p', '--prefix', help='Prefix inserted to the start of the filename, used to distinguish different measurements.', 
                                        type=str, default='frame_pairs')

    args = parser.parse_args()

    filename = os.path.join('/home/chime/jfliche/timing/', '%s_nframe_%d_cadence_%dmsec_%s.npz' % (args.prefix, args.nframe, 
                                                                                            int(np.round(1000 * args.time)), args.setup))

    save_frames(Nframe_pairs=args.nframe, cadence=args.time, setup=args.setup, filename=filename)
