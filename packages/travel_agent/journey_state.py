"""Schema-aware, pure plan deltas and evidence-scoped journey bookkeeping.

The caller must decode supplier references and validate the whole merged plan
before committing it. Patching does not establish reality, availability, booking,
or readiness. Untouched data is deep-copied without normalization or regeneration.
"""
from __future__ import annotations

import copy

from .validation import (PlanError, minutes, records, strings, text,
                         validate_action_guide, validate_day_details, validate_experience)


TOP_FIELDS = {'title', 'summary', 'signature', 'questions', 'change_summary',
              'understanding', 'reactions', 'commitment_schedule', 'budget'}
DAY_FIELDS = {'title', 'rationale', 'date', 'city', 'lodging'}
ITEM_FIELDS = {'time', 'end_time', 'title', 'why', 'transport', 'preparation',
               'supply_id', 'activity_kind', 'experience', 'action_guide'}
SIGNATURE_FIELDS = {'title', 'why', 'tradeoff', 'sequence', 'supply_ids', 'item_ids'}
DECISION_FIELDS = {'choice', 'reason', 'status', 'supply_ids'}
PREPARATION_FIELDS = {'category', 'task', 'why', 'status', 'supply_ids', 'when', 'steps', 'materials'}
DELTA_FIELDS = {'set', 'day_patches', 'move_items', 'decision_patches',
                'preparation_patches', 'day_changes'}
USER_SOURCE_KINDS = {'user_brief', 'user_text', 'user_feedback'}

# This contract is consumed by the model prompt as well as by the implementation.
# Examples describe shapes; absent keys preserve the previous value exactly.
DELTA_CONTRACT = {
    'set': {'allowed_fields': sorted(TOP_FIELDS),
            'signature_partial_fields': sorted(SIGNATURE_FIELDS),
            'field_shapes': {
                'title': 'string', 'summary': 'string', 'budget': '可选预算说明：人数、币种、范围与估算口径，string',
                'questions': ['待补充的一个问题，string'],
                'change_summary': ['真实变动的一条说明，string'],
                'understanding': [{'text': '当前理解，string', 'source': '依据，string'}],
                'reactions': [{'label': '短按钮文字，string', 'feedback': '完整调整要求，string'}],
                'signature': {'title': 'string', 'why': 'string', 'tradeoff': 'string',
                    'sequence': ['具体环节，string'], 'supply_ids': ['供给ID'], 'item_ids': ['安排ID']},
                'commitment_schedule': [{'anchor_id': '逐字复制commitment_anchors.id',
                    'item_id': '当天落实要求的实际日程ID', 'conditions': [
                        {'kind': 'arrival|check_in|departure|check_out|other',
                         'detail': '具体成立条件、备选及未确认状态，string'}]}]},
            'rule': '未提供的字段保持原值；signature按字段合并，其他数组只在受影响时替换'},
    'day_patches': [{'day': 1, 'set': {'title': '可选的新标题', 'rationale': '可选的新理由',
                     'date': '可选：仅已知出行日期时填写YYYY-MM-DD', 'city': '可选：当天城市，string',
                     'lodging': '可选：明确住宿建议及未落实条件，string'},
                     'upsert_items': [{'id': '已有ID', 'set': {'time': '09:00'}},
                         {'id': '全新ID', 'create': True, 'set': {
                             'title': '新增安排', 'time': '12:00', 'end_time': '13:00'}}],
                     'delete_item_ids': ['明确删除的已有ID']}],
    'item_set_fields': sorted(ITEM_FIELDS),
    'experience_shape': {'background': '背景，string', 'what_to_do': ['具体玩法'],
                         'personal_fit': '与人的关联，string', 'photo_tip': '取景提示，string',
                         'practical_tip': '实际准备提示，string'},
    'action_guide_shape': {'meeting_point': '具体入口或集合地点；未知则明确缺口，string',
                          'steps': ['已选方案的执行步骤，最多8条'],
                          'bring': ['随身携带物品，最多12条'],
                          'booking_note': '预约条件与真实未确认状态，string'},
    'move_items': [{'id': '已有ID', 'from_day': 1, 'to_day': 2,
                    'set': {'time': '14:00', 'end_time': '15:00'}}],
    'decision_patches': [{'category': 'transport', 'set': {
        'choice': '新的选择', 'reason': '新的依据', 'status': 'needs_check', 'supply_ids': []}}],
    'preparation_patches': [{'category': 'transport', 'items': [
        {'task': '新的准备事项', 'why': '原因', 'status': 'needs_check', 'supply_ids': [],
         'when': '可选：何时或什么条件下做', 'steps': ['可选：具体操作，最多8条'],
         'materials': ['可选：所需材料，最多12条']}]}],
    'day_changes': {'add': [{'day': 3, 'title': '新增一天', 'rationale': '用户改变天数',
                            'date': '可选：YYYY-MM-DD', 'city': '可选：城市', 'lodging': '可选：住宿建议'}],
                    'remove': [4]},
    'rules': [
        '只返回需要执行的操作；禁止整份days、decisions或preparation替换和任意JSON路径。',
        'title/summary/signature/questions/change_summary/understanding/reactions/commitment_schedule/budget放入delta.set，不是delta根字段。',
        'allowed_fields/field_shapes/rules/item_set_fields只是接口说明，不是可返回的操作字段。',
        '稳定安排ID不可改名；已有ID更新，新ID必须create:true并提供title/time/end_time。',
        '同一ID每轮只能一次upsert、delete或move；移动必须使用move_items，可同时set受影响字段。',
        '锁定安排不可改任何字段、移动或删除。未触及的天、安排和决定必须保持原样。',
        'action_guide可选；修改时提供完整对象，未修改时省略以保留。准备的when/steps/materials可选。',
        'decision_patches只能修改已有category；preparation_patches只替换指定category。',
        '新增天仅添加空日框架，再用create:true添加安排；删除天之前逐项明确删除或移动全部安排。',
        '最终天数连续从1开始。供给引用和全计划仍需系统校验；不能自行修改journey/readiness/supplies。',
    ],
}


