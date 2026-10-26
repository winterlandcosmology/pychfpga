# ui/map_page.py

from PyQt6.QtWidgets import *
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
import numpy as np
import ast

from ..workers.mapping_worker import MappingWorker
from ..utils import parse_range

# new: simple dialog to view/edit Map
class MapEditorDialog(QDialog):
    def __init__(self, map_obj, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit Map")
        self.resize(600, 300)
        layout = QVBoxLayout(self)

        self.editor = QPlainTextEdit(self)
        self.editor.setPlainText(repr(map_obj))
        layout.addWidget(self.editor)

        btns = QHBoxLayout()
        btns.addStretch(1)
        self.save_btn = QPushButton("Save")
        self.cancel_btn = QPushButton("Cancel")
        btns.addWidget(self.save_btn)
        btns.addWidget(self.cancel_btn)
        layout.addLayout(btns)

        self.save_btn.clicked.connect(self.accept)
        self.cancel_btn.clicked.connect(self.reject)

    def get_text(self):
        return self.editor.toPlainText()

class MapPage(QWidget):
    def __init__(self, state):
        super().__init__()
        self.state = state

        layout = QVBoxLayout()

        slot_layout = QHBoxLayout()
        slot_layout.addWidget(QLabel("Slots"))
        self.slots_input = QLineEdit("1-16")
        slot_layout.addWidget(self.slots_input)
        
        lane_layout = QHBoxLayout()
        lane_layout.addWidget(QLabel("Lanes"))
        self.lanes_input = QLineEdit("0-11")
        lane_layout.addWidget(self.lanes_input)

        dwell_layout = QHBoxLayout()
        dwell_layout.addWidget(QLabel("Dwell Time (s)"))
        self.dwell_input = QLineEdit("0.1")
        dwell_layout.addWidget(self.dwell_input)
        dwell_layout.addWidget(QLabel("Retrain Delay (s)"))
        self.retrain_delay_input = QLineEdit("0.2")
        dwell_layout.addWidget(self.retrain_delay_input)
        dwell_layout.addWidget(QLabel("Settle Time (s)"))
        self.settle_time_input = QLineEdit("0.5")
        dwell_layout.addWidget(self.settle_time_input)

        pwr_layout = QHBoxLayout()
        pwr_layout.addWidget(QLabel("TX Power (0-15)"))
        self.power_input = QLineEdit("10")
        pwr_layout.addWidget(self.power_input)
        pwr_layout.addWidget(QLabel("PRE (0-31)"))
        self.pre_input = QLineEdit("10")
        pwr_layout.addWidget(self.pre_input)
        pwr_layout.addWidget(QLabel("POST (0-31)"))
        self.post_input = QLineEdit("10")
        pwr_layout.addWidget(self.post_input)
        self.keep_tx = QCheckBox("Keep per-port TX from boot")
        self.keep_tx.setChecked(True)
        self.keep_tx.setToolTip("Map with the TX drive set at boot from each port's media, ignoring the three fields. "
                                "Falls back to the fields when boot did not set it.")
        pwr_layout.addWidget(self.keep_tx)

        self.start_btn = QPushButton("Run Mapping")
        self.edit_map_btn = QPushButton("Edit Map")

        self.log = QTextEdit()
        self.log.setReadOnly(True)

        self.table = QTableWidget()
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)

        header = QLabel("SLOT [LANE] MAP")
        header.setStyleSheet("font-weight: bold; font-size: 14px;")

        horizontal = self.table.horizontalHeader()
        horizontal.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        

        layout.addLayout(slot_layout)

        layout.addLayout(lane_layout)

        layout.addLayout(dwell_layout)

        layout.addLayout(pwr_layout)

        # start / edit CA buttons
        btn_row = QHBoxLayout()
        btn_row.addWidget(self.start_btn)
        btn_row.addWidget(self.edit_map_btn)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

        self.log.setMaximumHeight(50)
        layout.addWidget(self.log)

        layout.addWidget(header)
        layout.addWidget(self.table)

        self.setLayout(layout)


        self.start_btn.clicked.connect(self.start)
        self.edit_map_btn.clicked.connect(self.open_map_editor)


    # ---------- BER → color ----------
    def ber_to_color(self, ber):
        if ber is None:
            return QColor("lightgray")

        # clamp range
        ber = max(min(ber, 1e-3), 1e-12)

        # log scale normalize
        norm = (np.log10(ber) - np.log10(1e-12)) / (np.log10(1e-3) - np.log10(1e-12))
        norm = min(max(norm, 0), 1)

        # green → yellow → red
        if norm < 0.5:
            r = int(255 * (norm * 2))
            g = 255
        else:
            r = 255
            g = int(255 * (1 - (norm - 0.5) * 2))

        return QColor(r, g, 0)

    # ---------- setup table ----------
    def setup_table(self, slots, lanes):
        self.slots = slots
        self.lanes = lanes

        self.table.clear()

        self.table.setRowCount(len(slots))
        self.table.setColumnCount(len(lanes))

        self.table.setVerticalHeaderLabels([f"SLOT {s}" for s in slots])
        self.table.setHorizontalHeaderLabels([f"LANE {l}" for l in lanes])

        self.table.resizeColumnsToContents()

    # ---------- update cell ----------
    def update_cell(self, slot, lane, rx_slot, rx_lane, ber):
        row = self.slots.index(slot)
        col = self.lanes.index(lane)

        if ber is None:
            text = "NA"
        else:
            text = f"{rx_slot} [{rx_lane}]\n{ber:.1e}"

        item = QTableWidgetItem(text)
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

        item.setBackground(self.ber_to_color(ber))

        self.table.setItem(row, col, item)

    # ---------- start ----------
    def start(self):
        slots = parse_range(self.slots_input.text())
        lanes = parse_range(self.lanes_input.text())
        dwell = float(self.dwell_input.text())
        retrain_delay = float(self.retrain_delay_input.text())
        settle_time = float(self.settle_time_input.text())
        pwr = int(self.power_input.text())
        pre = int(self.pre_input.text())
        post = int(self.post_input.text())
        if self.keep_tx.isChecked():
            if self.state.bp_qsfp_tx:
                pwr = None   # the mapper leaves the TX drive as set_bp_qsfp_tx left it
                self.log.append('Keeping the per-port backplane QSFP TX from boot (TX fields ignored).')
            else:
                self.log.append('No per-port TX from boot; using the TX fields.')

        if not slots or not lanes:
            return

        self.setup_table(slots, lanes)

        self.worker = MappingWorker(self.state.ca, slots, lanes, dwell, retrain_delay, settle_time, pwr, pre, post)

        self.worker.log.connect(self.log.append)
        self.worker.update.connect(self.update_cell)
        self.worker.result.connect(self.finish)
        self.worker.temps.connect(self.storeTemps)       

        self.start_btn.setEnabled(False)
        self.worker.start()

    def finish(self, mapping):
        self.state.tx_rx_map = mapping
        self.start_btn.setEnabled(True)

    def storeTemps(self, temps):
        self.state.temps = temps

    def open_map_editor(self):
        # prefer worker map if running, else state map
        map_obj = getattr(getattr(self, "worker", None), "map", None) or self.state.tx_rx_map
        dlg = MapEditorDialog(map_obj, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            text = dlg.get_text()
            # try parsing safely, fall back to raw string
            try:
                new_map = ast.literal_eval(text)
            except Exception:
                new_map = text

            # update state and worker if present
            self.state.tx_rx_map = new_map
            if getattr(self, "worker", None):
                self.worker.map = new_map
            self.log.append("Map updated.")