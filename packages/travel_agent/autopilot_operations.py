"""Durable operation receipts and physical model-call reservations."""
import json
from uuid import uuid4

from .autopilot_contract import canonical, fail, fingerprint, integer, text


class OperationMethods:
    def _by_call(self, db, table, run_id, call_id):
        row = db.execute(f'SELECT data FROM {table} WHERE run_id=? AND call_id=?',
                         (run_id, call_id)).fetchone()
        return json.loads(row[0]) if row else None

    def _usage_ready(self, db, run):
        rows = db.execute('SELECT data FROM autopilot_model_calls WHERE run_id=?', (run['id'],))
        if any(json.loads(row[0])['status'] != 'settled' for row in rows):
            fail('USAGE_UNKNOWN')
        if any(value > run['limits'][key] for key, value in run['usage'].items()):
            fail('BUDGET_EXCEEDED')

    def _charge(self, run, delta):
        for key, value in delta.items():
            if run['usage'][key] + value > run['limits'][key]:
                fail('BUDGET_EXCEEDED')
        for key, value in delta.items():
            run['usage'][key] += value

    def reserve_operation(self, run_id, lease, call_id, name, arguments, *, db=None):
        text(call_id)
        text(name)
        request_hash = fingerprint({'name': name, 'arguments': arguments})
        with self.transaction(db) as connection:
            run = self._lease(connection, run_id, lease)
            existing = self._by_call(connection, 'autopilot_operations', run_id, call_id)
            if existing:
                if existing['request_hash'] != request_hash:
                    fail('IDEMPOTENCY_CONFLICT')
                disposition = 'replay' if existing['status'] == 'succeeded' else 'unknown'
                return dict(disposition=disposition, operation=existing)
            self.assert_dispatch(run_id, lease, db=connection)
            goal = self._load(connection, 'autopilot_goals', run['goal_id'])
            if name not in goal['authorization']['tools']:
                fail('TOOL_NOT_AUTHORIZED', 403)
            self._charge(run, {'tool_calls': 1})
            operation = dict(id=uuid4().hex, run_id=run_id, call_id=call_id, name=name,
                             arguments=arguments, request_hash=request_hash, status='in_flight',
                             result=None, created_at=self.clock(), lease_generation=run['lease_generation'])
            connection.execute('INSERT INTO autopilot_operations VALUES (?,?,?,?)',
                               (operation['id'], run_id, call_id, canonical(operation)))
            self._save(connection, 'autopilot_runs', run)
            return dict(disposition='execute', operation=operation)

    def finish_operation(self, run_id, lease, operation_id, result, *, status='succeeded', db=None):
        if status not in {'succeeded', 'failed', 'unknown'}:
            fail('INVALID_ARGUMENT', 400)
        with self.transaction(db) as connection:
            self._lease(connection, run_id, lease)
            operation = self._load(connection, 'autopilot_operations', operation_id)
            if operation['run_id'] != run_id:
                fail('NOT_FOUND', 404)
            if operation['status'] != 'in_flight':
                if operation['status'] == status and canonical(operation['result']) == canonical(result):
                    return operation
                fail('OPERATION_CONFLICT')
            # A replacement worker cannot guess what an abandoned dispatch did.
            if operation['lease_generation'] != lease['generation']:
                fail('OPERATION_UNKNOWN')
            operation.update(status=status, result=result, finished_at=self.clock())
            self._save(connection, 'autopilot_operations', operation)
            return operation

    def record_research_request(self, run_id, lease, operation_id, request):
        """Save the prepared outbound query before HTTP; this is not proof of delivery."""
        with self.transaction() as db:
            self._lease(db, run_id, lease)
            operation = self._load(db, 'autopilot_operations', operation_id)
            if operation['run_id'] != run_id:
                fail('NOT_FOUND', 404)
            if (operation['name'] != 'research_fact' or operation['status'] != 'in_flight'
                    or operation['lease_generation'] != lease['generation']):
                fail('OPERATION_CONFLICT')
            previous = operation.get('outbound_request')
            if previous is not None and canonical(previous) != canonical(request):
                fail('OPERATION_CONFLICT')
            operation['outbound_request'] = request
            self._save(db, 'autopilot_operations', operation)

    def get_operation(self, run_id, token, call_id):
        with self.transaction() as db:
            run = self._load(db, 'autopilot_runs', run_id)
            self._trip(db, run['trip_id'], token)
            operation = self._by_call(db, 'autopilot_operations', run_id, call_id)
            if operation is None:
                fail('NOT_FOUND', 404)
            return operation

    def internal_get_operation(self, run_id, lease, call_id):
        """Trusted receipt read; a lease for another run grants no access."""
        with self.transaction() as db:
            self._lease(db, run_id, lease)
            operation = self._by_call(db, 'autopilot_operations', run_id, call_id)
            if operation is None:
                fail('NOT_FOUND', 404)
            return operation

    def reserve_model_call(self, run_id, lease, call_id, input_tokens, output_tokens):
        text(call_id)
        integer(input_tokens)
        integer(output_tokens)
        requested = dict(input_tokens=input_tokens, output_tokens=output_tokens)
        with self.transaction() as db:
            run = self._lease(db, run_id, lease)
            existing = self._by_call(db, 'autopilot_model_calls', run_id, call_id)
            if existing:
                if existing['reserved'] != requested:
                    fail('IDEMPOTENCY_CONFLICT')
                return dict(disposition='replay' if existing['status'] == 'settled' else 'unknown',
                            reservation=existing)
            self.assert_dispatch(run_id, lease, db=db)
            self._charge(run, dict(model_calls=1, **requested))
            reservation = dict(id=uuid4().hex, run_id=run_id, call_id=call_id,
                               reserved=requested, actual=None, status='pending',
                               lease_generation=run['lease_generation'], created_at=self.clock())
            db.execute('INSERT INTO autopilot_model_calls VALUES (?,?,?,?)',
                       (reservation['id'], run_id, call_id, canonical(reservation)))
            self._save(db, 'autopilot_runs', run)
            return dict(disposition='execute', reservation=reservation)

    def settle_usage(self, run_id, lease, call_id, input_tokens, output_tokens):
        actual = {key: None if value is None else integer(value)
                  for key, value in dict(input_tokens=input_tokens, output_tokens=output_tokens).items()}
        known = all(value is not None for value in actual.values())
        with self.transaction() as db:
            run = self._lease(db, run_id, lease)
            reservation = self._by_call(db, 'autopilot_model_calls', run_id, call_id)
            if reservation is None:
                fail('NOT_FOUND', 404)
            if reservation['status'] != 'pending':
                if reservation['actual'] == actual:
                    return reservation
                fail('USAGE_CONFLICT')
            if reservation['lease_generation'] != lease['generation']:
                fail('USAGE_UNKNOWN')
            for key, value in actual.items():
                if value is not None:
                    run['usage'][key] += value - reservation['reserved'][key]
            reservation.update(actual=actual, status='settled' if known else 'unknown',
                               finished_at=self.clock())
            self._save(db, 'autopilot_model_calls', reservation)
            self._save(db, 'autopilot_runs', run)
            return reservation