def _object(value, allowed, label, required=()):
    if not isinstance(value, dict):
        raise PlanError(f'{label}必须是对象')
    unknown = set(value) - allowed
    if unknown:
        raise PlanError(f'{label}存在不允许的字段：' + '、'.join(sorted(map(str, unknown))))
    if set(required) - set(value):
        raise PlanError(f'{label}缺少必须字段：' + '、'.join(sorted(set(required) - set(value))))
    return value


def _identifier(value, label):
    value = text(value, label, 80)
    if not value.strip():
        raise PlanError(f'{label}不能为空')
    return value


def _day_number(value):
    if type(value) is not int or not 1 <= value <= 14:
        raise PlanError('局部修改的day必须是1—14的整数')
    return value


def _indexes(plan):
    days, items = {}, {}
    for day in records(plan.get('days'), '原方案天数', 14):
        number = _day_number(day.get('day'))
        if number in days:
            raise PlanError('原方案的day重复，不能确定局部修改目标')
        days[number] = day
        for item in records(day.get('items'), '原方案安排', 10):
            id_ = _identifier(item.get('id'), '安排ID')
            if id_ in items:
                raise PlanError('原方案安排ID重复，不能确定局部修改目标')
            items[id_] = (number, item)
    return days, items


def _decision_index(plan):
    result = {}
    for item in records(plan.get('decisions'), '原方案决定', 24):
        category = _identifier(item.get('category'), '决定类别')
        if category in result:
            raise PlanError('原方案决定类别重复，不能确定局部修改目标')
        result[category] = item
    return result


def _claim(id_, seen):
    if id_ in seen:
        raise PlanError(f'同一目标存在重复或冲突的修改：{id_}')
    seen.add(id_)


def _existing_item(id_, day, items):
    entry = items.get(id_)
    if entry is None:
        raise PlanError(f'不存在的安排ID：{id_}；新增需显式create:true')
    if entry[0] != day:
        raise PlanError(f'安排{id_}不在第{day}天；跨天修改必须用move_items')
    return entry[1]


def _item_fields(value, creating=False):
    required = {'title', 'time', 'end_time'} if creating else ()
    fields = _object(value, ITEM_FIELDS, '安排set', required)
    for key, value in fields.items():
        if key == 'supply_id' and value is None:
            continue
        if key == 'experience':
            validate_experience(value)
            continue
        if key == 'action_guide':
            validate_action_guide(value)
            continue
        text(value, f'安排{key}')
        if key in ('time', 'end_time'):
            minutes(value)
    return copy.deepcopy(fields)


def _metadata(value):
    fields = _object(value, DAY_FIELDS, '每日set')
    return {**{key: text(fields[key], f'每日{key}') for key in ('title', 'rationale') if key in fields},
            **validate_day_details(fields)}


def _add_days(change, days):
    change = _object(change, {'add', 'remove'}, 'day_changes')
    for day in records(change.get('add', []), '新增天数', 14):
        _object(day, {'day'} | DAY_FIELDS, '新增天', {'day', 'title'})
        number = _day_number(day['day'])
        if number in days:
            raise PlanError(f'第{number}天已经存在，不能重复新增')
        days[number] = {'day': number, 'rationale': '', 'items': [],
                        **_metadata({key: value for key, value in day.items() if key != 'day'})}
    remove = change.get('remove', [])
    if not isinstance(remove, list) or len(remove) > 14:
        raise PlanError('删除天数必须是整数列表')
    seen = set()
    for number in remove:
        _day_number(number)
        _claim(number, seen)
        if number not in days or any(x['day'] == number for x in change.get('add', [])):
            raise PlanError(f'不能删除不存在或本轮同时新增的第{number}天')
    return remove


