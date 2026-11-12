"""Shared, monotonic execution limits; callbacks return data, never publish it.

Create L0 at request acceptance (before queueing), reuse its remaining allowance
across attempts, and charge each physical model/tool attempt exactly once with ``call`` or
``begin_call``. A non-cooperative callback can finish after timeout; its output
is fenced and only numeric usage remains observable. External side effects must
use the application's durable action/idempotency boundary, not this executor.
"""
import copy
from contextlib import contextmanager
from dataclasses import dataclass
import math
import threading
import time


DEFAULT_LIMITS = {'L0': (600.0, 30), 'L1': (120.0, 8), 'L2': (30.0, 3)}


@dataclass(frozen=True)
class BudgetCallResult:
    status: str
    value: object = None
    reason: str = ''
    call_id: str | None = None


def _limits(level, seconds, max_calls):
    if level not in DEFAULT_LIMITS:
        raise ValueError('Unknown execution budget level')
    seconds = DEFAULT_LIMITS[level][0] if seconds is None else seconds
    max_calls = DEFAULT_LIMITS[level][1] if max_calls is None else max_calls
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        raise ValueError('seconds must be finite and nonnegative')
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError('seconds must be finite and nonnegative')
    if type(max_calls) is not int or max_calls < 0:
        raise ValueError('max_calls must be a nonnegative integer')
    return float(seconds), max_calls


def _usage(value):
    raw = value[1] if isinstance(value, tuple) and len(value) == 2 else {}
    if isinstance(value, dict):
        raw = value.get('usage', {})
    if not isinstance(raw, dict):
        return {}
    return {key: raw[key] for key in ('input_tokens', 'output_tokens', 'cost', 'cost_usd')
            if type(raw.get(key)) in (int, float) and math.isfinite(raw[key]) and raw[key] >= 0}


class ExecutionBudget:
    def __init__(self, level='L0', *, seconds=None, max_calls=None,
                 clock=time.monotonic, started_at=None, label='trip', _parent=None):
        seconds, self.max_calls = _limits(level, seconds, max_calls)
        self.level, self.label, self.parent = level, str(label), _parent
        self._clock = _parent._clock if _parent is not None else clock
        self.started_at = self._clock() if started_at is None else started_at
        if not math.isfinite(self.started_at) or self.started_at > self._clock():
            raise ValueError('started_at must be a past monotonic timestamp')
        self.deadline = self.started_at + seconds
        self._root = _parent._root if _parent is not None else self
        self._lock = _parent._lock if _parent is not None else threading.RLock()
        self._path = (_parent._path if _parent is not None else ()) + (self.label,)
        if _parent is not None:
            self.deadline = min(self.deadline, _parent.deadline)
        self._calls_used, self._closed = 0, ''
        self._children, self._records = {}, []

    def _ancestors(self):
        current = self
        while current is not None:
            yield current
            current = current.parent

    def _reason(self, *, check_calls=True):
        now = self._clock()
        scopes = list(self._ancestors())
        if any(scope._closed for scope in scopes):
            return 'closed'
        if any(now >= scope.deadline for scope in scopes):
            return 'deadline'
        if check_calls and any(scope._calls_used >= scope.max_calls for scope in scopes):
            return 'call_limit'
        return ''

    @property
    def remaining_seconds(self):
        with self._lock:
            if any(scope._closed for scope in self._ancestors()):
                return 0.0
            return max(0.0, self.deadline - self._clock())

    def child(self, level, key, *, seconds=None, max_calls=None):
        expected = {'L0': 'L1', 'L1': 'L2'}.get(self.level)
        if level != expected or not str(key):
            raise ValueError('Children must follow L0 -> L1 -> L2 with a stable key')
        with self._lock:
            child_key = (level, str(key))
            if child_key not in self._children:
                self._children[child_key] = ExecutionBudget(
                    level, seconds=seconds, max_calls=max_calls, label=str(key), _parent=self)
            return self._children[child_key]

    def attempt(self, key):
        """Fence one dispatch while retaining its request's deadline/call cap.

        Closing an attempt invalidates all of its descendant store/tool work,
        but leaves the request's unspent allowance available to an explicit
        retry. A fresh allowance must be authorized by the durable coordinator.
        """
        if self.parent is not None or not str(key):
            raise ValueError('Attempts require a root request budget and stable key')
        with self._lock:
            child_key = ('attempt', str(key))
            if child_key not in self._children:
                self._children[child_key] = ExecutionBudget(self.level,
                    seconds=self.deadline - self.started_at, max_calls=self.max_calls,
                    started_at=self.started_at, label='attempt-' + str(key), _parent=self)
            return self._children[child_key]

    def begin_call(self, kind, operation_id=None):
        """Reserve one actual attempt; exhaustion is a normal ``None`` result."""
        with self._lock:
            if self._reason():
                return None
            for scope in self._ancestors():
                scope._calls_used += 1
            record = {'call_id': f'call-{len(self._root._records) + 1}',
                      'level': self.level, 'scope': list(self._path), 'kind': str(kind),
                      'operation_id': operation_id, 'started_at': self._clock(),
                      'deadline': self.deadline, 'status': 'running'}
            self._root._records.append(record)
            return CallLease(self, record)

    def call(self, callback, *, kind='model', operation_id=None):
        """Wait within the shared deadline; callback receives a CallLease.

        No automatic retries. Each callback must be one physical attempt and
        must return its value without changing publishable application state.
        ``lease.budget`` can charge additional physical subcalls if necessary.
        """
        lease = self.begin_call(kind, operation_id)
        if lease is None:
            with self._lock:
                reason = self._reason()
            return BudgetCallResult('timeout' if reason == 'deadline' else 'exhausted', reason=reason)

        return _bounded_wait(lambda: callback(lease), lease)

    def can_publish(self):
        with self._lock:
            return not self._reason(check_calls=False)

    def publish(self, callback):
        """Fence a fast local atomic commit and close this scope on success.

        Callback owns full schema/critic/version validation and atomicity; do
        not perform network I/O here. Call limits never invalidate completed,
        validated work. Deadline-expired work may only return the retained
        previous plan or an already validated snapshot outside this method.
        """
        with self._lock:
            if self._reason(check_calls=False):
                return False
            callback()
            self._closed = 'published'
            return True

    @contextmanager
    def publication_guard(self):
        """Serialize a short local commit with closure of this scope/ancestors.

        Keep this OUTSIDE the database transaction context, so its lock lasts
        through COMMIT/ROLLBACK. No model/network calls belong inside it.
        """
        with self._lock:
            if self._reason(check_calls=False):
                raise TimeoutError('Execution scope is no longer publishable')
            yield

    def close(self, reason='returned'):
        with self._lock:
            if not self._closed:
                self._closed = str(reason) or 'returned'

    def snapshot(self):
        with self._lock:
            calls = [record for record in self._root._records
                     if tuple(record['scope'][:len(self._path)]) == self._path]
            return {'level': self.level, 'label': self.label, 'started_at': self.started_at,
                    'deadline': self.deadline, 'remaining_seconds': self.remaining_seconds,
                    'calls_used': self._calls_used, 'max_calls': self.max_calls,
                    'calls_remaining': max(0, min(scope.max_calls - scope._calls_used
                                                for scope in self._ancestors())),
                    'reason': self._reason(), 'closed': self._closed,
                    'calls': copy.deepcopy(calls)}


