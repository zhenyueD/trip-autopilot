"""Minimal last-write ownership, including tombstones and explicit same-value touches."""
import copy
import json
from uuid import uuid4

from .store import StateError


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def _identity(rows, path):
    key = 'day' if path and path[-1] == ['key', 'days'] else 'id'
    if not rows or any(not isinstance(row, dict) or key not in row for row in rows):
        return None
    ids = [row[key] for row in rows]
    if any(type(value) not in (str, int) for value in ids) or len(set(ids)) != len(ids):
        return None
    return key, ids


def changes(before, after, path=None):
    path = path or []
    if before == after:
        return []
    if isinstance(before, dict) and isinstance(after, dict):
        result = []
        for key in sorted(set(before) | set(after)):
            child = path + [['key', key]]
            if key not in before or key not in after:
                result.append(dict(path=child, before=_value(before, key), after=_value(after, key)))
            else:
                result.extend(changes(before[key], after[key], child))
        return result
    if isinstance(before, list) and isinstance(after, list):
        left, right = _identity(before, path), _identity(after, path)
        if left and left == right:
            return [change for a, b in zip(before, after)
                    for change in changes(a, b, path + [[left[0], a[left[0]]]])]
    return [dict(path=path, before=dict(exists=True, value=copy.deepcopy(before)),
                 after=dict(exists=True, value=copy.deepcopy(after)))]


def _value(value, key):
    return dict(exists=True, value=copy.deepcopy(value[key])) if key in value else dict(exists=False)


def _overlap(left, right):
    return left[:len(right)] == right or right[:len(left)] == left


def record_write(db, previous, trip, *, explicit=False, touches=None, owner=None):
    """Called in TripStore._save's transaction; never commits on its own."""
    delta = changes(previous.get('plan'), trip.get('plan'))
    if not delta and not explicit:
        return None
    db.execute('CREATE TABLE IF NOT EXISTS autopilot_plan_ownership '
               '(trip_id TEXT PRIMARY KEY, version INTEGER NOT NULL, data TEXT NOT NULL)')
    row = db.execute('SELECT version,data FROM autopilot_plan_ownership WHERE trip_id=?',
                     (trip['id'],)).fetchone()
    version, fields = (row[0] + 1, json.loads(row[1])) if row else (1, {})
    paths = [change['path'] for change in delta]
    # A whole-plan save cannot prove which equal values were explicitly reaffirmed.
    if explicit:
        paths.extend([[]] if touches is None else touches)
    owner = owner or 'legacy:' + uuid4().hex
    for path in paths:
        fields[canonical(path)] = dict(owner=owner, version=version)
    db.execute('INSERT OR REPLACE INTO autopilot_plan_ownership VALUES (?,?,?)',
               (trip['id'], version, canonical(fields)))
    return version


def ownership_version(db, trip_id):
    row = db.execute('SELECT version FROM autopilot_plan_ownership WHERE trip_id=?', (trip_id,)).fetchone()
    return row[0] if row else 0


def inverse(db, trip, revision):
    row = db.execute('SELECT data FROM autopilot_plan_ownership WHERE trip_id=?', (trip['id'],)).fetchone()
    fields = json.loads(row[0]) if row else {}
    for change in revision['changes']:
        for key, stamp in fields.items():
            if stamp['version'] > revision['write_version'] and _overlap(json.loads(key), change['path']):
                raise StateError('UNDO_CONFLICT', '后续写入或实体换代阻止撤销：' + canonical(change['path']), 409)
    candidate = copy.deepcopy(trip['plan'])
    for change in reversed(revision['changes']):
        candidate = _restore(candidate, change)
    return candidate


def _restore(candidate, change):
    path, before, after = change['path'], change['before'], change['after']
    if not path:
        if candidate != after.get('value'):
            raise StateError('UNDO_CONFLICT', '当前方案与原写入不一致', 409)
        return copy.deepcopy(before['value'])
    parent = candidate
    try:
        for kind, key in path[:-1]:
            parent = parent[key] if kind == 'key' else next(row for row in parent if row.get(kind) == key)
        kind, key = path[-1]
        if kind != 'key' or not isinstance(parent, dict) or _value(parent, key) != after:
            raise ValueError('ambiguous alignment')
        if before['exists']:
            parent[key] = copy.deepcopy(before['value'])
        else:
            del parent[key]
    except (KeyError, StopIteration, TypeError, ValueError, AttributeError):
        raise StateError('UNDO_CONFLICT', '无法证明实体对齐：' + canonical(path), 409) from None
    return candidate
