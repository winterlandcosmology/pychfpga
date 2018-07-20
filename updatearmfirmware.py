import sys
sys.path.append("c:/Users/Winterland/Desktop/ch_acq/pychfpga/")
sys.path.append("c:/Users/Winterland/Desktop/ch_acq/")
import numpy as np
import time

import fpga_array as FA
FA.setup_logging(log_target='syslog', log_level='debug', sql_log_level='warn', stderr_log_level='warn')

ca = FA.FPGAArray(icecrates='026', prog =2, open =1 , ignore_missing_boards=1)

ca._update_arm_firmware(image_filename="C:\Users\Winterland\Desktop\ch_acq\savelogs20thApril.img.bz2")