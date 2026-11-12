"""Convert verified writer edit roots to conservative stable ownership paths."""
from .autopilot_revision_fields import _identity, _overlap


def _path(pointer, previous, candidate):
    if not isinstance(pointer, str) or pointer == '/plan':
        return []
    if not pointer.startswith('/plan/'):
        return []
    tokens = pointer[len('/plan/'):].split('/')
    path, before, after = [], previous, candidate
    for raw in tokens:
        # Only RFC 6901 escapes; malformed roots cannot establish precision.
        if '~' in raw.replace('~0', '').replace('~1', ''):
            return path
        token = raw.replace('~1', '/').replace('~0', '~')
        if isinstance(before, dict) and isinstance(after, dict):
            path.append(['key', token])
            before, after = before.get(token), after.get(token)
        elif isinstance(before, list) and isinstance(after, list):
            left, right = _identity(before, path), _identity(after, path)
            if (not left or left != right or not token.isascii() or not token.isdigit()
                    or str(int(token)) != token or int(token) >= len(before)):
                return path  # Opaque/reordered/replaced list: own its whole parent.
            index = int(token)
            path.append([left[0], left[1][index]])
            before, after = before[index], after[index]
        else:
            return path
    return path


def plan_touches(previous, candidate, usage):
    """Touches include explicit same-value roots, not merely changed fields."""
    if not previous:
        return [[]]
    roots = usage.get('effective_edit_roots')
    if roots is None:
        # Without computed border roots, a structural removal cannot prove
        # narrow ownership. No inference from a mechanical diff is permitted.
        if usage.get('removal_declaration'):
            return [[]]
        roots = (usage.get('scope_selection') or {}).get('edit_paths')
    if not isinstance(roots, list) or not roots:
        return [[]]
    paths = []
    for root in roots:
        path = _path(root, previous, candidate)
        if any(_overlap(existing, path) and len(existing) <= len(path) for existing in paths):
            continue
        paths = [existing for existing in paths if not _overlap(existing, path)]
        paths.append(path)
    return paths or [[]]
