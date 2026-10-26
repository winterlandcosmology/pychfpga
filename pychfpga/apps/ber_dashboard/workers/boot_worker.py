# workers/boot_worker.py
import asyncio

from PyQt6.QtCore import QThread, pyqtSignal
from pychfpga.fpga_array import FPGAArray
from ..sfp_ber_tests import check_bp_qsfp_copper

class BootWorker(QThread):
    log = pyqtSignal(str)

    MODES = {'Backplane links (corr64)': 'corr64', 'QSFP loopback (shuffle8)': 'shuffle8'}

    def __init__(self, hwm, state, mode='corr64'):
        super().__init__()
        self.hwm = hwm
        self.ca = None
        self.state = state
        self.mode = mode

    async def initialize_fpga_array(self):
        if self.mode == 'shuffle8':
            # QSFP loopback: every board tests itself, so no backplane sync and any subset of slots is allowed
            kwargs = dict(sync_method='local', ignore_missing_boards=True)
        else:
            kwargs = dict(sync_method='local', sync_source='bp_trig', sync_master=1,
                          sync_master_source='irigb_gen', sync_master_output=1)
        self.ca = await FPGAArray(self.hwm, mode=self.mode, stdout_log_level='debug', **kwargs)

    def run(self):
        self.log.emit("Booting boards...")

        if self.hwm is not None:
            try:
                asyncio.run(self.initialize_fpga_array())
            except Exception as e:
                self.log.emit(f"Error initializing FPGAArray: {str(e)}")
                return
        else:
            self.log.emit("HWM must be provided to boot worker.")
            return

        if self.mode.startswith('corr'):
            for i in self.ca.ib:
                i.GPIO.ANT_RESET = 0
                i.GPIO.CORR_RESET = 1
                i.GPIO.CORR_RESET = 0

        if self.mode.startswith('corr'):
            # Backplane QSFP lanes come up at the single copper TX drive (set in the corner-turn init); warns on other media.
            try:
                check_bp_qsfp_copper(self.ca, log=self.log.emit)
            except Exception as e:
                self.log.emit(f'Backplane QSFP port check failed: {type(e).__name__}: {e}')
        self.state.ca = self.ca
        self.state.mode = self.mode
        self.log.emit("Boot complete.")
