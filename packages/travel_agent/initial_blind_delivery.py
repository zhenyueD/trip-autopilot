"""Narrow initial-only delivery with visible, unreviewed supplemental warnings.

An unavailable sixth critic is not a semantic PASS. This path cannot publish a
revision, an incomplete initial checkpoint, or a candidate with rejected truth
checks. The receipt is checked again inside the existing atomic transaction.
"""
import copy

from .blind_spots import validate_blind_spot_review
from .validation import PlanError, validate_plan
from .writer_critic import mechanical_diff, plan_hash


def _initial_checks(trip, plan, metrics):
    checkpoint = trip.get('revision_checkpoint') or {}
    raw = checkpoint.get('candidate')
    if (trip.get('plan') is not None or checkpoint.get('kind') != 'initial_generation'
            or checkpoint.get('status') != 'validated' or not isinstance(raw, dict)
            or checkpoint.get('candidate_sha256') != plan_hash(raw)):
        raise PlanError('补充复核未完成，缺少完整初始方案检查点')
    days, shards = raw.get('days') or [], checkpoint.get('shards') or []
    if not days or len(shards) != len(days) or any(s.get('status') != 'accepted' for s in shards):
        raise PlanError('补充复核未完成，初始每日方案尚未全部通过')
    if any(not isinstance(s.get('result'), dict)
           or s.get('result_sha256') != plan_hash(s['result']) for s in shards):
        raise PlanError('补充复核未完成，初始每日检查点不一致')
    if ((metrics.get('delivery_truth') or {}).get('status') != 'not_applicable'
            or metrics.get('commitment_unmet_count') != 0
            or (plan.get('readiness') or {}).get('ready_to_depart') is not False):
        raise PlanError('补充复核未完成，不能覆盖核心交付或准备状态检查')
    validate_plan(plan, plan.get('supplies') or [], expected_days=len(days))
    if any(c.get('status') == 'unmet' for c in plan.get('commitment_checks') or []):
        raise PlanError('初始方案仍有未满足的每日要求')
    return checkpoint


def unchecked_initial_delivery(trip, plan, metrics, result):
    sixth = result.get('sixth_check') or {}
    report = result.get('blind_spots')
    if (result.get('blind_spot_repair') or sixth.get('failure_kind') != 'operational' or sixth.get('candidate_checks')
            or not validate_blind_spot_review(report)['structurally_complete']):
        return None
    checkpoint = _initial_checks(trip, plan, metrics)
    result = copy.deepcopy(result)
    report = result['blind_spots']
    report.update(status='attention', semantic_review_required=True,
                  semantic_review_status='unavailable')
    for candidate in report['candidates']:
        candidate['prior_disposition'] = candidate['disposition']
        candidate['prior_reason'] = candidate['reason']
        reason = '盲区复核未完成，本项尚未确认或排除；' + candidate['reason']
        if len(reason) > 1600:
            reason = reason[:1550] + '（完整原处理说明保留于核查记录）'
        candidate.update(disposition='needs_attention', warning_visible=True,
                         reason=reason, unqueried_reason=reason)
    result.update(delivery_status='initial_with_unchecked_blind_spots',
                  initial_checkpoint_sha256=plan_hash(checkpoint),
                  previous_sha256=plan_hash(None))
    metrics['blind_spots'] = copy.deepcopy(report)
    return result


def assert_initial_delivery(trip, plan, metrics, result):
    """Recheck actual baseline, candidate/diff and checkpoint under COMMIT lock."""
    if result.get('delivery_status') != 'initial_with_unchecked_blind_spots':
        return
    checkpoint = _initial_checks(trip, plan, metrics)
    if (result.get('candidate_sha256') != plan_hash(plan)
            or result.get('diff_sha256') != plan_hash(mechanical_diff(None, plan))
            or result.get('previous_sha256') != plan_hash(None)
            or result.get('initial_checkpoint_sha256') != plan_hash(checkpoint)):
        raise PlanError('补充复核降级记录与当前初始方案不一致')
    report = result.get('blind_spots') or {}
    if (result.get('blind_spot_repair') or result.get('verdict') != 'fail'
            or (result.get('sixth_check') or {}).get('failure_kind') != 'operational'
            or not validate_blind_spot_review(report)['structurally_complete']
            or any(c.get('disposition') != 'needs_attention' for c in report['candidates'])
            or metrics.get('blind_spots') != report):
        raise PlanError('未完成的盲区复核必须保留三个可见待查预警')
