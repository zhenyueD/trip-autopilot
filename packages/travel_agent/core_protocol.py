"""Short, reversible supplier handles at the LLM boundary; public IDs stay stable."""
import copy
import json
import re


def normalize_delta_response(raw):
    """Canonicalize unambiguous field placement, never plan decisions or locks.

    Known top-level plan metadata belongs under delta.set. Conflicting copies
    are rejected; unknown operations remain present for the strict delta engine
    to reject. The original model response is not mutated.
    """
    from .journey_state import TOP_FIELDS
    from .validation import PlanError
    result, fixes = copy.deepcopy(raw), []
    delta = result.get('delta')
    if not isinstance(delta, dict):
        return result, fixes
    if ('journey' in delta and 'journey' not in result
            and isinstance(delta.get('journey'), dict)):
        result['journey'] = delta.pop('journey')
        fixes.append('delta.journey→journey')
    misplaced = set(delta).intersection(TOP_FIELDS)
    if misplaced:
        fields = delta.setdefault('set', {})
        if not isinstance(fields, dict) or misplaced.intersection(fields):
            raise PlanError('增量元信息存在重复或歧义，不能自动选择')
        for key in sorted(misplaced):
            fields[key] = delta.pop(key)
            fixes.append(f'delta.{key}→delta.set.{key}')
    decision_patches = delta.get('decision_patches')
    if isinstance(decision_patches, list):
        budget_rows = [row for row in decision_patches if isinstance(row, dict)
                       and row.get('category') == 'budget']
        fields = delta.get('set', {})
        if (len(budget_rows) == 1 and isinstance(fields, dict) and 'budget' not in fields):
            row = budget_rows[0]
            values = row.get('set')
            if (set(row) == {'category', 'set'} and isinstance(values, dict)
                    and set(values) <= {'choice', 'reason'}
                    and isinstance(values.get('choice'), str) and values['choice'].strip()
                    and (values.get('reason') is None or isinstance(values.get('reason'), str))):
                budget = values['choice'].strip()
                if isinstance(values.get('reason'), str) and values['reason'].strip():
                    budget += '；估算依据：' + values['reason'].strip()
                delta.setdefault('set', {})['budget'] = budget
                delta['decision_patches'] = [item for item in decision_patches if item is not row]
                fixes.append('delta.decision_patches[budget]→delta.set.budget')
    day_patches = delta.get('day_patches')
    if isinstance(day_patches, list):
        for index, patch in enumerate(day_patches):
            if not isinstance(patch, dict) or not isinstance(patch.get('upsert_items'), list):
                continue
            delete_ids = patch.get('delete_item_ids', [])
            if not isinstance(delete_ids, list):
                continue  # Preserve malformed data for the strict delta engine.
            remaining = []
            for operation in patch['upsert_items']:
                unambiguous = (isinstance(operation, dict)
                    and operation.get('delete') is True
                    and set(operation) <= {'id', 'delete', 'set', 'create'}
                    and isinstance(operation.get('id'), str)
                    and operation.get('set', {}) == {}
                    and operation.get('create', False) is False
                    and operation['id'] not in delete_ids)
                if not unambiguous:
                    remaining.append(operation)
                    continue
                delete_ids.append(operation['id'])
                fixes.append('delta.day_patches[' + str(index) + '].upsert_items['
                             + operation['id'] + '].delete→delete_item_ids')
            patch['upsert_items'] = remaining
            if delete_ids:
                patch['delete_item_ids'] = delete_ids
    return result, fixes


def display_supply_names(value, names, field=''):
    """Render known short handles in new prose, never in stable reference fields.

    Called on the model response only, before delta merge. Existing locked text
    is therefore never silently rewritten. Unknown handles remain visible for
    validation/debugging, and no supplier facts are invented.
    """
    if field == 'id' or field.endswith(('_id', '_ids')) or field in ('source', 'url', 'source_url'):
        return copy.deepcopy(value)
    if isinstance(value, dict):
        return {key: display_supply_names(item, names, key) for key, item in value.items()}
    if isinstance(value, list):
        return [display_supply_names(item, names, field) for item in value]
    if isinstance(value, str):
        return re.sub(r'(?<![A-Za-z0-9_])s\d{3}(?![A-Za-z0-9_])',
                      lambda match: names.get(match[0], match[0]), value)
    return value


