"""
Set of functions used in iceboard qc script to program and test the FPGA.
Based off of 'top_test.py'
"""

def get_boards(boards=None, ch_acq_path = '../../../../../../ch_acq/'):
    ''' Finds specified boards on the network and returns them as IceBoardPlusHandler
    :param boards: Array of serials of the boards to be returned. If not provided, will look for 'iceboard.local'
    :param ch_acq_path: will be added to PYTHONPATH. should almost always be left to its default
    :return: Array of IceBoardPlusHandler.
    '''
    import sys
    # Append ch_acq to PATH
    sys.path.append(ch_acq_path)
    from pychfpga.core.icecore import HardwareMap
    from pychfpga.core.icecore import IceBoardPlus

    # Add specified boards to hardware map
    hwm = HardwareMap()
    if boards is None:
        hwm.add(IceBoardPlus('iceboard.local'))
    else:
        for sn in boards:
            hwm.add(IceBoardPlus('iceboard{:0>4d}.local'.format(int(sn))))
    hwm.flush()

    found_boards = []
    for ib in hwm.query(IceBoardPlus):
        pings = ib.ping()
        if pings:
            found_boards.append(ib)
    return found_boards

def programFpga(board_sn, ch_acq_path = '../../../../../../ch_acq/',  bitfile_path = "../../../../../../chFPGA/xili"
            "nx_projects/CHFPGA_MGK7MB_REV2/CHFPGA_MGK7MB_REV2.runs/impl_Rev2/chFPGA_MGK7MB_Rev2.bit", force = False):
    '''
    This method programs the FPGA of the specified board. It will force it to reprogram if it was already.
    :param board_sn: e.g. '0021'
    :param ch_acq_path: will be added to PYTHONPATH. should almost always be left to its default
    :param bitfile_path: defaults to a 'chFPGA' repo in the same directory as 'ch_acq'
    :param force: force the FPGA to reprogram
    :return: 'c' instance of programmed chFPGA_controller
    '''

    import logging
    import sys
    # Append ch_acq to PATH
    sys.path.append(ch_acq_path)
    # Import icecore dependencies
    from pychfpga.core.icecore import IceBoardPlus, HardwareMap
    from pychfpga.core.chFPGA_controller import chFPGA_controller
    from pychfpga.core.icecore_ext.fpga_bitstream import FpgaBitstream

    logger = logging.getLogger('')
    logger.handlers = []  # Clear all existing handlers
    log_handler = logging.StreamHandler()

    # Make sure SQLAlchemy does not log too much
    sql_logger = logging.getLogger('sqlalchemy.engine.base.Engine')
    sql_logger.setLevel(logging.INFO)

    # Set-up log for this test run
    logger.setLevel(logging.INFO)
    logger.addHandler(log_handler)

    # Get fpga bitstream
    fpga_bitstream = FpgaBitstream(bitfile_path)

    # Find iceboard
    while True:
        ib = get_boards(boards=[board_sn], ch_acq_path=ch_acq_path)
        if len(ib) == 0:
            if raw_input("\nDid not find board on network with serial {}."
                         "\nTry again (y/n)?    ".format(board_sn)).lower().strip() == 'y':
                continue
            else:
                raise Exception("Did not find any board on the network with serial number {}".format(board_sn))
        else:
            break
    ib = ib[0]

    # Associate the fpga bitstream with the target Handler and program
    ib.set_handler(chFPGA_controller, fpga_bitstream)
    ib.set_fpga_bitstream(force = force)

    return ib

def discover_fpgas(host_ip):
    from pychfpga.icecore.fpga_core import FpgaCoreFirmware
    FpgaCoreFirmware.interface_ip_addr = host_ip
    return FpgaCoreFirmware.discover_fpgas()

