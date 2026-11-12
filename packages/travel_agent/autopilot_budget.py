"""Durable, opt-in accounting at the physical model transport boundary.

Preflight counts reserve capacity, never represent provider billing. Unknown
usage retains the reservation and the store fences all subsequent dispatch.
"""
from contextvars import ContextVar
import json
import re
import threading
import time
from uuid import uuid4

from .phase1_budget_context import BudgetExhausted
from .store import StateError


class PersistentBudget:
    supports_read_requests = True
    _READ_KINDS = frozenset({'public_page_http', 'source_redirect_http'})
    _GOOGLE_URL = re.compile(
        r'https://generativelanguage\.googleapis\.com/v1beta/models/[A-Za-z0-9._-]{1,120}:generateContent')

    def __init__(self, store, run_id, lease, *, clock=time.monotonic,
                 epoch_clock=time.time, max_read_requests=12):
        run = store.internal_get_run(run_id)
        self.store, self.run_id, self.lease = store, run_id, lease
        self._clock = clock
        self.deadline = clock() + max(0., run['deadline'] - epoch_clock())
        self.started_at = self.deadline - run['limits']['wall_seconds']
        self.level, self.label = 'L0', run_id
        self._root, self.parent = self, None
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._inflight = False
        self._prepared = ContextVar('autopilot_preparation_' + uuid4().hex, default=None)
        self._consumed = set()
        self._children, self._closed = {}, False
        self.max_calls, self._calls_used = run['limits']['model_calls'], 0
        self._initialize_reads(run, max_read_requests)

    def _initialize_reads(self, run, maximum):
        if type(maximum) is not int or maximum < 1:
            raise ValueError('max_read_requests must be a positive integer')
        # Physical page/redirect reads are independent from model/tool calls.
        # Existing persisted caps remain authoritative and can only tighten.
        cap = maximum
        with self.store.transaction() as db:
            self.store._lease(db, self.run_id, self.lease)
            db.execute("""CREATE TABLE IF NOT EXISTS autopilot_read_limits (
                run_id TEXT PRIMARY KEY, cap INTEGER NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS autopilot_read_attempts (
                id TEXT PRIMARY KEY, run_id TEXT NOT NULL, kind TEXT NOT NULL,
                started_at REAL NOT NULL, status TEXT NOT NULL, deadline REAL NOT NULL,
                error TEXT, billing TEXT NOT NULL, lease_generation INTEGER NOT NULL,
                finished_at REAL)""")
            db.execute('INSERT OR IGNORE INTO autopilot_read_limits VALUES (?,?)',
                       (self.run_id, cap))
            db.execute('UPDATE autopilot_read_limits SET cap=MIN(cap,?) WHERE run_id=?',
                       (cap, self.run_id))

    def _begin_read(self, kind):
        if not self.can_publish():
            raise self._read_rejection('Read deadline exhausted', 'READ_DEADLINE_EXHAUSTED')
        try:
            with self.store.transaction() as db:
                run = self.store.assert_dispatch(self.run_id, self.lease, db=db)
                goal = self.store._load(db, 'autopilot_goals', run['goal_id'])
                if not set(goal['authorization']['tools']) & {'read_source', 'research_fact', 'delegate'}:
                    raise self._read_rejection('Read not authorized by current goal', 'READ_NOT_AUTHORIZED')
                cap = db.execute('SELECT cap FROM autopilot_read_limits WHERE run_id=?',
                                 (self.run_id,)).fetchone()[0]
                count = db.execute('SELECT COUNT(*) FROM autopilot_read_attempts WHERE run_id=?',
                                   (self.run_id,)).fetchone()[0]
                if count >= cap:
                    raise self._read_rejection('Persistent read request limit exhausted', 'READ_LIMIT_EXHAUSTED')
                id_ = uuid4().hex
                db.execute('INSERT INTO autopilot_read_attempts VALUES (?,?,?,?,?,?,?,?,?,?)',
                    (id_, self.run_id, kind, self.store.clock(), 'pending',
                     min(run['deadline'], self.store.clock() + self.remaining_seconds),
                     None, 'not_applicable', self.lease['generation'], None))
            return PersistentReadLease(self, id_)
        except StateError as error:
            raise self._read_rejection('Read reservation rejected: ' + error.code, error.code) from error

    @staticmethod
    def _read_rejection(message, code):
        error = BudgetExhausted(message)
        error.code = code
        error.usage = {'timeout_stage': 'preflight', 'request_delivery_status': 'not_sent'}
        return error

    @property
    def remaining_seconds(self):
        if self._closed or (self.parent and not self.parent.can_publish()):
            return 0.
        return max(0., self.deadline - self._clock())

    def can_publish(self):
        return self.remaining_seconds > 0

    def prepare_request(self, input_tokens, output_tokens):
        self._prepared.set(None)
        if any(type(v) is not int or v < 0 for v in (input_tokens, output_tokens)):
            raise BudgetExhausted('Invalid request token measurement')
        self._prepared.set((uuid4().hex, input_tokens, output_tokens))

    def prepare_http_request(self, request):
        """Measure final encoded payload, including schema/tools and actual cap."""
        self._prepared.set(None)
        try:
            body = request.data
            payload = json.loads(body)
            if self._GOOGLE_URL.fullmatch(request.full_url):
                return self._prepare_google_request(request, payload)
            maximum = payload['max_tokens']
            if type(maximum) is not int or maximum <= 0:
                raise ValueError('output cap')
            messages = payload['messages']
            if not isinstance(messages, list) or not messages:
                raise ValueError('messages')
            if payload['model'] == 'deepseek-v4-pro':
                from .writer_tokenizer import measure_writer_input
                effort = payload.get('reasoning_effort', 'none')
                if payload.get('thinking', {}).get('type') == 'enabled' and effort == 'none':
                    raise ValueError('unknown reasoning effort')
                measured = measure_writer_input(messages, model=payload['model'],
                                                reasoning_effort=effort)
                count = measured['preflight_input_tokens']
                extras = {k: v for k, v in payload.items() if k in
                          ('tools', 'tool_choice', 'functions', 'function_call', 'response_format')}
                # JSON schema/tool envelopes are not covered by the writer encoder.
                # Add every encoded byte plus conservative framing as capacity only.
                if extras:
                    count += len(json.dumps(extras, ensure_ascii=False).encode('utf-8')) + 256
                kind = 'official_public_reference_plus_extra_utf8_estimate'
            else:
                # Full JSON includes messages, schemas and tools. UTF-8 bytes plus
                # role/framing margin is conservative for supported text payloads;
                # this is explicitly an estimate, not an exact tokenizer claim.
                if payload['model'] != 'gpt-4.1-mini':
                    raise ValueError('unsupported model measurement')
                count = len(body) + 256 * (len(messages) + 1)
                kind = 'utf8_upper_bound_estimate'
            self.prepare_request(count, maximum)
            return dict(input_tokens=count, output_tokens=maximum, count_kind=kind)
        except Exception as error:
            self._prepared.set(None)
            raise BudgetExhausted('HTTP request measurement unavailable') from error

    def _prepare_google_request(self, request, payload):
        """Only the existing text-only Google Search request, not arbitrary tools."""
        if (request.get_method() != 'POST' or not isinstance(payload, dict)
                or set(payload) != {'contents', 'tools', 'generationConfig'}
                or payload['tools'] != [{'google_search': {}}]):
            raise ValueError('Unsupported Google request shape')
        contents, config = payload['contents'], payload['generationConfig']
        if (not isinstance(contents, list) or len(contents) != 1
                or not isinstance(config, dict)
                or set(config) != {'temperature', 'maxOutputTokens'}):
            raise ValueError('Unsupported Google request configuration')
        maximum = config['maxOutputTokens']
        if type(maximum) is not int or maximum <= 0:
            raise ValueError('Invalid Google output cap')
        for content in contents:
            if (not isinstance(content, dict) or set(content) != {'role', 'parts'}
                    or content['role'] != 'user' or not isinstance(content['parts'], list)
                    or not content['parts'] or any(not isinstance(part, dict)
                        or set(part) != {'text'} or not isinstance(part['text'], str)
                        for part in content['parts'])):
                raise ValueError('Unsupported Google input modality')
        # Includes tools and generation schema bytes; not provider billing or a
        # bound on undisclosed server-added search/tool prompts. Settle actuals.
        count = len(request.data) + 256 * (len(contents) + len(payload['tools']) + 1)
        self.prepare_request(count, maximum)
        return dict(input_tokens=count, output_tokens=maximum,
                    count_kind='google_utf8_capacity_estimate')

    def transport_usage(self, request, raw):
        """Provider-authoritative Google counters; legacy chat parser otherwise."""
        if not self._GOOGLE_URL.fullmatch(request.full_url):
            from .phase1_budget_context import transport_usage
            return transport_usage(raw)
        try:
            body = json.loads(raw)
            metadata = body.get('usageMetadata') if isinstance(body, dict) else None
        except (ValueError, TypeError):
            metadata = None
        if not isinstance(metadata, dict):
            return {'usage_complete': False}
        return _google_usage(metadata)

    def begin_call(self, kind, operation_id=None):
        if kind in self._READ_KINDS:
            return self._begin_read(kind)
        with self._lock:
            prepared = self._prepared.get()
            self._prepared.set(None)
            if prepared is None or prepared[0] in self._root._consumed:
                raise BudgetExhausted('Physical request has no fresh measurement')
            self._root._consumed.add(prepared[0])
            while self._root._inflight:
                remaining = self.remaining_seconds
                if remaining <= 0:
                    raise BudgetExhausted('Execution deadline while waiting for physical call')
                self._condition.wait(timeout=remaining)
            scope = self
            while scope is not None:
                if not scope.can_publish() or scope._calls_used >= scope.max_calls:
                    raise BudgetExhausted('Execution deadline or call limit exhausted')
                scope = scope.parent
            call_id = uuid4().hex
            try:
                result = self.store.reserve_model_call(self.run_id, self.lease,
                    call_id, prepared[1], prepared[2])
            except StateError as error:
                raise BudgetExhausted('Persistent reservation rejected: ' + error.code) from error
            if result['disposition'] != 'execute':
                raise BudgetExhausted('Physical request cannot be redispatched')
            self._root._inflight = True
            scope = self
            while scope is not None:
                scope._calls_used += 1
                scope = scope.parent
            return PersistentCallLease(self, call_id)

    def child(self, level, key, *, seconds=None, max_calls=None):
        with self._lock:
            name = (level, str(key))
            if name not in self._children:
                child = object.__new__(type(self))
                child.__dict__ = self.__dict__.copy()
                child._root, child.parent = self._root, self
                child.level, child.label = level, str(key)
                child.deadline = min(self.deadline, self._clock() +
                                     (self.remaining_seconds if seconds is None else seconds))
                child.max_calls = self.max_calls if max_calls is None else max_calls
                child._calls_used, child._children, child._closed = 0, {}, False
                self._children[name] = child
            return self._children[name]

    def snapshot(self):
        run = self.store.internal_get_run(self.run_id)
        remaining = max(0, run['limits']['model_calls'] - run['usage']['model_calls'])
        with self.store.transaction() as db:
            reads = db.execute('SELECT COUNT(*) FROM autopilot_read_attempts WHERE run_id=?',
                               (self.run_id,)).fetchone()[0]
            cap = db.execute('SELECT cap FROM autopilot_read_limits WHERE run_id=?',
                             (self.run_id,)).fetchone()[0]
        return dict(level=self.level, deadline=self.deadline, read_requests=reads, max_read_requests=cap,
                    remaining_seconds=self.remaining_seconds, usage=run['usage'],
                    calls_remaining=min(remaining, max(0,self.max_calls-self._calls_used)))

    def close(self, reason='returned'):
        with self._condition:
            self._closed = True
            self._condition.notify_all()


