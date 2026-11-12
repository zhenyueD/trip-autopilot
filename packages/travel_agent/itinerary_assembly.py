"""Pure C5 assembly: current C3 plan, evidence accounting, and readable delivery.

No generation, research, persistence, execution, or semantic verdicts occur here.
The caller supplies a confirmed blueprint and the immutable notes-plan baseline.
"""
from __future__ import annotations

import copy
import html
import json
from datetime import date
from decimal import Decimal

from .comparison_card import ComparisonCard
from .intent_questions import IntentQuestions
from .revision_plan import plan_sha256
from .validation import validate_plan


SECTIONS = (('overview', '总览'), ('daily', '逐日安排'), ('connections', '首末段与返程衔接'),
            ('budget', '预算'), ('tasks', '待办与外部事项'), ('revision', '修订回执区'),
            ('unknowns', '未知与假设清单'))
LABELS = {'unknown': '未知', 'unverified': '未核验', 'supported': '模型标记有支持',
          'estimate': '估算', 'external_pending': '外部待办', 'user_confirmed': '用户确认',
          'applied': '已应用', 'undone': '已撤销', 'proposed': '拟修改', 'stale': '需重新核对',
          'not_requested': '未提出修订，行程未修改',
          'pass': '通过', 'fail': '未通过', 'ready': '审查通过', 'partial': '部分待核对',
          'failed': '审查未通过'}


def _day_index(days, label):
    if not isinstance(days, list) or any(not isinstance(day, dict) for day in days):
        raise ValueError(label + ' must be a list of day objects')
    numbers = [day.get('day') for day in days]
    if (any(type(number) is not int or number < 1 for number in numbers)
            or len(numbers) != len(set(numbers))):
        raise ValueError(label + ' day numbers must be positive integers and unique')
    return dict(zip(numbers, days))


def project_confirmed_trip_context(plan, context):
    """Fill absent day fields from structured frozen user confirmations only.

    Call before freezing the fixture hash, notes plan, and blueprint. No prose
    is parsed, dates are not extrapolated, and existing contrary values fail.
    The caller retains ``context`` with its source and confirmation references
    in the bundle. Inputs and nested plan values are never mutated or aliased.
    """
    projected = copy.deepcopy(plan)
    if context is None or context == {}:
        return projected
    if not isinstance(context, dict) or context.get('source') != 'user_confirmed_frozen_replay':
        raise ValueError('Confirmed trip context requires user_confirmed_frozen_replay source')
    current = _day_index(projected.get('days'), 'Plan')
    confirmed = _day_index(context.get('days'), 'Confirmed trip context')
    for number, row in confirmed.items():
        if number not in current:
            raise ValueError(f'Confirmed trip context refers to absent plan day {number}')
        for field in ('date', 'city', 'lodging'):
            if field not in row:
                continue
            value = row[field]
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f'Confirmed trip context day {number} {field} must be nonempty text')
            existing = current[number].get(field)
            absent = existing is None or (isinstance(existing, str) and not existing.strip())
            if not absent and existing != value:
                raise ValueError(f'Confirmed trip context conflict at day {number} field {field}')
            if absent:
                current[number][field] = value
    return projected


def _rows(value, label):
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ValueError(label + ' must be a list of objects')
    ids = [row.get('id') for row in value]
    if any(not isinstance(key, str) or not key for key in ids) or len(ids) != len(set(ids)):
        raise ValueError(label + ' IDs must be nonempty and unique')
    return copy.deepcopy(value)


def _items(plan):
    return {item['id']: dict(day=day['day'], date=day.get('date'), lodging=day.get('lodging'),
                             item=item) for day in plan['days'] for item in day['items']}


def _references(row):
    return set(row.get('item_ids', [])) | {row[key] for key in ('from_item_id', 'to_item_id')
                                          if row.get(key)}


