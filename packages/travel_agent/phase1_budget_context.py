"""Opt-in physical-call accounting and explicit context propagation."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from functools import wraps
import json
import urllib.request


_ACTIVE = ContextVar('travel_phase1_budget', default=None)
_PRECHARGED = ContextVar('travel_phase1_precharged_call', default=False)


def current_budget():
    return _ACTIVE.get()


@contextmanager
def use_budget(budget):
    token = _ACTIVE.set(budget)
    try:
        yield budget
    finally:
        _ACTIVE.reset(token)


def context_target(function):
    context = copy_context()
    return lambda *args, **kwargs: context.run(function, *args, **kwargs)


class ContextThreadPoolExecutor(ThreadPoolExecutor):
    def submit(self, function, /, *args, **kwargs):
        return super().submit(context_target(function), *args, **kwargs)


def clamp_deadline(deadline):
    budget = current_budget()
    return min(deadline, budget.deadline) if budget is not None else deadline


class BudgetExhausted(TimeoutError):
    """Safe transport exception; callers preserve normal timeout degradation."""


class PhysicalAttempt:
    def __init__(self, timeout, lease):
        self.timeout, self.lease, self.usage = timeout, lease, {}


@contextmanager
def physical_attempt(kind, timeout=None):
    budget = current_budget()
    if budget is None:
        yield PhysicalAttempt(timeout, None)
        return
    lease = budget.begin_call(kind)
    if lease is None:
        raise BudgetExhausted('Phase 1 execution budget exhausted')
    seconds = lease.remaining_seconds if timeout is None else min(timeout, lease.remaining_seconds)
    attempt = PhysicalAttempt(seconds, lease)
    try:
        if seconds <= 0:
            error = BudgetExhausted('Phase 1 execution deadline')
            error.usage = {'timeout_stage': 'preflight', 'request_delivery_status': 'not_sent',
                           'usage_status': 'unknown', 'usage_complete': False}
            raise error
        yield attempt
    except Exception as error:
        lease.fail(type(error).__name__, getattr(error, 'usage', attempt.usage))
        raise
    else:
        if not lease.complete({'usage': attempt.usage}):
            raise BudgetExhausted('Late Phase 1 transport result discarded')


def transport_usage(raw):
    """Observe known billing counters without retaining provider content."""
    try:
        body = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(body, dict):
        return {}
    usage = body.get('usage') or body.get('usageMetadata') or {}
    if not isinstance(usage, dict):
        return {}
    result = {}
    for keys, target in ((('prompt_tokens', 'promptTokenCount'), 'input_tokens'),
                         (('completion_tokens', 'candidatesTokenCount'), 'output_tokens')):
        for key in keys:
            if type(usage.get(key)) is int and usage[key] >= 0:
                result[target] = usage[key]
                break
    return result


def bind_budget(function, budget):
    @wraps(function)
    def bound(*args, **kwargs):
        active = current_budget()
        selected = active if active is not None and active._root is budget._root else budget
        with use_budget(selected):
            return function(*args, **kwargs)
    return bound


def bind_model_caller(caller, budget, day_keys=None):
    day_keys = day_keys or {}
    @wraps(caller)
    def bound(messages, **kwargs):
        active = current_budget()
        selected = active if active is not None and active._root is budget._root else budget
        try:
            payload = json.loads(messages[-1]['content'])
            root = payload.get('shard', {}).get('root', '')
            if root.startswith('/plan/days/') and root.split('/')[3] in day_keys:
                selected = budget.child('L1', day_keys[root.split('/')[3]])
        except (TypeError, KeyError, ValueError, IndexError, AttributeError):
            pass
        if not selected.can_publish():
            raise BudgetExhausted('Phase 1 execution deadline')
        options = dict(kwargs)
        options['timeout'] = min(options.get('timeout', 90), selected.remaining_seconds)
        with use_budget(selected):
            return caller(messages, **options)
    return bound


def uncharged_caller(caller):
    """Only for a caller whose physical attempt is already reserved by a hook."""
    @wraps(caller)
    def bound(*args, **kwargs):
        token = _PRECHARGED.set(True)
        try:
            with use_budget(None):
                return caller(*args, **kwargs)
        finally:
            _PRECHARGED.reset(token)
    return bound


def open_model_response(request, timeout, context):
    """Phase 1 disallows opaque provider redirects; each request is one call."""
    if current_budget() is None and not _PRECHARGED.get():
        return urllib.request.urlopen(request, timeout=timeout, context=context)
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=context))
    return opener.open(request, timeout=timeout)
