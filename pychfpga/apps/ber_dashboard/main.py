"""BER test dashboard (PyQt6). Run: python -m pychfpga.apps.ber_dashboard"""
import sys

from PyQt6.QtWidgets import QApplication

from .ui.dashboard import Dashboard


def main():
    app = QApplication(sys.argv)
    win = Dashboard()
    win.setWindowTitle("BER Testing UI")
    win.showMaximized()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