def _bind_notes(bundle, notes, plan, bound_hash):
    current, current_hash = _items(plan), plan_sha256(plan)
    baseline = bundle.get('notes_plan', bundle.get('plan'))
    matching = isinstance(baseline, dict) and plan_sha256(baseline) == bound_hash
    original = _items(baseline) if matching else {}
    changed = {key for key in set(original) | set(current) if original.get(key) != current.get(key)}
    output, stale = {}, []
    for group in ('connections', 'budget', 'tasks', 'claims', 'unknowns'):
        output[group] = []
        for row in _rows(notes.get(group, []), group):
            refs = _references(row)
            affected = refs - set(current)
            if bound_hash != current_hash:
                affected |= refs & changed if matching else refs
                if group == 'connections' and not refs and row.get('day') is not None:
                    day = row['day']
                    old = next((d for d in (baseline or {}).get('days', []) if d['day'] == day), None)
                    new = next((d for d in plan['days'] if d['day'] == day), None)
                    if not matching or old != new:
                        affected.add('day:' + str(day))
            row['binding_status'] = 'stale' if affected else 'current'
            if affected:
                stale.append(dict(group=group, id=row['id'], affected_item_ids=sorted(affected)))
                row = dict(id=row['id'], binding_status='stale', status='unknown',
                           item_ids=sorted(refs), affected_item_ids=sorted(affected),
                           text='关联安排已变更或缺少版本依据，原说明待重新核对。')
            output[group].append(row)
    return output, stale


def _claim_source_rejections(source, seed, as_of):
    """Availability is accounting metadata, never inferred from claim prose."""
    reasons = []
    for origin, atom in (('source', source), ('seed', seed)):
        if atom.get('usable_as_verified_supply') is False:
            reasons.append(origin + ':not_usable_as_verified_supply')
        for field in ('freshness_status', 'freshness'):
            if atom.get(field) in ('expired', 'audit_only', 'expired_event_audit_only'):
                reasons.append(origin + ':' + atom[field])
        reference = as_of or atom.get('as_of')
        if atom.get('fresh_until') and reference:
            try:
                expired = date.fromisoformat(atom['fresh_until']) < date.fromisoformat(reference)
            except (TypeError, ValueError):
                reasons.append(origin + ':invalid_freshness_window')
            else:
                if expired:
                    reasons.append(origin + ':expired')
    return reasons


def _claim_eligibility(row, known, seeds, as_of):
    ids = set(row.get('source_ids', [])) | ({row['source_id']} if row.get('source_id') else set())
    rejected = {key: reasons for key in sorted(ids)
                if (reasons := _claim_source_rejections(known.get(key, {}), seeds.get(key, {}), as_of))}
    accounting = row['accounting']
    accounting.update(s1_eligible=bool(ids) and not rejected
        and not accounting['missing_source_ids'] and accounting['quote_exact_match'],
        ineligible_sources=rejected)
    if not rejected:
        return
    # Historical quotation is retained, but no generated evaluation is promoted.
    # The unprojected claim remains in audit.original_notes, including safe A usage.
    quote = row.get('quote') if (accounting['quote_exact_match']
                                and row.get('source_id') in rejected) else None
    row.update(risk='S2' if row.get('risk') == 'S1' else row.get('risk'),
        status='unverified' if quote else 'unknown', quote=quote, claim_use='historical_audit',
        text='历史事件／审计记录；不作当前安排事实或评价依据。' +
             ('原始引文：' + quote if quote else '无可展示的逐字历史引文，需重新核验。'))


def _source_accounting(notes, sources, seeds=(), as_of=None):
    known = {source['id']: source for source in sources}
    atoms = {seed['id']: seed for seed in seeds}
    records = []
    for group in ('connections', 'budget', 'claims'):
        for row in notes[group]:
            ids = row.get('source_ids', [])
            if row.get('source_id'):
                ids = list(ids) + [row['source_id']]
            missing = sorted(set(ids) - set(known))
            quote = row.get('quote')
            source = known.get(row.get('source_id'), {})
            quote_match = bool(quote) and quote in source.get('content', '')
            row['accounting'] = dict(missing_source_ids=missing,
                quote_exact_match=quote_match if group == 'claims' else None)
            if missing or (group == 'claims' and row.get('status') == 'supported'
                           and not quote_match):
                # Membership/quote bookkeeping only; semantic truth is reviewed separately.
                row['status'] = 'unknown'
            if group == 'claims':
                _claim_eligibility(row, known, atoms, as_of)
            records.append(dict(group=group, id=row['id'], **row['accounting']))
    return records


