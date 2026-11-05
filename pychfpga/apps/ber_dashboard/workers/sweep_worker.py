from PyQt6.QtCore import QThread, pyqtSignal
from ..sfp_ber_tests import sweep_ber_heatmap
import numpy as np

class SweepWorker(QThread):
    log = pyqtSignal(str)
    progress = pyqtSignal(int, int)
    link_progress = pyqtSignal(int, int)
    heatmap = pyqtSignal(str, str, int, int, int, int, object, object, object, float, float)  # tx_sn, rx_sn, tx_slot, tx_lane, rx_slot, rx_lane, prerange, postrange, matrix, ber_floor, vmax
    finished = pyqtSignal(list)
    
    def __init__(self, ca, mapping, pre_range, post_range, dwell, power=13):
        super().__init__()
        self.ca = ca
        self.mapping = mapping
        self.pre = pre_range
        self.post = post_range
        self.bit_rate = 25.0e9 # matches the bit rate used in sweep_ber_heatmap
        self.dwell = dwell
        self.power = power
        self.running = True

        self.results = []

    def run(self):
        total_links = len(self.mapping)
        done_links = 0

        self.log.emit("Starting BER sweeps...\n")
        
        # board_sns = [sn.strip() for sn in str(list(self.ca.ib)).replace("CRS", "").replace('SN', '').replace('_', '').replace("(", "").replace(")", "").replace('[', '').replace(']', '').split(",")]
        board_sns = [ob['serial'] for ob in self.ca.get_hwm() if ob['model']=='CRS']
        print(self.ca.ib)
        for (tx_slot, tx_lane), (rx_slot, rx_lane) in self.mapping.items():
            if not self.running:
                return

            tx_sn = board_sns[tx_slot-1]
            rx_sn = board_sns[rx_slot-1]

            # ---------- HEADER ----------
            # self.log.emit(f"TX({tx_slot},{tx_lane}) RX({rx_slot},{rx_lane})")

            total_points = len(self.pre) * len(self.post)
            done_points = 0

            # ---------- SWEEP ----------
            ber_matrix = sweep_ber_heatmap(
                self.ca,
                tx_slot=tx_slot,
                tx_lane=tx_lane,
                rx_slot=rx_slot,
                rx_lane=rx_lane,
                pre_range=self.pre,
                post_range=self.post,
                dwell=self.dwell,
                tx_pow=self.power,
                plot=False,
                log=self.log.emit,  # we handle logging here
                progress=self.link_progress.emit  # we handle progress here
            )

            # lightweight display sweep for UI feedback
            print(type(self.bit_rate), self.bit_rate, '\n', type(self.dwell), self.dwell)
            ber_floor = 1.0 / (self.bit_rate * self.dwell)
            Z = np.clip(ber_matrix, ber_floor, None)   # avoid log(0)
            vmax = max(Z.max(), ber_floor * 10)        # keep vmax > vmin even if all zeros
            self.heatmap.emit(
                tx_sn, rx_sn,
                tx_slot, tx_lane,
                rx_slot, rx_lane,
                self.pre, self.post,
                ber_matrix,
                ber_floor, 
                vmax

            )

            self.results.append({
                "tx_sn": tx_sn,
                "rx_sn": rx_sn,
                "tx_slot": tx_slot,
                "tx_lane": tx_lane,
                "rx_slot": rx_slot,
                "rx_lane": rx_lane,
                "pre_range": self.pre,
                "post_range": self.post,
                "ber_matrix": ber_matrix,
                "ber_floor": ber_floor,
                "vmax": vmax
            })

            # ---------- DONE ----------
            # self.log.emit(f"TX({tx_slot},{tx_lane}) RX({rx_slot},{rx_lane}) DONE")
            # self.log.emit("-" * 40)

            done_links += 1
            self.progress.emit(done_links, total_links)

        # self.finished.emit()
        self.finished.emit(self.results)
        self.log.emit("ALL SWEEPS COMPLETE")