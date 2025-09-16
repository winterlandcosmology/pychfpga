import os

import matplotlib.pyplot as plt
from wtl.config import load_yaml_config

from pychfpga.fpga_array import FPGAArray
import matplotlib
matplotlib.use('TkAgg')

colors = plt.rcParams['axes.prop_cycle'].by_key()['color']


# =========================
# INPUTS = ['A1', 'A2', 'A3', 'A4']


# =========================


# When figure is closed program is terminated
def handle_close(evt):
    global close_flag
    close_flag = 1


input_names = {i: f'A{i + 1}' for i in range(8)}
input_names.update({i + 8: f'B{i + 1}' for i in range(8)})

# Connect to the board
config = load_yaml_config("connection_config")
ca = FPGAArray(**config)
b = ca.ib[0]
b.set_adc_delays()

# b.set_channelizer(adc_mode="ramp")
b.start_data_capture(period=1, source='adc')

for ch in b.chan:
    ch.SCALER.STATS_FRAME_COUNT = 390625

r = b.get_data_receiver(verbose=0)
ts, data, count = r.read_raw_frames()

figure, axs = plt.subplots(4, 4, figsize=(18, 8))
figure.tight_layout()
# ax.relim()
# ax.autoscale_view(True, True, True)
plt.show(block=False)
figure.canvas.mpl_connect('close_event', handle_close)
lines = []

for i in range(4):
    for j in range(4):
        inp = i * 4 + j
        line, = axs[i][j].plot(data[inp], c=colors[i])
        axs[i][j].set_ylim([-128, 127])
        axs[i][j].set_title(input_names[inp], x=0.03, y=1.0, pad=-14, ha='left', fontweight='bold')
        adc_of = b.chan[inp].SCALER.STATS_ADC_OVERFLOWS
        # line.set_label(input_names[inp])
        lines.append(line)

# plt.legend(loc='upper right')

close_flag = 0

while close_flag == 0:
    ts, data, count = r.read_raw_frames()
    for i in range(len(data)):
        os.system('clear')
        # print(np.max(data[input_map[inp]]))
        lines[i].set_ydata(data[i])
        # adc_of = b.chan[i].SCALER.STATS_ADC_OVERFLOWS
    figure.canvas.draw()
    figure.canvas.flush_events()
    if close_flag == 1:
        break
    # time.sleep(0.5)
