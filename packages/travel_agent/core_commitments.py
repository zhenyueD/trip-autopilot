"""Model-owned time commitments, with small computable schedule checks.

No natural-language keyword rules and no programmatically inserted itinerary:
the direction model interprets the request; the planner chooses every slot.
"""
import copy

from .validation import PlanError, minutes, records, text


USER_SOURCE_KINDS = {'user_brief', 'user_text', 'user_feedback'}
ACTIVITY_KINDS = {'rest', 'meal', 'activity', 'personal'}
LODGING_KINDS = {'hotel', 'lodging', 'accommodation'}


def _source(value, materials):
    if not isinstance(value, dict):
        raise PlanError('每日要求缺少用户原话来源')
    material = materials.get(value.get('material_id'))
    quote = text(value.get('quote'), '要求原话', 600).strip()
    if not material or material.get('kind') not in USER_SOURCE_KINDS:
        raise PlanError('每日要求只能来自用户输入，不能来自网页或图片推测')
    if not quote or quote not in material.get('content', ''):
        raise PlanError('每日要求的引文不在用户原话中')
    return {'material_id': material['id'], 'quote': quote}


def _upsert(update, source):
    duration = update.get('min_duration_minutes')
    # JSON-compatible providers sometimes serialize integral numbers as text
    # or 120.0. Normalize representation, never infer missing time or units.
    if isinstance(duration, str) and duration.isascii() and duration.isdecimal():
        duration = int(duration)
    elif isinstance(duration, float) and duration.is_integer():
        duration = int(duration)
    if type(duration) is not int or not 1 <= duration <= 1440:
        raise PlanError(f'每日要求需要明确的最短分钟数（收到{type(duration).__name__}）')
    start, end = minutes(update.get('window_start')), minutes(update.get('window_end'))
    if end - start < duration:
        raise PlanError('每日要求的时间窗口不足，不能静默缩短时长')
    if update.get('activity_kind') not in ACTIVITY_KINDS:
        raise PlanError('每日要求的活动类型不正确')
    if update.get('location_kind') not in {'lodging', 'any'}:
        raise PlanError('每日要求的地点类型不正确')
    recurrence = update.get('recurrence', 'every_day')
    if recurrence not in {'every_day', 'once'}:
        raise PlanError('时间要求的重复类型不正确')
    day = update.get('day')
    if recurrence == 'once' and (type(day) is not int or not 1 <= day <= 14):
        raise PlanError('单次时间要求必须指定1—14之间的旅行日')
    result = {'id': update['id'], 'status': 'active', 'recurrence': recurrence,
            'label': text(update.get('label'), '每日要求', 160),
            'activity_kind': update['activity_kind'], 'location_kind': update['location_kind'],
            'min_duration_minutes': duration, 'window_start': update['window_start'],
            'window_end': update['window_end'], 'source': source}
    if recurrence == 'once':
        result['day'] = day
    return result


def merge_commitments(updates, previous, materials, current_source_ids):
    """Omission preserves a requirement; only source-bound updates can change it."""
    result = {x['id']: copy.deepcopy(x) for x in previous}
    sources, seen = {x['id']: x for x in materials}, set()
    for update in records(updates, '每日要求更新', 8):
        id_ = text(update.get('id'), '每日要求ID', 80)
        if not id_ or id_ in seen:
            raise PlanError('每日要求ID必须非空且不能重复更新')
        seen.add(id_)
        source = _source(update.get('source'), sources)
        action = update.get('action')
        candidate = _upsert(update, source) if action == 'upsert' else None
        if source['material_id'] not in current_source_ids:
            if candidate == result.get(id_):
                continue  # Tolerate the model repeating an unchanged old requirement.
            raise PlanError('要求的新增、更改或取消必须依据本轮用户输入，不能复活旧要求')
        if action == 'cancel' and id_ in result:
            result[id_] = {**result[id_], 'status': 'cancelled', 'source': source}
        elif action == 'upsert':
            result[id_] = candidate
        else:
            raise PlanError('要求更新必须是upsert或取消一个已有要求')
    if len(result) > 16:
        raise PlanError('每日要求过多，请合并同一要求的修改')
    return list(result.values())


def commitment_anchors(commitments, days):
    anchors = []
    for commitment in commitments:
        if commitment['status'] != 'active':
            continue
        target_days = ([commitment['day']] if commitment.get('recurrence') == 'once'
                       else range(1, days + 1))
        if any(day > days for day in target_days):
            raise PlanError('单次时间要求超出本次旅行天数')
        for day in target_days:
            conditions = []
            if day == 1:
                conditions += ['arrival']
                if commitment['location_kind'] == 'lodging':
                    conditions += ['check_in']
            if day == days:
                conditions += ['departure']
                if commitment['location_kind'] == 'lodging':
                    conditions += ['check_out']
            anchors.append({**copy.deepcopy(commitment), 'id': f"{commitment['id']}:d{day}",
                            'commitment_id': commitment['id'], 'day': day,
                            'required_conditions': conditions})
    return anchors


def _conditions(binding, anchor):
    conditions = []
    required = set(anchor['required_conditions'])
    value = binding.get('conditions', [])
    if isinstance(value, list):
        for item in value[:8]:
            if (isinstance(item, dict) and isinstance(item.get('kind'), str)
                    and item['kind'] in required):
                detail = item.get('detail')
                if isinstance(detail, str) and 0 < len(detail.strip()) <= 1200:
                    conditions.append({'kind': item['kind'], 'detail': detail.strip()})
    missing = required - {x['kind'] for x in conditions}
    issues = ['缺少首尾日可行条件：' + '、'.join(sorted(missing))] if missing else []
    return conditions, issues