def _budget(rows):
    totals, unknown = {}, 0
    for row in rows:
        amount, quantity = row.get('amount'), row.get('quantity', 1)
        currency = row.get('currency') or '币种未知'
        valid = (type(amount) in (int, float) and type(quantity) is int and quantity > 0
                 and bool(row.get('basis')) and bool(row.get('currency'))
                 and row['binding_status'] == 'current'
                 and not row.get('accounting', {}).get('missing_source_ids'))
        subtotal = totals.setdefault(currency, dict(currency=currency, known_subtotal=Decimal(0),
                                                    known_count=0, unknown_count=0))
        if valid:
            value = Decimal(str(amount)) * quantity
            if not value.is_finite() or value < 0:
                raise ValueError('Budget amounts must be finite and nonnegative')
            row['line_amount'] = float(value)
            subtotal['known_subtotal'] += value
            subtotal['known_count'] += 1
        else:
            row['line_amount'] = None
            subtotal['unknown_count'] += 1
            unknown += 1
    for subtotal in totals.values():
        subtotal['known_subtotal'] = float(subtotal['known_subtotal']) if subtotal['known_count'] else None
        subtotal['label'] = '已知小计' if unknown else '已列项目合计'
    return dict(items=rows, currencies=list(totals.values()), unknown_count=unknown,
                status='unknown' if unknown or not rows else 'accounted',
                scope='仅覆盖已列项目；完整性由只读审查判断')


def _path_value(plan, path):
    value = plan
    try:
        for kind, key in path:
            value = value[key] if kind == 'key' else next(row for row in value if row.get(kind) == key)
        return dict(exists=True, value=value)
    except (KeyError, IndexError, StopIteration, TypeError, AttributeError):
        return dict(exists=False)


def _revision(envelope, plan):
    if envelope is None:
        return dict(state='unknown', receipt=None, plan_consistency='unknown')
    if envelope.get('state') == 'not_requested':
        if envelope.get('receipt') or envelope.get('revision_id') or envelope.get('diff'):
            raise ValueError('An unrequested revision cannot contain an execution receipt')
        return dict(state='not_requested', receipt=None, plan_consistency='not_applicable',
                    reason=envelope.get('reason', '未提供修订反馈'), source_envelope=copy.deepcopy(envelope))
    raw = copy.deepcopy(envelope)
    receipt = copy.deepcopy(raw.get('receipt', raw))
    state = raw.get('state', raw.get('status', 'unknown'))
    if state not in ('applied', 'undone', 'proposed'):
        state = 'unknown'
    actual = raw.get('trip', {}).get('plan')
    if actual is not None and plan_sha256(actual) != plan_sha256(plan):
        raise ValueError('Receipt trip plan does not match the rendered current plan')
    checks = []
    for change in receipt.get('diff', []):
        original_undone = state == 'undone' and raw.get('kind') not in ('undo', None)
        expected = change.get('before' if state == 'proposed' or original_undone else 'after')
        if state in ('applied', 'undone', 'proposed'):
            checks.append(_path_value(plan, change['path']) == expected)
    consistency = 'pass' if checks and all(checks) else ('fail' if checks else 'unknown')
    return dict(state=state, receipt=receipt, plan_consistency=consistency,
                source_envelope=raw, execution='由调用方提供的真实 C3 状态；此手册不执行修改')


def _review(review, current_hash):
    if review is None:
        return dict(status='unknown', report=None, binding_status='unknown')
    report = copy.deepcopy(review)
    matches = report.get('reviewed_plan_sha256') == current_hash
    return dict(status=report.get('outcome', 'unknown') if matches else 'unknown',
                report=report, binding_status='current' if matches else 'stale')


def _checklist(sections, review):
    criteria = {row['id']: row.get('verdict', 'unknown')
                for row in (review.get('report') or {}).get('criteria', [])}
    if review['binding_status'] != 'current':
        criteria = {}
    rows = [dict(id=key, label=title, structure_status='已做', evidence_status='未知')
            for key, title in SECTIONS]
    rows += [dict(id='criterion:' + key, label=key, structure_status='已做',
                  evidence_status={'pass': '已做', 'fail': '待做'}.get(value, '未知'))
             for key, value in criteria.items()]
    rows.append(dict(id='readonly-review', label='只读审查',
        structure_status='已做' if review['report'] else '待做',
        evidence_status={'ready': '已做', 'failed': '待做'}.get(review['status'], '未知')))
    return rows


