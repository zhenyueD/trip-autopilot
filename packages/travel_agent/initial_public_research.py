"""One bounded public query for initial planning, bound to public place names."""
from copy import deepcopy
import hashlib
import json

from .destination_discovery import validate_context
from .research_focus import blocked_gap


def compile_query(direction):
    destinations, _ = validate_context(direction.get('destinations'), [])
    names = list(dict.fromkeys(d.get('search_name') or d['name'] for d in destinations))[:3]
    if not names:
        return None
    targets = [{'id': 'destination-' + hashlib.sha256(name.encode()).hexdigest()[:12],
                'name': name} for name in names]
    query = {'tool': 'search_public', 'public_targets': targets,
             'topics': ['transport' if len(names) > 1 else 'official_entry'], 'gap_ids': []}
    query['query_id'] = 'initial-public-' + hashlib.sha256(
        json.dumps(query, sort_keys=True).encode()).hexdigest()[:12]
    return query


def authorize(action, query):
    allowed = (query is not None and action.get('query_id') == query['query_id']
               and not (set(action) - {'tool', 'query_id', 'reason'}))
    return {'allowed': allowed, 'effective_action': deepcopy(query) if allowed else {},
            'gap': None if allowed else blocked_gap()}