def top_test(board_sn, ch_acq_path='../../../../../../ch_acq/', init_FMC=True, host_ip=None, force=False):
    '''
    Creates fpga_controller and fpga_receiver instances and returns them as [c,r].
    :param ch_acq_path: will be added to PYTHONPATH. defaults to '../../ch_acq/'
    :param host_ip: IP address of adapter used by computer to communicate with FPGA
    :param board_sn: e.g. '0021'
    :return: list [c,r] chfpga controller and receiver, respectively
    '''
    import logging
    import sys
    # Append ch_acq to PATH
    sys.path.append(ch_acq_path)
    from pychfpga.core import chFPGA_receiver
    from pychfpga.MGADC08 import MGADC08  # Necessary for c.open() to initialize mezzanines

    ADC_DELAYS_MGK7MB_REV2_MGAC08_REV2 = (
    ([16]*8,     [3]*8), #CH0
    ([7]*8,                       [3]*8), #CH1
    ([22]*8,    [3]*8), #CH2
    ([19]*8,                       [3]*8), #CH3
    ([15]*8,                        [3]*8), #CH4
    ([14, 13, 14, 14, 13, 14, 15, 14],    [3]*8), #CH5
    ([18]*8,     [3]*8), #CH6
    ([17]*8,                       [4]*8), #CH7

    ([15, 17, 15, 18, 17, 14, 17, 15],   [3]*8), #CH8
    ([16]*8,                       [4]*8), #CH9
    ([20]*8,                       [3]*8), #CH10
    ([18]*8,                     [3]*8), #CH11
    ([15]*8,                       [3]*8), #CH12
    ([18]*8,                       [3]*8), #CH13
    ([18]*8,                       [3]*8), #CH14
    ([16]*8,                       [3]*8)  #CH15
    )

    # Parameters for FPGA open
    init = 1 if init_FMC else 0  # 'Initialization level: -1: Just create sockets, 0: connect and read only. 1: initialize hardware'
    sampling_frequency = 800  # 'Sampling frequency of the ADC in MHz'
    log_level = logging.INFO
    data_width = 8  # 'Data width of each Re and Im component of the channelizer output'
    group_frames = 1  # 'Number of frames to group before sending to the GPU or FPGA correlator.'
    enable_gpu_link = 0
    ADC_DELAY_TABLE = ADC_DELAYS_MGK7MB_REV2_MGAC08_REV2

    logger = logging.getLogger('')
    logger.handlers = []  # Clear all existing handlers
    log_handler = logging.StreamHandler()
    # Make sure SQLAlchemy does not log too much
    sql_logger = logging.getLogger('sqlalchemy.engine.base.Engine')
    sql_logger.setLevel(logging.INFO)
    # Set-up log for this test run
    logger.setLevel(logging.INFO)
    logger.addHandler(log_handler)
    # logging.basicConfig(level=log_level, format='%(asctime)s %(name)-32s %(levelname)-10s : %(message)s')
    logger.info('------------------------')
    logger.info('top_test for ICEboard QC')
    logger.info('------------------------')

    # get FPGA_controller
    c = programFpga(board_sn, ch_acq_path=ch_acq_path, force=force)
    c.discover_mezzanines()
    c.open(
        adc_delay_table=ADC_DELAY_TABLE,
        init=init,
        sampling_frequency=sampling_frequency * 1e6,
        reference_frequency=10e6,
        data_width=data_width,
        group_frames=group_frames,
        enable_gpu_link=enable_gpu_link)

    # Check if at least one FMC board present
    adc_present = c.is_mezzanine_present(1) or c.is_mezzanine_present(2)
    if not adc_present:
        logger.warning("No ADC boards are present, will not initialize a receiver.")
        r = None
        logger.warning("Returning receiver ('r') as None.")
    else:
        logger.info('Getting chFPGA configuration')
        if host_ip is not None:  # Set host computer interface
            c.interface_ip_addr = host_ip
        chFPGA_config = c.get_config()
        logger.info('Starting data/correlator receiver threads')
        r = chFPGA_receiver.chFPGA_receiver(chFPGA_config)
        c.set_local_data_port_number(r.port_number)

    return [c,r]

