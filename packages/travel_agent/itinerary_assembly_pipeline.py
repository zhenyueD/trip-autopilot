"""Reusable C5 orchestration over frozen C1/C2 assets and an existing C3 plan.

This module owns artifact and hash accounting, never semantic fact judgments or
product-call budgets. Callers supply budgeted model callbacks and real approval.
Each output directory is one immutable input/run version, not a mutable inbox.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from .comparison_card import ComparisonCard
from .delivery_consistency import apply_alignment, build_alignment_context, alignment_checklist
from .comparison_card_budget import private_json
from .intent_questions import IntentQuestions
from .itinerary_assembly import (SECTIONS, assemble_handbook, render_handbook, render_html,
                                project_confirmed_trip_context, connection_delivery_state)
from .revision_plan import plan_sha256
from .runtime_revision import attach_revision_review, bind_revision_review, revision_review_payload
from .validation import validate_plan


CRITERIA = ('executable', 'trustworthy', 'tradeoffs', 'cheap_to_verify', 'editable', 'intent')
CRITERION_LABELS = ('可执行', '可信', '取舍有依据', '验收便宜', '可修改', '意图识别')


def content_sha256(value):
    """Hash the complete JSON value; source edits are input edits too."""
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':'), allow_nan=False).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _component(value):
    if (not isinstance(value, str) or not value.strip() or value in ('.', '..')
            or any(character in value for character in ('/', '\\', '\x00'))):
        raise ValueError('Artifact identifiers must be nonempty path components')
    return value


def _fixture(value):
    fixture = copy.deepcopy(value)
    if not isinstance(fixture, dict):
        raise ValueError('fixture must be an object')
    for key in ('id', 'title', 'brief', 'bundle', 'plan', 'provenance'):
        if key not in fixture:
            raise ValueError('fixture is missing ' + key)
    _component(fixture['id'])
    bundle, plan = fixture['bundle'], fixture['plan']
    if fixture.get('supply_input'):
        from .itinerary_supply import load_itinerary_supply
        supplied = fixture['supply_input']
        imported = load_itinerary_supply(supplied['path'], destination=supplied['destination'],
            as_of=supplied['as_of'], structured_seeds=supplied.get('structured_seeds'))
        replaced = {row['id'] for row in bundle.get('supply_seeds', [])}
        replaced.update(row['id'] for row in imported['supply_seeds'])
        bundle['sources'] = [row for row in bundle.get('sources', []) if row['id'] not in replaced]
        bundle['sources'] += imported['sources']
        bundle['supply_evidence'] = [row for row in bundle.get('supply_evidence', [])
                                    if row.get('seed_id') not in replaced]
        bundle['supply_evidence'] += imported['supply_evidence']
        for key in ('supply_seeds', 'destination', 'price_as_of'):
            bundle[key] = imported[key]
        bundle.setdefault('provenance', {})['supply_import'] = imported['provenance']
    ComparisonCard.model_validate(bundle['comparison_card'])
    IntentQuestions.model_validate(bundle['intent_card'])
    validate_plan(plan, plan.get('supplies', []))
    baseline = bundle.get('notes_plan')
    if not isinstance(baseline, dict) or plan_sha256(baseline) != plan_sha256(plan):
        raise ValueError('fixture notes_plan must bind the input plan')
    if bundle.get('confirmed_trip_context'):
        plan = project_confirmed_trip_context(plan, bundle['confirmed_trip_context'])
    if bundle.get('price_rules'):
        from .itinerary_price_projection import project_prices
        pricing = project_prices([], bundle['supply_seeds'], rules=bundle['price_rules'],
                                 as_of=bundle['price_as_of'], plan=plan)
        by_id = {row['id']: row for row in pricing['budget']['items']}
        items = {item['id']: item for day in plan['days'] for item in day['items']}
        for rule in bundle['price_rules']:
            targets = rule.get('item_ids', [])
            if rule['disposition'] == 'priced' and len(targets) == 1 and targets[0] in items:
                items[targets[0]]['price'] = copy.deepcopy(by_id[rule['id']]['price'])
    fixture['plan'] = plan
    bundle['notes_plan'] = copy.deepcopy(plan)
    sources = bundle.get('sources', [])
    identifiers = [row.get('id') for row in sources if isinstance(row, dict)]
    if (len(identifiers) != len(sources) or len(set(identifiers)) != len(identifiers)
            or any(not isinstance(key, str) or not key for key in identifiers)):
        raise ValueError('fixture source IDs must be nonempty and unique')
    if set(row['id'] for row in bundle['comparison_card']['sources']) - set(identifiers):
        raise ValueError('Comparison source IDs must belong to the fixture')
    content_sha256(fixture)
    return fixture


def build_blueprint(fixture, call_allocation):
    """Project existing inputs into a proposal; never record user confirmation.

    ``call_allocation`` is an externally owned budget description. It is preserved
    and hash-bound, but this function does not reserve or spend any request.
    """
    value = _fixture(fixture)
    if not isinstance(call_allocation, dict) or not call_allocation:
        raise ValueError('call_allocation must describe this case and the shared budget')
    bundle, plan = value['bundle'], value['plan']
    comparison, intent = bundle['comparison_card'], bundle['intent_card']
    blueprint = dict(schema_version='c5-blueprint-1', case_id=value['id'], title=value['title'],
        input_sha256=content_sha256(value), plan_sha256=plan_sha256(plan),
        known=dict(goal=value['brief'], confirmed=copy.deepcopy(bundle.get('confirmed', [])),
            constraints=copy.deepcopy(bundle.get('constraints', [])),
            preferences=copy.deepcopy(bundle.get('preferences', [])),
            interpretation='仅搬运输入；confirmed 保留原记录，未结构化的偏好与约束见原始 brief。'),
        provisional=dict(assumptions=copy.deepcopy(intent['assumptions']),
            plan=copy.deepcopy(intent['provisional_plan']), questions=copy.deepcopy(intent['questions']),
            confirmation_status='not_confirmed_by_pipeline'),
        unknowns=dict(intent=copy.deepcopy(intent['unknowns']),
            comparison=copy.deepcopy(comparison['unknowns']),
            supply_evidence=copy.deepcopy(bundle.get('supply_evidence', {}))),
        pending_comparison=dict(title=comparison['title'], options=copy.deepcopy(comparison['options']),
            recommendation=copy.deepcopy(comparison['recommendation']),
            question=copy.deepcopy(comparison['question']), selection_status='not_recorded',
            recorded_selection=copy.deepcopy(bundle.get('comparison_selection'))),
        current_plan=dict(title=plan['title'], summary=plan.get('summary', ''),
            days=[dict(day=day['day'], date=day.get('date'), title=day['title'],
                       item_ids=[item['id'] for item in day['items']]) for day in plan['days']]),
        sections=[dict(id=key, title=title, status='待做') for key, title in SECTIONS],
        review_criteria=list(CRITERIA), call_allocation=copy.deepcopy(call_allocation),
        approval_status='pending', source_scope='仅使用本次冻结输入，未知不视为通过')
    blueprint['blueprint_sha256'] = content_sha256(blueprint)
    return blueprint


def render_blueprint(blueprint):
    lines = ['# ' + blueprint['title'] + ' · 组装蓝图',
             '目标：' + blueprint['known']['goal'], '状态：待确认；本蓝图不代用户选择或确认。',
             '## 已知条件与输入', json.dumps(blueprint['known'], ensure_ascii=False, indent=2),
             '## 暂定方案与待确认比较',
             json.dumps(dict(provisional=blueprint['provisional'],
                             comparison=blueprint['pending_comparison']), ensure_ascii=False, indent=2),
             '## 七部分交付', *['- ' + row['title'] for row in blueprint['sections']],
             '## 未知与证据边界', json.dumps(blueprint['unknowns'], ensure_ascii=False, indent=2),
             '## 调用分配', json.dumps(blueprint['call_allocation'], ensure_ascii=False, indent=2),
             '输入哈希：' + blueprint['input_sha256'],
             '蓝图哈希：' + blueprint['blueprint_sha256']]
    return '\n\n'.join(lines) + '\n'


def _approved_blueprint(fixture, approval):
    if not isinstance(approval, dict) or approval.get('confirmed') is not True:
        raise ValueError('A confirmed blueprint approval is required before assembly')
    blueprint = approval.get('blueprint')
    if not isinstance(blueprint, dict):
        raise ValueError('Approval must include the exact approved blueprint')
    expected = build_blueprint(fixture, blueprint.get('call_allocation'))
    if (blueprint != expected or approval.get('blueprint_sha256') != expected['blueprint_sha256']):
        raise ValueError('Approved blueprint or input hash does not match this fixture')
    return expected


def _text_file(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _request_file(directory, request_id, mode, payload):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    _component(request_id)
    descriptor = os.open(directory / 'request.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
        json.dump(dict(request_id=request_id, mode=mode, payload_sha256=content_sha256(payload)),
                  handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    private_json(directory / 'request-payload.json', payload)


def _assembly_payload(value, blueprint):
    bundle = value['bundle']
    payload = {key: copy.deepcopy(bundle[key]) for key in (
        'comparison_card', 'intent_card', 'confirmed', 'sources', 'comparison_selection',
        'supply_evidence', 'supply_seeds', 'confirmed_trip_context', 'price_rules', 'price_as_of',
        'destination') if key in bundle}
    payload.update(case_id=value['id'], title=value['title'], brief=value['brief'],
                   plan=copy.deepcopy(value['plan']), provenance=copy.deepcopy(value['provenance']),
                   input_sha256=blueprint['input_sha256'],
                   blueprint_sha256=blueprint['blueprint_sha256'])
    payload['delivery_context'] = build_alignment_context(payload)
    if value.get('revision_review') is not None:
        payload['revision_review'] = copy.deepcopy(value['revision_review'])
    return payload


def delivery_checklist(handbook, review=None):
    """Record structure and explicit critic verdicts separately, without inference."""
    criteria = {row['id']: row['verdict'] for row in (review or {}).get('criteria', [])}
    rows = [dict(id=key, label=title,
                 structure_status='已做' if key in handbook['sections'] else '待做',
                 evidence_status='未知') for key, title in SECTIONS]
    rows += [dict(id='criterion:' + key, label=label,
                  structure_status='已做' if key in criteria else '待做',
                  evidence_status={'pass': '已做', 'fail': '待做'}.get(criteria.get(key), '未知'))
             for key, label in zip(CRITERIA, CRITERION_LABELS)]
    rows += [dict(id='claim:' + row['id'], label=row['id'], structure_status='已做',
                  evidence_status={'pass': '已做', 'fail': '待做'}.get(row['verdict'], '未知'))
             for row in (review or {}).get('claims', [])]
    rows.append(dict(id='readonly-review', label='只读审查',
                     structure_status='已做' if review else '待做',
                     evidence_status={'ready': '已做', 'failed': '待做'}.get(
                         (review or {}).get('outcome'), '未知')))
    if 'pretravel_recheck' in handbook['sections']:
        reference = handbook['sections']['pretravel_recheck']
        rows.append(dict(id='pretravel_recheck', label='出行前复核清单', structure_status='已做',
            evidence_status='待复核', pending_count=reference['pending_count'], rows_sha256=reference['rows_sha256']))
    rows = connection_delivery_state(handbook['sections'], handbook['review'], rows)[1]
    return alignment_checklist(handbook.get('delivery', {}), rows)


def _checklist_text(rows):
    return '# 交付清单\n\n结构完成与证据完成分别记账；未知不等于通过。\n\n' + '\n'.join(
        '- ' + row['label'] + '：结构 ' + row['structure_status'] + ' · 证据 ' + row['evidence_status']
        for row in rows) + '\n'


def _write_handbook(directory, handbook, suffix=''):
    directory = Path(directory)
    paths = dict(handbook_json=str(directory / ('handbook' + suffix + '.json')),
        handbook_markdown=str(directory / ('handbook' + suffix + '.md')),
        handbook_html=str(directory / ('handbook' + suffix + '.html')),
        checklist_json=str(directory / ('checklist' + suffix + '.json')),
        checklist_markdown=str(directory / ('checklist' + suffix + '.md')))
    private_json(paths['handbook_json'], handbook)
    _text_file(paths['handbook_markdown'], render_handbook(handbook))
    _text_file(paths['handbook_html'], render_html(handbook))
    private_json(paths['checklist_json'], handbook['delivery_checklist'])
    _text_file(paths['checklist_markdown'], _checklist_text(handbook['delivery_checklist']))
    if 'pretravel_recheck' in handbook['sections']:
        from .runtime_reference_delivery import _refresh_rendered_outputs
        _refresh_rendered_outputs(handbook, paths)
    return paths


def persist_case(case, output_dir):
    """Save an assembled or C3-derived version; callers retain the frozen fixture.

    To save C3 apply/undo output, update ``case['plan']`` and ``case['handbook']``
    together, then pass a fresh output directory. Original versions stay intact.
    """
    result, directory = copy.deepcopy(case), Path(output_dir).resolve()
    if (directory / 'case.json').exists():
        raise FileExistsError('Persist to a new case version directory; original artifacts are immutable')
    _approved_blueprint(result['fixture'], result['approval'])
    if result['blueprint'] != result['approval']['blueprint']:
        raise ValueError('Case blueprint must match its approved version')
    handbook, plan = result['handbook'], result['plan']
    if handbook['plan_sha256'] != plan_sha256(plan):
        raise ValueError('Handbook and current plan must match before persistence')
    if handbook['sections']['daily'] != plan['days']:
        raise ValueError('Handbook daily section must match the current plan')
    result['input_sha256'] = content_sha256(result['fixture'])
    result['plan_sha256'] = plan_sha256(plan)
    result['handbook_sha256'] = content_sha256(handbook)
    result['paths'] = _write_handbook(directory, handbook)
    for name, value in (('input', result['fixture']), ('blueprint', result['blueprint']),
                        ('approval', result['approval']), ('notes', result['notes']), ('plan', plan)):
        path = directory / (name + '.json')
        private_json(path, value)
        result['paths'][name] = str(path)
    _text_file(directory / 'blueprint.md', render_blueprint(result['blueprint']))
    result['paths']['blueprint_markdown'] = str(directory / 'blueprint.md')
    result['paths']['case'] = str(directory / 'case.json')
    private_json(result['paths']['case'], result)
    return result


def assemble_case(fixture, approval, model, request_id, output_dir):
    """Approve exact inputs, invoke one supplied model, persist seven sections.

    The model callback has keyword arguments ``request_id``, ``payload`` and
    ``mode='assemble'`` and returns notes. No retry or real service is built in.
    """
    value = _fixture(fixture)
    blueprint = _approved_blueprint(value, approval)
    payload, directory = _assembly_payload(value, blueprint), Path(output_dir).resolve()
    _request_file(directory, request_id, 'assemble', payload)
    for name, artifact in (('input', value), ('blueprint', blueprint), ('approval', approval)):
        private_json(directory / (name + '.json'), artifact)
    _text_file(directory / 'blueprint.md', render_blueprint(blueprint))
    notes = model(request_id=request_id, payload=copy.deepcopy(payload), mode='assemble')
    if not isinstance(notes, dict):
        raise ValueError('Assembly model must return a notes object')
    private_json(directory / 'notes.json', notes)
    if 'delivery_alignment' not in notes:
        raise ValueError('Assembly generation must include delivery_alignment before delivery')
    _, alignment = apply_alignment(payload['delivery_context'], notes['delivery_alignment'])
    private_json(directory / 'delivery-alignment.json', alignment)
    bundle = dict(value['bundle'], case_id=value['id'], title=value['title'], brief=value['brief'])
    handbook = assemble_handbook(bundle, notes, value['plan'], blueprint_approval=approval,
                                receipt=value.get('delivery_receipt'),
                                notes_plan_sha256=plan_sha256(value['plan']),
                                delivery_alignment=alignment, alignment_provenance=value['provenance'])
    handbook['delivery_checklist'] = delivery_checklist(handbook)
    handbook = attach_revision_review(handbook, value.get('revision_review'))
    case = dict(schema_version='c5-pipeline-case-1', case_id=value['id'], request_id=request_id,
                fixture=value, plan=copy.deepcopy(value['plan']), blueprint=blueprint,
                approval=copy.deepcopy(approval), notes=copy.deepcopy(notes), handbook=handbook)
    if value.get('revision_review') is not None:
        case['revision_review'] = copy.deepcopy(value['revision_review'])
    return persist_case(case, directory)


def reassembly_fixture(case):
    """Prepare current-state input for the SAME approved assemble_case entry.

    This creates no approval and invokes no model. The prior frozen fixture and
    actual revision receipt remain untouched; callers approve the new blueprint.
    """
    from .delivery_consistency import project_delivery
    value = copy.deepcopy(case['fixture'])
    value['plan'] = copy.deepcopy(case['plan'])
    bundle = dict(value['bundle'], title=value['title'], brief=value['brief'])
    prior_audit = case['handbook'].get('audit')
    prior_audit = prior_audit if isinstance(prior_audit, dict) else {}
    materials, state, _ = project_delivery(bundle, value['plan'],
        prior_audit.get('delivery_alignment'), provenance=value['provenance'])
    # If no valid prior projection exists, originals are only NEW model input,
    # never a deliverable. assemble_case still requires its bound model decision.
    if materials['comparison_card'] is not None:
        value['bundle'].update(materials)
        value['brief'] = materials['brief']
    value['title'] = value['plan']['title']
    value['bundle']['title'] = value['title']
    value['bundle']['notes_plan'] = copy.deepcopy(value['plan'])
    value['delivery_receipt'] = copy.deepcopy(
        case['handbook']['sections']['revision'].get('source_envelope'))
    value['provenance']['delivery_reassembly'] = dict(
        previous_alignment=state['aligned_version'], current_version=state['current_version'],
        prior_selections_are_historical=True,
        revision=copy.deepcopy(case['handbook']['sections']['revision']))
    if case.get('revision_review') is not None:
        value['revision_review'] = copy.deepcopy(case['revision_review'])
        value['provenance']['revision_review'] = copy.deepcopy(case['revision_review'])
    return value


def _review_input(case):
    handbook, plan = case['handbook'], case['plan']
    _component(case['case_id'])
    _approved_blueprint(case['fixture'], case['approval'])
    if (case['input_sha256'] != content_sha256(case['fixture'])
            or case['plan_sha256'] != plan_sha256(plan)
            or case['handbook_sha256'] != content_sha256(handbook)
            or handbook['plan_sha256'] != case['plan_sha256']
            or handbook['sections']['daily'] != plan['days']
            or handbook['case_id'] != case['case_id']):
        raise ValueError('Review inputs do not match their input, plan and handbook hashes')
    return dict(case_id=case['case_id'], input_sha256=case['input_sha256'],
                plan_sha256=case['plan_sha256'], handbook_sha256=case['handbook_sha256'],
                handbook=copy.deepcopy(handbook),
                plan_metadata={key: copy.deepcopy(value) for key, value in plan.items() if key != 'days'},
                plan_days_location='handbook.sections.daily',
                provenance=copy.deepcopy(case['fixture']['provenance']))


def _findings(rows, expected_ids, group):
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError('Review ' + group + ' must contain findings')
    identifiers = [row.get('id') for row in rows]
    if len(identifiers) != len(set(identifiers)) or set(identifiers) != set(expected_ids):
        raise ValueError('Review ' + group + ' must cover every required ID exactly once')
    for row in rows:
        if row.get('verdict') not in ('pass', 'fail', 'unknown'):
            raise ValueError('Review finding must explicitly declare pass, fail or unknown')
        if row.get('risk') not in ('S1', 'S2', 'quality'):
            raise ValueError('Review finding must include its risk')
        if any(not isinstance(row.get(key), str) for key in ('basis', 'affected_object', 'needed')):
            raise ValueError('Review finding must include basis, affected_object and needed')
    return rows


def _artifact_reference(path, expected):
    path = Path(path).resolve()
    raw = path.read_bytes()
    if json.loads(raw) != expected:
        raise ValueError('Review audit reference differs from its retained original')
    return dict(ref=str(path), file_sha256=hashlib.sha256(raw).hexdigest(),
                content_sha256=content_sha256(expected))


def _diff_summary(rows):
    """Keep changed values, omit unchanged copies of list items by stable ID."""
    result = []
    for row in rows:
        before, after = row['before'].get('value'), row['after'].get('value')
        lists = (before, after)
        if not all(isinstance(items, list) and all(isinstance(item, dict) and
                isinstance(item.get('id'), str) for item in items) and
                len({item['id'] for item in items}) == len(items) for items in lists):
            result.append(copy.deepcopy(row))
            continue
        old, new = ({item['id']: item for item in items} for items in lists)
        changes = []
        for key in old.keys() & new.keys():
            fields = [field for field in sorted(old[key].keys() | new[key].keys())
                      if old[key].get(field) != new[key].get(field)
                      or (field in old[key]) != (field in new[key])]
            if fields:
                changes.append(dict(id=key, fields=[dict(field=field,
                    before=dict(exists=field in old[key], value=old[key].get(field)),
                    after=dict(exists=field in new[key], value=new[key].get(field))) for field in fields]))
        result.append(dict(path=copy.deepcopy(row['path']),
            before_exists=row['before']['exists'], after_exists=row['after']['exists'],
            order_before=list(old), order_after=list(new),
            removed=[copy.deepcopy(old[key]) for key in old if key not in new],
            added=[copy.deepcopy(new[key]) for key in new if key not in old],
            changed=sorted(changes, key=lambda item: item['id'])))
    return result


def _revision_summary(case, path):
    if path is None:
        raise ValueError('Compact revision review requires the retained eight-check evidence')
    record = json.loads(Path(path).read_bytes())
    evidence = record['evidence']
    original = case['handbook']['audit']['revision_demo_evidence']
    expected_checks = {'undo_preserves_later_edit', 'applied_changed_plan', 'applied_diff_matches',
        'undo_diff_matches', 'outside_scope_preserved', 'applied_handbook_consistent',
        'locked_items_preserved', 'undone_handbook_consistent'}
    checks = record['checks']
    if (record['case_id'] != case['case_id'] or evidence['undone']['trip']['plan'] != case['plan']
            or {k: v for k, v in original.items() if k != 'checks'} !=
               {k: v for k, v in evidence.items() if k != 'checks'}
            or checks != evidence['checks'] or set(checks) != expected_checks
            or any(type(value) is not bool for value in checks.values())
            or any(checks.get(key) != value for key, value in original['checks'].items())):
        raise ValueError('Compact revision evidence does not bind the original case and eight checks')
    return dict(original=_artifact_reference(path, record),
        **{key: copy.deepcopy(evidence[key]) for key in ('kind', 'request_id', 'scope_day_numbers',
            'selections', 'selections_are_new_user_confirmation', 'product_calls')},
        checks=copy.deepcopy(checks), diff_summary=dict(
            applied=_diff_summary(evidence['applied']['receipt']['diff']),
            later_edit=_diff_summary(evidence['later_edit']['diff']),
            undone=_diff_summary(evidence['undone']['receipt']['diff'])))


def _compact_review_input(case, payload, revision_evidence_path):
    """Project only approved duplicate audit fields; substantive text stays exact."""
    handbook = payload['handbook']
    handbook['blueprint_approval'] = _artifact_reference(
        case['paths']['approval'], handbook['blueprint_approval'])
    if 'revision_demo_evidence' in handbook.get('audit', {}):
        handbook['audit']['revision_demo_evidence'] = _revision_summary(case, revision_evidence_path)
    return payload


def _bound_review(report, case, request_id):
    if not isinstance(report.get('judgment'), str) or not report['judgment'].strip():
        raise ValueError('Review must include an explicit judgment')
    if report.get('outcome') not in ('ready', 'partial', 'failed'):
        raise ValueError('Review must include an explicit outcome')
    criteria = _findings(report.get('criteria'), CRITERIA, 'criteria')
    claims = _findings(report.get('claims'), [row['id'] for row in case['handbook']['claims']], 'claims')
    if report['outcome'] == 'ready' and any(row['verdict'] != 'pass' for row in criteria + claims):
        raise ValueError('Review ready outcome requires explicit pass for every criterion and claim')
    result = copy.deepcopy(report)
    result.update(reviewed_plan_sha256=case['plan_sha256'],
                  reviewed_handbook_sha256=case['handbook_sha256'],
                  reviewed_input_sha256=case['input_sha256'], request_id=request_id)
    return result


def _save_reviewed_case(case, report, directory):
    handbook = copy.deepcopy(case['handbook'])
    handbook['review'] = dict(status=report['outcome'], report=report, binding_status='current')
    handbook['delivery_checklist'] = delivery_checklist(handbook, report)
    handbook['review'], _ = connection_delivery_state(handbook['sections'], handbook['review'], [])
    handbook = attach_revision_review(handbook, case.get('revision_review'))
    paths = _write_handbook(directory, handbook, '-reviewed')
    paths['review'] = str(Path(directory) / 'review.json')
    private_json(paths['review'], report)
    result = dict(case_id=case['case_id'], input_sha256=case['input_sha256'],
                  plan_sha256=case['plan_sha256'], original_handbook_sha256=case['handbook_sha256'],
                  handbook_sha256=content_sha256(handbook), review=report, handbook=handbook, paths=paths)
    private_json(Path(directory) / 'reviewed-case.json', result)
    return result


def review_cases(cases, review_model, request_id, output_dir, *, compact=False,
                 revision_evidence_path=None):
    """One read-only model batch, complete independent reports for every case.

    The callback uses ``mode='review_batch'`` and returns ``{'cases': [...]}``.
    All coverage and hash checks complete before any reviewed edition is written.
    Raw model output persists even when its report is malformed or incomplete.
    """
    if not isinstance(cases, list) or not cases:
        raise ValueError('Review requires at least one case')
    snapshots = copy.deepcopy(cases)
    identifiers = [case['case_id'] for case in snapshots]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError('Review case IDs must be unique')
    payload = dict(cases=[_review_input(case) for case in snapshots], criteria_ids=list(CRITERIA))
    if compact:
        payload['cases'] = [_compact_review_input(case, value, revision_evidence_path)
                            for case, value in zip(snapshots, payload['cases'])]
    directory = Path(output_dir).resolve()
    _request_file(directory, request_id, 'review_batch', payload)
    raw = review_model(request_id=request_id, payload=copy.deepcopy(payload), mode='review_batch')
    private_json(directory / 'review-raw.json', raw)
    reports = raw.get('cases') if isinstance(raw, dict) else None
    if not isinstance(reports, list) or any(not isinstance(row, dict) for row in reports):
        raise ValueError('Batch review must return a cases list')
    found = [row.get('case_id') for row in reports]
    if len(found) != len(set(found)) or set(found) != set(identifiers):
        raise ValueError('Batch review must report every case exactly once')
    by_id = {row['case_id']: row for row in reports}
    bound = [_bound_review(by_id[case['case_id']], case, request_id) for case in snapshots]
    result = dict(request_id=request_id, cases=[
        _save_reviewed_case(case, report, directory / case['case_id'])
        for case, report in zip(snapshots, bound)])
    private_json(directory / 'review-results.json', result)
    return result


def run_assembly(fixture, approval, model, request_id, output_dir, *, budget):
    """One destination-independent assembly/review run, using the shared ledger.

    Supply file and destination metadata live in fixture.supply_input. Existing
    notes generation, price/freshness projection and independent review are reused.
    The caller owns authorization and the number of runs; failures are not retried.
    """
    directory = Path(output_dir).resolve()
    with budget.run(request_id, kind='assembly', case_id=fixture['id'],
                    artifact_path=directory / 'run.json'):
        if hasattr(model, 'preflight'):
            model.preflight(fixture=fixture, approval=approval,
                            output_path=directory / 'preflight.json')
        with budget.stage(request_id, 'supply_and_blueprint'):
            value = _fixture(fixture)
            _approved_blueprint(value, approval)
        with budget.stage(request_id, 'assembly_and_projection'):
            case = assemble_case(value, approval, model, request_id + '-assemble',
                                 directory / 'assembled')
        with budget.stage(request_id, 'readonly_review'):
            reviewed = review_cases([case], model, request_id + '-review',
                                    directory / 'review', compact=True)
        result = dict(case=case, reviewed=reviewed['cases'][0])
        private_json(directory / 'result.json', result)
    return dict(result, run=budget.run_snapshot(request_id))


def _revision_input_case(case, feedback, selections, scope, provenance_updates):
    """Bind runtime feedback before dispatch; preserve the original authority.

    Only explicit provenance additions are accepted here. User selections and
    confirmed travel facts are never invented, and arbitrary stale approvals
    still fail before the formal service can commit a revision.
    """
    value = copy.deepcopy(case)
    _approved_blueprint(value['fixture'], value['approval'])
    if value['blueprint'] != value['approval']['blueprint']:
        raise ValueError('Case blueprint must match its approved version')
    if provenance_updates is not None:
        if not isinstance(provenance_updates, dict):
            raise ValueError('Runtime provenance updates must be an object')
        previous = value['approval']
        value['fixture']['provenance'].update(copy.deepcopy(provenance_updates))
        value['fixture'] = _fixture(value['fixture'])
        blueprint = build_blueprint(value['fixture'], value['blueprint']['call_allocation'])
        value['approval'] = dict(previous, blueprint=blueprint,
            blueprint_sha256=blueprint['blueprint_sha256'], runtime_binding=dict(
                kind='runtime_continuation', parent_blueprint_sha256=previous['blueprint_sha256'],
                previous_input_sha256=case['input_sha256'], current_input_sha256=blueprint['input_sha256'],
                new_user_confirmation=False, change_scope='explicit_provenance_only'))
        value['blueprint'] = blueprint
        value['input_sha256'] = content_sha256(value['fixture'])
    value['revision_input'] = dict(feedback=feedback, selections=copy.deepcopy(selections),
                                  scope_day_numbers=copy.deepcopy(scope))
    return value


def _review_revision(case, result, callback, request_id, directory):
    payload = revision_review_payload(case, result)
    private_json(directory / 'revision-review-payload.json', payload)
    pending = dict(outcome='unknown', reviewed_plan_sha256=payload['reviewed_plan_sha256'],
                   reviewed_before_plan_sha256=payload['reviewed_before_plan_sha256'],
                   affected_days=payload['affected_days'])
    if callback is None or not payload['affected_days']:
        report = dict(pending, judgment='尚无针对当前受影响整日的一次语义审查。', state='not_run')
    else:
        try:
            raw = callback(request_id=request_id + '-review', payload=copy.deepcopy(payload),
                           mode='revision_review')
            private_json(directory / 'revision-review-raw.json', raw)
            report = bind_revision_review(raw, payload)
        except Exception as error:
            report = dict(pending, judgment='本次整日审查未取得可绑定结论，实际修订仍保留。',
                          state='unavailable', error_type=type(error).__name__)
    private_json(directory / 'revision-review.json', report)
    return report


def run_case_revision(case, service, trip_id, token, expected_revision, request_id,
                      output_dir, *, budget, intent_card, selections, scope_day_numbers,
                      feedback, provenance_updates=None, revision_review_model=None):
    """Apply C3 to an existing formal trip and reassemble its receipt and prices.

    The existing service owns authorization, locking, scope, commit and undo.
    This adapter only supplies context, rebuilds artifacts and records timings.
    """
    directory = Path(output_dir).resolve()
    case = _revision_input_case(case, feedback, selections, scope_day_numbers, provenance_updates)
    with budget.run(request_id, kind='revision', case_id=case['case_id'],
                    artifact_path=directory / 'run.json'):
        with budget.stage(request_id, 'read_formal_plan'):
            before = service.trips.get(trip_id, token)
            if plan_sha256(before['plan']) != case['plan_sha256']:
                raise ValueError('Re-read the case after a formal plan version change')
            bundle = case['fixture']['bundle']
            private_json(directory / 'before.json', before)
        with budget.stage(request_id, 'revision_apply_and_receipt'):
            result = service.apply(trip_id, token, expected_revision, request_id + '-apply',
                intent_card, selections, scope_day_numbers, confirmed=bundle.get('confirmed'),
                sources=bundle.get('sources'), verification_context=bundle.get('verification_context'),
                research_context=dict(feedback=feedback, destination=bundle.get('destination'),
                    provenance=case['fixture']['provenance'], context=bundle.get('research_context')))
            private_json(directory / 'receipt.json', result)
        with budget.stage(request_id, 'whole_day_semantic_review'):
            revision_review = _review_revision(case, result, revision_review_model, request_id, directory)
        with budget.stage(request_id, 'settlement_and_handbook'):
            revised = copy.deepcopy(case)
            revised['plan'] = copy.deepcopy(result['trip']['plan'])
            fixture = case['fixture']
            view = dict(bundle, case_id=case['case_id'], title=fixture['title'], brief=fixture['brief'])
            prior_audit = case['handbook'].get('audit')
            prior_audit = prior_audit if isinstance(prior_audit, dict) else {}
            revised['handbook'] = assemble_handbook(view, case['notes'], revised['plan'],
                blueprint_approval=case['approval'], receipt=result,
                notes_plan_sha256=prior_audit.get('notes_plan_sha256'),
                delivery_alignment=prior_audit.get('delivery_alignment'),
                alignment_provenance=fixture['provenance'])
            revised['handbook']['delivery_checklist'] = delivery_checklist(revised['handbook'])
            revised['revision_review'] = revision_review
            revised['handbook'] = attach_revision_review(revised['handbook'], revision_review)
            revised = persist_case(revised, directory / 'applied')
            private_json(directory / 'settlement.json', dict(plan_sha256=revised['plan_sha256'],
                budget=revised['handbook']['sections']['budget']))
    return dict(case=revised, receipt=result, revision_review=revision_review,
                run=budget.run_snapshot(request_id))
