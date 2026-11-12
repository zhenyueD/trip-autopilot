"""Private, versioned trip state. No model output can bypass the commit boundary."""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .intake import add_sources, material_receipts


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


class StateError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def public_trip(trip):
    hidden = {'token_hash', 'evidence_text', 'images', 'history', 'feedback', 'research', 'revision_base', 'revision_material_ids', 'revision_plan', 'revision_feedback', 'revision_pending_materials', 'material_sources', 'intake_attempts', 'consistency_attempts'}
    hidden.update({'revision_checkpoint', 'revision_request_id', 'revision_retry', 'execution_failures', 'material_detail_ledger', 'detail_research_context', 'detail_public_cache', 'revision_timeline', 'phase1_execution_marker'})
    result = {k: copy.deepcopy(v) for k, v in trip.items() if k not in hidden}
    receipt = result.get('revision_receipt')
    if receipt is not None and (trip.get('status') != 'ready' or not isinstance(receipt, dict)
            or receipt.get('revision') != trip.get('revision') or receipt.get('trip_id') != trip.get('id')):
        result.pop('revision_receipt', None)
    auto_receipt = result.get('autopilot_revision_receipt')
    if auto_receipt is not None and (trip.get('status') != 'ready'
            or auto_receipt.get('revision') != trip.get('revision')
            or auto_receipt.get('trip_id') != trip.get('id')):
        result.pop('autopilot_revision_receipt', None)
    from .traveler_profile import current_profile
    result['traveler_profile'] = current_profile(trip)
    result['profile_revision'] = trip.get('profile_revision', 0)
    result['materials'] = material_receipts(trip.get('material_sources') or [])
    result['can_undo'] = bool(trip.get('history') or trip.get('revision_base')) and trip['status'] != 'running'
    return result


