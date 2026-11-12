"""Thread/context-local composition of existing state methods into one transaction."""
from contextlib import contextmanager
from contextvars import ContextVar
from .autopilot_store import AutopilotStore


class ServiceStore(AutopilotStore):
    def __init__(self, trip_store, **kwargs):
        self._transaction_context = ContextVar('autopilot_service_transaction', default=None)
        super().__init__(trip_store, **kwargs)

    @contextmanager
    def transaction(self, db=None):
        current = self._transaction_context.get()
        if db is not None or current is not None:
            with super().transaction(db if db is not None else current) as connection:
                yield connection
            return
        with super().transaction() as connection:
            marker = self._transaction_context.set(connection)
            try:
                yield connection
            finally:
                self._transaction_context.reset(marker)