class CallLease:
    def __init__(self, budget, record):
        self.budget, self._record = budget, record
        self.deadline, self.call_id = budget.deadline, record['call_id']
        self._done = threading.Event()
        self._value = None

    @property
    def remaining_seconds(self):
        return self.budget.remaining_seconds

    def complete(self, value):
        with self.budget._lock:
            reason = self.budget._reason(check_calls=False)
            if self._record['status'] != 'running' or reason:
                self._record['late_finished_at'] = self.budget._clock()
                self._record['late_usage'] = _usage(value)
                if self._record['status'] == 'running':
                    self.abandon('timeout' if reason == 'deadline' else 'exhausted', reason)
                return False
            self._value = copy.deepcopy(value)
            self._record.update(status='ok', finished_at=self.budget._clock(), usage=_usage(value))
            self._done.set()
            return True

    def abandon(self, status, reason):
        with self.budget._lock:
            if self._record['status'] == 'running':
                self._record.update(status=status, reason=reason, finished_at=self.budget._clock())
                self._done.set()

    def fail(self, error_type, usage=None):
        with self.budget._lock:
            reason = self.budget._reason(check_calls=False)
            if reason:
                self.abandon('timeout' if reason == 'deadline' else 'exhausted', reason)
                self._record['late_error_type'] = error_type
                self._record['late_usage'] = _usage({'usage': usage})
            else:
                self.abandon('error', error_type)
                self._record['usage'] = _usage({'usage': usage})

    def result(self):
        with self.budget._lock:
            return BudgetCallResult(self._record['status'], copy.deepcopy(self._value),
                                    self._record.get('reason', ''), self.call_id)


def _bounded_wait(callback, lease):
    def run():
        if not lease.budget.can_publish():
            reason = lease.budget._reason(check_calls=False)
            lease.abandon('timeout' if reason == 'deadline' else 'exhausted', reason)
            return
        try:
            lease.complete(callback())
        except Exception as error:
            lease.fail(type(error).__name__, getattr(error, 'usage', None))
    threading.Thread(target=run, daemon=True, name='travel-budget-call').start()
    if not lease._done.wait(timeout=lease.remaining_seconds):
        lease.abandon('timeout', 'deadline')
    return lease.result()


def run_with_deadline(callback, budget):
    """Uncharged whole-workflow wait; always seal scope before returning.

    Construct budget at acceptance, before queueing. Callback takes no args;
    each physical attempt inside it must be charged separately. Its persistent
    writes must use the coordinator's budget-aware atomic store fence. A Python
    thread cannot undo arbitrary callback side effects or kill blocking I/O.
    Do not call ``budget.publish`` inside a callback whose return is needed:
    that seals the scope early. Use the store fence; this runner seals on return.
    """
    with budget._lock:
        reason = budget._reason(check_calls=False)
        if reason:
            return BudgetCallResult('timeout' if reason == 'deadline' else 'exhausted', reason=reason)
        record = {'call_id': None, 'status': 'running', 'started_at': budget._clock(),
                  'deadline': budget.deadline}
        lease = CallLease(budget, record)
    result = _bounded_wait(callback, lease)
    if result.status == 'ok' and isinstance(result.value, BudgetCallResult):
        result = result.value
    budget.close('workflow_' + result.status)
    return result
