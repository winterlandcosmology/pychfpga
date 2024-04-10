import matplotlib.pyplot as plt
import numpy as np
from wtl.config import load_yaml_config

from pychfpga.fpga_array import FPGAArray

colors = plt.rcParams['axes.prop_cycle'].by_key()['color']


# When figure is closed program is terminated
def handle_close(evt):
    global close_flag
    close_flag = 1


def data2abs(data):
    data_real = data[:, 0::2]
    data_imag = data[:, 1::2]
    return np.abs(data_real + 1j * data_imag)


input_names = {i: f'A{i + 1}' for i in range(8)}
input_names.update({i + 8: f'B{i + 1}' for i in range(8)})

# Connect to the board
config = load_yaml_config("connection_config")
ca = FPGAArray(**config)
b = ca.ib[0]
b.set_adc_delays()

chan_params = dict(
    adc_mode='data',  # Mode of the ADC *chip*: 'data' (digitized analog inputs), 'ramp' or 'pulse'
    adc_sampling_mode=0,  # 0: 4-channel, 4: 2-channel A+C
    adc_bandwidth=2,  # 0:500 MHz, 1: 600 MHz, 2:1500MHz, 3:2 GHz.
    adcdaq_mode='data',
    # 'data': data from the ADC chip;  or 'ramp': Ramp is generated in the FPGA (ADC chip is ignored)
    data_source='adc',  # Source of the function generator, as follows:
    gain=(1, 20),
    # data_source='sin',
    # freq=1,
    fft_bypass=0,
    scaler_bypass=0,
    # data_source='adc',
    # scaler_eight_bit=1,
    # prober_user_flags=0,
    # scaler_rounding_mode=0,
    # offset_binary_encoding=1,
    # fft_shift=(1 << 10) - 1,
)

# ylim = [-32, 31]
ylim = [-256, 255]

b.set_channelizer(**chan_params)
ca.sync()
b.start_data_capture(period=1, source='scaler')

for ch in b.chan:
    ch.SCALER.STATS_FRAME_COUNT = 390625

r = b.get_data_receiver(verbose=0)
ts, data, count = r.read_raw_frames()
data = data2abs(data)

figure, axs = plt.subplots(4, 4, figsize=(18, 8))
figure.tight_layout()
# ax.relim()
# ax.autoscale_view(True, True, True)
plt.show(block=False)
figure.canvas.mpl_connect('close_event', handle_close)
lines = []

freq = np.linspace(0, 400, 2048)
for i in range(4):
    for j in range(4):
        inp = i * 4 + j
        line, = axs[i][j].plot(data[inp], c=colors[i])
        axs[i][j].set_ylim(ylim)
        axs[i][j].set_title(input_names[inp], x=0.03, y=1.0, pad=-14, ha='left', fontweight='bold')
        adc_of = b.chan[inp].SCALER.STATS_ADC_OVERFLOWS
        # line.set_label(input_names[inp])
        lines.append(line)

# plt.legend(loc='upper right')

close_flag = 0

while close_flag == 0:
    ts, data, count = r.read_raw_frames()
    data = data2abs(data)
    for i in range(len(data)):
        # os.system('clear')
        # print(np.max(data[input_map[inp]]))
        lines[i].set_ydata(data[i])
        # adc_of = b.chan[i].SCALER.STATS_ADC_OVERFLOWS
    figure.canvas.draw()
    figure.canvas.flush_events()
    if close_flag == 1:
        break
    # time.sleep(0.5)
