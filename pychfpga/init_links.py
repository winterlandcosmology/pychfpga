import numpy as np
import struct
import time
import logging
#import core.icecore.icebox


class GpuData(object):
    def __repr__(self):
        return '\n'.join(['%10s = %r' % (name, value) for (name, value) in vars(self).items() if not name.startswith('_') and not name=='data'])

def get_gpu_data(node_number, dna_number):
    from subprocess import Popen, PIPE
    p = Popen(['sudo','chi-exec','%i' % node_number, '/root/inspect_pkt_dna_select', 'dna%i' % dna_number], stdout=PIPE)
    (data, stderr) = p.communicate()
    split_data = data.split('\n')
    d=[]
    for line in split_data[2:]:
        if line.startswith('Packet'):
            break
        split_line = line.lstrip().split(' ')
        # print split_line
        d += [int(c,16) for c in split_line[2:2+min(len(split_line)-2, 16)] if c]
    result=GpuData()
    result.ethernet_packet_size = len(d)
    result.mac_dst = ':'.join(['%02X' % c for c in d[0:6]])
    result.mac_src = ':'.join(['%02X' % c for c in d[6:12]])
    result.ethertype = '%04X' % (d[12]*256 + d[13])
    result.ip_length = d[16]*256 + d[17]
    result.ip_protocol = d[23]
    result.ip_src = d[26:30]
    result.ip_dst = d[30:34]
    result.udp_src_port = d[34]*256 + d[35]
    result.udp_dst_port = d[36]*256 + d[37]
    result.udp_length = d[38]*256 + d[39] # includes 8 bytes of the UDP header
    result.udp_payload_length = result.udp_length-8

    d = d[42:42+result.udp_payload_length]

    header = ''.join(chr(x) for x in d[0:16])
    (result.cookie, __, result.stream_id, __, __, result.timestamp) = struct.unpack('<BBHLLL',header)
    result.source_lane_number = result.stream_id & 0x00F

    result.data = d[16:]

    print 'UDP Payload = %i bytes, Ethernet packet=%i bytes' % (result.udp_payload_length, result.ethernet_packet_size)

    return result

#def shuffle_init(c, sync_board, frames_per_packet=1, cb1_lanes=4, cb1_bins=16, cb2_lanes=2, cb2_bins=1, cb2_bypass=0, bp_bypass=0, remap=True):
def shuffle_init(c, ni_board, sync_board, dsmap=range(16), frames_per_packet=1, cb1_lanes=4, cb1_bins=16, cb2_lanes=2, cb2_bins=1, cb2_bypass=0, bp_bypass=0, remap=True,
                 ni_enable = False, ni_offset = 0, ni_high_time = 8388608, ni_period = 16777216):
    """ Setup the crossbars and data shuffling in every board of the array.
    """
    tx_list = []
    logger = logging.getLogger(__name__)

    crate_set = set([cc.crate for cc in c])
    if len(crate_set) != 1:
        raise RuntimeError('All boards must be in the same crate. The provided set of Iceboards have the following crates: %r' % crate_set)
    crate = crate_set.pop()

    logger.info('%.32r: Configuring crate-wide data shuffling with frames_per_packet=%i, cb1_lanes=%i, cb1_bins=64, cb2_lanes=%i, cb2_bins=%i, cb2_bypass=%s, bp_bypass=%s' % (crate, cb1_lanes, cb1_bins, cb2_lanes, cb2_bins, bool(cb2_bypass), bool(bp_bypass)))

    # Set SMA output of sync board to be irigb trigger sync signal (was 'sync')
    sync_board.set_user_output_source('irigb_trig')
    # set-up transmitters
    for i,bb in enumerate(c):
        logger.info('%.32r: **** Initializing transmitters for Slot %02i (IceBoard SN%s) ****' % (crate, bb.slot, bb.serial))
        bb.set_corr_reset(0)
        # bb.set_data_source('funcgen')
        # bb.set_funcgen_function('a', a=0)
        #bb.set_data_source('adc')
        # set all analog inputs to send the (slot_number, analog input) complex number on every bin
        #for j in range(len(bb.ANT)):
        #    bb.set_funcgen_function('ab', a=(bb.slot-1)<<4, b=j<<4, channels=[j])
            # bb.set_funcgen_function('4bit_split_ramp')
        #    pass
        # set the stream ID of every transmitter to (slot_number, analog input) complex number on every bin
        for j,cb in enumerate(bb.CROSSBAR):
            cb.STREAM_ID = dsmap[bb.slot-1]

        for j,cb in enumerate(bb.CROSSBAR2):
            cb.STREAM_ID = dsmap[bb.slot-1]

        # Set the source of the IRIG-B signal
        bb.set_irigb_source('bp_time')
        # Set the source of the SYNC signal to irigb
        bb.REFCLK.set_sync_source('bp')#bb.REFCLK.SLAVE=1
        #bb.REFCLK.set_sync_source('irigb')

        tx_list.append((bb.slot, 0))  # Register Bypass lane (lane 0) as a transmitter in this slot
        for j,gtx in enumerate(bb.BP_SHUFFLE.gtx):
            tx_list.append((bb.slot, j+1))

        if remap:
            bb.CROSSBAR2.set_lane_map(compute_lane_map(bb))

        # Initialize the crossbars to select and send data in a specific format
        bb.init_crossbars(dsmap, frames_per_packet=frames_per_packet, cb1_lanes=cb1_lanes, cb1_bins=cb1_bins, cb2_lanes=cb2_lanes, cb2_bins=cb2_bins, cb2_bypass=cb2_bypass, remap=remap, bp_bypass=bp_bypass)

    # set-up receivers
    for i, bb in enumerate(c):
        # Disable all receivers for which there are no transmitters
        for i, gtx in enumerate(bb.BP_SHUFFLE.gtx):
            rx = (bb.slot, i+1)
            tx = bb.bp.get_matching_tx(rx)
            if tx in tx_list:
                gtx.USER_GTRXRESET = 0
            else:
                gtx.USER_GTRXRESET = 1
                # gtx.USER_RESET = 1

        bb.CROSSBAR2.SOF_WINDOW_STOP = 200
        bb.BP_SHUFFLE.reset_rx_equalizers()
        bb.REFCLK.sync() # needed

 # set-up receivers
    for bb in c:
        for i in range(bb.NUMBER_OF_CROSSBAR_OUTPUTS):
            rx = (bb.slot, i)
            tx = bb.bp.get_matching_tx(rx)
            if tx in tx_list:
                logger.info('%.32r: %s is receiving from %s' % (bb.crate, rx, tx))
            else:
                logger.info('%.32r: %s has no corresponding transmitter' % (bb.crate, rx,))

    # Configure noise injection gating signal
    if ni_enable:
        ni_board.set_user_output_source('pwm')
        ni_board.set_frame_pwm(ni_offset, ni_high_time, ni_period)
        
    # sync boards
    #soft_sync(c, sync_board)
    #irigb_sync(c, delay=5)
    time_soft_sync(c, sync_board, delay=5)


