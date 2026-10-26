# ui/boot_page.py
import os

from PyQt6.QtWidgets import QWidget, QVBoxLayout, QPushButton, QTextEdit, QLineEdit, QLabel, QHBoxLayout ,QComboBox, QCheckBox
from pychfpga.hardware.crs.discover import collect_beacons
import pychfpga.fpga_firmware.chfpga  # noqa: F401  registers the chFPGA modes; importing CRS first does not load them
from ..workers.boot_worker import BootWorker
from ..workers.task_worker import TaskWorker
import numpy as np
import logging
from ..utils import QtLogHandler
import os

# get ber_dashboard directory
from ..paths import DATA_DIR as ber_dashboard_dir   # prev_data/ lives in the user data directory


class BootPage(QWidget):
    def __init__(self, state):
        super().__init__()
        self.state = state

        layout = QVBoxLayout()

        # crate info
        craterow = QHBoxLayout()
        crate_label = QLabel("Crate Number:")
        craterow.addWidget(crate_label, 1)
        self.crate = QLineEdit()
        self.crate.setPlaceholderText("Enter crate number (e.g., '008')")
        craterow.addWidget(self.crate, 4)

        # fields for hwm
        # e.g., 
        # 1. 107
        # 2. 111
        #....
        # 16. 89
        # Should be a single field per board, and we should have the board number be PERMANENT (leading 1., 2., etc)
        
        self.hwm_inputs = []
        # import file: 'prev_data/hwm_prev.txt' to pre-populate the fields with the previous hwm used for testing    
        vals = {} # of the form {board_num: hwm_val}, where board_num is 1-16 and hwm_val is the value to use for that board (e.g., '89')
        for line in range(16):
            l = vals.get(line+1, '')
            row_layout = QHBoxLayout()
            label = QLabel(f"Board {line+1}:")
            hwm_input = QLineEdit()
            hwm_input.setPlaceholderText(f"Board {line+1} HWM (e.g., '89')")
            hwm_input.setText(l)
            row_layout.addWidget(label)
            row_layout.addWidget(hwm_input)
            layout.addLayout(row_layout)
            self.hwm_inputs.append(hwm_input)

        clear_btn = QPushButton("Clear HWM Fields")
        clear_btn.clicked.connect(self.clear_hwm_fields)
        layout.addWidget(clear_btn)

        prev_crate_path = os.path.join(ber_dashboard_dir, 'prev_data', 'prev_crate.txt')
        prev_crate = None
        if os.path.exists(prev_crate_path):
            with open(prev_crate_path, 'r') as f:
                prev_crate = f.read().strip()
        if prev_crate:
            self.load_crate_hwm(f"Crate {prev_crate}")
        else:
            print(f"Problem loading previous crate. Falling back to hwm_prev.txt list.")
            try:
                with open(os.path.join(ber_dashboard_dir, 'prev_data', 'hwm_prev.txt'), 'r') as f:
                    lines = f.read().splitlines()
                    for i, line in enumerate(lines):
                        vals[i+1] = line.strip()
            except Exception as e:
                print(f"Could not read prev_data/hwm_prev.txt: {str(e)}. Be sure the working directory is set to be `ber_dashboard`.")
        

        # self.hwm_input = QTextEdit()
        # self.hwm_input.setPlaceholderText("Enter HWM (one board per line, e.g., '89', '111', etc.)")

        # crate input and label
        self.save_crate_btn = QPushButton("Save Crate HWM")
        self.save_crate_btn.clicked.connect(self.save_crate_hwm)
        craterow.addWidget(self.save_crate_btn, 2)
        self.crate_dropdown = QComboBox()
        self.crate_dropdown.currentTextChanged.connect(self.load_crate_hwm)
        self.load_available_crates()
        craterow.addWidget(QLabel("Load Crate:"), 1)
        craterow.addWidget(self.crate_dropdown, 3)
        layout.addLayout(craterow)

        moderow = QHBoxLayout()
        moderow.addWidget(QLabel("Test mode:"), 1)
        self.mode = QComboBox()
        self.mode.addItems(list(BootWorker.MODES))
        moderow.addWidget(self.mode, 4)
        self.per_port_tx = QCheckBox("Set backplane QSFP TX from the plugged media")
        self.per_port_tx.setChecked(True)
        self.per_port_tx.setToolTip("corr64 only: after boot, read each backplane QSFP port through the expander and set the TX drive of the lanes "
                                    "reaching it (copper 13/6/8, optical 15/15/20, DFE). The Map page can keep these settings.")
        moderow.addWidget(self.per_port_tx, 3)
        layout.addLayout(moderow)

        # auto-discovery: every board broadcasts a beacon with its serial, crate and slot (TCPipe and U-Boot)
        discrow = QHBoxLayout()
        self.discover_btn = QPushButton("Discover boards")
        self.discover_btn.clicked.connect(self.discover)
        discrow.addWidget(self.discover_btn, 2)
        self.found_crates = QComboBox()
        self.found_crates.setPlaceholderText("Discovered crates")
        self.found_crates.currentIndexChanged.connect(self.fill_from_discovery)
        discrow.addWidget(self.found_crates, 4)
        self.include_uboot = QCheckBox("Include boards in U-Boot")
        self.include_uboot.setToolTip("Boards in U-Boot are idle; booting starts their firmware and loads a bitstream.")
        self.include_uboot.toggled.connect(self.fill_from_discovery)
        discrow.addWidget(self.include_uboot, 2)
        layout.addLayout(discrow)
        self.beacons = []


        # boot button and log
        btn_row = QHBoxLayout()
        self.btn = QPushButton("Boot Boards")
        self.clear_btn = QPushButton("Clear Output")
        self.logLevel = QComboBox() # select from DEBUG, INFO, WARNING, ERROR, CRITICAL
        self.logLevel.addItems(["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
        self.logLevel.setCurrentText("INFO")
        self.log = QTextEdit()
        btn_row.addWidget(self.btn)
        btn_row.addWidget(self.clear_btn)
        log_row = QHBoxLayout()
        log_row.addWidget(self.logLevel)
        log_row.addWidget(self.clear_btn)

        layout.addLayout(btn_row)
        layout.addLayout(log_row)
        layout.addWidget(self.log)

        self.setLayout(layout)

        self.btn.clicked.connect(self.start)
        self.clear_btn.clicked.connect(self.log.clear)

        # log handler
        self.qt_handler = QtLogHandler()
        self.qt_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
        # connect to QTextEdit
        self.qt_handler.emitter.log_signal.connect(self.log.append)
        # attach to your logger (or root)
        logging.getLogger().addHandler(self.qt_handler)
        log_level = getattr(logging, self.logLevel.currentText())
        logging.getLogger().setLevel(log_level)
        self.logLevel.currentTextChanged.connect(lambda level: logging.getLogger().setLevel(getattr(logging, level)))

    def start(self):
        # get hwm input from user, parse it, and pass to boot worker as: 'crs 89:1 111:2 107:3 ...' etc. We can also have a default value for testing purposes, but the user should be able to override it with their own HWM.
        hwm = 'crs '
        vals = {}
        for i, hwm_input in enumerate(self.hwm_inputs):
            print(f"Input for board {i+1}: {hwm_input.text()}")
            if hwm_input.text():
                hwm += f"{hwm_input.text()}:{i+1} "
                vals[i+1] = hwm_input.text()
        hwm = hwm.strip()  # remove trailing space
        if hwm == 'crs':
            hwm = None
        else:
            # save the hwm to 'prev_data/hwm_prev.txt' for next time
            try:
                with open(os.path.join(ber_dashboard_dir, 'prev_data', 'hwm_prev.txt'), 'w') as f:
                    for i in range(1, 17):
                        f.write(vals.get(i, '') + '\n')
            except Exception as e:
                print(f"Could not write to prev_data/hwm_prev.txt: {str(e)}")

        crate = self.crate.text().strip()
        if crate:
            self.state.crate = crate
            logging.info(f"Booting crate {crate} with HWM: {hwm}")
        
        self.worker = BootWorker(hwm, self.state, BootWorker.MODES[self.mode.currentText()], self.per_port_tx.isChecked())
        self.worker.log.connect(self.log.append)
        self.worker.start()

    def save_crate_hwm(self):
        crate_num = self.crate.text().strip()
        if not crate_num:
            logging.error("Crate number cannot be empty.")
            return
        filename = os.path.join(ber_dashboard_dir, 'prev_data', 'crates', f'crate_{crate_num}.txt')
        try:
            with open(filename, 'w') as f:
                for hwm_input in self.hwm_inputs:
                    f.write(hwm_input.text().strip() + '\n')
            with open(os.path.join(ber_dashboard_dir, 'prev_data', 'prev_crate.txt'), 'w') as f:
                f.write(crate_num)
            logging.info(f"Crate HWM saved to {filename}")
            self.load_available_crates()  # refresh the dropdown to include the new crate
        except Exception as e:
            logging.error(f"Could not save crate HWM: {str(e)}. Be sure the working directory is set to be `ber_dashboard` and that the `prev_data/crates/` directory exists.")

    def load_crate_hwm(self, text):
        if text.startswith("Crate "):
            crate_num = text[len("Crate "):]
            try:
                with open(os.path.join(ber_dashboard_dir, 'prev_data', 'crates', f'crate_{crate_num}.txt'), 'r') as f:
                    lines = f.read().splitlines()
                    self.crate.setText(crate_num)
                    for i, line in enumerate(lines):
                        if i < len(self.hwm_inputs):
                            self.hwm_inputs[i].setText(line.strip())
            except Exception as e:
                print(f"Could not load crate HWM: {str(e)}. Be sure the working directory is set to be `ber_dashboard` and that the `prev_data/crates/` directory exists.")
            if crate_num:
                self.state.crate = crate_num


    def load_available_crates(self):
        # look in prev_data/crates/ for files of the form 'crate_XXX.txt', where XXX is the crate number. For each file, add an option to the dropdown with the name 'Crate XXX'
        self.crate_dropdown.clear()
        self.crate_dropdown.addItem("Select a crate")
        try:
            for filename in os.listdir(os.path.join(ber_dashboard_dir, 'prev_data', 'crates')):
                if filename.startswith('crate_') and filename.endswith('.txt'):
                    crate_num = filename[len('crate_'):-len('.txt')]
                    self.crate_dropdown.addItem(f"Crate {crate_num}")
        except Exception as e:
            print(f"Could not load available crates: {str(e)}. Be sure the working directory is set to be `ber_dashboard` and that the `prev_data/crates/` directory exists.")

    def clear_hwm_fields(self):
        for hwm_input in self.hwm_inputs:
            hwm_input.clear()
        self.crate.clear()

    # ------------------------------------------------------------------ auto-discovery

    def discover(self):
        """Listen 3 s for board beacons in a worker thread, then list the crates found."""
        self.discover_btn.setEnabled(False)
        self.log.append("Listening for board beacons (3 s)...")

        def job(log, **_):
            return collect_beacons(3, on_new=lambda b: log(
                f"  SN{b.board_serial} {b.address}  crate {b.crate_part or '?'} SN{b.crate_serial or '?'}  "
                f"slot {b.slot if isinstance(b.slot, int) else '?'}  ({'running' if _running(b) else 'U-Boot'})"))

        self._discovery = TaskWorker(job)
        self._discovery.log.connect(self.log.append)
        self._discovery.done.connect(self._discovered)
        self._discovery.failed.connect(lambda tb: self.log.append("Discovery failed:\n" + tb))
        self._discovery.finished.connect(lambda: self.discover_btn.setEnabled(True))
        self._discovery.start()

    def _discovered(self, beacons):
        self.beacons = beacons
        crates = {}
        for b in beacons:
            crates.setdefault(b.crate_serial or "?", []).append(b)
        self.found_crates.blockSignals(True)
        self.found_crates.clear()
        for serial, bs in sorted(crates.items(), key=lambda kv: -len(kv[1])):
            part = next((b.crate_part for b in bs if b.crate_part), "crate")
            n_run = sum(_running(b) for b in bs)
            self.found_crates.addItem(f"{part} SN{serial}: {len(bs)} boards ({n_run} running, {len(bs) - n_run} in U-Boot)",
                                      userData=serial)
        self.found_crates.setCurrentIndex(0)          # the crate with the most boards; the placeholder selects nothing
        self.found_crates.blockSignals(False)
        self.log.append(f"Found {len(beacons)} boards in {len(crates)} crate(s).")
        self.fill_from_discovery()

    def fill_from_discovery(self, *_):
        """Put the discovered boards of the selected crate in their slot fields."""
        serial = self.found_crates.currentData()
        if serial is None:
            return
        use = [b for b in self.beacons if (b.crate_serial or "?") == serial
               and (self.include_uboot.isChecked() or _running(b))]
        self.clear_hwm_fields()
        placed, skipped = [], []
        for b in sorted(use, key=lambda b: b.slot if isinstance(b.slot, int) else 99):
            if isinstance(b.slot, int) and 1 <= b.slot <= len(self.hwm_inputs) and b.board_serial:
                self.hwm_inputs[b.slot - 1].setText(b.board_serial.lstrip("0") or "0")
                placed.append(f"{b.slot}:{b.board_serial}")
            else:
                skipped.append(str(b.board_serial))
        self.crate.setText(serial)
        self.state.crate = serial
        n_off = sum(1 for b in self.beacons if (b.crate_serial or "?") == serial) - len(use)
        self.log.append(f"Crate SN{serial}: slots filled {', '.join(placed) or '(none)'}"
                        + (f"; {n_off} board(s) in U-Boot not included" if n_off else "")
                        + (f"; no slot for {', '.join(skipped)}" if skipped else ""))


def _running(beacon):
    """True for a board running TCPipe (FPGA platform firmware); False for a board in U-Boot."""
    return (beacon.kind or "").upper() == "TCPIPE"