def _upsert_items(patch, day, original_items, claimed):
    for operation in records(patch.get('upsert_items', []), '安排增量', 30):
        _object(operation, {'id', 'set', 'create'}, '安排upsert', {'id', 'set'})
        id_ = _identifier(operation['id'], '安排ID')
        _claim(id_, claimed)
        creating = operation.get('create', False)
        if type(creating) is not bool:
            raise PlanError('create必须是布尔值')
        fields = _item_fields(operation['set'], creating)
        if creating:
            if id_ in original_items:
                raise PlanError(f'新增安排ID已经存在：{id_}')
            day['items'].append({'id': id_, **fields})
        else:
            _existing_item(id_, day['day'], original_items).update(fields)
    for id_ in strings(patch.get('delete_item_ids', []), '删除安排ID', 30):
        _identifier(id_, '删除安排ID')
        _claim(id_, claimed)
        item = _existing_item(id_, day['day'], original_items)
        day['items'].remove(item)


def _patch_days(delta, days, original_items, claimed):
    seen, touched = set(), set()
    for patch in records(delta.get('day_patches', []), '每日局部修改', 14):
        _object(patch, {'day', 'set', 'upsert_items', 'delete_item_ids'}, '每日修改', {'day'})
        number = _day_number(patch['day'])
        _claim(number, seen)
        if number not in days:
            raise PlanError(f'不存在的修改目标：第{number}天')
        day = days[number]
        day.update(_metadata(patch.get('set', {})))
        _upsert_items(patch, day, original_items, claimed)
        if patch.get('upsert_items') or patch.get('delete_item_ids'):
            touched.add(number)
    return touched


def _move_items(delta, days, original_items, claimed, touched):
    for operation in records(delta.get('move_items', []), '跨天移动', 100):
        _object(operation, {'id', 'from_day', 'to_day', 'set'}, '跨天移动',
                {'id', 'from_day', 'to_day'})
        id_ = _identifier(operation['id'], '移动安排ID')
        _claim(id_, claimed)
        source, target = _day_number(operation['from_day']), _day_number(operation['to_day'])
        if source == target or source not in days or target not in days:
            raise PlanError('移动需要两个不同且实际存在的day')
        item = _existing_item(id_, source, original_items)
        item.update(_item_fields(operation.get('set', {})))
        days[source]['items'].remove(item)
        days[target]['items'].append(item)
        touched.update((source, target))


def _patch_decisions(delta, decisions):
    seen = set()
    for patch in records(delta.get('decision_patches', []), '决定局部修改', 24):
        _object(patch, {'category', 'set'}, '决定修改', {'category', 'set'})
        category = _identifier(patch['category'], '决定类别')
        _claim(category, seen)
        if category not in decisions:
            raise PlanError(f'不存在的决定类别：{category}')
        fields = _object(patch['set'], DECISION_FIELDS, '决定set')
        decisions[category].update(copy.deepcopy(fields))


def _replace_category(previous, category, replacement):
    result, inserted = [], False
    for item in previous:
        if item.get('category') != category:
            result.append(item)
        elif not inserted:
            result.extend(replacement)
            inserted = True
    if not inserted:
        result.extend(replacement)
    return result


def _patch_preparation(delta, plan, decisions):
    seen = set()
    previous = records(plan.get('preparation'), '原方案准备事项', 30)
    known = set(decisions) | {x.get('category') for x in previous}
    for patch in records(delta.get('preparation_patches', []), '准备局部修改', 30):
        _object(patch, {'category', 'items'}, '准备修改', {'category', 'items'})
        category = _identifier(patch['category'], '准备类别')
        _claim(category, seen)
        if category not in known:
            raise PlanError(f'不存在的准备类别：{category}')
        replacement = []
        for item in records(patch['items'], '准备替换内容', 30):
            _object(item, PREPARATION_FIELDS, '准备事项', {'task', 'why', 'status'})
            if item.get('category', category) != category:
                raise PlanError('准备事项类别与局部修改目标不一致')
            replacement.append({**copy.deepcopy(item), 'category': category})
        plan['preparation'] = _replace_category(plan['preparation'], category, replacement)


def _patch_top(delta, plan):
    fields = _object(delta.get('set', {}), TOP_FIELDS, '方案set')
    for key, value in fields.items():
        if key == 'signature':
            _object(value, SIGNATURE_FIELDS, '主打体验set')
            plan[key] = {**plan.get(key, {}), **copy.deepcopy(value)}
        else:
            plan[key] = copy.deepcopy(value)


