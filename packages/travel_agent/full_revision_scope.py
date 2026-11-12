"""A full response may change structure, but never invent its own edit scope."""
import copy
import re

from .scoped_replan import classify_revision, resolve_plan_impact
from .validation import PlanError


_GLOBAL_CHANGE = re.compile(
    r'(?:全程|整趟|整个旅行|旅行目标|旅行主题|同行人|同行人数|人数).{0,16}(?:改|换|增加|减少|变|重新)|'
    r'(?:目的地|路线拓扑|旅行路线).{0,10}(?:改|换|重排)|'
    r'(?:总预算|预算上限).{0,10}(?:改|增|降|\d)|'
    r'预算(?:改为|提高|降低|增加|减少)|(?:增加|减少|延长|缩短).{0,5}[天晚]|'
    r'(?:重排|调整|更换|重新规划).{0,5}(?:全程|整趟行程|整个旅行|旅行路线|路线顺序)')
_DAYS = re.compile(r'第([一二三四五六七八九十\d]+)天')


def _mentioned_days(value):
    names = {name: number for number, name in enumerate('一二三四五六七八九十', 1)}
    return {int(word) if word.isdigit() else names.get(word) for word in _DAYS.findall(value)}


def build_full_revision_scope(previous, routing, prior_impact=None):
    """Reuse a resolved closure; widen permissions inside it, never outside it."""
    if not previous:
        return {'kind': 'initial', 'day_numbers': [], 'domains': [], 'rerun_taste': True,
                'reason': '尚无已有方案，不能进行局部增量修订。', 'impact_source': 'initial'}
    routing = routing or {}
    global_change = bool(_GLOBAL_CHANGE.search(routing.get('feedback', '')))
    numbers = [day['day'] for day in previous.get('days', [])]
    intent = {**routing, 'target_days': routing.get('target_days') or numbers}
    domains = list(routing.get('domains') or sorted({r['category'] for r in previous.get('decisions', [])}))
    intent['domains'] = domains
    impact = copy.deepcopy(prior_impact) if prior_impact and not global_change else resolve_plan_impact(previous, intent)
    days = numbers if global_change else impact['day_numbers']
    if not days:
        raise PlanError('未能从已有安排确定本轮全量修订范围，原方案保留')
    # Full compilation allows structural replacement of the target day. The
    # existing resolver still owns cross-day dependencies and category edges.
    impact['day_numbers'] = days
    impact['item_ids'] = [i['id'] for d in previous['days'] if d['day'] in days for i in d['items']]
    impact['route_days'] = list(days)
    impact['affected_node_ids'] = sorted(set(impact['affected_node_ids'])
                                         | {f'item:{id_}' for id_ in impact['item_ids']})
    reason = ('用户明确改变全局目标或行程结构，需要重新确定全局方向。' if global_change else
              '局部字段增量合同不足，需要在已有影响范围内修改结构。'
              if routing.get('fallback_reason') == 'scoped_validation_failed' else
              '本轮需要补查供给或更新事实；仅重算程序确定的影响范围。')
    return {**impact, 'kind': 'global_revision' if global_change else 'focused_revision',
            'domains': domains, 'rerun_taste': global_change, 'reason': reason,
            'needs_new_supply': routing.get('requested_pipeline') == 'needs_research',
            'impact_source': 'scoped_reuse' if prior_impact and not global_change else 'resolver'}