def rampTest(board_sn, directory, ch_acq_path='../../../../../../ch_acq/', host_ip=None):
    '''
    Run the ramp test on a single board.
    :param ch_acq_path: will be added to PYTHONPATH. defaults to '../../ch_acq/'
    :param host_ip: IP address of adapter used by computer to communicate with FPGA
    :param board_sn: e.g. '0021'
    :param directory: directory to save ramp_test results in (e.g. histogram PDFs and data)
    :return [ ADC_DELAY_TABLES, stuck_bits ]: returns results of compute_adc_delay_offsets
    '''

    # Import necessary pychfpga modules
    import sys
    import numpy as np
    sys.path.append(ch_acq_path)
    from pychfpga.common.tests.ramp_test import test_adc_ramp_histogram

    print "\nRunning top_test:"
    [c,r] = top_test(board_sn, ch_acq_path=ch_acq_path, host_ip=host_ip)

    # Timing for ADCs. Calculate proper offsets for this board.
    ADC_DELAY_TABLE, stuck_bits, bitposgood, problem = c.compute_adc_delay_offsets(channels=[0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15])
    print "Computed delay table:"
    print repr(ADC_DELAY_TABLE)
    print "Stuck bit flags (0 indicates a stuck bit):"
    print repr(bitposgood)
    #ADC_DELAY_TABLE= (
    #([16]*8,     [3]*8), #CH0
    #([7]*8,                       [3]*8), #CH1
    #([22]*8,    [3]*8), #CH2
    #([19]*8,                       [3]*8), #CH3
    #([15]*8,                        [3]*8), #CH4
    #([14, 13, 14, 14, 13, 14, 15, 14],    [3]*8), #CH5
    #([18]*8,     [3]*8), #CH6
    #([17]*8,                       [4]*8), #CH7
    #
    #([15, 17, 15, 18, 17, 14, 17, 15],   [3]*8), #CH8
    #([16]*8,                       [4]*8), #CH9
    #([20]*8,                       [3]*8), #CH10
    #([18]*8,                     [3]*8), #CH11
    #([15]*8,                       [3]*8), #CH12
    #([18]*8,                       [3]*8), #CH13
    #([18]*8,                       [3]*8), #CH14
    #([16]*8,                       [3]*8)  #CH15
    #)

    # Set ADC delays
    c.set_adc_delays(ADC_DELAY_TABLE)

    # Record serials (and other info) of mezzanines
    ipmi = [c._get_mezzanine_mcgill_ipmi(1), c._get_mezzanine_mcgill_ipmi(2)]

    # Begin Ramp test
    print "\nBegin ramp test:"
    test = test_adc_ramp_histogram(c, r)
    test.execute(directory)
    r.close()
    
    # Determine if test is pass or fail.  If fail, determine bad bits.
    ber_pass = bool(test.test_bit_error_rate.all())
    heq_pass = bool(test.test_hist_equal.all())
    hex_pass = bool(test.test_hist_expected.all())
    test_pass = ber_pass and heq_pass and hex_pass

    ber_string = None
    heq_string = None
    hex_string = None
    
    if not test_pass:
    
        nchannels = test.bit_error_rate.shape[0]
        nbits = test.bit_error_rate.shape[1]

        if not ber_pass:
            channel_matrix = np.arange(nchannels*nbits).reshape(nchannels,nbits) / nbits
            bits_matrix = np.arange(nchannels*nbits).reshape(nchannels,nbits) % nbits
        
            flag_bad = np.logical_not(test.test_bit_error_rate)
            bad_channels = channel_matrix[flag_bad]
            bad_bits = bits_matrix[flag_bad]
            bad_ber = test.bit_error_rate[flag_bad]
            nbad = len(bad_channels)
            ber_string = ' - '.join([("Channel %d, Bit %d: %0.2e" % (bad_channels[i], bad_bits[i], bad_ber[i])) for i in range(nbad)])
        
        if not heq_pass:
            heq_string = ", ".join(["%d" % cc for cc in range(nchannels)[np.logical_not(test.test_hist_equal)]])
        
        if not hex_pass:
            hex_string = ", ".join(["%d" % cc for cc in range(nchannels)[np.logical_not(test.test_hist_expected)]])
            
        
    test_results = {'status':test_pass, 'bit_error':ber_string, 'hist_equal':heq_string, 'hist_expected':hex_string}
        
    
    # Return results
    return [test_results, ADC_DELAY_TABLE, stuck_bits, ipmi]
    

