"""Validate a user-facing outcome against durable, current tool receipts."""
import json

from .autopilot_contract import fingerprint
from .autopilot_advice import advice_current, advice_integrity
from .autopilot_workspace_records import material_state
from .store import StateError

OUTCOMES = {'complete', 'ready_for_user', 'waiting_for_input', 'waiting_for_job', 'partial', 'blocked'}
KEYS = {'outcome_status', 'user_summary', 'decision_refs', 'revision_refs', 'receipt_refs',
        'unresolved', 'next_required_input', 'scheduled_job_refs'}
REF_KEYS = ('decision_refs', 'revision_refs', 'receipt_refs', 'scheduled_job_refs')


def _reject(code):
    raise StateError(code, '助手结论缺少可核对的实际结果，已保存进度', 409)


def parse_final(message):
    if not isinstance(message, dict) or message.get('tool_calls'):
        _reject('FINAL_FORMAT_INVALID')
    raw = message.get('content')
    if not isinstance(raw, str) or len(raw) > 20000:
        _reject('FINAL_FORMAT_INVALID')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                _reject('FINAL_FORMAT_INVALID')
            result[key] = value
        return result
    try:
        result = json.loads(raw, object_pairs_hook=unique)
    except (ValueError, RecursionError):
        _reject('FINAL_FORMAT_INVALID')
    if (not isinstance(result, dict) or set(result) != KEYS or not isinstance(result['outcome_status'], str) or result['outcome_status'] not in OUTCOMES
            or not isinstance(result['user_summary'], str) or not result['user_summary'].strip()):
        _reject('FINAL_FORMAT_INVALID')
    for key in (*REF_KEYS, 'unresolved'):
        if (not isinstance(result[key], list) or len(result[key]) > 30
                or any(not isinstance(i, str) or not i or len(i) > 2000 for i in result[key])):
            _reject('FINAL_FORMAT_INVALID')
    if result['next_required_input'] is not None and not isinstance(result['next_required_input'], str):
        _reject('FINAL_FORMAT_INVALID')
    return result


class OutcomeValidator:
    def __init__(self, store, resolver=None):
        self.store, self.resolver = store, resolver

    def _records(self, db, run, trip):
        records = {}
        for row in db.execute('SELECT data FROM autopilot_operations WHERE run_id=?', (run['id'],)):
            operation = json.loads(row[0])
            result = operation.get('result')
            if operation['status'] != 'succeeded' or not isinstance(result, dict) or result.get('error_code'):
                continue
            if result.get('material_state', material_state(trip, self.store.clock())) != material_state(trip, self.store.clock()):
                continue
            if result.get('scope') == 'advice' and (not advice_integrity(operation)
                    or not advice_current(result, run, trip, self.store.clock())):
                continue
            records[operation['id']] = {'kind': 'receipt', 'value': result}
            mapping = {'proposal_id': 'proposal', 'assessment_id': 'assessment', 'revision_id': 'revision',
                       'input_id': 'input', 'pending_input_id': 'input', 'job_id': 'job'}
            if result.get('scope') == 'advice':
                mapping.update(advice_id='advice', assessment_id='advice_assessment')
            for key, kind in mapping.items():
                if isinstance(result.get(key), str):
                    records[result[key]] = {'kind': kind, 'value': result}
        return records

    def validate(self, run_id, lease, message):
        outcome = parse_final(message)
        with self.store.transaction() as db:
            run = self.store._lease(db, run_id, lease)
            trip = self.store.trip_store._load(db, run['trip_id'])
            records = self._records(db, run, trip)
            for key in REF_KEYS:
                for ref in outcome[key]:
                    record = self.resolver(db, run, trip, ref) if self.resolver else records.get(ref)
                    if record:
                        records[ref] = record
                    allowed = {'decision_refs': {'proposal', 'assessment', 'advice', 'advice_assessment'}, 'revision_refs': {'revision'},
                               'receipt_refs': {'receipt'}, 'scheduled_job_refs': {'job'}}[key]
                    if not record or record['kind'] not in allowed:
                        _reject('FINAL_REFERENCE_INVALID')
            status = outcome['outcome_status']
            advice_refs = [r for key in REF_KEYS for r in outcome[key]
                           if records[r]['value'].get('scope') == 'advice']
            if advice_refs:
                if status != 'partial':
                    _reject('FINAL_ADVICE_SCOPE')
                self._advice_delivery(outcome, records, advice_refs)
            if status == 'complete':
                self._complete(outcome, records, run, trip)
            if status == 'ready_for_user':
                if not any(records[r]['kind'] == 'proposal' for r in outcome['decision_refs']):
                    _reject('FINAL_EVIDENCE_MISSING')
            if status == 'waiting_for_job' and not outcome['scheduled_job_refs']:
                _reject('FINAL_EVIDENCE_MISSING')
            if status == 'waiting_for_input':
                record = (self.resolver(db, run, trip, outcome['next_required_input'])
                          if self.resolver else records.get(outcome['next_required_input']))
                if not record or record['kind'] != 'input':
                    _reject('FINAL_EVIDENCE_MISSING')
        # An agent outcome is not a user confirmation that closes the long-lived goal.
        return dict(outcome, goal_confirmation='awaiting_user')

    def _advice_delivery(self, outcome, records, references):
        result = records[references[0]]['value']
        advice_id, assessment_id = result.get('advice_id'), result.get('assessment_id')
        if (result.get('verdict') != 'pass'
                or result.get('semantic', {}).get('status') != 'evaluated'
                or outcome['decision_refs'].count(advice_id) != 1
                or outcome['decision_refs'].count(assessment_id) != 1
                or records[advice_id]['kind'] != 'advice'
                or records[assessment_id]['kind'] != 'advice_assessment'
                or any(records[ref]['value'] != result for ref in references)
                or outcome['user_summary'] != result['advice']['text']
                or not set(result['advice']['unknowns']).issubset(outcome['unresolved'])):
            _reject('FINAL_ADVICE_MISMATCH')

    def _complete(self, outcome, records, run, trip):
        if outcome['unresolved'] or outcome['next_required_input']:
            _reject('FINAL_EVIDENCE_MISSING')
        current_plan = fingerprint(trip.get('plan'))
        for ref in outcome['decision_refs']:
            record = records[ref]
            assessment = record['value']
            if (record['kind'] == 'assessment' and assessment.get('goal_satisfied') is True
                    and assessment.get('goal_id') == run['goal_id']
                    and assessment.get('goal_version') == run['goal_version']
                    and assessment.get('plan_sha256') == current_plan
                    and assessment.get('semantic', {}).get('status') == 'evaluated'
                    and assessment.get('material_state') == material_state(trip, self.store.clock())
                    and type(assessment.get('valid_until')) in (int, float)
                    and assessment['valid_until'] > self.store.clock()):
                return
        _reject('FINAL_EVIDENCE_MISSING')
