"""Phase 1: explicit relative time shifts, computed without model edits.

This intentionally implements a small grammar, not general language inference.
The returned delta is a scope-check projection only: use ``plan`` as the result
so preparation evidence fields outside the legacy delta schema remain intact.
"""
from __future__ import annotations

import copy
import re
from fractions import Fraction

from .core_commitments import commitment_anchors, commitment_checks
from .journey_state import PREPARATION_FIELDS
from .scoped_replan import _target_days, validate_delta_scope, validate_scoped_result
from .validation import PlanError, _validate_locks, minutes, text


_DIRECTION = r'晚点|早点|晚些|早些|提前|推迟|延后|延迟|顺延|往后|往前|后移|前移|晚|早'
_NUMBER = r'\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百]+|半'
_SHIFT = re.compile(rf'(?P<direction>{_DIRECTION})\s*(?P<number>{_NUMBER})'
                    r'\s*个?\s*(?P<half>半)?\s*(?P<unit>小时|分钟)')
_DETECT = re.compile(r'晚点|早点|晚些|早些|提前|推迟|延后|延迟|顺延|往后|往前|后移|前移'
                     rf'|[晚早]\s*(?:{_NUMBER})|时间改|改到|调整时间|时间调整')
_DAY = re.compile(r'第\s*[\d零〇一二两三四五六七八九十百]+\s*天')
_PERIODS = {'上午': (0, 720), '早上': (0, 720), '下午': (720, 1080),
            '中午': (720, 840), '晚上': (1080, 1440)}
_WORDS = re.compile(r'请|帮我|把|将|让|从|开始|起|的|出发时间|出发|交通|活动|行程|安排|时间'
                    r'|整体|全部|统一|都|往|向')


def _number(raw):
    if raw == '半':
        return Fraction(1, 2)
    if re.fullmatch(r'\d+(?:\.\d+)?', raw):
        if not re.fullmatch(r'[0-9]{1,4}(?:\.[0-9]{1,2})?', raw):
            raise PlanError('时间数量最多支持4位整数和2位小数，请明确写出整分钟数')
        return Fraction(raw)
    digits = dict(zip('零〇一二两三四五六七八九', (0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9)))
    if raw in digits:
        return Fraction(digits[raw])
    # Standard Chinese integers up to 199; other spellings need clarification.
    if raw == '一百':
        return Fraction(100)
    if re.fullmatch(r'一百零[一二三四五六七八九]', raw):
        return Fraction(100 + digits[raw[-1]])
    hundred = raw.startswith('一百')
    remainder = raw[2:] if hundred else raw
    match = re.fullmatch(r'([一二两三四五六七八九]?十)([一二三四五六七八九])?', remainder)
    if not match:
        raise PlanError('暂不支持这个时间数量，请明确写出小时数或分钟数')
    tens, ones = match.groups()
    value = (100 if hundred else 0) + digits.get(tens[:-1], 1) * 10
    return Fraction(value + (digits[ones] if ones else 0))