def gtx_ber(board_sn, ch_acq_path='../../../../../../ch_acq/', links=None, period=1, power=None,
            bitfile_path = "../../../../../../chFPGA/xilinx_projects/CHFPGA_MGK7MB_REV2/CHFPGA_MGK7MB_REV2.runs/"+\
                           "impl_Rev2/chFPGA_MGK7MB_Rev2.bit", force=True):
    """ Measure the bit error rate on GTX links for a board.
    :param board_sn: e.g. '0021'
    :param ch_acq_path: will be added to PYTHONPATH. defaults to '../../ch_acq/'
    :param links: GTX links to test, can be either "gpu" or "bp".
    :param period: Period of time to count errors for. Default 1s.
    :param power: Set power of the GTX transmitters. Up to 15, default 10.
    :bitfile_path: Path of bitfile to use for programming the FPGA.
    :return: Dictionary of error counts, keys give lanes.
    """

    import sys
    sys.path.append(ch_acq_path)
    from pychfpga.chime_array import ChimeArray

    # Get ChimeArray object and program the FPGA
    # Enable GPU link so we can perform bit error test
    ca = ChimeArray(iceboards=[int(board_sn)], bitfile=bitfile_path, init=0, enable_gpu_link=1, open=0,
                    prog=1 if force else 0)

    # Choose links and run bit error rate test
    all_links = ca.get_link_map().keys()
    if links is None:
        print "Testing all BP and GPU links."
        links = [key for key in all_links if (key[0] == 'GPU' or key[0] == 'BP')]
    elif links == 'gpu':
        print "Testing only GPU links."
        links = [key for key in all_links if key[0] == 'GPU']
    elif links == 'bp':
        print "Testing only BackPlane links."
        links = [key for key in all_links if key[0] == 'BP']
    elif type(links) is tuple:
        links = [links]
    else:
        raise Exception("Unrecognized argument for 'links': '{}'.    Must be either 'bp' or 'gpu'.".format(str(links)))
    ber = ca.get_ber(link_list=links, tx_power=power, period=period)
    ca.ib.close()
    return ber

def write_ipmi(ib, pn, sn, vn):
    ''' Write IPMI to supplied board instance in standard format.
    :param ib: IceBoardPlusHandler to write IPMI on.
    :param pn: Part number to write to the IPMI.
    :param sn: Serial number to write to the IPMI.
    :param vn: Version number to write to the IPMI.
    :return:
    '''
    from pychfpga.core.icecore.hw import ipmi_fru
    import datetime
    import base64

    # Build IPMI object
    fru = ipmi_fru.FRU(
        board=ipmi_fru.Board(
            mfg_date=datetime.datetime.now(),
            manufacturer="Winterland",
            product_name="IceBoard",
            part_number=pn,
            serial_number=sn,
            fru_file="",
        ),
        product=ipmi_fru.Product(
            manufacturer="Winterland",
            product_name="IceBoard",
            part_number=pn,
            product_version=vn,
            serial_number=sn,
            asset_tag="",
            fru_file="",
        ),
    )
    b64_string = base64.b64encode(fru.encode())
    # Flash to board
    ib._motherboard_spi_flash_write_base64(b64_string)
    ib._motherboard_eeprom_write_base64(b64_string)
