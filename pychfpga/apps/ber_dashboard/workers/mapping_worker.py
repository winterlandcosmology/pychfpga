# workers/mapping_worker.py

from PyQt6.QtCore import QThread, pyqtSignal
from ..sfp_ber_tests import probe_tx_lane, get_temps


class MappingWorker(QThread):
    log = pyqtSignal(str)
    update = pyqtSignal(int, int, object, object, object)  
    # slot, lane, rx_slot, rx_lane, ber
    result = pyqtSignal(dict)
    temps = pyqtSignal(list)

    def __init__(self, ca, slots, lanes, dwell, retrain_delay, settle_time, power=13, pre=6, post=8):
        super().__init__()
        self.ca = ca
        self.slots = list(slots)
        self.lanes = list(lanes)
        self.dwell = dwell
        self.retrain_delay = retrain_delay
        self.settle_time = settle_time
        self.power = power
        self.pre = pre
        self.post = post
        self.running = True

    def run(self):
        mapping = {}

        for slot in self.slots:
            for lane in self.lanes:
                if not self.running:
                    return

                if self.ca is None:
                    self.update.emit(slot, lane, None, None, None)
                    self.log.emit("No CA found.")
                    continue
                self.log.emit(f"CA: {self.ca} \n -> Probing TX SLOT {slot}, LANE {lane}")

                matches = probe_tx_lane(
                    self.ca,
                    mapping,
                    tx_slot=slot,
                    tx_lane=lane,
                    dwell=self.dwell,
                    retrain_delay=self.retrain_delay,
                    settle_time=self.settle_time,
                    TX_POW=self.power,
                    TX_PRE=self.pre,
                    TX_POST=self.post,
                    verbose=False,
                    log=self.log,
                    attempts=1
                )

                if matches:
                    rx_slot, rx_lane, _, ber = matches[0]
                    mapping[(slot, lane)] = (rx_slot, rx_lane)

                    self.update.emit(slot, lane, rx_slot, rx_lane, ber)
                else:
                    self.update.emit(slot, lane, None, None, None)
                    # self.log.emit(f"No match found for SLOT {slot}, LANE {lane}")

        self.result.emit(mapping)