def display_change(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        labels = {'change': '调整', 'reason': '依据', 'before': '调整前', 'after': '调整后',
                  'preserved': '保留'}
        return '；'.join(f"{labels.get(key, key)}：{item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)}"
                         for key, item in value.items())
    return json.dumps(value, ensure_ascii=False)


def encode_references(value, handles, path=()):
    if isinstance(value, list):
        return [encode_references(item, handles, path) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        supply_record = any(part in ('catalog', 'geography', 'source_evidence') for part in path)
        if (key == 'supply_id' or key == 'id' and supply_record) and isinstance(item, str):
            result[key] = handles.get(item, item)
        elif key in ('supply_ids', 'ids', 'unknown_location_ids') and isinstance(item, list):
            result[key] = [handles.get(id_, id_) if isinstance(id_, str) else id_ for id_ in item]
        else:
            result[key] = encode_references(item, handles, path + (key,))
    return result


def proposal_from_model(raw, handles, allowed_years=None, locked_item_ids=(), repair_log=None):
    # Never rewrite the itinerary's own IDs: those identify locked arrangements.
    plan = copy.deepcopy(raw)
    reverse = {handle: real for real, handle in handles.items()}
    for day in plan.get('days', []) if isinstance(plan.get('days'), list) else []:
        if not isinstance(day, dict):
            continue
        # An unknown optional calendar date is not an invalid travel day.
        # Normalize only absence; real/ambiguous date text stays strict.
        if 'date' in day and (day['date'] is None or
                             isinstance(day['date'], str) and not day['date'].strip()):
            day.pop('date')
        if 'date' in day and allowed_years is not None:
            if not allowed_years:
                # Month/day without a user-supplied year remains a relative day;
                # a model default must never turn it into a past/future booking date.
                day.pop('date')
            elif (not isinstance(day['date'], str) or
                  not re.fullmatch(r'(\d{4})-\d{2}-\d{2}', day['date']) or
                  int(day['date'][:4]) not in allowed_years):
                from .validation import PlanError
                raise PlanError('日程年份与用户明确提供的年份不一致，不能默认或沿用其他年份')
        # Models sometimes emit correct non-overlapping clock slots in narrative
        # order instead of chronological array order. Canonicalize only when
        # every start time is already valid; validation still rejects real
        # overlap, invalid clocks and impossible durations.
        items = day.get('items')
        if isinstance(items, list) and all(isinstance(item, dict) and
                isinstance(item.get('time'), str) and
                re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', item['time']) for item in items):
            items.sort(key=lambda item: tuple(map(int, item['time'].split(':'))))
            locked = set(locked_item_ids or ())
            for item in items:
                if (item.get('id') in locked or item.get('activity_kind') != 'transfer'
                        or item.get('end_time') != item.get('time')):
                    continue
                start = int(item['time'][:2]) * 60 + int(item['time'][3:])
                if start >= 1439:
                    continue
                item['end_time'] = f'{(start + 1) // 60:02}:{(start + 1) % 60:02}'
                if repair_log is not None:
                    repair_log.append({'kind': 'instant_transfer_marker', 'day': day.get('day'),
                                       'item_id': item.get('id'), 'from': item['time'] + '—' + item['time'],
                                       'to': item['time'] + '—' + item['end_time']})
            for index, item in enumerate(items[1:], 1):
                previous, following = items[index - 1], items[index + 1:index + 2]
                values = [previous.get('end_time'), item.get('time'), item.get('end_time')]
                if (item.get('id') in locked or item.get('activity_kind') not in ('meal', 'rest', 'transfer')
                        or not all(isinstance(value, str) and
                                   re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', value)
                                   for value in values)):
                    continue
                before_end, start, end = [int(value[:2]) * 60 + int(value[3:]) for value in values]
                duration, overlap = end - start, before_end - start
                shifted_end = before_end + duration
                next_start = (int(following[0]['time'][:2]) * 60 + int(following[0]['time'][3:])
                              if following and isinstance(following[0].get('time'), str)
                              and re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', following[0]['time']) else 1440)
                if not (0 < overlap <= 60 and duration > 0 and shifted_end < 1440 and shifted_end <= next_start):
                    continue
                old = f"{item['time']}—{item['end_time']}"
                item['time'] = f'{before_end // 60:02}:{before_end % 60:02}'
                item['end_time'] = f'{shifted_end // 60:02}:{shifted_end % 60:02}'
                if repair_log is not None:
                    repair_log.append({'day': day.get('day'), 'item_id': item.get('id'),
                                       'from': old, 'to': f"{item['time']}—{item['end_time']}"})
        for item in day.get('items', []) if isinstance(day.get('items'), list) else []:
            if isinstance(item, dict) and isinstance(item.get('supply_id'), str):
                item['supply_id'] = reverse.get(item['supply_id'], item['supply_id'])
            if isinstance(item, dict):
                # Display-only notes may be absent or structured. Preserve their
                # meaning without rejecting the whole itinerary. Dates, IDs,
                # titles, references and mandatory time slots remain strict.
                for key in ('why', 'transport', 'preparation'):
                    value = item.get(key)
                    if value is None:
                        item[key] = ''
                    elif isinstance(value, dict):
                        item[key] = display_change(value)
                    elif isinstance(value, list):
                        item[key] = '；'.join(display_change(entry) for entry in value)
    sections = [plan.get('signature')] + [x for key in ('decisions', 'preparation')
        for x in (plan.get(key, []) if isinstance(plan.get(key), list) else [])]
    for section in sections:
        if isinstance(section, dict) and isinstance(section.get('supply_ids'), list):
            section['supply_ids'] = [reverse.get(x, x) if isinstance(x, str) else x
                                     for x in section['supply_ids']]
    # An optional change summary may be richer than its display field. Preserve
    # all content as text, instead of discarding a complete journey for this.
    summary = plan.get('change_summary')
    if summary is not None:
        plan['change_summary'] = [display_change(x) for x in (summary if isinstance(summary, list) else [summary])]
    return plan