class TripStore:
    def __init__(self, path=None):
        self.path = Path(path or os.environ.get('TRAVEL_AGENT_DB', 'data/private/trips.sqlite3'))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute('CREATE TABLE IF NOT EXISTS trips (id TEXT PRIMARY KEY, data TEXT NOT NULL)')
        # Versioned one-time local migration: remove free pages left by the
        # pre-secure_delete prototype, while retaining every live trip.
        with self.connection() as db:
            if db.execute('PRAGMA user_version').fetchone()[0] < 1:
                db.execute('VACUUM')
                db.execute('PRAGMA user_version=1')
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    @contextmanager
    def connection(self):
        db = sqlite3.connect(str(self.path), timeout=15)
        db.execute('PRAGMA busy_timeout=15000')
        db.execute('PRAGMA secure_delete=ON')
        db.execute('PRAGMA journal_mode=DELETE')
        try:
            with db:
                yield db
        finally:
            db.close()

    def _load(self, db, trip_id):
        row = db.execute('SELECT data FROM trips WHERE id=?', (trip_id,)).fetchone()
        if not row:
            raise StateError('TRIP_NOT_FOUND', '旅行不存在或访问凭证已失效', 404)
        return json.loads(row[0])

    def _save(self, db, trip, *, plan_write=False, plan_touches=None, plan_owner=None):
        from .autopilot_revision_fields import record_write
        previous = self._load(db, trip['id'])
        record_write(db, previous, trip, explicit=plan_write,
                     touches=plan_touches, owner=plan_owner)
        trip['updated_at'] = now()
        db.execute('UPDATE trips SET data=? WHERE id=?',
                   (json.dumps(trip, ensure_ascii=False), trip['id']))

    def _authorize(self, trip, token):
        digest = hashlib.sha256(str(token or '').encode()).hexdigest()
        if not hmac.compare_digest(trip['token_hash'], digest):
            raise StateError('TRIP_NOT_FOUND', '旅行不存在或访问凭证已失效', 404)

    def create(self, brief, evidence_text, images, links=None, documents=None):
        token = secrets.token_urlsafe(32)
        trip = self._new_trip(brief, evidence_text, images, token, links, documents)
        with self.connection() as db:
            db.execute('INSERT INTO trips VALUES (?,?)',
                       (trip['id'], json.dumps(trip, ensure_ascii=False)))
        return public_trip(trip), token

    def _new_trip(self, brief, evidence_text, images, token, links=None, documents=None):
        """Pure factory; caller owns insertion/transaction and raw-token lifetime."""
        return {
            'id': uuid4().hex, 'token_hash': hashlib.sha256(token.encode()).hexdigest(),
            'revision': 1, 'status': 'running', 'brief': brief,
            'evidence_text': evidence_text, 'images': images, 'feedback': [], 'history': [],
            'plan': None, 'locked_item_ids': [], 'metrics': {}, 'error': None,
            'material_sources': add_sources([], revision=1, brief=brief, note=evidence_text,
                                            links=links, images=images, documents=documents), 'intake': None,
            'created_at': now(), 'updated_at': now(),
            'events': [{'label': '已接收目标，准备理解你的旅行', 'at': now()}],
        }

    def get(self, trip_id, token):
        with self.connection() as db:
            trip = self._load(db, trip_id)
        self._authorize(trip, token)
        return public_trip(trip)

    def internal_get(self, trip_id):
        with self.connection() as db:
            return self._load(db, trip_id)

    def update_profile(self, trip_id, token, revision, profile_revision, value):
        from .traveler_profile import normalize_profile
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            trip = self._load(db, trip_id)
            self._authorize(trip, token)
            self._check_revision(trip, revision)
            if trip['status'] == 'running':
                raise StateError('BUSY', '上一轮仍在处理，完成后可编辑偏好', 409)
            if type(profile_revision) is not int or profile_revision != trip.get('profile_revision', 0):
                raise StateError('VERSION_CONFLICT', '旅行偏好已更新，请读取后再保存', 409)
            try:
                profile = normalize_profile(value)
            except ValueError as error:
                raise StateError('BAD_INPUT', str(error)) from None
            trip.update(traveler_profile=profile, profile_revision=profile_revision + 1)
            self._save(db, trip)
        return public_trip(trip)

    def update_job(self, trip_id, revision, **fields):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            trip = self._load(db, trip_id)
            if trip['revision'] != revision or trip['status'] != 'running':
                raise StateError('STALE_JOB', '任务版本已变化', 409)
            if 'plan' in fields:
                if fields.get('status') != 'ready':
                    raise StateError('UNCOMMITTED_PLAN', '候选方案只能在整轮完成后一起保存', 409)
                if (fields.get('metrics') or {}).get('phase1_delivery_review'):
                    from .phase1_sources import assert_review_sources, preserve_source_failures
                    assert_review_sources(trip, fields['metrics']['phase1_delivery_review'])
                    from .initial_blind_delivery import assert_initial_delivery
                    assert_initial_delivery(trip, fields['plan'], fields['metrics'],
                                            fields['metrics']['phase1_delivery_review'])
                    if 'source_evidence' in fields:
                        fields['source_evidence'] = preserve_source_failures(trip.get('source_evidence') or [], fields['source_evidence'])
                from .delivery_truth import validate_preparation_days
                validate_preparation_days(fields['plan'], trip.get('revision_feedback', ''), trip.get('plan'))
                from .store_timeline import timeline_commit_fields
                from .traveler_profile import profile_commit_fields
                fields.update(timeline_commit_fields(trip, fields['plan'], revision))
                fields.update(profile_commit_fields(trip, fields['plan']))
                if fields.get('revision_receipt') is not None:
                    fields['revision_receipt'] = self._bound_receipt(trip, fields['plan'],
                        fields['revision_receipt'], fields.get('source_evidence'))
            elif 'revision_receipt' in fields:
                raise StateError('UNCOMMITTED_RECEIPT', '调整回执必须与对应方案一起保存', 409)
            label = fields.pop('event', None)
            if not trip.get('plan') and fields.get('status') in ('error', 'interrupted', 'needs_attention'):
                label = self._initial_failure_text(label)
                if fields.get('error'):
                    fields['error'] = self._initial_failure_text(fields['error'])
            if label:
                trip['events'] = (trip['events'] + [{'label': label, 'at': now()}])[-24:]
            trip.update(fields)
            if trip['status'] != 'running':
                self._park_legacy_materials(trip)
                self._finish_revision(trip)
            self._save(db, trip, plan_write='plan' in fields)
        return public_trip(trip)

    def _finish_revision(self, trip):
        trip.pop('revision_stage', None)
        if trip['status'] != 'ready':
            self._record_failure(trip)
            if 'revision_plan' in trip:
                trip['plan'] = trip['revision_plan']
            pending = {row['source_id']: row for row in trip.get('revision_pending_materials') or []}
            pending.update({row['source_id']: row for row in trip.get('pending_materials') or []})
            trip['pending_materials'] = list(pending.values())
            self._park_material_ids(trip, set(trip.get('revision_material_ids') or []))
        if trip['status'] == 'ready':
            trip.pop('revision_checkpoint', None)
            trip.pop('revision_progress', None)
            trip.pop('revision_request_id', None)
            trip.pop('detail_research_context', None)
        trip['revision_material_ids'] = []
        trip.pop('revision_plan', None)
        trip.pop('revision_feedback', None)
        trip.pop('revision_pending_materials', None)
        trip['draft_plan'] = None

    @staticmethod
    def _initial_failure_text(text):
        for claim in ('原方案保留', '已有方案保留', '现有方案保留', '已有结果保留'):
            text = text.replace(claim, '尚未生成完整方案') if text else text
        return text

    @staticmethod
    def _record_failure(trip):
        """Keep the first concrete failure and its evidence across user retries."""
        if trip.get('status') not in ('error', 'interrupted', 'needs_attention'):
            return
        failures = trip.setdefault('execution_failures', [])
        row = next((entry for entry in failures if entry['revision'] == trip['revision']), None)
        if row is None:
            row = {'revision': trip['revision'], 'status': trip['status'],
                   'error': trip.get('error'), 'at': now(),
                   'events': copy.deepcopy(trip.get('events') or [])}
            failures.append(row)
        row['metrics'] = copy.deepcopy(trip.get('metrics') or {})
        if row.get('error'):
            trip['last_failure'] = {key: row[key] for key in ('revision', 'status', 'error', 'at')}

    def _park_legacy_materials(self, trip):
        if 'revision_material_ids' in trip or trip['status'] == 'ready':
            return
        baseline = ((trip.get('revision_base') or {}).get('material_state') or {})
        old_ids = {s['id'] for s in baseline.get('material_sources') or []}
        self._park_material_ids(trip, {s['id'] for s in trip.get('material_sources') or []} - old_ids)

    def _park_material_ids(self, trip, material_ids):
        pending = trip.setdefault('pending_materials', [])
        existing = {row['source_id'] for row in pending}
        for source in trip.get('material_sources') or []:
            if (source['id'] not in material_ids or source['id'] in existing
                    or source.get('kind') in ('user_feedback', 'user_brief')):
                continue
            pending.append({'source_id': source['id'],
                'label': source.get('name') or source.get('label') or source['id'],
                'reason': 'prior_revision_unassigned', 'status': 'awaiting_assignment'})

    def begin_revision(self, trip_id, token, revision, feedback='', locks=None, retry=False, additions=None):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            trip = self._load(db, trip_id)
            self._authorize(trip, token)
            self._check_revision(trip, revision)
            if trip['status'] == 'running':
                raise StateError('BUSY', '上一轮仍在处理，请等待结果', 409)
            if retry and trip['status'] not in ('error', 'interrupted', 'needs_attention'):
                raise StateError('NOT_RETRYABLE', '当前方案无需重试，请直接提出调整', 409)
            from .traveler_profile import current_profile
            trip['traveler_profile'] = current_profile(trip)
            trip.setdefault('profile_revision', 0)
            same_request = (retry and not feedback and not any((additions or {}).values())
                            and (locks is None or locks == trip.get('locked_item_ids')))
            self._record_failure(trip)
            trip['revision_started_at'] = now()
            if not same_request or not trip.get('revision_request_id'):
                trip['revision_request_id'] = uuid4().hex
            if not same_request:
                for field in ('revision_checkpoint', 'revision_progress', 'detail_research_context'):
                    trip.pop(field, None)
            self._park_legacy_materials(trip)
            trip['revision_plan'] = copy.deepcopy(trip.get('plan'))
            trip['revision_pending_materials'] = copy.deepcopy(trip.get('pending_materials') or [])
            trip['revision_feedback'] = feedback or (trip['feedback'][-1] if retry and trip['feedback'] else '')
            old_material_ids = {s['id'] for s in trip.get('material_sources') or []}
            if trip.get('plan') and not trip.get('revision_base'):
                trip['revision_base'] = {'plan': copy.deepcopy(trip['plan']),
                    'plan_revision': trip.get('plan_revision'),
                    'feedback': list(trip['feedback']), 'locks': list(trip['locked_item_ids']),
                    'material_state': {k: copy.deepcopy(trip.get(k)) for k in
                        ('evidence_text', 'material_sources', 'pending_materials', 'intake', 'preview', 'source_evidence', 'core_direction', 'agent_work', 'discovery', 'trip_graph')}}
            if locks is not None:
                self._check_locks(trip, locks)
                trip['locked_item_ids'] = locks
            if feedback:
                trip['feedback'] = (trip['feedback'] + [feedback])[-20:]
            self._append_materials(trip, additions or {}, feedback)
            trip['revision_material_ids'] = [s['id'] for s in trip['material_sources']
                                            if s['id'] not in old_material_ids]
            if retry:
                for source in trip['material_sources']:
                    if source['kind'] == 'web_link' and source['status'] == 'unavailable':
                        source.update(status='pending', content='', message='等待重新读取')
            trip.update(revision=trip['revision'] + 1, status='running', error=None, agent_work=None, draft_plan=None)
            trip['revision_retry'] = {'revision': trip['revision'], 'explicit': bool(retry),
                                      'same_request': bool(same_request)}
            trip.pop('revision_stage', None)
            label = ('保留现有方案，继续完成未完成内容' if retry else '保留现有方案，正在理解这次调整') if trip.get('plan') else (
                '继续生成完整方案，已保存的有效进度会复用' if retry else '正在理解目标并生成完整方案')
            trip['events'] = (trip.get('events', []) if same_request else []) + [{'label': label, 'at': now()}]
            trip['events'] = trip['events'][-24:]
            self._save(db, trip)
        return public_trip(trip)

    def _append_materials(self, trip, additions, feedback):
        note, images = additions.get('note', ''), additions.get('images', [])
        if trip['images'] and images:
            raise StateError('BAD_IMAGE', '上一批图片尚未读完，请先重试；读完后可再补充新图片')
        if len(trip['images']) + len(images) > 3:
            raise StateError('BAD_IMAGE', '还有未处理图片，单轮最多处理3张；请先重试本轮')
        sources = trip.get('material_sources')
        if sources is None:
            sources = add_sources([], revision=1, brief=trip['brief'])
            if trip['evidence_text']:
                sources.append({'id': 'legacy', 'kind': 'legacy_material', 'status': 'read',
                                'label': '旧版材料（来源未分离）', 'content': trip['evidence_text']})
        try:
            trip['material_sources'] = add_sources(sources, revision=trip['revision'] + 1,
                note=note, feedback=feedback, images=images, links=additions.get('links', []),
                documents=additions.get('documents', []))
        except ValueError as error:
            raise StateError('MATERIAL_LIMIT', str(error)) from None
        trip['images'] += images
        if note:
            trip['evidence_text'] += '\n' + note

    def report_action(self, trip_id, token, revision, action_id, status):
        """Persist the user's report, never a supplier confirmation or purchase."""
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            trip = self._load(db, trip_id)
            self._authorize(trip, token)
            self._check_revision(trip, revision)
            if trip['status'] == 'running':
                raise StateError('BUSY', '准备内容正在更新，请等待结果', 409)
            if status not in ('user_reported', 'reopen'):
                raise StateError('BAD_INPUT', '仅支持本人报告已办或恢复待办')
            actions = ((trip.get('plan') or {}).get('preparation_pack') or {}).get('actions', [])
            action = next((a for a in actions if a.get('id') == action_id), None)
            if not action:
                raise StateError('BAD_INPUT', '未找到这项准备，请刷新后重试')
            if action.get('owner') != 'user' or action.get('status') not in ('ready', 'needs_user', 'user_reported'):
                raise StateError('BAD_INPUT', '这项仍需Agent研究，不能勾选成已办理')
            before = copy.deepcopy(trip['plan'])
            from .store_timeline import timeline_commit_fields
            baseline = {**trip, 'plan': before}
            if status == 'user_reported':
                action.setdefault('status_before_report', action['status'])
                action.update(status='user_reported', reported_at=now())
            else:
                action['status'] = action.pop('status_before_report', 'needs_user')
                action.pop('reported_at', None)
            trip['history'] = (trip['history'] + [{'plan': before,
                'plan_revision': trip.get('plan_revision'),
                'feedback': list(trip['feedback']), 'locks': list(trip['locked_item_ids'])}])[-5:]
            trip['revision'] += 1
            trip.update(timeline_commit_fields(baseline, trip['plan'], trip['revision'], kind='action_report'))
            trip['events'] = (trip['events'] + [{'label': '已保存你的办理标记；不是供应方确认', 'at': now()}])[-24:]
            action_path = [['key', 'preparation_pack'], ['key', 'actions'], ['id', action_id]]
            touches = [action_path + [['key', field]] for field in
                       ('status', 'reported_at', 'status_before_report')]
            self._save(db, trip, plan_write=True, plan_touches=touches)
        return public_trip(trip)

    def _check_revision(self, trip, revision):
        if type(revision) is not int or revision != trip['revision']:
            raise StateError('VERSION_CONFLICT', '方案已更新，请刷新后再试', 409)

    def _check_locks(self, trip, locks):
        items = {i['id'] for d in (trip.get('plan') or {}).get('days', []) for i in d['items']}
        if not isinstance(locks, list) or len(locks) > 50 or any(not isinstance(x, str) or x not in items for x in locks):
            raise StateError('BAD_LOCK', '只能保留当前方案中存在的安排')

    def commit_plan(self, trip_id, revision, plan, metrics, *, intake=None, preview=None, research=None,
                    trip_graph=None):
        trip = self.internal_get(trip_id)
        history = trip['history']
        if trip.get('revision_base'):
            history = (history + [trip['revision_base']])[-5:]
        bundle = {key: value for key, value in {'intake': intake, 'preview': preview,
                  'research': research, 'trip_graph': trip_graph}.items() if value is not None}
        return self.update_job(trip_id, revision, plan=plan, metrics=metrics, **bundle,
                               history=history, revision_base=None, status='ready', error=None, draft_plan=None,
                               event='方案已保存；报价与必要预约仍以实际确认结果为准')

    def commit_reviewed_plan(self, trip_id, revision, plan, metrics, review, *, intake=None,
                             preview=None, research=None, trip_graph=None, source_evidence=None,
                             pending_materials=None, revision_receipt=None, coach_memory=None):
        """Commit exactly the critic-reviewed JSON, bound to the current baseline.

        Schema and critic protocol run before this boundary. Hash checks and
        the complete plan write share the same SQLite transaction; no legacy
        semantic gate rewrites or rejects the accepted candidate afterward.
        """
        from .revision_plan import plan_sha256
        candidate, receipt = copy.deepcopy(plan), copy.deepcopy(review)
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            trip = self._load(db, trip_id)
            if trip['revision'] != revision or trip['status'] != 'running':
                raise StateError('STALE_JOB', '任务版本已变化', 409)
            if (not isinstance(receipt, dict) or receipt.get('verdict') != 'pass'
                    or receipt.get('candidate_sha256') != plan_sha256(candidate)
                    or receipt.get('previous_sha256') != plan_sha256(trip.get('plan'))):
                raise StateError('REVIEW_MISMATCH', '审查结果与当前方案不一致，已有方案保留', 409)
            if receipt.get('phase1_review'):
                from .phase1_sources import assert_review_sources, preserve_source_failures
                assert_review_sources(trip, receipt['phase1_review'])
                if source_evidence is not None:
                    source_evidence = preserve_source_failures(trip.get('source_evidence') or [], source_evidence)
            public_receipt = (self._bound_receipt(trip, candidate, revision_receipt, source_evidence)
                              if revision_receipt is not None else None)
            from .store_timeline import timeline_commit_fields
            from .traveler_profile import profile_commit_fields
            commit_fields = {**timeline_commit_fields(trip, candidate, revision),
                             **profile_commit_fields(trip, candidate)}
            if trip.get('revision_base'):
                trip['history'] = (trip['history'] + [trip['revision_base']])[-5:]
            bundle = {'intake': intake, 'preview': preview, 'research': research,
                      'trip_graph': trip_graph, 'source_evidence': source_evidence,
                      'pending_materials': pending_materials, 'coach_memory': coach_memory}
            trip.update({key: copy.deepcopy(value) for key, value in bundle.items() if value is not None})
            trip.update(plan=candidate, metrics={**copy.deepcopy(metrics), 'revision_review': receipt},
                        revision_base=None, status='ready', error=None)
            trip.update(commit_fields)
            if public_receipt is not None:
                trip['revision_receipt'] = public_receipt
            else:
                trip.pop('revision_receipt', None)
            self._finish_revision(trip)
            trip['events'] = (trip['events'] + [{'label': '本轮调整已审查并完整保存', 'at': now()}])[-24:]
            self._save(db, trip, plan_write=True)
        return public_trip(trip)

    @staticmethod
    def _bound_receipt(trip, candidate, receipt, evidence=None):
        from .revision_receipt import validate_receipt_binding
        from .validation import PlanError
        try:
            validate_receipt_binding(receipt, trip.get('plan'), candidate,
                detail_context={'detail_ledger': trip.get('material_detail_ledger') or {}},
                evidence=evidence if evidence is not None else trip.get('source_evidence') or [],
                materials=trip.get('material_sources') or [])
        except (PlanError, TypeError, ValueError) as error:
            raise StateError('RECEIPT_MISMATCH', '调整回执与已审方案或原始依据不一致，原方案保留', 409) from error
        return {**copy.deepcopy(receipt), 'trip_id': trip['id'],
                'revision': trip['revision'], 'committed_at': now()}

    def undo(self, trip_id, token, revision):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            trip = self._load(db, trip_id)
            self._authorize(trip, token)
            self._check_revision(trip, revision)
            if trip['status'] == 'running' or not (trip.get('revision_base') or trip['history']):
                raise StateError('CANNOT_UNDO', '当前没有可撤销的已完成修改', 409)
            previous = trip.get('revision_base') or trip['history'].pop()
            from .store_timeline import timeline_commit_fields
            timeline = timeline_commit_fields(trip, previous['plan'], trip['revision'] + 1, kind='undo')
            trip.update(plan=previous['plan'], feedback=previous['feedback'],
                        locked_item_ids=previous['locks'], revision=trip['revision'] + 1,
                        status='ready', error=None, revision_base=None)
            trip.update(timeline)
            if previous.get('material_state'):
                trip.update(previous['material_state'])
                trip['images'] = []
            source_ids = {source['id'] for source in trip.get('material_sources') or []}
            trip['pending_materials'] = [row for row in trip.get('pending_materials') or []
                                         if row['source_id'] in source_ids]
            trip['revision_material_ids'] = []
            for key in ('revision_checkpoint', 'revision_progress', 'revision_request_id', 'detail_research_context'):
                trip.pop(key, None)
            trip['events'].append({'label': '已恢复上个方案；没有改变任何外部订单', 'at': now()})
            self._save(db, trip, plan_write=True)
        return public_trip(trip)

    def delete(self, trip_id, token):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            self._authorize(self._load(db, trip_id), token)
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='uncertainty_claims'").fetchone():
                from .uncertainty_store import UncertaintyStore
                # Constructor migrations cannot run inside this deletion transaction.
                records = object.__new__(UncertaintyStore)
                records.connection_factory, records.path = self.connection, None
                records.clock = now
                records.purge_trip(trip_id, db=db)
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='uncertainty_runs'").fetchone():
                db.execute('DELETE FROM uncertainty_runs WHERE trip_id=?', (trip_id,))
            self._purge_autopilot(db, trip_id)
            db.execute('DELETE FROM trips WHERE id=?', (trip_id,))

    @staticmethod
    def _purge_autopilot(db, trip_id):
        # Caller owns the deletion transaction and has already authenticated.
        tables = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'autopilot_%'")}
        run_ids = []
        if 'autopilot_runs' in tables:
            run_ids = [row[0] for row in db.execute('SELECT id,data FROM autopilot_runs')
                       if json.loads(row[1]).get('trip_id') == trip_id]
        for run_id in run_ids:
            if {'autopilot_revision_builds', 'autopilot_operations'} <= tables:
                db.execute('DELETE FROM autopilot_revision_builds WHERE operation_id IN '
                           '(SELECT id FROM autopilot_operations WHERE run_id=?)', (run_id,))
            for table in ('autopilot_messages', 'autopilot_operations',
                          'autopilot_model_calls', 'autopilot_read_attempts'):
                if table in tables:
                    db.execute(f'DELETE FROM {table} WHERE run_id=?', (run_id,))
            db.execute('DELETE FROM autopilot_runs WHERE id=?', (run_id,))
        for table in ('autopilot_proposals', 'autopilot_revisions',
                      'autopilot_plan_ownership', 'autopilot_workspace', 'autopilot_requests',
                      'autopilot_intent_requests', 'autopilot_intent_revisions'):
            if table in tables:
                db.execute(f'DELETE FROM {table} WHERE trip_id=?', (trip_id,))
        if 'autopilot_goals' in tables:
            goal_ids = [row[0] for row in db.execute('SELECT id,data FROM autopilot_goals')
                        if json.loads(row[1]).get('trip_id') == trip_id]
            db.executemany('DELETE FROM autopilot_goals WHERE id=?', [(id_,) for id_ in goal_ids])

    def recover_interrupted(self):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            for row in db.execute('SELECT data FROM trips').fetchall():
                trip = json.loads(row[0])
                if trip['status'] == 'running':
                    error = '服务已重启，已有方案保留，可重试继续' if trip.get('plan') else '服务已重启，尚未生成完整方案，可重试继续'
                    trip.update(status='interrupted', error=error)
                    self._park_legacy_materials(trip)
                    self._finish_revision(trip)
                    self._save(db, trip)
