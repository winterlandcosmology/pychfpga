from pychfpga.common import UDPPacketReceiver, RawDataPacketProcessor
from pychfpga.fpga_array import FPGAArray

config = {
    "prog": 1,  # 0 - do not reprogram, 1 - reprogram if outdated, 2 - always reprogram
    "mode": 'corr16',  # Do not change for a single board
    "hwm": 'mb 509',
}

def board_conn():
    ca = FPGAArray(**config)
    board = ca.ib[0]
    return board


b = board_conn()
b.set_channelizer(data_source='ramp', fft_bypass=1, scaler_bypass=1)
b.start_data_capture(period=1, source='funcgen', frames_per_burst=4,
                              burst_period_in_frames=None)

# Current API:
# - ``receive_window`` controls trailing incomplete-burst carryover during decode.
# - ``pr_buf_size`` controls how many UDP packets are batched before processing.
r = b.get_data_receiver(
    verbose=3,
    report_pkt_len_me=2000,
    receive_window=3,
    pr_buf_size=128,
)


ts, data, data_count = r.read_raw_frames(
    ncap=2,
    data_timeout=0.2,
)
print(ts[0])
print(data.shape)
print(data_count[0])
# for i in range(len(ts)):
#     print(f"Yielded ts: {ts[i]}")
#     print(f"First ten bytes of first channel yielded: {data[i, 0, 0, :10]}")


