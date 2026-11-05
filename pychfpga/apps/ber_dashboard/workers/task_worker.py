import traceback

from PyQt6.QtCore import QThread, pyqtSignal


class TaskWorker(QThread):
    """Run fn(log=..., progress=..., partial=..., stop=...) in a thread; emit its result or the error."""
    log = pyqtSignal(str)
    progress = pyqtSignal(int, int)
    partial = pyqtSignal(object)
    done = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, fn):
        super().__init__()
        self.fn = fn
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        try:
            result = self.fn(log=self.log.emit, progress=self.progress.emit, partial=self.partial.emit,
                             stop=lambda: self._stop)
        except Exception:
            self.failed.emit(traceback.format_exc())
            return
        self.done.emit(result)
