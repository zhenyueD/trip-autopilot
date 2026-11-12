"""Versioned single-card state and idempotent receipts, isolated from TripStore."""
from __future__ import annotations

import hashlib
import json
import re
import secrets

from .comparison_card_budget import DurableBudget, now
from .store import StateError


def fingerprint(action, body):
    return hashlib.sha256(json.dumps([action, body], sort_keys=True,
                                    ensure_ascii=False).encode()).hexdigest()


class ComparisonStore:
    def __init__(self, state_dir, case_id):
        self.case_id = case_id
        self.budget = DurableBudget(state_dir)
        with self.budget.connection() as db:
            db.execute('CREATE TABLE IF NOT EXISTS comparison_state '
                       '(id TEXT PRIMARY KEY, data TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS comparison_requests '
                       '(id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, status TEXT NOT NULL, '
                       'result TEXT, error TEXT)')
            initial = {'case_id': case_id, 'card': None, 'selection': None, 'version': 0, 'status': 'empty',
                       'last_error': None, 'active_request_id': None,
                       'csrf_token': secrets.token_urlsafe(32)}
            db.execute('INSERT OR IGNORE INTO comparison_state VALUES (?, ?)',
                       (case_id, json.dumps(initial)))

    def _load(self, db):
        return json.loads(db.execute('SELECT data FROM comparison_state WHERE id=?', (self.case_id,)).fetchone()[0])

    def _save(self, db, state):
        db.execute('UPDATE comparison_state SET data=? WHERE id=?',
                   (json.dumps(state, ensure_ascii=False), self.case_id))

    def public(self, state):
        return {key: value for key, value in state.items() if key != 'active_request_id'}

    def snapshot(self):
        with self.budget.connection() as db:
            return self.public(self._load(db))

    def recover_interrupted(self):
        """Only called by the exclusively locked server at process startup."""
        with self.budget.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            state = self._load(db)
            if state['active_request_id']:
                error = {'code': 'INTERRUPTED', 'message_zh': '上次处理被中断，原比较卡已保留；未自动重试'}
                db.execute("UPDATE comparison_requests SET status='failed', error=? WHERE id=?",
                           (json.dumps(error), state['active_request_id']))
                state.update(status='error', last_error=error, active_request_id=None)
                self._save(db, state)

    def _replay(self, db, request_id, digest):
        row = db.execute('SELECT fingerprint,status,result,error FROM comparison_requests WHERE id=?',
                         (request_id,)).fetchone()
        if not row:
            return None
        if row[0] != digest:
            raise StateError('IDEMPOTENCY_CONFLICT', '同一请求编号不能用于不同操作', 409)
        if row[1] == 'running':
            raise StateError('REQUEST_IN_PROGRESS', '这次请求还在处理中，请稍后刷新', 409)
        if row[1] == 'failed':
            error = json.loads(row[3])
            raise StateError(error['code'], error['message_zh'], error.get('status', 502))
        return json.loads(row[2])

    def begin(self, action, body):
        for field, code in (('option_id', 'BAD_OPTION'), ('answer_id', 'BAD_ANSWER')):
            if field in body and (not isinstance(body[field], str) or not body[field].strip()):
                raise StateError(code, '选项编号必须是非空字符串')
        request_id = body.get('request_id')
        if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', request_id):
            raise StateError('BAD_REQUEST_ID', '请求编号格式不正确')
        digest = fingerprint(action, {**body, 'case_id': self.case_id})
        with self.budget.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            replay = self._replay(db, request_id, digest)
            if replay is not None:
                return replay, True
            state = self._load(db)
            if state['active_request_id']:
                raise StateError('REQUEST_IN_PROGRESS', '正在更新比较卡，请稍后再操作', 409)
            if action != 'generate':
                if type(body.get('expected_version')) is not int or body['expected_version'] != state['version']:
                    raise StateError('VERSION_CONFLICT', '比较卡已更新，请刷新后再选择', 409)
                if not state['card']:
                    raise StateError('CARD_REQUIRED', '请先生成比较卡', 409)
            if action == 'generate' and state['card']:
                raise StateError('CARD_EXISTS', '已有比较卡，可直接调整偏好', 409)
            self._validate_action(action, body, state)
            db.execute('INSERT INTO comparison_requests VALUES (?, ?, ?, NULL, NULL)',
                       (request_id, digest, 'running'))
            state.update(active_request_id=request_id, last_error=None)
            if action != 'select':
                state['status'] = 'generating' if action == 'generate' else 'adjusting'
            self._save(db, state)
            return state, False

    def _validate_action(self, action, body, state):
        if action == 'select':
            if body.get('option_id') not in {option['id'] for option in state['card']['options']}:
                raise StateError('BAD_OPTION', '请选择卡片中的一个选项')
        if action == 'adjust':
            feedback = body.get('feedback')
            if not isinstance(feedback, str) or not 1 <= len(feedback.strip()) <= 2000:
                raise StateError('BAD_FEEDBACK', '请用 1 至 2000 字说明想调整什么')
            answer = body.get('answer_id')
            if answer is not None and answer not in {choice['id'] for choice in state['card']['question']['choices']}:
                raise StateError('BAD_ANSWER', '问题选项已经更新，请重新选择')

    def finish(self, request_id, expected_version, *, card=None, option_id=None):
        with self.budget.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            state = self._load(db)
            if state['active_request_id'] != request_id or state['version'] != expected_version:
                raise StateError('VERSION_CONFLICT', '比较卡已变化，未覆盖当前版本', 409)
            if card is not None:
                state['card'] = card
            if option_id is not None:
                state['selection'] = {'option_id': option_id, 'selected_at': now(), 'request_id': request_id}
            state.update(version=state['version'] + 1, status='ready',
                         active_request_id=None, last_error=None)
            self._save(db, state)
            result = self.public(state)
            db.execute("UPDATE comparison_requests SET status='done',result=? WHERE id=?",
                       (json.dumps(result, ensure_ascii=False), request_id))
            return result

    def fail(self, request_id, error):
        record = {'code': error.code, 'message_zh': error.message, 'status': error.status}
        with self.budget.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            state = self._load(db)
            if state['active_request_id'] == request_id:
                state.update(status='error', last_error=record, active_request_id=None)
                self._save(db, state)
                db.execute("UPDATE comparison_requests SET status='failed',error=? WHERE id=?",
                           (json.dumps(record, ensure_ascii=False), request_id))

    def begin_traffic(self, request_id):
        if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', request_id):
            raise StateError('BAD_REQUEST_ID', '请求编号格式不正确')
        digest = fingerprint('traffic', {'request_id': request_id, 'case_id': self.case_id})
        with self.budget.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT fingerprint,status FROM comparison_requests WHERE id=?',
                             (request_id,)).fetchone()
            if row:
                if row[0] != digest:
                    raise StateError('IDEMPOTENCY_CONFLICT', '请求编号已被其他操作使用', 409)
                return False
            db.execute('INSERT INTO comparison_requests VALUES (?, ?, ?, NULL, NULL)',
                       (request_id, digest, 'running'))
            return True

    def finish_traffic(self, request_id, status):
        # Google route metrics, raw responses and names are deliberately excluded.
        with self.budget.connection() as db:
            db.execute("UPDATE comparison_requests SET status='done',result=? WHERE id=?",
                       (json.dumps({'traffic_status': status}), request_id))
