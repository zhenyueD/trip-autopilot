"""Private durable ledger for the four authorized physical comparison requests."""
from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def now():
    return datetime.now(timezone.utc).isoformat()


class BudgetExhausted(RuntimeError):
    pass


_TOKEN_FIELDS = ('input_tokens', 'output_tokens', 'reasoning_tokens')


def token_usage(usage):
    """Normalize provider counts without replacing unreported values with zero."""
    usage = usage if isinstance(usage, dict) else {}
    details = usage.get('completion_tokens_details') or usage.get('output_tokens_details') or {}
    candidates = (usage.get('input_tokens', usage.get('prompt_tokens')),
                  usage.get('output_tokens', usage.get('completion_tokens')),
                  usage.get('reasoning_tokens', details.get('reasoning_tokens')
                            if isinstance(details, dict) else None))
    return {key: value if type(value) is int and value >= 0 else None
            for key, value in zip(_TOKEN_FIELDS, candidates)}


def _model_name(value):
    return value if isinstance(value, str) and value.strip() else None


def _call_summary(calls):
    counts = [token_usage(call.get('usage')) for call in calls]
    known = {key: sum(row[key] for row in counts if row[key] is not None)
             for key in _TOKEN_FIELDS}
    unknown = {key: sum(row[key] is None for row in counts) for key in _TOKEN_FIELDS}
    return {'calls': calls, 'call_count': len(calls),
            'models': sorted({_model_name(call.get('model')) or _model_name(call.get('requested_model'))
                              for call in calls if _model_name(call.get('model'))
                              or _model_name(call.get('requested_model'))}),
            'token_usage': {key: known[key] if calls and not unknown[key] else None
                            for key in _TOKEN_FIELDS},
            'known_token_totals': known, 'unknown_call_counts': unknown,
            'usage_status': ('not_applicable' if not calls else 'unknown'
                             if any(unknown.values()) else 'known')}


