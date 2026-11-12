"""Validate proposed arrangements, not subjective taste. Reality stays in tool evidence."""
from __future__ import annotations

import copy
from datetime import date
import re
from urllib.parse import urlparse


class PlanError(Exception):
    pass


def text(value, label, limit=1200):
    if not isinstance(value, str) or len(value) > limit:
        raise PlanError(f'{label}必须是长度适当的文字')
    return value


def records(value, label, limit=100):
    if not isinstance(value, list) or len(value) > limit or any(not isinstance(x, dict) for x in value):
        raise PlanError(f'{label}必须是对象列表')
    return value


def strings(value, label, limit=30):
    if not isinstance(value, list) or len(value) > limit:
        raise PlanError(f'{label}必须是文字列表')
    return [text(x, label) for x in value]


def minutes(value):
    if not isinstance(value, str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', value):
        raise PlanError('安排时间必须为 HH:MM')
    h, m = map(int, value.split(':'))
    return h * 60 + m


def safe_url(value):
    if not isinstance(value, str):
        return None
    parsed = urlparse(value)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        return None
    return value


def _supply_refs(ids, catalog, used):
    ids = strings(ids, '供给引用')
    for id_ in ids:
        if id_ not in catalog:
            raise PlanError(f'不存在的供给引用：{id_}')
        if id_ not in used:
            used.append(id_)
    return ids


def validate_experience(value):
    if not isinstance(value, dict):
        raise PlanError('体验内容必须是对象')
    return {**{key: text(value.get(key, ''), key) for key in
               ('background', 'personal_fit', 'photo_tip', 'practical_tip')},
            'what_to_do': strings(value.get('what_to_do', []), '体验步骤', 6)}


def validate_action_guide(value):
    """Normalize harmless optional note encodings, never broken guide objects.

    Null means no notes; plain text or a text list unambiguously means steps.
    Keep the original wording, order and limits, then use the strict object
    validator. In particular, do not stringify unknown objects into advice.
    """
    if value is None:
        value = {}
    elif isinstance(value, str):
        note = text(value, '行动步骤')
        value = {'steps': [note] if note.strip() else []}
    elif isinstance(value, list):
        value = {'steps': value}
    if not isinstance(value, dict):
        raise PlanError('行动指南必须是对象')
    if set(value) - {'meeting_point', 'steps', 'bring', 'booking_note'}:
        raise PlanError('行动指南只允许集合地点、步骤、携带物品和预约说明；链接使用已有供给来源')
    return {'meeting_point': text(value.get('meeting_point', ''), '集合地点'),
            'steps': strings(value.get('steps', []), '行动步骤', 8),
            'bring': strings(value.get('bring', []), '携带物品', 12),
            'booking_note': text(value.get('booking_note', ''), '预约说明')}


def validate_day_details(value):
    result = {}
    for key, limit in (('date', 10), ('city', 200), ('lodging', 1200)):
        if key in value:
            result[key] = text(value[key], f'每日{key}', limit)
    if 'date' in result:
        try:
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', result['date']):
                raise ValueError('not ISO calendar date')
            date.fromisoformat(result['date'])
        except ValueError as error:
            raise PlanError('每日date必须是有效的YYYY-MM-DD日期；未知日期应省略') from error
    return result


def _day_items(day, catalog, used, seen):
    previous_end = 0
    clean = []
    for item in records(day.get('items'), '每日安排', 10):
        item_id = text(item.get('id'), '安排ID', 80)
        if not item_id or item_id in seen:
            raise PlanError('安排ID必须非空且唯一')
        seen.add(item_id)
        start, end = minutes(item.get('time')), minutes(item.get('end_time'))
        if start < previous_end or end <= start:
            raise PlanError(f"同一天的安排时间重叠或先后错误：第{day.get('day')}天，"
                            f"{item_id}为{item.get('time')}—{item.get('end_time')}，"
                            f"前项结束{previous_end // 60:02}:{previous_end % 60:02}。"
                            '跨午夜活动须拆入相邻两天，不能将次日凌晨写成本日结束时间。')
        previous_end = end
        supply_id = item.get('supply_id')
        if supply_id:
            _supply_refs([supply_id], catalog, used)
        kind = item.get('activity_kind', 'activity')
        if kind not in ('rest', 'meal', 'activity', 'personal', 'transfer'):
            raise PlanError('安排类型不正确')
        clean.append({**{k: text(item.get(k, ''), k) for k in ('title', 'why', 'transport', 'preparation')},
                      'id': item_id, 'time': item['time'], 'end_time': item['end_time'],
                      'supply_id': supply_id or None, 'activity_kind': kind})
        if isinstance(item.get('experience'), dict):
            clean[-1]['experience'] = validate_experience(item['experience'])
        if 'action_guide' in item:
            clean[-1]['action_guide'] = validate_action_guide(item['action_guide'])
    if not clean:
        raise PlanError('每日安排不能为空；休息日也应说明安排')
    return clean


def _validate_locks(plan, previous, locks):
    if not locks:
        return
    old = {x['id']: ((d['day'], d.get('date')), x) for d in previous.get('days', []) for x in d['items']}
    new = {x['id']: ((d['day'], d.get('date')), x) for d in plan['days'] for x in d['items']}
    core = ('id', 'time', 'end_time', 'title', 'supply_id', 'action_guide')
    for id_ in locks:
        if id_ not in old or id_ not in new:
            raise PlanError('锁定安排缺失，必须保留或请求用户解除锁定')
        if old[id_][0] != new[id_][0]:
            raise PlanError('删除会变动锁定安排的位置/日期，请先处理锁定安排')
        if any(old[id_][1].get(k) != new[id_][1].get(k) for k in core):
            raise PlanError(f'修改触及锁定安排 {id_}，需要用户解除锁定')


def _public_supply(item):
    result = copy.deepcopy(item)
    result['source_url'] = safe_url(item.get('source_url'))
    if not result['source_url']:
        raise PlanError('供给缺少有效的公开 HTTPS 来源')
    result['image_url'] = safe_url(item.get('image_url'))
    result['price_status'] = 'unknown'
    result['availability_status'] = 'unknown'
    result['booking_status'] = 'not_booked'
    result['verification'] = item.get('verification', '来源资料；未核验出行日库存与价格')
    return result


def validate_preview(research, intake=None):
    if intake is not None:
        return {'understanding': [{'text': x['text'], 'kind': x['kind'],
                'source': '、'.join(dict.fromkeys(e['label'] for e in x['evidence'])) or '待纠正的猜测'}
                for x in intake['signals']],
                'experience_angles': [{'idea': d['choice'], 'why': d['reason'],
                    'tradeoff': '可调整的默认方向，不是已确认安排'} for d in intake['defaults']]}
    result = {'understanding': [], 'experience_angles': []}
    for item in records(research.get('understanding', []), '当前理解', 16):
        result['understanding'].append({'text': text(item.get('text'), '理解内容'),
            'source': text(item.get('source', ''), '理解来源', 200), 'kind': 'hypothesis'})
    for item in records(research.get('experience_angles', []), '体验方向', 12):
        result['experience_angles'].append({k: text(item.get(k, ''), k)
                                           for k in ('idea', 'why', 'tradeoff', 'place_query')})
    return result


def validate_plan(raw, supplies, previous=None, locks=None, expected_days=None, feedback='', intake=None):
    if not isinstance(raw, dict):
        raise PlanError('方案必须为对象')
    catalog, used = {s['id']: s for s in supplies}, []
    plan = {k: text(raw.get(k), k) for k in ('title', 'summary')}
    if 'budget' in raw:
        plan['budget'] = text(raw['budget'], '预算说明')
    plan['understanding'] = []
    for item in records(raw.get('understanding'), '当前理解', 16):
        # A model-written label is not evidence of confirmation. Explicit user
        # confirmation needs an evidence-bound endpoint before promoting this.
        kind = 'hypothesis'
        source = text(item.get('source', ''), '理解来源', 200)
        plan['understanding'].append({'text': text(item.get('text'), '理解内容'), 'kind': kind, 'source': source})
    if intake is not None:
        plan['understanding'] = [{'text': item['text'], 'kind': item['kind'],
            'source': '、'.join(dict.fromkeys(e['label'] for e in item['evidence'])) or '待你纠正的猜测',
            'evidence': copy.deepcopy(item['evidence']), 'implication': item['implication']}
            for item in intake['signals']]
    signature = raw.get('signature')
    if not isinstance(signature, dict):
        raise PlanError('缺少本次体验构想')
    plan['signature'] = {k: text(signature.get(k, ''), k) for k in ('title', 'why', 'tradeoff')}
    plan['signature'].update(sequence=strings(signature.get('sequence'), '体验环节'),
                             supply_ids=_supply_refs(signature.get('supply_ids', []), catalog, used))
    plan['days'] = _days(raw.get('days'), catalog, used, expected_days)
    plan['decisions'] = _decisions(raw.get('decisions'), catalog, used)
    plan['preparation'] = _preparation(raw.get('preparation'), catalog, used)
    plan['questions'] = strings(intake.get('questions', []) if intake is not None
                                else raw.get('questions', []), '待补充问题', 3)
    plan['change_summary'] = strings(raw.get('change_summary', []), '修改说明', 12)
    _validate_locks(plan, previous or {}, locks or [])
    if feedback and previous and not substantive_change(previous, plan):
        raise PlanError('最新反馈未在方案、决定、待办或回答中留下回应；不能只改标题或修改说明。'
                        '缺渠道时明确写出未落实的具体请求和下一步；不要求伪造供给。')
    plan['supplies'] = [_public_supply(catalog[id_]) for id_ in used]
    pending = sum(x['status'] != 'not_applicable' for x in plan['preparation'])
    plan['readiness'] = {'label': '方案已形成，仍有事项待核验', 'blocking_count': max(1, pending),
                         'ready_to_depart': False, 'warnings': ['当前没有实时库存、报价或预订确认。',
                         '交通时刻为规划建议，营业和预约时段须按出行日期核验。']}
    return plan


def substantive_change(previous, plan):
    """Compare equivalent semantic projections, not derived display fields."""
    keys = ('understanding', 'days', 'decisions', 'preparation', 'questions', 'budget')
    if any(plan.get(k) != previous.get(k) for k in keys):
        return True
    signature_keys = ('title', 'why', 'sequence', 'tradeoff', 'supply_ids')
    if any(plan.get('signature', {}).get(k) != previous.get('signature', {}).get(k) for k in signature_keys):
        return True
    return plan.get('journey', {}).get('artifacts', []) != previous.get('journey', {}).get('artifacts', [])


def _days(value, catalog, used, expected_days):
    days = records(value, '每日行程', 14)
    if not days or (expected_days and len(days) != expected_days):
        raise PlanError(f'行程天数与已理解的需求不一致：需要 {expected_days or "至少1"} 天')
    seen, result = set(), []
    for index, day in enumerate(days, 1):
        if type(day.get('day')) is not int or day['day'] != index:
            raise PlanError('行程天数必须连续，从1开始')
        result.append({'day': index, 'title': text(day.get('title'), '每日主题'),
                       'rationale': text(day.get('rationale', ''), '每日理由'),
                       'items': _day_items(day, catalog, used, seen)})
        result[-1].update(validate_day_details(day))
    return result


def _decisions(value, catalog, used):
    result = []
    for item in records(value, '决定', 24):
        status = item.get('status', 'needs_check')
        if status in ('user_action', 'not_applicable', 'pending', 'needs_confirmation', 'draft', 'unknown', 'unverified'):
            status = 'needs_check'
        if status not in ('proposed', 'provided', 'needs_check'):
            raise PlanError('决定状态不能声称已预订、已报价或已核验')
        if status == 'provided':
            status = 'needs_check'  # Model provenance is not a user confirmation.
        result.append({**{k: text(item.get(k, ''), k) for k in ('category', 'choice', 'reason')},
                       'status': status, 'supply_ids': _supply_refs(item.get('supply_ids', []), catalog, used)})
    required = {'flight', 'lodging', 'transport', 'dining', 'activities', 'packing', 'other'}
    missing = required - {x['category'] for x in result}
    if missing:
        raise PlanError('缺少整趟准备的决定或明确缺口：' + ', '.join(sorted(missing)))
    return result


def _preparation(value, catalog, used):
    result = []
    for item in records(value, '准备事项', 30):
        status = item.get('status', 'needs_check')
        if status not in ('needs_check', 'user_action', 'not_applicable'):
            raise PlanError('准备状态不可未经证据标为完成')
        result.append({**{k: text(item.get(k, ''), k) for k in ('task', 'category', 'why')},
                       'status': status, 'supply_ids': _supply_refs(item.get('supply_ids', []), catalog, used)})
        if 'when' in item:
            result[-1]['when'] = text(item['when'], '准备触发时间')
        for key, label, limit in (('steps', '准备步骤', 8), ('materials', '所需材料', 12)):
            if key in item:
                result[-1][key] = strings(item[key], label, limit)
    return result