def _parse_request(plan, feedback):
    matches = list(_SHIFT.finditer(feedback))
    raw_days = list(_DAY.finditer(feedback))
    days = _target_days(feedback)
    if len(matches) != 1 or len(raw_days) != 1 or len(days) != 1:
        raise PlanError('时间平移需要单一旅行日和明确的提前或延后分钟数；复合要求请分别调整')
    match = matches[0]
    if match['number'] == '半' and match['half']:
        raise PlanError('时间数量不明确，请明确写出小时数或分钟数')
    amount = _number(match['number']) + (Fraction(1, 2) if match['half'] else 0)
    amount *= 60 if match['unit'] == '小时' else 1
    if amount != int(amount) or not 0 < amount < 1440:
        raise PlanError('时间平移需要大于0且小于一天的整分钟数')
    shift = int(amount) * (-1 if match['direction'] in
                          ('早点', '早些', '提前', '往前', '前移', '早') else 1)
    remainder = feedback[:match.start()] + feedback[match.end():]
    remainder = _DAY.sub('', remainder)
    periods = [word for word in _PERIODS if word in remainder]
    if len(periods) > 1:
        raise PlanError('时间平移的目标时段不明确，请一次指定一个时段')
    period = periods[0] if periods else None
    if period:
        remainder = remainder.replace(period, '')
    ids = [item['id'] for day in plan.get('days', []) for item in day.get('items', [])]
    explicit = [id_ for id_ in ids if re.search(r'(?<![\w-])' + re.escape(id_) + r'(?![\w-])',
                                               remainder, flags=re.ASCII)]
    if len(explicit) > 1:
        raise PlanError('时间平移一次只支持一个起点安排')
    if explicit:
        remainder = remainder.replace(explicit[0], '')
    whole_day = any(word in remainder for word in ('整体', '全部', '统一', '都'))
    remainder = _WORDS.sub('', remainder)
    if re.sub(r'[\s，,。.!！：:；;]', '', remainder):
        raise PlanError('暂不支持这类时间平移表达；请只指定某一天、时段或安排ID及提前/延后时长')
    return days[0], period, explicit[0] if explicit else None, whole_day, shift


def _select_tail(plan, impact, day_number, period, explicit_id, whole_day):
    days = [day for day in plan.get('days', []) if day.get('day') == day_number]
    if len(days) != 1 or day_number not in impact.get('day_numbers', []):
        raise PlanError('时间平移目标不在程序计算的当天影响范围内')
    items = days[0]['items']
    allowed = set(impact.get('item_ids', []))
    original = allowed - set(impact.get('time_cascade_item_ids', []))
    candidates = [i for i, item in enumerate(items)
                  if item.get('id') == explicit_id] if explicit_id else [
                      i for i, item in enumerate(items) if item.get('id') in original]
    if period:
        start, end = _PERIODS[period]
        candidates = [i for i in candidates if start <= minutes(items[i].get('time')) < end]
    if not candidates:
        raise PlanError('指定时段或安排没有明确的受影响起点；请明确安排ID')
    if not period and not explicit_id and not whole_day and len(candidates) != 1:
        raise PlanError('当天有多个可调整起点，请指定时段或安排ID')
    tail = items[candidates[0]:]
    if any(item.get('id') not in allowed for item in tail):
        raise PlanError('时间级联超出程序计算的影响范围，无法安全平移')
    return days[0], tail


def _clock(value):
    if not 0 <= value < 1440:
        raise PlanError('时间平移将跨午夜；请明确跨天安排后再调整')
    return f'{value // 60:02}:{value % 60:02}'


def _shift_items(day, tail, shift, locked_item_ids):
    changes, operations = [], []
    for item in tail:
        if item['id'] in locked_item_ids:
            raise PlanError(f"时间平移触及锁定安排 {item['id']}，需要先解除锁定")
        old_time, old_end = item.get('time'), item.get('end_time')
        new_time, new_end = _clock(minutes(old_time) + shift), _clock(minutes(old_end) + shift)
        changes.append({'day': day['day'], 'item_id': item['id'], 'old_time': old_time,
                        'old_end_time': old_end, 'new_time': new_time, 'new_end_time': new_end})
        item.update(time=new_time, end_time=new_end)
        operations.append({'id': item['id'], 'set': {'time': new_time, 'end_time': new_end}})
    previous_end = 0
    for item in day['items']:
        start, end = minutes(item.get('time')), minutes(item.get('end_time'))
        if start < previous_end or end <= start:
            raise PlanError(f"平移后第{day['day']}天的安排时间重叠或先后错误，无法直接调整")
        previous_end = end
    return changes, operations


