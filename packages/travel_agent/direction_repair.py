"""Small protocol-only helpers for bounded full-direction repairs."""
import copy
import re

from . import model
from .core_commitments import ACTIVITY_KINDS
from .validation import PlanError, records


def invalid_activity_kinds(raw):
    """Audit enum labels, never serialize a model payload into usage."""
    updates = raw.get('commitment_updates') if isinstance(raw, dict) else None
    if not isinstance(updates, list):
        return []
    result = []
    for index, row in enumerate(updates[:8]):
        if not isinstance(row, dict) or row.get('action') != 'upsert':
            continue
        value = row.get('activity_kind')
        if isinstance(value, str) and value in ACTIVITY_KINDS:
            continue
        safe = value
        if isinstance(value, str):
            safe = model._redact_error_value(value)
            if (not re.fullmatch(r'[\w ()（），、-]{1,64}', safe)
                    or re.search(r'[A-Za-z0-9_-]{32,}', safe)
                    or re.search(r'(?i)(?:^|[ (（])(?:api[-_]?key|token|secret|password)[-_]', safe)):
                safe = '<REDACTED_NON_ENUM_VALUE>'
        elif value is not None and not isinstance(value, (bool, int, float)):
            safe = '<REDACTED_NON_ENUM_VALUE>'
        result.append({'field': f'commitment_updates[{index}].activity_kind',
                       'value': safe, 'value_type': type(value).__name__})
    return result


def retain_commitment_updates(updates, required):
    """Protocol repair must not succeed merely by deleting a user requirement."""
    rows = records(updates, '每日要求更新', 8)
    identities, pending = {}, {}
    for row in rows:
        id_ = row.get('id')
        source = row.get('source') or {}
        action = row.get('action')
        action = action if isinstance(action, str) and action in {'upsert', 'cancel'} else None
        keys = []
        if isinstance(id_, str) and 0 < len(id_) <= 80:
            keys.append(('id', id_))
        if (isinstance(source, dict) and isinstance(source.get('material_id'), str)
                and isinstance(source.get('quote'), str)):
            keys.append(('source', source['material_id'], source['quote']))
        for key in keys:
            identities.setdefault(key, set()).add(action)
        if keys:
            pending[keys[0]] = action
    if any(key not in identities or action is not None and action not in identities[key]
           for key, action in required.items()):
        raise PlanError('方向修复遗漏了待修正的用户要求；必须保留要求并修正协议')
    required.update(pending)


def repair_payload(payload, raw, error):
    return {**payload, 'repair_target': 'direction_only',
            'rejected_direction': copy.deepcopy(raw),
            'validation_error': (
                str(error) + '。请输出修正后的完整方向JSON，保留已有有效内容与用户要求，'
                '不得通过删除commitment_updates规避校验；activity_kind只允许'
                'rest / meal / activity / personal，不新增类别。')}
