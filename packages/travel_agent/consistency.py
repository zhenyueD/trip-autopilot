"""Deterministic text-only patch boundary for an untrusted semantic reviewer."""
from __future__ import annotations

import copy

from .validation import PlanError


_REVIEW_ERROR = '方案一致性复核未通过，请重试。'

# Walk this local schema, never reviewer-supplied paths or arbitrary bundle keys.
_EDITABLE = {
    'intake': {
        'summary': None,
        'signals': [{'text': None, 'implication': None}],
        'defaults': [{'choice': None, 'reason': None}],
        'conflicts': [{'summary': None, 'question': None}],
        'questions': [None],
    },
    'plan': {
        'title': None, 'summary': None,
        'signature': {'title': None, 'why': None, 'tradeoff': None, 'sequence': [None]},
        'days': [{'title': None, 'rationale': None, 'items': [
            {'title': None, 'why': None, 'transport': None, 'preparation': None,
             'time': None, 'end_time': None}]}],
        'decisions': [{'choice': None, 'reason': None}],
        'preparation': [{'task': None, 'why': None}],
        'change_summary': [None],
    },
}


def _walk(value, schema, path=()):
    if schema is None and type(value) is str:
        yield path, value
    elif type(schema) is dict and type(value) is dict:
        for key, child in schema.items():
            if key in value:
                yield from _walk(value[key], child, (*path, key))
    elif type(schema) is list and type(value) is list:
        for index, child in enumerate(value):
            yield from _walk(child, schema[0], (*path, index))


def editable_fields(bundle):
    """Return canonical field IDs and diagnostic JSON pointers for validated data."""
    return [{'id': f'f{index:03d}', 'path': '/' + '/'.join(map(str, path)), 'value': value}
            for index, (path, value) in enumerate(_walk(bundle, _EDITABLE))]


def _bounded_text(value, limit):
    return type(value) is str and bool(value.strip()) and len(value) <= limit


def _review_patches(review):
    if type(review) is not dict or set(review) != {'patches', 'unresolved'}:
        raise PlanError(_REVIEW_ERROR)
    if type(review['patches']) is not list or type(review['unresolved']) is not list:
        raise PlanError(_REVIEW_ERROR)
    if len(review['patches']) > 16 or len(review['unresolved']) > 8:
        raise PlanError(_REVIEW_ERROR)
    if any(not _bounded_text(reason, 800) for reason in review['unresolved']):
        raise PlanError(_REVIEW_ERROR)
    if review['unresolved']:
        raise PlanError(_REVIEW_ERROR)
    for patch in review['patches']:
        if type(patch) is not dict or set(patch) != {'field_id', 'value', 'reason'}:
            raise PlanError(_REVIEW_ERROR)
        if any(type(patch[key]) is not str for key in ('field_id', 'value', 'reason')):
            raise PlanError(_REVIEW_ERROR)
        if not _bounded_text(patch['value'], 1200) or not _bounded_text(patch['reason'], 800):
            raise PlanError(_REVIEW_ERROR)
    return review['patches']


def apply_review(bundle, review, *, allow_idempotent=False):
    """Apply a text patch set to a deep copy; semantic validation belongs upstream."""
    patches = _review_patches(review)
    fields = {f'f{index:03d}': (path, value)
              for index, (path, value) in enumerate(_walk(bundle, _EDITABLE))}
    seen, changes = set(), []
    for patch in patches:
        field_id = patch['field_id']
        if field_id not in fields or field_id in seen:
            raise PlanError(_REVIEW_ERROR)
        if patch['value'].strip() == fields[field_id][1].strip():
            if not allow_idempotent:
                raise PlanError(_REVIEW_ERROR)
        else:
            changes.append(patch)
        seen.add(field_id)
    result = copy.deepcopy(bundle)
    for patch in changes:
        path, _ = fields[patch['field_id']]
        target = result
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = patch['value']
    return result, len(changes)


def check_confirmation(review):
    """No second patch round: accept only a clean, strictly typed verdict."""
    if type(review) is not dict:
        raise PlanError(_REVIEW_ERROR)
    # Existing empty-review fixtures/callers express the same zero-issue verdict.
    if review == {'patches': [], 'unresolved': []}:
        return
    if set(review) != {'accepted', 'issues'} or type(review['accepted']) is not bool:
        raise PlanError(_REVIEW_ERROR)
    issues = review['issues']
    if type(issues) is not list or len(issues) > 8:
        raise PlanError(_REVIEW_ERROR)
    if any(not _bounded_text(issue, 160) for issue in issues):
        raise PlanError(_REVIEW_ERROR)
    if not review['accepted'] or issues:
        raise PlanError(_REVIEW_ERROR)
