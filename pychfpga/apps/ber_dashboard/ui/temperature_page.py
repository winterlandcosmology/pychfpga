# DOESN'T WORK WELL... kind of abandoned this one (also the corresponding worker)

import time
import numpy as np
import pyqtgraph as pg

from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QPushButton
from ..workers.temperature_worker import TemperatureWorker


class TemperaturePage(QWidget):
    def __init__(self, state):
        super().__init__()
        self.state = state

        self.start_time = None

        self.layout = QVBoxLayout(self)

        # -------- data buffer --------
        self.temps = []
        self.max_points = 300
        self.crs_sins = None

        # -------- controls --------
        controls = QHBoxLayout()

        self.start_btn = QPushButton("Start Monitoring")
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setEnabled(False)

        controls.addWidget(self.start_btn)
        controls.addWidget(self.stop_btn)

        self.layout.addLayout(controls)

        self.start_btn.clicked.connect(self.start_monitoring)
        self.stop_btn.clicked.connect(self.stop_monitoring)

        # -------- pyqtgraph graphics layout --------
        self.graphics = pg.GraphicsLayoutWidget()
        self.layout.addWidget(self.graphics)

        # plot
        self.plot = self.graphics.addPlot(row=0, col=0)
        self.img = pg.ImageItem()
        self.plot.addItem(self.img)

        # colormap
        self.cmap = pg.colormap.get("inferno")
        self.img.setLookupTable(self.cmap.getLookupTable())

        # colorbar (correct way)
        self.img.setLevels([20, 100])  # set colorbar range
        self.color_bar = pg.ColorBarItem(values=(20, 100), cmap=self.cmap)
        self.color_bar.setImageItem(self.img)
        self.graphics.addItem(self.color_bar, row=0, col=1)

        # labels
        self.plot.setLabel("left", "Time (s)")
        self.plot.setLabel("bottom", "CRS Serial")

        self.plot.getViewBox().setAspectLocked(False)

        # -------- worker --------
        self.worker = None

        # throttle (~10 FPS)
        self._last_update = 0

    # -------- controls --------
    def start_monitoring(self):
        if not hasattr(self.state, "ca") or not self.state.ca:
            print("CA not initialized")
            return

        if self.worker and self.worker.isRunning():
            return

        self.temps.clear()

        # get CRS serials
        self.crs_sins = list(self.state.ca.ib.serial)

        # set x-axis ticks ONCE
        axis = self.plot.getAxis("bottom")
        ticks = [(i+0.5, str(sn)) for i, sn in enumerate(self.crs_sins)]
        axis.setTicks([ticks])
        


        # set limits ONCE
        self.plot.setLimits(
            xMin=0,
            xMax=len(self.crs_sins),
            yMin=0,
            yMax=self.max_points
        )

        # start worker
        self.start_time = time.time()
        self.worker = TemperatureWorker(self.state.ca.ib)
        self.worker.new_data.connect(self.update_plot)
        self.worker.start()

        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)

    def stop_monitoring(self):
        if self.worker:
            self.worker.stop()
            self.worker.wait()
            self.worker = None

        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)

    # -------- plotting --------
    def update_plot(self, temp_list):
        # throttle updates
        now = time.time()
        if now - self._last_update < 0.1:
            return
        self._last_update = now

        # guard bad data
        if not temp_list or any(t is None for t in temp_list):
            return

        if self.temps and len(temp_list) != len(self.temps[0]):
            return

        self.temps.append(temp_list)
        if len(self.temps) > self.max_points:
            self.temps.pop(0)

        time_elapsed = now - self.start_time
        # set y-axis ticks every 30s
        axis = self.plot.getAxis("left")
        ticks = [(i, f"{int(time_elapsed - i)}s ago") for i in range(0, self.max_points, 30)]
        axis.setTicks([ticks])

        data = np.array(self.temps, dtype=float)

        if data.ndim != 2:
            return

        # transpose + flip so time flows top → bottom
        data = data.T[::-1]

        self.img.setImage(data, autoLevels=False)