class DurableBudget:
    """Reservations never refund; interrupted attempts stay unknown on restart."""

    def __init__(self, state_dir, *, maximum_requests=4, stop_before_request=None):
        if type(maximum_requests) is not int or maximum_requests < 1:
            raise ValueError('maximum_requests must be a positive integer')
        if stop_before_request is not None and (type(stop_before_request) is not int
                                                or stop_before_request < 1):
            raise ValueError('stop_before_request must be a positive integer or None')
        self.maximum_requests = maximum_requests
        self.stop_before_request = stop_before_request
        self._active_run = ContextVar('budget_run', default=None)
        self._active_stage = ContextVar('budget_stage', default=None)
        self.directory = Path(state_dir).expanduser()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        self.path = self.directory / 'comparison.sqlite3'
        with self.connection() as db:
            db.execute('CREATE TABLE IF NOT EXISTS calls '
                       '(id TEXT PRIMARY KEY, kind TEXT, request_id TEXT, data TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, data TEXT NOT NULL)')
        self.path.chmod(0o600)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.execute('PRAGMA busy_timeout=15000')
        db.execute('PRAGMA synchronous=FULL')
        db.execute('PRAGMA secure_delete=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def reserve(self, kind, request_id=None, *, model=None):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            used = db.execute('SELECT COUNT(*) FROM calls').fetchone()[0]
            if used >= self.maximum_requests:
                raise BudgetExhausted(f'本轮已达 {self.maximum_requests} 次调用上限，不再发起付费请求')
            lease = uuid4().hex
            record = {'id': lease, 'kind': str(kind), 'request_id': request_id,
                      'reserved_at': now(), 'status': 'unknown', 'usage': None,
                      'provider_request_id': None, 'elapsed_ms': None,
                      'run_id': self._active_run.get(), 'stage_id': self._active_stage.get(),
                      'requested_model': _model_name(model), 'model': None,
                      'model_identity_status': 'requested_only',
                      'token_usage': token_usage(None)}
            db.execute('INSERT INTO calls VALUES (?, ?, ?, ?)',
                       (lease, str(kind), request_id, json.dumps(record)))
        return lease

    def record_request(self, lease_id, *, model=None):
        """Attach request identity while retaining legacy reserve overrides."""
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM calls WHERE id=?', (lease_id,)).fetchone()
            if row is None:
                raise ValueError('Unknown reservation')
            record = json.loads(row[0])
            record.update(requested_model=_model_name(model), run_id=self._active_run.get(),
                          stage_id=self._active_stage.get())
            db.execute('UPDATE calls SET data=? WHERE id=?', (json.dumps(record), lease_id))

    def settle(self, lease_id, *, status, usage=None, provider_request_id=None,
               elapsed_ms=None, error=None, metadata=None, model=None):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM calls WHERE id=?', (lease_id,)).fetchone()
            if not row:
                raise ValueError('Unknown reservation')
            record = json.loads(row[0])
            safe_usage = None if usage is None else {
                k: v for k, v in usage.items()
                if isinstance(v, (int, float)) and not isinstance(v, bool)
            }
            record.update(status=str(status), usage=safe_usage,
                          provider_request_id=provider_request_id,
                          elapsed_ms=elapsed_ms, error=error, settled_at=now())
            # Keep nested provider usage needed for reasoning accounting; no estimates.
            for key in ('completion_tokens_details', 'output_tokens_details'):
                if isinstance((usage or {}).get(key), dict):
                    safe_usage[key] = {name: value for name, value in usage[key].items()
                                       if type(value) is int and value >= 0}
            record.update(token_usage=token_usage(safe_usage), model=_model_name(model),
                          model_identity_status='provider_reported' if _model_name(model) else 'requested_only')
            if metadata:
                record['metadata'] = {key: metadata[key] for key in
                                      ('http_status', 'error_reason', 'body_sha256', 'place_id',
                                       'response_evidence', 'provider_error_body_status',
                                       'response_evidence_status')
                                      if key in metadata}
            db.execute('UPDATE calls SET data=? WHERE id=?',
                       (json.dumps(record, ensure_ascii=False), lease_id))

    def snapshot(self):
        with self.connection() as db:
            calls = [json.loads(row[0]) for row in db.execute(
                'SELECT data FROM calls ORDER BY rowid')]
        return {'limit': self.maximum_requests, 'stop_before': self.stop_before_request,
                'used': len(calls), 'remaining': max(0, self.maximum_requests - len(calls)),
                'exhausted': len(calls) >= self.maximum_requests,
                'calls': calls}

    def _update_run(self, run_id, update):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM runs WHERE id=?', (run_id,)).fetchone()
            if row is None:
                raise ValueError('Unknown run')
            record = json.loads(row[0])
            update(record)
            db.execute('UPDATE runs SET data=? WHERE id=?',
                       (json.dumps(record, ensure_ascii=False), run_id))
        self._checkpoint_run(run_id)

    def run_snapshot(self, run_id):
        with self.connection() as db:
            row = db.execute('SELECT data FROM runs WHERE id=?', (run_id,)).fetchone()
            if row is None:
                raise ValueError('Unknown run')
            record = json.loads(row[0])
            calls = [json.loads(row[0]) for row in db.execute('SELECT data FROM calls ORDER BY rowid')]
        calls = [call for call in calls if call.get('run_id') == run_id]
        record.update(_call_summary(calls))
        for stage in record['stages']:
            stage.update(_call_summary([call for call in calls if call.get('stage_id') == stage['id']]))
        return record

    def _checkpoint_run(self, run_id):
        record = self.run_snapshot(run_id)
        private_json(record['artifact_path'], record)

    def _finish_accounting(self, run_id, update, failure):
        try:
            self._update_run(run_id, update)
        except Exception as accounting_error:
            if failure is None:
                raise
            failure.add_note('Run accounting checkpoint failed: ' + type(accounting_error).__name__)

    @contextmanager
    def run(self, run_id, *, kind=None, case_id=None, artifact_path=None):
        """Time a run without spending calls or changing pipeline control flow."""
        if (not isinstance(run_id, str) or not run_id or run_id in ('.', '..')
                or any(value in run_id for value in ('/', '\\', '\x00'))):
            raise ValueError('run_id must be a nonempty path component')
        record = dict(id=run_id, kind=kind, case_id=case_id, started_at=now(),
            status='running', elapsed_ms=None, stages=[],
            artifact_path=str(artifact_path or self.directory / 'runs' / (run_id + '.json')))
        try:
            with self.connection() as db:
                db.execute('INSERT INTO runs VALUES (?, ?)', (run_id, json.dumps(record)))
        except sqlite3.IntegrityError:
            raise ValueError('Run evidence already exists') from None
        self._checkpoint_run(run_id)
        token, started, failure = self._active_run.set(run_id), time.monotonic(), None
        stage_token = self._active_stage.set(None)
        try:
            yield record
        except BaseException as error:
            failure = error
            raise
        finally:
            from .run_reliability import failure_record
            self._active_run.reset(token)
            self._active_stage.reset(stage_token)
            self._finish_accounting(run_id, lambda row: row.update(
                status='failed' if failure else 'completed',
                error=type(failure).__name__ if failure else None,
                failure_accounting=failure_record(failure) if failure else None,
                finished_at=now(), elapsed_ms=round((time.monotonic() - started) * 1000)), failure)

    @contextmanager
    def stage(self, run_id, name):
        """Bind each existing physical-request reservation to its timed stage."""
        if self._active_run.get() != run_id:
            raise ValueError('Stage must belong to the active run')
        record = dict(id=uuid4().hex, name=str(name), started_at=now(), status='running', elapsed_ms=None)
        self._update_run(run_id, lambda row: row['stages'].append(record))
        token, started, failure = self._active_stage.set(record['id']), time.monotonic(), None
        try:
            yield record
        except BaseException as error:
            failure = error
            raise
        finally:
            from .run_reliability import failure_record
            self._active_stage.reset(token)
            def finish(row):
                stage = next(item for item in row['stages'] if item['id'] == record['id'])
                stage.update(status='failed' if failure else 'completed',
                    error=type(failure).__name__ if failure else None,
                    failure_accounting=failure_record(failure, stage=str(name)) if failure else None,
                    finished_at=now(), elapsed_ms=round((time.monotonic() - started) * 1000))
            self._finish_accounting(run_id, finish, failure)


def private_json(path, value):
    """Atomic, mode-600 checkpoints; raw model artifacts never enter static roots."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
