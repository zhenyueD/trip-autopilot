"""Current dependency and critic bindings; no model-authored authority fields."""
import math
import json
from datetime import datetime, timezone

from .revision_plan import plan_sha256, validate_revision_plan
from .phase1_sources import assert_review_sources
from .store import StateError
from .autopilot_workspace_records import source_freshness

DEPENDENCIES = ('locked_item_ids', 'profile_revision', 'traveler_profile', 'material_sources',
                'source_evidence', 'detail_public_cache', 'material_detail_ledger',
                'requirements', 'constraints', 'hard_constraints', 'commitments',
                'bookings', 'confirmed_bookings', 'warnings', 'todos')


def dependency_snapshot(trip, goal, db):
    inputs = []
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='autopilot_workspace'").fetchone():
        inputs = [json.loads(row[0]) for row in db.execute(
            "SELECT data FROM autopilot_workspace WHERE trip_id=? AND kind='input' ORDER BY id", (trip['id'],))]
    return {'trip': {key: plan_sha256(trip.get(key)) for key in DEPENDENCIES},
            'goal_id': goal['id'], 'goal_version': goal['version'],
            'user_inputs_sha256': plan_sha256(inputs),
            'constraints_sha256': plan_sha256(goal['constraints']),
            'authorization_sha256': plan_sha256(goal['authorization']),
            'token_sha256': trip['token_hash']}


def validate_candidate(candidate, trip):
    previous = trip.get('plan') or {}
    return validate_revision_plan(candidate, previous, previous.get('supplies', []),
        trip.get('locked_item_ids', []), sources=(trip.get('source_evidence') or []) +
        (trip.get('material_sources') or []))


def assert_review(candidate, previous, review):
    if (not isinstance(review, dict) or review.get('verdict') != 'pass'
            or review.get('candidate_sha256') != plan_sha256(candidate)
            or review.get('previous_sha256') != plan_sha256(previous)):
        raise StateError('REVIEW_MISMATCH', '审查结果与候选或基线不一致', 409)


def expiry(value, now):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= now:
        raise StateError('STALE_PROPOSAL', '提案适用时间已过或无效', 409)
    return value


def _source_times(trip, now):
    for source in (trip.get('source_evidence') or []) + (trip.get('material_sources') or []):
        if source_freshness(source, now)['historical_only']:
            raise StateError('STALE_EVIDENCE', '来源有效期已过，须重新核验', 409)


def _assert_snapshot(expected, current, explicit_user_undo):
    if explicit_user_undo:
        if current['constraints_sha256'] != expected['constraints_sha256']:
            raise StateError('UNDO_REVIEW_REQUIRED', '当前目标约束已变化，撤销前需要重新审查', 409)
        # Current run/lease/tool authority was checked separately. Old goal and
        # credential identity are not the lifetime of a user's applied revision.
        keys = ('trip', 'user_inputs_sha256')
        matches = all(current[key] == expected[key] for key in keys)
    else:
        matches = current == expected
    if not matches:
        raise StateError('STALE_DEPENDENCIES', '目标、授权、约束、锁或来源已变化，须重新审查', 409)


def assert_dependencies(proposal, trip, goal, records, db, now, *, undo=False, explicit_user_undo=False):
    user_undo = undo and explicit_user_undo
    _assert_snapshot(proposal['dependencies'], dependency_snapshot(trip, goal, db), user_undo)
    if not user_undo and now >= proposal['valid_until']:
        raise StateError('STALE_PROPOSAL', '提案适用时间已过，须重新审查', 409)
    _source_times(trip, now)
    review = proposal['review']
    assert_review_sources(trip, review.get('phase1_review'))
    records.assert_publishable(trip['id'], proposal['expected_revision'], proposal['assessment_ids'], db=db)
    assert_review(proposal['plan'], proposal['previous'], review)
    if plan_sha256(review) != proposal['review_sha256']:
        raise StateError('REVIEW_MISMATCH', '审查记录已变化', 409)
    # Undo may follow unrelated plan edits; assessments retain their original revision.
    if not undo:
        assert_review(proposal['plan'], trip['plan'], review)


def iso_time(clock):
    return datetime.fromtimestamp(clock(), timezone.utc).isoformat()
