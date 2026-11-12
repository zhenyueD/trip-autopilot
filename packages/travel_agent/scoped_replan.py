"""Deterministic routing and impact analysis for revision requests.

The classifier is deliberately conservative. It only selects the scoped path
when both the changed day and the affected planning domain are explicit. The
resolver then derives the allowed edit surface from persisted plan relations;
the model never chooses its own scope.
"""
from __future__ import annotations

import copy
import json
import re

from .journey_state import DECISION_FIELDS, DELTA_FIELDS, PREPARATION_FIELDS
from .validation import PlanError


PIPELINE_PURE = 'pure_adjustment'
PIPELINE_RESEARCH = 'needs_research'
PIPELINE_CORRECTION = 'fact_correction'

_CHINESE_DAYS = {
    '一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7,
    '八': 8, '九': 9, '十': 10, '十一': 11, '十二': 12, '十三': 13, '十四': 14,
}
_DOMAIN_KEYWORDS = {
    'flight': ('航班', '飞机', '机票', '起飞', '落地'),
    'lodging': ('住宿', '酒店', '民宿', '入住', '退房'),
    'transport': ('交通', '租车', '取车', '还车', '自驾', '打车', '出租车', '地铁', '公交', '开车', '出发'),
    'dining': ('餐厅', '吃饭', '午餐', '晚餐', '早餐', '餐饮'),
    'activities': ('景点', '活动', '门票', '参观', '体验'),
    'packing': ('行李', '打包', '携带', '准备物品'),
}
_RESEARCH_MARKERS = ('新增', '加一个', '推荐', '帮我找', '查一下', '换一家', '换个酒店', '换个景点')
_CORRECTION_MARKERS = ('你记错', '记得不对', '事实是', '纠正一下', '我其实', '不是我的')
_NEW_SUPPLY_WORDS = ('新', '另', '换', '找', '增加', '添加', '加一家', '改住')
_SUPPLY_NOUNS = ('酒店', '民宿', '餐厅', '饭店', '景点', '地点', '场馆', '体验',
                 '租车公司', '车行', '航班', '航空公司')
_DOMAIN_ITEM_FIELDS = {
    'flight': {'time', 'end_time', 'transport', 'preparation', 'action_guide'},
    'lodging': {'time', 'end_time', 'transport', 'preparation', 'action_guide'},
    'transport': {'time', 'end_time', 'transport', 'preparation', 'experience', 'action_guide'},
    'dining': {'time', 'end_time', 'preparation', 'action_guide'},
    'activities': {'time', 'end_time', 'preparation', 'experience', 'action_guide'},
    'packing': {'preparation', 'action_guide'},
}
_TIME_SHIFT_MARKERS = ('晚点', '早点', '晚些', '早些', '提前', '推迟', '延后', '延迟',
                       '顺延', '往后', '往前', '后移', '前移',
                       '时间改', '改到', '调整时间', '时间调整')


def _time_cascade_items(plan, intent, affected_items):
    """Over-approximate a shift from the first affected item, within each target day.

    Persisted item order is the validated schedule order. Do not follow relations
    from these extra nodes: a time shift grants no new content-edit permissions.
    """
    feedback = intent.get('feedback', '')
    relative_time = re.search(r'[晚早]\s*[一二两三四五六七八九十半\d]+个?\s*(?:小时|分钟)', feedback)
    if not relative_time and not any(marker in feedback for marker in _TIME_SHIFT_MARKERS):
        return set()
    result = set()
    for day in plan.get('days', []):
        if day.get('day') not in intent.get('target_days', []):
            continue
        follows_affected = False
        for item in day.get('items', []):
            item_id = item.get('id')
            follows_affected = follows_affected or item_id in affected_items
            if follows_affected and isinstance(item_id, str):
                result.add(item_id)
    return result - affected_items


def _target_days(feedback):
    result = set()
    for match in re.finditer(r'第\s*(\d{1,2}|十[一二三四]?|[一二两三四五六七八九])\s*天', feedback):
        raw = match.group(1)
        number = int(raw) if raw.isdigit() else _CHINESE_DAYS.get(raw)
        if number and 1 <= number <= 14:
            result.add(number)
    return sorted(result)


