# state.py
from pychfpga.fpga_array import FPGAArray

class AppState:
    def __init__(self):
        self.ca = None
        self.temps = []
        self.tx_rx_map = {}
        self.crate = None

    def reset_mapping(self):
        self.tx_rx_map = {}