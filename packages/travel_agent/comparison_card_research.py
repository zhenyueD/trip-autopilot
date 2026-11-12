"""One paid discovery request plus bounded, secure original-page reads.

Search answers are discovery leads, never factual evidence. This collector does
not adjudicate claims: source authority, applicability and independence need review.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import time
from urllib.parse import urlsplit

from . import material_reader, public_search
from .comparison_card_budget import now, private_json
from .deepseek_discovery import DeepSeekSearchResponse

MAX_CANDIDATES = 4
MAX_EXCERPT_CHARS = 6000
PAGE_READ_SECONDS = 36
INDEPENDENCE_CAVEAT = (
    'Domains identify publisher candidates only. Different domains, duplicate pages, '
    'translations and search summaries do not prove source independence or agreement. '
    'Compare original claims about the same entity, date and conditions; one source is '
    '未交叉确认, missing evidence is unknown. Do not promise future seats, crowding, '
    'or exhaustive absence from bounded reads.')


class ResearchFailed(RuntimeError):
    """Do not start the comparison model after an unaccounted research request."""


def _scope(seed):
    brief, options = seed.get('brief'), seed.get('options')
    if not isinstance(brief, str) or not brief.strip():
        raise ValueError('Explicit comparison brief is required')
    if (not isinstance(options, list) or len(options) != 2
            or any(not isinstance(option, dict) or not isinstance(option.get('name'), str)
                   or not option['name'].strip() for option in options)):
        raise ValueError('Exactly two named comparison options are required')
    return {key: copy.deepcopy(seed[key]) for key in
            ('brief', 'options', 'dates', 'timezone', 'conditions') if key in seed}


def _query(scope):
    return (
        'Research only the complete comparison request below; preserve its original question, '
        'entities, dates, timezone and conditions without adding unrelated objectives. '
        'Select the primary source by claim type: the operator for current hours, prices and '
        'availability, the responsible authority for entry/transit permissions, and Maps/Routes '
        'for the actual endpoints, transport mode and time. Then cross-check individual claims '
        'with independent sources of another type about the same entity, claim, date and conditions. '
        'For experience/current-condition checks, search-discovered public Reddit posts within '
        '7 days and reviews within one month may supply first-hand points; retain social_unverified '
        'and never use UGC to establish prices, opening hours, inventory or permissions. '
        'Keep older material as lower-weight background. Publication, experience, retrieval and '
        'policy-effective dates are distinct; unknown dates do not establish freshness. '
        'An official single-source claim remains 未交叉确认 next to that claim; missing evidence '
        'remains unknown. Preserve conflicts and investigate relevant changes rather than vote. '
        'Different source types, domains or languages can share an upstream publisher; they do '
        'not establish independence by themselves. Stop at login/app/challenge walls; no bypass. '
        'S2 uncertainty can accompany delivery; S1 blocks only the related definite promise. '
        'Do not claim future seating or crowding, '
        'or treat an incomplete list as exhaustive. Request: '
        + json.dumps(scope, ensure_ascii=False))


def _usage(response):
    metadata = response.get('usageMetadata')
    required = ('promptTokenCount', 'candidatesTokenCount', 'totalTokenCount')
    if not isinstance(metadata, dict) or any(
            type(metadata.get(key)) is not int or metadata[key] < 0 for key in required):
        return None
    if metadata['totalTokenCount'] < metadata['promptTokenCount'] + metadata['candidatesTokenCount']:
        return None
    return {key: value for key, value in metadata.items() if type(value) is int and value >= 0}


def _provided_sources(seed):
    return [{**copy.deepcopy(source), 'source_origin': 'developer_provided',
             'original_read_status': 'not_read_by_research', 'factual_status': 'uncorroborated',
             'independence_status': 'unassessed'} for source in seed.get('sources', [])]


def _safe_facts(facts):
    allowed = ('http_status', 'provider_request_id', 'provider_request_id_present',
               'request_delivery_status', 'usage_status', 'usage_complete', 'timeout_stage',
               'provider_error_body', 'provider_error_body_truncated', 'provider_error_body_status',
               'provider_request_id_capture_status', 'error_body_capture')
    return {key: facts[key] for key in allowed if isinstance(facts, dict) and key in facts}


class ComparisonResearch:
    """Synchronous enrichment; the caller shares its DurableBudget with the model."""

    def __init__(self, budget, *, searcher=None, reader=None):
        self.budget = budget
        self.searcher = searcher or public_search._ground
        self.reader = reader or material_reader.read_public_link

    def __call__(self, seed, *, request_id):
        scope = _scope(seed)
        path_id = hashlib.sha256(str(request_id).encode()).hexdigest()
        artifacts = self.budget.directory / 'research-artifacts' / path_id
        response, receipt = self._search(_query(scope), scope, request_id, artifacts)
        deadline = time.monotonic() + PAGE_READ_SECONDS
        candidates = public_search._result_urls(response, deadline=deadline)
        sources, reads = _provided_sources(seed), []
        for index, url in enumerate(candidates[:MAX_CANDIDATES]):
            source, read = self._read(url, index, artifacts, deadline)
            reads.append(read)
            if source is not None:
                existing_ids = {item['id'] for item in sources}
                while source['id'] in existing_ids:
                    source['id'] += '-read'
                sources.append(source)
        context = {'query': _query(scope), 'scope': scope, 'retrieved_at': now(),
                   'search_receipt': receipt, 'original_reads': reads,
                   'skipped_candidates': candidates[MAX_CANDIDATES:],
                   'publisher_candidates': list(dict.fromkeys(
                       urlsplit(source['url']).hostname for source in sources)),
                   'independence_caveat': INDEPENDENCE_CAVEAT, 'exhaustive': False,
                   'search_text_used_as_evidence': False}
        enriched = {**copy.deepcopy(seed), 'sources': sources, 'research_context': context,
                    'verification_context': {'claims': [], 'default_source_status': 'uncorroborated',
                                             'default_claim_status': 'unknown'}}
        private_json(artifacts / 'enriched-seed.json', enriched)
        return enriched

    def _search(self, query, scope, request_id, artifacts):
        lease = self.budget.reserve('research', request_id)
        started, captured = time.monotonic(), {}

        def on_request(prepared):
            captured['request'] = True
            private_json(artifacts / 'search-request.json', {
                'lease_id': lease, 'request_id': request_id, 'request_headers': 'omitted', **prepared})

        def on_response(received):
            raw = received['raw']
            captured.update(response=True, facts=_safe_facts(received.get('response_facts')))
            private_json(artifacts / 'search-response.json', {
                'lease_id': lease, 'elapsed_ms': round((time.monotonic() - started) * 1000),
                'response_facts': captured['facts'], 'body_utf8': raw.decode('utf-8', errors='replace'),
                'body_base64': base64.b64encode(raw).decode('ascii')})

        try:
            response = self.searcher(query, deadline=time.monotonic() + public_search.SEARCH_SECONDS,
                                     parameters=scope, on_request=on_request, on_response=on_response)
        except Exception as error:
            facts = _safe_facts(getattr(error, 'usage', {}))
            elapsed = round((time.monotonic() - started) * 1000)
            private_json(artifacts / 'search-error.json', {
                'lease_id': lease, 'error_type': type(error).__name__, 'elapsed_ms': elapsed,
                'response_facts': facts, 'exact_response_captured': bool(captured.get('response'))})
            self.budget.settle(lease, status='unknown', error=type(error).__name__,
                               elapsed_ms=elapsed, provider_request_id=facts.get('provider_request_id'))
            raise ResearchFailed('Research failed; comparison model was not called') from error
        return response, self._settle_search(response, lease, captured, started, artifacts)

    def _settle_search(self, response, lease, captured, started, artifacts):
        request_priced = isinstance(response, DeepSeekSearchResponse)
        usage = ({'requests': 1} if request_priced else
                 _usage(response) if isinstance(response, dict) else None)
        provider_id = captured.get('facts', {}).get('provider_request_id')
        if not provider_id and isinstance(response, dict):
            provider_id = response.get('responseId')
        status = response.status if request_priced else 'response_received'
        if usage is None:
            status = 'usage_unknown'
        elif not captured.get('request') or not captured.get('response'):
            status = 'audit_incomplete'
        elif not request_priced and response.get('error'):
            status = 'api_error'
        receipt = {'lease_id': lease, 'status': status, 'usage': usage,
                   'provider_request_id': provider_id, 'elapsed_ms': round((time.monotonic() - started) * 1000),
                   'request_artifact': str(artifacts / 'search-request.json'),
                   'response_artifact': str(artifacts / 'search-response.json')}
        if request_priced:
            receipt.update(usage_basis='requests', token_usage_status='not_applicable',
                           provider_request_id_capture_status=(
                               'captured' if provider_id else 'not_captured'))
        private_json(artifacts / 'search-receipt.json', receipt)
        self.budget.settle(lease, status=status, usage=usage, provider_request_id=provider_id,
                           elapsed_ms=receipt['elapsed_ms'])
        if status != 'response_received':
            raise ResearchFailed('Research accounting or API failed; comparison model was not called')
        return receipt

    def _read(self, url, index, artifacts, deadline):
        path = artifacts / f'original-{index + 1}.json'
        try:
            document = self.reader(url, deadline=deadline)
        except Exception as error:
            document = {'url': url, 'status': 'unavailable', 'reason': 'reader_error',
                        'error_type': type(error).__name__, 'text': ''}
        if not isinstance(document, dict):
            document = {'url': url, 'status': 'unavailable', 'reason': 'invalid_reader_result', 'text': ''}
        private_json(path, document)
        receipt = {key: document[key] for key in ('status', 'reason', 'http_status', 'fetched_at',
                   'body_sha256', 'truncated', 'method', 'redirect_chain', 'verification',
                   'source_provenance') if key in document}
        receipt.update(requested_url=url, artifact_path=str(path))
        text = document.get('text')
        if document.get('status') != 'read' or not isinstance(text, str) or not text.strip():
            return None, receipt
        source_url = document.get('url') or url
        complete = (not document.get('truncated', False) and len(text) <= MAX_EXCERPT_CHARS
                    and document.get('content_complete') is not False)
        source = {'id': 'research-' + hashlib.sha256(source_url.encode()).hexdigest()[:16],
                  'title': document.get('title') or urlsplit(source_url).hostname,
                  'url': source_url, 'content': text[:MAX_EXCERPT_CHARS],
                  'source_origin': 'direct_http_read', 'original_read_status': 'read',
                  'factual_status': 'uncorroborated', 'independence_status': 'unassessed',
                  'retrieved_at': document.get('fetched_at'), 'body_sha256': document.get('body_sha256'),
                  'content_complete': complete, 'original_artifact': str(path),
                  'content_scope': ('complete returned visible text; not an exhaustive source inventory' if complete
                                    else 'incomplete excerpt; omitted content may change interpretation')}
        if document.get('factual_status') == 'social_unverified':
            source.update(factual_status='social_unverified',
                          verification=copy.deepcopy(document.get('verification')),
                          source_provenance=copy.deepcopy(document.get('source_provenance')))
        return source, receipt