def connection_delivery_state(sections, review, checklist):
    """Cap delivery on stale connections while preserving the raw critic report."""
    stale = [row['id'] for row in sections.get('connections', [])
             if row.get('binding_status') == 'stale']
    checked, rows = copy.deepcopy(review), copy.deepcopy(checklist)
    if not stale:
        return checked, rows
    checked['delivery_blockers'] = [dict(connection_id=key, reason='stale_connection') for key in stale]
    if checked['status'] == 'ready':
        checked['status'] = 'partial'
    for row in rows:
        if row['id'] in ('connections', 'criterion:executable', 'readonly-review'):
            row.update(blocking=True, evidence_status='阻断', blocked_connection_ids=stale)
    rows += [dict(id='connection:' + key, label='衔接 ' + key + '：需重新组装核对',
                  structure_status='已做', evidence_status='阻断', blocking=True,
                  connection_id=key, reason='stale_connection') for key in stale]
    return checked, rows


def assemble_handbook(bundle, notes, plan, *, blueprint_approval, receipt=None, review=None,
                      notes_plan_sha256=None, delivery_alignment=None, alignment_provenance=None):
    """Assemble all seven sections; unknown evidence never implies acceptance.

    ``bundle['notes_plan']`` is the immutable C3 plan used to generate notes.
    Receipt may be a C3 result envelope with an explicit ``state``. Reviewer
    results require ``reviewed_plan_sha256``. Inputs are never mutated.
    """
    if (not isinstance(blueprint_approval, dict) or blueprint_approval.get('confirmed') is not True
            or not isinstance(blueprint_approval.get('blueprint_sha256'), str)
            or not blueprint_approval['blueprint_sha256'].strip()):
        raise ValueError('A confirmed, hash-bound blueprint is required before assembly')
    validate_plan(plan, plan.get('supplies', []))
    # Local import: the alignment component reuses this module's source policy.
    from .delivery_consistency import project_delivery, alignment_checklist
    materials, delivery, alignment_audit = project_delivery(
        bundle, plan, delivery_alignment, provenance=alignment_provenance)
    comparison = (ComparisonCard.model_validate(materials['comparison_card']).model_dump()
                  if materials['comparison_card'] is not None else None)
    intent = (IntentQuestions.model_validate(materials['intent_card']).model_dump()
              if materials['intent_card'] is not None else None)
    current_hash = plan_sha256(plan)
    sources = _rows(bundle.get('sources', []), 'sources')
    if comparison and set(row['id'] for row in comparison['sources']) - {row['id'] for row in sources} and not delivery['reference_only']:
        raise ValueError('Comparison source IDs must belong to the bundle')
    bound, stale = _bind_notes(bundle, notes, plan, notes_plan_sha256)
    accounting = _source_accounting(bound, sources, bundle.get('supply_seeds', []),
                                    bundle.get('price_as_of'))
    pricing = None
    if bundle.get('price_rules'):
        from .itinerary_price_projection import project_prices
        pricing = project_prices(bound['budget'], bundle['supply_seeds'],
            rules=bundle['price_rules'], as_of=bundle['price_as_of'], plan=plan,
            binding_plan=bundle.get('notes_plan'))
        actual_items = _items(plan)
        for item_id, price in pricing['item_prices'].items():
            if actual_items[item_id]['item'].get('price') != price:
                raise ValueError('Formal plan price must match the verified frozen projection')
    checked_review = _review(review, current_hash)
    sections = dict(overview=dict(brief=materials['brief'], summary=plan.get('summary', ''),
        confirmed=materials['confirmed'], comparison=comparison, intent=intent,
        judgment=notes.get('judgment') if notes_plan_sha256 == current_hash else
        '当前计划已变更或缺少版本依据，原组装判断待重新审查。'),
        daily=copy.deepcopy(plan['days']), connections=bound['connections'],
        budget=pricing['budget'] if pricing else _budget(bound['budget']),
        tasks=bound['tasks'], revision=_revision(receipt, plan),
        unknowns=dict(items=bound['unknowns'] + (pricing['unknowns'] if pricing else []),
                      assumptions=intent['assumptions'] if intent else [],
                      intent_unknowns=intent['unknowns'] if intent else [],
                      comparison_unknowns=comparison['unknowns'] if comparison else []))
    checked_review, checklist = connection_delivery_state(sections, checked_review,
                                                         _checklist(sections, checked_review))
    return dict(schema_version='c5-handbook-1', case_id=bundle['case_id'],
        title=materials['title'] or plan['title'], plan_sha256=current_hash, delivery=delivery,
        blueprint_approval=copy.deepcopy(blueprint_approval), sections=sections,
        claims=bound['claims'], sources=sources, review=checked_review,
        delivery_checklist=alignment_checklist(delivery, checklist),
        audit=dict(original_notes=copy.deepcopy(notes), notes_plan_sha256=notes_plan_sha256,
                   delivery_alignment=alignment_audit,
                   stale_notes=stale, source_accounting=accounting,
                   **(dict(price_projection=dict(dispositions=pricing['dispositions'],
                       as_of=bundle['price_as_of'])) if pricing else {}),
                   provenance=copy.deepcopy(bundle.get('provenance', {}))))


