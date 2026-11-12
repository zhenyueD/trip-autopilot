"""Program-owned answers about committed revisions, never model recollection."""
from copy import deepcopy
import re

from .delivery_truth import revision_diff


def make_revision_entry(previous, candidate, revision, base_revision, *, kind='committed'):
    known_days = all(type(day.get('day')) is int for plan in (previous or {}, candidate or {})
                     for day in plan.get('days', []))
    if not known_days:
        # Old prototype records occasionally omitted actual day identifiers.
        # Keep the lossless change record without pretending list positions are days.
        from .writer_critic import mechanical_diff
        changes = mechanical_diff(previous or {}, candidate or {})
        return {'revision': revision if type(revision) is int else None,
                'base_revision': base_revision if type(base_revision) is int else None,
                'kind': kind, 'changed_days': [], 'changes': changes, 'day_identity_unknown': True}
    changes = revision_diff(previous or {}, candidate or {}, complete=True)
    days = sorted({int(match[1]) for row in changes
                   if (match := re.match(r'^/days/(\d+)(?:/|$)', row['path']))})
    return {'revision': revision if type(revision) is int else None,
            'base_revision': base_revision if type(base_revision) is int else None,
            'kind': kind, 'changed_days': days, 'changes': changes}


def _legacy_timeline(trip):
    snapshots = [row for row in trip.get('history', [])
                 if isinstance(row, dict) and isinstance(row.get('plan'), dict)]
    current = {'plan': trip.get('plan') or {}, 'plan_revision': trip.get('plan_revision')}
    entries = []
    for before, after in zip(snapshots, [*snapshots[1:], current]):
        entries.append(make_revision_entry(before['plan'], after['plan'],
            after.get('plan_revision'), before.get('plan_revision')))
    return entries


def _value(value):
    if value is None:
        return '无'
    if isinstance(value, dict):
        parts = [str(value[key]) for key in ('title', 'name', 'task', 'action', 'value', 'time')
                 if isinstance(value.get(key), (str, int, float))]
        text = '，'.join(parts) or '安排详情'
    elif isinstance(value, list):
        text = '；'.join(_value(row) for row in value) or '无'
    else:
        text = str(value)
    return text if len(text) <= 180 else text[:180] + '…（详情见变更记录）'


def _field_label(path):
    fields = {'title': '活动名称', 'time': '时间安排', 'transport': '交通',
        'transportation': '交通', 'duration': '活动时长', 'rationale': '安排理由',
        'date': '日期', 'city': '城市', 'lodging': '住宿', 'budget': '预算',
        'summary': '行程概述', 'notes': '注意事项', 'caution': '注意事项',
        'booking': '预约', 'price': '价格记录', 'intensity': '活动强度',
        'walking': '步行安排', 'breaks': '休息安排', 'status': '办理状态'}
    if '/item_order' in path:
        return '活动顺序'
    if path.startswith(('/preparation', '/user_commitments', '/commitment_schedule')):
        return '准备事项'
    return fields.get(path.rsplit('/', 1)[-1], '活动安排' if '/items/' in path else '方案内容')


def _entry_text(entry, ordinal):
    version = ('版本 ' + str(entry['revision']) if type(entry.get('revision')) is int
               else f'历史第 {ordinal} 轮（版本号未记录）')
    kind = {'undone': '撤销', 'undo': '撤销', 'action_report': '本人办理标记'}.get(entry.get('kind'), '已提交调整')
    days = '、'.join(f'第 {day} 天' for day in entry.get('changed_days', []))
    lines = [f'{version} · {kind}：' + (days if days else '无日程天数变更')]
    if entry.get('day_identity_unknown'):
        return f'{version} · {kind}：旧记录日期标识缺失，无法归属具体天。'
    for row in entry.get('changes', []):
        path = row.get('path', '')
        match = re.match(r'^/days/(\d+)(?:/|$)', path)
        label = (f'第 {match[1]} 天 ' if match else '') + _field_label(path)
        if '/item_order' in path:
            lines.append(f'  {label}已调整。')
        else:
            lines.append(f'  {label}：{_value(row.get("before"))} → {_value(row.get("after"))}')
    return '\n'.join(lines)


def timeline_answer(trip):
    entries = deepcopy(trip.get('revision_timeline') or _legacy_timeline(trip))
    current = trip.get('plan_revision')
    prefix = (f'当前显示方案：版本 {current}。' if type(current) is int
              else '当前显示方案的版本号未记录；请求版本不等于方案版本。')
    texts, citations = [], []
    for ordinal, entry in enumerate(entries, 1):
        entry['status'] = 'historical_revision'
        text = _entry_text(entry, ordinal)
        texts.append(text)
        citations.append({'source_id': 'revision:' + str(entry.get('revision')) + ':' + str(ordinal),
            'label': text.split('\n')[0], 'quote': text, 'url': None,
            'status': 'historical_revision', 'revision': entry.get('revision'),
            'base_revision': entry.get('base_revision')})
    suffix = '以下逐轮列出保存的历史变更；变更前快照不代表当前安排。'
    if trip.get('timeline_truncated') or not trip.get('revision_timeline'):
        suffix += '历史记录可能不完整。'
    return {'text': prefix + '\n' + suffix + '\n' + ('\n\n'.join(texts) or '暂无可用的已提交变更记录。'),
            'citations': citations, 'judgments': [], 'timeline': entries,
            'current_plan_revision': current, 'status': 'supported' if entries else 'unknown',
            'timeline_limited': bool(trip.get('timeline_truncated') or not trip.get('revision_timeline'))}
