"""Commit-time revision receipts; failures never enter the historical timeline."""
from copy import deepcopy

from .message_timeline import make_revision_entry


def timeline_commit_fields(trip, candidate, revision, *, kind='committed'):
    entries = deepcopy(trip.get('revision_timeline') or [])
    limited = bool(trip.get('timeline_truncated'))
    if 'revision_timeline' not in trip and trip.get('history'):
        snapshots = [row for row in trip['history'] if isinstance(row.get('plan'), dict)]
        current = {'plan': trip.get('plan'), 'plan_revision': trip.get('plan_revision')}
        for before, after in zip(snapshots, [*snapshots[1:], current]):
            entries.append(make_revision_entry(before['plan'], after['plan'],
                after.get('plan_revision'), before.get('plan_revision')))
        limited = True  # Legacy snapshots do not prove complete revision coverage.
    if trip.get('plan') is not None:
        entries.append(make_revision_entry(trip['plan'], candidate, revision,
            trip.get('plan_revision'), kind=kind))
    return {'plan_revision': revision, 'revision_timeline': entries[-50:],
            'timeline_truncated': limited or len(entries) > 50}