class PersistentCallLease:
    def __init__(self, budget, call_id):
        self.budget, self.call_id = budget, call_id
        self.deadline, self._settled = budget.deadline, False

    @property
    def remaining_seconds(self):
        return self.budget.remaining_seconds

    def _settle(self, usage):
        with self.budget._lock:
            if self._settled:
                return False
            usage = usage if isinstance(usage, dict) else {}
            counts = [usage.get(key) for key in ('input_tokens', 'output_tokens')]
            counts = [v if type(v) is int and v >= 0 else None for v in counts]
            try:
                result = self.budget.store.settle_usage(self.budget.run_id,
                    self.budget.lease, self.call_id, *counts)
            except StateError as error:
                self.budget._root.close()
                raise BudgetExhausted('Usage settlement rejected: ' + error.code) from error
            finally:
                self.budget._root._inflight = False
                self.budget._condition.notify_all()
            self._settled = True
            return result['status'] == 'settled' and self.budget.can_publish()

    def complete(self, value):
        return self._settle(value.get('usage', {}) if isinstance(value, dict) else {})

    def fail(self, error_type, usage=None):
        return self._settle(usage)


class PersistentReadLease:
    """Safe read receipt, separate from model billing and token accounting."""
    def __init__(self, budget, call_id):
        self.budget, self.call_id = budget, call_id
        self.deadline = budget.deadline

    @property
    def remaining_seconds(self):
        return self.budget.remaining_seconds

    def _finish(self, status, error=None):
        try:
            with self.budget.store.transaction() as db:
                self.budget.store._lease(db, self.budget.run_id, self.budget.lease)
                row = db.execute('SELECT status,lease_generation FROM autopilot_read_attempts WHERE id=?',
                                 (self.call_id,)).fetchone()
                if row is None or row[0] != 'pending' or row[1] != self.budget.lease['generation']:
                    return False
                db.execute('UPDATE autopilot_read_attempts SET status=?,error=?,finished_at=? WHERE id=?',
                           (status, error, self.budget.store.clock(), self.call_id))
            return self.budget.can_publish()
        except StateError as failure:
            raise BudgetExhausted('Read settlement rejected: ' + failure.code) from failure

    def complete(self, value):
        return self._finish('succeeded')

    def fail(self, error_type, usage=None):
        # Only exception class names, never URLs, bodies or credentials.
        return self._finish('failed', str(error_type)[:100])


