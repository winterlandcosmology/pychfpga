import os
import sys
import getopt
import logging
import time
import pickle
import gc
from memory_profiler import profile



from datetime import datetime
import numpy as np
import h5py

import iceboard_receiver as ir

from correlator_ana import *

def build_logger(log_level="INFO"):

    '''
    Set up the logger. Returns a logger handle.
    '''

    reload(logging)
    # create logger
    logger = logging.getLogger(__name__) # pylint: disable=locally-disabled, invalid-name

    logger.setLevel(log_level)
    # create console handler and set level to debug
    console = logging.StreamHandler()
    console.setLevel(log_level)
    # create formatter

    #formatter = logging.Formatter("%(asctime)s - %(funcName)s - %(levelname)s - %(message)s")
    formatter = logging.Formatter("%(asctime)s - %(funcName)s - %(levelname)s - %(message)s")
    # add formatter to ch
    console.setFormatter(formatter)
    # add ch to logger
    logger.addHandler(console)       

    return logger




def read_correlator_frame(verbose):
    '''
    read_correlator_frame(verbose)
    Returns a correlator frame.
    '''

    return  ir.read_correlator_frame(verbose=verbose)


def write_to_disk(handler, index, data):
    '''
    write_to_disk(handler, index, data)
    Write a data tuple to an h5 file.
    '''
    handler[index] = data