def extend_full_revision_scope(previous, routing, scope, materials=(), commitments=None):
    """Add explicit material-day relations and validated hard-constraint anchors.

    The model cannot supply day numbers here. Material text uses the existing
    deterministic parser; commitments must already have passed source/quote,
    recurrence and type validation. Unresolved material stays visible in a pool;
    it neither vetoes this edit nor silently expands its scope.
    """
    additions = []
    pending = {row['source_id']: row for row in scope.get('pending_materials', [])}
    all_days = [day['day'] for day in previous.get('days', [])]
    for source in materials:
        if source.get('kind') == 'user_feedback':
            continue
        content = source.get('content') or ''
        relation = classify_revision(previous, content)
        label = source.get('name') or source.get('label') or source['id']
        feedback = (routing or {}).get('feedback', '')
        # Only the actual user's current feedback may assign a pending file.
        # Quoted instructions in the uploaded file are not assignments.
        if label in feedback or source['id'] in feedback:
            assigned = classify_revision(previous, feedback)
            if assigned['target_days'] and assigned['domains']:
                relation = assigned
        days, domains = relation['target_days'], relation['domains']
        if re.search(r'接机|送机', content) and 'transport' not in domains:
            domains.append('transport')
        if not days:
            named = {s['id'] for s in previous.get('supplies', [])
                     if s.get('name') and s['name'] in content}
            days = [d['day'] for d in previous['days']
                    if any(i.get('supply_id') in named for i in d['items'])]
        if not days and set(scope['day_numbers']) == set(all_days) and domains:
            days = all_days
        if not days or not domains or not set(days).issubset(all_days):
            pending[source['id']] = {'source_id': source['id'], 'label': label,
                'reason': 'unresolved_day_or_topic', 'status': 'awaiting_assignment'}
            continue
        pending.pop(source['id'], None)
        additions.append({'source_id': source['id'], 'basis': 'material_day_relation',
                          'day_numbers': days, 'domains': domains})
    if commitments is not None:
        before = {r['id']: r for r in previous.get('user_commitments', [])}
        after = {r['id']: r for r in commitments}
        for id_ in before.keys() | after.keys():
            if before.get(id_) == after.get(id_):
                continue
            for row in (before.get(id_), after.get(id_)):
                if not row:
                    continue
                days = [row['day']] if row.get('recurrence') == 'once' else all_days
                domains = ['dining' if row.get('activity_kind') == 'meal' else 'activities']
                if row.get('location_kind') == 'lodging':
                    domains.append('lodging')
                additions.append({'commitment_id': id_, 'basis': 'validated_hard_constraint',
                                  'day_numbers': days, 'domains': domains,
                                  'research_domains': ['lodging'] if row.get('location_kind') == 'lodging' else []})
    if not additions:
        return {**scope, 'pending_materials': list(pending.values())}
    days = sorted(set(scope['day_numbers']).union(*(set(a['day_numbers']) for a in additions)))
    domains = sorted(set(scope['domains']).union(*(set(a['domains']) for a in additions)))
    expanded = build_full_revision_scope(previous, {**routing, 'target_days': days,
        'domains': domains, 'feedback': ''})
    expanded.update(reason=scope['reason'] + ' 新增材料关联或已校验硬约束需一并联动。',
                    scope_extensions=scope.get('scope_extensions', []) + additions,
                    impact_source='resolver_with_validated_extensions',
                    pending_materials=list(pending.values()))
    public_additions = [a for a in additions if a['basis'] == 'material_day_relation'
                        or a.get('research_domains')]
    expanded['research_day_numbers'] = sorted(set(scope.get('research_day_numbers', scope['day_numbers']))
        .union(*(set(a['day_numbers']) for a in public_additions)))
    expanded['research_domains'] = sorted(set(scope.get('research_domains', scope['domains']))
        .union(*(set(a.get('research_domains', a['domains'])) for a in public_additions)))
    return expanded


def research_plan_scope(previous, scope):
    if 'research_day_numbers' not in scope:
        return scope
    return build_full_revision_scope(previous, {'target_days': scope['research_day_numbers'],
                                                'domains': scope['research_domains'], 'feedback': ''})


def preparation_in_scope(row, scope):
    refs = set(row.get('related_item_ids', []))
    if refs:
        return bool(refs.intersection(scope['item_ids']))
    if isinstance(row.get('day'), int):
        return row['day'] in scope['day_numbers']
    days = _mentioned_days(' '.join(str(row.get(key, '')) for key in ('when', 'task', 'title')))
    if days:
        return bool(days.intersection(scope['day_numbers']))
    return row.get('category') in scope.get('preparation_categories', [])


def focused_plan(previous, scope):
    result = copy.deepcopy(previous)
    result['days'] = [d for d in result.get('days', []) if d['day'] in scope['day_numbers']]
    result['decisions'] = [r for r in result.get('decisions', [])
                           if r['category'] in scope.get('decision_categories', [])]
    result['preparation'] = [r for r in result.get('preparation', []) if preparation_in_scope(r, scope)]
    result['routes'] = [r for r in result.get('routes', []) if r.get('day') in scope['day_numbers']]
    if isinstance(result.get('journey'), dict):
        result['journey'] = {'artifacts': [a for a in result['journey'].get('artifacts', [])
            if set(a.get('related_item_ids', [])).intersection(scope['item_ids'])]}
    used = {i.get('supply_id') for d in result['days'] for i in d['items']}
    used.update(id_ for row in result['decisions']
                if set(row.get('related_item_ids', [])).intersection(scope['item_ids'])
                for id_ in row.get('supply_ids', []))
    result['supplies'] = [row for row in result.get('supplies', []) if row.get('id') in used]
    if isinstance(result.get('preparation_pack'), dict):
        result['preparation_pack']['actions'] = [row for row in result['preparation_pack'].get('actions', [])
                                                if preparation_in_scope(row, scope)]
    return result


