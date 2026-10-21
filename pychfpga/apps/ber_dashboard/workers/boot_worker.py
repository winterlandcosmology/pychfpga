# workers/boot_worker.py
import asyncio

from PyQt6.QtCore import QThread, pyqtSignal
from pychfpga.fpga_array import FPGAArray

class BootWorker(QThread):
    log = pyqtSignal(str)

    def __init__(self, hwm, state):
        super().__init__()
        self.hwm = hwm
        self.ca = None
        self.state = state

    async def initialize_fpga_array(self):
        self.ca =  await FPGAArray(
                self.hwm, 
                mode='corr64', 
                sync_method='local', 
                sync_source='bp_trig', 
                sync_master=1, 
                sync_master_source='irigb_gen', 
                sync_master_output=1, 
                stdout_log_level='debug')

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

        for i in self.ca.ib:
            i.GPIO.ANT_RESET = 0
            i.GPIO.CORR_RESET = 1
            i.GPIO.CORR_RESET = 0

        self.state.ca = self.ca
        self.log.emit("Boot complete.")