"""Persistent, model-directed action loop over actual registered tools."""
import copy
import json
import threading
from contextlib import contextmanager
from uuid import uuid4

from .autopilot_context import build_messages
from .autopilot_gateway import receipt
from .autopilot_history import compatible, history_state, project_history
from .autopilot_workspace_records import material_state
from .autopilot_final import OutcomeValidator
from .autopilot_model import call_tools
from .model import ModelError
from .phase1_budget_context import BudgetExhausted, use_budget
from .store import StateError


class AutopilotRunner:
    def __init__(self, store, gateway, *, model_caller=call_tools, budget_factory=None,
                 snapshot=None, outcome_validator=None, lease_seconds=120):
        self.store, self.gateway, self.model_caller = store, gateway, model_caller
        self.budget_factory, self.snapshot_reader = budget_factory, snapshot
        self.outcomes = outcome_validator or OutcomeValidator(store)
        self.lease_seconds = lease_seconds

    @contextmanager
    def _keep_lease(self, run_id, lease):
        stopped = threading.Event()
        def heartbeat():
            while not stopped.wait(self.lease_seconds / 3):
                try:
                    self.store.renew_run(run_id, lease, self.lease_seconds)
                except StateError:
                    return
        thread = threading.Thread(target=heartbeat, name='autopilot-lease', daemon=True)
        thread.start()
        try:
            yield
        finally:
            stopped.set()
            thread.join(timeout=1)

    def _snapshot(self, run_id, lease):
        with self.store.transaction() as db:
            run = self.store._lease(db, run_id, lease)
            trip = self.store.trip_store._load(db, run['trip_id'])
            goal = self.store._load(db, 'autopilot_goals', run['goal_id'])
        # Production supplies workspace's snapshot to include invalidation/work.
        now = self.store.clock()
        snapshot = (self.snapshot_reader(run_id, lease) if self.snapshot_reader else
                    dict(run=run, trip=trip, goal=goal, work=[],
                         material_state=material_state(trip, now)))
        provenance = history_state(trip, snapshot, now)
        if self.snapshot_reader:
            with self.store.transaction() as db:
                self.store._lease(db, run_id, lease)
                latest = self.store.trip_store._load(db, run['trip_id'])
            if provenance != history_state(latest, snapshot, self.store.clock()):
                raise StateError('CONTEXT_CHANGED', '读取上下文时资料发生变化，未发送旧状态请求', 409)
        snapshot['history_state'] = provenance
        return snapshot

    def _history(self, run_id, lease):
        return self.store.internal_get_messages(run_id, lease)

    def _native(self, message):
        content = message['content']
        if not isinstance(content, dict) or not isinstance(content.get('native'), dict):
            raise StateError('BAD_HISTORY', '恢复消息缺少协议状态', 409)
        return content['native']

    def _append(self, run_id, lease, message, message_id, material_state, *, history_state=None):
        content = {'native': message, 'material_state': material_state}
        if history_state is not None:
            content['history_state'] = history_state
        self.store.append_message(run_id, lease, message['role'],
                                  content, message_id)

    def _resume_tools(self, run_id, lease):
        saved = self._history(run_id, lease)
        if not saved:
            return
        assistant = next((m for m in reversed(saved) if m['role'] == 'assistant'), None)
        if assistant is None:
            return
        native = self._native(assistant)
        done = {self._native(m).get('tool_call_id') for m in saved if m['role'] == 'tool'}
        for call in native.get('tool_calls') or []:
            if call['id'] in done:
                continue
            current = self._snapshot(run_id, lease)
            preserved = compatible(assistant['content'].get('history_state'), current['history_state'])
            unverified_result = False
            stale = (assistant['content'].get('material_state') != current['material_state']
                     or not preserved)
            if stale:
                # Never execute a saved model instruction after its source context
                # changed. Preserve an explicit invalidation, without old payload.
                try:
                    prior = self.store.internal_get_operation(run_id, lease, call['id'])
                except StateError as error:
                    if error.code != 'NOT_FOUND':
                        raise
                    prior = None
                if prior and prior['status'] == 'succeeded':
                    # Recover a committed effect without dispatching the tool again.
                    # Incompatible provenance is hidden by the history projection.
                    payload = prior['result']
                    result = receipt(self.store, current['run'], result=payload, operation=prior,
                                     code=payload.get('error_code') if isinstance(payload, dict) else None)
                    unverified_result = (not preserved or not isinstance(payload, dict)
                        or payload.get('material_state') != current['history_state']['material_state'])
                else:
                    result = {'status': 'unknown' if prior and prior['status'] == 'in_flight' else 'rejected',
                              'error_code': 'STALE_TOOL_CONTEXT', 'operation_id': (prior or {}).get('id'),
                              'already_executed': False}
            else:
                before = current['history_state']
                result = self.gateway.execute(run_id, lease, call)
                current = self._snapshot(run_id, lease)
                unverified_result = not compatible(before, current['history_state'])
            tool = dict(role='tool', tool_call_id=call['id'], content=json.dumps(result, ensure_ascii=False))
            provenance = assistant['content'] if stale and not preserved else current
            self._append(run_id, lease, tool, 'tool:' + call['id'], provenance['material_state'],
                         history_state={} if unverified_result else provenance.get('history_state'))
            if result['status'] == 'unknown':
                raise StateError('OPERATION_UNKNOWN', '操作结果待核对，未重复执行', 409)

    def _context(self, run_id, lease):
        snapshot = self._snapshot(run_id, lease)
        history = self._history(run_id, lease)
        # Validate the durable protocol before projecting any stale facts.
        for message in history:
            self._native(message)
        messages = build_messages(snapshot['goal'], snapshot['trip'], snapshot['run']['event'],
            history=project_history(history, snapshot), work=snapshot['work'])
        return messages, snapshot

    def _step(self, run_id, lease, budget):
        self._resume_tools(run_id, lease)
        messages, snapshot = self._context(run_id, lease)
        remaining = snapshot['run']['limits']['input_tokens'] - snapshot['run']['usage']['input_tokens']
        output = snapshot['run']['limits']['output_tokens'] - snapshot['run']['usage']['output_tokens']
        if min(remaining, output) <= 0:
            raise BudgetExhausted('Token allowance exhausted')
        assistant, _usage = self.model_caller(messages, tools=self.gateway.catalog(run_id, lease),
            timeout=min(60, budget.remaining_seconds), max_tokens=min(4096, output),
            max_input_tokens=min(32768, remaining))
        previous_ids = {call['id'] for saved in self._history(run_id, lease)
                        for call in self._native(saved).get('tool_calls') or []}
        if any(call['id'] in previous_ids for call in assistant.get('tool_calls') or []):
            raise StateError('DUPLICATE_TOOL_CALL_ID', '模型重复了已持久调用标识，未重复操作', 409)
        self._append(run_id, lease, assistant, 'assistant:' + uuid4().hex, snapshot['material_state'],
                     history_state=snapshot['history_state'])
        if assistant.get('tool_calls'):
            self._resume_tools(run_id, lease)
            return None
        return self.outcomes.validate(run_id, lease, assistant)

    def run(self, run_id, worker_id=None):
        from .autopilot_budget import PersistentBudget
        worker_id = worker_id or uuid4().hex
        lease = self.store.claim_run(run_id, worker_id, self.lease_seconds)
        factory = self.budget_factory or PersistentBudget
        budget = factory(self.store, run_id, lease, epoch_clock=self.store.clock)
        with self._keep_lease(run_id, lease), use_budget(budget):
            try:
                while True:
                    self.store.assert_dispatch(run_id, lease)
                    outcome = self._step(run_id, lease, budget)
                    if outcome is not None:
                        status = 'completed' if outcome['outcome_status'] == 'complete' else 'waiting'
                        return self.store.finish_run(run_id, lease, status, outcome)
            except Exception as error:
                code = getattr(error, 'code', None) or getattr(error, 'usage', {}).get('error_code') or ('BUDGET_EXCEEDED' if isinstance(error, BudgetExhausted) else 'EXECUTION_ERROR')
                result = dict(outcome_status='partial', error_code=code,
                    user_summary='本轮已停止，已有成果和原方案保留；可查看具体阻塞与实际操作记录。',
                    unresolved=[code], goal_confirmation='awaiting_user')
                return self.store.finish_run(run_id, lease, 'waiting', result)
