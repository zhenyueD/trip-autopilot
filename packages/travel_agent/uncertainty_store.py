"""Trip-scoped SQLite uncertainty records, without a default production path.

This module is a trusted server boundary, not an HTTP authorization boundary.
Callers authenticate trip access before using it. Only a server tool adapter
may receive record_evidence; Writer input goes through propose_claim/assess_claim.
Pass db= to participate in the caller's already-open atomic transaction.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from .uncertainty_contract import (
    UncertaintyError, assess_conditions, canonical, check_version, evidence_current,
    fingerprint, instant, require_text, require_version, utcnow, validate_judgment,
)
from .uncertainty_actions import ActionMethods


SCHEMA = '''
CREATE TABLE IF NOT EXISTS uncertainty_claims (
 id TEXT PRIMARY KEY, trip_id TEXT NOT NULL, revision INTEGER NOT NULL,
 scope_id TEXT NOT NULL, version INTEGER NOT NULL, data TEXT NOT NULL,
 UNIQUE(id, trip_id));
CREATE TABLE IF NOT EXISTS uncertainty_evidence (
 id TEXT PRIMARY KEY, trip_id TEXT NOT NULL, revision INTEGER NOT NULL,
 scope_id TEXT NOT NULL, version INTEGER NOT NULL, data TEXT NOT NULL,
 UNIQUE(id, trip_id));
CREATE TABLE IF NOT EXISTS uncertainty_evidence_events (
 id TEXT PRIMARY KEY, evidence_id TEXT NOT NULL, trip_id TEXT NOT NULL,
 version INTEGER NOT NULL, data TEXT NOT NULL, UNIQUE(evidence_id, version),
 FOREIGN KEY(evidence_id, trip_id) REFERENCES uncertainty_evidence(id, trip_id));
CREATE TABLE IF NOT EXISTS uncertainty_assessments (
 id TEXT PRIMARY KEY, trip_id TEXT NOT NULL, claim_id TEXT NOT NULL,
 revision INTEGER NOT NULL, scope_id TEXT NOT NULL, version INTEGER NOT NULL,
 data TEXT NOT NULL, UNIQUE(id, trip_id),
 FOREIGN KEY(claim_id, trip_id) REFERENCES uncertainty_claims(id, trip_id));
CREATE TABLE IF NOT EXISTS uncertainty_assessment_evidence (
 assessment_id TEXT NOT NULL, evidence_id TEXT NOT NULL, trip_id TEXT NOT NULL,
 state_version INTEGER NOT NULL, PRIMARY KEY(assessment_id, evidence_id),
 FOREIGN KEY(assessment_id, trip_id) REFERENCES uncertainty_assessments(id, trip_id),
 FOREIGN KEY(evidence_id, trip_id) REFERENCES uncertainty_evidence(id, trip_id));
CREATE TABLE IF NOT EXISTS uncertainty_actions (
 id TEXT PRIMARY KEY, trip_id TEXT NOT NULL, revision INTEGER NOT NULL,
 scope_id TEXT NOT NULL, version INTEGER NOT NULL, idempotency_key TEXT NOT NULL,
 data TEXT NOT NULL, UNIQUE(id, trip_id), UNIQUE(trip_id, idempotency_key));
CREATE TABLE IF NOT EXISTS uncertainty_action_claims (
 action_id TEXT NOT NULL, claim_id TEXT NOT NULL, trip_id TEXT NOT NULL,
 claim_version INTEGER NOT NULL, PRIMARY KEY(action_id, claim_id),
 FOREIGN KEY(action_id, trip_id) REFERENCES uncertainty_actions(id, trip_id),
 FOREIGN KEY(claim_id, trip_id) REFERENCES uncertainty_claims(id, trip_id));
CREATE TABLE IF NOT EXISTS uncertainty_action_evidence (
 action_id TEXT NOT NULL, evidence_id TEXT NOT NULL, trip_id TEXT NOT NULL,
 state_version INTEGER NOT NULL, PRIMARY KEY(action_id, evidence_id),
 FOREIGN KEY(action_id, trip_id) REFERENCES uncertainty_actions(id, trip_id),
 FOREIGN KEY(evidence_id, trip_id) REFERENCES uncertainty_evidence(id, trip_id));
CREATE TABLE IF NOT EXISTS uncertainty_action_events (
 id TEXT PRIMARY KEY, action_id TEXT NOT NULL, trip_id TEXT NOT NULL,
 data TEXT NOT NULL, FOREIGN KEY(action_id, trip_id) REFERENCES uncertainty_actions(id, trip_id));
CREATE TABLE IF NOT EXISTS uncertainty_audit_batches (
 id TEXT PRIMARY KEY, week TEXT NOT NULL, sampling_key TEXT NOT NULL UNIQUE, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS uncertainty_audit_log (
 id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, assessment_id TEXT NOT NULL,
 trip_id TEXT NOT NULL, data TEXT NOT NULL, UNIQUE(batch_id, assessment_id),
 FOREIGN KEY(batch_id) REFERENCES uncertainty_audit_batches(id),
 FOREIGN KEY(assessment_id, trip_id) REFERENCES uncertainty_assessments(id, trip_id));
CREATE TABLE IF NOT EXISTS uncertainty_audit_control (
 id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS uncertainty_evidence_no_update
 BEFORE UPDATE ON uncertainty_evidence BEGIN SELECT RAISE(ABORT, 'immutable evidence'); END;
DROP TRIGGER IF EXISTS uncertainty_evidence_no_delete;
CREATE TRIGGER uncertainty_evidence_no_delete
 BEFORE DELETE ON uncertainty_evidence WHEN uncertainty_privacy_purge() != 1
 BEGIN SELECT RAISE(ABORT, 'immutable evidence'); END;
CREATE TRIGGER IF NOT EXISTS uncertainty_assessment_no_update
 BEFORE UPDATE ON uncertainty_assessments BEGIN SELECT RAISE(ABORT, 'immutable assessment'); END;
DROP TRIGGER IF EXISTS uncertainty_assessment_no_delete;
CREATE TRIGGER uncertainty_assessment_no_delete
 BEFORE DELETE ON uncertainty_assessments WHEN uncertainty_privacy_purge() != 1
 BEGIN SELECT RAISE(ABORT, 'immutable assessment'); END;
CREATE INDEX IF NOT EXISTS uncertainty_claim_trip ON uncertainty_claims(trip_id, revision);
CREATE INDEX IF NOT EXISTS uncertainty_evidence_trip ON uncertainty_evidence(trip_id, revision);
CREATE INDEX IF NOT EXISTS uncertainty_assessment_claim ON uncertainty_assessments(trip_id, claim_id);
'''


class UncertaintyStore(ActionMethods):
    def __init__(self, path=None, *, connection_factory=None, clock=None):
        if (path is None) == (connection_factory is None):
            raise ValueError('Provide exactly one explicit path or connection_factory')
        self.path = Path(path) if path is not None else None
        self.connection_factory = connection_factory
        self.clock = clock or utcnow
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connection(self):
        if self.connection_factory:
            with self.connection_factory() as db:
                db.create_function('uncertainty_privacy_purge', 0, lambda: 0)
                yield db
        else:
            db = sqlite3.connect(str(self.path), timeout=15)
            db.create_function('uncertainty_privacy_purge', 0, lambda: 0)
            try:
                with db:
                    yield db
            finally:
                db.close()

    @contextmanager
    def _using(self, db=None, *, write=False):
        if db is not None:
            # Ownership (including BEGIN/COMMIT/ROLLBACK) stays with the caller.
            db.create_function('uncertainty_privacy_purge', 0, lambda: 0)
            yield db
            return
        with self.connection() as conn:
            conn.execute('PRAGMA foreign_keys=ON')
            if write:
                conn.execute('BEGIN IMMEDIATE')
            yield conn

    def initialize(self):
        with self.connection() as db:
            db.execute('PRAGMA foreign_keys=ON')
            db.executescript(SCHEMA)
            db.execute('INSERT OR IGNORE INTO uncertainty_audit_control VALUES (1,?)',
                       (canonical({'automatic_verified': True, 'status': 'not_audited',
                                   'failed_batch_id': None, 'repair': None}),))

    def _get(self, db, kind, trip_id, record_id):
        if kind not in {'claims', 'evidence', 'assessments', 'actions'}:
            raise ValueError('unsupported record type')
        row = db.execute(f'SELECT data FROM uncertainty_{kind} WHERE id=? AND trip_id=?',
                         (record_id, trip_id)).fetchone()
        if not row:
            raise UncertaintyError('NOT_FOUND', '记录不存在或不属于当前旅行', 404)
        value = json.loads(row[0])
        if kind == 'evidence':
            event = db.execute('SELECT data FROM uncertainty_evidence_events '
                               'WHERE evidence_id=? AND trip_id=? ORDER BY version DESC LIMIT 1',
                               (record_id, trip_id)).fetchone()
            value.update(json.loads(event[0]))
        return value

    def get_claim(self, trip_id, claim_id, *, db=None):
        with self._using(db) as conn:
            return self._get(conn, 'claims', trip_id, claim_id)

    def get_evidence(self, trip_id, evidence_id, *, db=None):
        with self._using(db) as conn:
            return self._get(conn, 'evidence', trip_id, evidence_id)

    def _base(self, trip_id, revision, payload):
        require_text(trip_id, 'trip_id')
        require_version(revision)
        return {'id': uuid4().hex, 'trip_id': trip_id, 'revision': revision,
                'scope_id': require_text(payload.get('scope_id'), 'scope_id'),
                'version': 1, 'created_at': self.clock()}

    def propose_claim(self, trip_id, revision, payload, *, db=None):
        value = self._base(trip_id, revision, payload)
        value.update(self._claim_fields(payload))
        with self._using(db, write=True) as conn:
            conn.execute('INSERT INTO uncertainty_claims VALUES (?,?,?,?,?,?)',
                         (value['id'], trip_id, revision, value['scope_id'], 1, canonical(value)))
        return value

    @staticmethod
    def _claim_fields(payload):
        conditions = payload.get('conditions')
        if not isinstance(conditions, dict):
            raise UncertaintyError('BAD_INPUT', 'conditions必须是对象')
        return {'entity_id': require_text(payload.get('entity_id'), 'entity_id'),
                'predicate': require_text(payload.get('predicate'), 'predicate'),
                'value': payload.get('value'), 'conditions': conditions,
                'risk_type': require_text(payload.get('risk_type', 'unspecified'), 'risk_type'),
                'source_field_ref': payload.get('source_field_ref'),
                'unknown_kind': payload.get('unknown_kind', 'missing_fact'),
                'next_step': payload.get('next_step'), 'owner': payload.get('owner')}

    def revise_claim(self, trip_id, claim_id, payload, *, expected_version, db=None):
        with self._using(db, write=True) as conn:
            claim = self._get(conn, 'claims', trip_id, claim_id)
            check_version(claim['version'], expected_version)
            value = claim | self._claim_fields(payload) | {'version': claim['version'] + 1}
            conn.execute('UPDATE uncertainty_claims SET version=?,data=? WHERE id=? AND trip_id=?',
                         (value['version'], canonical(value), claim_id, trip_id))
            self._invalidate_actions(conn, trip_id, claim_id=claim_id, reason='claim_changed')
            return value

    def record_evidence(self, trip_id, revision, receipt, *, actor, db=None):
        """Trusted tool/material adapter only. Never expose actor as a client field."""
        if actor != 'tool':
            raise UncertaintyError('FORBIDDEN', 'Writer不能签发证据', 403)
        value = self._base(trip_id, revision, receipt)
        for key in ('tool_call_id', 'entity_id', 'source_ref', 'source_identity', 'source_family', 'raw_status'):
            value[key] = require_text(receipt.get(key), key)
        if not isinstance(receipt.get('request_conditions'), dict) or 'content' not in receipt:
            raise UncertaintyError('BAD_EVIDENCE', '证据须保存请求条件和原始内容')
        value.update({key: receipt.get(key) for key in (
            'request_conditions', 'content', 'published_at', 'valid_from', 'valid_until', 'http_status')})
        value['observed_at'] = require_text(receipt.get('observed_at'), 'observed_at')
        for key in ('observed_at', 'published_at', 'valid_from', 'valid_until'):
            if value[key] is not None:
                instant(value[key])
        if value['http_status'] is not None and type(value['http_status']) is not int:
            raise UncertaintyError('BAD_EVIDENCE', 'http_status必须是整数或null')
        value['content_hash'] = fingerprint(value['content'])
        value['receipt_hash'] = fingerprint(value)
        event = {'state': 'active', 'state_version': 1, 'state_reason': None, 'state_at': self.clock()}
        with self._using(db, write=True) as conn:
            conn.execute('INSERT INTO uncertainty_evidence VALUES (?,?,?,?,?,?)',
                         (value['id'], trip_id, revision, value['scope_id'], 1, canonical(value)))
            conn.execute('INSERT INTO uncertainty_evidence_events VALUES (?,?,?,?,?)',
                         (uuid4().hex, value['id'], trip_id, 1, canonical(event)))
        return value | event

    def _refs(self, db, trip_id, claim, refs):
        if not isinstance(refs, list) or any(not isinstance(ref, dict) for ref in refs):
            raise UncertaintyError('BAD_REFERENCE', 'evidence_refs必须为引用列表')
        if len({ref.get('id') for ref in refs}) != len(refs):
            raise UncertaintyError('BAD_REFERENCE', '不能重复引用同一证据')
        evidence = []
        for ref in refs:
            item = self._get(db, 'evidence', trip_id, ref.get('id'))
            check_version(item['state_version'], ref.get('state_version'))
            if (item['revision'], item['scope_id']) != (claim['revision'], claim['scope_id']):
                raise UncertaintyError('SCOPE_MISMATCH', '证据不属于本次版本和编辑范围', 409)
            evidence.append(item)
        return evidence

    def assess_claim(self, trip_id, claim_id, *, expected_version, evidence_refs,
                     judgment, verifier_version, db=None):
        judgment = validate_judgment(judgment)
        require_text(verifier_version, 'verifier_version')
        with self._using(db, write=True) as conn:
            claim = self._get(conn, 'claims', trip_id, claim_id)
            check_version(claim['version'], expected_version)
            evidence = self._refs(conn, trip_id, claim, evidence_refs)
            at = self.clock()
            result = assess_conditions(claim, evidence, judgment, at,
                                       automatic_verified=self._automatic_verified(conn, verifier_version))
            assessment = self._base(trip_id, claim['revision'], claim) | result | {
                'claim_id': claim_id, 'claim_version': claim['version'],
                'evidence_refs': evidence_refs, 'judgment': judgment,
                'verifier_version': verifier_version, 'rule_version': 'six-conditions-v1',
                'assessed_at': at, 'claim_snapshot': claim, 'evidence_snapshot': evidence,
            }
            conn.execute('INSERT INTO uncertainty_assessments VALUES (?,?,?,?,?,?,?)', (
                assessment['id'], trip_id, claim_id, claim['revision'], claim['scope_id'],
                1, canonical(assessment)))
            conn.executemany('INSERT INTO uncertainty_assessment_evidence VALUES (?,?,?,?)',
                             [(assessment['id'], e['id'], trip_id, e['state_version']) for e in evidence])
            return assessment

    def _audit_control(self, db):
        return json.loads(db.execute('SELECT data FROM uncertainty_audit_control WHERE id=1').fetchone()[0])

    def _automatic_verified(self, db, verifier_version):
        control = self._audit_control(db)
        return (control['automatic_verified']
                and verifier_version not in control.get('revoked_verifier_versions', []))

    def current_assessment(self, trip_id, claim_id, *, db=None):
        with self._using(db) as conn:
            claim = self._get(conn, 'claims', trip_id, claim_id)
            row = conn.execute('SELECT data FROM uncertainty_assessments WHERE trip_id=? '
                               'AND claim_id=? ORDER BY rowid DESC LIMIT 1', (trip_id, claim_id)).fetchone()
            if not row:
                return {'claim_id': claim_id, 'tier': 'unknown', 'reasons': ['not_assessed'],
                        'checks': {}, 'claim_version': claim['version']}
            assessment = json.loads(row[0])
            if assessment['claim_version'] != claim['version']:
                return assessment | {'tier': 'unknown', 'reasons': ['claim_version_changed']}
            try:
                evidence = self._refs(conn, trip_id, claim, assessment['evidence_refs'])
            except UncertaintyError as error:
                return assessment | {'tier': 'unknown', 'reasons': [error.code]}
            return assessment | assess_conditions(claim, evidence, assessment['judgment'], self.clock(),
                automatic_verified=self._automatic_verified(conn, assessment['verifier_version']))

    def list_claims(self, trip_id, *, revision=None, scope_id=None, db=None):
        return self._list_records('claims', trip_id, revision=revision, scope_id=scope_id, db=db)

    def list_evidence(self, trip_id, *, revision=None, scope_id=None, db=None):
        return self._list_records('evidence', trip_id, revision=revision, scope_id=scope_id, db=db)

    def list_actions(self, trip_id, *, revision=None, scope_id=None, db=None):
        return self._list_records('actions', trip_id, revision=revision, scope_id=scope_id, db=db)

    def list_assessments(self, trip_id, *, revision=None, scope_id=None, db=None):
        with self._using(db) as conn:
            claims = self.list_claims(trip_id, revision=revision, scope_id=scope_id, db=conn)
            return [self.current_assessment(trip_id, claim['id'], db=conn) for claim in claims]

    def _list_records(self, kind, trip_id, *, revision=None, scope_id=None, db=None):
        if kind not in {'claims', 'evidence', 'actions'}:
            raise ValueError('unsupported record type')
        with self._using(db) as conn:
            rows = conn.execute(f'SELECT id FROM uncertainty_{kind} WHERE trip_id=? ORDER BY rowid',
                                (trip_id,)).fetchall()
            values = [self._get(conn, kind, trip_id, row[0]) for row in rows]
            return [value for value in values if (revision is None or value['revision'] == revision)
                    and (scope_id is None or value['scope_id'] == scope_id)]

    def invalidate_evidence(self, trip_id, evidence_id, *, expected_state_version, reason,
                            state='withdrawn', db=None):
        if state not in {'withdrawn', 'expired', 'unavailable'}:
            raise UncertaintyError('BAD_STATE', '失效状态不合法')
        require_text(reason, 'reason')
        with self._using(db, write=True) as conn:
            evidence = self._get(conn, 'evidence', trip_id, evidence_id)
            check_version(evidence['state_version'], expected_state_version)
            event = {'state': state, 'state_version': evidence['state_version'] + 1,
                     'state_reason': reason, 'state_at': self.clock()}
            conn.execute('INSERT INTO uncertainty_evidence_events VALUES (?,?,?,?,?)',
                         (uuid4().hex, evidence_id, trip_id, event['state_version'], canonical(event)))
            self._invalidate_actions(conn, trip_id, evidence_id=evidence_id, reason=reason)
            return evidence | event

    def expire_evidence(self, trip_id, *, db=None):
        expired = []
        with self._using(db, write=True) as conn:
            rows = conn.execute('SELECT id FROM uncertainty_evidence WHERE trip_id=?', (trip_id,)).fetchall()
            for row in rows:
                evidence = self._get(conn, 'evidence', trip_id, row[0])
                if (evidence['state'] == 'active' and evidence.get('valid_until')
                        and instant(evidence['valid_until']) <= instant(self.clock())):
                    expired.append(self.invalidate_evidence(trip_id, row[0],
                        expected_state_version=evidence['state_version'], reason='valid_until_elapsed',
                        state='expired', db=conn)['id'])
        return expired

    def assert_publishable(self, trip_id, revision, assessment_ids, *, db=None):
        """Call inside the trip commit transaction to close assessment-to-use races."""
        with self._using(db) as conn:
            for assessment_id in assessment_ids:
                original = self._get(conn, 'assessments', trip_id, assessment_id)
                if original['revision'] != revision:
                    raise UncertaintyError('VERSION_CONFLICT', '核验对应的行程版本已变化', 409)
                current = self.current_assessment(trip_id, original['claim_id'], db=conn)
                if current.get('id') != assessment_id or current['tier'] != 'verified':
                    raise UncertaintyError('STALE_ASSESSMENT', '发布前须重新核验', 409)
        return True

    def purge_trip(self, trip_id, *, db=None):
        """Explicit authenticated trip deletion; erase raw snapshots, keep aggregates.

        The caller owns trip authorization. When db is supplied, deletion joins
        the same transaction as TripStore.delete and rolls back with it.
        """
        with self._using(db, write=True) as conn:
            counts = {}
            rows = conn.execute('SELECT batch_id,COUNT(*) FROM uncertainty_audit_log '
                                'WHERE trip_id=? GROUP BY batch_id', (trip_id,)).fetchall()
            for batch_id, count in rows:
                batch = json.loads(conn.execute('SELECT data FROM uncertainty_audit_batches WHERE id=?',
                                               (batch_id,)).fetchone()[0])
                batch['privacy_erased_items'] = batch.get('privacy_erased_items', 0) + count
                if batch['status'] not in {'passed', 'failed'}:
                    batch['status'] = 'incomplete_privacy_erasure'
                conn.execute('UPDATE uncertainty_audit_batches SET data=? WHERE id=?',
                             (canonical(batch), batch_id))
            conn.create_function('uncertainty_privacy_purge', 0, lambda: 1)
            try:
                for table in ('audit_log', 'action_events', 'action_claims', 'action_evidence',
                              'actions', 'assessment_evidence', 'assessments', 'evidence_events',
                              'evidence', 'claims'):
                    result = conn.execute(f'DELETE FROM uncertainty_{table} WHERE trip_id=?', (trip_id,))
                    counts[table] = result.rowcount
            finally:
                conn.create_function('uncertainty_privacy_purge', 0, lambda: 0)
            return {'trip_id': trip_id, 'deleted': counts}
