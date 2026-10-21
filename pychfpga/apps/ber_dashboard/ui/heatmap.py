# ui/heatmap.py

import numpy as np
from PyQt6.QtWidgets import QWidget, QGridLayout, QLabel, QScrollArea, QVBoxLayout
from PyQt6.QtGui import QPixmap, QImage, QColor
from PyQt6.QtCore import Qt

import matplotlib.pyplot as plt
from matplotlib.colors import LogNorm
import io


class HeatmapGrid(QWidget):
    def __init__(self, slots, lanes):
        super().__init__()

        self.slots = slots
        self.lanes = lanes

        self.layout = QVBoxLayout(self)

        self.scroll = QScrollArea()
        self.scroll_widget = QWidget()
        self.grid = QGridLayout(self.scroll_widget)

        self.scroll.setWidget(self.scroll_widget)
        self.scroll.setWidgetResizable(True)

        self.layout.addWidget(self.scroll)

        self.cells = {}

    # ---------- BER → color ----------
    def ber_color(self, ber, ber_floor, vmax):
        if ber is None or np.isnan(ber):
            return (30, 30, 30)

        ber = max(ber, ber_floor)

        norm = (np.log10(ber) - np.log10(ber_floor)) / (
            np.log10(vmax) - np.log10(ber_floor)
        )
        norm = min(max(norm, 0), 1)

        # green → yellow → red
        if norm < 0.5:
            r = int(255 * (norm * 2))
            g = 255
        else:
            r = 255
            g = int(255 * (1 - (norm - 0.5) * 2))

        return (r, g, 0)

    # ---------- create/update heatmap ----------
    def update(self, tx_sn, rx_sn, tx_slot, tx_lane, rx_slot, rx_lane, prerange, postrange, ber_matrix, ber_floor, vmax):
        key = (tx_slot, tx_lane)

        # pixmap = self._matrix_to_image(ber_matrix, ber_floor, vmax)
        pixmap = self._matrix_to_pixmap(
            ber_matrix, ber_floor, vmax, tx_sn, rx_sn, tx_slot, tx_lane, rx_slot, rx_lane, prerange, postrange
        )

        if key in self.cells:
            label = self.cells[key]
            label.setPixmap(pixmap)
            return

        label = QLabel()
        label.setPixmap(pixmap)

        title = QLabel(f"TX {tx_slot}:{tx_lane}")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)

        container = QWidget()
        v = QVBoxLayout(container)
        v.addWidget(title)
        v.addWidget(label)

        row = tx_slot - 1
        col = tx_lane

        self.grid.addWidget(container, row, col)

        self.cells[key] = label

    # ---------- convert BER matrix to QImage ----------
    def _matrix_to_image(self, Z, ber_floor, vmax, size=180):
        Z = np.clip(Z, ber_floor, None)

        h, w = Z.shape
        img = QImage(w, h, QImage.Format.Format_RGB888)

        for i in range(h):
            for j in range(w):
                ber = Z[i, j]

                if np.isnan(ber):
                    r, g, b = (40, 40, 40)
                else:
                    norm = (np.log10(ber) - np.log10(ber_floor)) / (
                        np.log10(vmax) - np.log10(ber_floor)
                    )
                    norm = min(max(norm, 0), 1)

                    if norm < 0.5:
                        r = int(255 * (norm * 2))
                        g = 255
                    else:
                        r = 255
                        g = int(255 * (1 - (norm - 0.5) * 2))

                    b = 0

                img.setPixel(j, i, QColor(r, g, b).rgb())

        return QPixmap.fromImage(img).scaled(
            size, size,
            Qt.AspectRatioMode.KeepAspectRatio
        )
    
    def clear(self):
        # Remove all widgets from the grid
        while self.grid.count():
            item = self.grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.cells.clear()

    def _matrix_to_pixmap(self, Z, ber_floor, vmax, tx_sn, rx_sn, tx_slot, tx_lane, rx_slot, rx_lane, prerange, postrange):

        Z = np.clip(Z, ber_floor, None)

        fig, ax = plt.subplots(figsize=(6, 6), dpi=100)

        im = ax.imshow(
            Z,
            cmap="viridis",
            origin="lower",
            norm=LogNorm(vmin=ber_floor, vmax=vmax),
            aspect="equal",
            extent=[-0.5, Z.shape[1] - 0.5, -0.5, Z.shape[0] - 0.5],
        )

        ax.set_xticks(np.arange(len(postrange)))
        ax.set_yticks(np.arange(len(prerange)))

        ax.set_xticklabels(postrange)
        ax.set_yticklabels(prerange)

        post_range = np.arange(Z.shape[1])
        pre_range = np.arange(Z.shape[0])
        zy, zx = np.where(Z == ber_floor)
        print(ber_floor, zx, zy)
        if zx.size:
            posts = [post_range[j] for j in zx]
            pres  = [pre_range[i] for i in zy]
            ax.scatter(posts, pres, facecolors='none', edgecolors='purple', linewidths=1.2, zorder=10)

        # --- labels ---
        ax.set_title(f"TX ({tx_slot}:{tx_lane}) - RX ({rx_slot}:{rx_lane})\nSN{tx_sn} → SN{rx_sn}")
        ax.set_xlabel("POST")
        ax.set_ylabel("PRE")

        # --- colorbar ---
        cbar = plt.colorbar(im, ax=ax)
        cbar.set_label("BER")


        # --- layout ---
        plt.tight_layout()

        # --- convert to QPixmap ---
        buf = io.BytesIO()
        plt.savefig(buf, format="png")
        plt.close(fig)

        buf.seek(0)

        img = QImage()
        img.loadFromData(buf.getvalue())

        return QPixmap.fromImage(img)