def _google_usage(metadata):
    """Include thoughts/tool prompts, without equating omitted counters to zero."""
    def count(key):
        value = metadata.get(key)
        return value if type(value) is int and value >= 0 else None
    prompt, candidates, thoughts, tool, total = [count(key) for key in (
        'promptTokenCount', 'candidatesTokenCount', 'thoughtsTokenCount',
        'toolUsePromptTokenCount', 'totalTokenCount')]
    result = {}
    # The official total includes prompt+tool prompt+thoughts+candidates. Only
    # an exact residual can establish an omitted tool-use count (including 0).
    if ('toolUsePromptTokenCount' not in metadata
            and all(value is not None for value in (prompt, candidates, thoughts, total))):
        residual = total - prompt - candidates - thoughts
        if residual >= 0:
            tool = residual
            result['google_tool_count_basis'] = 'provider_total_residual'
    for key, value in (('google_prompt_tokens', prompt), ('google_candidate_tokens', candidates),
                       ('google_thought_tokens', thoughts), ('google_tool_prompt_tokens', tool)):
        if value is not None:
            result[key] = value
    if prompt is not None and tool is not None:
        result['input_tokens'] = prompt + tool
    if candidates is not None and thoughts is not None:
        result['output_tokens'] = candidates + thoughts
    known = 'input_tokens' in result and 'output_tokens' in result
    if 'totalTokenCount' in metadata and (total is None or
            (known and total != result['input_tokens'] + result['output_tokens'])):
        result.pop('input_tokens', None)
        result.pop('output_tokens', None)
        result['google_usage_error'] = 'inconsistent_total'
    result['usage_complete'] = 'input_tokens' in result and 'output_tokens' in result
    return result