def _slot_issues(anchor, day, item, raw_item, lodging_ids, supplies):
    issues = []
    if day != anchor['day']:
        issues.append('安排不在要求的当天')
    start, end = minutes(item['time']), minutes(item['end_time'])
    if end - start < anchor['min_duration_minutes']:
        issues.append(f"独立时段不足{anchor['min_duration_minutes']}分钟，交通/用餐不可凑时长")
    if start < minutes(anchor['window_start']) or end > minutes(anchor['window_end']):
        issues.append('安排不在用户要求的时间窗口内')
    if raw_item.get('activity_kind') != anchor['activity_kind']:
        issues.append('必须安排对应活动的独立时段，不与用餐或交通合并')
    if anchor['location_kind'] == 'lodging':
        supply = supplies.get(item.get('supply_id'), {})
        # A traveler may identify an already-booked hotel only by area. In
        # that honest case the lodging decision has no catalog IDs and the
        # bound slot must likewise stay unlinked. Once the planner selects a
        # concrete public lodging supply, the slot must use that exact ID.
        if ((lodging_ids and (supply.get('kind') not in LODGING_KINDS
                              or item.get('supply_id') not in lodging_ids))
                or (not lodging_ids and item.get('supply_id') is not None)):
            issues.append('需要引用住宿决定中选定的真实酒店，不是景点/空地点')
    return issues


def commitment_checks(raw, plan, anchors):
    """Compare real schedule slots, never a model-written satisfied boolean."""
    bindings = raw.get('commitment_schedule', [])
    bindings = bindings if isinstance(bindings, list) else []
    slots = {item['id']: (day['day'], item) for day in plan['days'] for item in day['items']}
    raw_items = {item['id']: item for day in raw['days'] for item in day['items']}
    supplies = {s['id']: s for s in plan['supplies']}
    lodging_ids = {id_ for d in plan['decisions'] if d['category'] == 'lodging' for id_ in d['supply_ids']}
    checks, used = [], set()
    for anchor in anchors:
        matches = [x for x in bindings if isinstance(x, dict) and x.get('anchor_id') == anchor['id']]
        binding = matches[0] if len(matches) == 1 else {}
        item_id = binding.get('item_id')
        slot = slots.get(item_id) if isinstance(item_id, str) else None
        conditions, issues = _conditions(binding, anchor)
        evidence = {}
        if not slot:
            issues.append('未找到当天独立安排的有效item_id')
        else:
            day, item = slot
            issues += _slot_issues(anchor, day, item, raw_items[item_id], lodging_ids, supplies)
            if item_id in used:
                issues.append('同一安排不能重复充当独立时段')
            if not issues:
                used.add(item_id)
            evidence = {key: item[key] for key in ('id', 'time', 'end_time', 'supply_id')}
            evidence['item_id'] = evidence.pop('id')
        checks.append({'anchor_id': anchor['id'], 'commitment_id': anchor['commitment_id'],
                       'day': anchor['day'], 'label': anchor['label'], 'source': anchor['source'],
                       'status': 'unmet' if issues else 'conditional' if conditions else 'scheduled',
                       'conditions': conditions, 'issues': issues, **evidence})
    return checks


def attach_commitments(plan, commitments, checks):
    plan['user_commitments'] = copy.deepcopy(commitments)
    plan['commitment_checks'] = checks
    unmet = [x for x in checks if x['status'] == 'unmet']
    conditional = [x for x in checks if x['status'] == 'conditional']
    if not unmet and not conditional:
        return
    notes = [f"第{x['day']}天「{x['label']}」未落实：{'；'.join(x['issues'])}" for x in unmet]
    notes += [f"第{x['day']}天「{x['label']}」有条件安排："
              + '；'.join(c['detail'] for c in x['conditions']) for x in conditional]
    # Preserve the actual itinerary and expose exceptions instead of inserting
    # slots, inventing availability, or retaining a blanket success claim.
    plan['summary'] = ('部分每日要求尚未落实，具体见每天安排。' if unmet else
                       plan['summary'] + ' 首尾日的每日要求为条件安排，到离时间及使用条件仍待确认。')
    plan['change_summary'] = notes
    plan['readiness']['label'] = '部分用户要求未落实' if unmet else '方案已形成，首尾日安排有条件'
    plan['readiness']['warnings'] += notes
    plan['readiness']['blocking_count'] += len(unmet) + len(conditional)


def attach_signature_schedule(raw, plan):
    """Expose the actual signature slots, not another invented time summary."""
    item_ids = raw.get('signature', {}).get('item_ids')
    if item_ids is None:  # Backward-compatible with existing plans/callers.
        return
    if not isinstance(item_ids, list) or not item_ids or len(item_ids) > 12:
        raise PlanError('主打体验需引用实际行程item_ids')
    slots = {x['id']: {'day': day['day'], **x} for day in plan['days'] for x in day['items']}
    if any(not isinstance(id_, str) or id_ not in slots for id_ in item_ids):
        invalid = [str(id_) for id_ in item_ids if not isinstance(id_, str) or id_ not in slots]
        raise PlanError('主打体验引用了不存在的行程item_id：' + '、'.join(invalid)
                        + '。合并后可用的真实安排ID：' + '、'.join(slots)
                        + '。请仅更新signature.item_ids及对应sequence，不恢复已删除的安排。')
    plan['signature']['item_ids'] = item_ids
    plan['signature']['schedule'] = [{k: slots[id_][k] for k in
        ('id', 'day', 'time', 'end_time', 'title', 'supply_id')} for id_ in dict.fromkeys(item_ids)]
