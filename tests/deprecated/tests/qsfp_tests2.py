""" Test whether the <QSFP links come up reliably after FPGA programming

NOTES:

NOTE0: Works on linux only (tested on Ubuntu)

NOTE1: Creating FPGAArrays repetitively leaks file descriptors. 1024 fd's is
       not enough (we use 3 per FPGA object creation). Increase user limit with

          ulimit -n 102400

NOTE2: Jumbo frames must be enabled on the 10G interfaces

    sudo ifconfig ens1f0 mtu 9000
    sudo ifconfig ens1f1 mtu 9000
    sudo ifconfig ens9f0 mtu 9000
    sudo ifconfig ens9f1 mtu 9000
"""

from pychfpga import FPGAArray
from ps import AgilentN5700
import time
import psutil

INTERFACES = ('ens1f0', 'ens1f1', 'ens9f0', 'ens9f1')
POWER_SUPPLY_IP_ADDR = '10.10.10.151'
# ICEBOARD_IP_ADDR = '10.10.10.220' # slot 1/16
ICEBOARD_IP_ADDR = '10.10.10.244' # slot 2/16



def check_interface_status(iface=INTERFACES):
    s = []
    for i in iface:
        with open('/sys/class/net/%s/operstate' % i) as f:
            s.append(f.read().startswith('up'))
    print('Interface states: %s' % ', '.join('%s:%s' % (iface[i], ('down', 'up')[ss]) for i, ss in enumerate(s)))
    return s

def is_interface_receiving_data(iface=INTERFACES):

    packets1 = []
    packets2 = []
    for i in iface:
        with open('/sys/class/net/%s/statistics/rx_packets' % i) as f:
            packets1.append(int(f.read()))
    # we receive  390625 packets/seconds at 1 frame/packet
    # we wait for 100 ms. Should get about 39000 packets.
    time.sleep(0.1)
    for i in iface:
        with open('/sys/class/net/%s/statistics/rx_packets' % i) as f:
            packets2.append(int(f.read()))

    s=[p2 > p1 + 10 for p1,p2 in zip(packets1, packets2)]

    print('Interface states: %s' % ', '.join('%s:%s (%i pkts)' % (
        iface[i],
        ('no_rx', 'rx_ok')[ss],
        packets2[i] - packets1[i]) for i, ss in enumerate(s)))
    return s

def get_packet_recv(iface=INTERFACES[0], interval=0.1):


    p1 = psutil.net_io_counters(pernic=True)[iface].packets_recv
    time.sleep(interval)
    p2 = psutil.net_io_counters(pernic=True)[iface].packets_recv
    p = p2 - p1
    print('Interface packets: %i pkts (%.1f%% of expected value)' % (p, (p/(800e6/2048/2*interval)-1)*100))
    return p

def run_test(interfaces=['ens1f0'], cycle_power=True, reset_gpu_links=True ):
    """
    """

    N = 1000
    rb = []
    r = []
    n_resets = []
    ps = AgilentN5700(POWER_SUPPLY_IP_ADDR)


    for n in range(N):
        print('Trial #%i' % n)
        if cycle_power:
            print('Powering down power supply')
            ps.unlock()
            ps.power_off()
            time.sleep(5)
            print('Powering up power supply')
            ps.power_on()
            time.sleep(25)


        # while True:
        print('initializing FPGAs: Trial #%i, results so far=%r' % (n,r))
        try:
            # 10.10.10.220 # Board with QSFP errors
            ca = FPGAArray(ICEBOARD_IP_ADDR,
                           prog=2, open=1,
                           mode='shuffle16',
                           sync_method='local_soft_trigger',
                           stderr_log_level='WARN',
                           ignore_missing_boards=True)
            # ca=FPGAArray('mb 338', prog=2, open=1, mode='shuffle16', sync_method='local_soft_trigger', stderr_log_level='INFO', ignore_missing_boards=True)
            # ca = FPGAArray('bp16 25:0', prog=2, open=1, mode='shuffle256', sync_method='distributed_time', sync_source='bp_time', stderr_log_level='INFO')
            # break
        except (IOError, RuntimeError) as e:
            print('Got an exception. Will retry\n%r' % e)
            time.sleep(2)
            continue

        ib = ca.ib[0]
        ib.GPU.set_tx_power(15)
        ib.GPU.CORE_RESET = 1
        ib.GPU.CORE_RESET = 0

        # while True:
        #     print('Resetting GPU links')
        #     ca.ib.GPU.CORE_RESET = 1
        #     # time.sleep(.5)
        #     ca.ib.GPU.CORE_RESET = 0
        #     # ca.sync()
        #     time.sleep(.1)
        #     for _ in xrange(30):
        #         get_packet_recv(iface=interfaces[0], interval=1)

        up = check_interface_status(iface=interfaces)
        for _ in xrange(10):
            # states = is_interface_receiving_data(iface=interfaces)
            print("    GPU FIFOS: Data=%i, Flags=%i" % (ib.GPU.DATA_FIFO_OVERFLOW, ib.GPU.FRAME_FIFO_OVERFLOW))
            print("    QSFP 2:%i 6:%i" % (ib.hw.qsfp[1].read_byte(2), ib.hw.qsfp[1].read_byte(6)))
            states = [get_packet_recv(iface=interfaces[0], interval=1)]
        rb.append(all(states))
        print('State: %r, Rx data data: %r' % (up, states))
        n_reset = 0
        if reset_gpu_links:
            while True:
                if all(states) or n_reset >= 10:
                    break
                print('No data from one link. Resetting GPU links')
                ca.ib.GPU.CORE_RESET = 1
                time.sleep(.5)
                ca.ib.GPU.CORE_RESET = 0
                # ca.sync()
                time.sleep(.5)
                n_reset += 1
                for _ in xrange(10):
                    # states = is_interface_receiving_data(iface=interfaces)
                    states = [get_packet_recv(iface=interfaces[0], interval=1)]
        n_resets.append(n_reset)
        print('Waiting for interface to come up')
        time.sleep(2)
        # states = check_interface_status()
        for _ in xrange(10):
            # states = is_interface_receiving_data(iface=interfaces)
            states = [get_packet_recv(iface=interfaces[0], interval=1)]
        state = all(states)
        if not state:
            return
        r.append(state)
        # cycle_power = state
        print('Interface status so far: Pre-reset', rb)
        print('Interface status so far: Post-reset', r)
        print('Number of resets so far:           ', n_resets)
        print('---------------------------------------------------------------------')
        print('---------------------------------------------------------------------')
        print('Number of trials:    %i' % len(rb))
        print('Pre-reset failures:  %i/%i' % (len(rb)-sum(rb), len(rb)))
        print('Post-reset failures: %i/%i' % (len(r)-sum(r), len(r)))
        print('Max number of resets %i/%i' % (max(n_resets), len(n_resets)))
        print('---------------------------------------------------------------------')
        print('---------------------------------------------------------------------')

    #    if not state:
    #        break
        ca.ib.close()


    print r


if __name__ == '__main__':
    run_test()