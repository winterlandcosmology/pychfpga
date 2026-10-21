# ui/dashboard.py
import os
import sys

from PyQt6.QtWidgets import *
from ..state import AppState


from .boot_page import BootPage
from .map_page import MapPage
from .sweep_page import SweepPage

class Dashboard(QWidget):
    def __init__(self):
        super().__init__()

        self.state = AppState()

        layout = QVBoxLayout()
        nav = QHBoxLayout()

        self.stack = QStackedWidget()

        self.pages = [
            BootPage(self.state),
            MapPage(self.state),
            SweepPage(self.state)
            # TemperaturePage(self.state)
        ]

        for p in self.pages:
            self.stack.addWidget(p)

        for i, name in enumerate(["1. Boot", "2. Map", "3. Sweep"]): 
            btn = QPushButton(name)
            btn.clicked.connect(lambda _, x=i: self.stack.setCurrentIndex(x))
            nav.addWidget(btn)

        layout.addLayout(nav)
        layout.addWidget(self.stack)

        self.setLayout(layout)