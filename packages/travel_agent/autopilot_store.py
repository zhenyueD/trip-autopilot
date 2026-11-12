"""Durable user goals and fenced runs in the existing TripStore SQLite DB.

Only authenticated user routes may create/update goals. Worker APIs are trusted
server interfaces, not model tools. External calls always happen outside a DB
transaction. The caller must preserve that authority boundary when integrating.
"""
import hmac
import json
import secrets
import time
from contextlib import contextmanager
from uuid import uuid4

from .autopilot_contract import (RUN_TERMINAL, canonical, duration,
                                  fail, fingerprint, integer, public_run, text)
from .autopilot_operations import OperationMethods


class AutopilotStore(OperationMethods):
    def __init__(self, trip_store, clock=time.time):
        self.trip_store = trip_store
        self.clock = clock
        with self.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS autopilot_goals (id TEXT PRIMARY KEY, data TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS autopilot_runs (id TEXT PRIMARY KEY, goal_id TEXT NOT NULL, request_key TEXT NOT NULL, data TEXT NOT NULL, UNIQUE(goal_id, request_key))')
            db.execute('CREATE TABLE IF NOT EXISTS autopilot_operations (id TEXT PRIMARY KEY, run_id TEXT NOT NULL, call_id TEXT NOT NULL, data TEXT NOT NULL, UNIQUE(run_id, call_id))')
            db.execute('CREATE TABLE IF NOT EXISTS autopilot_model_calls (id TEXT PRIMARY KEY, run_id TEXT NOT NULL, call_id TEXT NOT NULL, data TEXT NOT NULL, UNIQUE(run_id, call_id))')
            db.execute('CREATE TABLE IF NOT EXISTS autopilot_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, message_id TEXT NOT NULL, data TEXT NOT NULL, UNIQUE(run_id, message_id))')

    @contextmanager
    def transaction(self, db=None):
        if db is not None:
            actual = db.execute('PRAGMA database_list').fetchone()[2]
            if not db.in_transaction or actual != str(self.trip_store.path.resolve()):
                fail('INVALID_TRANSACTION', 400)
            yield db
            return
        with self.trip_store.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            yield connection

    def _load(self, db, table, object_id):
        # table names are internal constants, never model/user arguments.
        row = db.execute(f'SELECT data FROM {table} WHERE id=?', (object_id,)).fetchone()
        if row is None:
            fail('NOT_FOUND', 404)
        return json.loads(row[0])

    def _save(self, db, table, value):
        db.execute(f'UPDATE {table} SET data=? WHERE id=?', (canonical(value), value['id']))

    def _trip(self, db, trip_id, token):
        trip = self.trip_store._load(db, trip_id)
        self.trip_store._authorize(trip, token)
        return trip

    def _goal_for_user(self, db, goal_id, token):
        goal = self._load(db, 'autopilot_goals', goal_id)
        self._trip(db, goal['trip_id'], token)
        return goal

    def create_goal(self, trip_id, token, objective, success_criteria, constraints,
                    authorization, expected_revision):
        from .autopilot_contract import authorization as validate_authorization
        text(objective)
        self._validate_goal_fields(success_criteria, constraints)
        auth = validate_authorization(authorization)
        integer(expected_revision, 1)
        with self.transaction() as db:
            trip = self._trip(db, trip_id, token)
            if trip['revision'] != expected_revision:
                fail('VERSION_CONFLICT')
            goal = dict(id=uuid4().hex, trip_id=trip_id, version=1, status='active',
                        objective=objective, success_criteria=success_criteria,
                        constraints=constraints, authorization=auth, created_at=self.clock())
            db.execute('INSERT INTO autopilot_goals VALUES (?,?)', (goal['id'], canonical(goal)))
            return json.loads(canonical(goal))

    def _validate_goal_fields(self, criteria, constraints):
        if not isinstance(criteria, list) or not criteria or not isinstance(constraints, dict):
            fail('INVALID_ARGUMENT', 400)
        for criterion in criteria:
            text(criterion)
        canonical(constraints)

    def get_goal(self, goal_id, token):
        with self.transaction() as db:
            return self._goal_for_user(db, goal_id, token)

    def update_goal(self, goal_id, token, expected_goal_version, *, objective=None,
                    success_criteria=None, constraints=None, authorization=None, status=None):
        from .autopilot_contract import authorization as validate_authorization
        integer(expected_goal_version, 1)
        with self.transaction() as db:
            goal = self._goal_for_user(db, goal_id, token)
            if goal['version'] != expected_goal_version:
                fail('GOAL_VERSION_CONFLICT')
            if objective is not None:
                goal['objective'] = text(objective)
            if success_criteria is not None:
                goal['success_criteria'] = success_criteria
            if constraints is not None:
                goal['constraints'] = constraints
            self._validate_goal_fields(goal['success_criteria'], goal['constraints'])
            if authorization is not None:
                goal['authorization'] = validate_authorization(authorization)
            if status is not None:
                if status not in {'active', 'completed', 'cancelled'}:
                    fail('INVALID_ARGUMENT', 400)
                goal['status'] = status
            goal['version'] += 1
            self._save(db, 'autopilot_goals', goal)
            return goal

    def start_run(self, goal_id, token, event, limits, idempotency_key):
        with self.transaction() as db:
            goal = self._goal_for_user(db, goal_id, token)
            return self._start_authorized_run(db, goal, event, limits, idempotency_key)

    def _start_authorized_run(self, db, goal, event, limits, idempotency_key):
        """Caller already verified user or job authority in this transaction.

        Not a model tool/API. Scheduler must first validate its job's current
        permission/hash, goal version, claim secret/generation and expiry.
        """
        from .autopilot_contract import limits as validate_limits
        text(idempotency_key)
        request_hash = fingerprint({'event': event, 'limits': limits})
        with self.transaction(db) as db:
            current = self._load(db, 'autopilot_goals', goal['id'])
            if current['version'] != goal['version']:
                fail('GOAL_VERSION_CONFLICT')
            goal = current
            goal_id = goal['id']
            trip = self.trip_store._load(db, goal['trip_id'])
            row = db.execute('SELECT data FROM autopilot_runs WHERE goal_id=? AND request_key=?',
                             (goal_id, idempotency_key)).fetchone()
            if row:
                run = json.loads(row[0])
                if run['request_hash'] != request_hash:
                    fail('IDEMPOTENCY_CONFLICT')
                return public_run(run, self.clock())
            if goal['status'] != 'active':
                fail('GOAL_NOT_ACTIVE')
            budget = validate_limits(limits, goal['authorization']['limits'])
            now = self.clock()
            run = dict(id=uuid4().hex, trip_id=trip['id'], goal_id=goal_id,
                       goal_version=goal['version'], token_hash=trip['token_hash'],
                       request_hash=request_hash, status='pending', event=event,
                       limits=budget, usage=dict(model_calls=0, input_tokens=0, output_tokens=0, tool_calls=0),
                       started_at=now, deadline=now + budget['wall_seconds'], lease_generation=0,
                       lease_worker=None, lease_secret=None, lease_until=0, result=None)
            db.execute('INSERT INTO autopilot_runs VALUES (?,?,?,?)',
                       (run['id'], goal_id, idempotency_key, canonical(run)))
            return public_run(run, self.clock())

    def get_run(self, run_id, token):
        with self.transaction() as db:
            run = self._load(db, 'autopilot_runs', run_id)
            self._trip(db, run['trip_id'], token)
            return public_run(run, self.clock())

    def _current(self, db, run_id):
        run = self._load(db, 'autopilot_runs', run_id)
        trip = self.trip_store._load(db, run['trip_id'])
        if not hmac.compare_digest(trip['token_hash'], run['token_hash']):
            fail('AUTHORIZATION_REVOKED', 403)
        goal = self._load(db, 'autopilot_goals', run['goal_id'])
        if goal['version'] != run['goal_version']:
            fail('GOAL_VERSION_CONFLICT')
        if goal['status'] != 'active':
            fail('GOAL_NOT_ACTIVE')
        return run

    def internal_get_run(self, run_id):
        with self.transaction() as db:
            return self._current(db, run_id)

    def _active(self, run):
        if run['status'] not in {'pending', 'running'}:
            fail('RUN_NOT_ACTIVE')

    def _wall_budget(self, run):
        if self.clock() >= run['deadline']:
            fail('BUDGET_EXCEEDED')

    def _lease(self, db, run_id, lease):
        run = self._current(db, run_id)
        self._active(run)
        if (not isinstance(lease, dict) or lease.get('generation') != run['lease_generation']
                or lease.get('worker_id') != run['lease_worker']
                or not isinstance(lease.get('secret'), str)
                or not hmac.compare_digest(lease['secret'], run['lease_secret'] or '')
                or self.clock() >= run['lease_until']):
            fail('LEASE_LOST')
        return run

    def claim_run(self, run_id, worker_id, lease_seconds=60):
        text(worker_id)
        duration(lease_seconds)
        with self.transaction() as db:
            run = self._current(db, run_id)
            self._active(run)
            self._wall_budget(run)
            if self.clock() < run['lease_until']:
                fail('LEASE_BUSY')
            run.update(status='running', lease_worker=worker_id, lease_secret=secrets.token_hex(24),
                       lease_generation=run['lease_generation'] + 1, lease_until=self.clock() + lease_seconds)
            self._save(db, 'autopilot_runs', run)
            return dict(worker_id=worker_id, generation=run['lease_generation'], secret=run['lease_secret'])

    def renew_run(self, run_id, lease, lease_seconds=60):
        duration(lease_seconds)
        with self.transaction() as db:
            run = self._lease(db, run_id, lease)
            self._wall_budget(run)
            run['lease_until'] = self.clock() + lease_seconds
            self._save(db, 'autopilot_runs', run)
            return dict(lease)

    def finish_run(self, run_id, lease, status, result=None):
        if status not in RUN_TERMINAL:
            fail('INVALID_ARGUMENT', 400)
        with self.transaction() as db:
            run = self._lease(db, run_id, lease)
            if status == 'completed':
                self._usage_ready(db, run)
                rows = db.execute('SELECT data FROM autopilot_operations WHERE run_id=?', (run_id,))
                if any(json.loads(row[0])['status'] in {'in_flight', 'unknown'} for row in rows):
                    fail('OPERATION_UNKNOWN')
            run.update(status=status, result=result, finished_at=self.clock())
            self._save(db, 'autopilot_runs', run)
            return public_run(run, self.clock())

    def assert_dispatch(self, run_id, lease, *, db=None):
        with self.transaction(db) as connection:
            run = self._lease(connection, run_id, lease)
            self._wall_budget(run)
            self._usage_ready(connection, run)
            return run

    def append_message(self, run_id, lease, role, content, message_id):
        text(message_id)
        if role not in {'user', 'assistant', 'tool', 'system'}:
            fail('INVALID_ARGUMENT', 400)
        message = dict(role=role, content=content, message_id=message_id)
        with self.transaction() as db:
            self._lease(db, run_id, lease)
            row = db.execute('SELECT data FROM autopilot_messages WHERE run_id=? AND message_id=?',
                             (run_id, message_id)).fetchone()
            if row:
                if canonical(message) != row[0]:
                    fail('IDEMPOTENCY_CONFLICT')
                return json.loads(row[0])
            db.execute('INSERT INTO autopilot_messages(run_id,message_id,data) VALUES (?,?,?)',
                       (run_id, message_id, canonical(message)))
            return message

    def get_messages(self, run_id, token):
        with self.transaction() as db:
            run = self._load(db, 'autopilot_runs', run_id)
            self._trip(db, run['trip_id'], token)
            return [json.loads(row[0]) for row in db.execute(
                'SELECT data FROM autopilot_messages WHERE run_id=? ORDER BY id', (run_id,))]

    def internal_get_messages(self, run_id, lease):
        """Trusted recovery read, fenced by the current run lease and authority."""
        with self.transaction() as db:
            self._lease(db, run_id, lease)
            return [json.loads(row[0]) for row in db.execute(
                'SELECT data FROM autopilot_messages WHERE run_id=? ORDER BY id', (run_id,))]

    def internal_get_goal(self, goal_id):
        """Trusted worker read. Never expose on an unauthenticated route."""
        with self.transaction() as db:
            goal = self._load(db, 'autopilot_goals', goal_id)
            self.trip_store._load(db, goal['trip_id'])
            return goal

    def list_runnable_runs(self, now=None, limit=100):
        """Trusted scheduler hint; claim_run rechecks all volatile conditions."""
        from .store import StateError
        integer(limit, 1)
        if limit > 1000:
            fail('INVALID_ARGUMENT', 400)
        now = self.clock() if now is None else now
        result = []
        with self.transaction() as db:
            for row in db.execute('SELECT data FROM autopilot_runs ORDER BY rowid'):
                run = json.loads(row[0])
                if (run['status'] not in {'pending', 'running'} or now < run['lease_until']
                        or now >= run['deadline']):
                    continue
                try:
                    self._current(db, run['id'])
                except StateError:
                    continue
                result.append(public_run(run, self.clock()))
                if len(result) == limit:
                    break
        return result