def _domains(feedback):
    return [domain for domain, words in _DOMAIN_KEYWORDS.items()
            if any(word in feedback for word in words)]


def _has_real_additions(additions):
    """Distinguish the browser's empty material envelope from real material."""
    if additions is None:
        return False
    if isinstance(additions, dict):
        note = additions.get('note') or ''
        return bool(note.strip()) or any(
            additions.get(key) for key in ('images', 'links', 'documents'))
    return bool(additions)


def classify_revision(previous_plan, feedback, additions=None):
    """Return an auditable routing decision without calling a model."""
    feedback = feedback.strip() if isinstance(feedback, str) else ''
    days, domains = _target_days(feedback), _domains(feedback)
    new_supply = (any(marker in feedback for marker in _NEW_SUPPLY_WORDS)
                  and any(noun in feedback for noun in _SUPPLY_NOUNS))
    if (_has_real_additions(additions) or new_supply
            or any(marker in feedback for marker in _RESEARCH_MARKERS)):
        requested = PIPELINE_RESEARCH
    elif any(marker in feedback for marker in _CORRECTION_MARKERS):
        requested = PIPELINE_CORRECTION
    else:
        requested = PIPELINE_PURE

    confident = (requested == PIPELINE_PURE and bool(feedback) and bool(days) and bool(domains)
                 and all(any(day.get('day') == number for day in (previous_plan or {}).get('days', []))
                         for number in days))
    executed = 'scoped' if confident else 'full'
    fallback = None if confident or requested != PIPELINE_PURE else 'ambiguous_scope'
    return {
        'requested_pipeline': requested,
        'executed_pipeline': executed,
        'fallback_reason': fallback,
        'target_days': days,
        'domains': domains,
        'scope_confident': confident,
        'feedback': feedback,
    }


def _related_ids(record):
    values = record.get('related_item_ids', []) if isinstance(record, dict) else []
    return [value for value in values if isinstance(value, str) and value]


def _matches_domain(item, domains):
    text_value = ' '.join(str(item.get(key, '')) for key in
                          ('title', 'why', 'transport', 'preparation', 'activity_kind'))
    kind = item.get('activity_kind')
    kind_matches = {
        'flight': kind in ('flight', 'airport', 'transfer'),
        'lodging': kind in ('lodging', 'hotel', 'check_in', 'check_out'),
        'transport': kind in ('transport', 'transfer', 'driving'),
        'dining': kind in ('meal', 'dining', 'restaurant'),
        'activities': kind in ('activity', 'visit', 'attraction', 'experience'),
        'packing': False,
    }
    return any(kind_matches.get(domain, False)
               or any(word in text_value for word in _DOMAIN_KEYWORDS.get(domain, ()))
               for domain in domains)