# r.CROSSBAR2[0].print_frame_info()
def compute_lane_map(c):
    lane_map = np.zeros(16, dtype=np.int8)
    for i in range(16):
        rx = (c.slot, i)
        tx = c.bp.get_matching_tx(rx)
        print '%s is receiving from %s' % (rx, tx)
        lane_map[tx[0]-1] = i
    return lane_map

def test_sync(c, sync_board):
    sync_ctr = np.zeros(len(list(c)), dtype=int)
    for i,bb in enumerate(c):
        bb.REFCLK.set_sync_source('bp')#bb.REFCLK.SLAVE=1
        sync_ctr[i] = bb.REFCLK.SYNC_CTR

    fail=0
    for test_number in range(10):
        print 'Trial # %i: Sending SYNC pulse from Slot %02i (Iceboard SN%s)' % (test_number+1, sync_board.slot, sync_board.serial)
        sync_board.REFCLK.sync()
        for i,bb in enumerate(c):
            new_sync_ctr = bb.REFCLK.SYNC_CTR
            diff = (new_sync_ctr - sync_ctr[i]) & 0xF
            sync_ctr[i] = bb.REFCLK.SYNC_CTR
            fail += bool(diff!=1)
            print '    Slot %02i (Iceboard SN%s): Sync counter = %2i, diff = %2i => %s' % (bb.slot, bb.serial, new_sync_ctr, diff, ('FAILED!', 'PASS')[bool(diff==1)])
        time.sleep(0.2)
    if fail:
        print 'SYNC Test has FAILED!'
    else:
        print 'SYNC Test has PASSED!'

def check_gpu_data(nodes):
    if isinstance(nodes,int):
        nodes=[nodes]
    for node in nodes:
        for port in range(8):
            errors=np.sum( np.array(get_gpu_data(node,port).data[:256])!=np.arange(256))
            print 'GPU Node %2i port %2i has %i error(s)' % (node, port, errors)
