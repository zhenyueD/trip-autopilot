"""Application-owned Monday sampler; humans perform every actual audit verdict."""
from datetime import datetime
import threading

from .uncertainty_audit import UncertaintyAudit


class WeeklyAuditSampler:
    def __init__(self, store, *, interval=60):
        self.audit = UncertaintyAudit(store)
        self.interval = interval
        self._stop = threading.Event()
        self.last_error = None

    def tick(self):
        at = datetime.fromisoformat(self.audit.store.clock().replace('Z', '+00:00'))
        if at.weekday() == 0:
            return self.audit.sample_week()  # Persisted weekly key makes restarts idempotent.
        return None

    def start(self):
        def run():
            while not self._stop.is_set():
                try:
                    self.tick()
                    self.last_error = None
                except Exception as error:
                    self.last_error = type(error).__name__  # No private payload in a scheduler log.
                self._stop.wait(self.interval)
        thread = threading.Thread(target=run, name='uncertainty-weekly-sample', daemon=True)
        thread.start()
        return self

    def stop(self):
        self._stop.set()
