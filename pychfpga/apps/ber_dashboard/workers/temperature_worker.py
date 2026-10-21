# DOESN'T WORK WELL... kind of abandoned this one...

from PyQt6.QtCore import QThread, pyqtSignal
import time

from ..state import AppState

class TemperatureWorker(QThread):
    new_data = pyqtSignal(list)

    def __init__(self, ib, interval=1.0):
        super().__init__()
        self.ib = ib
        self.interval = interval
        self.running = True

    def run(self):
        while self.running:
            temps = [i.SYSMON.temperature() for i in self.ib]
            self.new_data.emit(temps)
            time.sleep(self.interval)

    def stop(self):
        self.running = False

        