"""Progress tracking shared by long-running stages (run, grade) and the UI."""
import threading


class Progress:
    def __init__(self, kind=""):
        self.kind = kind
        self.done = 0
        self.total = 0
        self.message = ""
        self.errors = []
        self.failed = 0               # conversations that raised an error (counted in done, but not saved)
        self.state = "idle"            # idle | running | finished | stopped | failed
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    @property
    def should_stop(self):
        return self._stop.is_set()

    def snapshot(self):
        return {"kind": self.kind, "done": self.done, "total": self.total,
                "message": self.message, "state": self.state,
                "errors": self.errors[-5:], "error_count": len(self.errors), "failed": self.failed}
