from pychfpga.common import UDPPacketReceiver, RawDataPacketProcessor
from pychfpga.fpga_array import FPGAArray

config = {
    "prog": 1,  # 0 - do not reprogram, 1 - reprogram if outdated, 2 - always reprogram
    "mode": 'corr16',  # Do not change for a single board
    "hwm": 'mb 333',
}

def board_conn():
    ca = FPGAArray(**config)
    board = ca.ib[0]
    return board


b = board_conn()
b.set_channelizer(data_source='ramp', fft_bypass=1, scaler_bypass=1)
b.start_data_capture(period=1, source='funcgen', frames_per_burst=4,
                              burst_period_in_frames=None)

# pr = UDPPacketReceiver(b.get_data_socket(port_number=None), verbose=1)
r = b.get_data_receiver(verbose = 3, report_pkt_len_me=2000, ring_buffer_size=3)


ts, data, data_count = r.read_raw_frames(2, split_frames = True)
print(ts[0])
print(data.shape)
print(data_count[0])
# for i in range(len(ts)):
#     print(f"Yielded ts: {ts[i]}")
#     print(f"First ten bytes of first channel yielded: {data[i, 0, 0, :10]}")