def _check_locks(locks, original_items, new_items, original_days, new_days):
    if not isinstance(locks, (list, tuple)):
        raise PlanError('锁定安排必须是ID列表')
    for id_ in locks:
        _identifier(id_, '锁定安排ID')
        if id_ not in original_items or id_ not in new_items:
            raise PlanError(f'锁定安排缺失：{id_}；必须保留或请求解除锁定')
        if original_items[id_] != new_items[id_]:
            raise PlanError(f'修改触及锁定安排{id_}；任何字段、跨天移动和删除均需先解除锁定')
        day = original_items[id_][0]
        if original_days[day].get('date') != new_days[day].get('date'):
            raise PlanError(f'修改触及锁定安排{id_}的日期；需先解除锁定')


def apply_plan_delta(previous, delta, locks=()):
    """Merge a strict local delta without modifying either input.

    This intentionally returns a *raw* plan. Supply identity, chronology, expected
    day count, semantic feedback response and commitments must be revalidated by
    the caller before committing. Never retry errors as full-plan regeneration.
    """
    if not isinstance(previous, dict):
        raise PlanError('局部修改需要已有方案')
    _object(delta, DELTA_FIELDS, '方案增量')
    old_days, old_items = _indexes(previous)
    plan = copy.deepcopy(previous)
    days, items = _indexes(plan)
    decisions, claimed = _decision_index(plan), set()
    remove = _add_days(delta.get('day_changes', {}), days)
    touched = _patch_days(delta, days, items, claimed)
    _move_items(delta, days, items, claimed, touched)
    for number in remove:
        if days[number]['items']:
            raise PlanError('删除天之前必须逐项明确删除或移动该天全部安排')
        del days[number]
    if not days or sorted(days) != list(range(1, len(days) + 1)):
        raise PlanError('增量合并后的天数必须连续，从1开始且至少保留一天')
    for number in touched - set(remove):
        days[number]['items'].sort(key=lambda item: minutes(item.get('time')))
    plan['days'] = [days[number] for number in sorted(days)]
    _patch_decisions(delta, decisions)
    _patch_preparation(delta, plan, decisions)
    _patch_top(delta, plan)
    _, new_items = _indexes(plan)
    _check_locks(locks, old_items, new_items, old_days, days)
    return plan


def plan_changes(previous, plan, reason=''):
    """Return the actual structural diff, including removed IDs and day moves."""
    _, before = _indexes(previous) if previous else ({}, {})
    _, after = _indexes(plan)
    ids = list(dict.fromkeys([*before, *after]))
    old_decisions = _decision_index(previous) if previous else {}
    new_decisions = _decision_index(plan)
    categories = list(dict.fromkeys([*old_decisions, *new_decisions]))
    return {
        'changed_item_ids': [id_ for id_ in ids if before.get(id_) != after.get(id_)],
        'preserved_item_ids': [id_ for id_ in ids if id_ in before and before[id_] == after.get(id_)],
        'changed_decision_ids': [category for category in categories
                                 if old_decisions.get(category) != new_decisions.get(category)],
        'reason': text(reason, '修改原因', 5000),
    }


def scoped_learning(previous, feedback, sources, assumptions=()):
    """Retain local corrections with exact user-source references, not profiles.

    Sources use the persisted material ledger's id/kind/content schema. A model,
    photograph or web page cannot establish a user preference. Missing provenance
    remains unverified; assumptions are always hypotheses scoped to this trip.
    """
    result = copy.deepcopy(records(previous, '已有旅行学习', 200))
    feedback = text(feedback, '反馈学习', 5000)
    if not isinstance(sources, (list, tuple)) or not isinstance(assumptions, (list, tuple)):
        raise PlanError('旅行学习来源和假设必须是列表')
    if feedback.strip():
        ids = list(dict.fromkeys(source['id'] for source in sources
            if isinstance(source, dict) and isinstance(source.get('id'), str)
            and source['id'].strip()
            and source.get('kind') in USER_SOURCE_KINDS
            and isinstance(source.get('content'), str) and feedback in source['content']))
        entry = {'text': feedback, 'scope': 'this_trip', 'source': 'user_feedback',
                 'source_ids': ids, 'status': 'stated' if ids else 'unverified'}
        if entry not in result:
            result.append(entry)
    for assumption in assumptions:
        entry = {'text': text(assumption, '偏好假设'), 'scope': 'this_trip', 'source': 'model',
                 'source_ids': [], 'status': 'hypothesis'}
        if entry['text'].strip() and entry not in result:
            result.append(entry)
    return result
