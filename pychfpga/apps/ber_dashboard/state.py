# state.py
from pychfpga.fpga_array import FPGAArray

class AppState:
    def __init__(self):
        self.ca = None
        self.temps = []
        self.tx_rx_map = {}
        self.crate = None
        self.mode = None   # FPGAArray mode of the booted boards: 'corr64' (backplane) or 'shuffle8' (QSFP)
        self.bp_qsfp_tx = None   # per-port backplane QSFP TX applied at boot: {slot: {lane: (port, media, (swing, pre, post))}}

    def reset_mapping(self):
        self.tx_rx_map = {}