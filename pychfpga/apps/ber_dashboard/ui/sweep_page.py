# ui/sweep_page.py

from PyQt6.QtWidgets import *
from PyQt6.QtCore import QTimer
from ..workers.sweep_worker import SweepWorker
from .heatmap import HeatmapGrid
from ..utils import parse_range
import time
import matplotlib.pyplot as plt
from matplotlib.colors import LogNorm
import math
import numpy as np
import os

from ..paths import PLOT_DIR
PLOTDIR = PLOT_DIR + "/"
os.makedirs(PLOTDIR, exist_ok=True)


class SweepPage(QWidget):
    def __init__(self, state):
        super().__init__()
        self.state = state

        self._start_time = None
        self._timer = QTimer()
        self._timer.timeout.connect(self.update_timer)

        layout = QVBoxLayout()

        CRATErow = QHBoxLayout()
        CRATErow.addWidget(QLabel("CRATE NUMBER"))
        self.crate_sn_input = QLineEdit()
        CRATErow.addWidget(self.crate_sn_input)
        self.overwrite_checkbox = QCheckBox()
        CRATErow.addWidget(QLabel("Overwrite Output Plot: "))
        CRATErow.addWidget(self.overwrite_checkbox)
        crate = self.state.crate
        if crate:
            self.crate_sn_input.setText(crate)

        PRErow = QHBoxLayout()
        PRErow.addWidget(QLabel("PRE range"))
        self.PRErange_input = QLineEdit("0-20")
        PRErow.addWidget(self.PRErange_input)
        self.PREstep = QLineEdit("1")
        PRErow.addWidget(QLabel("Step"))
        PRErow.addWidget(self.PREstep)

        POSTrow = QHBoxLayout()
        POSTrow.addWidget(QLabel("POST range"))
        self.POSTrange_input = QLineEdit("0-20")
        POSTrow.addWidget(self.POSTrange_input)
        self.POSTstep = QLineEdit("1")
        POSTrow.addWidget(QLabel("Step"))
        POSTrow.addWidget(self.POSTstep)
        
        PWRrow = QHBoxLayout()
        PWRrow.addWidget(QLabel("TX Power"))
        self.power_input = QLineEdit("10")
        PWRrow.addWidget(self.power_input)

        DWELLrow = QHBoxLayout()
        DWELLrow.addWidget(QLabel("Dwell Time (s)"))
        self.dwell_input = QLineEdit("0.5")
        DWELLrow.addWidget(self.dwell_input)

        btn_row = QHBoxLayout()
        self.start_btn = QPushButton("Run Sweeps")
        self.estimate_label = QLabel("Est: --")
        self.estimate_label.setStyleSheet("color: gray; font-size: 11px;")
        btn_row.addWidget(self.start_btn, stretch=5)
        btn_row.addWidget(self.estimate_label, stretch=1)

        self.timer_label = QLabel("Elapsed: 00:00")
        self.timer_label.setStyleSheet("font-size: 11px; color: gray;")
        
        progress_layout = QHBoxLayout()
        self.link_progress = QProgressBar()
        self.link_progress.setValue(0)
        progress_layout.addWidget(QLabel("Link Progress:"))
        progress_layout.addWidget(self.link_progress)
        self.progress = QProgressBar()
        self.progress.setValue(0)
        progress_layout.addWidget(QLabel("Overall Progress:"))
        progress_layout.addWidget(self.progress)


        self.log = QTextEdit()
        self.log.setReadOnly(True)

        self.heatmap = HeatmapGrid([], [])

        # LAYOUT
        layout.addLayout(CRATErow)
        layout.addLayout(PRErow)
        layout.addLayout(POSTrow)
        layout.addLayout(PWRrow)
        layout.addLayout(DWELLrow)
        layout.addLayout(btn_row)
        layout.addWidget(self.timer_label)
        layout.addLayout(progress_layout)
        self.log.setMaximumHeight(60)
        layout.addWidget(self.log)

        layout.addWidget(self.heatmap)

        self.setLayout(layout)

        # CONNECTORS
        self.PRErange_input.textChanged.connect(self.update_estimate)
        self.POSTrange_input.textChanged.connect(self.update_estimate)
        self.PREstep.textChanged.connect(self.update_estimate)
        self.POSTstep.textChanged.connect(self.update_estimate)
        self.dwell_input.textChanged.connect(self.update_estimate)

        self.update_estimate()

        self.start_btn.clicked.connect(self.start)

    def start(self):
        if not self.state.tx_rx_map:
            self.log.append("No mapping found.")
            return

        self._start_time = time.time()
        self._timer.start(1000)  # update every 1s

        self.progress.setValue(0)
        self.link_progress.setValue(0)
        self.log.clear()
        self.heatmap.clear()

        self.worker = SweepWorker(
            self.state.ca,
            self.state.tx_rx_map,
            parse_range(self.POSTrange_input.text(), step=int(self.POSTstep.text())),
            parse_range(self.PRErange_input.text(), step=int(self.PREstep.text())),
            float(self.dwell_input.text()),
            int(self.power_input.text())
        )

        self.worker.log.connect(self.log.append)
        self.worker.progress.connect(self.update_progress)
        self.worker.link_progress.connect(self.update_link_progress)
        self.worker.heatmap.connect(self.update_heatmap)
        self.worker.finished.connect(self.stop_timer)
        self.worker.finished.connect(self.save_full_report)

        self.worker.start()

    def update_estimate(self):
        crate = self.state.crate
        if crate:
            self.crate_sn_input.setText(crate)
        try:
            pre = parse_range(self.PRErange_input.text(), step=int(self.PREstep.text()))
            post = parse_range(self.POSTrange_input.text(), step=int(self.POSTstep.text()))
            
            dwell = float(self.dwell_input.text())

            links = len(self.state.tx_rx_map)

            total_points = len(pre) * len(post) * links
            seconds = total_points * (dwell + 0.3)  # add 0.3s overhead per point

            mins = int(seconds // 60)
            secs = int(seconds % 60)

            self.estimate_label.setText(f"Est: {mins:02d}:{secs:02d}")
        except:
            self.estimate_label.setText("Est: --")

    def update_progress(self, done, total):
        self.progress.setMaximum(total)
        self.progress.setValue(done)

    def update_link_progress(self, done, total):
        self.link_progress.setMaximum(total)
        self.link_progress.setValue(done)

    def update_timer(self):
        if not self._start_time:
            return
        
        elapsed = time.time() - self._start_time

        em, es = divmod(int(elapsed), 60)

        self.timer_label.setText(
            f"Elapsed: {em:02d}:{es:02d}"
        )

    def stop_timer(self):
        self._timer.stop()

    def update_heatmap(self, tx_sn, rx_sn, tx_slot, tx_lane, rx_slot, rx_lane, prerange, postrange, matrix, ber_floor, vmax):
        self.heatmap.update(tx_sn, rx_sn, tx_slot, tx_lane, rx_slot, rx_lane, prerange, postrange, matrix, ber_floor, vmax)

    def save_full_report(self, results):
        if not results:
            return

        # Create a mapping of unique (tx_slot, tx_lane) pairs
        unique_pairs = sorted(set((r["tx_slot"], r["tx_lane"]) for r in results))
        num_heatmaps = len(unique_pairs)
        
        rows = len(set(r["tx_slot"] for r in results))
        cols = len(set(r["tx_lane"] for r in results))

        print(f"Generating full report with {num_heatmaps} heatmaps, arranged in {rows} rows × {cols} cols...")

        fig, axes = plt.subplots(rows, cols, figsize=(cols * 4, rows * 4))
        fig.subplots_adjust(right=0.88)  # leave space for colorbar

        # Create a mapping from (tx_slot, tx_lane) to axis position
        slot_to_row = {slot: i for i, slot in enumerate(sorted(set(r["tx_slot"] for r in results)))}
        lane_to_col = {lane: j for j, lane in enumerate(sorted(set(r["tx_lane"] for r in results)))}

        for res in results:
            i = slot_to_row[res["tx_slot"]]
            j = lane_to_col[res["tx_lane"]]
            try:
                ax = axes[i, j]
            except (IndexError, AttributeError):
                ax = axes[max(i, j)]  # if only one row or one column, use the single axis

            Z = res["ber_matrix"]

            im = ax.imshow(
            Z,
            cmap="viridis",
            origin="lower",
            norm=LogNorm(vmin=1e-12, vmax=1e-3)
            )

            post_range = np.arange(Z.shape[1])
            pre_range = np.arange(Z.shape[0])
            zy, zx = np.where(Z == res['ber_floor'])
            if zx.size:
                posts = [post_range[j] for j in zx]
                pres  = [pre_range[i] for i in zy]
                ax.scatter(posts, pres, facecolors='none', edgecolors='purple', linewidths=1.2, zorder=10)



            ax.set_title(
            f"TX ({res['tx_slot']}:{res['tx_lane']}) → RX ({res['rx_slot']}:{res['rx_lane']})\nSN{res['tx_sn']} → SN{res['rx_sn']}"
            )
            ax.set_xlabel("POST")
            ax.set_ylabel("PRE")

            ax.set_xticks(range(len(res["post_range"])))
            ax.set_xticklabels(res["post_range"])

            ax.set_yticks(range(len(res["pre_range"])))
            ax.set_yticklabels(res["pre_range"])

        # remove unused axes
        for i in range(len(results), len(axes)):
            axes[i].axis("off")

        fig.suptitle(f"BER Sweep Report - CRATE SN{self.crate_sn_input.text()}", fontsize=16)
        plt.tight_layout(rect=[0, 0, 0.88, 0.96])

        cbar_ax = fig.add_axes([0.90, 0.15, 0.02, 0.7])  # [left, bottom, width, height]

        cbar = fig.colorbar(im, cax=cbar_ax)
        cbar.set_label("BER")


        base = f"{PLOTDIR}berSweep_crateSN-{self.crate_sn_input.text()}"
        filename = f"{base}.png"

        i = 1
        if not self.overwrite_checkbox.isChecked():
            while os.path.exists(filename):
                filename = f"{base}_{i}.png"
                i += 1

        plt.savefig(filename, bbox_inches='tight', dpi=200)
        plt.close(fig)

        self.log.append(f"Saved full report: {filename}")