# crx=b[0]
# cb1=crx.CROSSBAR
# cb2=crx.CROSSBAR2
# bp=crx.BP_SHUFFLE
# rx1=bp.gtx[0]
# rx2=bp.gtx[1]
# rx3=bp.gtx[2]
# gpu=crx.GPU
# bs2=cb2[0]
def print_frame_info(self):
        bs = self
        ts=[]
        sid=[]

        # get 8 bits of stream ID
        self.HEADER_CAPTURE_DATA_SEL=0
        self.HEADER_CAPTURE_EN=1
        self.HEADER_CAPTURE_EN=0
        for i in range(16):
            self.HEADER_CAPTURE_LANE_SEL=i
            sid.append(self.HEADER_CAPTURE_DATA)

        # get lsb of timestamp
        self.HEADER_CAPTURE_DATA_SEL=1
        self.HEADER_CAPTURE_EN=1
        self.HEADER_CAPTURE_EN=0
        for i in range(16):
            self.HEADER_CAPTURE_LANE_SEL=i
            ts.append(self.HEADER_CAPTURE_DATA)

        for i in range(len(ts)):
            print 'Lane %02i: Stream ID=0x%02x, Frame = 0x%02x (delta = %i)' % (i, sid[i], ts[i], ts[i]-ts[0])

def reopen(boards, bitstream):
    for ib in boards:
        if ib.is_open():
            print 'IceBoard SN%i (Slot #%s) is already opened' % (ib.serial, ib.slot)
        else:
            while not ib.is_open():
                print 'Reprogramming FPGA on IceBoard SN%s ' % (ib.serial)
                try:
                    ib.set_fpga_firmware(bitstream, force=1)
                    ib.open()
                    print 'IceBoard SN%s (Slot #%i) is now opened' % (ib.serial, ib.slot)
                    break
                except:
                    print 'Failed to open IceBoard SN%s. Retrying' % (ib.serial)

def init_gains(c):
    import pickle
    for cc in c:
        try:
            g_array = pickle.load(open('/home/chime/ch_acq/gains_'+str(cc.GPIO.FPGA_SERIAL_NUMBER)+'.pkl', 'rb'))
        except:
            g_array = pickle.load(open('/home/chime/ch_acq/gains.pkl', 'rb'))
            print 'Could not find gain settings for %r, sn %i. Using default gain settings.' % (cc, cc.get_fpga_serial_number())
        print 'Setting gains on IceBoard SN%s' % cc.serial
        cc.set_gain(g_array)

def soft_sync(boards, sync_board):
    """ Synchronize all boards"""
    boards = list(boards)
    print 'Masking ADC data before sync'
    for ib in boards:
        for ant in ib.ANT:
            ant.ADCDAQ.BYTE_MASK = 0

    print 'Initiating global sync'
    sync_board.REFCLK.sync()

    print 'Unmasking ADC data'
    for ib in boards:
        for ant in ib.ANT:
            ant.ADCDAQ.BYTE_MASK = 255

def time_soft_sync(boards, sync_board, delay):
    """ Synchronize all boards"""
    boards = list(boards)
    print 'Masking ADC data before sync'
    for ib in boards:
        for ant in ib.ANT:
            ant.ADCDAQ.BYTE_MASK = 0
    
    # Get current time
    current_time = sync_board.get_irigb_time()
    print 'Setting IRIG-B sync after %d seconds' %delay
    # Send sync pulse delay seconds in the future
    sync_board.set_irigb_trigger_time(current_time, delay)

    print 'Unmasking ADC data'
    for ib in boards:
        for ant in ib.ANT:
            ant.ADCDAQ.BYTE_MASK = 255

def irigb_sync(boards, delay):
    """ Synchronize all boards"""

    # Get current time
    current_time = boards[0].get_irigb_time()
    print 'Setting IRIG-B sync after %d seconds' %delay
    for cc in boards:
        cc.set_irigb_trigger_time(current_time, delay)


def print_temperatures(boards):
    t = [(b.slot, b.serial, b.SYSMON.temperature()) for b in boards]
    t.sort()
    for (slot, serial_number, fpga_temp) in t:
        print 'Slot %2i (SN%s): FPGA %2.1f C' % (slot, serial_number, fpga_temp)

def set_adc_mask(boards, value):

    boards = list(boards)
    for ib in boards:
        for ant in ib.ANT:
            ant.ADCDAQ.BYTE_MASK = value

def print_fmc_power(boards):
    sensor_list = ['FMCA_12V0', 'FMCA_3V3','FMCA_VADJ','FMCB_12V0','FMCB_3V3','FMCB_VADJ']
    for b in boards:
        for sensor in sensor_list:
            (voltage, current, power) = b.hw.get_power(sensor)[sensor]
            if voltage is not None:
                print '%0.1fV@%0.2fA=%0.1fW ' % (voltage, current, power),
            else:
                print 'None                 ',
        print
