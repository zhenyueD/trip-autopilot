"""Explicit, editable traveler preferences; no inferred medical conditions."""
from copy import deepcopy


def normalize_profile(value):
    if (not isinstance(value, dict) or type(value.get('version', 1)) is not int
            or value.get('version', 1) != 1):
        raise ValueError('旅行偏好需要有效对象')
    result = {'version': 1}
    limits = {'traveler_groups': (8, 100), 'mobility_preferences': (12, 300), 'preferences': (20, 500)}
    for field, (count, length) in limits.items():
        rows = value.get(field, [])
        if not isinstance(rows, list) or len(rows) > count or any(not isinstance(row, str) or len(row) > length for row in rows):
            raise ValueError(f'{field}需要有界文字列表')
        result[field] = list(dict.fromkeys(row.strip() for row in rows if row.strip()))
    for field, length in (('pace_preference', 500), ('user_notes', 2000)):
        text = value.get(field, '')
        if not isinstance(text, str) or len(text) > length:
            raise ValueError(f'{field}需要有效文字')
        result[field] = text.strip()
    return result


def initial_profile(plan):
    understanding = (plan or {}).get('understanding') or []
    # Preserve the recorded wording. Semantic preference refinement is a model
    # task; age alone must not be translated into illness or mobility limits.
    preferences = [row['text'] for row in understanding if isinstance(row, dict)
                   and isinstance(row.get('text'), str) and len(row['text']) <= 500][:20]
    return normalize_profile({'preferences': preferences})


def current_profile(trip):
    value = trip.get('traveler_profile')
    return deepcopy(value) if isinstance(value, dict) else initial_profile(trip.get('plan'))


def profile_commit_fields(trip, candidate):
    previous = current_profile(trip)
    proposed = candidate.get('traveler_profile')
    if proposed is not None:
        proposed = normalize_profile(proposed)
        embedded = (trip.get('plan') or {}).get('traveler_profile')
        baseline = normalize_profile(embedded) if isinstance(embedded, dict) else previous
        # Only explicit changes relative to the candidate's plan baseline are
        # updates. Unchanged embedded preferences may predate an editor save.
        profile = {**previous, **{key: value for key, value in proposed.items()
                                 if value != baseline.get(key)}}
    else:
        profile = previous if trip.get('traveler_profile') is not None else initial_profile(candidate)
    revision = trip.get('profile_revision', 0)
    return {'traveler_profile': profile, 'profile_revision': revision + int(profile != previous)}