def _text(value):
    if value is None or value == '':
        return '未知'
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _status(value):
    return LABELS.get(value, _text(value))


def _overview_lines(value):
    card, intent = value['comparison'], value['intent']
    lines = [value['brief'], value['summary'], '组装判断：' + _text(value['judgment']), '已定事项：']
    lines += ['- ' + row['text'] for row in value['confirmed']] or ['- 未提供']
    if card is None or intent is None:
        return lines + ['旧比较与意图材料未取得有效对齐记录，待重新组装；当前计划与实际修订回执保留。']
    lines += ['取舍依据：' + card['recommendation']['reason'],
              '接受的取舍：' + card['recommendation']['tradeoff'], '意图理解：' + intent['framing']]
    for question in intent['questions']:
        lines += ['- 待确认：' + question['text'], '  影响：' + question['why_it_matters']]
    return lines


def _daily_lines(days):
    lines = []
    for day in days:
        lines += [f"### 第 {day['day']} 天 · {day['title']}",
                  f"日期 {_text(day.get('date'))} · 城市 {_text(day.get('city'))}",
                  f"住宿 {_text(day.get('lodging'))}", day.get('rationale', '')]
        items = day['items']
        lines += ['首项：' + (items[0]['title'] if items else '未知'),
                  '末项：' + (items[-1]['title'] if items else '未知')]
        rest = [item for item in items if item.get('activity_kind') == 'rest']
        lines += ['休息窗口：' + ('；'.join(f"{i['time']}–{i['end_time']} {i['title']}" for i in rest)
                                if rest else '未在当前计划中单列，适用性待核对')]
        for item in items:
            lines += [f"- {item['time']}–{item['end_time']} {item['title']} [{item['id']}]",
                      '  安排理由：' + _text(item.get('why')),
                      '  交通：' + _text(item.get('transport')),
                      '  准备：' + _text(item.get('preparation')),
                      '  入口/集合点：' + _text((item.get('action_guide') or {}).get('meeting_point'))]
    return lines


def _connection_lines(rows):
    lines = []
    fields = (('date', '日期'), ('timezone', '时区'), ('mode', '方式'), ('walking', '步行'),
              ('waiting', '候车'), ('transfer', '换乘'), ('ride', '车程'), ('buffer', '缓冲'),
              ('cost', '费用'), ('basis', '计价/估算依据'), ('source_ids', '来源'), ('checked_at', '查询时间'))
    for row in rows:
        if row['binding_status'] == 'stale':
            lines.append('- ' + row['id'] + '：' + row['text'])
            continue
        lines += [f"### {_text(row.get('origin'))} → {_text(row.get('destination'))}",
                  '状态：' + _status(row.get('status'))]
        lines += [label + '：' + _text(row.get(key)) for key, label in fields]
    return lines or ['未知：尚未提供门到门衔接。抵达、退房、行李、机场交通、值机需逐项审查。']


def _budget_lines(value):
    lines = [value['scope']]
    for row in value['items']:
        lines += ['- ' + _text(row.get('label', row['id'])) + '：' +
                  _text(row.get('line_amount')) + ' ' + _text(row.get('currency')),
                  '  计价依据：' + _text(row.get('basis')),
                  '  数量：' + _text(row.get('quantity')) + '；单价：' + _text(row.get('amount'))]
        if row.get('disposition'):
            lines.append('  价格状态：' + {'priced': '已计价（非锁价/预订）',
                'unpriced': '未计价', 'self_booked': '用户自订，不计入合计'}[row['disposition']])
            lines += ['  ' + part for part in row['price']['breakdown']]
    for row in value['currencies']:
        lines += [f"{row['label']}：{_text(row['known_subtotal'])} {row['currency']}"]
    lines.append(f"全部预算项中未计价：{value['unknown_count']} 项；未计价金额不计入小计。")
    return lines if value['items'] else lines + ['预算未知，未提供有计价依据的分项。']


