import time
import numpy as np
import matplotlib.pyplot as plt




def get_ber(array, link_list, period=0.1, tx_power = None):

    link_list.sort(key=lambda ((ss,sl),(ds,dl)): ss*16+ds)
    ib_map = {ib.slot:ib for ib in array}
    ber_table={}
    for ((ss,sl),(ds,dl)) in link_list:
        if ss not in ib_map or ds not in ib_map:
            continue
        source_ib = ib_map[ss]
        dest_ib = ib_map[ds]
        if not source_ib.is_open() or not dest_ib.is_open():
            continue
        source_gtx = source_ib.BP_SHUFFLE.gtx[sl-1]
        dest_gtx = dest_ib.BP_SHUFFLE.gtx[dl-1]

        if tx_power is not None:
            source_gtx.TXDIFFCTRL=tx_power

        source_gtx.TXPRBSSEL=4

        print 'Measuring BER for Slots %2i->%2i (SN%s, GTX[%2i])=> (SN%s, GTX[%2i])' % (ss, ds, source_ib.serial, sl-1,  dest_ib.serial, dl-1),

        # First, make sure we can get errors by setting the wrong RX PRBS Sequence
        dest_gtx.RXPRBSCNTRESET=1
        dest_gtx.RXPRBSSEL=3
        dest_gtx.RXPRBSCNTRESET=0
        t0=time.time()
        while True:
            if dest_gtx.ERR_CTR:
                break
            if time.time()-t0 < 1:
                raise SystemError('Cannot detect errors even with the wrong sequence!')

        #dest_gtx.RXPRBSCNTRESET=1
        #dest_gtx.RXPRBSCNTRESET=0
        #dest_gtx.RXPRBSCNTRESET=1
        #dest_gtx.RXPRBSCNTRESET=0
        #t0=time.time()
        #while time.time()-t0 < 13:
        #    print  dest_gtx.ERR_CTR, 'from', dest_gtx
        #    #dest_gtx.RXPRBSCNTRESET=1
        #    #dest_gtx.RXPRBSCNTRESET=0
        #    time.sleep(0.5)
        #    #if not dest_gtx.ERR_CTR:
        #    #    print 'locked',
        #    #    break
        #dest_gtx.RXPRBSCNTRESET=1
        dest_gtx.RXDFELPMRESET=1
        time.sleep(0.001)
        dest_gtx.RXDFELPMRESET=0
        time.sleep(0.001)
        dest_gtx.RXPRBSCNTRESET=1
        dest_gtx.RXPRBSSEL=4
        dest_gtx.RXDFELPMRESET=1
        time.sleep(0.001)
        dest_gtx.RXDFELPMRESET=0
        time.sleep(0.001)
        dest_gtx.RXPRBSCNTRESET=0
        time.sleep(period)
        cnt=dest_gtx.ERR_CTR
        err=(float(cnt)*16)/(period*10e9)
        err_max=(float(cnt)*16+1)/(period*10e9)

        print 'BER = %1.1e (%i errors, BER<%1.1e)' % (err, cnt, err_max)
        ber_table[(ss,ds)]=err
    return ber_table


def get_ber_vs_power(array, max_power, period=0.1):


    links = scan_links(array, tx_power = max_power)
    power = range(0,max_power+1)
    data = {}
    for tx_power in power:
        e = get_ber(array, links, period=period, tx_power = tx_power)
        for (link, ber) in e.items():
            if link in data:
                data[link][0].append(tx_power)
                data[link][1].append(ber)
            else:
                data[link] = [[tx_power], [ber]]
    return data

def plot_ber_vs_power(data):
    for (ss,ds),(tx_power, ber) in data.items(): print '%10s'% ((ss,ds),), ','.join(['%6.1g' % b for b in ber])
    plt.figure(1)
    plt.clf()

    for (ss,ds),(tx_power, ber) in data.items():
        plt.plot(tx_power, np.log10(np.array(ber)+1e-12), label='Slot %i=>%i' % (ss,ds))
    plt.legend()

def is_locked(rx, count=1000):
    return all([rx.BLOCK_LOCK for x in range(count)])

def reverse_bits(x):
    x = ((x & 0x5555555555555555) << 1) | ((x & 0xAAAAAAAAAAAAAAAA) >> 1)
    x = ((x & 0x3333333333333333) << 2) | ((x & 0xCCCCCCCCCCCCCCCC) >> 2)
    x = ((x & 0x0F0F0F0F0F0F0F0F) << 4) | ((x & 0xF0F0F0F0F0F0F0F0) >> 4)
    return x

def reverse_bytes(x):
    x = ((x & 0x00FF00FF00FF00FF) << 8) | ((x & 0xFF00FF00FF00FF00) >> 8)
    x = ((x & 0x0000FFFF0000FFFF) << 16) | ((x & 0xFFFF0000FFFF0000) >> 16)
    #x = ((x & 0x00000000FFFFFFFF) << 32) | ((x & 0xFFFFFFFF00000000) >> 32)
    return x

VALID_WORDS = [
"78555555", "555555D5",
"90E2BA2F", "F88090E2",
"BA2FF880", "08004500",
"0034013F", "00000111",
"D37B0202", "0201E000",
"00FCC313", "14EB0020",
"70AC73D4", "00000001",
"00000000", "00000669",
"73617461", "70000001",
"E10001B8", "5E5E0000",
"1E000000", "00000000"
]

def scramble(data):
    scrambler = 0b0101010101010101010101010101010101010101010101010101010101
    result=[]
    for d in data:
        poly = scrambler
        dout=0
        for i in range(32):
            xorBit = bool (d & (1<<i)) ^ bool(poly & (1<<38)) ^ bool(poly & (1<<57))
            poly = ((poly <<1) | xorBit) & ((1<<58)-1)
            dout |= xorBit << i
        scrambler = poly
        result.append(dout)
    return result

def serialize(data):
    stream=[]
    for d in data:
        for i in range(32):
            stream.append(bool(d&(1<<i)))
    return stream

def plot_power_vs_lane_separation(data):

    power=[]
    slot_sep=[]
    for (ss,ds),(tx_power, ber) in data.items():
        p = np.where(np.array(ber)!=0)[0]

        if len(p):
            pp=max(p)+1
        else:
            pp=0
        #print

        print '%10s' % ((ss,ds),), pp, ber
        power.append(pp)
        slot_sep.append(abs(ds-ss))
    plt.clf()
    plt.plot(slot_sep, power, '.')

def packet_length(frames_per_packet=4, number_of_lanes=4, number_of_selected_bins=8):
    data_per_frame = number_of_lanes * number_of_selected_bins
    data_flags_per_frame = number_of_selected_bins
