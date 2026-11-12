"""One request-priced Search call; returned text is never original-page evidence."""
from __future__ import annotations

import copy
from dataclasses import dataclass
import json
import os
import urllib.request

from . import public_search
from .model import _read_response
from .phase1_budget_context import uncharged_caller
from .supply_discovery_queue import SummaryLead, discovery_queue, summary_leads


API = 'https://api.deepseek.com/v1/tools/search'
MAX_RESPONSE_BYTES = 2_000_000


@dataclass(frozen=True)
class DiscoveryHint:
    """Untrusted search labels used only to schedule original-page reads."""

    title: str = ''
    site_name: str = ''


@dataclass(frozen=True)
class DeepSeekSearchResponse:
    """Adapter-owned response type; provider JSON cannot select request accounting."""

    urls: tuple[str, ...]
    status: str
    discovery_hints: tuple[DiscoveryHint, ...] = ()
    summary_leads: tuple[SummaryLead, ...] = ()


class DeepSeekResponseError(ValueError):
    """The received body was retained, but is not a usable Search response."""

    def __init__(self, facts):
        super().__init__('Invalid DeepSeek Search response')
        self.usage = facts


def _response(raw, facts):
    try:
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError('Search response exceeds limit')
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError('Search response must be an object')
        if 'error' in data:
            return DeepSeekSearchResponse((), 'api_error')
        results = data.get('results')
        if not isinstance(results, list) or len(results) > 50:
            raise ValueError('Search response needs a bounded results list')
        if any(not isinstance(item, dict) or not isinstance(item.get('url'), str) for item in results):
            raise ValueError('Search result needs a URL')
    except (ValueError, TypeError):
        raise DeepSeekResponseError(facts) from None
    # Bounded labels can prioritize reads, but never authenticate source evidence.
    # Summary URL occurrences retain only bounded discovery context. No search
    # content, summary claim, date or authority score becomes original evidence.
    hints = tuple(DiscoveryHint(**{key: item[key][:300] if isinstance(item.get(key), str) else ''
                                 for key in ('title', 'site_name')}) for item in results)
    return DeepSeekSearchResponse(tuple(item['url'] for item in results),
                                  'response_received' if results else 'empty_results', hints,
                                  summary_leads(results))


def _response_facts(request):
    facts = copy.deepcopy(getattr(request, '_travel_response_facts', None) or {})
    # The reused transport does not expose every header (including x-ds-trace-id).
    # Missing capture does not establish that the provider omitted an identifier.
    facts['provider_request_id_capture_status'] = (
        'captured' if facts.get('provider_request_id') else 'not_captured')
    return facts


@dataclass(frozen=True)
class DeepSeekDiscovery:
    """Explicit frozen short inputs; never truncate or derive a city-specific query."""

    query: str
    context: str
    count: int = 30

    def __post_init__(self):
        for name in ('query', 'context'):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or len(value) > 128:
                raise ValueError(name + ' must contain 1 to 128 characters')
        if type(self.count) is not int or not 1 <= self.count <= 50:
            raise ValueError('count must be an integer from 1 to 50')

    def __call__(self, query, *, deadline=None, parameters=None, on_request=None, on_response=None):
        key = os.getenv('DEEPSEEK_API_KEY')
        if not key:
            raise ValueError('DeepSeek Search is not configured')
        payload = {'query': self.query, 'ori_question': self.context,
                   'count': self.count, 'need_content': False}
        request = urllib.request.Request(API, method='POST',
            data=json.dumps(payload, ensure_ascii=False).encode(),
            headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key})
        if on_request is not None:
            on_request({'query_text': query, 'parameters': copy.deepcopy(parameters or {}),
                        'request_body': json.loads(request.data), 'method': request.get_method(),
                        'endpoint': request.full_url, 'phase': 'prepared'})
        try:
            # ComparisonResearch already reserved this physical request. This
            # existing wrapper also forbids redirects, which could dispatch again.
            raw = uncharged_caller(_read_response)(request, public_search._preflight_remaining(deadline, 20))
        except Exception as error:
            diagnostic = getattr(error, '_travel_diagnostic', None)
            if isinstance(diagnostic, dict):
                error.usage = {**copy.deepcopy(diagnostic), **getattr(error, 'usage', {}),
                               'error_body_capture': 'sanitized_diagnostic_only'}
            raise
        facts = _response_facts(request)
        if on_response is not None:
            on_response({'raw': raw, 'response_facts': facts})
        return _response(raw, facts)


def discovery_urls(response):
    """Keep only queued public URLs; the separate ledger retains every exclusion."""
    return discovery_queue(response)[0]
