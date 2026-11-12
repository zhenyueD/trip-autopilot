"""D1-D2-only provider fuse over the existing dedicated SQLite ledger."""
import json

from .autopilot_budget import PersistentBudget
from .autopilot_contract import fail
from .autopilot_service_transactions import ServiceStore
from .phase1_budget_context import BudgetExhausted

EXPERIMENT_ID = 'expert-d1d2-20261024'
AUTHORIZED_PROVIDER_REQUESTS = 12
MAX_PROVIDER_REQUESTS = AUTHORIZED_PROVIDER_REQUESTS
STOP_FIELD = 'experiment_budget_stop'
LEGACY_POLICY_FIELD = 'experiment_budget_policy_v2'
POLICY_FIELD = 'experiment_budget_policy_v3'
AUTHORIZATION = 'user-approved-total12-continuation-20261024'


class ExperimentStore(ServiceStore):
    """Terminal unknown usage retains its reservation under request-count policy."""

    def _usage_ready(self, db, run):
        rows = db.execute('SELECT data FROM autopilot_model_calls WHERE run_id=?', (run['id'],))
        if any(not _terminal(json.loads(row[0])) for row in rows):
            fail('USAGE_UNKNOWN')
        if any(value > run['limits'][key] for key, value in run['usage'].items()):
            fail('BUDGET_EXCEEDED')


def _terminal(call):
    return call['status'] == 'settled' or (call['status'] == 'unknown'
                                         and call.get('finished_at') is not None)


class ExperimentBudget(PersistentBudget):
    """Caller owns dedicated DB isolation; no old budget is resumed or imported."""

    def _state(self, db):
        calls = [json.loads(row[0]) for row in db.execute(
            'SELECT data FROM autopilot_model_calls')]
        runs = [json.loads(row[0]) for row in db.execute('SELECT data FROM autopilot_runs')]
        stops = [value for run in runs for value in
                 (run.get(STOP_FIELD), run.get(LEGACY_POLICY_FIELD, {}).get('stop'),
                  run.get(POLICY_FIELD, {}).get('stop'))
                 if value and value.get('experiment_id') == EXPERIMENT_ID]
        return calls, stops

    @staticmethod
    def _active_stop(stops):
        return next((stop for stop in stops
                     if stop['reason'] == 'EXPERIMENT_REQUEST_LIMIT'
                     and stop.get('authorization') == AUTHORIZATION), None)

    def _record_policy(self, db, stopped=False):
        run = self.store._load(db, 'autopilot_runs', self.run_id)
        policy = run.setdefault(POLICY_FIELD, dict(experiment_id=EXPERIMENT_ID,
            version=3, authorization=AUTHORIZATION, max_provider_requests=MAX_PROVIDER_REQUESTS,
            mode='provider_request_count', terminal_unknown='retain_reservation',
            permanent_stop='EXPERIMENT_REQUEST_LIMIT', at=self.store.clock()))
        if stopped:
            policy.setdefault('stop', dict(experiment_id=EXPERIMENT_ID,
                reason='EXPERIMENT_REQUEST_LIMIT', authorization=AUTHORIZATION,
                max_provider_requests=MAX_PROVIDER_REQUESTS, at=self.store.clock()))
        self.store._save(db, 'autopilot_runs', run)

    def begin_call(self, kind, operation_id=None):
        with self.store.transaction() as db:
            calls, stops = self._state(db)
            stop = self._active_stop(stops)
            reason = stop['reason'] if stop else None
            if kind not in self._READ_KINDS and len(calls) >= MAX_PROVIDER_REQUESTS:
                reason = 'EXPERIMENT_REQUEST_LIMIT'
            if any(not _terminal(call) for call in calls):
                reason = reason or 'EXPERIMENT_USAGE_UNRESOLVED'
            self._record_policy(db, reason == 'EXPERIMENT_REQUEST_LIMIT')
            if not reason:
                lease = super().begin_call(kind, operation_id)
                if kind not in self._READ_KINDS and len(calls) + 1 >= MAX_PROVIDER_REQUESTS:
                    self._record_policy(db, stopped=True)
        if reason:
            error = BudgetExhausted(reason)
            error.code = reason
            raise error
        return lease

    def snapshot(self):
        result = super().snapshot()
        with self.store.transaction() as db:
            calls, stops = self._state(db)
            reads = db.execute('SELECT COUNT(*) FROM autopilot_read_attempts').fetchone()[0]
        result['experiment'] = dict(id=EXPERIMENT_ID,
            authorized_provider_requests=AUTHORIZED_PROVIDER_REQUESTS,
            max_provider_requests=MAX_PROVIDER_REQUESTS, provider_requests=len(calls),
            pending_requests=sum(call['status'] == 'pending' for call in calls),
            unknown_requests=sum(call['status'] == 'unknown' for call in calls),
            read_requests=reads, policy_version=3, authorization=AUTHORIZATION,
            historical_stops=stops, stop=self._active_stop(stops))
        return result