def resolve_plan_impact(plan, intent):
    """Resolve writable nodes from target items, then follow explicit relations."""
    plan = plan or {}
    domains = set(intent.get('domains', []))
    target_days = set(intent.get('target_days', []))
    items, item_days = {}, {}
    for day in (plan or {}).get('days', []):
        number = day.get('day')
        for item in day.get('items', []):
            if isinstance(item.get('id'), str):
                items[item['id']], item_days[item['id']] = item, number
    affected_days = {number for number in target_days
                     if any(day.get('day') == number for day in plan.get('days', []))}
    affected_items = {id_ for id_, item in items.items()
                      if item_days[id_] in affected_days and _matches_domain(item, domains)}
    decisions, preparations, actions, routes = set(), set(), set(), set()
    decision_rows = [row for row in plan.get('decisions', []) if isinstance(row, dict)]
    preparation_rows = [row for row in plan.get('preparation', []) if isinstance(row, dict)]
    pack = (plan or {}).get('preparation_pack') or {}
    action_rows = [row for row in (pack.get('actions', []) if isinstance(pack, dict) else [])
                   if isinstance(row, dict)]
    route_rows = [row for row in plan.get('routes', []) if isinstance(row, dict)]

    changed = True
    while changed:
        before = (len(affected_items), len(affected_days), len(decisions),
                  len(preparations), len(actions), len(routes))
        for route in route_rows:
            related = set(_related_ids(route)).intersection(items)
            if route.get('day') in affected_days or related.intersection(affected_items):
                routes.add(route.get('day'))
                affected_items.update(related)
        for rows, selected in ((decision_rows, decisions), (preparation_rows, preparations)):
            for row in rows:
                category = row.get('category')
                related = set(_related_ids(row)).intersection(items)
                linked = bool(related.intersection(affected_items))
                if category in domains or linked:
                    selected.add(category)
                if linked:
                    affected_items.update(related)
        for action in action_rows:
            related = set(_related_ids(action)).intersection(items)
            linked = bool(related.intersection(affected_items))
            targeted = (action.get('category') in domains and
                        (action.get('day') in affected_days
                         or action.get('day') is None and not related))
            if linked or targeted:
                if isinstance(action.get('id'), str):
                    actions.add(action['id'])
                affected_items.update(related)
        affected_days.update(item_days[id_] for id_ in affected_items if id_ in item_days)
        changed = before != (len(affected_items), len(affected_days), len(decisions),
                             len(preparations), len(actions), len(routes))

    routes.update(day for day in affected_days
                  if any(route.get('day') == day for route in route_rows))
    # A preparation patch replaces the whole category. It is safe only when
    # no unrelated same-category row can be erased by that replacement.
    replaceable = set()
    for category in preparations | (decisions & domains):
        rows = [row for row in preparation_rows if row.get('category') == category]
        if len(rows) <= 1 or all(_related_ids(row)
                                 and set(_related_ids(row)).issubset(affected_items) for row in rows):
            replaceable.add(category)

    allowed_fields = set()
    for domain in domains:
        allowed_fields.update(_DOMAIN_ITEM_FIELDS.get(domain, ()))
    item_field_allowlist = {id_: sorted(allowed_fields) for id_ in sorted(affected_items)}
    semantic_text_contracts = {
        id_: {'feedback': intent.get('feedback', ''),
              'original': {field: items[id_].get(field) for field in ('title', 'why')},
              'requires_semantic_review': True}
        for id_ in sorted(affected_items)
        if _matches_domain({key: items[id_].get(key, '')
                            for key in ('title', 'why', 'activity_kind')}, domains)
    }
    for id_ in semantic_text_contracts:
        item_field_allowlist[id_] = sorted(allowed_fields | {'title', 'why'})
    cascade_items = _time_cascade_items(plan, intent, affected_items)
    item_field_allowlist.update({id_: ['end_time', 'time'] for id_ in sorted(cascade_items)})
    affected_items.update(cascade_items)
    nodes = ([f'day:{number}' for number in affected_days]
             + [f'item:{id_}' for id_ in affected_items]
             + [f'decision:{category}' for category in decisions]
             + [f'preparation:{category}' for category in preparations]
             + [f'route:{number}' for number in routes]
             + [f'preparation_action:{id_}' for id_ in actions])
    return {
        'day_numbers': sorted(affected_days),
        'item_ids': sorted(affected_items),
        'decision_categories': sorted(decisions),
        'preparation_categories': sorted(preparations),
        'replaceable_preparation_categories': sorted(replaceable),
        'required_preparation_categories': sorted(
            category for category in preparations if category in domains and category in replaceable),
        'route_days': sorted(number for number in routes if isinstance(number, int)),
        'preparation_action_ids': sorted(actions),
        'affected_node_ids': sorted(nodes),
        'allowed_item_fields': sorted(allowed_fields),
        'item_field_allowlist': item_field_allowlist,
        'semantic_text_contracts': semantic_text_contracts,
        'time_cascade_item_ids': sorted(cascade_items),
    }


def _scope_violation(kind, node_id=None, fields=()):
    return {'kind': kind, 'node_id': node_id, 'fields': sorted(map(str, fields))}


def _field_violations(value, allowed, kind, node_id=None):
    """Collect forbidden keys before general merge validation can echo them."""
    unknown = set(value) - allowed if isinstance(value, dict) else set()
    return [_scope_violation(kind, node_id, unknown)] if unknown else []


