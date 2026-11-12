"""Independent evaluation accounting. Unknown truth/cost never becomes a zero.

This module measures supplied observations; it does not manufacture labels,
approve a rollout, or let a model define the high-risk coverage denominator.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from pathlib import Path


COST_FIELDS = ('input_tokens', 'output_tokens', 'tool_calls', 'tool_cost',
               'human_seconds', 'system_wait_seconds', 'total_wait_seconds')
RISK_LEVELS = ('low', 'medium', 'high', 'critical')


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def proportion(correct, total, unknown=0):
    """Wilson 95% interval, excluding but explicitly reporting unjudged samples."""
    if not total:
        return {'correct': correct, 'evaluated': 0, 'unknown': unknown,
                'rate': None, 'interval95': None}
    z, p = 1.959963984540054, correct / total
    den = 1 + z * z / total
    center = (p + z * z / (2 * total)) / den
    radius = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / den
    return {'correct': correct, 'evaluated': total, 'unknown': unknown,
            'rate': p, 'interval95': [max(0, center - radius), min(1, center + radius)]}


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _labels(rows):
    known = [row for row in rows if type(row.get('correct')) is bool]
    return proportion(sum(row['correct'] for row in known), len(known), len(rows) - len(known))


def _claim_judgments(value):
    if not isinstance(value, dict):
        raise ValueError('claim_judgments must be an object')
    for claim_id, judgment in value.items():
        if not isinstance(claim_id, str) or not claim_id or not isinstance(judgment, dict):
            raise ValueError('each claim judgment must be an identified object')
        if (set(judgment) - {'correct', 'decision_helpful', 'reason'}
                or not {'correct', 'decision_helpful'}.intersection(judgment)):
            raise ValueError('claim judgment needs an explicit verdict or null for unknown')
        for key in ('correct', 'decision_helpful'):
            if judgment.get(key) is not None and type(judgment[key]) is not bool:
                raise ValueError('claim verdict must be boolean or null')
        if 'reason' in judgment and not isinstance(judgment['reason'], str):
            raise ValueError('claim judgment reason must be text')
    return value


def _costs(tasks, successes):
    result = {}
    for field in COST_FIELDS:
        observed = [t.get('cost', {}).get(field) for t in tasks]
        known = [value for value in observed if _number(value)]
        missing = len(tasks) - len(known)
        total = sum(known)
        result[field] = {'known_total': total, 'missing_tasks': missing,
            'total': total if not missing else None,
            'per_incoming_task': total / len(tasks) if tasks and not missing else None,
            'per_successful_task': total / successes if successes and not missing else None}
    return result


def summarize(tasks, independent_cases):
    """Join ALL enrolled tasks to independent labels, retaining unmatched requests.

    Case required_conditions are independently annotated objects with id/risk.
    Task condition_results are completed assessments keyed by those ids. A
    model-discovered extra condition cannot enlarge or shrink the denominator.
    """
    if len({t['id'] for t in tasks}) != len(tasks):
        raise ValueError('duplicate incoming task id')
    cases = {c['task_id']: c for c in independent_cases}
    if len(cases) != len(independent_cases):
        raise ValueError('duplicate independent task labels')
    verified, conditional, risk_rows = [], [], []
    required, covered, missing_cases, missing_denominators = 0, 0, 0, 0
    successes, outcomes = 0, Counter()
    changes, rechecked, losses = [], 0, []
    for task in tasks:
        case = cases.get(task['id'])
        # A model's completion claim is not independent task success.
        outcome = (case or {}).get('outcome') or 'unjudged'
        outcomes[outcome] += 1
        successes += outcome == 'restaurant_l2_complete'
        if case is None:
            missing_cases += 1
        judgments = _claim_judgments((case or {}).get('claim_judgments', {}))
        for claim in task.get('claims', []):
            band = claim.get('band')
            if band == 'verified':
                verified.append({'correct': judgments.get(claim['id'], {}).get('correct')})
            elif band == 'conditional':
                conditional.append({'correct': judgments.get(claim['id'], {}).get('decision_helpful')})
        conditions = (case or {}).get('required_conditions')
        incomplete = not isinstance(conditions, list)
        for condition in conditions if isinstance(conditions, list) else []:
            if (not isinstance(condition, dict) or not isinstance(condition.get('id'), str)
                    or not condition['id'] or condition.get('risk') not in RISK_LEVELS):
                incomplete = True
                continue
            if condition.get('risk') not in ('high', 'critical'):
                continue
            required += 1
            result = task.get('condition_results', {}).get(condition['id'], {})
            covered += result.get('assessment_completed') is True
            risk_rows.append({'risk': condition.get('category', 'unspecified'),
                              'correct': result.get('risk_detected')})
        missing_denominators += incomplete
        for event in task.get('evidence_changes', []):
            if event.get('requires_recheck') is True:
                changes.append(event)
                rechecked += event.get('dependent_decision_rechecked') is True
        losses.extend((case or {}).get('critical_errors', []))
    risk_types = sorted({row['risk'] for row in risk_rows})
    return {
        'incoming_tasks': len(tasks), 'independently_labeled_tasks': len(tasks) - missing_cases,
        'verified_accuracy': _labels(verified), 'conditional_helpfulness': _labels(conditional),
        'high_risk_coverage': {'completed': covered, 'required': required,
            'rate': covered / required if required and not missing_denominators else None,
            'observed_rate': covered / required if required else None,
            'tasks_missing_denominator': missing_denominators},
        'task_completion': {'completed': successes, 'incoming': len(tasks),
            'rate': successes / len(tasks) if tasks else None, 'outcomes': dict(outcomes)},
        'abandonment': {'count': outcomes['abandoned'], 'rate': outcomes['abandoned'] / len(tasks) if tasks else None},
        'critical_errors': {'count': len(losses), 'events': losses,
            'known_loss': sum(e['loss'] for e in losses if _number(e.get('loss'))),
            'unknown_loss_events': sum(not _number(e.get('loss')) for e in losses)},
        'evidence_recheck': proportion(rechecked, len(changes)),
        'cost': _costs(tasks, successes),
        'new_layer_miss_rate': {risk: _labels([
            {'correct': not row['correct'] if type(row['correct']) is bool else None}
            for row in risk_rows if row['risk'] == risk]) for risk in risk_types},
        'limits': 'Independent labels required; missing truth remains unjudged. This report does not authorize switching.'}


def validate_real_case(case, snapshot_root):
    """Validate traceability, not factual correctness or annotator independence."""
    required = ('id', 'task_id', 'family', 'observed_at', 'snapshot_path', 'snapshot_sha256',
                'request_conditions', 'required_conditions')
    if any(not case.get(key) for key in required) or case.get('kind') != 'real':
        raise ValueError('real case needs request conditions and an observed source snapshot')
    root = Path(snapshot_root).resolve()
    path = (root / case['snapshot_path']).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError('snapshot must exist inside the evidence directory')
    if hashlib.sha256(path.read_bytes()).hexdigest() != case['snapshot_sha256']:
        raise ValueError('snapshot hash changed')
    conditions = case['required_conditions']
    if not isinstance(conditions, list) or any(not isinstance(c, dict)
            or not isinstance(c.get('id'), str) or not c['id'] or c.get('risk') not in RISK_LEVELS for c in conditions):
        raise ValueError('independent condition list malformed')
    if len({c['id'] for c in conditions}) != len(conditions):
        raise ValueError('duplicate required condition')
    return True


def freeze_cases(cases, snapshot_root, output, *, annotator, implementation_actors,
                 seed='rfc001-2026-10-11', minimum=200, maximum=300):
    """Create an immutable split manifest only after actual independent labeling.

    Families (source/entity/time cohorts) stay together. A manifest describes
    the fixed split before evaluation and is never silently overwritten.
    """
    if not annotator or annotator in implementation_actors:
        raise ValueError('independent human annotator required')
    if not minimum <= len(cases) <= maximum:
        raise ValueError(f'need {minimum}-{maximum} real cases; received {len(cases)}')
    if len({case['id'] for case in cases}) != len(cases):
        raise ValueError('duplicate case id')
    if len({case['task_id'] for case in cases}) != len(cases):
        raise ValueError('one incoming task cannot become multiple split cases')
    for case in cases:
        validate_real_case(case, snapshot_root)
        label = case.get('annotation', {})
        if label.get('actor') != annotator or label.get('kind') != 'human' or not label.get('at'):
            raise ValueError('independent human annotation missing')
        if case.get('outcome') not in {'restaurant_l2_complete', 'no_data', 'no_match', 'abandoned', 'safety_stop'}:
            raise ValueError('independent outcome truth missing')
        if not isinstance(case.get('claim_judgments'), dict):
            raise ValueError('independent claim truth missing')
        _claim_judgments(case['claim_judgments'])
        if any(not isinstance(case.get(key), str) or not case[key].strip()
               for key in ('expected_action', 'error_consequences')):
            raise ValueError('expected action and error consequences required')
    families = sorted({case['family'] for case in cases},
                      key=lambda value: fingerprint([seed, value]))
    if len(families) < 2:
        raise ValueError('need independent family groups for dev and held-out sets')
    target, dev_families, count = round(len(cases) * .6), set(), 0
    for family in families[:-1]:
        if count >= target:
            break
        dev_families.add(family)
        count += sum(case['family'] == family for case in cases)
    manifest = {'protocol_version': 'rfc001-v1', 'seed': seed,
        'annotator': annotator, 'case_count': len(cases),
        'cases': [{'id': c['id'], 'task_id': c['task_id'], 'family': c['family'], 'sha256': fingerprint(c),
            'split': 'dev' if c['family'] in dev_families else 'held_out'} for c in cases],
        'policy': 'No prompt tuning on held-out labels; protocol changes require a new version.'}
    manifest['sha256'] = fingerprint(manifest)
    with Path(output).open('x', encoding='utf-8') as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    return manifest
