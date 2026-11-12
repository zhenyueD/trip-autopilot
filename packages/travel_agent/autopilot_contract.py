"""Small shared contracts for durable execution; no model-owned authority."""
import hashlib
import json
import math

from .store import StateError

LIMIT_KEYS = frozenset({'model_calls', 'input_tokens', 'output_tokens',
                        'wall_seconds', 'tool_calls'})
MAX_LIMITS = dict(model_calls=1000, input_tokens=10000000, output_tokens=1000000,
                  wall_seconds=86400, tool_calls=1000)
RUN_TERMINAL = frozenset({'completed', 'waiting', 'failed', 'cancelled'})


def fail(code, status=409):
    raise StateError(code, code, status)


def canonical(value):
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False,
                          separators=(',', ':'), allow_nan=False)
    except (TypeError, ValueError):
        fail('INVALID_ARGUMENT', 400)


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 100000:
        fail('INVALID_ARGUMENT', 400)
    return value


def integer(value, minimum=0):
    if type(value) is not int or value < minimum:
        fail('INVALID_ARGUMENT', 400)
    return value


def duration(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 3600:
        fail('INVALID_ARGUMENT', 400)
    return value


def limits(value, ceiling=None):
    ceiling = MAX_LIMITS if ceiling is None else ceiling
    if not isinstance(value, dict) or set(value) != LIMIT_KEYS:
        fail('INVALID_ARGUMENT', 400)
    for key, amount in value.items():
        integer(amount, 1)
        if amount > ceiling[key]:
            fail('BUDGET_EXCEEDED')
    return json.loads(canonical(value))


def authorization(value):
    if not isinstance(value, dict) or set(value) != {'tools', 'limits'}:
        fail('INVALID_AUTHORIZATION', 400)
    names = value['tools']
    if not isinstance(names, list) or not names or any(not isinstance(n, str) or not n.strip() for n in names):
        fail('INVALID_AUTHORIZATION', 400)
    return {'tools': sorted(set(names)), 'limits': limits(value['limits'])}


def public_run(run, observed_at):
    hidden = {'token_hash', 'lease_secret', 'lease_worker', 'lease_until', 'request_hash'}
    result = {key: value for key, value in run.items() if key not in hidden}
    result['wall_seconds'] = max(0, run.get('finished_at', observed_at) - run['started_at'])
    return result
