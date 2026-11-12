"""Authenticated read-only restaurant pilot and bounded uncertainty projection.

No client can write Evidence, Assessment, a verifier verdict, or an Action result.
The existing itinerary is never changed by this query. Its revision is checked
again inside the transaction that stores the tool receipt.
"""
from __future__ import annotations

import copy
import json
import sqlite3
import threading
import time
from uuid import uuid4

from .execution_budget import ExecutionBudget
from .store import StateError
from .uncertainty_contract import UncertaintyError, utcnow
from .uncertainty_store import UncertaintyStore


RUN_SCHEMA = '''CREATE TABLE IF NOT EXISTS uncertainty_runs (
 id TEXT PRIMARY KEY, trip_id TEXT NOT NULL, revision INTEGER NOT NULL,
 data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS uncertainty_run_trip ON uncertainty_runs(trip_id, revision);'''
_INIT_LOCK = threading.Lock()


def _dump(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def has_records(store):
    with store.connection() as db:
        return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='uncertainty_runs'").fetchone())


class Phase1Service:
    def __init__(self, store, *, transport=None, clock=None, budget_factory=ExecutionBudget):
        self.store, self.transport = store, transport
        self.clock, self.budget_factory = clock or utcnow, budget_factory
        self.evidence = UncertaintyStore(connection_factory=store.connection, clock=self.clock)
        self._slots = threading.BoundedSemaphore(2)
        with self.store.connection() as db:
            db.executescript(RUN_SCHEMA)

    def _authorize(self, trip_id, token, revision=None, *, db=None):
        trip = self.store._load(db, trip_id) if db is not None else self.store.internal_get(trip_id)
        self.store._authorize(trip, token)
        if revision is not None:
            self.store._check_revision(trip, revision)
        return trip

    def query_availability(self, trip_id, token, body):
        accepted = time.monotonic()
        trip = self._authorize(trip_id, token)
        allowed = {'expected_revision', 'branch_id', 'local_date', 'local_time', 'timezone_name', 'party_size'}
        if not isinstance(body, dict) or set(body) != allowed:
            raise StateError('BAD_INPUT', '请提供分店、日期、当地时间、时区、人数及当前版本')
        if any(not isinstance(body[key], str) or not 0 < len(body[key]) < 100 for key in
               ('branch_id', 'local_date', 'local_time', 'timezone_name')) or type(body['party_size']) is not int:
            raise StateError('BAD_INPUT', '餐厅条件格式无效，未发送查询')
        self.store._check_revision(trip, body['expected_revision'])
        if trip['status'] == 'running':
            raise StateError('BUSY', '方案正在更新，请完成后核查餐厅条件', 409)
        if not self._slots.acquire(blocking=False):
            raise StateError('CAPACITY', '正在核查其他安排，请稍后再试；未发送查询', 429)
        root = self.budget_factory(started_at=accepted)
        fact = root.child('L1', body['local_date']).child('L2', 'restaurant_availability')
        scope_id = uuid4().hex
        run = {'id': scope_id, 'trip_id': trip_id, 'revision': trip['revision'],
            'accepted_at': self.clock(), 'status': 'running', 'request': copy.deepcopy(body),
            'reservation_status': 'not_attempted', 'model_calls': 0}
        try:
            self._save_run(run)
            result = self._query(body, fact, scope_id)
            def publish():
                with self.store.connection() as db:
                    db.execute('BEGIN IMMEDIATE')
                    self._authorize(trip_id, token, trip['revision'], db=db)
                    claims = self._record_result(run, result, db)
                    run.update(status=result['status'], result=self._public_result(result),
                               claim_ids=[c['id'] for c in claims], budget=root.snapshot())
                    db.execute('UPDATE uncertainty_runs SET data=? WHERE id=?', (_dump(run), scope_id))
            if not root.publish(publish):
                run.update(status='unknown', reason='查询预算已用完，结果未写入当前方案', budget=root.snapshot())
                self._save_run(run)
        except (UncertaintyError, StateError):
            run.update(status='unknown', reason='旅行条件已变化或证据未通过校验', budget=root.snapshot())
            self._save_run(run)
            raise
        except (ValueError, TypeError):
            run.update(status='unknown', reason='查询条件或渠道结果无效', budget=root.snapshot())
            self._save_run(run)
            raise StateError('BAD_INPUT', '查询条件或渠道结果无效，未创建预约') from None
        except Exception:
            run.update(status='unknown', reason='渠道查询未成功；没有创建预约', budget=root.snapshot())
            self._save_run(run)
        finally:
            root.close()
            self._slots.release()
        return self.view(trip_id, token)

    def _query(self, body, budget, scope_id):
        from .restaurant_availability import query_restaurant_availability, default_transport
        actual = self.transport or default_transport
        def counted(url, *, deadline):
            result = budget.call(lambda lease: actual(url, deadline=min(deadline, lease.deadline)),
                                 kind='restaurant_public_get')
            if result.status != 'ok':
                raise TimeoutError('availability query budget or transport failed')
            return result.value
        fields = {key: body[key] for key in ('branch_id', 'local_date', 'local_time', 'timezone_name', 'party_size')}
        return query_restaurant_availability(**fields, scope_id=scope_id,
            tool_call_id=uuid4().hex, transport=counted, deadline=time.monotonic() + budget.remaining_seconds)

    def verify_recorded_claim(self, trip_id, token, claim_id, *, expected_revision,
                              expected_version, evidence_refs, budget, caller=None):
        """Server orchestration entry; callers cannot supply a semantic verdict.

        Reuse its enclosing fact budget. No public arbitrary-Evidence/Claim
        creation endpoint is introduced by this bridge.
        """
        from .model import call_json
        from .uncertainty_verifier import SemanticVerifier
        from .phase1_runtime import _immediate_store
        immediate = _immediate_store(self.store)
        with immediate.connection() as db:
            self._authorize(trip_id, token, expected_revision, db=db)
        def guard(db, actual_trip_id, revision):
            self._authorize(actual_trip_id, token, revision, db=db)
        evidence = copy.copy(self.evidence)
        evidence.connection_factory = immediate.connection
        verifier = SemanticVerifier(evidence, caller=caller or call_json, commit_guard=guard)
        result = verifier.verify(trip_id, claim_id, expected_revision=expected_revision,
            expected_version=expected_version, evidence_refs=evidence_refs, budget=budget)
        try:
            with immediate.connection() as db:
                db.execute('BEGIN IMMEDIATE')
                trip = self._authorize(trip_id, token, expected_revision, db=db)
                metrics = trip.setdefault('metrics', {})
                receipt = {key: copy.deepcopy(result.get(key)) for key in ('status', 'tier', 'reason',
                    'claim_id', 'claim_version', 'revision', 'evidence_refs', 'verifier_version',
                    'input_hash', 'prompt_hash', 'usage', 'budget')}
                receipt['assessment_id'] = (result.get('assessment') or {}).get('id')
                metrics.setdefault('semantic_verification_runs', []).append(receipt)
                self.store._save(db, trip)
            result['usage_persistence'] = 'persisted'
        except sqlite3.OperationalError:
            # The shared parent ledger still owns the physical-call costs.
            result['usage_persistence'] = 'parent_budget_only'
        return result

    def _save_run(self, run):
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM trips WHERE id=?', (run['trip_id'],)).fetchone():
                return False  # A late query/error cannot recreate erased private data.
            db.execute('INSERT INTO uncertainty_runs VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data',
                (run['id'], run['trip_id'], run['revision'], _dump(run)))
            return True

    def _record_result(self, run, result, db):
        claims = []
        for receipt in result.get('evidence', []):
            if not receipt:
                continue
            condition = receipt['request_conditions']
            alternative = condition['time'] != run['request']['local_time']
            evidence = self.evidence.record_evidence(run['trip_id'], run['revision'], receipt, actor='tool', db=db)
            available = result['status'] == 'available' or alternative
            claim = self.evidence.propose_claim(run['trip_id'], run['revision'], {
                'scope_id': run['id'], 'entity_id': receipt['entity_id'], 'predicate': 'availability',
                'value': available if result['status'] != 'unknown' else None,
                'conditions': receipt['request_conditions'], 'risk_type': 'availability',
                'source_field_ref': 'alternative' if alternative else 'requested_slot',
                'next_step': '选定时段后，在预约渠道再次核对并自行完成预约', 'owner': 'traveler'}, db=db)
            direct = result['status'] in ('available', 'not_available')
            self.evidence.assess_claim(run['trip_id'], claim['id'], expected_version=1,
                evidence_refs=[{'id': evidence['id'], 'state_version': evidence['state_version']}],
                judgment={'source_suitable': direct, 'support': 'direct' if direct else 'insufficient',
                    'no_conflict': True if direct else None,
                    'reason': '餐厅指定预约渠道的结构化时段结果，条件由适配器逐项核对；不是预约回执',
                    'assumptions': []}, verifier_version='restaurant-structured-result-v1', db=db)
            claims.append(claim)
        if not any(c.get('source_field_ref') == 'requested_slot' for c in claims):
            claims.append(self.evidence.propose_claim(run['trip_id'], run['revision'], {
                'scope_id': run['id'], 'entity_id': run['request']['branch_id'], 'predicate': 'availability',
                'value': None, 'conditions': {'branch_id': run['request']['branch_id'],
                    'date': run['request']['local_date'], 'time': run['request']['local_time'],
                    'timezone': run['request']['timezone_name'], 'party_size': run['request']['party_size']},
                'risk_type': 'availability', 'owner': 'traveler',
                'next_step': '在餐厅预约渠道核对指定条件，或选择备选'}, db=db))
        return claims

    @staticmethod
    def _public_result(result):
        return {key: copy.deepcopy(result.get(key)) for key in
            ('status', 'request_conditions', 'slot', 'opening_status', 'reservation_status', 'reason')}

    def view(self, trip_id, token):
        trip = self._authorize(trip_id, token)
        with self.store.connection() as db:
            rows = db.execute('SELECT data FROM uncertainty_runs WHERE trip_id=? AND revision=? ORDER BY rowid DESC LIMIT 1',
                              (trip_id, trip['revision'])).fetchall()
        run = json.loads(rows[0][0]) if rows else None
        scope = run['id'] if run else '__none__'
        claims = self.evidence.list_claims(trip_id, revision=trip['revision'], scope_id=scope)
        view = []
        for claim in claims:
            assessment = self.evidence.current_assessment(trip_id, claim['id'])
            sources = []
            for ref in assessment.get('evidence_refs', []):
                evidence = self.evidence.get_evidence(trip_id, ref['id'])
                sources.append({key: evidence.get(key) for key in
                    ('id', 'source_ref', 'source_identity', 'observed_at', 'valid_until', 'state', 'state_reason')})
            view.append({key: claim.get(key) for key in
                ('id', 'entity_id', 'predicate', 'value', 'conditions', 'owner', 'next_step', 'source_field_ref')} | {
                'tier': assessment['tier'], 'reasons': assessment['reasons'],
                'assumptions': assessment.get('assumptions', []), 'sources': sources,
                'reason': assessment.get('judgment', {}).get('reason', '尚未取得可核验的当前证据')})
        if run and not view:
            request = run['request']
            view.append({'id': run['id'] + ':pending', 'entity_id': request['branch_id'],
                'predicate': 'availability', 'value': None, 'tier': 'unknown',
                'conditions': {'branch_id': request['branch_id'], 'date': request['local_date'],
                    'time': request['local_time'], 'timezone': request['timezone_name'], 'party_size': request['party_size']},
                'owner': 'traveler', 'next_step': '在餐厅预约渠道核对指定条件，或稍后重新查询',
                'reason': run.get('reason') or '当前查询尚未取得可核验结果',
                'reasons': ['no_assessment'], 'assumptions': [], 'sources': []})
        from .uncertainty_audit import UncertaintyAudit
        control = UncertaintyAudit(self.evidence).control()
        sampler = getattr(self, 'audit_sampler', None)
        return {'revision': trip['revision'], 'claims': view,
            'run': {key: run.get(key) for key in ('id', 'status', 'accepted_at', 'reason', 'budget')} if run else None,
            'opening_status': 'unknown', 'reservation_status': 'not_attempted',
            'blind_spots': trip.get('metrics', {}).get('blind_spots'),
            'audit': {'automatic_verified': control['automatic_verified'], 'status': control['status'],
                      'scheduler_error': getattr(sampler, 'last_error', None)},
            'limits': '可用性只代表查询时点；营业信息与预约成功分别核对。'}


def get_phase1(studio, *, create=False):
    with _INIT_LOCK:
        instance = getattr(studio, 'phase1_service', None)
        if instance is None and (create or has_records(studio.store)):
            from .uncertainty_schedule import WeeklyAuditSampler
            instance = Phase1Service(studio.store)
            instance.audit_sampler = WeeklyAuditSampler(instance.evidence).start()
            studio.phase1_service = instance
        return instance


def handle_phase1(studio, method, trip_id, action, body, token):
    studio.store.get(trip_id, token)  # Authorize before initialization or provider work.
    instance = get_phase1(studio, create=method == 'POST' and action == 'availability')
    try:
        if method == 'GET' and not action:
            return {'uncertainty': instance.view(trip_id, token) if instance else None}, 200
        if method == 'POST' and action == 'availability':
            return {'uncertainty': instance.query_availability(trip_id, token, body)}, 200
    except UncertaintyError as error:
        raise StateError(error.code, error.message, error.status) from None
    raise StateError('NOT_FOUND', '核验接口不存在', 404)