#@profile
def acquire_data(output_filename, number_frames, integration_period, gains_file, logger, verbose=0):

    '''
    acquire_data(output_filename, number_frames, integration_period, gains_file, logger, verbose=0)

    Acquire correlator data and write it to disk in an h5 file. 
    '''


    n_corr = 16 * 17 / 2

    output_file = h5py.File(output_filename, 'w')    
    output_file.attrs['process_date_start'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
    output_file.attrs['number_frames'] = number_frames
    output_file.attrs['digital_gains_present'] = False
    output_file.attrs['integration_period'] = integration_period

    comp_type = np.dtype([('computer_timestamp', '|S30'), 
                          ('products', 'complex64', (n_corr, 1024)), 
                          ('frame_number', 'uint32'), 
                         ])
    #chunk_size = np.min([100, number_frames])
    output_handler = output_file.create_dataset("correlations", 
                                                (number_frames,), 
                                                dtype=comp_type, 
                                                maxshape=(number_frames,), 
                                                #chunks=(chunk_size,),
                                                chunks=True,
                                                #compression="lzf",
                                               ) 
                                                #compression_opts=9)
    indices = np.triu_indices(16)

    last_time = time.time()
    for i in xrange(number_frames):       

        
        timestamp, data = read_correlator_frame(verbose)
        
        write_to_disk(output_handler, i, ("{0:30s}".format(datetime.now().strftime("%H:%M:%S.%f")),
                                          data[indices],
                                          timestamp
                                         )
                     )
        
        #del timestamp, data
        now_time = time.time()
        if now_time - last_time >= 5.0:
            last_time = now_time
            logger.info("At measurement {0:d}/{1:d}".format(i, number_frames))
            logger.debug("Most recent timestamp: {0:d}".format(timestamp))
        


    output_file.attrs['process_date_stop'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")       
    
    if gains_file is not None:
        output_file.attrs['digital_gains_present'] = True
        logger.info("Adding digital gains: {0:s}".format(gains_file))
        add_digital_gains(output_file, gains_file)

    logger.info("Done.")
    output_file.close()
    garbage = gc.collect()

    logger.debug("Amount of garbage collected: {0:d}".format(garbage))

    return output_filename


def load_digital_gains(gains_file):
    '''
    Load and linearize a digital gains file.
    '''

    gains = pickle.load(open(gains_file, "r"))
    linear_gain = np.empty((16, 1024))
    for (i, gain) in enumerate(gains[0:16]):
        linear_gain[i] = 2**gain[1][1] * abs(np.array(gain[1][0]))
        
    return linear_gain

def add_digital_gains(output_file_handler, gains_file):
    '''
    Add_digital_gains(output_file_handler, gains_file)
    Write the digital gains corresponding to a data file to the h5 dataset. 
    Also creates an attribute with the gains file's original location as redundancy.
    '''

    output_file_handler.create_dataset("digital_gains", data=load_digital_gains(gains_file))
    output_file_handler.attrs['digital_gains_file'] = gains_file

def test_daq_receive_deltas(N = 100, verbose = 0):
    '''
    test_daq_receive_deltas(N = 100, verbose = 0)

    Look at the incremental increase in timestamp numbers in both regular and 'burst' mode.

    '''

    timestamps = np.zeros(N, dtype=np.uint)
    deltas = np.zeros(len(timestamps) -1)

    for i in xrange(N):
        if i%10 == 0:
            print datetime.now().strftime("%H:%M:%S.%f"), i, N
        timestamps[i] = ir.read_correlator_frame(verbose=verbose)[0]

    for i in xrange(1, N):
        deltas[i-1] = timestamps[i] - timestamps[i-1]
        

    timestamps2, _ = ir.capture_correlator_burst(N)
    deltas2 = np.zeros(len(timestamps) -1)

    for i in xrange(1, N):
        deltas2[i-1] = timestamps2[i] - timestamps2[i-1]
        


    return timestamps, deltas, timestamps2, deltas2


def run_daq(data_dir, filename_prefix, number_accumulations, number_files, integration_period, logger=None, gains_file=None, verbose=0, log_level="INFO"):

    '''
    Start the main DAQ loop. This function can be called from main() or on its own.
    '''

    if logger is None:
        logger = build_logger(log_level)    
    
    data_file_length_s = number_accumulations * integration_period * 2.56e-6
    print data_file_length_s
    print number_files
    logger.info("Expected execution time: {0:.1f} s / file ({2:.1f} min.) ; {1:.1f} s ({3:.1f} min.) total."\
        .format(data_file_length_s,
                number_files * data_file_length_s,            
                data_file_length_s/60.,
                number_files * data_file_length_s / 60.))


    for i in xrange(number_files):
        logger.info("Beginning acuisition {0:d}/{1:d}".format(i+1, number_files))
        file_number = ".{0:04d}".format(i)
        output_filename = (data_dir + time.strftime("d%Y%m%d") + "/" + 
                           filename_prefix + file_number + ".h5")
        logger.info("Output filename: {0:s}".format(output_filename))

        if not os.path.exists(os.path.dirname(output_filename)):
            os.makedirs(os.path.dirname(output_filename))
            
        if os.path.isfile(output_filename):
            logger.error("Target file '{0:s}' exists".format(output_filename))
            if filename_prefix[0:4] == 'test':
                logger.info("Overwriting...")
                os.remove(output_filename)
                logger.info("Deleted '{0:s}'".format(output_filename))    
            else:
                logger.error("Remove file or choose a new input filename to continue.")
                sys.exit(2)
        
        acquire_data(output_filename, number_accumulations, integration_period, gains_file, 
                     verbose=verbose, logger=logger)
        #acquire_data_alternate(output_filename, number_accumulations, integration_period, gains_file, 
        #             verbose=verbose, logger=logger)
        logger.info("----")



def main(argv):

    logger = None
    log_level = "INFO"
    number_files = 1
    number_accumulations = 100
    verbose = 0
    filename_prefix = None
    gains_file = None
    integration_period = None
    data_dir = "/data/firmware_correlator/data/"


    if logger is None:
        logger = build_logger()     


    try:
        opts, _ = getopt.getopt(argv, 
                                "h:o:g:N:L:P:v:D", 
                                ["ofile=", "digital_gains=", "datadir="])
    except getopt.GetoptError:
        print 'correlator_daq.py -o <outputfilename> -N <number files> -L <number integrations> -v <verbosity> -g <digital gains file>'
        sys.exit(2)
    for opt, arg in opts:
        if opt == '-h':
            print 'correlator_daq.py -o <outputfilename> -N <number files> -L <number integrations> -v <verbosity> -g <digital gains file>'
            sys.exit()
        elif opt in ("-o", "--ofile"):
            filename_prefix = arg
        elif opt in ("-g", "--digital_gains"):
            gains_file = arg            
        elif opt == '-L':
            number_accumulations = int(arg)
        elif opt == '-N':
            number_files = int(arg)            
        elif opt == '-P':
            integration_period = int(arg)                        
        elif opt == '-v':
            verbose = int(arg)
        elif opt in ("-D", "--datadir"):
            data_dir = arg        


    if filename_prefix is None:
        logger.error("No specified filename prefix! Exiting...")
        sys.exit(2)
    elif integration_period is None:
        logger.error("No specified integration period! Exiting...")
        sys.exit(2)
    else:
        #run_daq(filename_prefix, number_accumulations, number_files, integration_period, data_dir, logger=None, gains_file=None, verbose=0, log_level="INFO"):
        #def run_daq(data_dir, filename_prefix, number_accumulations, number_files, integration_period, logger=None, gains_file=None, verbose=0, log_level="INFO"):
        run_daq(data_dir, filename_prefix, number_accumulations, number_files,
                integration_period=integration_period, logger=logger, 
                gains_file=gains_file, verbose=verbose, log_level=log_level)

if __name__ == '__main__':
    
    main(sys.argv[1:])