def research_scope(previous, scope):
    public_scope = research_plan_scope(previous, scope)
    focused = focused_plan(previous, public_scope)
    public = [row for row in focused.get('supplies', [])
              if isinstance(row.get('source_url'), str) and row['source_url'].startswith(('http://', 'https://'))]
    ids = [row['id'] for row in public]
    targets = [{'supply_id': row['id'], 'name': row['name'], 'role': 'destination'}
               for row in public if row.get('name')]
    facets = ('fare', 'booking_channel', 'pickup_point', 'timetable')
    gaps = [{'id': f"full:{domain}:{facet}", 'domain': domain, 'facet': facet,
             'item_ids': list(public_scope['item_ids']), 'supply_ids': ids, 'owner': 'agent', 'status': 'open'}
            for domain in public_scope['domains'] for facet in (facets if domain == 'transport' else ('booking_channel',))]
    return {'day_numbers': list(public_scope['day_numbers']), 'item_ids': list(public_scope['item_ids']),
            'supply_ids': ids, 'domains': list(public_scope['domains']), 'public_targets': targets, 'gaps': gaps,
            'public_cities': sorted({row['city'] for row in public if row.get('city')}),
            'needs_new_supply': bool(scope.get('needs_new_supply'))}


def validate_full_result(previous, proposed, scope):
    if scope.get('kind') != 'focused_revision':
        return
    days = set(scope['day_numbers'])
    before = {d['day']: d for d in previous.get('days', [])}
    after = {d['day']: d for d in proposed.get('days', [])}
    if set(before) != set(after) or any(after.get(n) != day for n, day in before.items() if n not in days):
        raise PlanError('模型修改超出程序确定的全量修订范围：未影响日期必须保留')
    categories = set(scope.get('decision_categories', []))
    for field, predicate in (
        ('decisions', lambda row: row.get('category') in categories),
        ('preparation', lambda row: preparation_in_scope(row, scope)),
    ):
        old = [r for r in previous.get(field, []) if not predicate(r)]
        new = [r for r in proposed.get(field, []) if not predicate(r)]
        if old != new:
            raise PlanError('模型修改超出程序确定的全量修订范围：未影响决定或准备必须保留')


def prepare_full_response(raw, previous, scope):
    """Reject explicit out-of-scope intent before any normalization or merge."""
    result = copy.deepcopy(raw)
    delta = result.get('delta') or {}
    days = set(scope['day_numbers'])
    if delta.get('day_changes'):
        raise PlanError('模型修改超出程序确定的全量修订范围：天数变化需明确全局授权')
    for row in delta.get('day_patches', []):
        if not isinstance(row, dict) or row.get('day') not in days:
            raise PlanError('模型修改超出程序确定的全量修订范围：包含未授权日期')
    for row in delta.get('move_items', []):
        if not isinstance(row, dict) or row.get('from_day') not in days or row.get('to_day') not in days:
            raise PlanError('模型修改超出程序确定的全量修订范围：跨日移动未授权')
    if 'journey' not in result:
        return result
    journey = result['journey']
    if not isinstance(journey, dict) or set(journey) - {'artifacts'}:
        raise PlanError('全量修订范围只允许更新相关旅行材料')
    incoming = journey.get('artifacts', [])
    if not isinstance(incoming, list):
        raise PlanError('旅行材料必须为列表')
    old = previous.get('journey', {}).get('artifacts', [])
    preserved = [a for a in old if not a.get('related_item_ids')
                 or not set(a['related_item_ids']).issubset(scope['item_ids'])]
    by_id = {a.get('id'): a for a in preserved}
    for artifact in incoming:
        if not isinstance(artifact, dict):
            raise PlanError('旅行材料必须为对象')
        if artifact.get('id') in by_id:
            if artifact != by_id[artifact['id']]:
                raise PlanError('旅行材料修改超出全量修订范围')
        elif not artifact.get('related_item_ids') or not set(artifact['related_item_ids']).issubset(scope['item_ids']):
            raise PlanError('旅行材料修改超出全量修订范围')
    journey['artifacts'] = copy.deepcopy(preserved) + [a for a in incoming if a.get('id') not in by_id]
    return result


def restore_untouched_fields(previous, proposed, normalized, scope):
    """Only after validating the raw candidate, preserve old representation bytes."""
    normalized['days'] = [copy.deepcopy(next(d for d in previous['days'] if d['day'] == day['day']))
                          if day['day'] not in scope['day_numbers'] else day for day in normalized['days']]
    for index, row in enumerate(proposed.get('preparation', [])):
        if not preparation_in_scope(row, scope):
            normalized['preparation'][index] = copy.deepcopy(row)