def _raise_scope_error(violations, message='模型delta超出程序计算的影响范围'):
    if message == '模型delta超出程序计算的影响范围':
        codes = []
        for violation in violations:
            kind = violation['kind']
            kind = {'set_type': 'set', 'item_fields_type': 'item_fields',
                    'operations': 'day_changes/move_items'}.get(kind, kind)
            node_id = violation['node_id']
            codes.append(kind if node_id is None else f'{kind}:{node_id}')
        message += '：' + '、'.join(codes)
    error = PlanError(message)
    # This data is for server-side audit/repair only, never user-facing text.
    error.scope_violations = violations
    raise error


def validate_delta_scope(delta, impact, *, defer_text_review=False):
    """Reject any model operation outside the resolver-owned edit surface."""
    if not isinstance(delta, dict):
        raise PlanError('局部修改缺少可校验的delta')
    allowed_days = set(impact.get('day_numbers', []))
    allowed_items = set(impact.get('item_ids', []))
    allowed_decisions = set(impact.get('decision_categories', []))
    allowed_preparation = set(impact.get('replaceable_preparation_categories', []))
    required_preparation = set(impact.get('required_preparation_categories', []))
    allowed_item_fields = set(impact.get('allowed_item_fields', []))
    item_field_allowlist = impact.get('item_field_allowlist')

    outside = _field_violations(delta, DELTA_FIELDS, 'delta_fields')
    if delta.get('day_changes') or delta.get('move_items'):
        outside.append(_scope_violation('operations', fields=[
            key for key in ('day_changes', 'move_items') if delta.get(key)]))
    top_fields = delta.get('set', {})
    if not isinstance(top_fields, dict):
        outside.append(_scope_violation('set_type', fields=['set']))
    elif set(top_fields) - {'change_summary', 'reactions'}:
        outside.append(_scope_violation('set', fields=set(top_fields) - {'change_summary', 'reactions'}))
    for patch in delta.get('day_patches', []):
        if isinstance(patch, dict):
            outside.extend(_field_violations(
                patch, {'day', 'set', 'upsert_items', 'delete_item_ids'},
                'day_patch_fields', patch.get('day')))
        if not isinstance(patch, dict) or patch.get('day') not in allowed_days:
            outside.append(_scope_violation('day', patch.get('day') if isinstance(patch, dict) else None))
            continue
        if patch.get('set'):
            fields = patch['set']
            outside.append(_scope_violation('day_fields', patch.get('day'),
                                             fields if isinstance(fields, dict) else ['set']))
        for operation in patch.get('upsert_items', []):
            if isinstance(operation, dict):
                outside.extend(_field_violations(
                    operation, {'id', 'set', 'create'}, 'item_operation_fields', operation.get('id')))
            if not isinstance(operation, dict) or operation.get('create', False):
                outside.append(_scope_violation('item_create',
                                                 operation.get('id') if isinstance(operation, dict) else None))
                continue
            if operation.get('id') not in allowed_items:
                outside.append(_scope_violation('item', operation.get('id')))
                continue
            fields = operation.get('set', {})
            allowed = (set(item_field_allowlist.get(operation['id'], []))
                       if item_field_allowlist is not None else allowed_item_fields)
            if not isinstance(fields, dict):
                outside.append(_scope_violation('item_fields_type', operation.get('id'), ['set']))
            elif set(fields) - allowed:
                outside.append(_scope_violation('item_fields', operation.get('id'), set(fields) - allowed))
            if isinstance(fields, dict):
                text_fields = set(fields) & {'title', 'why'}
                contract = impact.get('semantic_text_contracts', {}).get(operation['id'])
                if text_fields and (not defer_text_review or not contract):
                    outside.append(_scope_violation('item_text_review_required', operation['id'], text_fields))
        for item_id in patch.get('delete_item_ids', []):
            outside.append(_scope_violation('item_delete', item_id))
    for patch in delta.get('decision_patches', []):
        if isinstance(patch, dict):
            outside.extend(_field_violations(
                patch, {'category', 'set'}, 'decision_patch_fields', patch.get('category')))
            outside.extend(_field_violations(
                patch.get('set'), DECISION_FIELDS, 'decision_fields', patch.get('category')))
        if not isinstance(patch, dict) or patch.get('category') not in allowed_decisions:
            outside.append(_scope_violation('decision', patch.get('category') if isinstance(patch, dict) else None))
    patched_preparation = set()
    for patch in delta.get('preparation_patches', []):
        if isinstance(patch, dict):
            outside.extend(_field_violations(
                patch, {'category', 'items'}, 'preparation_patch_fields', patch.get('category')))
            if isinstance(patch.get('items'), list):
                for item in patch['items']:
                    outside.extend(_field_violations(
                        item, PREPARATION_FIELDS, 'preparation_fields', patch.get('category')))
        if not isinstance(patch, dict) or patch.get('category') not in allowed_preparation:
            outside.append(_scope_violation('preparation', patch.get('category') if isinstance(patch, dict) else None))
        elif isinstance(patch.get('category'), str):
            patched_preparation.add(patch['category'])
    for category in required_preparation - patched_preparation:
        outside.append(_scope_violation('preparation_missing', category))
    if outside:
        _raise_scope_error(outside)