def _task_lines(rows):
    lines = []
    for row in rows:
        lines += ['- ' + _status(row.get('status')) + '：' + row['text'],
                  '  责任方：' + _text(row.get('owner')) + '；触发/截止：' + _text(row.get('trigger'))]
    return lines or ['未知：尚未提供待办。不能据此判断现实事务已解决。']


def _receipt_lines(value):
    lines = ['实际状态：' + _status(value['state']), '与当前计划的账目一致性：' + _status(value['plan_consistency'])]
    receipt = value['receipt']
    if not receipt:
        return lines + ['尚无修订回执。']
    lines += ['判断：' + _text(receipt.get('judgment')), '真实 diff：']
    for row in receipt.get('diff', []):
        lines += ['- 路径：' + _text(row.get('path')), '  修改前：' + _text(row.get('before')),
                  '  修改后：' + _text(row.get('after'))]
    lines += ['替代方案：']
    for row in receipt.get('alternatives', []):
        lines += ['- ' + _text(row.get('label')) + '；后果/取舍：' + _text(row.get('tradeoff'))]
    action = receipt.get('next_action', {})
    lines += ['下一行动：' + _text(action.get('label')),
              '行动记录：' + _text(action), '执行说明：通过原 C3 操作入口执行，此静态手册只展示回执。']
    return lines


def _unknown_lines(value):
    lines = []
    for row in value['items']:
        lines += ['- ' + row['text'], '  影响：' + _text(row.get('impact')),
                  '  复查条件：' + _text(row.get('recheck_when'))]
    lines += ['- 暂定假设：' + row for row in value['assumptions']]
    lines += ['- 意图未知：' + row for row in value['intent_unknowns']]
    lines += ['- 比较未知：' + row for row in value['comparison_unknowns']]
    return lines or ['尚未列出未知；是否存在遗漏需只读审查确认。']


def _evidence_lines(handbook):
    lines = ['## 证据与只读审查', '审查状态：' + _status(handbook['review']['status'])]
    for row in handbook['claims']:
        if row['binding_status'] == 'stale':
            lines += ['- ' + row['id'] + '：' + row['text']]
            continue
        lines += [f"### {row.get('risk', '未知')} · {row['id']}", row['text'],
                  '声明状态：' + _status(row.get('status')) + '；此标签不等于审查通过',
                  '来源：' + _text(row.get('source_id')), '逐字引文：' + _text(row.get('quote')),
                  '核验时间：' + _text(row.get('checked_at')),
                  '逐字归属记账：' + _text(row.get('accounting'))]
    review = handbook['review']
    if review['report']:
        lines += ['版本绑定：' + _status(review['binding_status']),
                  '审查判断：' + _text(review['report'].get('judgment'))]
        for row in review['report'].get('criteria', []) + review['report'].get('claims', []):
            lines += ['- ' + row['id'] + '：' + _status(row.get('verdict')),
                      '  依据：' + _text(row.get('basis')), '  对象：' + _text(row.get('affected_object')),
                      '  待补：' + _text(row.get('needed'))]
    for source in handbook['sources']:
        lines += ['- 来源 ' + source['id'] + '：' + _text(source.get('title')),
                  '  ' + _text(source.get('url'))]
    return lines


def render_handbook(handbook):
    """Render the same assembled structure as readable Markdown."""
    sections = handbook['sections']
    renderers = (_overview_lines, _daily_lines, _connection_lines, _budget_lines,
                 _task_lines, _receipt_lines, _unknown_lines)
    lines = ['# ' + handbook['title'], '历史/离线资产回放手册 · 外部事项以实际凭证为准']
    if handbook.get('delivery', {}).get('status') != 'aligned':
        lines += ['交付状态：待重新组装。当前计划与已成立的修订回执仍有效；比较、意图及其假设如有展示，仅为上一版对齐参考，不代表本版已重新核对。']
    for index, ((key, title), render) in enumerate(zip(SECTIONS, renderers), 1):
        lines += [f'## {index:02d} {title}', *render(sections[key])]
    lines += ['## 交付清单', '结构完成和证据完成分别记账；未知不等于通过。']
    for row in handbook['delivery_checklist']:
        lines += [f"- {row['label']}：结构 {row['structure_status']} · 证据 {row['evidence_status']}"]
    lines += _evidence_lines(handbook)
    lines += ['版本：' + handbook['plan_sha256']]
    return '\n\n'.join(str(line) for line in lines if line != '') + '\n'


