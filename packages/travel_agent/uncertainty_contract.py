"""Structural evidence checks; semantic support must come from a verifier.

The checks here deliberately do not infer entailment from quotes or keywords.
An entry's source category is also never itself an assessment.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone


class UncertaintyError(ValueError):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def instant(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError('timezone required')
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        raise UncertaintyError('BAD_TIME', '时间必须包含时区') from None


def require_text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise UncertaintyError('BAD_INPUT', f'{name}不能为空')
    return value


def require_version(value):
    if type(value) is not int or value < 1:
        raise UncertaintyError('BAD_VERSION', '版本必须为正整数')
    return value


def check_version(actual, expected):
    require_version(expected)
    if actual != expected:
        raise UncertaintyError('VERSION_CONFLICT', '记录已变化，请读取最新版本', 409)


def evidence_current(evidence, at):
    return (evidence['state'] == 'active'
            and evidence.get('raw_status') in {'read', 'succeeded', 'ok'}
            and evidence.get('http_status') not in {202, 404, 410}
            and (evidence.get('http_status') is None or 200 <= evidence['http_status'] < 300)
            and instant(evidence['observed_at']) <= instant(at)
            and bool(evidence.get('valid_until'))
            and instant(evidence['valid_until']) > instant(at)
            and (not evidence.get('valid_from') or instant(evidence['valid_from']) <= instant(at)))


def condition_match(claim, evidence):
    conditions = claim['conditions']
    # Exact supplied parameters, including types: 2 guests is not True guests.
    supplied = evidence['request_conditions']
    matches = all(key in supplied and canonical(value) == canonical(supplied[key])
                  for key, value in conditions.items())
    required = {'date', 'time', 'timezone', 'party_size'} if claim['predicate'] == 'availability' else set()
    return (claim['entity_id'] == evidence['entity_id'] and matches
            and required.issubset(conditions)
            and all(conditions[key] not in (None, '') for key in required))


def assess_conditions(claim, evidence, judgment, at, *, automatic_verified=True):
    """Return the public tier from all six checks, never a Writer-supplied tier."""
    checks = {
        'recorded_tool': bool(evidence) and all(e.get('tool_call_id') for e in evidence),
        'source_suitable': judgment.get('source_suitable') is True,
        'direct_support': judgment.get('support') == 'direct' and claim.get('value') is not None,
        'conditions_match': bool(evidence) and all(condition_match(claim, e) for e in evidence),
        'current': bool(evidence) and all(evidence_current(e, at) for e in evidence),
        'no_conflict': judgment.get('no_conflict') is True and judgment.get('support') != 'contradicts',
    }
    reasons = [name for name, passed in checks.items() if not passed]
    if all(checks.values()):
        tier = 'verified' if automatic_verified else 'conditional'
        if not automatic_verified:
            reasons.append('verifier_audit_failed')
    elif (checks['recorded_tool'] and checks['conditions_match'] and checks['current']
          and checks['no_conflict'] and judgment.get('support') in {'direct', 'related'}
          and judgment.get('assumptions')):
        tier = 'conditional'
    else:
        tier = 'unknown'
    return {'tier': tier, 'checks': checks, 'reasons': reasons,
            'assumptions': judgment.get('assumptions') or [],
            'independent_source_count': len({e['source_family'] for e in evidence})}


def validate_judgment(value):
    if not isinstance(value, dict) or value.get('support') not in {'direct', 'related', 'contradicts', 'insufficient'}:
        raise UncertaintyError('BAD_ASSESSMENT', '核验须区分直接支持、相关、矛盾与不足')
    for key in ('source_suitable', 'no_conflict'):
        if value.get(key) is not None and type(value[key]) is not bool:
            raise UncertaintyError('BAD_ASSESSMENT', f'{key}必须是布尔或未知')
    require_text(value.get('reason'), 'reason')
    assumptions = value.get('assumptions', [])
    if not isinstance(assumptions, list) or any(not isinstance(a, str) or not a.strip() for a in assumptions):
        raise UncertaintyError('BAD_ASSESSMENT', 'assumptions必须为文字列表')
    return {key: value.get(key) for key in ('source_suitable', 'support', 'no_conflict', 'reason')} | {'assumptions': assumptions}