def scoped_text_changes(previous_plan, plan, impact):
    """Bind mandatory semantic review to actual candidate fields, not model claims."""
    before = {item['id']: item for day in previous_plan.get('days', [])
              for item in day.get('items', [])}
    after = {item['id']: item for day in plan.get('days', [])
             for item in day.get('items', [])}
    changes = []
    for item_id, contract in impact.get('semantic_text_contracts', {}).items():
        for field in ('title', 'why'):
            old, new = before.get(item_id, {}).get(field), after.get(item_id, {}).get(field)
            if old != new:
                changes.append({'item_id': item_id, 'field': field, 'before': old,
                                'after': new, 'feedback': contract['feedback']})
    return changes


def validate_scoped_result(previous_plan, plan, impact):
    """Require every preparation category coupled to the edit to change materially."""
    required = set(impact.get('required_preparation_categories', []))
    for category in required:
        before = [row for row in previous_plan.get('preparation', [])
                  if row.get('category') == category]
        after = [row for row in plan.get('preparation', [])
                 if row.get('category') == category]
        canonical_before = sorted(json.dumps(row, ensure_ascii=False, sort_keys=True)
                                  for row in before)
        canonical_after = sorted(json.dumps(row, ensure_ascii=False, sort_keys=True)
                                 for row in after)
        if canonical_before == canonical_after:
            raise PlanError(f'局部修改未实际更新关联准备类别：{category}')


def prepare_scoped_response(raw, impact, previous_plan):
    """Gate the entire model response and preserve unrelated digital artifacts."""
    if not isinstance(raw, dict):
        raise PlanError('局部修改必须返回对象')
    unknown = set(raw) - {'delta', 'journey'}
    if unknown:
        _raise_scope_error([_scope_violation('response_fields', fields=unknown)],
                           '局部修改返回了超出作用域的顶层字段')
    validate_delta_scope(raw.get('delta'), impact, defer_text_review=True)
    result = copy.deepcopy(raw)
    changed = set(impact.get('item_ids', [])) - set(impact.get('time_cascade_item_ids', []))
    old_artifacts = ((previous_plan or {}).get('journey') or {}).get('artifacts', [])
    preserved = [copy.deepcopy(row) for row in old_artifacts
                 if not row.get('related_item_ids')
                 or not set(row.get('related_item_ids', [])).issubset(changed)]
    journey = result.get('journey') or {}
    if not isinstance(journey, dict):
        raise PlanError('局部修改的journey只能更新关联旅行材料')
    journey_violations = _field_violations(journey, {'artifacts'}, 'journey_fields')
    if journey_violations:
        _raise_scope_error(journey_violations, '局部修改的journey只能更新关联旅行材料')
    incoming = journey.get('artifacts', [])
    if not isinstance(incoming, list):
        raise PlanError('局部修改的旅行材料必须是列表')
    preserved_ids = {row.get('id') for row in preserved}
    for artifact in incoming:
        refs = artifact.get('related_item_ids', []) if isinstance(artifact, dict) else []
        if (not refs or not set(refs).issubset(changed)
                or isinstance(artifact, dict) and artifact.get('id') in preserved_ids):
            raise PlanError('模型旅行材料越过程序计算的影响范围')
    result['journey'] = {'artifacts': preserved + copy.deepcopy(incoming)}
    return result