STYLE = """
:root{color-scheme:light;--ink:#1d302e;--muted:#586863;--line:#ccd3ca;--accent:#28594c}
*{box-sizing:border-box}body{margin:0;background:#eeeee5;color:var(--ink);font-family:ui-sans-serif,-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;line-height:1.8}
a{color:var(--accent);text-underline-offset:4px}a:focus-visible{outline:3px solid #a56f28;outline-offset:4px}
header{max-width:1120px;margin:auto;padding:64px 40px 38px;border-bottom:2px solid var(--ink)}
header p{color:var(--muted);max-width:65ch}h1{font-size:clamp(30px,5vw,58px);letter-spacing:-.035em;line-height:1.2;font-weight:650;margin:0 0 22px}
.layout{max-width:1120px;margin:auto;display:grid;grid-template-columns:195px minmax(0,1fr);gap:45px;padding:38px 40px 80px}
nav{position:sticky;top:24px;align-self:start;font-size:14px}nav a{display:block;min-height:44px;padding:10px 0;text-decoration:none;border-bottom:1px solid var(--line)}
main{min-width:0}section{padding:0 0 36px;margin-bottom:36px;border-bottom:1px solid var(--line);scroll-margin-top:24px}
h2{font-size:25px;line-height:1.4;letter-spacing:-.025em;margin:0 0 22px}h3{font-size:18px;margin:28px 0 12px;padding:10px 15px;background:#dde5da;border-left:3px solid var(--accent)}
p{margin:10px 0;overflow-wrap:anywhere}.detail{font-size:14px;color:var(--muted);padding-left:19px;margin:4px 0}.entry{padding-left:19px;position:relative}.entry:before{content:'—';position:absolute;left:0;color:var(--accent)}
.notice{border-left:3px solid #a56f28;padding:12px 18px;background:#f7f2e5}.version{font-size:12px;color:var(--muted);overflow-wrap:anywhere}
@media(max-width:700px){header{padding:32px 22px 25px}.layout{display:block;padding:22px}nav{position:static;display:grid;grid-template-columns:1fr 1fr;gap:0 20px;margin-bottom:34px}h2{font-size:22px}h3{font-size:17px}}
@media print{body{background:white;font-size:10pt}header{padding:0 0 20px}.layout{display:block;padding:20px 0}nav{display:none}section{break-inside:auto}h2,h3{break-after:avoid}a{color:inherit}h1{font-size:28pt}}
"""


def render_html(handbook):
    """Self-contained, script-free reading surface; no imitation action controls."""
    blocks, section = [], []
    headings = []
    for line in render_handbook(handbook).split('\n\n'):
        line = line.strip('\n')
        if line.startswith('# '):
            continue
        if line.startswith('## '):
            if section:
                blocks.append('</section>')
            key = 'section-' + str(len(headings) + 1)
            title = html.escape(line[3:])
            headings.append((key, title))
            blocks.append(f'<section id="{key}"><h2>{title}</h2>')
            section.append(key)
        elif line.startswith('### '):
            blocks.append('<h3>' + html.escape(line[4:]) + '</h3>')
        else:
            css = 'entry' if line.startswith('- ') else ('detail' if line.startswith('  ') else '')
            content = line[2:] if line.startswith('- ') else line
            blocks.append('<p class="' + css + '">' + html.escape(content) + '</p>')
    if section:
        blocks.append('</section>')
    links = ''.join(f'<a href="#{key}">{title}</a>' for key, title in headings)
    title = html.escape(handbook['title'])
    return ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{title}</title><style>{STYLE}</style></head><body>'
            f'<header><h1>{title}</h1><p>行程手册 · 当前计划、取舍依据与待办，一处查看。</p>'
            '<p class="notice">历史/离线资产回放。缺口保持可见，外部事项以实际凭证为准。</p></header>'
            f'<div class="layout"><nav aria-label="手册目录">{links}</nav><main>' + ''.join(blocks)
            + '</main></div></body></html>')
