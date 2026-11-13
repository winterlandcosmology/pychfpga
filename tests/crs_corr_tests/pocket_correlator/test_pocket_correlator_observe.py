import asyncio
import numpy as np
import sys
sys.path.insert(0, '../../../pychfpga/')
from pocket_correlator import POCKET_CORRELATOR


data_path = '/home/<user>/test_data'


async def main():
    pc = await POCKET_CORRELATOR(hwm = 'crs 0050', stderr_log_level = 'debug', prog = 2).init()
    await pc.observe_async(data_path = data_path,
           obs_tag = '_test', 
           digital_gains_path = None, 
           n_vis_per_file = 16,
           n_software_frames = 119, # 119 software frames with 16384 firmware frames is approximately 10 s
           autocorr_only = 0, # Get all products by default
           no_accum = 0, # accumulate by default
           n_firmware_frames = 16384,
           gain_target = 1.5*np.sqrt(2), # borrowed from CHIME
           n_fft_frames_for_gain_comp = 2*500,
           capture_adc_bursts = True,
           n_adc_frames_per_file = 2*5,
           capture_fft_bursts = True,
           n_fft_frames_per_file = 2*5
           )

asyncio.run(main())
