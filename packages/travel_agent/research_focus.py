"""Program-owned, bounded research permissions for a scoped revision.

Names are taken from public supply records, never free-form itinerary text.
Registry membership proves the requested domain/facet; keywords alone do not.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json


FACETS = {'fare': '价格', 'booking_channel': '预订渠道',
          'pickup_point': '上车点', 'timetable': '班次'}
ROLES = {'origin', 'destination', 'transport_provider'}
DOMAIN_RESEARCH = {
    'lodging': ('lodging_policy', 'hotel lodging accommodation', ('hotel', 'lodging')),
    'dining': ('official_entry', 'restaurant dining food', ('dining', 'restaurant')),
    'activities': ('ticketing', 'attraction activities tickets', ('place', 'activity', 'experience')),
}


def recent_research(record):
    """Match the existing preparation cache window, never suppress forever."""
    try:
        when = datetime.fromisoformat(record.get('fetched_at') or record.get('checked_at') or '')
        return when.tzinfo is not None and 0 <= (datetime.now(timezone.utc) - when).total_seconds() < 86400
    except (ValueError, TypeError):
        return False


def filter_candidates(items, scope):
    """Shared pre-read gate for existing public candidate discovery services."""
    if scope is None:
        return items
    if (not isinstance(scope, dict) or any(not isinstance(scope.get(key), list)
            or not scope[key] or any(not isinstance(x, str) or not x.strip() for x in scope[key])
            for key in ('cities', 'kinds'))):
        raise ValueError('Candidate scope requires public cities and kinds')
    cities = {' '.join(city.split()).casefold() for city in scope['cities']}
    return [s for s in items if isinstance(s.get('city'), str)
            and ' '.join(s['city'].split()).casefold() in cities and s.get('kind') in scope['kinds']]


def _register(registry, request, gap_ids):
    key = 'query-' + hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()[:14]
    registry[key] = {**request, 'gap_ids': gap_ids}
    return key


def _rows(value):
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _ids(value):
    return list(dict.fromkeys(x for x in value if isinstance(x, str))) if isinstance(value, list) else []


def compile_research_focus(scope, supplies, evidence=None):
    """Compile trusted runtime scope; return None only when scope is absent."""
    if scope is None:
        return None
    scope = scope if isinstance(scope, dict) else {}
    ids = _ids(scope.get('supply_ids'))
    by_id = {s.get('id'): s for s in _rows(supplies) if s.get('id') in ids}
    targets = []
    for row in _rows(scope.get('public_targets')):
        supply = by_id.get(row.get('supply_id'), {})
        name = supply.get('name')
        if (row.get('role') in ROLES and isinstance(name, str) and name.strip()
                and len(name) <= 240 and supply.get('id') not in [t['id'] for t in targets]):
            targets.append({'id': supply['id'], 'name': name.strip(), 'role': row['role']})
    targets = targets[:3]
    gaps = []
    for raw in _rows(scope.get('gaps')):
        facet = raw.get('facet')
        domain = raw.get('domain')
        if (not isinstance(facet, str) or facet not in FACETS or not isinstance(domain, str)
                or domain not in _ids(scope.get('domains'))):
            continue
        gaps.append({'id': str(raw.get('id') or domain + '-' + facet), 'domain': domain,
                     'facet': facet, 'title': FACETS[facet] + '待研究', 'kind': 'supply',
                     'item_ids': [x for x in _ids(raw.get('item_ids')) if x in _ids(scope.get('item_ids'))],
                     'supply_ids': [x for x in _ids(raw.get('supply_ids')) if x in ids],
                     'owner': 'agent', 'status': 'open',
                     'detail': 'Agent 需取得适用公开规则；搜索或读到页面不代表缺口已解决。'})
    registry = {}
    facets = list(dict.fromkeys(g['facet'] for g in gaps if g['domain'] == 'transport'))
    if facets and targets:
        topics = ['transport'] + ['transport_' + facet for facet in facets]
        for topic in topics:
            request = {'tool': 'search_public', 'public_targets': targets, 'topics': [topic]}
            _register(registry, request, [g['id'] for g in gaps if g['domain'] == 'transport'
                and (topic == 'transport' or topic == 'transport_' + g['facet'])])
    cities = list(dict.fromkeys(s['city'].strip() for s in by_id.values()
        if isinstance(s.get('city'), str) and s['city'].strip() and len(s['city']) <= 160))[:3]
    for domain, (topic, keywords, kinds) in DOMAIN_RESEARCH.items():
        gap_ids = [g['id'] for g in gaps if g['domain'] == domain]
        if not gap_ids:
            continue
        if scope.get('needs_new_supply') is True and not cities:
            for gap in gaps:
                if gap['domain'] == domain:
                    gap['detail'] = '已有公共供应缺少城市锚点，未执行新候选发现；Agent 需先补齐公共地点依据，不使用私人住宿字符串拼查询。'
        if cities and scope.get('needs_new_supply') is True:
            _register(registry, {'tool': 'search_supply', 'queries': [f'{c} {keywords}' for c in cities],
                'destinations': [{'name': city, 'search_name': city} for city in cities],
                'place_hints': [], 'candidate_scope': {'domain': domain, 'cities': cities, 'kinds': list(kinds)}}, gap_ids)
        domain_targets = [t for t in targets if by_id[t['id']].get('kind') in kinds]
        if domain_targets:
            _register(registry, {'tool': 'search_public', 'public_targets': domain_targets,
                                 'topics': [topic]}, gap_ids)
    return {'supply_ids': ids, 'item_ids': _ids(scope.get('item_ids')), 'gaps': gaps,
            'queries': registry, 'initial_query_id': next(iter(registry), None)}


def admit_discovered_candidates(focus, action, supplies):
    """Only tool-filtered public candidates extend membership, never model IDs."""
    if focus is None or action.get('tool') != 'search_supply' or not action.get('candidate_scope'):
        return
    bound = action['candidate_scope']
    topic = DOMAIN_RESEARCH[bound['domain']][0]
    for supply in supplies:
        if (not filter_candidates([supply], bound)
                or not isinstance(supply.get('name'), str) or not supply.get('source_url')):
            continue
        if supply['id'] not in focus['supply_ids']:
            focus['supply_ids'].append(supply['id'])
        _register(focus['queries'], {'tool': 'search_public', 'public_targets': [
            {'id': supply['id'], 'name': supply['name'], 'role': 'destination'}],
            'topics': [topic]}, action.get('gap_ids', []))


def blocked_gap():
    return {'id': 'research-scope-blocked', 'title': '范围外或关系不明的研究未执行',
            'kind': 'supply', 'owner': 'agent', 'status': 'open',
            'detail': 'Agent 需先证明研究与本次安排的关系；未把未知事项交给用户核实。'}


def authorize_research_action(action, focus, state):
    """Private-model arguments cannot override a registered public request."""
    if focus is None:
        return {'allowed': True, 'effective_action': action, 'gap': None}
    name = action.get('tool')
    allowed_keys = {'search_public': {'tool', 'query_id', 'reason'},
                    'search_supply': {'tool', 'query_id', 'reason'},
                    'inspect_source': {'tool', 'query_id', 'supply_id', 'url', 'reason'},
                    'inspect_geography': {'tool', 'supply_ids', 'reason'}}
    valid = isinstance(name, str) and name in allowed_keys and not (set(action) - allowed_keys.get(name, set()))
    query = focus['queries'].get(action.get('query_id')) if isinstance(action.get('query_id'), str) else None
    effective = deepcopy(action)
    if valid and name in ('search_public', 'search_supply'):
        valid = query is not None and query['tool'] == name
        if valid:
            effective = deepcopy(query) | {'query_id': action['query_id']}
    elif valid and name == 'inspect_source':
        id_ = action.get('supply_id')
        url = action.get('url')
        valid = query is not None and id_ in focus['supply_ids'] and isinstance(url, str)
        valid = valid and any(e.get('query_id') == action['query_id']
            and id_ in e.get('supply_ids', []) and url in (e.get('url'), e.get('source_url'))
            for e in state['evidence'])
    elif valid and name == 'inspect_geography':
        ids = action.get('supply_ids')
        valid = isinstance(ids, list) and bool(ids) and all(
            isinstance(x, str) and x in focus['supply_ids'] for x in ids)
    return {'allowed': bool(valid), 'effective_action': effective,
            'gap': None if valid else blocked_gap()}
