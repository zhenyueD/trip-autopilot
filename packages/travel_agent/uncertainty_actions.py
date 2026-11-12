"""No external calls: durable action prerequisites and result reconciliation."""
import json
from uuid import uuid4

from .uncertainty_contract import (
    UncertaintyError, canonical, check_version, evidence_current, fingerprint, require_text,
)


class ActionMethods:
    def get_action(self, trip_id, action_id, *, db=None):
        with self._using(db) as conn:
            return self._get(conn, 'actions', trip_id, action_id)

    def create_action(self, trip_id, revision, payload, *, db=None):
        action = self._base(trip_id, revision, payload)
        intent = payload.get('intent')
        if not isinstance(intent, dict) or not intent:
            raise UncertaintyError('BAD_ACTION', '动作必须保存明确目标')
        key = require_text(payload.get('idempotency_key'), 'idempotency_key')
        action.update({'intent': intent, 'owner': require_text(payload.get('owner'), 'owner'),
            'idempotency_key': key, 'claim_refs': payload.get('claim_refs', []),
            'evidence_refs': payload.get('evidence_refs', []), 'status': 'pending',
            'attempts': 0, 'result': None, 'recheck_needed': False, 'events': []})
        signature = fingerprint({key: action[key] for key in (
            'trip_id', 'revision', 'scope_id', 'intent', 'claim_refs', 'evidence_refs')})
        action['request_signature'] = signature
        with self._using(db, write=True) as conn:
            row = conn.execute('SELECT data FROM uncertainty_actions WHERE trip_id=? AND idempotency_key=?',
                               (trip_id, key)).fetchone()
            if row:
                existing = json.loads(row[0])
                if existing['request_signature'] != signature:
                    raise UncertaintyError('IDEMPOTENCY_CONFLICT', '相同幂等键不能用于不同动作', 409)
                return existing
            claims = self._action_claims(conn, trip_id, action)
            evidence = self._refs(conn, trip_id, action, action['evidence_refs'])
            conn.execute('INSERT INTO uncertainty_actions VALUES (?,?,?,?,?,?,?)',
                (action['id'], trip_id, revision, action['scope_id'], 1, key, canonical(action)))
            conn.executemany('INSERT INTO uncertainty_action_claims VALUES (?,?,?,?)',
                [(action['id'], c['id'], trip_id, c['version']) for c in claims])
            conn.executemany('INSERT INTO uncertainty_action_evidence VALUES (?,?,?,?)',
                [(action['id'], e['id'], trip_id, e['state_version']) for e in evidence])
            return action

    def _action_claims(self, db, trip_id, action):
        refs = action['claim_refs']
        if not isinstance(refs, list) or any(not isinstance(ref, dict) for ref in refs):
            raise UncertaintyError('BAD_REFERENCE', 'claim_refs必须为引用列表')
        if len({ref.get('id') for ref in refs}) != len(refs):
            raise UncertaintyError('BAD_REFERENCE', '不能重复引用同一断言')
        claims = []
        for ref in refs:
            claim = self._get(db, 'claims', trip_id, ref.get('id'))
            check_version(claim['version'], ref.get('version'))
            if (claim['revision'], claim['scope_id']) != (action['revision'], action['scope_id']):
                raise UncertaintyError('SCOPE_MISMATCH', '动作与依赖断言范围不匹配', 409)
            claims.append(claim)
        return claims

    def _save_action(self, db, action, *, reason):
        action = action | {'version': action['version'] + 1, 'updated_at': self.clock()}
        event = {'at': self.clock(), 'status': action['status'], 'version': action['version'], 'reason': reason}
        db.execute('UPDATE uncertainty_actions SET version=?,data=? WHERE id=? AND trip_id=?',
                   (action['version'], canonical(action), action['id'], action['trip_id']))
        db.execute('INSERT INTO uncertainty_action_events VALUES (?,?,?,?)',
                   (uuid4().hex, action['id'], action['trip_id'], canonical(event)))
        return action

    def _prerequisites(self, db, trip_id, action, authorization, budget):
        missing = []
        if (not isinstance(authorization, dict) or authorization.get('confirmed') is not True
                or canonical(authorization.get('intent')) != canonical(action['intent'])):
            missing.append('authorization')
        if not isinstance(budget, dict) or budget.get('allowed') is not True:
            missing.append('budget')
        try:
            claims = self._action_claims(db, trip_id, action)
            evidence = self._refs(db, trip_id, action, action['evidence_refs'])
            if not claims and not evidence:
                missing.append('dependencies_missing')
            if any(self.current_assessment(trip_id, c['id'], db=db)['tier'] != 'verified' for c in claims):
                missing.append('claim_not_verified')
            if any(not evidence_current(e, self.clock()) for e in evidence):
                missing.append('evidence_not_current')
            # A valid fact about another branch/date/party cannot authorize
            # this intent. Raw evidence alone has no semantic assessment.
            intent = action['intent']
            matching = [c for c in claims if c['entity_id'] == intent.get('entity_id')
                and canonical(c['conditions']) == canonical(intent.get('conditions'))
                and self.current_assessment(trip_id, c['id'], db=db)['tier'] == 'verified']
            if intent.get('operation') == 'reservation':
                matching = [c for c in matching if c['predicate'] == 'availability' and c['value'] is True]
            else:
                # New operation types need an explicit, server-owned prerequisite
                # policy before they can be dispatched; none exist in this pilot.
                matching = []
            if not matching:
                missing.append('verified_intent_dependency')
        except UncertaintyError as error:
            missing.append(error.code)
        return missing

    def check_action(self, trip_id, action_id, *, expected_version, authorization, budget, db=None):
        with self._using(db, write=True) as conn:
            action = self._get(conn, 'actions', trip_id, action_id)
            check_version(action['version'], expected_version)
            if action['status'] in {'running', 'succeeded', 'failed', 'outcome_unknown'}:
                return action
            missing = self._prerequisites(conn, trip_id, action, authorization, budget)
            action.update(status='blocked' if missing else 'ready', missing=missing,
                          authorization=authorization, budget=budget, recheck_needed=bool(missing))
            return self._save_action(conn, action, reason='prerequisites_checked')

    def start_action(self, trip_id, action_id, *, expected_version, authorization, budget, db=None):
        """Persist before external dispatch. Repeated dispatch attempts fail closed."""
        with self._using(db, write=True) as conn:
            action = self._get(conn, 'actions', trip_id, action_id)
            check_version(action['version'], expected_version)
            if action['status'] != 'ready':
                raise UncertaintyError('ACTION_NOT_READY', '动作未就绪；已发出动作须先查询结果', 409)
            missing = self._prerequisites(conn, trip_id, action, authorization, budget)
            if missing:
                action.update(status='blocked', missing=missing, recheck_needed=True)
                return self._save_action(conn, action, reason='pre_dispatch_check_failed')
            action.update(status='running', attempts=action['attempts'] + 1, dispatched_at=self.clock(),
                          authorization=authorization, budget=budget)
            return self._save_action(conn, action, reason='dispatch_reserved')

    def record_action_result(self, trip_id, action_id, result, *, expected_version, actor, db=None):
        if actor != 'tool':
            raise UncertaintyError('FORBIDDEN', '外部结果须由工具回读', 403)
        if not isinstance(result, dict):
            raise UncertaintyError('BAD_RESULT', '外部结果须是对象')
        with self._using(db, write=True) as conn:
            action = self._get(conn, 'actions', trip_id, action_id)
            check_version(action['version'], expected_version)
            if action['status'] == 'succeeded':
                return action
            if action['status'] not in {'running', 'outcome_unknown'}:
                raise UncertaintyError('BAD_ACTION_STATE', '动作尚未发出或已有最终结果', 409)
            matches = canonical(result.get('intent')) == canonical(action['intent'])
            final = (matches and result.get('final') is True
                     and result.get('readback_verified') is True
                     and bool(result.get('result_ref')) and bool(result.get('tool_call_id'))
                     and result.get('http_status') != 202)
            code = result.get('http_status')
            transport_success = code is None or (type(code) is int and 200 <= code < 300 and code != 202)
            if final and transport_success and result.get('status') == 'succeeded' and result.get('external_id'):
                action['status'] = 'succeeded'
            elif final and result.get('status') == 'failed':
                action['status'] = 'failed'
            else:
                action['status'] = 'outcome_unknown'
            action.update(result=result, recovery='query_external_result' if action['status'] == 'outcome_unknown' else None)
            return self._save_action(conn, action, reason='external_result_recorded')

    def recover_action(self, trip_id, action_id, *, expected_version, db=None):
        """A crash after reservation cannot prove whether dispatch occurred."""
        with self._using(db, write=True) as conn:
            action = self._get(conn, 'actions', trip_id, action_id)
            check_version(action['version'], expected_version)
            if action['status'] != 'running':
                return action
            action.update(status='outcome_unknown', recovery='query_external_result')
            return self._save_action(conn, action, reason='interrupted_dispatch')

    def authorize_retry(self, trip_id, action_id, *, expected_version, db=None):
        """Only a final readback proving no external effect can re-arm this key."""
        with self._using(db, write=True) as conn:
            action = self._get(conn, 'actions', trip_id, action_id)
            check_version(action['version'], expected_version)
            if action['status'] != 'failed' or (action.get('result') or {}).get('no_effect_confirmed') is not True:
                raise UncertaintyError('RECONCILE_REQUIRED', '重试前须回读确认没有外部效果', 409)
            action.update(status='pending', recheck_needed=True)
            return self._save_action(conn, action, reason='retry_after_negative_readback')

    def _invalidate_actions(self, db, trip_id, *, evidence_id=None, claim_id=None, reason):
        if evidence_id:
            rows = db.execute('SELECT action_id FROM uncertainty_action_evidence WHERE trip_id=? AND evidence_id=? '
                'UNION SELECT ac.action_id FROM uncertainty_action_claims ac '
                'JOIN uncertainty_assessments a ON a.claim_id=ac.claim_id AND a.trip_id=ac.trip_id '
                'JOIN uncertainty_assessment_evidence ae ON ae.assessment_id=a.id AND ae.trip_id=a.trip_id '
                'WHERE ac.trip_id=? AND ae.evidence_id=?', (trip_id, evidence_id, trip_id, evidence_id)).fetchall()
        else:
            rows = db.execute('SELECT action_id FROM uncertainty_action_claims WHERE trip_id=? AND claim_id=?',
                              (trip_id, claim_id)).fetchall()
        for row in rows:
            action = self._get(db, 'actions', trip_id, row[0])
            action['recheck_needed'] = True
            if action['status'] in {'pending', 'ready', 'blocked'}:
                action['status'] = 'pending'
            elif action['status'] in {'running', 'outcome_unknown'}:
                action.update(status='outcome_unknown', recovery='query_external_result')
            # Succeeded/failed are historical outcomes, never reset by changed facts.
            self._save_action(db, action, reason=reason)
