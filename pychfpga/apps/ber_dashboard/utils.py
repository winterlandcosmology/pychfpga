# utils.py
import logging
from PyQt6.QtCore import pyqtSignal, QObject

def parse_range(text, step=1, default=None):
    text = text.strip()

    if not text:
        return default or []

    if "-" in text:
        a, b = map(int, text.split("-"))
        step = step
        return list(range(a, b + 1, step))

    return [int(x) for x in text.split(",")]

def constrain_vals(entry, lowerlim=None, upperlim=None):
    if lowerlim is not None:
        if entry < lowerlim:
            return lowerlim
    if upperlim is not None:
        if entry > upperlim: 
            return upperlim
    return entry


# QT logging hanlder
class QtLogEmitter(QObject):
    log_signal = pyqtSignal(str)


class QtLogHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.emitter = QtLogEmitter()

    def emit(self, record):
        msg = self.format(record)
        self.emitter.log_signal.emit(msg)