def _preparation_patches(plan, impact, changes, summary):
    required = set(impact.get('required_preparation_categories', []))
    categories = required | ({'transport'} & set(impact.get('replaceable_preparation_categories', [])))
    allowed_ids = set(impact.get('item_ids', []))
    first = changes[0]
    note = f"时间调整：{summary}；起点由{first['old_time']}改为{first['new_time']}。"
    patches = []
    for category in sorted(categories):
        rows = [row for row in plan.get('preparation', []) if row.get('category') == category]
        updated = False
        for row in rows:
            related = set(row.get('related_item_ids') or [])
            if related and not related.issubset(allowed_ids):
                continue
            previous = text(row.get('when', ''), '关联准备时间')
            row['when'] = text(previous + ('；' if previous else '') + note, '关联准备时间')
            updated = True
        if not updated and category in required:
            raise PlanError(f'关联准备类别{category}没有可安全更新的事项，请明确对应安排')
        if updated:
            patches.append({'category': category, 'items': [
                {key: copy.deepcopy(value) for key, value in row.items() if key in PREPARATION_FIELDS}
                for row in rows]})
    return patches


def _check_commitments(previous, plan, changes):
    anchors = previous.get('commitment_anchors')
    if anchors is None:
        anchors = commitment_anchors(previous.get('user_commitments', []), len(plan['days']))
    if not anchors:
        return
    bindings = previous.get('commitment_schedule')
    if bindings is None:
        bindings = [{key: row[key] for key in ('anchor_id', 'item_id', 'conditions') if key in row}
                    for row in previous.get('commitment_checks', [])]
    affected_days = {change['day'] for change in changes}
    affected = [anchor for anchor in anchors if anchor['day'] in affected_days]
    if not affected:
        return
    raw = {**plan, 'commitment_schedule': bindings}
    validation_plan = {**plan, 'supplies': plan.get('supplies', []),
                       'decisions': plan.get('decisions', [])}
    checks = commitment_checks(raw, validation_plan, affected)
    failures = [check for check in checks if check['status'] == 'unmet']
    if failures:
        raise PlanError('时间平移冲突于硬时间要求：' + '；'.join(
            f"{check['label']}（{'、'.join(check['issues'])}）" for check in failures))
    changed_ids = {change['item_id'] for change in changes}
    refreshed = {check['anchor_id']: check for check in checks if check.get('item_id') in changed_ids}
    if not refreshed:
        return
    stored = plan.setdefault('commitment_checks', [])
    for row in stored:
        update = refreshed.pop(row.get('anchor_id'), None)
        if update is not None:
            row.update({key: copy.deepcopy(value) for key, value in update.items()
                        if key not in ('source', 'conditions') or key not in row})
    stored.extend(copy.deepcopy(list(refreshed.values())))


def plan_time_shift(plan, impact, feedback, locked_item_ids=()):
    """Return a pure deterministic shift, None for other intent, or PlanError.

    Supported: one day, one explicit relative offset, optionally a period or
    existing item ID. The tail of that day moves by the same number of minutes.
    No absolute time setting, compound edits, model inference or external calls.
    """
    if not isinstance(feedback, str) or not _DETECT.search(feedback):
        return None
    unsafe_preparation = (set(impact.get('preparation_categories', []))
                          - set(impact.get('replaceable_preparation_categories', [])))
    if unsafe_preparation:
        raise PlanError('关联准备类别含有范围外事项，无法安全平移：' + '、'.join(sorted(unsafe_preparation)))
    if not isinstance(locked_item_ids, (list, tuple)):
        raise PlanError('锁定安排必须是ID列表')
    request = _parse_request(plan, feedback)
    result = copy.deepcopy(plan)
    day, tail = _select_tail(result, impact, *request[:4])
    shift = request[4]
    changes, operations = _shift_items(day, tail, shift, set(locked_item_ids))
    direction = '延后' if shift > 0 else '提前'
    summary = f"第{day['day']}天从{changes[0]['old_time']}起的{len(changes)}项安排统一{direction}{abs(shift)}分钟，时长保持不变"
    delta = {'day_patches': [{'day': day['day'], 'upsert_items': operations}],
             'preparation_patches': _preparation_patches(result, impact, changes, summary)}
    validate_delta_scope(delta, impact)
    validate_scoped_result(plan, result, impact)
    _validate_locks(result, plan, locked_item_ids)
    _check_commitments(plan, result, changes)
    return {'plan': result, 'delta': delta, 'changes': changes,
            'summary': summary, 'shift_minutes': shift}
